#!/usr/bin/env python3
"""Retry only the LLM stage from a completed, unloaded VLM run."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.config import AppConfig
from app.llm_runner import build_llm_command
from app.memory_guard import MemoryGuard
from app.metrics import atomic_write_json, atomic_write_text, utc_now
from app.model_discovery import find_local_downloaded_model
from app.output_parser import clean_blog_markdown, validate_blog_markdown
from app.process_runner import run_process
from app.schemas import AgentError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    config = AppConfig.load(Path.cwd())
    run_dir = args.run_dir.resolve()
    vision_path = run_dir / "vision.json"
    prompt_path = run_dir / "blog_prompt.txt"
    if not vision_path.is_file() or not prompt_path.is_file():
        raise AgentError("retry requires an existing vision.json and blog_prompt.txt")

    model = find_local_downloaded_model(config.llm_hf_repo, config.model_dir, config.llm_quant_preferences)
    if model is None:
        raise AgentError("no local LLM model found")
    guard = MemoryGuard(config.min_available_during_run_mb, config.memory_check_interval_sec)
    guard.assert_model_size_allowed(model.total_bytes, config.max_llm_gguf_gib, config.allow_oversized_model)
    before = guard.wait_for_available(config.min_available_before_llm_mb, config.memory_recovery_timeout_sec, "LLM retry")

    started_at = utc_now()
    started = time.monotonic()
    result = run_process(
        build_llm_command(config, model, prompt_path),
        run_dir / "llm_retry_stdout.txt",
        run_dir / "llm_retry_stderr.txt",
        config.llm_timeout_sec,
        config.process_terminate_grace_sec,
        guard.should_abort_running_process,
        config.memory_check_interval_sec,
    )
    if result.exit_code != 0:
        raise AgentError(f"LLM retry failed with exit code {result.exit_code}")

    blog = clean_blog_markdown((run_dir / "llm_retry_stdout.txt").read_text(encoding="utf-8", errors="replace"))
    validate_blog_markdown(blog)
    atomic_write_text(run_dir / "blog.md", blog)
    atomic_write_text(args.output.resolve(), blog)
    after = guard.snapshot()
    metrics = {
        "status": "completed",
        "source_vlm_run": str(run_dir),
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "model": str(model.primary_path),
        "quantization": model.quantization,
        "model_bytes": model.total_bytes,
        "exit_code": result.exit_code,
        "peak_rss_mb": round(result.peak_rss_mb, 1),
        "mem_available_before_mb": before.mem_available_mb,
        "mem_available_after_mb": after.mem_available_mb,
        "output": str(args.output.resolve()),
    }
    atomic_write_json(run_dir / "llm_retry_metrics.json", metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
