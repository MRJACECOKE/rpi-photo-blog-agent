"""로컬 GUI (FastAPI + Jinja2 + 순수 JS).

GUI는 파이프라인을 직접 실행하지 않는다. app.pipeline.Pipeline을 호출만 한다.
CLI(`python -m app.cli_jobs run --job <id>`)와 100% 동일하게 동작해야 한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import shutil
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..job_state import JobStore, Stage, now_kst
from ..memory_guard import MemoryGuard
from ..pipeline import Pipeline, create_job, new_job_id
from ..schemas import AgentError, LockError
from ..settings import Settings

LOG = logging.getLogger("blog_agent.gui")

BASE_DIR = Path(__file__).resolve().parent
SETTINGS = Settings.load(Path.cwd())
STORE = JobStore(SETTINGS.state_dir)
GUARD = MemoryGuard(int(SETTINGS.memory.get("min_free_mb_during_run", 1024)), 2.0, LOG)

app = FastAPI(title="로컬 블로그 에이전트", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
(BASE_DIR / "static").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------------------
# 실행 상태 관리 — 동시 실행은 1개만 허용한다 (VLM/LLM 동시 적재 금지가 근거).
# --------------------------------------------------------------------------
class JobRunner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current_job: str | None = None
        self.thread: threading.Thread | None = None
        self.pipeline: Pipeline | None = None
        self.cancel_requested = False
        self.subscribers: dict[str, list[queue.Queue]] = {}
        self.log_buffer: deque[str] = deque(maxlen=int(SETTINGS.gui.get("log_tail_lines", 200)))
        self.last_result: dict[str, Any] = {}

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def subscribe(self, job_id: str) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=500)
        self.subscribers.setdefault(job_id, []).append(channel)
        return channel

    def unsubscribe(self, job_id: str, channel: queue.Queue) -> None:
        channels = self.subscribers.get(job_id, [])
        if channel in channels:
            channels.remove(channel)

    def publish(self, job_id: str, event: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"event": event, **payload}, ensure_ascii=False)
        if event == "log":
            self.log_buffer.append(payload.get("line", ""))
        for channel in list(self.subscribers.get(job_id, [])):
            try:
                channel.put_nowait(message)
            except queue.Full:
                pass

    def start(self, job_id: str) -> None:
        with self.lock:
            if self.busy():
                raise LockError(f"이미 다른 작업이 실행 중입니다: {self.current_job}")
            self.current_job = job_id
            self.cancel_requested = False
            self.thread = threading.Thread(target=self._run, args=(job_id,), daemon=True)
            self.thread.start()

    def _run(self, job_id: str) -> None:
        handler = _SseLogHandler(self, job_id)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        root = logging.getLogger()
        root.addHandler(handler)

        def progress(event: str, payload: dict[str, Any]) -> None:
            self.publish(job_id, event, payload)
            if self.cancel_requested:
                raise KeyboardInterrupt("사용자가 취소를 요청했습니다")

        try:
            self.pipeline = Pipeline(SETTINGS, LOG, progress)
            result = self.pipeline.run(job_id)
            self.last_result = {
                "job_id": result.job_id, "completed": result.completed,
                "output_txt": result.output_txt, "quality_passed": result.quality_passed,
            }
            self.publish(job_id, "done", self.last_result)
        except KeyboardInterrupt:
            self.publish(job_id, "cancelled", {"job_id": job_id, "message": "사용자가 취소했습니다. 모델을 내리고 종료합니다."})
            if self.pipeline is not None:
                try:
                    self.pipeline.cancel_cleanup()
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("취소 정리 실패: %s", exc)
        except Exception as exc:  # noqa: BLE001 - GUI는 어떤 실패도 화면에 보여야 한다
            LOG.exception("파이프라인 실패")
            self.publish(job_id, "error", {"job_id": job_id, "message": str(exc)})
        finally:
            root.removeHandler(handler)
            self.pipeline = None
            self.current_job = None


class _SseLogHandler(logging.Handler):
    def __init__(self, runner: "JobRunner", job_id: str) -> None:
        super().__init__(level=logging.INFO)
        self.runner = runner
        self.job_id = job_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.runner.publish(self.job_id, "log", {"line": self.format(record)})
        except Exception:  # noqa: BLE001
            pass


RUNNER = JobRunner()


# --------------------------------------------------------------------------
# 인증 — runtime.yaml에 토큰이 있을 때만 검사한다. 기본은 LAN 전용.
# --------------------------------------------------------------------------
def require_token(request: Request) -> None:
    token = str(SETTINGS.gui.get("auth_token", "") or "")
    if not token:
        return
    provided = request.headers.get("X-Auth-Token") or request.query_params.get("token", "")
    if provided != token:
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다")


def _read_active_lock() -> dict[str, Any] | None:
    """run.lock의 주인이 살아 있으면 그 내용을 돌려준다. 죽은 lock은 없는 것으로 본다."""
    path = SETTINGS.lock_path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0) or 0)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if not pid:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return data
    return data


def _job_view(job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "category": job.category,
        "topic_hint": job.topic_hint,
        "stage": job.stage,
        "stage_status": {s.value: job.stage_status.get(s.value, "NOT_STARTED") for s in Stage.ordered()},
        "progress": job.progress,
        "images": [
            {
                "file": i.file, "slug": i.slug, "status": i.status,
                "publish_image": i.publish_image, "reasons": i.privacy_reasons,
            }
            for i in job.images
        ],
        "images_total": len(job.images),
        "images_usable": len(job.usable_images()),
        "images_held": len(job.held_images()),
        "models_used": job.models_used,
        "output": job.output,
        "blockers": job.blockers,
        "quality": job.quality,
        "retries": [r.to_dict() for r in job.retries],
        "memory_evidence": job.memory_evidence,
        "timings": job.timings,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html",
        {"categories": list(SETTINGS.categories), "default_category": SETTINGS.blog.get("default_category", "")},
    )


@app.get("/api/system")
def api_system(_: None = Depends(require_token)) -> dict[str, Any]:
    snapshot = GUARD.snapshot()
    disk = shutil.disk_usage(SETTINGS.root)

    # 실행 주체는 GUI일 수도, CLI일 수도 있다. run.lock을 봐야 장비의 실제 상태를 보고할 수 있다.
    active_job = RUNNER.current_job
    running = RUNNER.busy()
    lock_owner = _read_active_lock()
    if lock_owner:
        running = True
        active_job = active_job or lock_owner.get("job_id")

    loaded = "없음"
    if running:
        stage = ""
        if active_job:
            try:
                stage = STORE.load(active_job).stage
            except AgentError:
                stage = ""
        loaded = {"STAGE_2_VISION": "VLM", "STAGE_4_WRITE": "LLM"}.get(stage, "준비 중")
    return {
        "at": now_kst(),
        "memory": {
            "total_mb": snapshot.mem_total_mb,
            "available_mb": snapshot.mem_available_mb,
            "used_percent": round(100 * (1 - snapshot.mem_available_mb / max(1, snapshot.mem_total_mb)), 1),
            "swap_used_percent": round(snapshot.swap_used_percent, 1),
        },
        "thermal": {
            "cpu_temp_c": snapshot.cpu_temp_c,
            "throttled": snapshot.throttled,
            "throttling_detected": snapshot.throttled not in (None, "throttled=0x0"),
        },
        "disk": {"free_gib": round(disk.free / 1024**3, 1), "total_gib": round(disk.total / 1024**3, 1)},
        "loaded_model": loaded,
        "busy": running,
        "current_job": active_job,
        "runner": "gui" if RUNNER.busy() else ("cli" if lock_owner else "idle"),
    }


@app.get("/api/jobs")
def api_jobs(_: None = Depends(require_token)) -> list[dict[str, Any]]:
    return [_job_view(job) for job in STORE.list_jobs()]


@app.post("/api/jobs")
async def api_create_job(
    _: None = Depends(require_token),
    files: list[UploadFile] = File(...),
    category: str = Form(""),
    topic: str = Form(""),
) -> JSONResponse:
    category = category or str(SETTINGS.blog.get("default_category", "부엌가구"))
    slug_base = SETTINGS.blog.get("category_slugs", {}).get(category, "job")
    job_id = new_job_id(slug_base)
    inbox = SETTINGS.inbox_dir / job_id
    inbox.mkdir(parents=True, exist_ok=True)

    allowed = {s.lower() for s in SETTINGS.images.get("allowed_suffixes", [])}
    max_bytes = int(SETTINGS.gui.get("max_upload_mb", 60)) * 1024 * 1024
    saved, rejected = [], []
    for upload in files:
        name = Path(upload.filename or "").name
        if not name or Path(name).suffix.lower() not in allowed:
            rejected.append({"file": name or "(이름 없음)", "reason": "지원하지 않는 확장자"})
            continue
        data = await upload.read()
        if len(data) > max_bytes:
            rejected.append({"file": name, "reason": f"{max_bytes // 1024 // 1024}MB 초과"})
            continue
        (inbox / name).write_bytes(data)
        saved.append(name)

    if not saved:
        shutil.rmtree(inbox, ignore_errors=True)
        return JSONResponse({"error": "저장된 이미지가 없습니다", "rejected": rejected}, status_code=400)

    job = create_job(SETTINGS, STORE, job_id, category, topic)
    return JSONResponse({"job_id": job.job_id, "saved": saved, "rejected": rejected})


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    try:
        return _job_view(STORE.load(job_id))
    except AgentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/run")
def api_run(job_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    if not STORE.exists(job_id):
        raise HTTPException(status_code=404, detail="job을 찾을 수 없습니다")
    try:
        RUNNER.start(job_id)
    except LockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job_id, "started": True}


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel(job_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    if RUNNER.current_job != job_id or not RUNNER.busy():
        raise HTTPException(status_code=409, detail="실행 중인 작업이 아닙니다")
    RUNNER.cancel_requested = True
    return {"job_id": job_id, "cancelling": True}


def _result_path(job_id: str) -> Path:
    job = STORE.load(job_id)
    if not job.output or not job.output.get("txt"):
        raise HTTPException(status_code=404, detail="아직 생성된 결과가 없습니다")
    path = Path(job.output["txt"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="결과 파일이 사라졌습니다")
    return path


@app.get("/api/jobs/{job_id}/result")
def api_result(job_id: str, download: bool = False, _: None = Depends(require_token)):
    path = _result_path(job_id)
    if download:
        return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)
    return JSONResponse({"job_id": job_id, "filename": path.name, "content": path.read_text(encoding="utf-8")})


@app.put("/api/jobs/{job_id}/result")
async def api_save_result(job_id: str, request: Request, _: None = Depends(require_token)) -> dict[str, Any]:
    path = _result_path(job_id)
    body = await request.json()
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="내용이 비어 있습니다")
    backup = path.with_suffix(f".txt.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(content, encoding="utf-8")
    return {"job_id": job_id, "saved": True, "backup": backup.name, "chars": len(content)}


@app.get("/api/jobs/{job_id}/image/{name}")
def api_image(job_id: str, name: str, _: None = Depends(require_token)) -> FileResponse:
    safe = Path(name).name
    for candidate in (
        SETTINGS.output_dir / job_id / "images" / safe,
        SETTINGS.work_dir / job_id / "images" / safe,
    ):
        if candidate.exists():
            return FileResponse(candidate)
    raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다")


@app.get("/events")
async def events(job: str, _: None = Depends(require_token)) -> StreamingResponse:
    channel = RUNNER.subscribe(job)

    async def stream():
        # keepalive를 매 폴링마다 보내면 Pi에서 불필요한 트래픽이 된다.
        # 큐는 자주 확인하되, 주석 프레임은 일정 간격으로만 내보낸다.
        poll_interval = 0.4
        keepalive_every = 15.0
        since_keepalive = 0.0
        try:
            for line in list(RUNNER.log_buffer)[-40:]:
                yield f"data: {json.dumps({'event': 'log', 'line': line}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    message = channel.get_nowait()
                    yield f"data: {message}\n\n"
                    since_keepalive = 0.0
                except queue.Empty:
                    await asyncio.sleep(poll_interval)
                    since_keepalive += poll_interval
                    if since_keepalive >= keepalive_every:
                        since_keepalive = 0.0
                        yield ": keepalive\n\n"
        finally:
            RUNNER.unsubscribe(job, channel)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
