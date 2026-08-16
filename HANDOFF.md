# Engineering handoff

기준 시각: 2026-08-17 KST
상태: 구현·smoke 검증·공개 저장소 패키징 완료

## 최종 결과

- OOM을 일으킨 17.28 GiB급 Ollama Q4 모델 경로를 폐기하고, 로컬 `llama.cpp`와 10.10 GiB `IQ2_M` 모델로 전환했다.
- VLM과 LLM을 절대 동시에 실행하지 않는 상태 머신을 구현했다.
- VLM PID 소멸과 `MemAvailable >= 12,288 MiB`를 모두 확인해야 LLM을 시작한다.
- 성공 smoke에서 VLM exit 0, LLM retry exit 0, OOM 없음, 당시 테스트 32개 통과를 확인했다. 공개 패키징 회귀 테스트는 33개가 통과했다.
- 최종 검수 블로그는 [`examples/smoke-success.md`](examples/smoke-success.md)이며 SHA-256은 `69b0e33d3b31993d370064acb75059398d4080440363df400276de2a46de4a67`이다.
- 실제 smoke 입력은 [`fixtures/smoke/kitchen-cabinets-sink.jpg`](fixtures/smoke/kitchen-cabinets-sink.jpg)이며, CC BY 2.0 attribution과 입력 해시를 함께 보존했다.

![VLM에서 LLM으로 이어지는 메모리 안전 워크플로우](docs/diagrams/workflow.svg)

## 다음 담당자가 먼저 읽을 문서

1. [`docs/ENGINEERING.md`](docs/ENGINEERING.md): 설계, 상태 머신, 메모리 안전장치, 운영 방법
2. [`docs/SMOKE_TEST_REPORT.md`](docs/SMOKE_TEST_REPORT.md): OOM 원인과 성공 검증 수치
3. [`docs/SOURCE_MANIFEST.md`](docs/SOURCE_MANIFEST.md): 업로드 대상 소스와 제외 대상
4. [`docs/GITHUB_PUBLISH.md`](docs/GITHUB_PUBLISH.md): GitHub 게시 절차와 점검표
5. [`docs/evidence/smoke-20260816.json`](docs/evidence/smoke-20260816.json): 경로를 제거한 기계 판독용 증거

## 운영상 중요한 사실

- 검증 장비는 Raspberry Pi 5 16GB, ARM64, Debian 13이다.
- LLM은 `Qwen3-30B-A3B-Instruct-2507-UD-IQ2_M.gguf`였다. 12.5 GiB보다 큰 GGUF는 기본 거부한다.
- `.env`는 성공 장비의 로컬 설정이며 커밋하지 않는다. 공개 기본값은 `.env.example`에 반영했다.
- GGUF, `llama.cpp` checkout/build, venv, raw run 로그는 커밋하지 않는다. 빌드 스크립트와 정확한 llama.cpp commit hash로 재현한다.
- 성공 retry의 모델 원문에는 시각적으로 뒷받침되지 않는 표현이 있어 사람이 사진과 대조해 제거했다. 따라서 기술 파이프라인 성공과 콘텐츠 사실성 검수는 별도 gate다.
- 기존 throttle 이력 비트가 관측됐지만 성공 실행의 최고 기록 온도는 66.65°C였다. 다음 장기 실행 전 `vcgencmd get_throttled`를 다시 확인한다.

## 이번 작업에서 변경된 소스

- `app/orchestrator.py`: VLM 종료·PID 소멸·메모리 회복을 LLM 선행 조건으로 강제
- `app/llm_runner.py`: 비대화형 completion 플래그와 prompt 비표시 옵션 적용
- `app/output_parser.py`, `app/schemas.py`: placeholder, 잘린 출력, prompt leakage hard gate 추가
- `config/prompts.yaml`: 관찰 근거만 허용하고 schema placeholder 복사를 금지
- `scripts/retry_llm.py`: 성공한 VLM 결과를 보존한 채 LLM만 안전하게 재시도
- `app/config.py`, `.env.example`: 실제 성공한 16GB 보수적 메모리 profile을 기본값으로 반영
- `scripts/build_llama_cpp.sh`, `scripts/doctor.sh`: `llama-completion` 빌드·검증 및 commit pin 적용
- `tests/`: memory handoff와 출력 검증을 포함한 33개 회귀 테스트

## 로컬 원본 증거 위치

공개 저장소에는 정제된 evidence만 포함한다. 장비에 남아 있는 원본은 다음과 같다.

- 성공 run: `runs/20260816T160703Z-f3393417/`
- 전체 파이프라인 metrics: `runs/20260816T160703Z-f3393417/metrics.json`
- 성공 LLM retry metrics: `runs/20260816T160703Z-f3393417/llm_retry_metrics.json`
- 콘텐츠 audit: `runs/20260816T160703Z-f3393417/smoke-success-audit.json`
- 모델 원문: `runs/20260816T160703Z-f3393417/llm_retry_stdout.txt`
- 최종 운영 출력: `outputs/smoke-success.md`

원본 run은 입력 이미지, 절대 경로, 모델 로그를 포함할 수 있으므로 공개 전에 자동으로 추가하지 않는다.

## 재개 명령

```bash
cd ~/rpi-photo-blog-agent
source .venv-blog-agent/bin/activate
./scripts/doctor.sh
pytest -q
python -m app.cli --image inputs/example.jpg --topic "사진으로 기록하는 하루" --dry-run
```

실제 사진 smoke는 15~20분이 걸릴 수 있다. 실행 중 다른 모델 서버를 동시에 띄우지 않는다.
