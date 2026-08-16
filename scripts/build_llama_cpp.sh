#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

JOBS="${JOBS:-4}"
CLEAN=false
if [[ "${1:-}" == "--clean" ]]; then
  CLEAN=true
fi

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
  echo "FAIL: ARM64(aarch64/arm64) 환경이 필요합니다. 현재: $ARCH" >&2
  exit 1
fi

LLAMA_DIR="third_party/llama.cpp"
PIN_FILE=".llama-cpp-version"
PINNED_COMMIT=""
if [[ -s "$PIN_FILE" ]]; then
  PINNED_COMMIT="$(tr -d '[:space:]' < "$PIN_FILE")"
fi

if [[ ! -d "$LLAMA_DIR/.git" ]]; then
  if [[ -d "$LLAMA_DIR" ]] && find "$LLAMA_DIR" -mindepth 1 ! -name '.gitkeep' -print -quit | grep -q .; then
    echo "FAIL: $LLAMA_DIR exists but is not a git checkout. Move it aside or run with an empty third_party/llama.cpp." >&2
    exit 1
  fi
  rm -rf "$LLAMA_DIR"
  git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
fi

if [[ -n "$PINNED_COMMIT" && "$(git -C "$LLAMA_DIR" rev-parse HEAD)" != "$PINNED_COMMIT" ]]; then
  if [[ -n "$(git -C "$LLAMA_DIR" status --porcelain)" ]]; then
    echo "FAIL: $LLAMA_DIR checkout에 로컬 변경이 있어 pinned commit으로 전환할 수 없습니다." >&2
    exit 1
  fi
  git -C "$LLAMA_DIR" fetch --depth 1 origin "$PINNED_COMMIT"
  git -C "$LLAMA_DIR" checkout --detach "$PINNED_COMMIT"
fi

if [[ "$CLEAN" == "true" ]]; then
  rm -rf "$LLAMA_DIR/build"
fi

if [[ -x "$LLAMA_DIR/build/bin/llama-completion" && -x "$LLAMA_DIR/build/bin/llama-mtmd-cli" && -x "$LLAMA_DIR/build/bin/llama-quantize" ]]; then
  echo "PASS: 기존 llama.cpp 빌드를 재사용합니다."
else
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DGGML_CUDA=OFF -DGGML_METAL=OFF -DGGML_VULKAN=OFF
  cmake --build "$LLAMA_DIR/build" --target llama-completion llama-mtmd-cli llama-quantize -j "$JOBS"
fi

git -C "$LLAMA_DIR" rev-parse HEAD > "$PIN_FILE"

check_binary_runs() {
  local bin="$1"
  local path="$2"

  if [[ "$bin" == "llama-quantize" ]]; then
    local output status
    set +e
    output="$("$path" --help 2>&1)"
    status=$?
    set -e

    [[ "$output" == usage:* || "$output" == *$'\nusage:'* ]] && return 0
    return "$status"
  fi

  "$path" --version >/dev/null 2>&1 || "$path" --help >/dev/null 2>&1
}

for bin in llama-completion llama-mtmd-cli llama-quantize; do
  path="$LLAMA_DIR/build/bin/$bin"
  if [[ ! -x "$path" ]]; then
    echo "FAIL: 빌드된 바이너리가 없습니다: $path" >&2
    exit 1
  fi
  check_binary_runs "$bin" "$path" || {
    echo "FAIL: 바이너리 실행 검증 실패: $path" >&2
    exit 1
  }
done

echo "PASS: llama.cpp 빌드 완료: $(cat .llama-cpp-version)"
