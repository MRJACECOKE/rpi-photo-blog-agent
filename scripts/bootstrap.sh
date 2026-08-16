#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

INSTALL_SYSTEM_DEPS=false
if [[ "${1:-}" == "--install-system-deps" ]]; then
  INSTALL_SYSTEM_DEPS=true
fi

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
  echo "FAIL: ARM64(aarch64/arm64) 환경이 필요합니다. 현재: $ARCH" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("FAIL: Python 3.11 이상이 필요합니다.")
print(f"PASS: Python {sys.version.split()[0]}")
PY

missing=()
for cmd in git cmake make pkg-config; do
  command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
dpkg -s build-essential >/dev/null 2>&1 || missing+=("build-essential")
dpkg -s libcurl4-openssl-dev >/dev/null 2>&1 || missing+=("libcurl4-openssl-dev")
dpkg -s python3-venv >/dev/null 2>&1 || missing+=("python3-venv")

if (( ${#missing[@]} > 0 )); then
  echo "WARN: 누락된 시스템 패키지: ${missing[*]}"
  if [[ "$INSTALL_SYSTEM_DEPS" == "true" ]]; then
    echo "INFO: 사용자가 요청했으므로 apt로 시스템 패키지를 설치합니다."
    sudo apt-get update
    sudo apt-get install -y git cmake build-essential pkg-config libcurl4-openssl-dev python3-venv
  else
    echo "INFO: 자동 설치하지 않습니다. 필요하면 ./scripts/bootstrap.sh --install-system-deps 를 실행하세요."
  fi
fi

if [[ ! -d .venv-blog-agent ]]; then
  "$PYTHON_BIN" -m venv .venv-blog-agent
fi

export PIP_REQUIRE_VIRTUALENV=true
.venv-blog-agent/bin/python -m pip install --upgrade pip
.venv-blog-agent/bin/python -m pip install -r requirements.txt

mkdir -p models/vlm models/llm inputs outputs runs logs third_party/llama.cpp
touch runs/.gitkeep outputs/.gitkeep third_party/llama.cpp/.gitkeep

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "INFO: .env.example을 복사해 .env를 만들었습니다."
fi

echo "PASS: bootstrap 완료"
