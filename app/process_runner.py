from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .metrics import utc_now
from .schemas import ProcessResult


AbortCallback = Callable[[int], str | None]


def process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(proc: subprocess.Popen[str], grace_sec: float) -> None:
    if proc.poll() is not None:
        proc.wait()
        return
    pgid = os.getpgid(proc.pid)
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            proc.wait()
            return
        time.sleep(0.1)
    if proc.poll() is None:
        os.killpg(pgid, signal.SIGKILL)
    proc.wait()


def _stream_to_file(pipe, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", errors="replace") as handle:
        while True:
            chunk = pipe.readline()
            if chunk == "":
                break
            handle.write(chunk)
            handle.flush()


def run_process(
    args: list[str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_sec: float,
    grace_sec: float,
    abort_callback: AbortCallback | None = None,
    check_interval_sec: float = 1.0,
) -> ProcessResult:
    if not args:
        raise ValueError("args cannot be empty")
    started = time.monotonic()
    result = ProcessResult(args=args, exit_code=-999, duration_seconds=0.0, started_at=utc_now())
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        start_new_session=True,
        bufsize=1,
    )
    result.pid = proc.pid
    threads = [
        threading.Thread(target=_stream_to_file, args=(proc.stdout, stdout_path), daemon=True),
        threading.Thread(target=_stream_to_file, args=(proc.stderr, stderr_path), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout_sec
    peak = 0.0
    try:
        while proc.poll() is None:
            if abort_callback is not None:
                reason = abort_callback(proc.pid)
                if reason:
                    result.killed_for_safety = True
                    with stderr_path.open("a", encoding="utf-8", errors="replace") as handle:
                        handle.write(f"\n[agent] safety stop: {reason}\n")
                    terminate_process_group(proc, grace_sec)
                    break
            if time.monotonic() >= deadline:
                result.timed_out = True
                with stderr_path.open("a", encoding="utf-8", errors="replace") as handle:
                    handle.write(f"\n[agent] timeout after {timeout_sec} seconds\n")
                terminate_process_group(proc, grace_sec)
                break
            if abort_callback is not None:
                try:
                    import psutil

                    root = psutil.Process(proc.pid)
                    total = root.memory_info().rss + sum((child.memory_info().rss for child in root.children(recursive=True)), 0)
                    peak = max(peak, total / (1024 * 1024))
                except Exception:
                    pass
            time.sleep(check_interval_sec)
    finally:
        if proc.poll() is None:
            terminate_process_group(proc, grace_sec)
        result.exit_code = proc.wait()
        for thread in threads:
            thread.join(timeout=5)
        result.finished_at = utc_now()
        result.duration_seconds = time.monotonic() - started
        result.peak_rss_mb = peak
        if result.exit_code in {-9, 137}:
            result.oom_suspected = True
    return result
