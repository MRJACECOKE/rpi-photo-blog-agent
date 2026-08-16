from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from .schemas import MemoryGuardError

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemorySnapshot:
    mem_total_mb: int
    mem_available_mb: int
    swap_total_mb: int
    swap_free_mb: int
    process_tree_rss_mb: float
    load_average: tuple[float, float, float]
    cpu_temp_c: float | None
    throttled: str | None

    @property
    def swap_used_percent(self) -> float:
        if self.swap_total_mb <= 0:
            return 0.0
        return ((self.swap_total_mb - self.swap_free_mb) / self.swap_total_mb) * 100.0


class MemoryGuard:
    def __init__(self, min_during_run_mb: int = 512, check_interval_sec: float = 1.0, logger: logging.Logger | None = None) -> None:
        self.min_during_run_mb = min_during_run_mb
        self.check_interval_sec = check_interval_sec
        self.logger = logger or LOG
        self.maximum_temp_c: float | None = None
        self.throttling_detected = False

    @staticmethod
    def read_meminfo() -> dict[str, int]:
        info: dict[str, int] = {}
        with Path("/proc/meminfo").open("r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    info[parts[0].rstrip(":")] = int(parts[1]) // 1024
        return info

    @staticmethod
    def process_tree_rss_mb(pid: int | None = None) -> float:
        if pid is None:
            pid = os.getpid()
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return 0.0
        total = 0
        procs = [proc]
        try:
            procs.extend(proc.children(recursive=True))
        except psutil.Error:
            pass
        for item in procs:
            try:
                total += item.memory_info().rss
            except psutil.Error:
                continue
        return total / (1024 * 1024)

    @staticmethod
    def cpu_temp_c() -> float | None:
        candidates = [
            Path("/sys/class/thermal/thermal_zone0/temp"),
            Path("/sys/class/hwmon/hwmon0/temp1_input"),
        ]
        for path in candidates:
            try:
                if path.exists():
                    raw = path.read_text(encoding="utf-8").strip()
                    value = float(raw)
                    return value / 1000.0 if value > 200 else value
            except (OSError, ValueError):
                continue
        try:
            result = subprocess.run(["vcgencmd", "measure_temp"], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2)
            if "temp=" in result.stdout:
                return float(result.stdout.split("temp=", 1)[1].split("'")[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return None

    @staticmethod
    def throttled_state() -> str | None:
        try:
            result = subprocess.run(["vcgencmd", "get_throttled"], check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2)
            text = result.stdout.strip()
            return text or None
        except (OSError, subprocess.SubprocessError):
            return None

    def snapshot(self, pid: int | None = None) -> MemorySnapshot:
        mem = self.read_meminfo()
        temp = self.cpu_temp_c()
        throttled = self.throttled_state()
        if temp is not None:
            self.maximum_temp_c = temp if self.maximum_temp_c is None else max(self.maximum_temp_c, temp)
        if throttled and throttled != "throttled=0x0":
            self.throttling_detected = True
        return MemorySnapshot(
            mem_total_mb=mem.get("MemTotal", 0),
            mem_available_mb=mem.get("MemAvailable", 0),
            swap_total_mb=mem.get("SwapTotal", 0),
            swap_free_mb=mem.get("SwapFree", 0),
            process_tree_rss_mb=self.process_tree_rss_mb(pid),
            load_average=os.getloadavg(),
            cpu_temp_c=temp,
            throttled=throttled,
        )

    def validate_total_ram(self, reject_below_mb: int = 14 * 1024) -> None:
        snap = self.snapshot()
        if snap.mem_total_mb < reject_below_mb:
            raise MemoryGuardError(f"RAM total is {snap.mem_total_mb} MiB; Raspberry Pi 5 16GB class environment is required")

    def wait_for_available(self, target_mb: int, timeout_sec: float, reason: str) -> MemorySnapshot:
        deadline = time.monotonic() + timeout_sec
        last = self.snapshot()
        while True:
            last = self.snapshot()
            if last.mem_available_mb >= target_mb:
                return last
            if time.monotonic() >= deadline:
                raise MemoryGuardError(f"MemAvailable stayed below {target_mb} MiB for {reason}; last={last.mem_available_mb} MiB")
            self.logger.info("Waiting for memory before %s: MemAvailable=%s MiB target=%s MiB", reason, last.mem_available_mb, target_mb)
            time.sleep(self.check_interval_sec)

    def assert_model_size_allowed(self, total_bytes: int, max_gib: float, allow_oversized: bool) -> None:
        limit = int(max_gib * 1024**3)
        if total_bytes > limit and not allow_oversized:
            actual = total_bytes / 1024**3
            raise MemoryGuardError(f"LLM GGUF size {actual:.2f} GiB exceeds {max_gib:.2f} GiB; set ALLOW_OVERSIZED_MODEL=true only if you accept the risk")
        if total_bytes > limit:
            self.logger.warning("Oversized LLM model allowed: %.2f GiB > %.2f GiB", total_bytes / 1024**3, max_gib)

    def should_abort_running_process(self, pid: int) -> str | None:
        snap = self.snapshot(pid)
        if snap.mem_available_mb < self.min_during_run_mb:
            return f"MemAvailable dropped below {self.min_during_run_mb} MiB ({snap.mem_available_mb} MiB)"
        if snap.swap_used_percent > 85.0:
            return f"swap usage exceeded 85% ({snap.swap_used_percent:.1f}%)"
        if snap.cpu_temp_c is not None and snap.cpu_temp_c >= 82.0:
            return f"CPU temperature exceeded 82C ({snap.cpu_temp_c:.1f}C)"
        return None

    def maybe_drop_caches_after_vlm(self, allow_cache_drop: bool, vlm_pid_alive: bool) -> bool:
        if not allow_cache_drop:
            return False
        if os.geteuid() != 0 or vlm_pid_alive:
            return False
        subprocess.run(["sync"], check=False)
        try:
            Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="utf-8")
            self.logger.warning("Dropped kernel page cache after VLM because ALLOW_CACHE_DROP=true and UID is root")
            return True
        except OSError as exc:
            self.logger.warning("Cache drop requested but failed: %s", exc)
            return False
