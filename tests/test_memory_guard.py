from __future__ import annotations

import pytest

from app.memory_guard import MemoryGuard, MemorySnapshot
from app.schemas import MemoryGuardError


def snap(avail: int, total: int = 16 * 1024, swap_total: int = 1024, swap_free: int = 1024, temp: float | None = None) -> MemorySnapshot:
    return MemorySnapshot(total, avail, swap_total, swap_free, 0.0, (0.1, 0.1, 0.1), temp, None)


def test_total_ram_rejects_low_ram(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = MemoryGuard(check_interval_sec=0)
    monkeypatch.setattr(guard, "snapshot", lambda pid=None: snap(1000, total=8 * 1024))
    with pytest.raises(MemoryGuardError):
        guard.validate_total_ram()


def test_low_memavailable_refuses_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = MemoryGuard(check_interval_sec=0)
    monkeypatch.setattr(guard, "snapshot", lambda pid=None: snap(128))
    with pytest.raises(MemoryGuardError):
        guard.wait_for_available(4096, 0.01, "LLM")


def test_abort_when_swap_too_high(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = MemoryGuard(min_during_run_mb=512)
    monkeypatch.setattr(guard, "snapshot", lambda pid=None: snap(800, swap_total=1000, swap_free=100))
    assert "swap" in (guard.should_abort_running_process(123) or "")
