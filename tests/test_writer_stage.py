from __future__ import annotations

from pathlib import Path

import pytest

from app.prompt_builder import load_prompts
from app.quality_stage import make_section_validator
from app.settings import BlogSection, Settings
from app.writer_stage import (
    BlogWriter, build_base_prompt, clean_generated, parse_meta_output,
)

ROOT = Path(__file__).resolve().parent.parent

MERGED_VISION = {
    "photo_count": 2,
    "furniture_types": ["주방 상부장"],
    "space": "주방",
    "layout": "ㄱ자형",
    "color_tone": ["내추럴 오크"],
    "notable_points": ["상부장이 벽면을 따라 이어집니다."],
}


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings.load(ROOT)


@pytest.fixture(scope="module")
def prompts() -> dict[str, str]:
    return load_prompts(ROOT / "config" / "prompts.yaml")


class FakeTier:
    name = "fake-tier"

    def get(self, key, default=None):
        return default


class ScriptedRuntime:
    """미리 정해진 응답을 순서대로 돌려주는 가짜 런타임. 프롬프트를 전부 기록한다."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.tier = FakeTier()

    def complete(self, prompt: str, max_tokens=None, stop=None, temperature=None):
        self.prompts.append(prompt)
        content = self.responses.pop(0) if self.responses else "기본 응답입니다."
        return {"content": content, "timings": {"predicted_n": 42}}


def test_clean_generated_strips_control_tokens_and_markdown():
    raw = "```markdown\n## 제목\n**강조**된 문장입니다.<|im_end|>\n```"
    cleaned = clean_generated(raw)
    assert "<|im_end|>" not in cleaned
    assert "```" not in cleaned
    assert "##" not in cleaned
    assert "**" not in cleaned
    assert "강조된 문장입니다." in cleaned


def test_clean_generated_keeps_bullet_markers():
    assert clean_generated("- 첫째 항목\n- 둘째 항목").startswith("- 첫째 항목")


def test_parse_meta_output():
    summary, tags = parse_meta_output("SUMMARY: 요약 문장입니다.\nTAGS: 주방가구, 싱크대, 오크")
    assert summary == "요약 문장입니다."
    assert tags == ["주방가구", "싱크대", "오크"]


def test_base_prompt_contains_evidence_and_no_image_data(settings, prompts):
    base = build_base_prompt(settings, prompts, MERGED_VISION, "좁은 주방", "부엌가구")
    assert "<photo_analysis>" in base
    assert "ㄱ자형" in base
    assert "좁은 주방" in base
    # 원본 이미지 바이너리나 경로가 LLM 프롬프트에 들어가면 안 된다
    assert ".jpg" not in base and ".webp" not in base


def test_prompt_is_always_an_extension_of_the_previous_one(settings, prompts):
    """cache_prompt가 공통 프리픽스를 재사용하려면 이 성질이 유지돼야 한다."""
    runtime = ScriptedRuntime(["첫 번째 섹션 본문입니다. " * 4, "두 번째 섹션 본문입니다. " * 4])
    writer = BlogWriter(settings, prompts, runtime)
    writer.base_prompt = build_base_prompt(settings, prompts, MERGED_VISION, "", "부엌가구")

    section_a = BlogSection(id="a", title="A", kind="prose", guidance="")
    section_b = BlogSection(id="b", title="B", kind="prose", guidance="")
    writer.generate_section(section_a, 300)
    writer.generate_section(section_b, 300)

    first, second = runtime.prompts
    # 두 번째 프롬프트는 첫 번째 프롬프트의 지시 부분을 제외한 앞부분을 그대로 포함해야 한다
    assert second.startswith(writer.base_prompt)
    assert "첫 번째 섹션 본문입니다." in second
    assert len(second) > len(first)


def test_failed_section_is_regenerated_and_only_final_text_is_kept(settings, prompts):
    """품질 게이트 실패 케이스: 금칙어가 든 섹션은 그 섹션만 다시 만든다."""
    bad = "이 구성은 대략 250만원 정도입니다. 가격 대비 만족도가 높은 편이라고 볼 수 있습니다."
    good = (
        "내추럴 오크 도어가 벽면을 따라 이어지며 시선을 안정적으로 잡아 줍니다. "
        "창가 쪽 싱크볼 덕분에 설거지 동선이 짧아지고 채광도 함께 들어옵니다."
    )
    runtime = ScriptedRuntime([bad, good])
    writer = BlogWriter(settings, prompts, runtime)
    writer.base_prompt = build_base_prompt(settings, prompts, MERGED_VISION, "", "부엌가구")

    validator = make_section_validator(settings, MERGED_VISION)
    section = BlogSection(id="design_points", title="디자인 포인트", kind="prose", guidance="")
    result = writer.generate_section(section, 400, lambda text: validator(section, text), max_attempts=3)

    assert result.attempts == 2
    assert "250만원" not in result.text
    assert result.text == good
    # 거부된 본문이 대화 기록에 남으면 다음 섹션이 그 표현을 따라 쓴다
    assert all("250만원" not in content for _, content in writer.transcript)
    # 재시도 프롬프트에는 거부 사유가 들어가야 한다
    assert "거부됐다" in runtime.prompts[1]


def test_section_that_never_passes_is_kept_but_reported(settings, prompts):
    """3회 실패해도 조용히 통과시키지 않는다. 마지막 결과를 남기고 상위에서 판단한다."""
    bad = "문의는 010-1234-5678로 주시면 됩니다. 자세한 안내를 드리겠습니다. 편하게 연락 주세요."
    runtime = ScriptedRuntime([bad, bad, bad])
    writer = BlogWriter(settings, prompts, runtime)
    writer.base_prompt = build_base_prompt(settings, prompts, MERGED_VISION, "", "부엌가구")

    validator = make_section_validator(settings, MERGED_VISION)
    section = BlogSection(id="closing", title="정리", kind="prose", guidance="")
    result = writer.generate_section(section, 400, lambda text: validator(section, text), max_attempts=3)

    assert result.attempts == 3
    # 문서 단계 검사가 이 문제를 다시 잡아낸다
    assert validator(section, result.text) is not None


def test_transcript_trimming_keeps_generation_going(settings, prompts):
    runtime = ScriptedRuntime(["아주 긴 본문입니다. " * 200 for _ in range(6)])
    writer = BlogWriter(settings, prompts, runtime)
    writer.base_prompt = build_base_prompt(settings, prompts, MERGED_VISION, "", "부엌가구")
    for index in range(6):
        writer.generate_section(BlogSection(id=f"s{index}", title=f"S{index}", kind="prose", guidance=""), 300)
    # 예산을 넘긴 뒤에는 오래된 섹션이 축약돼야 한다
    assert any(content.startswith("(앞부분 생략") for _, content in writer.transcript)
