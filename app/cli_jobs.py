"""사진 폴더 -> 블로그 .txt 파이프라인의 CLI.

GUI와 동일한 orchestrator(app.pipeline)를 호출한다. 기능 차이가 없어야 한다.

  python -m app.cli_jobs new --images <dir> [--category 부엌가구] [--topic "..."]
  python -m app.cli_jobs run --job <job_id>
  python -m app.cli_jobs list
  python -m app.cli_jobs show --job <job_id>
  python -m app.cli_jobs preflight
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .job_state import JobStore, Stage, now_kst
from .memory_guard import MemoryGuard
from .pipeline import Pipeline, create_job, new_job_id
from .schemas import AgentError
from .settings import Settings


def configure_logging(settings: Settings, verbose: bool = False) -> logging.Logger:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs_dir / f"agent-{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_path, encoding="utf-8")],
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)
    return logging.getLogger("blog_agent")


def cmd_new(settings: Settings, args: argparse.Namespace) -> int:
    store = JobStore(settings.state_dir)
    category = args.category or settings.blog.get("default_category", "부엌가구")
    slug_base = settings.blog.get("category_slugs", {}).get(category, "job")
    job_id = args.job or new_job_id(slug_base)

    inbox = settings.inbox_dir / job_id
    inbox.mkdir(parents=True, exist_ok=True)
    source = Path(args.images).expanduser().resolve()
    allowed = {s.lower() for s in settings.images.get("allowed_suffixes", [])}
    copied = 0
    if source.is_dir():
        for path in sorted(source.iterdir()):
            if path.is_file() and path.suffix.lower() in allowed:
                shutil.copy2(path, inbox / path.name)
                copied += 1
    elif source.is_file():
        shutil.copy2(source, inbox / source.name)
        copied = 1
    else:
        raise AgentError(f"입력 경로를 찾을 수 없습니다: {source}")
    if copied == 0:
        raise AgentError(f"복사할 이미지가 없습니다: {source}")

    job = create_job(settings, store, job_id, category, args.topic or "")
    print(json.dumps({"job_id": job.job_id, "inbox": str(inbox), "images_copied": copied}, ensure_ascii=False, indent=2))
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace, logger: logging.Logger) -> int:
    store = JobStore(settings.state_dir)
    job = store.load(args.job)

    if not args.force:
        duplicates = store.find_by_input_hash(job.input_set_hash, job.job_id)
        if duplicates:
            print(json.dumps({
                "status": "DUPLICATE_INPUT",
                "message": "같은 사진 집합으로 이미 생성된 결과가 있습니다. 재생성하려면 --force를 쓰세요.",
                "existing_jobs": [d.job_id for d in duplicates],
            }, ensure_ascii=False, indent=2))
            return 3

    def progress(event: str, payload: dict) -> None:
        if event == "progress" and payload.get("detail"):
            logger.info("[진행] %s", payload["detail"])

    pipeline = Pipeline(settings, logger, progress)
    result = pipeline.run(args.job)
    print(json.dumps({
        "job_id": result.job_id,
        "completed": result.completed,
        "output_txt": result.output_txt,
        "output_meta": result.output_meta,
        "quality_passed": result.quality_passed,
        "blockers": result.blockers,
        "summary": result.summary,
    }, ensure_ascii=False, indent=2))
    return 0 if result.quality_passed else 4


def cmd_list(settings: Settings, _args: argparse.Namespace) -> int:
    store = JobStore(settings.state_dir)
    rows = []
    for job in store.list_jobs():
        rows.append({
            "job_id": job.job_id,
            "created_at": job.created_at,
            "stage": job.stage,
            "status": {s.value: job.stage_status.get(s.value, "NOT_STARTED") for s in Stage.ordered()},
            "images": len(job.images),
            "output": (job.output or {}).get("txt", ""),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_show(settings: Settings, args: argparse.Namespace) -> int:
    store = JobStore(settings.state_dir)
    print(json.dumps(store.load(args.job).to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_preflight(settings: Settings, _args: argparse.Namespace) -> int:
    guard = MemoryGuard(logger=logging.getLogger("preflight"))
    snapshot = guard.snapshot()
    disk = shutil.disk_usage(settings.root)
    report = {
        "checked_at": now_kst(),
        "memory": {
            "total_mb": snapshot.mem_total_mb,
            "available_mb": snapshot.mem_available_mb,
            "swap_total_mb": snapshot.swap_total_mb,
            "swap_used_percent": round(snapshot.swap_used_percent, 1),
        },
        "thermal": {"cpu_temp_c": snapshot.cpu_temp_c, "throttled": snapshot.throttled},
        "disk": {"total_gib": round(disk.total / 1024**3, 1), "free_gib": round(disk.free / 1024**3, 1)},
        "vlm_tiers": [{"name": t.name, "available": ok, "reason": why} for t, ok, why in settings.available_tiers("vlm")],
        "llm_tiers": [{"name": t.name, "available": ok, "reason": why} for t, ok, why in settings.available_tiers("llm")],
        "face_model": (settings.root / "models" / "privacy" / "face_detection_yunet_2023mar.onnx").exists(),
        "lock": str(settings.lock_path) + (" (존재)" if settings.lock_path.exists() else " (없음)"),
    }
    blocking = [t["name"] for t in report["vlm_tiers"] if t["available"]] and [t["name"] for t in report["llm_tiers"] if t["available"]]
    report["ready"] = bool(blocking) and report["disk"]["free_gib"] > 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli_jobs", description="로컬 사진 -> 블로그 .txt 에이전트")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="사진 폴더로 새 job 생성")
    new.add_argument("--images", required=True, help="사진이 있는 폴더 또는 파일")
    new.add_argument("--category", default="")
    new.add_argument("--topic", default="", help="주제 힌트 (선택)")
    new.add_argument("--job", default="", help="job_id 직접 지정 (선택)")

    run = sub.add_parser("run", help="파이프라인 실행 (중단 후 재실행 시 resume)")
    run.add_argument("--job", required=True)
    run.add_argument("--force", action="store_true", help="같은 사진 집합이어도 재생성")

    sub.add_parser("list", help="job 목록")

    show = sub.add_parser("show", help="job 상태 상세")
    show.add_argument("--job", required=True)

    sub.add_parser("preflight", help="모델/RAM/디스크/스왑 점검")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load(Path.cwd())
    logger = configure_logging(settings, args.verbose)
    try:
        if args.command == "new":
            return cmd_new(settings, args)
        if args.command == "run":
            return cmd_run(settings, args, logger)
        if args.command == "list":
            return cmd_list(settings, args)
        if args.command == "show":
            return cmd_show(settings, args)
        if args.command == "preflight":
            return cmd_preflight(settings, args)
    except AgentError as exc:
        logger.error("%s", exc)
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        logger.error("사용자가 중단했습니다. 완료된 스테이지는 보존됩니다 (resume 가능).")
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
