# 소스 맵 — 파일별 책임

각 파일이 무엇을 책임지고, 무엇을 책임지지 않는지 적는다.
"어디를 고쳐야 하는가"를 5분 안에 판단할 수 있게 하는 것이 목적이다.

범례: 🆕 이번 스코프에서 추가 · ♻️ 기존 파일 재사용 · ✏️ 기존 파일 수정

---

## 진입점

| 파일 | 책임 | 책임지지 않는 것 |
|---|---|---|
| 🆕 `app/cli_jobs.py` | 인자 파싱, 서브커맨드(`new`/`run`/`list`/`show`/`preflight`), 종료 코드, 중복 입력 확인 | 파이프라인 로직 (전부 `pipeline.py`에 위임) |
| 🆕 `app/gui/app.py` | HTTP 엔드포인트, 업로드 저장, SSE 스트림, 실행 스레드 1개 관리, 토큰 인증 | 파이프라인 로직 (동일하게 위임). **GUI에 로직을 넣지 말 것** |
| ♻️ `app/cli.py` | 기존 단일 이미지 경로 | 새 경로와 무관 |

종료 코드: `0` 성공 · `2` 에이전트 오류 · `3` 중복 입력 · `4` 품질 게이트 실패 · `130` 사용자 중단

---

## orchestrator

| 파일 | 책임 |
|---|---|
| 🆕 `app/pipeline.py` | 6개 스테이지 순서 강제, `preflight()`, tier 선택·하향, `run.lock` 획득, 진행률 이벤트 발행, 실패 시 정리 |

여기서만 스테이지 간 순서를 결정한다. 스테이지 모듈은 서로를 직접 호출하지 않는다.

---

## 스테이지 모듈

| 파일 | 책임 | 핵심 함수 |
|---|---|---|
| 🆕 `app/image_prep.py` | 원본 보존, 사본 2벌(768/1600px), EXIF 제거·검증, sha256 중복 제거, 의미 있는 slug 부여 | `prepare_images()`, `input_set_hash()` |
| 🆕 `app/privacy.py` | 규칙 기반 1차 검사(YuNet 얼굴·QR·바코드·GPS), VLM 2차 결과 병합, 잠정/확정 보류 판정 | `RuleBasedPrivacyScanner.scan()`, `merge_vlm_privacy_flags()` |
| 🆕 `app/vision_stage.py` | 사진 1장씩 VLM 실행, JSON 파싱·정규화, 재시도(해상도 하향), confidence 정책, 사진 단위 resume | `run_vision_stage()`, `normalize_vision_json()`, `merge_vision_documents()` |
| 🆕 `app/writer_stage.py` | ChatML 기록 누적, 섹션 단위 생성, 섹션 재생성, 컨텍스트 예산 축약, 총 예산 초과 중단 | `BlogWriter.generate_section()`, `run_write_stage()` |
| 🆕 `app/quality_stage.py` | 규칙 기반 검사 2층(섹션/문서) | `make_section_validator()`, `check_document()` |
| 🆕 `app/output_stage.py` | `.txt` 헤더 조립, ALT 생성, 이미지 복사(보류 제외), `.meta.json` 작성 | `assemble()`, `write_output()` |

### 고칠 때 주의

- **새 금칙어를 추가**하려면 `config/blog.yaml:quality.banned_patterns`에 넣는다. `quality_stage.py`를 고치지 않는다.
- **새 섹션을 추가**하려면 `config/blog.yaml:sections`에 넣는다. 단, 처리량 감쇠(H-1)를 먼저 읽을 것.
- **VLM 스키마를 바꾸려면** `vision_stage.py:REQUIRED_KEYS`/`ENUMS`와 `config/prompts.yaml:furniture_vision_system`을 함께 바꿔야 한다. 한쪽만 바꾸면 파서가 전부 거부한다.

---

## 런타임 계층 — 모델 수명 관리

| 파일 | 책임 |
|---|---|
| 🆕 `app/runtime.py` | `MtmdVisionRuntime`(VLM one-shot), `LlamaServerRuntime`(LLM 서버), `OllamaRuntime`(폴백), **`unload_gate()`**, `select_tier()`, `required_free_mb()` |

`unload_gate()`가 이 프로젝트에서 가장 중요한 함수다. 여기를 약화시키면 OOM으로 장비가 멈춘다.
PID 소멸과 `MemAvailable` 회수를 **실측으로** 확인하기 전에는 다음 모델을 올리지 않는다.

---

## 상태 · 설정

| 파일 | 책임 |
|---|---|
| 🆕 `app/job_state.py` | `Job`/`JobImage` 스키마, `JobStore`(atomic write), 스테이지 전이, `RunLock`(PID + stale 인수), slug 생성 |
| 🆕 `app/settings.py` | `config/*.yaml` 로드, `ModelTier` 추상화, 섹션·금칙어 파싱, tier 가용성 판정 |

`Settings`는 frozen dataclass다. 테스트에서 값을 바꾸려면 인스턴스 속성이 아니라
`ModelTier.is_available` 같은 클래스 수준을 monkeypatch해야 한다.

---

## 공유 모듈 (기존 경로와 공용, 재작성 금지)

| 파일 | 책임 | 이번에 바뀐 것 |
|---|---|---|
| ✏️ `app/memory_guard.py` | `MemAvailable`·swap·온도·throttle 관찰, 대기, 모델 크기 검증 | 온도/swap 임계값을 인자로 받도록 변경 (기본값은 기존과 동일) |
| ♻️ `app/process_runner.py` | 새 process group 실행, 스트림 기록, 타임아웃, 안전 종료, peak RSS | 변경 없음 |
| ♻️ `app/llama_options.py` | 바이너리 `--help`를 파싱해 지원하는 플래그만 사용 | 변경 없음 |
| ♻️ `app/metrics.py` | `atomic_write_text/json`, run id, UTC 시각 | 변경 없음 |
| ♻️ `app/model_discovery.py` | HF 저장소 조회, 로컬 GGUF 탐색 | 변경 없음 |
| ✏️ `app/prompt_builder.py` | YAML 프롬프트 로드 | 변경 없음 (`furniture_*` 키만 추가로 사용) |
| ♻️ `app/schemas.py` | 예외 계층, 기존 경로 dataclass | 변경 없음 |

---

## 설정 파일

| 파일 | 무엇을 바꿀 때 여기를 고치나 |
|---|---|
| 🆕 `config/models.yaml` | 모델 교체, tier 추가/제거, ctx·샘플링 조정, 게이트 기준 변경 |
| 🆕 `config/blog.yaml` | 섹션 구조, 문체, 금칙어, 브랜드 후보, 용어사전, 분량, 색상어 |
| 🆕 `config/runtime.yaml` | 경로, 메모리·온도 임계값, 타임아웃, 이미지 크기, 개인정보 임계값, GUI 포트 |
| ✏️ `config/prompts.yaml` | VLM/LLM 프롬프트 문구 (`furniture_*`가 새 경로용) |

---

## 스크립트

| 파일 | 용도 |
|---|---|
| 🆕 `scripts/run_gui.sh` | GUI 기동 (포트는 runtime.yaml에서 읽음) |
| 🆕 `scripts/run_cli.sh` | 헤드리스 실행 |
| 🆕 `scripts/preflight.sh` | 하드웨어 + 에이전트 점검 |
| 🆕 `scripts/resume_test.sh` | 강제 종료 후 재개 검증 |
| 🆕 `scripts/offline_test.sh` | 네트워크 네임스페이스 분리 후 전 과정 실행 |
| ✏️ `scripts/doctor.sh` | 환경 점검 (llama-server·얼굴 모델·설정·GUI 의존성 확인 추가) |
| ♻️ `scripts/build_llama_cpp.sh` | 고정 commit으로 llama.cpp 빌드 |
| ♻️ `scripts/download_models.py` | 모델 다운로드 |

---

## 테스트

| 파일 | 검증 대상 | 개수 |
|---|---|---|
| 🆕 `tests/test_job_state.py` | 상태 전이, atomic write, resume 플래그, stale lock 인수, 중복 입력 탐지 | 11 |
| 🆕 `tests/test_vision_stage.py` | JSON 추출, 열거값 강등, 중복 제거, **지시문 복사 거부**, 색상어 필터, confidence 정책 | 16 |
| 🆕 `tests/test_quality_stage.py` | 금칙어 전종, 브랜드 근거, 지역+시공, 반복, 언어 이탈, 섹션 형식, 문서 검사 | 28 |
| 🆕 `tests/test_writer_stage.py` | **프롬프트가 항상 이전의 확장인지**, 섹션 재생성, 거부본 격리, 컨텍스트 축약 | 8 |
| 🆕 `tests/test_pipeline_preflight.py` | 디스크 부족·모델 부재 시 `BLOCKED_EXTERNAL` | 3 |
| ✏️ `tests/test_memory_guard.py` | 기존 + 임계값이 설정에서 오는지 | 4 |
| ♻️ 기존 테스트 | 단일 이미지 경로 회귀 | 나머지 |

각 테스트는 **실측에서 실제로 관측된 실패**를 고정한 것이 많다.
예: `test_copied_prompt_placeholder_is_rejected`, `test_color_words_are_dropped_from_hardware_and_storage_fields`,
`test_dangling_unverified_phrase_is_blocked`는 모두 실행 중 눈으로 발견한 문제다.

---

## 데이터 디렉터리 (git 제외)

```
inbox/<job_id>/      사용자 원본 — 읽기 전용 취급, 절대 수정·삭제 금지
work/<job_id>/
  images/            EXIF 제거 사본 (.vlm.jpg 768px / .webp 1600px)
  vision/*.json      사진별 VLM 결과 + _merged.json
  draft/             섹션별 중간 산출물 (전원 차단 대비)
  vlm_logs/          VLM stdout/stderr
  llm_server.log     llama-server 로그
output/<job_id>/     <slug>.txt · <slug>.meta.json · images/
state/jobs/*.json    job 상태
state/run.lock       PID 기록
logs/agent-*.log     일자별 로그
```
