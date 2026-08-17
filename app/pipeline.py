"""6 스테이지 파이프라인 오케스트레이터.

GUI는 이 모듈을 호출만 한다. CLI도 같은 함수를 부른다.
Recoverable 실패 하나로 전체를 중단하지 않는다. 전략을 바꿔 계속 진행한다.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .image_prep import input_set_hash, prepare_images
from .job_state import (
    ImageStatus, Job, JobStore, RunLock, Stage, StageStatus, now_kst, slugify,
)
from .memory_guard import MemoryGuard
from .metrics import atomic_write_json
from .output_stage import assemble, write_output
from .prompt_builder import load_prompts
from .quality_stage import check_document, make_section_validator
from .runtime import (
    LlamaServerRuntime, MtmdVisionRuntime, OllamaRuntime,
    required_free_mb, select_tier, unload_gate,
)
from .schemas import AgentError
from .settings import ModelTier, Settings
from .vision_stage import merge_vision_documents, run_vision_stage
from .writer_stage import run_write_stage

LOG = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class PipelineResult:
    job_id: str
    completed: bool
    output_txt: str = ""
    output_meta: str = ""
    quality_passed: bool = False
    blockers: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def create_job(
    settings: Settings,
    store: JobStore,
    job_id: str,
    category: str = "",
    topic_hint: str = "",
) -> Job:
    inbox = settings.inbox_dir / job_id
    if not inbox.exists():
        raise AgentError(f"입력 폴더가 없습니다: {inbox}")
    job = Job(
        job_id=job_id,
        created_at=now_kst(),
        input_dir=str(inbox.relative_to(settings.root)) if inbox.is_relative_to(settings.root) else str(inbox),
        category=category or settings.blog.get("default_category", "부엌가구"),
        topic_hint=topic_hint,
        stage_status={stage.value: StageStatus.NOT_STARTED.value for stage in Stage.ordered()},
    )
    store.save(job)
    return job


def new_job_id(category_slug: str = "job") -> str:
    from datetime import datetime

    from .job_state import KST

    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M")
    return f"{stamp}-{slugify(category_slug, 'job')}"


class Pipeline:
    def __init__(self, settings: Settings, logger: logging.Logger | None = None, progress: ProgressCallback | None = None) -> None:
        self.settings = settings
        self.log = logger or LOG
        self.store = JobStore(settings.state_dir)
        self.guard = MemoryGuard(
            int(settings.memory.get("min_free_mb_during_run", 1024)),
            float(settings.memory.get("unload_poll_interval_sec", 2.0)),
            self.log,
            max_swap_used_percent=float(settings.memory.get("max_swap_used_percent", 85.0)),
            abort_above_celsius=float(settings.thermal.get("abort_above_celsius", 82.0)),
        )
        self.prompts = load_prompts(settings.root / "config" / "prompts.yaml")
        self.progress = progress
        self._llm_runtime: LlamaServerRuntime | OllamaRuntime | None = None

    # ---- 유틸 ----
    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.progress is not None:
            try:
                self.progress(event, payload)
            except Exception as exc:  # noqa: BLE001 - 진행률 보고 실패가 파이프라인을 죽이면 안 된다
                self.log.debug("progress callback 실패: %s", exc)

    def work_dir(self, job: Job) -> Path:
        return self.settings.work_dir / job.job_id

    # ---- 사전 점검 ----
    def preflight(self, job: Job) -> None:
        """실행 전에 사람이 조치해야만 풀리는 문제를 먼저 걸러낸다.

        여기서 걸리면 BLOCKED_EXTERNAL이다. 재시도로는 풀리지 않으므로 시도하지 않는다.
        """
        blockers: list[str] = []

        free_gib = shutil.disk_usage(self.settings.root).free / 1024**3
        min_free_gib = float(self.settings.runtime.get("disk", {}).get("min_free_gib", 2.0))
        if free_gib < min_free_gib:
            stale = sorted(self.settings.work_dir.glob("*"))
            hint = f" 정리 후보: work/ 아래 {len(stale)}개 작업 폴더" if stale else ""
            blockers.append(
                f"디스크 여유가 {free_gib:.1f} GiB로 최소 {min_free_gib:.1f} GiB에 못 미칩니다. "
                f"자동으로 지우지 않습니다. 직접 정리한 뒤 다시 실행하세요.{hint}"
            )

        for kind, download_hint in (
            ("vlm", "python scripts/download_models.py --vlm-only"),
            ("llm", "python scripts/download_models.py --llm-only"),
        ):
            report = self.settings.available_tiers(kind)
            if not any(ok for _, ok, _ in report):
                reasons = "; ".join(f"{tier.name}: {why}" for tier, ok, why in report if not ok)
                blockers.append(
                    f"사용 가능한 {kind.upper()} 모델이 없습니다. 사유: {reasons}. "
                    f"내려받기: {download_hint}"
                )

        if blockers:
            for blocker in blockers:
                self.log.error("BLOCKED_EXTERNAL: %s", blocker)
            job.stage = Stage.PREP.value
            self.store.finish_stage(job, Stage.PREP, StageStatus.BLOCKED_EXTERNAL, blockers[0])
            for extra in blockers[1:]:
                if extra not in job.blockers:
                    job.blockers.append(extra)
            self.store.save(job)
            raise AgentError("실행 전 점검에서 막혔습니다: " + " | ".join(blockers))

    def _store_progress(self, job: Job, detail: str, current: int = 0, total: int = 0) -> None:
        self.store.record_progress(job, detail, current, total)
        self._emit("progress", {"job_id": job.job_id, "stage": job.stage, "detail": detail, "current": current, "total": total})

    # ---- STAGE 1 ----
    def stage_prep(self, job: Job) -> None:
        if job.is_done(Stage.PREP) and job.images:
            self.log.info("STAGE_1_PREP는 이미 완료돼 건너뜁니다 (resume)")
            return
        self.store.begin_stage(job, Stage.PREP)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.PREP.value, "status": "IN_PROGRESS"})

        work = self.work_dir(job)
        images_dir = work / "images"
        category_slugs = self.settings.blog.get("category_slugs", {})
        slug_base = category_slugs.get(job.category, "furniture")
        face_model = self.settings.root / "models" / "privacy" / "face_detection_yunet_2023mar.onnx"
        vlm_edge = int(self.settings.images.get("vlm_max_edge", 768))

        inbox = self.settings.root / job.input_dir if not Path(job.input_dir).is_absolute() else Path(job.input_dir)

        def cb(done: int, total: int, name: str) -> None:
            self._store_progress(job, f"사진 준비 {done}/{total}: {name}", done, total)

        outcome = prepare_images(
            inbox, images_dir, slug_base, self.settings.images, self.settings.privacy,
            face_model, vlm_edge, progress_cb=cb,
        )
        job.images = outcome.images
        job.input_set_hash = input_set_hash([img.sha256 for img in outcome.images])
        job.timings["prep"] = {"heif": outcome.heif_status, "skipped": outcome.skipped}
        self.store.finish_stage(job, Stage.PREP, StageStatus.SUCCESS,
                                f"{len(outcome.images)}장 준비 완료 (건너뜀 {len(outcome.skipped)}장)")
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.PREP.value, "status": "SUCCESS"})

    # ---- STAGE 2 ----
    def stage_vision(self, job: Job) -> MtmdVisionRuntime | OllamaRuntime:
        tier, notes = select_tier(self.settings, "vlm", self.guard, logger=self.log)
        job.models_used["vlm"] = tier.name
        if notes:
            job.timings.setdefault("tier_notes", {})["vlm"] = notes
        runtime = MtmdVisionRuntime(tier, self.settings, self.guard, self.log)

        if job.is_done(Stage.VISION):
            self.log.info("STAGE_2_VISION은 이미 완료돼 건너뜁니다 (resume)")
            return runtime

        self.store.begin_stage(job, Stage.VISION)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.VISION.value, "status": "IN_PROGRESS"})

        before = self.guard.snapshot()
        started = time.monotonic()
        outcome = run_vision_stage(
            job, self.store, self.settings, runtime, self.guard,
            self.work_dir(job), self.prompts, self.log,
        )
        job.timings["vision"] = {
            "analyzed": outcome.analyzed,
            "skipped": outcome.skipped,
            "newly_held": outcome.newly_held,
            "released": outcome.released,
            "resumed": outcome.resumed,
            "per_image_seconds": outcome.per_image_seconds,
            "peak_rss_mb": round(outcome.peak_rss_mb, 1),
            "total_seconds": round(time.monotonic() - started, 1),
            "mem_available_before_mb": before.mem_available_mb,
            "notes": outcome.notes,
        }
        status = StageStatus.SUCCESS if outcome.skipped == 0 else StageStatus.SUCCESS
        detail = f"{outcome.analyzed}장 분석 완료"
        if outcome.skipped:
            detail += f", {outcome.skipped}장 건너뜀"
        if outcome.newly_held:
            detail += f", {outcome.newly_held}장 개인정보 보류"
        if outcome.released:
            detail += f", 잠정 보류 {outcome.released}장 해제"
        if outcome.resumed:
            detail += f", {outcome.resumed}장 기존 결과 재사용"
        self.store.finish_stage(job, Stage.VISION, status, detail)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.VISION.value, "status": status.value})
        return runtime

    # ---- STAGE 3 ----
    def stage_unload(self, job: Job, vision_runtime: MtmdVisionRuntime | OllamaRuntime) -> ModelTier:
        self.store.begin_stage(job, Stage.UNLOAD)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.UNLOAD.value, "status": "IN_PROGRESS"})
        self._store_progress(job, "VLM 언로드 및 메모리 회수 검증")

        skip: set[str] = set()
        evidence_log: list[dict[str, Any]] = []

        while True:
            llm_tier, notes = select_tier(self.settings, "llm", self.guard, skip_names=skip, logger=self.log)
            target = required_free_mb(llm_tier, self.settings.memory)
            evidence = unload_gate(
                self.guard, self.settings.memory, target,
                f"VLM:{vision_runtime.tier.name}", getattr(vision_runtime, "last_pid", None), self.log,
            )
            evidence_log.append({**evidence.to_dict(), "llm_tier": llm_tier.name})
            if evidence.passed:
                job.memory_evidence["unload_gate"] = evidence_log
                job.models_used["llm"] = llm_tier.name
                if notes:
                    job.timings.setdefault("tier_notes", {})["llm"] = notes
                self.store.finish_stage(
                    job, Stage.UNLOAD, StageStatus.SUCCESS,
                    f"메모리 회수 확인: {evidence.available_before_mb} -> {evidence.available_after_mb} MiB "
                    f"(목표 {target} MiB, LLM tier={llm_tier.name})",
                )
                self._emit("stage", {"job_id": job.job_id, "stage": Stage.UNLOAD.value, "status": "SUCCESS"})
                return llm_tier

            # 게이트 미달 -> 한 단계 낮은 LLM으로 전환하여 계속 진행한다.
            self.store.record_retry(
                job, Stage.UNLOAD,
                failure=f"언로드 후 메모리 부족: {evidence.note}",
                root_cause=f"{llm_tier.name} 요구 {target} MiB > 실제 {evidence.available_after_mb} MiB",
                action="한 티어 낮은 LLM으로 전환",
            )
            job.stage_status[Stage.UNLOAD.value] = StageStatus.FAILED_RECOVERABLE.value
            self.store.save(job)
            skip.add(llm_tier.name)
            self.log.warning("LLM tier '%s'를 제외하고 다시 시도합니다.", llm_tier.name)

    # ---- STAGE 4 ----
    def stage_write(self, job: Job, llm_tier: ModelTier):
        self.store.begin_stage(job, Stage.WRITE)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.WRITE.value, "status": "IN_PROGRESS"})

        work = self.work_dir(job)
        merged_vision = merge_vision_documents(job, work)
        atomic_write_json(work / "vision" / "_merged.json", merged_vision)

        if llm_tier.backend == "ollama":
            runtime: LlamaServerRuntime | OllamaRuntime = OllamaRuntime(llm_tier, self.settings, self.guard, "llm", self.log)
        else:
            runtime = LlamaServerRuntime(llm_tier, self.settings, self.guard, self.log)
            self._store_progress(job, f"LLM 로드 중 ({llm_tier.name})")
            runtime.start(work / "llm_server.log")
        self._llm_runtime = runtime

        validator = make_section_validator(self.settings, merged_vision)
        outcome = run_write_stage(
            job, self.store, self.settings, self.prompts, runtime,
            merged_vision, work / "draft", validator, self.log,
        )
        job.timings["write"] = {
            "truncated_reason": outcome.truncated_reason,
            "total_seconds": outcome.total_seconds,
            "total_tokens": outcome.total_tokens,
            "sections": [{"id": s.id, "seconds": s.seconds, "tokens": s.tokens, "attempts": s.attempts} for s in outcome.sections],
        }
        write_status = StageStatus.FAILED_RECOVERABLE if outcome.truncated_reason else StageStatus.SUCCESS
        detail = f"{len(outcome.sections)}개 섹션 생성 ({outcome.total_seconds:.0f}s)"
        if outcome.truncated_reason:
            detail += f" — {outcome.truncated_reason}"
        self.store.finish_stage(job, Stage.WRITE, write_status, detail)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.WRITE.value, "status": write_status.value})
        return outcome, merged_vision, runtime

    # ---- STAGE 5 + 6 ----
    def stage_quality_and_output(self, job: Job, outcome, merged_vision: dict[str, Any]) -> tuple[Any, Any]:
        work = self.work_dir(job)
        per_image_vision: dict[str, dict[str, Any]] = {}
        for image in job.usable_images():
            if not image.vision_json:
                continue
            path = work / "vision" / image.vision_json
            if path.exists():
                per_image_vision[image.slug] = json.loads(path.read_text(encoding="utf-8"))

        assembled = assemble(job, self.settings, outcome, per_image_vision, self.log)

        self.store.begin_stage(job, Stage.OUTPUT)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.OUTPUT.value, "status": "IN_PROGRESS"})
        assembled = write_output(
            job, self.settings, assembled, outcome, work / "images",
            quality={}, extra_meta={"input_set_hash": job.input_set_hash},
        )

        # 품질 검사는 파일이 자리를 잡은 뒤에 한다. 이미지 1:1 대응을 실제 파일로 검사하기 위해서다.
        self.store.begin_stage(job, Stage.QUALITY)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.QUALITY.value, "status": "IN_PROGRESS"})
        assert assembled.images_dir is not None
        report = check_document(
            self.settings, job, assembled.document, assembled.body,
            merged_vision, assembled.image_entries, assembled.images_dir,
        )
        job.quality = report.to_dict()
        quality_status = StageStatus.SUCCESS if report.passed else StageStatus.FAILED_RECOVERABLE
        detail = "품질 게이트 통과" if report.passed else f"품질 게이트 실패 {len(report.errors)}건"
        self.store.finish_stage(job, Stage.QUALITY, quality_status, detail)
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.QUALITY.value, "status": quality_status.value})

        # 검사 결과를 meta에 반영해 다시 쓴다.
        assembled = write_output(
            job, self.settings, assembled, outcome, work / "images",
            quality=job.quality, extra_meta={"input_set_hash": job.input_set_hash},
        )

        job.output = {
            "txt": str(assembled.txt_path),
            "meta": str(assembled.meta_path),
            "slug": assembled.slug,
            "images": [entry["file"] for entry in assembled.image_entries],
        }
        self.store.finish_stage(job, Stage.OUTPUT, StageStatus.SUCCESS, f"{assembled.txt_path}")
        self._emit("stage", {"job_id": job.job_id, "stage": Stage.OUTPUT.value, "status": "SUCCESS"})
        return assembled, report

    # ---- 전체 ----
    def run(self, job_id: str) -> PipelineResult:
        job = self.store.load(job_id)
        lock_cfg = self.settings.runtime.get("lock", {})
        started = time.monotonic()

        with RunLock(self.settings.lock_path, job_id, bool(lock_cfg.get("stale_takeover", True))):
            vision_runtime = None
            try:
                self.preflight(job)
                self.stage_prep(job)
                vision_runtime = self.stage_vision(job)
                llm_tier = self.stage_unload(job, vision_runtime)
                outcome, merged_vision, llm_runtime = self.stage_write(job, llm_tier)

                # LLM을 내린 뒤에 규칙 기반 검사를 한다 (지시서 STAGE_5).
                self._store_progress(job, "LLM 언로드")
                evidence = llm_runtime.unload(int(self.settings.memory.get("min_free_mb_for_vlm", 4096)))
                job.memory_evidence["llm_unload"] = evidence.to_dict()
                self._llm_runtime = None
                self.store.save(job)

                assembled, report = self.stage_quality_and_output(job, outcome, merged_vision)

                job.timings["total_seconds"] = round(time.monotonic() - started, 1)
                self.store.save(job)
                return PipelineResult(
                    job_id=job_id, completed=True,
                    output_txt=str(assembled.txt_path), output_meta=str(assembled.meta_path),
                    quality_passed=report.passed, blockers=list(job.blockers),
                    summary={
                        "images_in": len(job.images),
                        "images_used": len(job.usable_images()),
                        "images_held": len(job.held_images()),
                        "quality": job.quality,
                        "timings": job.timings,
                        "models": job.models_used,
                        "memory_evidence": job.memory_evidence,
                    },
                )
            finally:
                if self._llm_runtime is not None:
                    try:
                        self._llm_runtime.unload()
                    except Exception as exc:  # noqa: BLE001
                        self.log.warning("정리 중 LLM 언로드 실패: %s", exc)
                    self._llm_runtime = None

    def cancel_cleanup(self) -> None:
        """안전 취소: 모델을 내린 뒤 종료한다."""
        if self._llm_runtime is not None:
            self._llm_runtime.unload()
            self._llm_runtime = None
