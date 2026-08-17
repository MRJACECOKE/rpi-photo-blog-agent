# TASK STATUS

기준 시각: 2026-08-17 KST
대상: 사진 폴더 → 블로그 `.txt` 파이프라인 (`app.cli_jobs`, `app.gui`)

## Phase 진행

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 하드웨어·모델 실측, tier 확정 | `SUCCESS` — `bench/RESULTS.md` |
| 1 | 상태머신 / Job 관리 / lock | `SUCCESS` — `app/job_state.py` |
| 2 | STAGE_1 이미지 전처리 + 개인정보 1차 | `SUCCESS` — `app/image_prep.py`, `app/privacy.py` |
| 3 | STAGE_2 VLM 분석 | `SUCCESS` — `app/vision_stage.py` |
| 4 | STAGE_3 언로드 게이트 | `SUCCESS` — `app/runtime.py:unload_gate()` |
| 5 | STAGE_4 섹션 단위 작성 | `SUCCESS` — `app/writer_stage.py` |
| 6 | STAGE_5 품질 게이트 | `SUCCESS` — `app/quality_stage.py` |
| 7 | GUI | `SUCCESS` — `app/gui/` |
| 8 | 실행 스크립트 / systemd | `SUCCESS` — `scripts/`, `deploy/blogagent.service` (자동 활성화는 안 함) |

## 확정된 모델 tier

```
VLM  qwen2.5-vl-3b-q4_k_m   입력 768px   실측 이미지당 150~175s   peak RSS 5.0GB
LLM  qwen3-30b-a3b-iq2_m    llama-server  실측 4.79 tok/s          peak RSS 10.7GB (mmap)
```

근거는 `bench/RESULTS.md`. 지시서의 dense 30B Q4(18GiB)는 16GB에 적재 불가라 사다리에서 제외했다.

## 완료 정의 체크리스트

기준 시각 2026-08-17 16:26 KST. **실제로 실행해서 확인한 것만 `[x]`로 표시한다.**

- [x] Phase 0 실측 완료, `bench/RESULTS.md` 작성, tier 확정 근거 기록
- [x] `config/*.yaml`로 모델·경로·파라미터 외부화 (하드코딩 없음)
- [x] VLM 종료 후 LLM 로드 시점의 메모리 회수를 로그로 증명
      → job json `memory_evidence.unload_gate`: 15,105 MiB, PID 소멸 확인, 목표 12,288 MiB
- [x] 개인정보 포함 사진 테스트 → `PRIVACY_HOLD`로 본문 제외 확인
      → 인물 사진 규칙 기반 0.91 + VLM 확인으로 보류. 조명 기구 오탐 0.767은 VLM 부정으로 해제
- [x] 품질 게이트 실패 케이스 → 해당 섹션만 재생성 동작 확인
      → `tests/test_writer_stage.py::test_failed_section_is_regenerated_and_only_final_text_is_kept`
- [x] `requirements.txt`를 실제 import 기준으로 작성
- [x] `.gitignore`에 `output/`, `work/`, `inbox/`, `state/jobs/` 추가
- [x] 보안 스캔 (51개 신규 파일, 시크릿 패턴 0건, 사용자 사진·`.env` 미포함 확인)
- [x] 로컬 추론임을 프로세스·소켓·mmap 수준에서 확인
      → `llama-server`가 127.0.0.1에만 바인딩, 외부 연결 0개, GGUF가 프로세스에 mmap됨
- [ ] **진행 중** 사진 4장으로 CLI 1회 완주 → `output/<job_id>/<slug>.txt` 생성
      → job `20260817-e2e-kitchen`, 12개 섹션 중 8개 완료
- [ ] **미실행** GUI에서 버튼만으로 완주 (API 엔드포인트 개별 응답은 확인함)
- [ ] **미실행** 파이프라인 중간 강제 종료 후 재실행 시 resume 동작 확인
      → `scripts/resume_test.sh` 작성 완료, job `20260817-offline-resume` 준비됨
- [ ] **미실행** 네트워크 차단 상태에서 전 과정 성공
      → `scripts/offline_test.sh` 작성 완료. 네임스페이스 분리·루프백 동작은 사전 확인함
- [ ] **미실행** 코드 변경분 commit
- [x] `HANDOFF.md` / `WORK_LOG.md` / `TASK_STATUS.md` 갱신 (실행 결과 반영은 완주 후)

## 알려진 제약

1. **microSD 단독, NVMe 없음.** LLM cold load가 137.8s다. page cache가 살아 있으면 1.85s로 떨어진다.
   연속 실행에서는 문제가 없고, 재부팅 직후 첫 실행만 느리다.
2. **디스크 여유 22GiB (81% 사용).** 모델 추가 다운로드가 제약된다. VLM을 7B로 올리지 않은 이유 중 하나다.
3. **VLM이 3B라 지시 준수력이 약하다.** 프롬프트를 열거형으로 고정하고 파서에서 placeholder·중복을 검사해 막고 있다.
   더 큰 VLM으로 올리면 이미지 인코딩 시간이 180s 게이트를 넘길 위험이 있다.
4. **STAGE_4는 resume 대상이 아니다.** 생성 단계이고, 재실행이 곧 "재생성" 의미이기 때문이다.
   가장 긴 STAGE_2(VISION)는 사진 단위로 재개된다.
5. **개인정보 1차 검사는 얼굴·QR·바코드·GPS만 잡는다.** 문자 PII는 VLM 2차 검사에 의존한다.
   OCR 기반 검사는 넣지 않았다.

## 다음에 손댈 만한 것

- `output/`에 쌓인 과거 job 정리 UI (자동 삭제는 하지 않는다)
- 섹션별 부분 재생성 버튼 (현재는 전체 재작성)
- VLM 7B tier를 디스크 확보 후 실측해 비교
