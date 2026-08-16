from __future__ import annotations

import json

import pytest

from app.output_parser import clean_blog_markdown, extract_json_object, parse_vision_json, validate_blog_markdown
from app.schemas import BlogOutputError, VisionOutputError


def valid_vision_payload() -> dict[str, object]:
    return {
        "summary": "A blue test image",
        "scene": {"location_type": "unknown", "time_or_lighting": "bright", "weather": ""},
        "subjects": [],
        "visible_text": [],
        "colors_and_composition": {"dominant_colors": ["blue"], "composition": "simple", "mood_from_visuals": "calm"},
        "blog_worthy_details": ["color"],
        "uncertainties": [],
        "privacy_notes": [],
        "raw_caption": "A blue test image",
    }


def test_extract_json_object_ignores_surrounding_model_text() -> None:
    payload = json.dumps(valid_vision_payload())

    assert extract_json_object(f"Here is the JSON:\n{payload}\nDone.") == payload


def test_parse_vision_json_rejects_wrong_field_types() -> None:
    payload = valid_vision_payload()
    payload["subjects"] = "not a list"

    with pytest.raises(VisionOutputError, match="subjects must be a list"):
        parse_vision_json(json.dumps(payload))


def test_parse_vision_json_reports_missing_object() -> None:
    with pytest.raises(VisionOutputError, match="did not contain a JSON object"):
        parse_vision_json("no structured output")


def test_clean_blog_markdown_removes_markdown_fence_and_normalizes_newline() -> None:
    cleaned = clean_blog_markdown("```markdown\n# Title\n\nBody\n```")

    assert cleaned == "# Title\n\nBody\n"


def test_incomplete_blog_is_rejected() -> None:
    with pytest.raises(BlogOutputError, match="incomplete"):
        validate_blog_markdown("# 잘린 글\n\n## 마무리\n\n문장이 끝나지")


def test_prompt_leakage_is_rejected() -> None:
    text = (
        "# 제목\n\n" + "본문 " * 500 + "\n\n"
        "## 사진에서 가장 먼저 눈에 들어오는 것\n본문\n"
        "## 장면을 자세히 살펴보면\n본문\n"
        "## 사진이 전하는 분위기\n본문\n"
        "## 마무리\n본문\n"
        "**이미지 대체 텍스트:** 설명\n"
        "**메타 설명:** 설명\n"
        "**추천 태그:** 사진\n<vision_data>"
    )
    with pytest.raises(BlogOutputError, match="leaked"):
        validate_blog_markdown(text)
