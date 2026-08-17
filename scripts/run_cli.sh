#!/usr/bin/env bash
# 헤드리스 실행. GUI와 동일한 orchestrator를 호출한다.
#   scripts/run_cli.sh new --images ~/photos --category 부엌가구 --topic "좁은 주방 수납"
#   scripts/run_cli.sh run --job <job_id>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec .venv-blog-agent/bin/python -m app.cli_jobs "$@"
