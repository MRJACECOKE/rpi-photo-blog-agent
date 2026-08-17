"""STAGE_4 — LLM 블로그 작성.

LLM에는 원본 이미지를 절대 넣지 않는다. VLM이 만든 vision JSON 통합본과 config/blog.yaml만 넣는다.
VLM은 이 시점에 이미 종료돼 있고, 멀티모달 컨텍스트를 다시 만들지 않는다.

생성 전략(Pi 성능 대응):
  - 한 번에 전체를 만들지 않고 섹션 단위로 나눠 생성한다.
  - ChatML 대화 기록을 누적해 프롬프트가 항상 이전 프롬프트의 확장이 되게 한다.
    그래야 llama-server의 cache_prompt가 공통 프리픽스를 전부 재사용한다.
    (실측 prompt eval 8.57 tok/s. 매번 다시 프리필하면 섹션당 약 140s가 낭비된다.)
  - 섹션마다 work/<job>/draft/ 에 즉시 저장한다. 전원 차단 대비.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .job_state import Job, JobStore
from .metrics import atomic_write_text
from .runtime import GenerationError, LlamaServerRuntime, OllamaRuntime
from .schemas import AgentError
from .settings import BlogSection, Settings

LOG = logging.getLogger(__name__)

IM_START = "<|im_start|>"
IM_END = "<|im_end|>"
STOP_TOKENS = [IM_END, IM_START, "<|endoftext|>"]

FORMAT_HINTS = {
    "prose": "3~6문장의 자연스러운 문단으로 쓴다. 목록 기호를 쓰지 않는다.",
    "bullets": "각 줄을 '- '로 시작하는 목록으로 {min_items}~{max_items}개 쓴다. 다른 문장을 덧붙이지 않는다.",
    "qa": "각 항목을 'Q. 질문' 다음 줄에 'A. 답변' 형식으로 {min_items}~{max_items}개 쓴다.",
    "title": "",
}


@dataclass
class SectionResult:
    id: str
    title: str
    text: str
    tokens: int = 0
    seconds: float = 0.0
    attempts: int = 1


@dataclass
class WriteOutcome:
    sections: list[SectionResult] = field(default_factory=list)
    title: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    total_seconds: float = 0.0
    total_tokens: int = 0
    model_name: str = ""
    truncated_reason: str = ""

    def body_text(self, sections: tuple[BlogSection, ...]) -> str:
        titles = {section.id: section.title for section in sections}
        parts = []
        for item in self.sections:
            if item.id in ("title",):
                continue
            parts.append(f"[{titles.get(item.id, item.title)}]\n{item.text.strip()}")
        return "\n\n".join(parts) + "\n"


def _chatml(role: str, content: str) -> str:
    return f"{IM_START}{role}\n{content}{IM_END}\n"


def clean_generated(text: str) -> str:
    """모델이 흘리는 제어 토큰과 마크다운 기호를 제거한다."""
    cleaned = text
    for token in (IM_END, IM_START, "<|endoftext|>"):
        cleaned = cleaned.replace(token, "")
    cleaned = re.sub(r"^\s*```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    cleaned = re.sub(r"\[end of text\]", "", cleaned, flags=re.IGNORECASE)
    # 마크다운 제목/강조 기호 제거. 목록의 '- '는 유지한다.
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def build_photo_analysis_block(merged_vision: dict[str, Any]) -> str:
    return "<photo_analysis>\n" + json.dumps(merged_vision, ensure_ascii=False, indent=2) + "\n</photo_analysis>"


def build_base_prompt(
    settings: Settings,
    prompts: dict[str, str],
    merged_vision: dict[str, Any],
    topic_hint: str,
    category: str,
) -> str:
    system = prompts.get("furniture_writer_system")
    if not system:
        raise AgentError("config/prompts.yaml에 furniture_writer_system이 없습니다")

    glossary = settings.blog.get("style", {}).get("glossary", {})
    glossary_line = ", ".join(f"{k}({v})" for k, v in glossary.items())
    length = settings.blog.get("length", {})

    context = {
        "카테고리": category,
        "사용자_주제_힌트": topic_hint or "(없음)",
        "목표_분량_한글자수": f"{length.get('min_chars', 1800)}~{length.get('max_chars', 3000)}",
        "용어_병기_대상": glossary_line,
    }
    system_content = (
        system.strip()
        + "\n\n<writing_context>\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
        + "\n</writing_context>\n\n"
        + build_photo_analysis_block(merged_vision)
    )
    return _chatml("system", system_content)


def build_section_instruction(prompts: dict[str, str], section: BlogSection) -> str:
    if section.kind == "title":
        template = prompts.get("furniture_title_instruction", "")
        return template.format(guidance=section.guidance).strip()
    hint_template = FORMAT_HINTS.get(section.kind, FORMAT_HINTS["prose"])
    format_hint = hint_template.format(min_items=section.min_items or 3, max_items=section.max_items or 5)
    template = prompts.get("furniture_section_instruction", "")
    return template.format(section_title=section.title, guidance=section.guidance, format_hint=format_hint).strip()


class BlogWriter:
    def __init__(
        self,
        settings: Settings,
        prompts: dict[str, str],
        runtime: LlamaServerRuntime | OllamaRuntime,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.prompts = prompts
        self.runtime = runtime
        self.log = logger or LOG
        self.transcript: list[tuple[str, str]] = []  # (role, content)
        self.base_prompt = ""

    def prompt_for(self, instruction: str) -> str:
        parts = [self.base_prompt]
        for role, content in self.transcript:
            parts.append(_chatml(role, content))
        parts.append(_chatml("user", instruction))
        parts.append(f"{IM_START}assistant\n")
        return "".join(parts)

    def _transcript_chars(self) -> int:
        return sum(len(content) for _, content in self.transcript)

    def _trim_if_needed(self, budget_chars: int) -> None:
        """컨텍스트가 넘칠 것 같으면 오래된 섹션 본문을 줄인다. 캐시 손해를 감수하는 마지막 수단이다."""
        while self._transcript_chars() > budget_chars and len(self.transcript) > 4:
            for index, (role, content) in enumerate(self.transcript):
                if role == "assistant" and not content.startswith("(앞부분 생략"):
                    first = content.strip().split("\n")[0][:60]
                    self.transcript[index] = (role, f"(앞부분 생략) {first}")
                    self.log.warning("컨텍스트 예산 초과로 이전 섹션을 축약했습니다.")
                    break
            else:
                return

    def generate_section(
        self,
        section: BlogSection,
        max_tokens: int,
        validator: Callable[[str], str | None] | None = None,
        max_attempts: int = 1,
    ) -> SectionResult:
        instruction = build_section_instruction(self.prompts, section)
        budget = int(self.settings.blog.get("length", {}).get("max_chars", 3000)) * 2
        self._trim_if_needed(budget)

        started = time.monotonic()
        text = ""
        tokens = 0
        problem: str | None = None
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            effective_instruction = instruction
            if problem:
                effective_instruction = (
                    instruction
                    + f"\n\n직전 시도는 다음 이유로 거부됐다: {problem}\n그 부분을 반드시 고쳐서 다시 쓴다."
                )
            prompt = self.prompt_for(effective_instruction)
            response = self.runtime.complete(prompt, max_tokens=max_tokens, stop=STOP_TOKENS)
            text = clean_generated(str(response.get("content", "")))
            timings = response.get("timings") or {}
            tokens = int(timings.get("predicted_n", 0) or response.get("tokens_predicted", 0) or 0)
            if not text:
                problem = "출력이 비어 있다"
                continue
            problem = validator(text) if validator else None
            if problem is None:
                break
            self.log.warning("섹션 '%s' 재생성 (%s/%s): %s", section.id, attempt, max_attempts, problem)

        if problem is not None:
            self.log.warning("섹션 '%s'가 %d회 시도 후에도 검사를 통과하지 못했습니다: %s", section.id, attempt, problem)

        # 재생성 여부와 무관하게 최종 채택본만 대화 기록에 남긴다.
        self.transcript.append(("user", instruction))
        self.transcript.append(("assistant", text))

        return SectionResult(
            id=section.id, title=section.title, text=text,
            tokens=tokens, seconds=round(time.monotonic() - started, 1), attempts=attempt,
        )


def run_write_stage(
    job: Job,
    store: JobStore,
    settings: Settings,
    prompts: dict[str, str],
    runtime: LlamaServerRuntime | OllamaRuntime,
    merged_vision: dict[str, Any],
    draft_dir: Path,
    section_validator: Callable[[BlogSection, str], str | None] | None = None,
    logger: logging.Logger | None = None,
) -> WriteOutcome:
    log = logger or LOG
    draft_dir.mkdir(parents=True, exist_ok=True)

    category = job.category or settings.blog.get("default_category", "부엌가구")
    writer = BlogWriter(settings, prompts, runtime, log)
    writer.base_prompt = build_base_prompt(settings, prompts, merged_vision, job.topic_hint, category)
    atomic_write_text(draft_dir / "base_prompt.txt", writer.base_prompt)

    max_tokens = int(settings.blog.get("length", {}).get("max_tokens_per_section", 560))
    max_attempts = int(settings.quality_limits.get("max_regeneration_attempts", 3))

    outcome = WriteOutcome()
    outcome.model_name = getattr(runtime.tier, "name", "unknown")
    sections = settings.sections
    started_all = time.monotonic()

    total_budget = float(settings.timeouts.get("llm_total_sec", 7200))

    for index, section in enumerate(sections, start=1):
        elapsed = time.monotonic() - started_all
        if elapsed > total_budget:
            # 예산을 넘기면 남은 섹션을 포기하되, 만든 것은 버리지 않는다.
            # 품질 게이트가 섹션 부족을 잡아내 FAILED_RECOVERABLE로 남긴다.
            outcome.truncated_reason = (
                f"작성 예산 {total_budget:.0f}s를 초과해 '{section.title}' 이후 {len(sections) - index + 1}개 섹션을 중단했습니다"
            )
            log.error("%s", outcome.truncated_reason)
            break

        store.record_progress(job, f"본문 작성 {index}/{len(sections)}: {section.title}", index, len(sections))

        validator = None
        if section_validator is not None:
            validator = lambda text, _section=section: section_validator(_section, text)  # noqa: E731

        try:
            result = writer.generate_section(section, max_tokens, validator, max_attempts)
        except GenerationError as exc:
            raise AgentError(f"섹션 '{section.id}' 생성 실패: {exc}") from exc

        outcome.sections.append(result)
        outcome.total_tokens += result.tokens
        if section.kind == "title":
            outcome.title = result.text.strip().splitlines()[0].strip().strip('"').strip("'")

        atomic_write_text(draft_dir / f"draft_v{index:02d}_{section.id}.txt", result.text)
        log.info("섹션 '%s' 완료 (%.1fs, %d tokens, 시도 %d회)", section.id, result.seconds, result.tokens, result.attempts)

    # SUMMARY / TAGS
    store.record_progress(job, "요약과 태그 생성", len(sections), len(sections))
    tag_count = int(settings.blog.get("output", {}).get("tag_count", 4))
    meta_instruction = prompts.get("furniture_meta_instruction", "").format(tag_count=tag_count).strip()
    try:
        response = runtime.complete(writer.prompt_for(meta_instruction), max_tokens=220, stop=STOP_TOKENS)
        summary, tags = parse_meta_output(clean_generated(str(response.get("content", ""))))
        outcome.summary = summary
        outcome.tags = tags
    except GenerationError as exc:
        log.warning("요약/태그 생성 실패, 본문에서 대체합니다: %s", exc)

    if not outcome.summary:
        first_prose = next((s.text for s in outcome.sections if s.id == "overview"), "")
        outcome.summary = first_prose.replace("\n", " ").lstrip("- ")[:120]
    if not outcome.tags:
        outcome.tags = [category] + merged_vision.get("color_tone", [])[:2]

    outcome.total_seconds = round(time.monotonic() - started_all, 1)
    atomic_write_text(draft_dir / "draft_full.txt", outcome.body_text(sections))
    return outcome


def parse_meta_output(text: str) -> tuple[str, list[str]]:
    summary = ""
    tags: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SUMMARY:"):
            summary = stripped.split(":", 1)[1].strip()
        elif stripped.upper().startswith("TAGS:"):
            raw = stripped.split(":", 1)[1]
            tags = [t.strip().lstrip("#") for t in re.split(r"[,،]", raw) if t.strip()]
    return summary, tags
