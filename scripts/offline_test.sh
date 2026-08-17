#!/usr/bin/env bash
# 오프라인 검증. 네트워크 네임스페이스를 분리해 외부 연결이 아예 존재하지 않는 상태에서 파이프라인을 돌린다.
#
# `ip link set wlan0 down` 대신 이 방식을 쓰는 이유:
#   - 호스트 네트워크를 건드리지 않으므로 원격 접속이 끊기지 않는다.
#   - "방화벽으로 막혔다"가 아니라 "인터페이스가 없다"라서 더 강한 증명이다.
#   - loopback만 살려 두므로 llama-server(127.0.0.1)는 정상 동작한다.
#
#   scripts/offline_test.sh <job_id>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

JOB="${1:?사용법: scripts/offline_test.sh <job_id>}"

if ! unshare -rn true 2>/dev/null; then
  echo "이 환경에서는 사용자 네임스페이스를 만들 수 없습니다. 오프라인 검증을 건너뜁니다." >&2
  exit 3
fi

exec unshare -rn sh -c "
  ip link set lo up
  echo '--- 네트워크 상태 (loopback 외에는 없어야 한다) ---'
  ip -br addr show
  echo '--- 외부 연결 확인 (실패해야 정상) ---'
  python3 -c \"
import urllib.request
try:
    urllib.request.urlopen('https://pypi.org', timeout=5)
    print('FAIL: 외부 네트워크에 연결됐습니다')
    raise SystemExit(1)
except Exception as exc:
    print('OK: 외부 연결 차단됨 (%s)' % type(exc).__name__)
\"
  echo '--- 파이프라인 실행 ---'
  cd '$ROOT'
  exec .venv-blog-agent/bin/python -m app.cli_jobs run --job '$JOB' --force
"
