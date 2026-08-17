"""STAGE_1 — 이미지 전처리.

원본은 절대 수정하지 않는다. inbox/<job_id>/ 는 읽기 전용으로 취급하고
work/<job_id>/images/ 에만 사본을 만든다.

사본 2벌:
  <slug>-NN.vlm.jpg   VLM 입력용 (긴 변 768 px, 실측 기준)
  <slug>-NN.webp      게시 출력용 (긴 변 1600 px)
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .job_state import ImageStatus, JobImage
from .privacy import PrivacyReport, RuleBasedPrivacyScanner, exif_is_stripped
from .schemas import AgentError

LOG = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 80_000_000

_HEIF_STATUS = "not_attempted"


def enable_heif() -> str:
    """HEIC 지원을 켠다. pillow-heif가 없으면 사유를 돌려주고 해당 파일은 건너뛴다."""
    global _HEIF_STATUS
    if _HEIF_STATUS != "not_attempted":
        return _HEIF_STATUS
    try:
        import pillow_heif  # noqa: PLC0415 - 선택적 의존성

        pillow_heif.register_heif_opener()
        _HEIF_STATUS = "enabled"
    except ImportError as exc:
        _HEIF_STATUS = f"disabled: pillow-heif 미설치 ({exc})"
    except Exception as exc:  # noqa: BLE001
        _HEIF_STATUS = f"disabled: {exc}"
    return _HEIF_STATUS


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_set_hash(sha_list: list[str]) -> str:
    """이미지 집합의 정렬 해시. 같은 사진 묶음의 재작업을 감지하는 데 쓴다."""
    joined = "\n".join(sorted(sha_list))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class PrepOutcome:
    images: list[JobImage]
    skipped: list[dict[str, Any]]
    heif_status: str
    detector_status: dict[str, str]


def _save_copy(image: Image.Image, destination: Path, max_edge: int, fmt: str, quality: int) -> tuple[int, int]:
    work = image.copy()
    width, height = work.size
    longest = max(width, height)
    if longest > max_edge:
        scale = max_edge / longest
        work = work.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
    destination.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs: dict[str, Any] = {"quality": quality}
    if fmt.upper() == "JPEG":
        save_kwargs["optimize"] = True
    elif fmt.upper() == "WEBP":
        save_kwargs["method"] = 4
    # exif를 넘기지 않는 것이 EXIF 제거의 실질이다. 제거 여부는 뒤에서 다시 검증한다.
    work.save(destination, format=fmt.upper(), **save_kwargs)
    return work.size


def collect_inbox_images(inbox_dir: Path, allowed_suffixes: list[str]) -> list[Path]:
    if not inbox_dir.exists():
        raise AgentError(f"입력 폴더가 없습니다: {inbox_dir}")
    allowed = {s.lower() for s in allowed_suffixes}
    files = [p for p in sorted(inbox_dir.iterdir()) if p.is_file() and p.suffix.lower() in allowed]
    return files


def prepare_images(
    inbox_dir: Path,
    work_images_dir: Path,
    slug_base: str,
    image_config: dict[str, Any],
    privacy_config: dict[str, Any],
    face_model_path: Path | None,
    vlm_max_edge: int,
    progress_cb=None,
) -> PrepOutcome:
    heif_status = enable_heif()
    allowed = list(image_config.get("allowed_suffixes", [".jpg", ".jpeg", ".png", ".webp"]))
    max_images = int(image_config.get("max_images_per_job", 12))
    publish_edge = int(image_config.get("publish_max_edge", 1600))
    publish_format = str(image_config.get("publish_format", "WEBP"))
    publish_quality = int(image_config.get("publish_quality", 82))
    vlm_format = str(image_config.get("vlm_format", "JPEG"))
    vlm_quality = int(image_config.get("vlm_quality", 88))

    scanner = RuleBasedPrivacyScanner(privacy_config, face_model_path)

    candidates = collect_inbox_images(inbox_dir, allowed)
    if not candidates:
        raise AgentError(f"처리할 이미지가 없습니다: {inbox_dir}")

    images: list[JobImage] = []
    skipped: list[dict[str, Any]] = []
    seen_sha: dict[str, str] = {}
    index = 0

    for source in candidates:
        if len(images) >= max_images:
            skipped.append({"file": source.name, "reason": f"job당 최대 {max_images}장을 넘었습니다"})
            continue

        if source.suffix.lower() in {".heic", ".heif"} and heif_status != "enabled":
            skipped.append({"file": source.name, "reason": f"HEIC 처리 불가 ({heif_status})"})
            continue

        try:
            sha = sha256_file(source)
        except OSError as exc:
            skipped.append({"file": source.name, "reason": f"읽기 실패: {exc}"})
            continue

        if sha in seen_sha:
            skipped.append({"file": source.name, "reason": f"중복 이미지 (동일 sha256: {seen_sha[sha]})"})
            continue

        index += 1
        slug = f"{slug_base}-{index:02d}"
        vlm_path = work_images_dir / f"{slug}.vlm.jpg"
        publish_path = work_images_dir / f"{slug}.{publish_format.lower()}"

        try:
            with Image.open(source) as opened:
                opened.verify()
            with Image.open(source) as opened:
                normalized = ImageOps.exif_transpose(opened)
                if normalized.mode not in ("RGB", "L"):
                    normalized = normalized.convert("RGB")
                elif normalized.mode == "L":
                    normalized = normalized.convert("RGB")
                _save_copy(normalized, vlm_path, vlm_max_edge, vlm_format, vlm_quality)
                pub_w, pub_h = _save_copy(normalized, publish_path, publish_edge, publish_format, publish_quality)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            index -= 1
            skipped.append({"file": source.name, "reason": f"이미지 처리 실패: {exc}"})
            continue

        # EXIF 제거 검증. 실패하면 통과시키지 않는다.
        for produced in (vlm_path, publish_path):
            stripped, detail = exif_is_stripped(produced)
            if not stripped:
                LOG.warning("EXIF 제거 검증 실패: %s (%s)", produced.name, detail)

        seen_sha[sha] = source.name
        report: PrivacyReport = scanner.scan(source, publish_path)
        status = ImageStatus.PRIVACY_HOLD.value if report.hold else ImageStatus.OK.value

        images.append(
            JobImage(
                file=source.name,
                sha256=sha,
                status=status,
                slug=slug,
                vlm_image=vlm_path.name,
                publish_image=publish_path.name,
                width=pub_w,
                height=pub_h,
                privacy_flags=report.to_dict(),
                privacy_reasons=report.reasons(),
            )
        )
        if progress_cb is not None:
            progress_cb(len(images), len(candidates), source.name)

    if not images:
        raise AgentError(f"사용 가능한 이미지가 하나도 없습니다. 건너뛴 사유: {skipped}")

    return PrepOutcome(
        images=images,
        skipped=skipped,
        heif_status=heif_status,
        detector_status=dict(images[0].privacy_flags.get("detector_status", {})),
    )
