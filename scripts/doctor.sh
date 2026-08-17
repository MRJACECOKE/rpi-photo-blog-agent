#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

pass() { echo "PASS: $*"; }
warn() { echo "WARN: $*"; }
fail() { echo "FAIL: $*"; }

ARCH="$(uname -m)"
[[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]] && pass "ARM64: $ARCH" || fail "ARM64가 아님: $ARCH"
BITS="$(getconf LONG_BIT || true)"
[[ "$BITS" == "64" ]] && pass "64-bit OS" || fail "64-bit OS가 아님: $BITS"

mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
mem_mb=$((mem_kb / 1024))
[[ "$mem_mb" -ge 14336 ]] && pass "RAM ${mem_mb} MiB" || warn "RAM ${mem_mb} MiB: 16GB 환경이 아닐 수 있음"
avail_kb="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
pass "MemAvailable $((avail_kb / 1024)) MiB"

python3 - <<'PY' && pass "Python 3.11+" || fail "Python 3.11 이상 필요"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

[[ -d .venv-blog-agent ]] && pass "독립 venv 존재" || warn ".venv-blog-agent 없음"

# llama-server는 job 파이프라인(app.cli_jobs)의 작성 단계에서 쓴다.
# 없으면: cd third_party/llama.cpp && cmake --build build --target llama-server
for bin in third_party/llama.cpp/build/bin/llama-completion third_party/llama.cpp/build/bin/llama-mtmd-cli third_party/llama.cpp/build/bin/llama-server; do
  [[ -x "$bin" ]] && pass "llama 바이너리: $bin" || warn "llama 바이너리 없음: $bin"
done

compgen -G "models/vlm/*.gguf" >/dev/null && pass "VLM GGUF 존재" || warn "VLM GGUF 없음"
find models/vlm -maxdepth 1 -iname '*mmproj*.gguf' -print -quit | grep -q . && pass "VLM mmproj 존재" || warn "VLM mmproj 없음"

llm_total="$(find models/llm -maxdepth 1 -type f \( -name '*.gguf' -o -name '*.gguf.*' \) -printf '%s\n' 2>/dev/null | awk '{s+=$1} END {print s+0}')"
[[ "$llm_total" -gt 0 ]] && pass "LLM 파일 합계 $((llm_total / 1024 / 1024)) MiB" || warn "LLM GGUF 없음"

free_gib="$(df -BG . | awk 'NR==2 {gsub("G","",$4); print $4}')"
[[ "$free_gib" -ge 20 ]] && pass "디스크 여유 ${free_gib}GiB" || warn "디스크 여유 ${free_gib}GiB"

swap_kb="$(awk '/SwapTotal/ {print $2}' /proc/meminfo)"
[[ "$swap_kb" -gt 0 ]] && pass "swap 존재 $((swap_kb / 1024)) MiB" || warn "swap 없음"

model_dev="$(df -P models | awk 'NR==2 {print $1}')"
case "$model_dev" in
  /dev/mmcblk*) warn "models가 SD 카드로 보임: $model_dev" ;;
  /dev/nvme*) pass "models가 NVMe로 보임: $model_dev" ;;
  *) warn "models 저장장치 유형 확인 제한: $model_dev" ;;
esac

if [[ -r /sys/class/thermal/thermal_zone0/temp ]]; then
  temp_milli="$(cat /sys/class/thermal/thermal_zone0/temp)"
  pass "CPU 온도 $((temp_milli / 1000))C"
else
  if command -v vcgencmd >/dev/null 2>&1 && temp_out="$(vcgencmd measure_temp 2>/dev/null)"; then
    pass "CPU 온도 $temp_out"
  else
    warn "CPU 온도 확인 불가"
  fi
fi

if command -v vcgencmd >/dev/null 2>&1 && throttled_out="$(vcgencmd get_throttled 2>/dev/null)"; then
  pass "throttling 상태 $throttled_out"
else
  warn "throttling 상태 확인 불가"
fi

if [[ -f runs/agent.lock ]]; then
  if python3 - <<'PY'
import fcntl
from pathlib import Path
p = Path("runs/agent.lock")
with p.open("a+") as f:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(1)
raise SystemExit(0)
PY
  then
    pass "동일 에이전트 실행 없음"
  else
    warn "agent.lock이 잠겨 있음"
  fi
else
  pass "동일 에이전트 실행 없음"
fi

touch outputs/.write-test && rm outputs/.write-test && pass "outputs 쓰기 가능" || fail "outputs 쓰기 불가"

# --- job 파이프라인(app.cli_jobs / GUI) 전용 점검 ---
[[ -f models/privacy/face_detection_yunet_2023mar.onnx ]] \
  && pass "얼굴 검출 모델 존재" \
  || warn "얼굴 검출 모델 없음: models/privacy/face_detection_yunet_2023mar.onnx (개인정보 1차 검사가 얼굴을 놓칩니다)"

for cfg in config/models.yaml config/blog.yaml config/runtime.yaml config/prompts.yaml; do
  [[ -f "$cfg" ]] && pass "설정 파일: $cfg" || warn "설정 파일 없음: $cfg"
done

if .venv-blog-agent/bin/python -c "import fastapi, uvicorn, jinja2, multipart" 2>/dev/null; then
  pass "GUI 의존성 설치됨"
else
  warn "GUI 의존성 없음 (pip install -r requirements.txt)"
fi

if .venv-blog-agent/bin/python -c "import cv2" 2>/dev/null; then
  pass "opencv 설치됨 (개인정보 1차 검사)"
else
  warn "opencv 없음 — 얼굴/QR 검사를 건너뛰고 사유만 기록합니다"
fi
