"""Phase 0 하드웨어/모델 실측 harness.

llama.cpp 프로세스를 직접 띄우고 peak RSS, MemAvailable, swap, 온도를 표본 추출한다.
GNU time(1)이 없는 환경이므로 psutil로 직접 측정한다.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "third_party" / "llama.cpp" / "build" / "bin"


def meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            info[parts[0].rstrip(":")] = int(parts[1]) // 1024
    return info


def cpu_temp() -> float | None:
    path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return float(path.read_text(encoding="utf-8").strip()) / 1000.0
    except (OSError, ValueError):
        return None


def throttled() -> str:
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Sampler(threading.Thread):
    def __init__(self, pid: int, interval: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.pid = pid
        self.interval = interval
        self.stop_flag = threading.Event()
        self.peak_rss_mb = 0.0
        self.min_available_mb = 10**9
        self.max_temp_c = 0.0
        self.max_swap_used_mb = 0
        self.samples: list[dict] = []

    def run(self) -> None:
        try:
            proc = psutil.Process(self.pid)
        except psutil.NoSuchProcess:
            return
        while not self.stop_flag.is_set():
            try:
                total = proc.memory_info().rss
                for child in proc.children(recursive=True):
                    try:
                        total += child.memory_info().rss
                    except psutil.Error:
                        continue
                self.peak_rss_mb = max(self.peak_rss_mb, total / (1024 * 1024))
            except psutil.Error:
                break
            mem = meminfo()
            avail = mem.get("MemAvailable", 0)
            swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
            temp = cpu_temp() or 0.0
            self.min_available_mb = min(self.min_available_mb, avail)
            self.max_swap_used_mb = max(self.max_swap_used_mb, swap_used)
            self.max_temp_c = max(self.max_temp_c, temp)
            self.samples.append({"t": round(time.time(), 1), "avail_mb": avail, "swap_used_mb": swap_used, "temp_c": temp, "rss_mb": round(self.peak_rss_mb, 1)})
            self.stop_flag.wait(self.interval)


TIMING_PATTERNS = {
    "load_time_ms": re.compile(r"load time\s*=\s*([\d.]+) ms"),
    "prompt_eval_ms": re.compile(r"prompt eval time\s*=\s*([\d.]+) ms\s*/\s*(\d+) tokens"),
    "eval_ms": re.compile(r"\beval time\s*=\s*([\d.]+) ms\s*/\s*(\d+) (?:runs|tokens)"),
    "total_ms": re.compile(r"total time\s*=\s*([\d.]+) ms"),
}


def parse_timings(stderr_text: str) -> dict:
    result: dict = {}
    m = TIMING_PATTERNS["load_time_ms"].search(stderr_text)
    if m:
        result["load_time_s"] = round(float(m.group(1)) / 1000.0, 2)
    m = TIMING_PATTERNS["prompt_eval_ms"].search(stderr_text)
    if m:
        ms, toks = float(m.group(1)), int(m.group(2))
        result["prompt_tokens"] = toks
        result["prompt_tok_s"] = round(toks / (ms / 1000.0), 2) if ms > 0 else None
    for m in TIMING_PATTERNS["eval_ms"].finditer(stderr_text):
        ms, toks = float(m.group(1)), int(m.group(2))
        result["gen_tokens"] = toks
        result["gen_tok_s"] = round(toks / (ms / 1000.0), 2) if ms > 0 else None
    m = TIMING_PATTERNS["total_ms"].search(stderr_text)
    if m:
        result["total_s"] = round(float(m.group(1)) / 1000.0, 2)
    return result


def run_bench(label: str, args: list[str], timeout: float) -> dict:
    out_dir = ROOT / "bench" / "raw" / label
    out_dir.mkdir(parents=True, exist_ok=True)
    before = meminfo()
    started = time.monotonic()
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    sampler = Sampler(proc.pid)
    sampler.start()
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        stdout, stderr = proc.communicate()
    finally:
        sampler.stop_flag.set()
        sampler.join(timeout=5)
    wall = time.monotonic() - started
    after = meminfo()
    (out_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (out_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    (out_dir / "samples.json").write_text(json.dumps(sampler.samples, ensure_ascii=False), encoding="utf-8")
    record = {
        "label": label,
        "args": args,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "oom_suspected": proc.returncode in (-9, 137),
        "wall_s": round(wall, 2),
        "peak_rss_mb": round(sampler.peak_rss_mb, 1),
        "min_available_mb": sampler.min_available_mb,
        "max_swap_used_mb": sampler.max_swap_used_mb,
        "max_temp_c": round(sampler.max_temp_c, 1),
        "avail_before_mb": before.get("MemAvailable", 0),
        "avail_after_mb": after.get("MemAvailable", 0),
        "throttled_after": throttled(),
        "output_chars": len(stdout.strip()),
        **parse_timings(stderr),
    }
    (out_dir / "record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def llm_args(model: Path, prompt_file: Path, ctx: int, n_predict: int) -> list[str]:
    return [
        str(BIN_DIR / "llama-completion"), "-m", str(model), "-f", str(prompt_file),
        "-t", "4", "--threads-batch", "4", "-c", str(ctx), "-n", str(n_predict),
        "--temp", "0.65", "--top-p", "0.9", "-b", "128", "-ub", "64", "-ngl", "0",
        "--parallel", "1", "-no-cnv", "--simple-io", "--no-display-prompt",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
    ]


def vlm_args(model: Path, mmproj: Path, image: Path, prompt_file: Path, ctx: int, n_predict: int) -> list[str]:
    return [
        str(BIN_DIR / "llama-mtmd-cli"), "-m", str(model), "--mmproj", str(mmproj),
        "--image", str(image), "-f", str(prompt_file), "-t", "4", "-c", str(ctx),
        "-n", str(n_predict), "--temp", "0.2", "-ngl", "0", "--no-mmproj-offload",
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["llm", "vlm"], required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--mmproj", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--ctx", type=int, default=4096)
    parser.add_argument("--n-predict", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()

    if args.kind == "llm":
        cmd = llm_args(args.model, args.prompt, args.ctx, args.n_predict)
    else:
        if not args.mmproj or not args.image:
            parser.error("--mmproj and --image are required for vlm")
        cmd = vlm_args(args.model, args.mmproj, args.image, args.prompt, args.ctx, args.n_predict)

    record = run_bench(args.label, cmd, args.timeout)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record["exit_code"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
