#!/usr/bin/env bash
# Resume 검증. 파이프라인을 VLM 분석 도중 SIGKILL로 강제 종료한 뒤,
# 재실행에서 완료된 작업을 건너뛰는지 확인한다. 전원 차단 상황을 흉내 낸다.
#
#   scripts/resume_test.sh <job_id> [kill_after_seconds]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

JOB="${1:?사용법: scripts/resume_test.sh <job_id> [kill_after_seconds]}"
KILL_AFTER="${2:-200}"
PY=.venv-blog-agent/bin/python

echo "=== 1) 파이프라인 시작 후 ${KILL_AFTER}초 뒤 SIGKILL ==="
$PY -m app.cli_jobs run --job "$JOB" --force > "/tmp/resume_run1_${JOB}.log" 2>&1 &
RUN_PID=$!

sleep "$KILL_AFTER"

if kill -0 "$RUN_PID" 2>/dev/null; then
  # 자식(llama-mtmd-cli)까지 확실히 죽인다
  pkill -9 -P "$RUN_PID" 2>/dev/null || true
  kill -9 "$RUN_PID" 2>/dev/null || true
  echo "SIGKILL 전송 (pid=$RUN_PID)"
else
  echo "경고: 프로세스가 이미 끝났습니다. kill_after 값을 줄이세요." >&2
fi
pkill -9 -f llama-mtmd-cli 2>/dev/null || true
pkill -9 -f llama-server 2>/dev/null || true
sleep 3

echo
echo "=== 2) 강제 종료 직후 상태 ==="
$PY - "$JOB" <<'PYEOF'
import json, os, sys
from pathlib import Path
job_id = sys.argv[1]
job = json.loads(Path(f"state/jobs/{job_id}.json").read_text(encoding="utf-8"))
print("stage        :", job["stage"])
print("stage_status :", json.dumps(job["stage_status"], ensure_ascii=False))
done = [i["slug"] for i in job["images"] if i.get("vision_json")]
print("분석 완료 사진:", done)
lock = Path("state/run.lock")
if lock.exists():
    data = json.loads(lock.read_text(encoding="utf-8"))
    alive = Path(f"/proc/{data.get('pid')}").exists()
    print(f"run.lock     : pid={data.get('pid')} 살아있음={alive} (stale lock이어야 정상)")
else:
    print("run.lock     : 없음")
PYEOF

echo
echo "=== 3) 이제 재실행하면 완료된 사진을 건너뛴다 ==="
echo "   온라인: ./scripts/run_cli.sh run --job $JOB --force"
echo "   오프라인: ./scripts/offline_test.sh $JOB"
echo "   (재실행 로그에 '기존 분석 결과를 재사용합니다 (resume)'가 보여야 한다)"
