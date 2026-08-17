"""STAGE_5 — 품질 게이트.

LLM을 다시 부르지 않고 규칙 기반으로만 검사한다. 빠르고 결정적이어야 한다.
검사는 두 층이다.
  1) 섹션 검사: 작성 중에 호출돼 해당 섹션만 재생성시킨다 (전체 재생성 아님).
  2) 문서 검사: 조립된 최종 문서를 검사한다.
실패를 몰래 통과시키지 않는다. 통과하지 못하면 결과에 그대로 남긴다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .job_state import Job
from .settings import BlogSection, Settings

LOG = logging.getLogger(__name__)

HANGUL = re.compile(r"[가-힣]")
LETTERS = re.compile(r"[A-Za-z가-힣]")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")


@dataclass
class Finding:
    check: str
    severity: str  # error | warning
    message: str
    section_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "severity": self.severity, "message": self.message, "section_id": self.section_id}


@dataclass
class QualityReport:
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "error_count": len(self.errors),
            "warning_count": len(self.findings) - len(self.errors),
            "findings": [f.to_dict() for f in self.findings],
            "stats": self.stats,
        }


def hangul_ratio(text: str) -> float:
    letters = LETTERS.findall(text)
    if not letters:
        return 0.0
    hangul = HANGUL.findall(text)
    return len(hangul) / len(letters)


def sentences_of(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if len(s.strip()) >= 8]


WORD = re.compile(r"[가-힣A-Za-z0-9]+")


def trigram_self_overlap(text: str) -> float:
    """단어 3-gram 기준 자기 중복률. 같은 표현을 돌려 쓰는 글을 잡는다.

    문자 3-gram을 쓰면 한국어에서 동작하지 않는다. 조사와 어미("습니다", "되어", "있는")가
    문자 단위 반복을 지배해서, 정상적인 글도 0.20~0.34가 나온다. 실측:

        생성 본문 0.352 · 기존 검수 통과 샘플 0.199 · 사람이 쓴 기술문서 0.200~0.338

    즉 문자 기준으로는 "패딩"과 "정상 한국어"를 구분할 수 없다.
    단어 3-gram으로 바꾸면 분리된다:

        생성 본문 0.029 · 검수 샘플 0.026 · 사람이 쓴 문서 0.004 · 의도적 반복 0.966
    """
    words = WORD.findall(text)
    if len(words) < 30:
        return 0.0
    grams = [tuple(words[i : i + 3]) for i in range(len(words) - 2)]
    if not grams:
        return 0.0
    return round(1.0 - (len(set(grams)) / len(grams)), 4)


def brands_in_evidence(merged_vision: dict[str, Any]) -> str:
    import json

    return json.dumps(merged_vision, ensure_ascii=False)


def check_banned_patterns(settings: Settings, text: str, section_id: str = "") -> list[Finding]:
    findings = []
    for pattern in settings.banned_patterns:
        match = pattern.regex.search(text)
        if match:
            findings.append(
                Finding(
                    check=f"banned:{pattern.name}",
                    severity="error",
                    message=f"금칙 패턴 '{pattern.name}' 발견: '{match.group(0)[:40]}' — {pattern.reason}",
                    section_id=section_id,
                )
            )
    return findings


def check_unsupported_brands(settings: Settings, text: str, evidence: str, section_id: str = "") -> list[Finding]:
    quality = settings.blog.get("quality", {})
    whitelist = set(quality.get("brand_whitelist", []) or [])
    findings = []
    for brand in quality.get("brand_candidates", []) or []:
        if brand in whitelist:
            continue
        if brand in text and brand not in evidence:
            findings.append(
                Finding(
                    check="unsupported_brand",
                    severity="error",
                    message=f"사진 분석 결과에 없는 브랜드가 본문에 등장했습니다: '{brand}'",
                    section_id=section_id,
                )
            )
    return findings


def check_region_install_claim(settings: Settings, text: str, section_id: str = "") -> list[Finding]:
    quality = settings.blog.get("quality", {})
    findings = []
    for region in quality.get("region_names", []) or []:
        for match in re.finditer(re.escape(region), text):
            window = text[match.start() : match.start() + 40]
            if re.search(r"시공|설치|현장|납품", window):
                findings.append(
                    Finding(
                        check="region_install_claim",
                        severity="error",
                        message=f"지역명과 시공 주장이 함께 등장했습니다: '{window.strip()[:40]}'",
                        section_id=section_id,
                    )
                )
                break
    return findings


def check_repetition(settings: Settings, text: str, section_id: str = "") -> list[Finding]:
    limits = settings.quality_limits
    findings = []
    counts: dict[str, int] = {}
    for sentence in sentences_of(text):
        counts[sentence] = counts.get(sentence, 0) + 1
    max_repeat = int(limits.get("max_repeat_sentence", 2))
    for sentence, count in counts.items():
        if count > max_repeat:
            findings.append(
                Finding(
                    check="repeated_sentence",
                    severity="error",
                    message=f"같은 문장이 {count}회 반복됩니다: '{sentence[:40]}'",
                    section_id=section_id,
                )
            )
    return findings


def check_language(settings: Settings, text: str, section_id: str = "") -> list[Finding]:
    limits = settings.quality_limits
    minimum = float(limits.get("min_hangul_ratio", 0.45))
    ratio = hangul_ratio(text)
    if ratio < minimum:
        return [
            Finding(
                check="hangul_ratio",
                severity="error",
                message=f"한글 비율이 {ratio:.2f}로 기준 {minimum:.2f} 미만입니다 (모델이 언어를 이탈했을 수 있습니다)",
                section_id=section_id,
            )
        ]
    return []


def check_section_format(section: BlogSection, text: str) -> list[Finding]:
    findings = []
    if section.kind == "bullets":
        items = [line for line in text.splitlines() if line.strip().startswith("- ")]
        if section.min_items and len(items) < section.min_items:
            findings.append(
                Finding("section_format", "error", f"목록 항목이 {len(items)}개로 최소 {section.min_items}개에 못 미칩니다", section.id)
            )
    elif section.kind == "qa":
        questions = len(re.findall(r"^\s*Q[.．:]", text, flags=re.MULTILINE))
        if section.min_items and questions < section.min_items:
            findings.append(
                Finding("section_format", "error", f"질문이 {questions}개로 최소 {section.min_items}개에 못 미칩니다", section.id)
            )
    elif section.kind == "title":
        if len(text.strip().splitlines()) != 1:
            findings.append(Finding("section_format", "error", "제목은 한 줄이어야 합니다", section.id))
        if len(text.strip()) > 60:
            findings.append(Finding("section_format", "error", f"제목이 {len(text.strip())}자로 너무 깁니다", section.id))
    elif section.kind == "prose":
        if len(text.strip()) < 60:
            findings.append(Finding("section_format", "error", f"본문이 {len(text.strip())}자로 너무 짧습니다", section.id))
        if text.strip().startswith("- "):
            findings.append(Finding("section_format", "error", "문단으로 써야 할 섹션이 목록으로 작성됐습니다", section.id))
    return findings


def make_section_validator(settings: Settings, merged_vision: dict[str, Any]):
    """작성 중 섹션 재생성을 유발하는 검사기. 첫 번째 error 메시지를 돌려준다."""
    evidence = brands_in_evidence(merged_vision)

    def validate(section: BlogSection, text: str) -> str | None:
        findings: list[Finding] = []
        findings += check_section_format(section, text)
        findings += check_banned_patterns(settings, text, section.id)
        findings += check_unsupported_brands(settings, text, evidence, section.id)
        findings += check_region_install_claim(settings, text, section.id)
        findings += check_repetition(settings, text, section.id)
        findings += check_language(settings, text, section.id)
        errors = [f for f in findings if f.severity == "error"]
        return errors[0].message if errors else None

    return validate


def check_document(
    settings: Settings,
    job: Job,
    document_text: str,
    body_text: str,
    merged_vision: dict[str, Any],
    image_entries: list[dict[str, str]],
    output_images_dir: Path,
) -> QualityReport:
    """조립된 최종 문서 검사."""
    report = QualityReport()
    limits = settings.quality_limits
    evidence = brands_in_evidence(merged_vision)

    report.findings += check_banned_patterns(settings, document_text)
    report.findings += check_unsupported_brands(settings, body_text, evidence)
    report.findings += check_region_install_claim(settings, body_text)
    report.findings += check_repetition(settings, body_text)
    report.findings += check_language(settings, body_text)

    # 제목
    if "TITLE:" not in document_text:
        report.findings.append(Finding("structure", "error", "TITLE 헤더가 없습니다"))
    else:
        title_line = next((l for l in document_text.splitlines() if l.startswith("TITLE:")), "")
        if len(title_line.split("TITLE:", 1)[1].strip()) < 6:
            report.findings.append(Finding("structure", "error", "제목이 비어 있거나 너무 짧습니다"))

    # 섹션 개수
    section_titles = [s.title for s in settings.sections if s.kind != "title"]
    present = [t for t in section_titles if f"[{t}]" in body_text]
    min_sections = int(limits.get("min_sections", 10))
    if len(present) < min_sections:
        report.findings.append(
            Finding("structure", "error", f"섹션이 {len(present)}개로 최소 {min_sections}개에 못 미칩니다")
        )

    # 3-gram 자기중복률
    overlap = trigram_self_overlap(body_text)
    threshold = float(limits.get("max_trigram_self_overlap", 0.18))
    if overlap > threshold:
        report.findings.append(
            Finding("self_overlap", "error", f"3-gram 자기중복률 {overlap:.3f}이 기준 {threshold:.3f}을 넘었습니다")
        )

    # 분량
    body_chars = len(re.sub(r"\s", "", body_text))
    length = settings.blog.get("length", {})
    min_chars, max_chars = int(length.get("min_chars", 1800)), int(length.get("max_chars", 3000))
    if body_chars < min_chars:
        report.findings.append(Finding("length", "warning", f"본문이 {body_chars}자로 목표 {min_chars}자에 못 미칩니다"))
    elif body_chars > max_chars * 1.2:
        report.findings.append(Finding("length", "warning", f"본문이 {body_chars}자로 목표 상한 {max_chars}자를 크게 넘었습니다"))

    # 이미지 참조 1:1 대응과 ALT
    referenced = {entry["file"] for entry in image_entries}
    on_disk = {p.name for p in output_images_dir.glob("*") if p.is_file()} if output_images_dir.exists() else set()
    missing = referenced - on_disk
    extra = on_disk - referenced
    if missing:
        report.findings.append(Finding("images", "error", f"본문이 참조하지만 파일이 없는 이미지: {sorted(missing)}"))
    if extra:
        report.findings.append(Finding("images", "error", f"출력 폴더에 있으나 본문이 참조하지 않는 이미지: {sorted(extra)}"))
    for entry in image_entries:
        if not entry.get("alt", "").strip():
            report.findings.append(Finding("images", "error", f"ALT가 없는 이미지: {entry['file']}"))

    # 개인정보 보류 이미지가 본문/헤더에 없어야 한다
    for image in job.held_images():
        for name in (image.publish_image, image.file, image.slug):
            if name and name in document_text:
                report.findings.append(
                    Finding("privacy", "error", f"PRIVACY_HOLD 이미지가 산출물에 등장합니다: {name}")
                )
                break

    report.stats = {
        "body_chars_without_space": body_chars,
        "body_chars": len(body_text),
        "hangul_ratio": round(hangul_ratio(body_text), 3),
        "trigram_self_overlap": overlap,
        "sections_present": len(present),
        "sections_expected": len(section_titles),
        "images_referenced": len(image_entries),
        "images_on_disk": len(on_disk),
        "privacy_hold_images": len(job.held_images()),
    }
    return report
