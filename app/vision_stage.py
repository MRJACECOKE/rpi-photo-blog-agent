"""STAGE_2 — VLM 사진 분석.

이미지 1장씩 순차 처리한다. 배치하지 않는다 (RAM 안정성 우선).
실패하면 해상도를 낮춰 1회 재시도하고, 그래도 실패하면 그 이미지만 건너뛰고 나머지를 계속한다.
전체를 중단하지 않는다.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from .job_state import ImageStatus, Job, JobStore, Stage
from .memory_guard import MemoryGuard
from .metrics import atomic_write_json, atomic_write_text
from .privacy import merge_vlm_privacy_flags
from .runtime import MtmdVisionRuntime
from .schemas import AgentError, VisionOutputError
from .settings import Settings

LOG = logging.getLogger(__name__)

REQUIRED_KEYS = (
    "furniture_type", "space", "layout", "door_style", "color_tone",
    "countertop_look", "hardware_visible", "storage_features", "sink_features",
    "lighting", "notable_points", "privacy_flags", "confidence", "uncertain",
)

ENUMS: dict[str, set[str]] = {
    "space": {"주방", "현관", "거실", "세탁실", "욕실", "기타"},
    "layout": {"일자형", "ㄱ자형", "ㄷ자형", "아일랜드", "대면형"},
    "door_style": {"민무늬 무광", "도장", "필름", "원목무늬", "유리"},
    "countertop_look": {"인조대리석 느낌", "세라믹 느낌", "원목 느낌", "스테인리스 느낌"},
    "lighting": {"하부 간접조명", "다운라이트", "자연광", "혼합"},
}

# 프롬프트 지시문을 값으로 베낀 경우를 잡는다. bench에서 실제로 관측된 실패다.
PLACEHOLDER_MARKERS = (
    "40자", "한국어 문장", "Korean sentence", "exactly one of", "array of",
    "under 40", "such as", "or null", "값을", "예시",
)

LOW_CONFIDENCE_LABEL = "확인되지 않음"


@dataclass
class VisionOutcome:
    analyzed: int = 0
    skipped: int = 0
    newly_held: int = 0
    released: int = 0
    resumed: int = 0
    per_image_seconds: list[float] = field(default_factory=list)
    peak_rss_mb: float = 0.0
    notes: list[str] = field(default_factory=list)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return stripped


def extract_json_object(text: str) -> str:
    stripped = _strip_fence(text)
    start = stripped.find("{")
    if start == -1:
        raise VisionOutputError("VLM 출력에 JSON 객체가 없습니다")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : index + 1]
    raise VisionOutputError("VLM JSON 객체가 닫히지 않았습니다 (출력이 잘렸을 수 있습니다)")


def _dedupe_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _contains_placeholder(value: Any) -> str | None:
    serialized = json.dumps(value, ensure_ascii=False)
    for marker in PLACEHOLDER_MARKERS:
        if marker in serialized:
            return marker
    return None


def _drop_color_only(values: list[str], color_terms: set[str]) -> list[str]:
    """색상 단어만으로 된 항목을 버린다.

    3B VLM이 hardware_visible / storage_features / sink_features에 색상을 넣는 일이 잦다.
    색상은 color_tone에 이미 있으므로 여기서 버려도 정보가 사라지지 않는다.
    """
    if not color_terms:
        return values
    kept = []
    for value in values:
        stripped = value.strip().strip("의 ")
        if stripped in color_terms:
            continue
        kept.append(value)
    return kept


def normalize_vision_json(raw: dict[str, Any], color_terms: set[str] | None = None) -> dict[str, Any]:
    """스키마를 강제하고, 모델이 흘린 잡음을 정리한다. 없는 사실을 채워 넣지는 않는다."""
    if not isinstance(raw, dict):
        raise VisionOutputError("VLM JSON 최상위는 객체여야 합니다")

    missing = [key for key in REQUIRED_KEYS if key not in raw]
    if missing:
        raise VisionOutputError(f"VLM JSON에 필수 키가 없습니다: {', '.join(missing)}")

    data: dict[str, Any] = {}

    furniture_type = raw.get("furniture_type")
    if not isinstance(furniture_type, str) or not furniture_type.strip():
        raise VisionOutputError("furniture_type이 비어 있습니다")
    data["furniture_type"] = furniture_type.strip()

    for key, allowed in ENUMS.items():
        value = raw.get(key)
        if isinstance(value, str) and value.strip() in allowed:
            data[key] = value.strip()
        else:
            # 열거값 밖의 값은 사실로 인정하지 않는다.
            data[key] = None

    colors = color_terms or set()
    data["color_tone"] = _dedupe_strings(raw.get("color_tone"))[:3]
    data["hardware_visible"] = _drop_color_only(_dedupe_strings(raw.get("hardware_visible")), colors)[:6]
    data["storage_features"] = _drop_color_only(_dedupe_strings(raw.get("storage_features")), colors)[:8]
    data["sink_features"] = _drop_color_only(_dedupe_strings(raw.get("sink_features")), colors)[:6]

    notable = [point for point in _dedupe_strings(raw.get("notable_points")) if len(point) <= 80]
    if len(notable) < 2:
        raise VisionOutputError(f"notable_points가 부족합니다 (중복 제거 후 {len(notable)}개)")
    data["notable_points"] = notable[:6]

    flags_raw = raw.get("privacy_flags")
    if not isinstance(flags_raw, dict):
        raise VisionOutputError("privacy_flags가 객체가 아닙니다")
    data["privacy_flags"] = {key: bool(flags_raw.get(key, False)) for key in ("face", "text_pii", "plate", "signage")}

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    data["confidence"] = max(0.0, min(1.0, confidence))

    data["uncertain"] = _dedupe_strings(raw.get("uncertain"))[:8]

    marker = _contains_placeholder({k: v for k, v in data.items() if k != "uncertain"})
    if marker:
        raise VisionOutputError(f"VLM이 프롬프트 지시문을 값으로 복사했습니다: '{marker}'")

    return data


def parse_vision_output(text: str, color_terms: set[str] | None = None) -> dict[str, Any]:
    raw_json = extract_json_object(text)
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise VisionOutputError(f"VLM JSON 파싱 실패: {exc}") from exc
    return normalize_vision_json(parsed, color_terms)


def apply_confidence_policy(data: dict[str, Any], threshold: float = 0.5) -> dict[str, Any]:
    """confidence가 임계값 미만이면 단정형 필드를 '확인되지 않음'으로 낮춘다."""
    if data.get("confidence", 0.0) >= threshold:
        return data
    lowered = dict(data)
    for key in ("space", "layout", "door_style", "countertop_look", "lighting"):
        if lowered.get(key) is not None:
            lowered[key] = None
    reason = f"confidence {data.get('confidence', 0.0):.2f} < {threshold}이라 분류 항목을 {LOW_CONFIDENCE_LABEL}으로 처리"
    lowered["uncertain"] = list(dict.fromkeys([*lowered.get("uncertain", []), reason]))
    lowered["confidence_policy_applied"] = True
    return lowered


def _downscale(source: Path, destination: Path, max_edge: int) -> None:
    with Image.open(source) as image:
        converted = image.convert("RGB")
        width, height = converted.size
        longest = max(width, height)
        if longest > max_edge:
            scale = max_edge / longest
            converted = converted.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
        converted.save(destination, format="JPEG", quality=88, optimize=True)


def run_vision_stage(
    job: Job,
    store: JobStore,
    settings: Settings,
    runtime: MtmdVisionRuntime,
    guard: MemoryGuard,
    work_dir: Path,
    prompts: dict[str, str],
    logger: logging.Logger | None = None,
) -> VisionOutcome:
    log = logger or LOG
    vision_dir = work_dir / "vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    images_dir = work_dir / "images"
    logs_dir = work_dir / "vlm_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = work_dir / "vision_prompt.txt"
    system_prompt = prompts.get("furniture_vision_system")
    if not system_prompt:
        raise AgentError("config/prompts.yaml에 furniture_vision_system이 없습니다")
    atomic_write_text(prompt_path, system_prompt.strip() + "\n")

    timeout = float(settings.timeouts.get("vlm_per_image_sec", 300))
    gate_seconds = float(settings.gates.get("max_vlm_seconds_per_image", 180))
    color_terms = set(settings.blog.get("vision_normalization", {}).get("color_terms", []) or [])
    pause_above = float(settings.thermal.get("pause_above_celsius", 80.0))
    pause_seconds = float(settings.thermal.get("pause_seconds", 30))
    retry_edge = int(runtime.tier.get("retry_image_max_edge", 640))

    outcome = VisionOutcome()
    total = len(job.images)

    for index, image in enumerate(job.images, start=1):
        if image.status in (ImageStatus.SKIPPED.value, ImageStatus.DUPLICATE.value, ImageStatus.FAILED.value):
            continue

        # 확정 보류는 어떤 경우에도 해제되지 않는다. VLM에 넣어 봐야 결과를 쓸 수 없고,
        # 얼굴이 담긴 사진을 모델에 통과시킬 이유도 없다.
        if image.privacy_flags.get("confirmed_hold"):
            image.note = "개인정보 확정 보류라 VLM 분석을 건너뜁니다"
            outcome.notes.append(f"{image.slug}: {image.note}")
            log.info("%s: 확정 보류이므로 VLM 분석을 건너뜁니다", image.slug)
            continue

        # resume: 이미 분석이 끝난 사진은 다시 돌리지 않는다.
        # VISION은 이 파이프라인에서 가장 긴 스테이지라 여기서의 재개가 가장 큰 이득이다.
        if image.vision_json and (vision_dir / image.vision_json).exists():
            outcome.analyzed += 1
            outcome.resumed += 1
            log.info("%s: 기존 분석 결과를 재사용합니다 (resume)", image.slug)
            store.record_progress(job, f"사진 분석 {index}/{total}: {image.slug} (재사용)", index, total)
            continue

        store.record_progress(job, f"사진 분석 {index}/{total}: {image.slug}", index, total)

        snapshot = guard.snapshot()
        if snapshot.cpu_temp_c is not None and snapshot.cpu_temp_c > pause_above:
            log.warning("온도 %.1f°C > %.1f°C. %.0f초 대기 후 계속합니다.", snapshot.cpu_temp_c, pause_above, pause_seconds)
            outcome.notes.append(f"{image.slug}: 온도 {snapshot.cpu_temp_c:.1f}°C로 {pause_seconds:.0f}초 대기")
            time.sleep(pause_seconds)

        vlm_image_path = images_dir / image.vlm_image
        attempts: list[tuple[str, Path]] = [("full", vlm_image_path)]
        reduced_path = images_dir / f"{image.slug}.retry.jpg"
        started = time.monotonic()
        parsed: dict[str, Any] | None = None
        last_error = ""

        for attempt_index, (label, path) in enumerate(attempts, start=1):
            if label == "retry":
                try:
                    _downscale(vlm_image_path, reduced_path, retry_edge)
                except OSError as exc:
                    last_error = f"재시도 이미지 축소 실패: {exc}"
                    break
            stdout_path = logs_dir / f"{image.slug}.{label}.stdout.txt"
            stderr_path = logs_dir / f"{image.slug}.{label}.stderr.txt"
            result = runtime.analyze(path, prompt_path, stdout_path, stderr_path, timeout)
            outcome.peak_rss_mb = max(outcome.peak_rss_mb, result.peak_rss_mb)

            if result.exit_code != 0:
                last_error = f"VLM 종료 코드 {result.exit_code}" + (" (타임아웃)" if result.timed_out else "")
            else:
                try:
                    parsed = parse_vision_output(stdout_path.read_text(encoding="utf-8", errors="replace"), color_terms)
                    break
                except VisionOutputError as exc:
                    last_error = str(exc)

            if attempt_index == 1:
                store.record_retry(
                    job, Stage.VISION,
                    failure=f"{image.slug} 분석 실패: {last_error}",
                    root_cause="VLM 출력이 스키마를 만족하지 못하거나 프로세스가 실패함",
                    action=f"입력 해상도를 {retry_edge}px로 낮춰 1회 재시도",
                )
                attempts.append(("retry", reduced_path))

        elapsed = time.monotonic() - started

        if parsed is None:
            image.status = ImageStatus.SKIPPED.value
            image.note = f"VLM 분석 실패 후 건너뜀: {last_error}"
            outcome.skipped += 1
            outcome.notes.append(f"{image.slug}: {image.note}")
            log.warning("%s 건너뜀: %s", image.slug, last_error)
            store.save(job)
            continue

        parsed = apply_confidence_policy(parsed)
        parsed["_image"] = {"slug": image.slug, "file": image.publish_image, "sha256": image.sha256}
        vision_path = vision_dir / f"{image.slug}.json"
        atomic_write_json(vision_path, parsed)
        image.vision_json = vision_path.name

        merged, new_reasons, cleared = merge_vlm_privacy_flags(image.privacy_flags, parsed["privacy_flags"])
        image.privacy_flags = merged
        if new_reasons:
            image.privacy_reasons.extend(new_reasons)
        if cleared:
            image.privacy_reasons.extend(cleared)
        if merged["hold"]:
            if image.status == ImageStatus.OK.value:
                image.status = ImageStatus.PRIVACY_HOLD.value
                outcome.newly_held += 1
                log.warning("%s: VLM 2차 검사로 PRIVACY_HOLD 처리 (%s)", image.slug, "; ".join(new_reasons))
        elif image.status == ImageStatus.PRIVACY_HOLD.value:
            image.status = ImageStatus.OK.value
            outcome.released += 1
            log.info("%s: 잠정 보류를 해제했습니다 (%s)", image.slug, "; ".join(cleared))

        outcome.analyzed += 1
        outcome.per_image_seconds.append(round(elapsed, 1))
        log.info("%s 분석 완료 (%.1fs, confidence=%.2f)", image.slug, elapsed, parsed.get("confidence", 0.0))
        if elapsed > gate_seconds:
            note = f"{image.slug}: {elapsed:.0f}s로 Phase 0 게이트 {gate_seconds:.0f}s를 초과했습니다"
            outcome.notes.append(note)
            log.warning("%s", note)
        store.save(job)

        if reduced_path.exists():
            try:
                reduced_path.unlink()
            except OSError:
                pass

    if outcome.analyzed == 0:
        raise AgentError("모든 이미지의 VLM 분석이 실패했습니다. 블로그를 생성할 근거가 없습니다.")

    return outcome


def merge_vision_documents(job: Job, work_dir: Path) -> dict[str, Any]:
    """작성 단계에 넘길 통합본. 개인정보 보류 이미지는 근거에서 제외한다."""
    vision_dir = work_dir / "vision"
    photos = []
    for image in job.usable_images():
        if not image.vision_json:
            continue
        path = vision_dir / image.vision_json
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.pop("privacy_flags", None)
        photos.append(data)
    if not photos:
        raise AgentError("본문 근거로 쓸 수 있는 사진 분석 결과가 없습니다 (모두 보류되었거나 실패했습니다)")

    def union(key: str) -> list[str]:
        seen: list[str] = []
        for photo in photos:
            for value in photo.get(key, []) or []:
                if value not in seen:
                    seen.append(value)
        return seen

    def majority(key: str) -> str | None:
        counts: dict[str, int] = {}
        for photo in photos:
            value = photo.get(key)
            if isinstance(value, str) and value:
                counts[value] = counts.get(value, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda item: item[1])[0]

    return {
        "photo_count": len(photos),
        "furniture_types": list(dict.fromkeys(photo["furniture_type"] for photo in photos)),
        "space": majority("space"),
        "layout": majority("layout"),
        "door_style": majority("door_style"),
        "countertop_look": majority("countertop_look"),
        "lighting": majority("lighting"),
        "color_tone": union("color_tone"),
        "hardware_visible": union("hardware_visible"),
        "storage_features": union("storage_features"),
        "sink_features": union("sink_features"),
        "notable_points": union("notable_points"),
        "uncertain": union("uncertain"),
        "confidence_avg": round(sum(p.get("confidence", 0.0) for p in photos) / len(photos), 2),
        "photos": [
            {"slug": p.get("_image", {}).get("slug", ""), "furniture_type": p.get("furniture_type"), "notable_points": p.get("notable_points", [])}
            for p in photos
        ],
    }
