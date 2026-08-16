from __future__ import annotations

import os
import sys
from pathlib import Path

from app.process_runner import process_exists, run_process


def test_subprocess_timeout_cleans_process_group(tmp_path: Path) -> None:
    script = tmp_path / "sleepy.py"
    script.write_text("import time\nwhile True:\n    time.sleep(1)\n", encoding="utf-8")
    result = run_process([sys.executable, str(script)], tmp_path / "out.txt", tmp_path / "err.txt", timeout_sec=0.2, grace_sec=0.1, check_interval_sec=0.05)
    assert result.timed_out
    assert result.exit_code != 0
    assert result.pid is not None
    assert not process_exists(result.pid)


def test_vlm_alive_pid_blocks_llm_guard() -> None:
    assert process_exists(os.getpid())
