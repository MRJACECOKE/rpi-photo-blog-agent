"""STAGE_6 — 산출물 조립.

output/<job_id>/<slug>.txt  +  <slug>.meta.json  +  images/
PRIVACY_HOLD 이미지는 복사하지도, 참조하지도 않는다.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .job_state import KST, Job, slugify
from .metrics import atomic_write_json, atomic_write_text
from .settings import Settings
from .writer_stage import WriteOutcome

LOG = logging.getLogger(__name__)


@dataclass
class AssembledOutput:
    slug: str
    document: str
    body: str
    image_entries: list[dict[str, str]] = field(default_factory=list)
    txt_path: Path | None = None
    meta_path: Path | None = None
    images_dir: Path | None = None


def build_alt_text(job_category: str, vision: dict[str, Any], index: int) -> str:
    """이미지 ALT는 확인된 관찰만으로 만든다. 없는 사실을 채우지 않는다."""
    parts: list[str] = []
    for key in ("color_tone",):
        values = vision.get(key) or []
        if values:
            parts.append(values[0])
    for key in ("door_style", "layout"):
        value = vision.get(key)
        if value:
            parts.append(value)
    subject = vision.get("furniture_type") or job_category
    prefix = " ".join(parts).strip()
    alt = f"{prefix} {subject}".strip() if prefix else str(subject)
    return re.sub(r"\s+", " ", alt)[:90] or f"{job_category} 참고 이미지 {index}"


def choose_slug(title: str, category_slugs: dict[str, str], category: str, job_id: str) -> str:
    base = category_slugs.get(category, "furniture")
    hint = slugify(title, fallback="")
    stamp = job_id.split("-")[0] if "-" in job_id else job_id
    return f"{base}-{hint}" if hint else f"{base}-{stamp}"


def assemble(
    job: Job,
    settings: Settings,
    outcome: WriteOutcome,
    per_image_vision: dict[str, dict[str, Any]],
    logger: logging.Logger | None = None,
) -> AssembledOutput:
    log = logger or LOG
    output_cfg = settings.blog.get("output", {})
    rule = str(output_cfg.get("header_rule", "=" * 48))
    caption = str(output_cfg.get("image_caption", "참고 이미지"))
    source_line = str(output_cfg.get("source_line", "사용자 제공 사진"))
    category = job.category or settings.blog.get("default_category", "부엌가구")
    category_slugs = settings.blog.get("category_slugs", {})

    title = outcome.title.strip() or f"{category} 사진으로 정리한 구성 기록"
    slug = choose_slug(title, category_slugs, category, job.job_id)

    image_entries: list[dict[str, str]] = []
    for index, image in enumerate(job.usable_images(), start=1):
        vision = per_image_vision.get(image.slug, {})
        image_entries.append(
            {
                "file": image.publish_image,
                "caption": caption,
                "alt": build_alt_text(category, vision, index),
                "sha256": image.sha256,
                "width": str(image.width),
                "height": str(image.height),
            }
        )

    body = outcome.body_text(settings.sections)
    created = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    tags = ", ".join(outcome.tags) if outcome.tags else category

    header_lines = [
        rule,
        f"TITLE: {title}",
        f"DATE: {created}",
        f"CATEGORY: {category}",
        f"TAGS: {tags}",
        f"SUMMARY: {outcome.summary.strip()}",
        "IMAGES:",
    ]
    for entry in image_entries:
        header_lines.append(f"  - {entry['file']} | {entry['caption']} | ALT: {entry['alt']}")
    if job.held_images():
        header_lines.append(
            f"NOTE: 개인정보 보류로 본문에서 제외된 사진 {len(job.held_images())}장이 있습니다."
        )
    header_lines.append(f"SOURCE: {source_line}")
    header_lines.append(rule)

    document = "\n".join(header_lines) + "\n\n" + body
    log.info("산출물 조립 완료: slug=%s, 이미지 %d장", slug, len(image_entries))
    return AssembledOutput(slug=slug, document=document, body=body, image_entries=image_entries)


def write_output(
    job: Job,
    settings: Settings,
    assembled: AssembledOutput,
    outcome: WriteOutcome,
    work_images_dir: Path,
    quality: dict[str, Any],
    extra_meta: dict[str, Any],
) -> AssembledOutput:
    output_dir = settings.output_dir / job.job_id
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # 이미 있는 산출 이미지 중 이번에 참조하지 않는 것은 정리한다 (재실행 시 1:1 대응 유지).
    referenced = {entry["file"] for entry in assembled.image_entries}
    for existing in images_dir.glob("*"):
        if existing.is_file() and existing.name not in referenced:
            existing.unlink()

    for entry in assembled.image_entries:
        source = work_images_dir / entry["file"]
        if not source.exists():
            raise FileNotFoundError(f"게시용 이미지가 없습니다: {source}")
        shutil.copy2(source, images_dir / entry["file"])

    txt_path = output_dir / f"{assembled.slug}.txt"
    meta_path = output_dir / f"{assembled.slug}.meta.json"
    atomic_write_text(txt_path, assembled.document)

    meta = {
        "job_id": job.job_id,
        "title": outcome.title,
        "slug": assembled.slug,
        "category": job.category,
        "topic_hint": job.topic_hint,
        "tags": outcome.tags,
        "summary": outcome.summary,
        "created_at_kst": datetime.now(KST).replace(microsecond=0).isoformat(),
        "images": assembled.image_entries,
        "privacy_hold_images": [
            {"file": image.file, "reasons": image.privacy_reasons} for image in job.held_images()
        ],
        "models": job.models_used,
        "generation": {
            "total_seconds": outcome.total_seconds,
            "total_tokens": outcome.total_tokens,
            "sections": [
                {"id": s.id, "seconds": s.seconds, "tokens": s.tokens, "attempts": s.attempts}
                for s in outcome.sections
            ],
        },
        "body_chars": len(assembled.body),
        "quality": quality,
        **extra_meta,
    }
    atomic_write_json(meta_path, meta)

    assembled.txt_path = txt_path
    assembled.meta_path = meta_path
    assembled.images_dir = images_dir
    return assembled
