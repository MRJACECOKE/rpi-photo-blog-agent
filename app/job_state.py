"""Job 상태머신과 영속화.

- 각 스테이지 시작/종료마다 atomic write (temp -> os.replace)
- 중단 후 재실행 시 완료된 스테이지는 건너뛴다 (resume)
- state/run.lock으로 중복 실행 방지. PID가 죽은 stale lock은 자동 해제한다.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .metrics import atomic_write_json
from .schemas import AgentError, LockError

KST = timezone(timedelta(hours=9), "KST")


class Stage(str, Enum):
    PREP = "STAGE_1_PREP"
    VISION = "STAGE_2_VISION"
    UNLOAD = "STAGE_3_UNLOAD"
    WRITE = "STAGE_4_WRITE"
    QUALITY = "STAGE_5_QUALITY"
    OUTPUT = "STAGE_6_OUTPUT"

    @classmethod
    def ordered(cls) -> tuple["Stage", ...]:
        return (cls.PREP, cls.VISION, cls.UNLOAD, cls.WRITE, cls.QUALITY, cls.OUTPUT)


class StageStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"


class ImageStatus(str, Enum):
    OK = "OK"
    PRIVACY_HOLD = "PRIVACY_HOLD"
    SKIPPED = "SKIPPED"
    DUPLICATE = "DUPLICATE"
    FAILED = "FAILED"


def now_kst() -> str:
    return datetime.now(KST).replace(microsecond=0).isoformat()


def slugify(text: str, fallback: str = "photo") -> str:
    """한글 제목도 안전한 영문 slug로 바꾼다. 로마자 변환이 불가능하면 fallback을 쓴다."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:60] or fallback


@dataclass
class JobImage:
    file: str  # inbox 원본 파일명
    sha256: str = ""
    status: str = ImageStatus.OK.value
    slug: str = ""
    vlm_image: str = ""  # work/<job>/images/ 상대 경로
    publish_image: str = ""
    width: int = 0
    height: int = 0
    privacy_flags: dict[str, Any] = field(default_factory=dict)
    privacy_reasons: list[str] = field(default_factory=list)
    vision_json: str = ""  # work/<job>/vision/ 상대 경로
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetryRecord:
    at: str
    stage: str
    attempt: int
    failure: str
    root_cause: str
    action: str
    result: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Job:
    job_id: str
    created_at: str
    input_dir: str
    images: list[JobImage] = field(default_factory=list)
    stage: str = Stage.PREP.value
    stage_status: dict[str, str] = field(default_factory=dict)
    models_used: dict[str, str] = field(default_factory=dict)
    retries: list[RetryRecord] = field(default_factory=list)
    output: dict[str, Any] | None = None
    blockers: list[str] = field(default_factory=list)
    # 이번 스코프에서 추가된 필드
    topic_hint: str = ""
    category: str = ""
    updated_at: str = ""
    progress: dict[str, Any] = field(default_factory=dict)
    memory_evidence: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    input_set_hash: str = ""

    # ---- 상태 조회 ----
    def status_of(self, stage: Stage) -> StageStatus:
        return StageStatus(self.stage_status.get(stage.value, StageStatus.NOT_STARTED.value))

    def is_done(self, stage: Stage) -> bool:
        return self.status_of(stage) is StageStatus.SUCCESS

    def usable_images(self) -> list[JobImage]:
        """본문에 실을 수 있는 이미지. PRIVACY_HOLD와 실패 이미지는 제외한다."""
        return [img for img in self.images if img.status == ImageStatus.OK.value]

    def held_images(self) -> list[JobImage]:
        return [img for img in self.images if img.status == ImageStatus.PRIVACY_HOLD.value]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["images"] = [img.to_dict() for img in self.images]
        data["retries"] = [r.to_dict() for r in self.retries]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        images = [JobImage(**img) for img in data.get("images", [])]
        retries = [RetryRecord(**r) for r in data.get("retries", [])]
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in data.items() if k in known}
        payload["images"] = images
        payload["retries"] = retries
        return cls(**payload)


class JobStore:
    def __init__(self, state_dir: Path) -> None:
        self.jobs_dir = state_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, job_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", job_id):
            raise AgentError(f"허용되지 않는 job_id: {job_id}")
        return self.jobs_dir / f"{job_id}.json"

    def exists(self, job_id: str) -> bool:
        return self.path_for(job_id).exists()

    def save(self, job: Job) -> None:
        job.updated_at = now_kst()
        atomic_write_json(self.path_for(job.job_id), job.to_dict())

    def load(self, job_id: str) -> Job:
        path = self.path_for(job_id)
        if not path.exists():
            raise AgentError(f"job을 찾을 수 없습니다: {job_id}")
        with path.open("r", encoding="utf-8") as handle:
            return Job.from_dict(json.load(handle))

    def list_jobs(self) -> list[Job]:
        jobs = []
        for path in sorted(self.jobs_dir.glob("*.json"), reverse=True):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    jobs.append(Job.from_dict(json.load(handle)))
            except (json.JSONDecodeError, TypeError, OSError):
                continue
        return jobs

    def find_by_input_hash(self, input_set_hash: str, exclude_job_id: str = "") -> list[Job]:
        """같은 이미지 집합으로 이미 완료된 job을 찾는다 (Idempotency)."""
        if not input_set_hash:
            return []
        return [
            job
            for job in self.list_jobs()
            if job.input_set_hash == input_set_hash
            and job.job_id != exclude_job_id
            and job.is_done(Stage.OUTPUT)
        ]

    # ---- 스테이지 전이 ----
    def begin_stage(self, job: Job, stage: Stage) -> None:
        job.stage = stage.value
        job.stage_status[stage.value] = StageStatus.IN_PROGRESS.value
        job.progress = {"stage": stage.value, "detail": "", "current": 0, "total": 0, "at": now_kst()}
        self.save(job)

    def finish_stage(self, job: Job, stage: Stage, status: StageStatus, detail: str = "") -> None:
        job.stage_status[stage.value] = status.value
        if detail:
            job.progress = {"stage": stage.value, "detail": detail, "current": 0, "total": 0, "at": now_kst()}
        if status is StageStatus.BLOCKED_EXTERNAL and detail and detail not in job.blockers:
            job.blockers.append(detail)
        self.save(job)

    def record_progress(self, job: Job, detail: str, current: int = 0, total: int = 0) -> None:
        job.progress = {"stage": job.stage, "detail": detail, "current": current, "total": total, "at": now_kst()}
        self.save(job)

    def record_retry(self, job: Job, stage: Stage, failure: str, root_cause: str, action: str, result: str = "") -> RetryRecord:
        attempt = sum(1 for r in job.retries if r.stage == stage.value) + 1
        record = RetryRecord(
            at=now_kst(), stage=stage.value, attempt=attempt,
            failure=failure, root_cause=root_cause, action=action, result=result,
        )
        job.retries.append(record)
        self.save(job)
        return record


class RunLock:
    """PID 기록 + stale lock 자동 해제를 포함한 단일 실행 락."""

    def __init__(self, lock_path: Path, job_id: str, takeover_stale: bool = True) -> None:
        self.lock_path = lock_path
        self.job_id = job_id
        self.takeover_stale = takeover_stale
        self._acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def read(self) -> dict[str, Any] | None:
        if not self.lock_path.exists():
            return None
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def __enter__(self) -> "RunLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read()
        if existing:
            pid = int(existing.get("pid", 0) or 0)
            if pid and self._pid_alive(pid) and pid != os.getpid():
                raise LockError(
                    f"다른 실행이 진행 중입니다 (pid={pid}, job={existing.get('job_id')}). "
                    f"중복 실행을 막기 위해 시작하지 않습니다."
                )
            if not self.takeover_stale:
                raise LockError(f"stale lock이 남아 있습니다: {self.lock_path}")
            # stale lock: 프로세스가 없으므로 인수한다.
        atomic_write_json(self.lock_path, {"pid": os.getpid(), "job_id": self.job_id, "started_at": now_kst()})
        self._acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._acquired:
            current = self.read()
            if current and int(current.get("pid", 0) or 0) == os.getpid():
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
        return False
