from __future__ import annotations

import json

import pytest

from app.schemas import VisionOutputError
from app.vision_stage import (
    apply_confidence_policy, extract_json_object, normalize_vision_json, parse_vision_output,
)

GOOD = {
    "furniture_type": "주방 상부장",
    "space": "주방",
    "layout": "ㄱ자형",
    "door_style": "원목무늬",
    "color_tone": ["내추럴 오크", "웜 화이트"],
    "countertop_look": "인조대리석 느낌",
    "hardware_visible": ["슬림 바 손잡이"],
    "storage_features": ["3단 서랍"],
    "sink_features": ["1볼"],
    "lighting": "자연광",
    "notable_points": ["상부장이 벽면을 따라 이어집니다.", "창가에 싱크볼이 있습니다.", "손잡이가 가로로 길게 붙어 있습니다."],
    "privacy_flags": {"face": False, "text_pii": False, "plate": False, "signage": False},
    "confidence": 0.9,
    "uncertain": [],
}


def test_parses_json_wrapped_in_code_fence():
    text = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    assert parse_vision_output(text)["furniture_type"] == "주방 상부장"


def test_extracts_json_ignoring_trailing_prose():
    text = json.dumps(GOOD, ensure_ascii=False) + "\n\n이상입니다."
    assert json.loads(extract_json_object(text))["space"] == "주방"


def test_extracts_nested_braces_correctly():
    text = "잡담 " + json.dumps(GOOD, ensure_ascii=False)
    parsed = json.loads(extract_json_object(text))
    assert parsed["privacy_flags"]["face"] is False


def test_truncated_json_is_rejected():
    text = json.dumps(GOOD, ensure_ascii=False)[:-20]
    with pytest.raises(VisionOutputError, match="닫히지"):
        extract_json_object(text)


def test_enum_values_outside_allowed_set_become_null():
    data = {**GOOD, "layout": "U-shaped", "space": "kitchen"}
    normalized = normalize_vision_json(data)
    assert normalized["layout"] is None
    assert normalized["space"] is None


def test_duplicate_notable_points_are_removed():
    """bench 실측에서 실제로 관측된 실패다."""
    data = {**GOOD, "notable_points": ["화이트 타일 벽지가 있습니다."] * 4 + ["창가에 싱크볼이 있습니다."]}
    normalized = normalize_vision_json(data)
    assert normalized["notable_points"] == ["화이트 타일 벽지가 있습니다.", "창가에 싱크볼이 있습니다."]


def test_copied_prompt_placeholder_is_rejected():
    """bench 실측: 예시 JSON을 주면 모델이 지시문을 값으로 베꼈다."""
    data = {**GOOD, "notable_points": ["40자 이내 한국어 문장", "두 번째", "세 번째"]}
    with pytest.raises(VisionOutputError, match="복사"):
        normalize_vision_json(data)


def test_missing_required_key_is_rejected():
    data = {k: v for k, v in GOOD.items() if k != "privacy_flags"}
    with pytest.raises(VisionOutputError, match="필수 키"):
        normalize_vision_json(data)


def test_too_few_notable_points_is_rejected():
    data = {**GOOD, "notable_points": ["하나뿐입니다."]}
    with pytest.raises(VisionOutputError, match="부족"):
        normalize_vision_json(data)


def test_confidence_is_clamped():
    assert normalize_vision_json({**GOOD, "confidence": 7})["confidence"] == 1.0
    assert normalize_vision_json({**GOOD, "confidence": "이상한값"})["confidence"] == 0.0


def test_low_confidence_downgrades_classification_fields():
    normalized = normalize_vision_json({**GOOD, "confidence": 0.3})
    lowered = apply_confidence_policy(normalized, threshold=0.5)
    assert lowered["layout"] is None
    assert lowered["door_style"] is None
    # 관찰 목록 자체는 지운다고 사실이 되지 않으므로 유지한다
    assert lowered["notable_points"]
    assert any("확인되지 않음" in reason for reason in lowered["uncertain"])


def test_high_confidence_is_untouched():
    normalized = normalize_vision_json(GOOD)
    assert apply_confidence_policy(normalized, threshold=0.5)["layout"] == "ㄱ자형"


COLORS = {"화이트", "청록색", "갈색", "베이지색"}


def test_color_words_are_dropped_from_hardware_and_storage_fields():
    """실측: 3B VLM이 색상 단어를 하드웨어/수납 필드에 넣었다."""
    data = {
        **GOOD,
        "hardware_visible": ["화이트", "메탈 핸들"],
        "storage_features": ["화이트", "상부장", "하부장"],
        "sink_features": ["화이트", "싱크대"],
    }
    normalized = normalize_vision_json(data, COLORS)
    assert normalized["hardware_visible"] == ["메탈 핸들"]
    assert normalized["storage_features"] == ["상부장", "하부장"]
    assert normalized["sink_features"] == ["싱크대"]


def test_color_tone_itself_is_not_filtered():
    data = {**GOOD, "color_tone": ["화이트", "청록색"]}
    assert normalize_vision_json(data, COLORS)["color_tone"] == ["화이트", "청록색"]


def test_compound_terms_containing_a_color_are_kept():
    data = {**GOOD, "hardware_visible": ["화이트 슬림 바 손잡이"]}
    assert normalize_vision_json(data, COLORS)["hardware_visible"] == ["화이트 슬림 바 손잡이"]


def test_without_color_terms_nothing_is_dropped():
    data = {**GOOD, "hardware_visible": ["화이트", "메탈 핸들"]}
    assert normalize_vision_json(data)["hardware_visible"] == ["화이트", "메탈 핸들"]
