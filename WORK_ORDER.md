# 작업 지시서 — 로컬 VLM → LLM 블로그 에이전트

이 문서는 **무엇을 만들어야 하는가**와 **무엇으로 완료를 판정하는가**를 정의한다.
구현이 어떻게 되어 있는지는 [`docs/JOB_PIPELINE.md`](docs/JOB_PIPELINE.md)를,
무엇이 부족한지는 [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md)를 본다.

---

## 0. 불변 원칙

이 8가지는 협상 대상이 아니다. 어떤 요구사항도 이것을 덮어쓰지 못한다.

```
1. Demo / Sample / Prototype이 아니다. 실제 운영 코드다.
2. 인터넷 없이 사진만으로 블로그 생성이 가능해야 한다. (Offline-first)
3. VLM과 LLM을 절대 동시에 메모리에 올리지 않는다. 순차 실행 + 실측으로 검증한 언로드.
4. 사진에서 확인되지 않은 사실을 만들어내지 않는다. (브랜드/가격/시공일/고객/치수/지역)
5. Recoverable Error 하나로 전체 작업을 중단하지 않는다. 전략을 바꿔 재시도한다.
6. 같은 실패 명령을 아무 변화 없이 반복하지 않는다.
7. 파괴적 명령 금지: rm -rf, git reset --hard, git clean -fd, git push --force
8. 사용자 원본 사진은 절대 수정/삭제하지 않는다. 항상 사본으로 작업한다.
```

---

## 1. 스코프

### 1.1 이번 스코프

| 항목 | 내용 |
|---|---|
| 입력 | 사용자가 폴더에 올린 **가구 사진 여러 장** |
| 이미지 | 사용자 촬영 원본 (라이선스 문제 없음, **개인정보 검사는 필수**) |
| 추론 | **로컬 VLM + 로컬 LLM** (llama.cpp) |
| 출력 | **`.txt` 파일 1개 + `.meta.json` 사이드카** |
| 실행 | 로컬 GUI에서 버튼으로, 그리고 CLI로 동일하게 |
| 웹 리서치 | **선택. 네트워크 없어도 반드시 동작해야 함** |

### 1.2 명시적 비스코프

- 생성된 블로그 `.txt`의 자동 게시 (GitHub Pages 등). **코드 변경만 커밋한다.**
- 웹 검색 기반 리서치
- 클라우드 추론 API

### 1.3 기존 문서에서 유지되는 규칙

허위 시공사례 금지, 개인정보 제거, HANDOFF/WORK_LOG 갱신, 상태머신, Idempotency, 실패 은폐 금지.

---

## 2. 하드웨어 게이트 (Phase 0)

**"30B"를 무조건 가정하지 않는다.** 반드시 실측 후 tier를 결정하고 근거를 남긴다.

### 2.1 합격 기준

```
[ ] 모델 로드 성공, OOM 없음
[ ] gen 속도 >= 1.5 tok/s
[ ] swap 쓰기가 지속 폭주하지 않음
[ ] VLM 이미지 1장 분석 <= 180s
```

기준 미달이면 **한 단계 아래 tier로 내려가고**, 그 사실과 수치를 기록한다.
"지시서에 30B라고 썼으니 억지로 쓴다"로 처리하지 않는다. **근거 있는 다운그레이드는 실패가 아니다.**

### 2.2 결과 (실측 완료)

| | 확정 | 근거 |
|---|---|---|
| LLM | `Qwen3-30B-A3B-Instruct-2507 UD-IQ2_M` (MoE 총 30B / 활성 3B, 10.1 GiB) | 벤치 4.79 tok/s, 실운영 2.38~3.83 tok/s. OOM·swap 없음 |
| VLM | `Qwen2.5-VL-3B-Instruct Q4_K_M` + mmproj Q8_0, 입력 768 px | 768 px에서 159.8초로 게이트 통과 (1024 px는 227.9초로 초과) |

**지시서 대비 변경 2건과 사유:**

1. dense 30B Q4_K_M(약 18 GiB) → 16 GB에 적재 불가. MoE 구조인 30B-A3B의 IQ2_M으로 대체.
2. VLM 7B → 장비에 3B만 존재, 3B가 게이트 통과, 디스크 여유 22 GiB. 근거 있는 하향으로 기록.

전체 수치: [`bench/RESULTS.md`](bench/RESULTS.md)

---

## 3. 요구 파이프라인

```
[GUI/CLI] 사진 업로드 · 실행
        ↓
inbox/<job_id>/*.jpg|png|webp|heic
        ↓
STAGE_1_PREP      원본 보존 → 사본 → EXIF/GPS 제거 → 리사이즈 → sha256
        ↓
STAGE_2_VISION    VLM 로드 → 사진별 구조화 JSON → 개인정보 플래그
        ↓
STAGE_3_UNLOAD    VLM 완전 언로드 → RAM 회수 검증 (통과해야 다음 진행)
        ↓
STAGE_4_WRITE     LLM 로드 → VLM JSON만 근거로 한국어 블로그 생성
        ↓
STAGE_5_QUALITY   금칙어/허위주장/구조/반복 검사 (LLM 언로드 후 규칙 기반)
        ↓
STAGE_6_OUTPUT    output/<job_id>/<slug>.txt + .meta.json + 이미지 사본
        ↓
[GUI] 결과 미리보기 · 다운로드 · 재실행
```

### 3.1 스테이지별 필수 요건

**STAGE_1_PREP**
- 지원: jpg, jpeg, png, webp, heic (heic는 pillow-heif, 없으면 스킵 사유 기록)
- 원본 절대 수정 금지. `work/<job_id>/images/`로 사본 생성
- EXIF 전체 제거(특히 GPS) **후 제거 여부를 검증**
- 긴 변 기준 2벌: VLM 입력용 / 게시용
- sha256 중복 제거, 의미 있는 영문 slug 부여 (`IMG_0012.jpg` 금지)
- 규칙 기반 개인정보 1차 검사

**STAGE_2_VISION**
- 이미지 1장씩 순차 처리 (**배치 금지** — RAM 안정성 우선)
- 이미지당 타임아웃, 실패 시 해상도 낮춰 1회 재시도 → 그래도 실패면 스킵하고 기록
- 온도 초과 시 대기 후 계속 (**중단하지 않음**)
- 한국어 JSON only, 확신 없으면 `null` + `uncertain`에 사유
- `confidence < 0.5` 항목은 "확인되지 않음"으로 처리

**STAGE_3_UNLOAD** — 이 단계가 이 프로젝트의 핵심이다
- 종료를 "명령을 보냈으니 됐다"로 처리하지 **않는다**
- ① 종료 신호 ② PID 소멸 확인 ③ `MemAvailable` 폴링 ④ 미달 시 drop_caches ⑤ 그래도 미달 → tier 하향
- 게이트 통과 전에는 **절대** LLM을 로드하지 않는다
- 언로드 전/후 값을 로그와 job 기록에 남긴다

**STAGE_4_WRITE**
- LLM에 **원본 이미지를 넣지 않는다.** vision JSON 통합본 + 설정만
- 섹션 단위 다단 생성 (한 번에 전체 생성 금지)
- 섹션 사이 KV 캐시 유지, 모델 재로드 금지
- 중간 산출물을 매 섹션 저장 (전원 차단 대비)
- 진행률을 상태 파일 + GUI SSE로 실시간 보고

**STAGE_5_QUALITY** — LLM 재호출 없이 규칙 기반
- TODO/placeholder, 가격, 전화번호, 시공 주장, 후기, 인증, 치수, 지역+시공
- vision JSON에 없는 브랜드명
- 제목 존재, 섹션 최소 개수, 문장 반복, 3-gram 자기중복, 한글 비율
- 이미지 참조 1:1 대응, 전 이미지 ALT, `PRIVACY_HOLD` 이미지 미노출
- 실패 시 **해당 섹션만 재생성**(전체 아님). 3회 실패하면 `FAILED_RECOVERABLE` + 사용자 노출.
  **몰래 통과시키지 않는다.**

**STAGE_6_OUTPUT** — `.txt` 형식은 [`docs/JOB_PIPELINE.md` §10](docs/JOB_PIPELINE.md) 참조

---

## 4. 상태 관리 요건

상태 값은 이 5가지만 사용한다.

```
SUCCESS | FAILED_RECOVERABLE | BLOCKED_EXTERNAL | IN_PROGRESS | NOT_STARTED
```

- 각 스테이지 시작/종료 시 job json을 **atomic write** (temp → `os.replace`)
- 중단 후 재실행 시 완료된 스테이지는 건너뛴다 (resume)
- `state/run.lock`으로 중복 실행 방지, PID 기록, stale lock 자동 해제
- 동일 이미지 집합(sha256 정렬 해시)의 job이 이미 있으면 재생성 대신 사용자 확인

---

## 5. GUI 요건

- FastAPI + Jinja2 + 순수 JS (무거운 프론트 프레임워크 금지 — Pi 자원 절약)
- 진행 상황은 SSE. WebSocket 불필요
- `0.0.0.0:8770` 바인딩, 설정으로 변경 가능. 반응형
- 인증: 기본 LAN 전용, 토큰 설정 시 헤더 인증
- **GUI는 파이프라인을 직접 실행하지 않는다.** orchestrator를 호출만 하고,
  CLI로도 100% 동일하게 동작해야 한다

필수 엔드포인트: `POST/GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/run`,
`POST /api/jobs/{id}/cancel`, `GET/PUT /api/jobs/{id}/result`, `GET /events`, `GET /api/system`

---

## 6. 실패 처리 규칙

| 상황 | 요구 처리 |
|---|---|
| 모델 로드 OOM | 한 tier 낮은 quant/모델로 자동 폴백, 기록 후 계속 |
| VLM 이미지 1장 실패 | 해상도 축소 재시도 → 스킵하고 나머지 진행 (**전체 중단 금지**) |
| LLM 출력 깨짐 | 파서 완화 → 재프롬프트 → 섹션 축소 재생성 |
| 언로드 후 RAM 부족 | drop_caches → 대기 → tier 다운 |
| 디스크 부족 | 정리 **제안**(자동 삭제 금지) → `BLOCKED_EXTERNAL` |
| 모델 파일 없음 | `BLOCKED_EXTERNAL`, 정확한 다운로드 명령을 기재 |
| 온도 스로틀링 | 대기 후 계속, 기록 |

모든 재시도는 [`RETRY_LOG.md`](RETRY_LOG.md)에 `Attempt / Failure / Root cause / Action / Result` 형식으로 남긴다.

---

## 7. 완료 정의 (Definition of Done)

현재 달성 상태는 [`TASK_STATUS.md`](TASK_STATUS.md)에서 관리한다. **실행해서 확인한 것만 체크한다.**

```
[ ] Phase 0 실측 완료, bench/RESULTS.md 작성, 모델 tier 확정 근거 기록
[ ] config/*.yaml로 모델·경로·파라미터 전부 외부화 (하드코딩 0)
[ ] 사진 3장 이상 넣고 CLI 1회 완주 → output/<job_id>/<slug>.txt 생성
[ ] 동일 작업을 GUI에서 버튼만으로 완주
[ ] VLM 종료 후 LLM 로드 시점의 free 로그가 실제로 회수를 증명
[ ] 파이프라인 중간 강제 종료 후 재실행 시 resume 동작 확인
[ ] 개인정보 포함 사진 1장 테스트 → PRIVACY_HOLD로 본문 제외 확인
[ ] 네트워크 차단 상태에서 전 과정 성공
[ ] 품질 게이트 실패 케이스 1건 인위적으로 만들어 재생성 동작 확인
[ ] requirements.txt는 실제 import 기준 (pip freeze 통째 복사 금지)
[ ] .env / 토큰 / 개인정보가 git에 포함되지 않음 (커밋 전 스캔)
[ ] HANDOFF.md / WORK_LOG.md / TASK_STATUS.md 갱신
[ ] 코드 변경분 commit (블로그 .txt는 커밋 대상 아님)
```

---

## 8. 작업 순서

```
1.  하드웨어·git·문서 조사
2.  HANDOFF·AGENTS·TASK_STATUS·WORK_LOG 읽기
3.  기존 코드 구조 파악, 재사용 여부 결정      ← 재작성보다 재사용을 우선한다
4.  Phase 0 모델 실측 → bench/RESULTS.md → tier 확정
5.  디렉터리 + config/*.yaml 생성
6.  job_state / runtime(언로드 게이트) 구현 후 단위 테스트
7.  image_prep + privacy 구현, 샘플 사진으로 검증
8.  vision_stage 구현, 사진 1장 → JSON 성공 확인
9.  언로드 게이트를 실측 로그로 검증
10. writer_stage 구현, 섹션 단위 생성 → .txt 완성
11. quality_stage 구현, 실패 케이스 테스트
12. orchestrator + cli로 엔드투엔드 1회 완주
13. GUI 구현 → 브라우저에서 버튼만으로 완주
14. 오프라인 / resume / 개인정보 테스트
15. 보안 스캔 → git diff 검토 → commit
16. HANDOFF / WORK_LOG / TASK_STATUS 갱신
```

각 단계 완료마다 [`WORK_LOG.md`](WORK_LOG.md)에 KST 시각과 함께 기록한다.

---

## 9. 보고 형식

### 9.1 착수 보고

```
- Loaded HANDOFF: <경로 또는 '없음'>
- Smoke Test 상태: <PASSED / UNKNOWN>
- 현재 Production State: <스테이지>
- 하드웨어 실측 요약: RAM / swap / NVMe 여부 / 온도
- 확정된 모델 tier: VLM=<...> LLM=<...> (근거 1줄)
- 기존 코드 재사용 여부: <재사용 / 신규 생성>
- 현재 Blocker: <없음 또는 내용>
- 바로 다음 작업: <1개>
```

### 9.2 최종 보고

```
Mode / Hardware / VLM / LLM / Unload Gate
Job ID / Images In·Used·Privacy-Hold
Article 경로 · 글자수 · 소요
Quality Gate / Offline Test / Resume Test / GUI
Retry Count / Blockers / Docs Updated
Exact Next Action / Resume Command
```

---

## 10. 이 지시서를 받은 사람이 먼저 할 일

```bash
cd ~/rpi-photo-blog-agent
cat AGENTS.md                    # 불변 원칙과 금지 사항
cat docs/LIMITATIONS.md          # 알려진 결함 — 지뢰 목록
./scripts/preflight.sh           # 환경이 살아 있는지
pytest -q                        # 회귀 테스트
```

그다음 [`docs/JOB_PIPELINE.md`](docs/JOB_PIPELINE.md)와 [`docs/SOURCE_MAP.md`](docs/SOURCE_MAP.md)를 읽는다.
