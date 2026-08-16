from __future__ import annotations

import json
import re
from typing import Any

from .schemas import BlogOutputError, VisionOutputError

REQUIRED_KEYS = {
    "summary",
    "scene",
    "subjects",
    "visible_text",
    "colors_and_composition",
    "blog_worthy_details",
    "uncertainties",
    "privacy_notes",
    "raw_caption",
}

PLACEHOLDER_VALUES = {
    "사진 전체 요약",
    "관찰 대상",
    "외형과 상태",
    "화면 내 위치",
    "관찰 가능한 조명 특성",
    "보이는 경우에만",
    "관찰된 문자",
    "문자의 위치",
}


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else stripped


def extract_json_object(text: str) -> str:
    stripped = strip_code_fence(text)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise VisionOutputError("VLM output did not contain a JSON object")
    return stripped[start : end + 1]


def parse_vision_json(text: str) -> dict[str, Any]:
    raw = extract_json_object(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VisionOutputError(f"VLM JSON parse failed: {exc}") from exc
    validate_vision_json(data)
    return data


def validate_vision_json(data: Any) -> None:
    if not isinstance(data, dict):
        raise VisionOutputError("VLM JSON root must be an object")
    missing = REQUIRED_KEYS - set(data)
    if missing:
        raise VisionOutputError(f"VLM JSON is missing required keys: {', '.join(sorted(missing))}")
    if not isinstance(data["summary"], str):
        raise VisionOutputError("summary must be a string")
    if not data["summary"].strip():
        raise VisionOutputError("summary must not be empty")
    if not isinstance(data["scene"], dict):
        raise VisionOutputError("scene must be an object")
    for key in ("subjects", "visible_text", "blog_worthy_details", "uncertainties", "privacy_notes"):
        if not isinstance(data[key], list):
            raise VisionOutputError(f"{key} must be a list")
    if not isinstance(data["colors_and_composition"], dict):
        raise VisionOutputError("colors_and_composition must be an object")
    if not isinstance(data["raw_caption"], str):
        raise VisionOutputError("raw_caption must be a string")
    serialized = json.dumps(data, ensure_ascii=False)
    copied = sorted(value for value in PLACEHOLDER_VALUES if value in serialized)
    if copied:
        raise VisionOutputError(f"VLM copied schema placeholders: {', '.join(copied)}")
    if not data["subjects"]:
        raise VisionOutputError("subjects must contain at least one observed subject")


def clean_blog_markdown(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:markdown|md)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    stripped = re.sub(r"\s*\[end of text\]\s*$", "", stripped, flags=re.IGNORECASE)
    return stripped.strip() + "\n"


def validate_blog_markdown(text: str) -> None:
    required = (
        "## 사진에서 가장 먼저 눈에 들어오는 것",
        "## 장면을 자세히 살펴보면",
        "## 사진이 전하는 분위기",
        "## 마무리",
        "**이미지 대체 텍스트:**",
        "**메타 설명:**",
        "**추천 태그:**",
    )
    missing = [marker for marker in required if marker not in text]
    if missing:
        raise BlogOutputError(f"LLM output is incomplete; missing: {', '.join(missing)}")
    if len(text.strip()) < 900:
        raise BlogOutputError("LLM output is too short")
    if any(marker in text for marker in ("<vision_data>", "<user_blog_preferences>", "시스템 프롬프트", "<think>")):
        raise BlogOutputError("LLM output leaked prompt or reasoning text")
