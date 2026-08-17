from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.job_state import Job, JobStore, Stage, StageStatus
from app.pipeline import Pipeline
from app.schemas import AgentError
from app.settings import ModelTier, Settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def pipeline(tmp_path, monkeypatch) -> Pipeline:
    settings = Settings.load(ROOT)
    # job 파일을 실제 state/에 쓰지 않도록 격리한다
    object.__setattr__(settings, "runtime", {**settings.runtime, "paths": {**settings.runtime["paths"], "state_dir": str(tmp_path / "state")}})
    return Pipeline(settings)


def make_job() -> Job:
    return Job(
        job_id="preflight-test",
        created_at="2026-08-17T00:00:00+09:00",
        input_dir="inbox/preflight-test",
        stage_status={s.value: StageStatus.NOT_STARTED.value for s in Stage.ordered()},
    )


def test_preflight_passes_on_healthy_system(pipeline):
    job = make_job()
    pipeline.store.save(job)
    pipeline.preflight(job)  # 예외가 없어야 한다
    assert job.status_of(Stage.PREP) is not StageStatus.BLOCKED_EXTERNAL


def test_low_disk_blocks_externally_without_deleting_anything(pipeline, monkeypatch):
    job = make_job()
    pipeline.store.save(job)

    class Tiny:
        total = 100 * 1024**3
        used = 100 * 1024**3
        free = 1 * 1024**2  # 1 MiB

    monkeypatch.setattr(shutil, "disk_usage", lambda _path: Tiny)

    work_before = sorted(p.name for p in pipeline.settings.work_dir.glob("*")) if pipeline.settings.work_dir.exists() else []
    with pytest.raises(AgentError, match="디스크"):
        pipeline.preflight(job)

    assert job.status_of(Stage.PREP) is StageStatus.BLOCKED_EXTERNAL
    assert job.blockers or "디스크" in str(job.stage_status)
    # 자동 삭제는 하지 않는다
    work_after = sorted(p.name for p in pipeline.settings.work_dir.glob("*")) if pipeline.settings.work_dir.exists() else []
    assert work_before == work_after


def test_missing_models_block_externally_with_download_command(pipeline, monkeypatch):
    job = make_job()
    pipeline.store.save(job)

    # Settings는 frozen dataclass라 인스턴스 속성을 바꿀 수 없다. tier 판정 자체를 막는다.
    monkeypatch.setattr(ModelTier, "is_available", lambda self: (False, "모델 파일 없음: /nowhere.gguf"))

    with pytest.raises(AgentError) as excinfo:
        pipeline.preflight(job)

    message = str(excinfo.value)
    assert "download_models.py" in message
    assert job.status_of(Stage.PREP) is StageStatus.BLOCKED_EXTERNAL
