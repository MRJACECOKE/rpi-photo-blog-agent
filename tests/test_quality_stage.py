from __future__ import annotations

from pathlib import Path

import pytest

from app.job_state import ImageStatus, Job, JobImage, Stage, StageStatus
from app.quality_stage import (
    check_banned_patterns, check_document, check_language, check_region_install_claim,
    check_repetition, check_section_format, check_unsupported_brands, hangul_ratio,
    make_section_validator, trigram_self_overlap,
)
from app.settings import BlogSection, Settings

MERGED_VISION = {
    "furniture_types": ["주방 상부장"],
    "layout": "ㄱ자형",
    "color_tone": ["내추럴 오크"],
    "notable_points": ["상부장이 벽면을 따라 이어집니다."],
}


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load(Path(__file__).resolve().parent.parent)


def test_price_mention_is_blocked(settings):
    findings = check_banned_patterns(settings, "이 구성은 150만원 정도입니다.")
    assert any(f.check == "banned:price" for f in findings)


def test_phone_number_is_blocked(settings):
    findings = check_banned_patterns(settings, "문의는 010-1234-5678로 주세요.")
    assert any(f.check == "banned:phone" for f in findings)


def test_installation_claim_is_blocked(settings):
    findings = check_banned_patterns(settings, "저희가 시공한 현장입니다.")
    assert any(f.check == "banned:install_claim" for f in findings)


def test_dimension_claim_is_blocked(settings):
    findings = check_banned_patterns(settings, "상부장 높이는 720mm입니다.")
    assert any(f.check == "banned:dimension_claim" for f in findings)


def test_placeholder_is_blocked(settings):
    assert check_banned_patterns(settings, "TODO: 내용을 채울 것")


def test_prompt_leak_is_blocked(settings):
    assert check_banned_patterns(settings, "<vision_data> 내용 </vision_data>")


def test_clean_text_passes(settings):
    text = "내추럴 오크 도어와 밝은 상판이 어우러진 구성입니다. 수납은 서랍 중심으로 계획되어 있습니다."
    assert check_banned_patterns(settings, text) == []


def test_brand_not_in_evidence_is_blocked(settings):
    evidence = '{"furniture_types": ["주방 상부장"]}'
    findings = check_unsupported_brands(settings, "한샘 제품으로 보입니다.", evidence)
    assert findings and findings[0].check == "unsupported_brand"


def test_brand_present_in_evidence_is_allowed(settings):
    evidence = '{"visible_text": ["한샘"]}'
    assert check_unsupported_brands(settings, "간판에 한샘이 보입니다.", evidence) == []


def test_region_plus_install_claim_is_blocked(settings):
    findings = check_region_install_claim(settings, "분당 지역에서 시공한 사례입니다.")
    assert findings and findings[0].check == "region_install_claim"


def test_region_without_install_claim_is_allowed(settings):
    assert check_region_install_claim(settings, "서울처럼 좁은 주방이 많은 곳에서는 수납이 중요합니다.") == []


def test_repeated_sentence_is_blocked(settings):
    text = " ".join(["수납이 넉넉하게 계획되어 있습니다."] * 4)
    findings = check_repetition(settings, text)
    assert findings and findings[0].check == "repeated_sentence"


def test_english_drift_is_detected(settings):
    findings = check_language(settings, "This kitchen cabinet layout is very efficient and modern for small spaces.")
    assert findings and findings[0].check == "hangul_ratio"


def test_korean_text_passes_language_check(settings):
    assert check_language(settings, "내추럴 오크 도어가 적용된 주방 구성입니다.") == []


def test_hangul_ratio_ignores_numbers_and_symbols():
    assert hangul_ratio("주방 123 !!! 가구") > 0.9


def test_trigram_overlap_detects_padding():
    repeated = "수납이 넉넉합니다. " * 30   # 단어 3-gram 최소 길이(30단어)를 넘겨야 측정된다
    varied = (
        "내추럴 오크 도어가 벽면을 따라 이어집니다. 창가에 싱크볼이 배치되어 조리 동선이 짧습니다. "
        "손잡이는 가로형 바 타입으로 보이며 상판은 밝은 색입니다. 키큰장이 한쪽 끝에 세워져 있습니다."
    )
    assert trigram_self_overlap(repeated) > trigram_self_overlap(varied)


def test_bullet_section_requires_minimum_items():
    section = BlogSection(id="pros", title="장점", kind="bullets", guidance="", min_items=3, max_items=5)
    assert check_section_format(section, "- 하나\n- 둘")
    assert check_section_format(section, "- 하나\n- 둘\n- 셋") == []


def test_qa_section_requires_questions():
    section = BlogSection(id="faq", title="FAQ", kind="qa", guidance="", min_items=3, max_items=5)
    ok = "Q. 첫 질문\nA. 답\nQ. 둘째\nA. 답\nQ. 셋째\nA. 답"
    assert check_section_format(section, ok) == []
    assert check_section_format(section, "Q. 하나\nA. 답")


def test_title_section_rejects_multiline_and_long_titles():
    section = BlogSection(id="title", title="제목", kind="title", guidance="")
    assert check_section_format(section, "첫 줄\n둘째 줄")
    assert check_section_format(section, "가" * 70)
    assert check_section_format(section, "내추럴 오크 주방가구 구성 정리") == []


def test_prose_section_rejects_bullet_output():
    section = BlogSection(id="care", title="관리", kind="prose", guidance="")
    findings = check_section_format(section, "- 목록으로 썼습니다\n- 두 번째 항목입니다\n- 세 번째 항목도 길게 씁니다")
    assert any("목록" in f.message for f in findings)


def test_section_validator_returns_first_error(settings):
    validate = make_section_validator(settings, MERGED_VISION)
    section = BlogSection(id="care", title="관리 방법", kind="prose", guidance="")
    good = (
        "부드러운 천으로 표면을 닦고 물기를 남기지 않는 것이 좋습니다. 세제는 중성 제품을 쓰는 편이 안전합니다. "
        "문틈과 손잡이 주변은 먼지가 쌓이기 쉬우므로 주기적으로 확인하는 편이 좋습니다."
    )
    assert validate(section, good) is None
    assert validate(section, good + " 가격은 200만원입니다.") is not None


def _job_with_hold() -> Job:
    job = Job(job_id="j", created_at="", input_dir="", stage_status={s.value: StageStatus.NOT_STARTED.value for s in Stage.ordered()})
    job.images = [
        JobImage(file="ok.jpg", slug="k-01", publish_image="k-01.webp", status=ImageStatus.OK.value),
        JobImage(file="face.jpg", slug="k-02", publish_image="k-02.webp", status=ImageStatus.PRIVACY_HOLD.value),
    ]
    return job


SAMPLE_PARAGRAPHS = [
    "내추럴 오크 도어가 벽면을 따라 길게 이어지며 시선을 안정적으로 잡아 줍니다.",
    "창가 쪽에 놓인 싱크볼 덕분에 설거지 동선이 짧아지고 채광도 함께 들어옵니다.",
    "가로로 긴 바 형태 손잡이는 젖은 손으로도 걸리지 않고 부드럽게 열립니다.",
    "키큰장을 한쪽 끝에 세워 두면 자잘한 조리 도구를 한곳에 몰아 둘 수 있습니다.",
    "밝은 톤 상판은 재료 색이 잘 보여 손질할 때 눈이 덜 피로해지는 편입니다.",
    "서랍을 세 단으로 나누면 자주 쓰는 물건과 계절 용품을 층별로 구분하기 쉽습니다.",
    "무광 마감은 지문이 덜 도드라지지만 기름때는 그때그때 닦아 주는 편이 낫습니다.",
    "코너 공간은 손이 닿기 어려우므로 회전 선반 같은 보조 장치를 고려하게 됩니다.",
    "조명이 상부장 아래를 비추면 도마 위 그림자가 줄어 작업이 한결 편해집니다.",
    "문 여닫는 각도를 미리 확인해야 냉장고나 벽면과 부딪히는 일을 피할 수 있습니다.",
    "수납 계획은 실제로 무엇을 얼마나 두는지부터 세어 보는 데서 출발합니다.",
]


def _document(settings, body_sections: int = 12) -> tuple[str, str]:
    titles = [s.title for s in settings.sections if s.kind != "title"]
    body = "\n\n".join(
        f"[{title}]\n{SAMPLE_PARAGRAPHS[index % len(SAMPLE_PARAGRAPHS)]}"
        for index, title in enumerate(titles[:body_sections])
    ) + "\n"
    header = "\n".join([
        "=" * 10, "TITLE: 내추럴 오크 주방가구 구성 정리", "DATE: 2026-08-17 10:42 KST",
        "CATEGORY: 부엌가구", "TAGS: 주방가구", "SUMMARY: 요약입니다.",
        "IMAGES:", "  - k-01.webp | 참고 이미지 | ALT: 내추럴 오크 주방 상부장",
        "SOURCE: 사용자 제공 사진", "=" * 10,
    ])
    return header + "\n\n" + body, body


def test_document_check_passes_for_clean_output(settings, tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "k-01.webp").write_bytes(b"x")
    document, body = _document(settings)
    entries = [{"file": "k-01.webp", "alt": "내추럴 오크 주방 상부장", "caption": "참고 이미지"}]
    report = check_document(settings, _job_with_hold(), document, body, MERGED_VISION, entries, images_dir)
    assert report.passed, [f.message for f in report.errors]


def test_privacy_hold_image_in_output_is_an_error(settings, tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "k-01.webp").write_bytes(b"x")
    document, body = _document(settings)
    document += "\n참고: k-02.webp\n"
    entries = [{"file": "k-01.webp", "alt": "내추럴 오크 주방 상부장", "caption": "참고 이미지"}]
    report = check_document(settings, _job_with_hold(), document, body, MERGED_VISION, entries, images_dir)
    assert any(f.check == "privacy" for f in report.errors)


def test_missing_alt_is_an_error(settings, tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "k-01.webp").write_bytes(b"x")
    document, body = _document(settings)
    entries = [{"file": "k-01.webp", "alt": "", "caption": "참고 이미지"}]
    report = check_document(settings, _job_with_hold(), document, body, MERGED_VISION, entries, images_dir)
    assert any("ALT" in f.message for f in report.errors)


def test_image_reference_mismatch_is_an_error(settings, tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "k-01.webp").write_bytes(b"x")
    (images_dir / "orphan.webp").write_bytes(b"x")
    document, body = _document(settings)
    entries = [{"file": "k-01.webp", "alt": "설명", "caption": "참고 이미지"}]
    report = check_document(settings, _job_with_hold(), document, body, MERGED_VISION, entries, images_dir)
    assert any(f.check == "images" for f in report.errors)


def test_too_few_sections_is_an_error(settings, tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "k-01.webp").write_bytes(b"x")
    document, body = _document(settings, body_sections=3)
    entries = [{"file": "k-01.webp", "alt": "설명", "caption": "참고 이미지"}]
    report = check_document(settings, _job_with_hold(), document, body, MERGED_VISION, entries, images_dir)
    assert any(f.check == "structure" for f in report.errors)


def test_dangling_unverified_phrase_is_blocked(settings):
    """실측: 모델이 확인 불가 표현을 무관한 문장 끝에 접속어미로 붙였다."""
    bad = "모든 요소는 통일된 톤 안에서 균형 있게 조화를 이루고 있으며, 사진만으로는 확인되지 않습니다."
    findings = check_banned_patterns(settings, bad)
    assert any(f.check == "banned:dangling_unverified" for f in findings)


def test_standalone_unverified_sentence_is_allowed(settings):
    good = "상판의 정확한 소재는 사진만으로 확인되지 않습니다. 도어는 원목 무늬로 보입니다."
    assert check_banned_patterns(settings, good) == []


def test_trigram_metric_does_not_flag_normal_korean_prose():
    """실측 교정: 문자 3-gram은 한국어 조사·어미 때문에 정상 글도 0.2~0.34가 나왔다.
    단어 3-gram으로 바꿔야 패딩과 정상 글이 분리된다."""
    normal = (
        "내추럴 오크 도어가 벽면을 따라 이어지며 시선을 안정적으로 잡아 줍니다. "
        "창가 쪽에 놓인 싱크볼 덕분에 설거지 동선이 짧아지고 채광도 함께 들어옵니다. "
        "가로로 긴 바 형태 손잡이는 젖은 손으로도 걸리지 않고 부드럽게 열립니다. "
        "키큰장을 한쪽 끝에 세워 두면 자잘한 조리 도구를 한곳에 몰아 둘 수 있습니다. "
        "밝은 톤 상판은 재료 색이 잘 보여 손질할 때 눈이 덜 피로해지는 편입니다."
    )
    padded = "수납이 넉넉하게 계획되어 있습니다. " * 20
    assert trigram_self_overlap(normal) < 0.15
    assert trigram_self_overlap(padded) > 0.5
