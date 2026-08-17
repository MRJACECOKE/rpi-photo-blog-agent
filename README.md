# rpi-photo-blog-agent

Raspberry Pi 5 16GB에서 사진 한 장을 로컬 VLM으로 분석한 뒤, VLM 프로세스를 완전히 종료하고 메모리 회복을 확인한 다음 Qwen3 30B 계열 GGUF LLM으로 한국어 Markdown 블로그 글을 생성하는 순차 실행 에이전트입니다.

![VLM에서 LLM으로 이어지는 메모리 안전 워크플로우](docs/diagrams/workflow.svg)

Python은 모델을 직접 로드하지 않습니다. 프로젝트 전용으로 빌드한 `llama.cpp`의 `llama-mtmd-cli`와 `llama-completion`을 subprocess로 한 번씩 실행합니다. VLM과 LLM은 동시에 실행되지 않습니다.

## 두 가지 실행 경로

| | 사진 1장 → Markdown | **사진 여러 장 → `.txt` (+ GUI)** |
|---|---|---|
| 진입점 | `python -m app.cli --image ...` | `python -m app.cli_jobs` / `scripts/run_gui.sh` |
| 출력 | `outputs/<run_id>.md` | `output/<job_id>/<slug>.txt` + `.meta.json` + `images/` |
| LLM | `llama-completion` 1회 | `llama-server` 1회 기동 후 섹션 단위 생성 |
| 개인정보 | VLM privacy_notes | 규칙 기반 1차 + VLM 2차, `PRIVACY_HOLD` 분류 |
| 재개 | 없음 | `state/jobs/<job_id>.json` 기반 resume |

부엌가구·싱크대·신발장 같은 **가구 사진 여러 장을 한 편의 한국어 블로그 글로** 만드는 쪽이 두 번째 경로입니다.
자세한 설계는 [`docs/JOB_PIPELINE.md`](docs/JOB_PIPELINE.md)를 보세요.

```bash
./scripts/preflight.sh                                    # 점검
./scripts/run_cli.sh new --images ~/photos --category 부엌가구
./scripts/run_cli.sh run --job <job_id>
./scripts/run_gui.sh                                      # http://<pi-ip>:8770
```

GUI는 파이프라인을 직접 실행하지 않고 CLI와 같은 orchestrator(`app/pipeline.py`)를 호출합니다.

![6단계 파이프라인](docs/diagrams/job-pipeline.svg)

## 클라우드 API를 쓰지 않는다는 것을 직접 확인하는 방법

실행 중에 아래를 돌리면 됩니다. 모든 추론은 이 장비의 `llama.cpp` 프로세스에서 일어납니다.

```bash
ps -eo pid,rss,args | grep llama-server   # 로드한 GGUF 경로 확인
ss -tanp | grep "pid=<PID>"               # 루프백 외 연결이 없어야 함
grep gguf /proc/<PID>/maps                # 모델이 프로세스에 mmap됐는지
grep -rniE "api\.openai|anthropic|googleapis|Authorization: Bearer" app/   # 0건
```

실제 확인 결과는 [`docs/JOB_PIPELINE.md` §12](docs/JOB_PIPELINE.md)에 있습니다.

## 문서

**이번 스코프 (사진 여러 장 → `.txt` + GUI)**

| 문서 | 내용 |
|---|---|
| [작업 지시서](WORK_ORDER.md) | 무엇을 만들어야 하는가, 완료 판정 기준 |
| [엔지니어링 문서](docs/JOB_PIPELINE.md) | 설계와 판단 근거 (다이어그램 6종 포함) |
| [소스 맵](docs/SOURCE_MAP.md) | 파일별 책임, "어디를 고쳐야 하는가" |
| [한계점 · 문제점 · 개선점](docs/LIMITATIONS.md) | **인수인계용 결함 목록. 먼저 읽을 것** |
| [Phase 0 실측 결과](bench/RESULTS.md) | 하드웨어·모델 벤치마크와 tier 결정 근거 |
| [재시도 로그](RETRY_LOG.md) | 전략을 바꾼 지점과 이유 |
| [작업 로그](WORK_LOG.md) | 시간순 기록 |
| [진행 상태](TASK_STATUS.md) | 완료 정의 체크리스트 |
| [에이전트 규칙](AGENTS.md) | 이 저장소의 불변 원칙과 금지 사항 |

**기존 스코프 (사진 1장 → Markdown)**

- [Engineering handoff](HANDOFF.md)
- [설계·운영 기술 문서](docs/ENGINEERING.md)
- [Smoke test 및 OOM 분석](docs/SMOKE_TEST_REPORT.md)
- [소스 공개 범위](docs/SOURCE_MANIFEST.md)
- [GitHub 게시 절차](docs/GITHUB_PUBLISH.md)
- [검수된 최종 블로그 샘플](examples/smoke-success.md)

## 실제 smoke test 입력

![Smoke test에 사용한 주방 사진](fixtures/smoke/kitchen-cabinets-sink.jpg)

성공 run에는 위 1280×854 사진을 사용했습니다. “Kitchen Cabinets - Sink” by amslerPIX, [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/), via [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Kitchen_Cabinets_-_Sink.jpg). 저장소의 파일은 크기를 조정하고 EXIF를 제거한 파생본입니다. 자세한 내용은 [`fixtures/smoke/ATTRIBUTION.md`](fixtures/smoke/ATTRIBUTION.md)를 참조하세요.

## 왜 Q4_K_M이 기본이 아닌가

`Qwen3-30B-A3B-Instruct-2507`은 MoE 모델이라 토큰당 활성 파라미터 수가 전체 파라미터보다 적습니다. 하지만 GGUF 파일은 전체 모델 가중치를 저장하므로 파일 크기와 mmap/page cache 압박은 여전히 큽니다. Raspberry Pi 5 16GB에서는 18GB급 `Q4_K_M`을 기본값으로 잡으면 OOM, swap 폭주, 시스템 정지 위험이 커집니다.

그래서 LLM 기본 선호도는 `IQ2_M`, `Q2_K_L`, `Q2_K`, `IQ2_S` 순서입니다. 전체 GGUF shard 합계가 `12.5GiB`를 넘으면 기본 실행을 거부합니다.

## 설치

```bash
cd ~/rpi-photo-blog-agent
./scripts/bootstrap.sh
source .venv-blog-agent/bin/activate
```

`bootstrap.sh`는 `python3 -m venv .venv-blog-agent`로 독립 venv를 만들고 `PIP_REQUIRE_VIRTUALENV=true`를 적용합니다. 시스템 Python이나 기존 venv에는 설치하지 않습니다. 시스템 패키지는 자동 설치하지 않으며, 원하면 명시적으로 실행합니다.

```bash
./scripts/bootstrap.sh --install-system-deps
```

## llama.cpp 빌드

```bash
./scripts/build_llama_cpp.sh
```

`third_party/llama.cpp`에 별도 clone/build를 만들고 `llama-completion`, `llama-mtmd-cli`, `llama-quantize`를 검증합니다. `.llama-cpp-version`에 기록된 commit을 사용하며 기존 시스템 llama.cpp는 변경하지 않습니다.

## 모델 다운로드

```bash
python scripts/download_models.py --dry-run
python scripts/download_models.py
```

VLM은 `ggml-org/Qwen2.5-VL-3B-Instruct-GGUF`의 `Q4_K_M`과 mmproj를 선택합니다. LLM은 `unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF`에서 실제 파일 목록을 조회해 선호 양자화를 고릅니다. shard 모델이면 모든 shard 크기를 합산하고 첫 shard를 실행 모델 경로로 씁니다.

## 설정

```bash
cp .env.example .env
```

주요 값:

```dotenv
LLAMA_CLI=third_party/llama.cpp/build/bin/llama-completion
LLAMA_MTMD_CLI=third_party/llama.cpp/build/bin/llama-mtmd-cli
MAX_LLM_GGUF_GIB=12.5
ALLOW_OVERSIZED_MODEL=false
MIN_AVAILABLE_BEFORE_LLM_MB=12288
MIN_AVAILABLE_DURING_RUN_MB=1536
```

`LLM_MODEL_PATH`를 지정하면 자동 선택보다 우선합니다. 단, `ALLOW_OVERSIZED_MODEL=true` 또는 CLI의 `--force-oversized-model`이 없으면 12.5GiB 초과 모델은 거부됩니다.

## 진단

```bash
./scripts/doctor.sh
```

ARM64, 64-bit OS, RAM, venv, llama.cpp 바이너리, VLM/mmproj, LLM shard 합계, 디스크 여유, swap, 저장장치 유형 추정, 온도, throttling, lock, outputs 쓰기 가능 여부를 PASS/WARN/FAIL로 표시합니다. 시스템 설정은 변경하지 않습니다.

## Dry Run

```bash
python -m app.cli \
  --image inputs/example.jpg \
  --topic "사진으로 기록하는 하루" \
  --dry-run
```

dry-run은 이미지 검증/전처리, 메모리 확인, 모델/바이너리 경고, 실제 실행될 argument list 출력을 수행합니다. 모델 프로세스는 실행하지 않습니다.

## 실제 실행

```bash
python -m app.cli \
  --image fixtures/smoke/kitchen-cabinets-sink.jpg \
  --topic "사진으로 살펴보는 주방 수납과 작업 동선 아이디어" \
  --audience "실용적인 주방 정리 아이디어를 찾는 한국어 독자" \
  --tone "전문적이고 차분하며 과장 없는 한국어" \
  --output outputs/example-blog.md
```

## 순차 파이프라인

상태 머신은 다음 순서만 허용합니다.

```text
preflight
-> 이미지 전처리
-> VLM 메모리 확인
-> VLM 실행
-> VLM 종료 확인
-> VLM PID 소멸 확인
-> MemAvailable 회복 대기
-> 블로그 프롬프트 구성
-> LLM 메모리 확인
-> LLM 실행
-> LLM 종료 확인
-> 결과와 metrics 저장
```

`runs/agent.lock`에 `fcntl.flock`을 걸어 동시 실행을 막습니다. subprocess는 `start_new_session=True`로 새 process group에서 실행하고, timeout 또는 안전 중단 시 해당 process group만 종료합니다. `pkill`, `killall`, `pkill -f llama`는 사용하지 않습니다.

## 메모리 보호 정책

판단 기준은 `free`가 아니라 Linux `/proc/meminfo`의 `MemAvailable`입니다.

VLM 전 최소 4096MiB, LLM 전 최소 12288MiB를 기다립니다. 실행 중 MemAvailable이 1536MiB 아래로 내려가거나 swap 사용률이 85%를 넘거나 CPU 온도가 82도 이상이면 현재 모델 process group을 종료합니다. `/proc/sys/vm/drop_caches`는 기본적으로 건드리지 않습니다.

VLM 종료 후 LLM 실행 여부는 run 로그와 `metrics.json`의 `vlm.finished_at`, `memory_release`, `llm.started_at`을 보면 확인할 수 있습니다. 또한 VLM PID가 살아 있으면 LLM 시작을 거부합니다.

## 출력 구조

각 실행은 `runs/YYYYMMDDTHHMMSSZ-xxxxxxxx/` 아래에 저장됩니다.

```text
request.json
prepared_image.jpg
vision_prompt.txt
vision_stdout.txt
vision_stderr.txt
vision.json
blog_prompt.txt
llm_stdout.txt
llm_stderr.txt
blog.md
metrics.json
run.log
```

최종 블로그는 run 디렉터리의 `blog.md`와 사용자가 지정한 `--output` 양쪽에 저장됩니다. 공개용 smoke 결과는 `examples/smoke-success.md`에 별도로 보존합니다.

## 문제 해결

OOM: 더 작은 LLM 양자화(`IQ2_M`, `IQ2_S`)를 사용하고 `MIN_AVAILABLE_BEFORE_LLM_MB`를 높이세요. 12.5GiB 초과 모델은 기본 거부됩니다.

swap 폭주: swap 사용률 85% 초과 시 실행을 중단합니다. NVMe SSD와 더 작은 GGUF를 권장합니다.

온도 상승: 82도 이상이면 중단합니다. Raspberry Pi 5에는 능동 냉각을 권장합니다.

잘못된 mmproj: VLM 실행이 실패하면 `models/vlm`의 mmproj가 같은 VLM 계열인지 확인하세요. `VLM_MMPROJ_PATH`로 명시할 수 있습니다.

JSON 파싱 실패: VLM stdout 원문은 `vision_stdout.txt`에 보존됩니다. JSON 복구가 실패하면 LLM 단계로 넘기지 않습니다.

GGUF shard 누락: 첫 shard만 있으면 실행하지 않습니다. `scripts/download_models.py`로 모든 shard를 받으세요.

실행 속도가 매우 느림: CPU 전용 30B급 모델은 느립니다. NVMe, active cooling, 작은 양자화, 낮은 `LLM_MAX_TOKENS`를 사용하세요.

모델 라이선스: Hugging Face 저장소의 모델 카드와 라이선스를 사용 전에 확인하세요.

## systemd user service

선택 기능입니다.

```bash
./scripts/install_user_service.sh
```

서비스는 일반 CLI 실행을 막지 않습니다. 고정 이미지 인자는 넣지 않고 `.env`의 `AGENT_ARGS` 또는 별도 systemd override로 지정합니다.
