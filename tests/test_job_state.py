from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.job_state import (
    ImageStatus, Job, JobImage, JobStore, RunLock, Stage, StageStatus, slugify,
)
from app.schemas import LockError


def make_job(job_id: str = "20260817-1042-oak-kitchen") -> Job:
    return Job(
        job_id=job_id,
        created_at="2026-08-17T10:42:00+09:00",
        input_dir=f"inbox/{job_id}",
        stage_status={stage.value: StageStatus.NOT_STARTED.value for stage in Stage.ordered()},
    )


def test_slugify_handles_korean_and_symbols():
    assert slugify("내추럴 오크 주방", fallback="kitchen") == "kitchen"
    assert slugify("Oak Kitchen -- 2026!") == "oak-kitchen-2026"
    assert slugify("") == "photo"


def test_job_roundtrip_preserves_images(tmp_path: Path):
    store = JobStore(tmp_path)
    job = make_job()
    job.images = [JobImage(file="a.jpg", sha256="x", slug="k-01", status=ImageStatus.OK.value)]
    store.save(job)
    loaded = store.load(job.job_id)
    assert loaded.images[0].slug == "k-01"
    assert isinstance(loaded.images[0], JobImage)


def test_job_json_is_written_atomically(tmp_path: Path):
    store = JobStore(tmp_path)
    job = make_job()
    store.save(job)
    path = store.path_for(job.job_id)
    assert json.loads(path.read_text(encoding="utf-8"))["job_id"] == job.job_id
    # 임시 파일이 남지 않아야 한다
    assert [p.name for p in (tmp_path / "jobs").iterdir()] == [path.name]


def test_stage_transitions_and_resume_flags(tmp_path: Path):
    store = JobStore(tmp_path)
    job = make_job()
    store.begin_stage(job, Stage.PREP)
    assert job.status_of(Stage.PREP) is StageStatus.IN_PROGRESS
    assert not job.is_done(Stage.PREP)
    store.finish_stage(job, Stage.PREP, StageStatus.SUCCESS, "4장")
    assert job.is_done(Stage.PREP)
    assert store.load(job.job_id).is_done(Stage.PREP)


def test_blocked_external_is_recorded_as_blocker(tmp_path: Path):
    store = JobStore(tmp_path)
    job = make_job()
    store.finish_stage(job, Stage.PREP, StageStatus.BLOCKED_EXTERNAL, "모델 파일 없음")
    assert "모델 파일 없음" in job.blockers


def test_usable_and_held_images_are_separated():
    job = make_job()
    job.images = [
        JobImage(file="a.jpg", status=ImageStatus.OK.value),
        JobImage(file="b.jpg", status=ImageStatus.PRIVACY_HOLD.value),
        JobImage(file="c.jpg", status=ImageStatus.SKIPPED.value),
    ]
    assert [i.file for i in job.usable_images()] == ["a.jpg"]
    assert [i.file for i in job.held_images()] == ["b.jpg"]


def test_retry_records_increment_per_stage(tmp_path: Path):
    store = JobStore(tmp_path)
    job = make_job()
    first = store.record_retry(job, Stage.VISION, "실패1", "원인", "조치")
    second = store.record_retry(job, Stage.VISION, "실패2", "원인", "조치")
    other = store.record_retry(job, Stage.WRITE, "실패3", "원인", "조치")
    assert (first.attempt, second.attempt, other.attempt) == (1, 2, 1)


def test_duplicate_input_hash_finds_completed_jobs_only(tmp_path: Path):
    store = JobStore(tmp_path)
    done = make_job("job-done")
    done.input_set_hash = "abc"
    done.stage_status[Stage.OUTPUT.value] = StageStatus.SUCCESS.value
    store.save(done)

    unfinished = make_job("job-running")
    unfinished.input_set_hash = "abc"
    store.save(unfinished)

    found = store.find_by_input_hash("abc", exclude_job_id="job-new")
    assert [j.job_id for j in found] == ["job-done"]


def test_rejects_unsafe_job_id(tmp_path: Path):
    store = JobStore(tmp_path)
    with pytest.raises(Exception):
        store.path_for("../escape")


def test_run_lock_blocks_live_pid(tmp_path: Path):
    lock_path = tmp_path / "run.lock"
    with RunLock(lock_path, "job-a"):
        assert lock_path.exists()
        # 살아 있는 다른 PID를 흉내 낸다 (PID 1은 항상 존재한다)
        lock_path.write_text(json.dumps({"pid": 1, "job_id": "other"}), encoding="utf-8")
        with pytest.raises(LockError):
            with RunLock(lock_path, "job-b"):
                pass


def test_run_lock_takes_over_stale_lock(tmp_path: Path):
    lock_path = tmp_path / "run.lock"
    # 존재하지 않는 PID로 stale lock을 만든다
    dead_pid = 999999
    assert not Path(f"/proc/{dead_pid}").exists()
    lock_path.write_text(json.dumps({"pid": dead_pid, "job_id": "ghost"}), encoding="utf-8")
    with RunLock(lock_path, "job-new"):
        current = json.loads(lock_path.read_text(encoding="utf-8"))
        assert current["pid"] == os.getpid()
    assert not lock_path.exists()
