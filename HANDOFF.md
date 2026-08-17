# Engineering handoff

기준 시각: 2026-08-17 KST
저장소: `MRJACECOKE/rpi-photo-blog-agent` (branch `main`)
장비: Raspberry Pi 5 Model B Rev 1.1 / 16 GB / aarch64 / kernel 6.18.34-rpt-rpi-2712

## 이번 작업에서 무엇이 바뀌었나

기존 저장소는 **사진 1장 → Markdown 1편**을 만드는 단일 실행 에이전트였다.
이번에 **사진 여러 장을 job 단위로 처리해 `.txt` 1개 + `.meta.json`을 만들고, 로컬 GUI에서 버튼으로 실행하는 경로**를 추가했다.
기존 경로(`app/cli.py`)는 그대로 살아 있고 테스트도 계속 통과한다.

새 진입점은 `app/cli_jobs.py`(CLI)와 `app/gui/app.py`(GUI)이며, **둘 다 같은 `app/pipeline.py`를 호출한다.**
GUI는 파이프라인 로직을 갖고 있지 않다.

설계 상세는 [`docs/JOB_PIPELINE.md`](docs/JOB_PIPELINE.md), 실측 근거는 [`bench/RESULTS.md`](bench/RESULTS.md)에 있다.

## 확정된 모델 tier와 근거

```
VLM  qwen2.5-vl-3b-q4_k_m (Q4_K_M + mmproj Q8_0), 입력 긴 변 768 px
     실측 이미지당 150~175 s (게이트 180 s), peak RSS 5.0 GB
LLM  qwen3-30b-a3b-iq2_m (MoE 총 30B / 활성 3B, UD-IQ2_M 10.1 GiB), llama-server
     실측 gen 4.79 tok/s (게이트 1.5), prompt eval 8.57 tok/s, peak RSS 10.7 GB (mmap)
```

**지시서의 dense 30B Q4_K_M(약 18 GiB)은 16 GB에 적재 불가라 사다리에서 제외했다.**
VLM은 지시서가 7B를 상정했으나 장비에 3B만 있고, 3B가 게이트를 통과했으며 디스크 여유가 22 GiB뿐이라 하향했다.
근거 있는 하향이며 수치는 전부 `bench/RESULTS.md`에 있다.

## 반드시 알아야 할 사실 6가지

1. **peak RSS 10.7 GB는 물리 점유가 아니다.** llama.cpp가 GGUF를 mmap하므로 대부분 파일 백업 페이지다.
   LLM 실행 중에도 `MemAvailable`은 14 GB 아래로 내려가지 않았다.
   그래서 언로드 게이트 기준은 RSS가 아니라 `MemAvailable`이고, tier마다 `min_free_mb`를 실측값으로 따로 둔다.

2. **cold load 137.8 s vs warm load 1.85 s.** 차이는 전부 microSD에서 10.1 GiB를 읽는 시간이다.
   **이 장비에는 NVMe가 없다.** 재부팅 직후 첫 실행만 느리고, 이후에는 page cache가 살린다.

3. **진짜 병목은 생성이 아니라 프롬프트 처리다 (8.57 tok/s).**
   그래서 작성 단계는 `llama-server`를 한 번만 띄우고 ChatML 대화 기록을 누적해
   프롬프트가 항상 직전 프롬프트의 확장이 되게 만든다. `cache_prompt`가 공통 프리픽스를 재사용한다.
   실측 효과: 첫 섹션 195.8 s(1,340 토큰 프리필 포함) → 이후 섹션 약 70 s.
   one-shot으로 12개 섹션을 만들면 프리필만 약 28분이 낭비된다.

4. **`llama-server` 실행 파일은 원래 빌드에 없었다.** `libllama-server-impl.so`만 있었다.
   **같은 commit `0cea362`에서** `cmake --build build --target llama-server`로 링크만 추가했다.
   다른 commit의 llama-server(장비에 있는 build 9986)를 쓰지 않았다. 런타임 일관성을 위해서다.

5. **얼굴 검출은 2단이다.** OpenCV 5.x가 Haar cascade를 제거해 YuNet ONNX를 쓴다.
   주방 사진의 조명 기구 장식이 0.767로 얼굴 오검출된 실측 사례가 있어,
   0.90 이상만 확정 보류하고 0.75~0.90은 VLM 2차 검사가 부정하면 해제한다.
   **이 모델 파일은 git에 없다.** 아래 명령으로 받는다.

   ```bash
   curl -sSL -o models/privacy/face_detection_yunet_2023mar.onnx \
     https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
   ```

6. **Qwen2.5-VL-3B는 지시 준수력이 약하다.** 두 가지가 실측으로 확인됐고 프롬프트/파서에 대응이 들어 있다.
   - 한국어 지시만으로는 값이 영어로 나온다 → 필드별 한국어 열거값을 명시한다.
   - 채워진 예시 JSON을 주면 그대로 베낀다 → 완성 예시를 주지 않는다. 파서가 지시문 복사를 검사한다.
   - 색상 단어를 하드웨어/수납 필드에 넣는다 → `config/blog.yaml:vision_normalization.color_terms`로 걸러낸다.

## 검증 상태

`## 검증 결과` 절 참조. 갱신 시각 기준으로 유지한다.

## 다음 담당자가 먼저 읽을 문서

1. [`AGENTS.md`](AGENTS.md) — 이 저장소의 불변 원칙과 금지 사항
2. [`docs/JOB_PIPELINE.md`](docs/JOB_PIPELINE.md) — 6 스테이지 설계와 판단 근거
3. [`bench/RESULTS.md`](bench/RESULTS.md) — 실측 수치와 tier 결정 근거
4. [`RETRY_LOG.md`](RETRY_LOG.md) — 전략을 바꾼 지점과 이유
5. [`TASK_STATUS.md`](TASK_STATUS.md) — 완료 정의 체크리스트와 알려진 제약

## 재개 명령

```bash
cd ~/rpi-photo-blog-agent
source .venv-blog-agent/bin/activate
./scripts/preflight.sh
pytest -q

# CLI
./scripts/run_cli.sh new --images ~/photos/kitchen --category 부엌가구 --topic "좁은 주방 수납"
./scripts/run_cli.sh run --job <job_id>

# GUI
./scripts/run_gui.sh          # http://<pi-ip>:8770
```

사진 4장 기준 1회 완주에 약 30분이 걸린다. 실행 중 다른 모델 서버를 동시에 띄우지 않는다.
