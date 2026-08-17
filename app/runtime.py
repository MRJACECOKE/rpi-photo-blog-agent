"""ModelRuntime 추상화 — load / generate / unload 와 언로드 게이트.

핵심 원칙: 언로드를 "명령을 보냈으니 됐다"로 처리하지 않는다.
  1) 종료 신호  2) PID 소멸 확인  3) MemAvailable 폴링  4) 미달 시 tier 하향
이 게이트를 통과하기 전에는 절대 다음 모델을 올리지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory_guard import MemoryGuard
from .metrics import utc_now
from .process_runner import process_exists, run_process
from .schemas import AgentError
from .settings import ModelTier, Settings

LOG = logging.getLogger(__name__)


class RuntimeStartError(AgentError):
    pass


class GenerationError(AgentError):
    pass


class UnloadGateError(AgentError):
    pass


@dataclass
class UnloadEvidence:
    """언로드 전후 실측 증거. 로그와 job json에 그대로 남긴다."""
    model: str
    pid: int | None
    pid_gone: bool
    available_before_mb: int
    available_after_mb: int
    target_mb: int
    waited_sec: float
    cache_dropped: bool
    passed: bool
    note: str = ""
    at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "pid": self.pid,
            "pid_gone": self.pid_gone,
            "available_before_mb": self.available_before_mb,
            "available_after_mb": self.available_after_mb,
            "target_mb": self.target_mb,
            "waited_sec": round(self.waited_sec, 2),
            "cache_dropped": self.cache_dropped,
            "passed": self.passed,
            "note": self.note,
            "at": self.at,
        }


def unload_gate(
    guard: MemoryGuard,
    memory_config: dict[str, Any],
    target_mb: int,
    model_label: str,
    pid: int | None,
    logger: logging.Logger | None = None,
) -> UnloadEvidence:
    """PID 소멸과 메모리 회수를 실측으로 검증한다. 통과하지 못해도 예외를 던지지 않고 증거를 돌려준다."""
    log = logger or LOG
    before = guard.snapshot()
    log.info("unload gate 시작: model=%s pid=%s MemAvailable=%s MiB target=%s MiB", model_label, pid, before.mem_available_mb, target_mb)

    pid_gone = not process_exists(pid)
    if not pid_gone:
        log.warning("PID %s가 아직 살아 있습니다. SIGTERM 후 대기합니다.", pid)
        try:
            os.kill(pid, signal.SIGTERM)  # type: ignore[arg-type]
        except (ProcessLookupError, PermissionError, TypeError):
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process_exists(pid):
            time.sleep(0.5)
        if process_exists(pid):
            log.warning("PID %s가 SIGTERM에 응답하지 않아 SIGKILL을 보냅니다.", pid)
            try:
                os.kill(pid, signal.SIGKILL)  # type: ignore[arg-type]
            except (ProcessLookupError, PermissionError, TypeError):
                pass
            time.sleep(2)
        pid_gone = not process_exists(pid)

    timeout = float(memory_config.get("unload_wait_timeout_sec", 60))
    interval = float(memory_config.get("unload_poll_interval_sec", 2.0))
    allow_drop = bool(memory_config.get("allow_cache_drop", False))

    started = time.monotonic()
    cache_dropped = False
    snapshot = guard.snapshot()
    dropped_once = False
    while True:
        snapshot = guard.snapshot()
        if snapshot.mem_available_mb >= target_mb:
            break
        elapsed = time.monotonic() - started
        if elapsed >= timeout / 2 and allow_drop and not dropped_once:
            dropped_once = True
            cache_dropped = guard.maybe_drop_caches_after_vlm(True, not pid_gone)
            if cache_dropped:
                log.info("page cache를 비웠습니다.")
        if elapsed >= timeout:
            break
        log.info("메모리 회수 대기 중: MemAvailable=%s MiB target=%s MiB", snapshot.mem_available_mb, target_mb)
        time.sleep(interval)

    waited = time.monotonic() - started
    passed = pid_gone and snapshot.mem_available_mb >= target_mb
    note = ""
    if not pid_gone:
        note = f"{model_label} PID {pid}가 소멸하지 않았습니다"
    elif not passed:
        note = f"MemAvailable {snapshot.mem_available_mb} MiB < 목표 {target_mb} MiB ({waited:.1f}s 대기)"

    evidence = UnloadEvidence(
        model=model_label, pid=pid, pid_gone=pid_gone,
        available_before_mb=before.mem_available_mb,
        available_after_mb=snapshot.mem_available_mb,
        target_mb=target_mb, waited_sec=waited,
        cache_dropped=cache_dropped, passed=passed, note=note,
    )
    log.info("unload gate 결과: %s", json.dumps(evidence.to_dict(), ensure_ascii=False))
    return evidence


def required_free_mb(tier: ModelTier, memory_config: dict[str, Any]) -> int:
    """게이트 통과 조건: 선택된 모델 예상 RSS + headroom.

    llama.cpp는 GGUF를 mmap하므로 peak RSS 대부분이 파일 백업 페이지다.
    그래서 tier에 min_free_mb가 명시돼 있으면 그 실측 기반 값을 우선한다.
    """
    explicit = tier.min_free_mb
    if explicit is not None:
        return explicit
    headroom = int(memory_config.get("headroom_mb", 1500))
    return tier.expected_rss_mb + headroom


# --------------------------------------------------------------------------
# VLM: llama-mtmd-cli 는 이미지 1장마다 프로세스가 뜨고 죽는 one-shot 구조다.
# --------------------------------------------------------------------------
class MtmdVisionRuntime:
    kind = "vlm"

    def __init__(self, tier: ModelTier, settings: Settings, guard: MemoryGuard, logger: logging.Logger | None = None) -> None:
        self.tier = tier
        self.settings = settings
        self.guard = guard
        self.log = logger or LOG
        self.last_pid: int | None = None

    def build_command(self, image_path: Path, prompt_path: Path, max_tokens: int | None = None) -> list[str]:
        binary = self.tier.binary
        model = self.tier.model_path
        mmproj = self.tier.mmproj_path
        if binary is None or model is None or mmproj is None:
            raise RuntimeStartError(f"VLM tier '{self.tier.name}' 경로가 불완전합니다")
        return [
            str(binary),
            "-m", str(model),
            "--mmproj", str(mmproj),
            "--image", str(image_path),
            "-f", str(prompt_path),
            "-t", str(self.tier.get("threads", 4)),
            "-c", str(self.tier.get("ctx_size", 4096)),
            "-n", str(max_tokens if max_tokens is not None else self.tier.get("max_tokens", 420)),
            "--temp", str(self.tier.get("temperature", 0.2)),
            "-ngl", str(self.tier.get("gpu_layers", 0)),
            "--no-mmproj-offload",
        ]

    def analyze(self, image_path: Path, prompt_path: Path, stdout_path: Path, stderr_path: Path, timeout_sec: float, max_tokens: int | None = None):
        args = self.build_command(image_path, prompt_path, max_tokens)
        self.log.info("VLM 실행: %s", image_path.name)
        result = run_process(
            args, stdout_path, stderr_path, timeout_sec,
            float(self.settings.timeouts.get("process_terminate_grace_sec", 15)),
            self.guard.should_abort_running_process,
            float(self.settings.memory.get("unload_poll_interval_sec", 2.0)),
        )
        self.last_pid = result.pid
        return result

    def unload(self) -> UnloadEvidence:
        """one-shot이므로 프로세스는 이미 죽었다. 그래도 실측으로 확인한다."""
        llm_tier = self.settings.llm_tiers[0] if self.settings.llm_tiers else None
        target = required_free_mb(llm_tier, self.settings.memory) if llm_tier else int(self.settings.memory.get("min_free_mb_for_llm", 12288))
        return unload_gate(self.guard, self.settings.memory, target, f"VLM:{self.tier.name}", self.last_pid, self.log)


# --------------------------------------------------------------------------
# LLM: llama-server 로 한 번만 올리고 섹션마다 /completion 을 호출한다.
# cache_prompt=true 로 공통 프리픽스의 KV 캐시를 재사용한다.
# 근거: 실측 prompt eval 8.57 tok/s. one-shot 재적재는 섹션당 약 140s의 프리필을 반복한다.
# --------------------------------------------------------------------------
class LlamaServerRuntime:
    kind = "llm"

    def __init__(self, tier: ModelTier, settings: Settings, guard: MemoryGuard, logger: logging.Logger | None = None) -> None:
        self.tier = tier
        self.settings = settings
        self.guard = guard
        self.log = logger or LOG
        self.process: subprocess.Popen[bytes] | None = None
        self.pid: int | None = None
        self.port = int(tier.get("port", 8771))
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path: Path | None = None

    def build_command(self) -> list[str]:
        binary = self.tier.binary
        model = self.tier.model_path
        if binary is None or model is None:
            raise RuntimeStartError(f"LLM tier '{self.tier.name}' 경로가 불완전합니다")
        args = [
            str(binary),
            "-m", str(model),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-t", str(self.tier.get("threads", 4)),
            "--threads-batch", str(self.tier.get("threads_batch", 4)),
            "-c", str(self.tier.get("ctx_size", 8192)),
            "-b", str(self.tier.get("batch_size", 128)),
            "-ub", str(self.tier.get("ubatch_size", 64)),
            "-ngl", str(self.tier.get("gpu_layers", 0)),
            "--parallel", "1",
        ]
        if self.tier.get("cache_type_k"):
            args += ["--cache-type-k", str(self.tier.get("cache_type_k"))]
        if self.tier.get("cache_type_v"):
            args += ["--cache-type-v", str(self.tier.get("cache_type_v"))]
        return args

    def start(self, log_path: Path) -> None:
        if self.process is not None:
            return
        args = self.build_command()
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("wb")
        self.log.info("llama-server 기동: %s (port=%s)", self.tier.name, self.port)
        self.process = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        self.pid = self.process.pid
        timeout = float(self.settings.timeouts.get("llm_server_start_sec", 300))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            exit_code = self.process.poll()
            if exit_code is not None:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                self.process = None
                raise RuntimeStartError(f"llama-server가 기동 중 종료됐습니다 (exit={exit_code}).\n{tail}")
            if self._health_ok():
                self.log.info("llama-server 준비 완료 (%.1fs)", timeout - (deadline - time.monotonic()))
                return
            time.sleep(2)
        self.stop()
        raise RuntimeStartError(f"llama-server가 {timeout}s 안에 준비되지 않았습니다")

    def _health_ok(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=5) as response:
                return response.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
            return False

    def complete(self, prompt: str, max_tokens: int | None = None, stop: list[str] | None = None, temperature: float | None = None) -> dict[str, Any]:
        if self.process is None:
            raise GenerationError("llama-server가 실행 중이 아닙니다")
        payload = {
            "prompt": prompt,
            "n_predict": int(max_tokens if max_tokens is not None else self.tier.get("max_tokens", 560)),
            "temperature": float(temperature if temperature is not None else self.tier.get("temperature", 0.7)),
            "top_p": float(self.tier.get("top_p", 0.9)),
            "top_k": int(self.tier.get("top_k", 40)),
            "repeat_penalty": float(self.tier.get("repeat_penalty", 1.08)),
            "cache_prompt": True,
            "stream": False,
            "stop": stop or [],
        }
        request = urllib.request.Request(
            f"{self.base_url}/completion",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = float(self.settings.timeouts.get("llm_section_sec", 900))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise GenerationError(f"llama-server 호출 실패: {exc}") from exc
        if "content" not in body:
            raise GenerationError(f"llama-server 응답에 content가 없습니다: {list(body)[:8]}")
        return body

    def stop(self) -> None:
        if self.process is None:
            return
        proc = self.process
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.5)
        if proc.poll() is None:
            self.log.warning("llama-server가 SIGTERM에 응답하지 않아 SIGKILL을 보냅니다.")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait(timeout=10)
        self.process = None

    def unload(self, target_mb: int | None = None) -> UnloadEvidence:
        pid = self.pid
        self.stop()
        target = target_mb if target_mb is not None else int(self.settings.memory.get("min_free_mb_for_vlm", 4096))
        return unload_gate(self.guard, self.settings.memory, target, f"LLM:{self.tier.name}", pid, self.log)


# --------------------------------------------------------------------------
# 폴백 런타임: Ollama. 로컬 데몬이므로 오프라인에서도 동작한다.
# --------------------------------------------------------------------------
class OllamaRuntime:
    def __init__(self, tier: ModelTier, settings: Settings, guard: MemoryGuard, kind: str, logger: logging.Logger | None = None) -> None:
        self.tier = tier
        self.settings = settings
        self.guard = guard
        self.kind = kind
        self.log = logger or LOG
        self.endpoint = str(tier.get("endpoint", "http://127.0.0.1:11434")).rstrip("/")
        self.model = str(tier.get("model", ""))

    def available(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(f"{self.endpoint}/api/tags", timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            return False, f"ollama 데몬에 연결할 수 없습니다: {exc}"
        names = {m.get("name", "") for m in body.get("models", [])}
        if self.model not in names:
            return False, f"ollama에 모델이 없습니다: {self.model}"
        return True, ""

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise GenerationError(f"ollama 호출 실패: {exc}") from exc

    def complete(self, prompt: str, images_b64: list[str] | None = None, max_tokens: int | None = None, stop: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "5m",
            "options": {
                "num_predict": int(max_tokens if max_tokens is not None else self.tier.get("max_tokens", 480)),
                "temperature": float(self.tier.get("temperature", 0.7)),
                "top_p": float(self.tier.get("top_p", 0.9)),
                "num_ctx": int(self.tier.get("ctx_size", 4096)),
            },
        }
        if images_b64:
            payload["images"] = images_b64
        if stop:
            payload["options"]["stop"] = stop
        timeout = float(self.settings.timeouts.get("llm_section_sec", 900))
        body = self._post("/api/generate", payload, timeout)
        return {"content": body.get("response", ""), "raw": body}

    def unload(self, target_mb: int | None = None) -> UnloadEvidence:
        """keep_alive=0 으로 모델을 즉시 내리고 메모리 회수를 실측한다."""
        try:
            self._post("/api/generate", {"model": self.model, "keep_alive": 0, "prompt": ""}, 30)
        except GenerationError as exc:
            self.log.warning("ollama 언로드 요청 실패: %s", exc)
        subprocess.run(["ollama", "stop", self.model], check=False, capture_output=True, timeout=30)
        target = target_mb if target_mb is not None else int(self.settings.memory.get("min_free_mb_for_llm", 12288))
        # ollama 서버 PID는 우리 소유가 아니므로 PID 소멸이 아니라 메모리 회수만 검증한다.
        evidence = unload_gate(self.guard, self.settings.memory, target, f"ollama:{self.model}", None, self.log)
        return evidence


def select_tier(
    settings: Settings,
    kind: str,
    guard: MemoryGuard,
    skip_names: set[str] | None = None,
    logger: logging.Logger | None = None,
) -> tuple[ModelTier, list[str]]:
    """사다리 위에서부터 실행 가능한 첫 tier를 고른다. 내려간 이유를 전부 돌려준다."""
    log = logger or LOG
    skip = skip_names or set()
    notes: list[str] = []
    snapshot = guard.snapshot()
    for tier, ok, reason in settings.available_tiers(kind):
        if tier.name in skip:
            notes.append(f"{tier.name}: 이번 실행에서 제외됨")
            continue
        if not ok:
            notes.append(f"{tier.name}: {reason}")
            continue
        if kind == "llm":
            need = required_free_mb(tier, settings.memory)
            if snapshot.mem_available_mb < need:
                notes.append(f"{tier.name}: MemAvailable {snapshot.mem_available_mb} MiB < 필요 {need} MiB")
                continue
        if tier.backend == "ollama":
            probe = OllamaRuntime(tier, settings, guard, kind, log)
            usable, why = probe.available()
            if not usable:
                notes.append(f"{tier.name}: {why}")
                continue
        for note in notes:
            log.warning("tier 하향: %s", note)
        return tier, notes
    raise AgentError(f"사용 가능한 {kind} tier가 없습니다. 사유: " + "; ".join(notes))
