from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import time
from contextlib import AbstractContextManager
from pathlib import Path
from types import FrameType
from typing import Any

from .config import AppConfig
from .image_preprocessor import preprocess_image
from .llm_runner import build_llm_command
from .memory_guard import MemoryGuard
from .metrics import atomic_write_json, atomic_write_text, new_run_id, utc_now
from .model_discovery import find_local_downloaded_model, find_local_vlm, local_model_selection
from .output_parser import clean_blog_markdown, parse_vision_json, validate_blog_markdown
from .process_runner import process_exists, run_process
from .prompt_builder import build_blog_prompt, build_vision_prompt, load_prompts
from .schemas import AgentError, LockError, Metrics, ModelSelection, ModelSelectionError, RunRequest, RunState, StateError
from .vision_runner import build_vlm_command

LOG = logging.getLogger(__name__)


ALLOWED_TRANSITIONS = {
    RunState.INITIALIZING: {RunState.PRECHECK, RunState.FAILED},
    RunState.PRECHECK: {RunState.PREPROCESSING_IMAGE, RunState.FAILED},
    RunState.PREPROCESSING_IMAGE: {RunState.WAITING_FOR_VLM_MEMORY, RunState.FAILED},
    RunState.WAITING_FOR_VLM_MEMORY: {RunState.RUNNING_VLM, RunState.FAILED},
    RunState.RUNNING_VLM: {RunState.STOPPING_VLM, RunState.FAILED},
    RunState.STOPPING_VLM: {RunState.WAITING_FOR_MEMORY_RELEASE, RunState.FAILED},
    RunState.WAITING_FOR_MEMORY_RELEASE: {RunState.BUILDING_BLOG_PROMPT, RunState.FAILED},
    RunState.BUILDING_BLOG_PROMPT: {RunState.WAITING_FOR_LLM_MEMORY, RunState.FAILED},
    RunState.WAITING_FOR_LLM_MEMORY: {RunState.RUNNING_LLM, RunState.FAILED},
    RunState.RUNNING_LLM: {RunState.STOPPING_LLM, RunState.FAILED},
    RunState.STOPPING_LLM: {RunState.WRITING_OUTPUT, RunState.FAILED},
    RunState.WRITING_OUTPUT: {RunState.COMPLETED, RunState.FAILED},
    RunState.COMPLETED: set(),
    RunState.FAILED: set(),
}


class AgentLock(AbstractContextManager["AgentLock"]):
    def __init__(self, lock_path: Path, request: RunRequest) -> None:
        self.lock_path = lock_path
        self.request = request
        self.handle: Any = None

    def __enter__(self) -> "AgentLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockError(f"another agent run already holds {self.lock_path}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": utc_now(),
                    "input_file": str(self.request.image),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return False


class PhotoBlogOrchestrator:
    def __init__(self, config: AppConfig, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or LOG
        self.state = RunState.INITIALIZING
        self.memory_guard = MemoryGuard(config.min_available_during_run_mb, config.memory_check_interval_sec, self.logger)
        self.current_vlm_pid: int | None = None
        self.current_llm_pid: int | None = None
        self._interrupted = False

    def transition(self, state: RunState) -> None:
        if state not in ALLOWED_TRANSITIONS[self.state]:
            raise StateError(f"invalid state transition: {self.state.value} -> {state.value}")
        self.logger.info("state=%s", state.value)
        self.state = state

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        self._interrupted = True
        raise KeyboardInterrupt(f"received signal {signum}")

    def _select_vlm(self, request: RunRequest) -> ModelSelection:
        if request.vlm_model or self.config.vlm_model_path:
            model_path = request.vlm_model or self.config.vlm_model_path
            assert model_path is not None
            selection = local_model_selection(self.config.vlm_hf_repo, self.config.vlm_quant, model_path)
            mmproj = request.vlm_mmproj or self.config.vlm_mmproj_path
            return ModelSelection(**{**selection.__dict__, "mmproj_path": mmproj})
        selection = find_local_vlm(self.config.vlm_hf_repo, self.config.model_dir, self.config.vlm_quant)
        if selection:
            return selection
        raise ModelSelectionError("VLM model is not downloaded; run scripts/download_models.py --vlm-only")

    def _select_llm(self, request: RunRequest) -> ModelSelection:
        allow_oversized = request.force_oversized_model or self.config.allow_oversized_model
        if request.llm_model or self.config.llm_model_path:
            model_path = request.llm_model or self.config.llm_model_path
            assert model_path is not None
            return local_model_selection(self.config.llm_hf_repo, "manual", model_path, self.config.max_llm_gguf_gib, allow_oversized)
        selection = find_local_downloaded_model(self.config.llm_hf_repo, self.config.model_dir, self.config.llm_quant_preferences)
        if selection:
            self.memory_guard.assert_model_size_allowed(selection.total_bytes, self.config.max_llm_gguf_gib, allow_oversized)
            return selection
        raise ModelSelectionError("LLM model is not downloaded; run scripts/download_models.py --llm-only")

    def _soft_select_models_for_dry_run(self, request: RunRequest, run_dir: Path, vision_prompt_path: Path, blog_prompt_path: Path) -> tuple[list[str], list[str], list[str]]:
        warnings: list[str] = []
        try:
            vlm = self._select_vlm(request)
        except AgentError as exc:
            warnings.append(str(exc))
            vlm = ModelSelection(self.config.vlm_hf_repo, self.config.vlm_quant, (self.config.model_dir / "vlm" / "MODEL.gguf",), self.config.model_dir / "vlm" / "MODEL.gguf", 0, mmproj_path=self.config.model_dir / "vlm" / "MMPROJ.gguf")
        try:
            llm = self._select_llm(request)
        except AgentError as exc:
            warnings.append(str(exc))
            llm = ModelSelection(self.config.llm_hf_repo, self.config.llm_quant_preferences[0], (self.config.model_dir / "llm" / "MODEL.gguf",), self.config.model_dir / "llm" / "MODEL.gguf", 0)
        vlm_args = build_vlm_command(self.config, vlm, run_dir / "prepared_image.jpg", vision_prompt_path)
        llm_args = build_llm_command(self.config, llm, blog_prompt_path)
        return vlm_args, llm_args, warnings

    def run(self, request: RunRequest) -> dict[str, Any]:
        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        run_id = new_run_id()
        run_dir = self.config.run_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        metrics = Metrics(run_id=run_id, started_at=utc_now())
        run_log_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
        run_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(run_log_handler)
        request_json = {
            "image": str(request.image),
            "topic": request.topic,
            "audience": request.audience,
            "tone": request.tone,
            "keywords": request.keywords,
            "output": str(request.output) if request.output else "",
            "dry_run": request.dry_run,
        }
        atomic_write_json(run_dir / "request.json", request_json)
        prompts = load_prompts(self.config.root / "config" / "prompts.yaml")
        vision_prompt = build_vision_prompt(prompts)
        vision_prompt_path = run_dir / "vision_prompt.txt"
        blog_prompt_path = run_dir / "blog_prompt.txt"
        atomic_write_text(vision_prompt_path, vision_prompt)
        try:
            with AgentLock(self.config.run_dir / "agent.lock", request):
                self.transition(RunState.PRECHECK)
                if not request.dry_run:
                    self.memory_guard.validate_total_ram()
                if request.dry_run:
                    self.logger.info("dry-run: preflight warnings are non-fatal unless explicit paths are invalid")
                else:
                    for binary in (self.config.llama_mtmd_cli, self.config.llama_cli):
                        if not binary.exists() or not os.access(binary, os.X_OK):
                            raise AgentError(f"required binary is missing or not executable: {binary}")

                self.transition(RunState.PREPROCESSING_IMAGE)
                prepared = preprocess_image(request.image, run_dir / "prepared_image.jpg", self.config.max_image_edge, self.config.jpeg_quality)
                metrics.input_sha256 = prepared.source_sha256

                if request.dry_run:
                    placeholder_vision = {
                        "summary": "dry-run placeholder",
                        "scene": {"location_type": "알 수 없음", "time_or_lighting": "알 수 없음", "weather": ""},
                        "subjects": [],
                        "visible_text": [],
                        "colors_and_composition": {"dominant_colors": [], "composition": "", "mood_from_visuals": ""},
                        "blog_worthy_details": [],
                        "uncertainties": ["dry-run에서는 VLM을 실행하지 않음"],
                        "privacy_notes": [],
                        "raw_caption": "",
                    }
                    atomic_write_json(run_dir / "vision.json", placeholder_vision)
                    blog_prompt = build_blog_prompt(prompts, placeholder_vision, request.topic, request.audience, request.tone, request.keywords, request.language)
                    atomic_write_text(blog_prompt_path, blog_prompt)
                    vlm_args, llm_args, warnings = self._soft_select_models_for_dry_run(request, run_dir, vision_prompt_path, blog_prompt_path)
                    memory = self.memory_guard.snapshot()
                    dry = {
                        "run_id": run_id,
                        "run_dir": str(run_dir),
                        "vlm_command": vlm_args,
                        "llm_command": llm_args,
                        "memory": {
                            "mem_total_mb": memory.mem_total_mb,
                            "mem_available_mb": memory.mem_available_mb,
                            "swap_total_mb": memory.swap_total_mb,
                            "swap_free_mb": memory.swap_free_mb,
                        },
                        "warnings": warnings,
                    }
                    atomic_write_json(run_dir / "metrics.json", {**metrics.to_dict(), "status": "dry-run", "dry_run": dry})
                    return dry

                vlm = self._select_vlm(request)
                llm = self._select_llm(request)

                self.transition(RunState.WAITING_FOR_VLM_MEMORY)
                before_vlm = self.memory_guard.wait_for_available(self.config.min_available_before_vlm_mb, self.config.memory_recovery_timeout_sec, "VLM")
                self.transition(RunState.RUNNING_VLM)
                if process_exists(self.current_llm_pid):
                    raise StateError("LLM PID is alive; refusing to start VLM")
                vlm_result = run_process(
                    build_vlm_command(self.config, vlm, prepared.prepared_path, vision_prompt_path),
                    run_dir / "vision_stdout.txt",
                    run_dir / "vision_stderr.txt",
                    self.config.vlm_timeout_sec,
                    self.config.process_terminate_grace_sec,
                    self.memory_guard.should_abort_running_process,
                    self.config.memory_check_interval_sec,
                )
                self.current_vlm_pid = vlm_result.pid
                self.transition(RunState.STOPPING_VLM)
                if process_exists(vlm_result.pid):
                    raise StateError(f"VLM PID still exists after wait: {vlm_result.pid}")
                after_vlm = self.memory_guard.snapshot()
                metrics.vlm = {
                    "model": str(vlm.primary_path),
                    "quantization": vlm.quantization,
                    "model_bytes": vlm.total_bytes,
                    "started_at": vlm_result.started_at,
                    "finished_at": vlm_result.finished_at,
                    "duration_seconds": round(vlm_result.duration_seconds, 3),
                    "exit_code": vlm_result.exit_code,
                    "peak_rss_mb": round(vlm_result.peak_rss_mb, 1),
                    "mem_available_before_mb": before_vlm.mem_available_mb,
                    "mem_available_after_mb": after_vlm.mem_available_mb,
                }
                if vlm_result.exit_code != 0:
                    raise AgentError(f"VLM failed with exit code {vlm_result.exit_code}")
                vision_data = parse_vision_json((run_dir / "vision_stdout.txt").read_text(encoding="utf-8", errors="replace"))
                atomic_write_json(run_dir / "vision.json", vision_data)

                self.transition(RunState.WAITING_FOR_MEMORY_RELEASE)
                self.memory_guard.maybe_drop_caches_after_vlm(self.config.allow_cache_drop, process_exists(vlm_result.pid))
                release_started = time.monotonic()
                recovered_snapshot = self.memory_guard.wait_for_available(self.config.min_available_before_llm_mb, self.config.memory_recovery_timeout_sec, "memory release before LLM")
                metrics.memory_release = {
                    "wait_seconds": round(time.monotonic() - release_started, 3),
                    "target_available_mb": self.config.min_available_before_llm_mb,
                    "recovered": True,
                }

                self.transition(RunState.BUILDING_BLOG_PROMPT)
                blog_prompt = build_blog_prompt(prompts, vision_data, request.topic, request.audience, request.tone, request.keywords, request.language)
                atomic_write_text(blog_prompt_path, blog_prompt)

                self.transition(RunState.WAITING_FOR_LLM_MEMORY)
                before_llm = recovered_snapshot
                if process_exists(vlm_result.pid):
                    raise StateError("VLM PID is alive; refusing to start LLM")
                before_llm = self.memory_guard.wait_for_available(self.config.min_available_before_llm_mb, self.config.memory_recovery_timeout_sec, "LLM")
                self.transition(RunState.RUNNING_LLM)
                llm_result = run_process(
                    build_llm_command(self.config, llm, blog_prompt_path),
                    run_dir / "llm_stdout.txt",
                    run_dir / "llm_stderr.txt",
                    self.config.llm_timeout_sec,
                    self.config.process_terminate_grace_sec,
                    self.memory_guard.should_abort_running_process,
                    self.config.memory_check_interval_sec,
                )
                self.current_llm_pid = llm_result.pid
                self.transition(RunState.STOPPING_LLM)
                if process_exists(llm_result.pid):
                    raise StateError(f"LLM PID still exists after wait: {llm_result.pid}")
                after_llm = self.memory_guard.snapshot()
                metrics.llm = {
                    "model": str(llm.primary_path),
                    "quantization": llm.quantization,
                    "model_bytes": llm.total_bytes,
                    "started_at": llm_result.started_at,
                    "finished_at": llm_result.finished_at,
                    "duration_seconds": round(llm_result.duration_seconds, 3),
                    "exit_code": llm_result.exit_code,
                    "peak_rss_mb": round(llm_result.peak_rss_mb, 1),
                    "mem_available_before_mb": before_llm.mem_available_mb,
                    "mem_available_after_mb": after_llm.mem_available_mb,
                }
                if llm_result.exit_code != 0:
                    raise AgentError(f"LLM failed with exit code {llm_result.exit_code}")

                self.transition(RunState.WRITING_OUTPUT)
                blog = clean_blog_markdown((run_dir / "llm_stdout.txt").read_text(encoding="utf-8", errors="replace"))
                validate_blog_markdown(blog)
                atomic_write_text(run_dir / "blog.md", blog)
                output_path = (request.output or self.config.output_dir / f"{run_id}.md").resolve()
                atomic_write_text(output_path, blog)
                metrics.output_file = str(output_path)
                metrics.status = "completed"
                metrics.finished_at = utc_now()
                metrics.thermal = {
                    "maximum_celsius": self.memory_guard.maximum_temp_c,
                    "throttling_detected": self.memory_guard.throttling_detected,
                }
                atomic_write_json(run_dir / "metrics.json", metrics.to_dict())
                self.transition(RunState.COMPLETED)
                return {"run_id": run_id, "run_dir": str(run_dir), "output": str(output_path), "metrics": metrics.to_dict()}
        except Exception:
            if self.state != RunState.FAILED:
                try:
                    self.transition(RunState.FAILED)
                except StateError:
                    self.state = RunState.FAILED
            metrics.status = "failed"
            metrics.finished_at = utc_now()
            metrics.thermal = {
                "maximum_celsius": self.memory_guard.maximum_temp_c,
                "throttling_detected": self.memory_guard.throttling_detected,
            }
            atomic_write_json(run_dir / "metrics.json", metrics.to_dict())
            raise
        finally:
            logging.getLogger().removeHandler(run_log_handler)
            run_log_handler.close()
            signal.signal(signal.SIGINT, original_sigint)
            signal.signal(signal.SIGTERM, original_sigterm)
