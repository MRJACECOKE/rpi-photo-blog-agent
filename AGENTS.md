# AGENTS.md — 이 저장소에서 작업하는 에이전트를 위한 규칙

## 이 저장소가 하는 일

Raspberry Pi 5 16GB 위에서 **완전히 로컬로** 도는 블로그 생성 에이전트다. 경로가 두 개 있다.

1. `app.cli` — 사진 1장 → Markdown. 기존 경로. 유지 보수만 한다.
2. `app.cli_jobs` + `app.gui` — 사진 여러 장 → `.txt` + `.meta.json`. 이번 스코프. 신규 작업은 여기에 한다.

두 경로 모두 인터넷 없이 동작해야 한다.

## 불변 원칙

```
1. Demo/Sample/Prototype이 아니다. 실제 운영 코드다.
2. 인터넷 없이 사진만으로 블로그 생성이 가능해야 한다 (offline-first).
3. VLM과 LLM을 절대 동시에 메모리에 올리지 않는다. 순차 실행 + 실측으로 검증한 언로드.
4. 사진에서 확인되지 않은 사실을 만들지 않는다 (브랜드/가격/시공일/고객/치수/지역).
5. Recoverable 실패 하나로 전체를 중단하지 않는다. 전략을 바꿔 재시도한다.
6. 같은 실패 명령을 아무 변화 없이 반복하지 않는다.
7. 파괴적 명령 금지: rm -rf, git reset --hard, git clean -fd, git push --force
8. 사용자 원본 사진은 절대 수정/삭제하지 않는다. 항상 사본으로 작업한다.
```

## 반드시 지킬 것

### 값은 코드에 넣지 않는다

모델 경로, 임계값, 금칙어, 섹션 구조, 문체는 전부 `config/*.yaml`에 있다.
코드에는 스키마와 안전한 기본값만 둔다. 새 값을 하드코딩하지 말고 YAML에 추가한다.

### 언로드는 실측으로 증명한다

"종료 명령을 보냈다"를 성공으로 치지 않는다. `app/runtime.py:unload_gate()`를 거쳐야 한다.
PID 소멸 → `MemAvailable` 폴링 → 필요 시 drop_caches → 그래도 미달이면 tier 하향.
증거는 job json의 `memory_evidence`에 언로드 전/후 값으로 남는다.

### 실패를 숨기지 않는다

품질 게이트가 3회 재생성 후에도 통과하지 못하면 `FAILED_RECOVERABLE`로 남기고 사람에게 보여 준다.
통과한 척하지 않는다. 건너뛴 이미지, 사용한 tier, 온도 대기, 재시도는 전부 job json과 `RETRY_LOG.md`에 기록한다.

### 성능 수치는 추정하지 말고 측정한다

`bench/bench.py`로 재고 `bench/RESULTS.md`에 표로 남긴다.
"아마 빠를 것이다"라고 쓰지 않는다. 이 저장소의 tier 결정은 전부 실측 근거가 붙어 있다.

### 모델을 새로 받기 전에 디스크를 본다

`/`가 81% 차 있고 여유가 22 GiB뿐이다. 모델 추가 다운로드는 tier 결정의 제약 조건이다.

## 하지 말 것

- `inbox/`의 사용자 원본을 수정하거나 지우는 것
- `work/`, `output/`을 사용자 확인 없이 자동 삭제하는 것 (정리 제안만 한다)
- VLM이 돌고 있는데 LLM을 띄우는 것, 또는 그 반대
- 생성된 블로그 `.txt`를 git에 커밋하는 것 (`.gitignore`에 있다)
- `.env`, 토큰, 사용자 사진을 커밋하는 것

## 작업 후 갱신할 문서

`HANDOFF.md`, `WORK_LOG.md`, `TASK_STATUS.md`, 재시도가 있었으면 `RETRY_LOG.md`.
성능을 측정했으면 `bench/RESULTS.md`.

## 실행 환경

```bash
cd ~/rpi-photo-blog-agent
source .venv-blog-agent/bin/activate    # 시스템 python을 쓰지 않는다
./scripts/preflight.sh                  # 먼저 점검
pytest -q                               # 회귀 테스트
```

전체 파이프라인 1회는 사진 4장 기준 약 30분이 걸린다. 실행 중 다른 모델 서버를 띄우지 않는다.
