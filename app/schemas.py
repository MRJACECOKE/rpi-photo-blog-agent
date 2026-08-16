from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class AgentError(Exception):
    """Base error for controlled agent failures."""


class ConfigError(AgentError):
    pass


class MemoryGuardError(AgentError):
    pass


class ModelSelectionError(AgentError):
    pass


class ProcessExecutionError(AgentError):
    pass


class ProcessTimeoutError(ProcessExecutionError):
    pass


class ProcessKilledError(ProcessExecutionError):
    pass


class VisionOutputError(AgentError):
    pass


class BlogOutputError(AgentError):
    pass


class StateError(AgentError):
    pass


class LockError(AgentError):
    pass


class RunState(str, Enum):
    INITIALIZING = "INITIALIZING"
    PRECHECK = "PRECHECK"
    PREPROCESSING_IMAGE = "PREPROCESSING_IMAGE"
    WAITING_FOR_VLM_MEMORY = "WAITING_FOR_VLM_MEMORY"
    RUNNING_VLM = "RUNNING_VLM"
    STOPPING_VLM = "STOPPING_VLM"
    WAITING_FOR_MEMORY_RELEASE = "WAITING_FOR_MEMORY_RELEASE"
    BUILDING_BLOG_PROMPT = "BUILDING_BLOG_PROMPT"
    WAITING_FOR_LLM_MEMORY = "WAITING_FOR_LLM_MEMORY"
    RUNNING_LLM = "RUNNING_LLM"
    STOPPING_LLM = "STOPPING_LLM"
    WRITING_OUTPUT = "WRITING_OUTPUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RunRequest:
    image: Path
    topic: str = ""
    audience: str = ""
    tone: str = ""
    keywords: str = ""
    output: Path | None = None
    language: str = "ko"
    dry_run: bool = False
    keep_run_files: bool = False
    verbose: bool = False
    vlm_model: Path | None = None
    vlm_mmproj: Path | None = None
    llm_model: Path | None = None
    force_oversized_model: bool = False


@dataclass(frozen=True)
class PreparedImage:
    source_path: Path
    prepared_path: Path
    source_sha256: str
    original_size: tuple[int, int]
    prepared_size: tuple[int, int]
    bytes_written: int


@dataclass(frozen=True)
class ModelSelection:
    repo_id: str
    quantization: str
    files: tuple[Path, ...]
    primary_path: Path
    total_bytes: int
    oversized: bool = False
    mmproj_path: Path | None = None


@dataclass
class ProcessResult:
    args: list[str]
    exit_code: int
    duration_seconds: float
    pid: int | None = None
    peak_rss_mb: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    killed_for_safety: bool = False
    timed_out: bool = False
    oom_suspected: bool = False


@dataclass
class Metrics:
    run_id: str
    started_at: str
    finished_at: str = ""
    status: str = "running"
    input_sha256: str = ""
    vlm: dict[str, Any] = field(default_factory=dict)
    memory_release: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    thermal: dict[str, Any] = field(default_factory=lambda: {"maximum_celsius": None, "throttling_detected": False})
    output_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "input_sha256": self.input_sha256,
            "vlm": self.vlm,
            "memory_release": self.memory_release,
            "llm": self.llm,
            "thermal": self.thermal,
            "output_file": self.output_file,
        }
