#!/usr/bin/env bash
# 모델 파일 존재 / RAM / 디스크 / 스왑 / 온도 점검. 실행 전에 돌린다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== 하드웨어 =="
grep -m1 Model /proc/cpuinfo || true
free -m | head -2
df -h "$ROOT" | tail -1
echo "-- swap --"; swapon --show || echo "(스왑 없음)"
echo "-- 온도/스로틀 --"
vcgencmd measure_temp 2>/dev/null || echo "vcgencmd 없음"
vcgencmd get_throttled 2>/dev/null || true

echo
echo "== 에이전트 점검 =="
exec .venv-blog-agent/bin/python -m app.cli_jobs preflight
