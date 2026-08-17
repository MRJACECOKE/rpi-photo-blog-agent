# WORK LOG

시각은 모두 KST다. 최신 항목이 아래로 간다.

---

## 2026-08-17 — 사진 폴더 → 블로그 `.txt` 경로 추가

### 15:16 조사

기존 저장소 조사 완료. `/home/pi`에 관련 프로젝트가 6개 있었다.

- `/home/pi/blog_agent` — 웹 리서치 + GitHub Pages 게시 경로. 이번 스코프와 다르다 (PUSH가 `BLOCKED_EXTERNAL` 상태).
- `/home/pi/rpi-photo-blog-agent` — **사진 → VLM → LLM 로컬 파이프라인.** 이번 스코프에 가장 가깝다.
  llama.cpp가 commit `0cea362`로 빌드돼 있고, VLM/LLM GGUF가 내려받혀 있으며, 메모리 안전 상태머신과 33개 회귀 테스트가 있다.

**결정: `rpi-photo-blog-agent`를 재사용하고 job 계층만 새로 얹는다.** 재작성하지 않는다.
`memory_guard.py`, `process_runner.py`, `model_discovery.py`, `llama_options.py`, `metrics.py`를 그대로 쓴다.

하드웨어 실측: RAM 16,214 MiB / available 14,900 MiB, zram 2 GiB (SD 스왑 없음), **NVMe 없음 (microSD 단독, 여유 22 GiB)**,
온도 49.4 °C, `throttled=0x0`.

### 15:20–15:45 Phase 0 실측

`bench/bench.py`를 만들었다. 이 이미지에는 GNU `time -v`가 없어 psutil 기반 자체 샘플러로 대체했다.

- **LLM Qwen3-30B-A3B UD-IQ2_M**: gen **4.79 tok/s** (게이트 1.5의 3.2배), prompt eval 8.57 tok/s,
  cold load 137.8 s / warm load 1.85 s, peak RSS 10,726 MB, swap 0, 최고 61.1 °C → **PASS**
- **VLM Qwen2.5-VL-3B Q4_K_M @1024 px**: 227.9 s → 180 s 게이트 **초과**. 이미지 인코딩만 107.9 s.
- **VLM @768 px**: 159.8 s → **PASS**. 운영 입력 크기를 768 px로 확정했다.

VLM 프롬프트 실패 2건을 관측하고 고쳤다.
1. 한국어 지시만으로는 값이 영어로 나왔다 → 필드별 한국어 열거값을 명시했다.
2. 채워진 예시 JSON을 주니 그대로 베꼈다("40자 이내 한국어 문장"이 값으로 출력됨) → 완성 예시를 없애고 키와 허용값만 나열했다.

전체 수치와 해석은 `bench/RESULTS.md`에 있다.

### 15:27 llama-server 링크

실측 결과 프롬프트 처리가 병목이라 섹션 단위 생성에는 프롬프트 캐시가 필수라고 판단했다.
빌드 산출물에 `libllama-server-impl.so`는 있는데 `llama-server` 실행 파일이 없었다.
**같은 commit에서** `cmake --build build --target llama-server`로 링크만 추가했다. 다른 것은 재빌드하지 않았다.

### 15:30–16:00 구현

`config/models.yaml`, `config/blog.yaml`, `config/runtime.yaml` 신설. 값은 전부 외부화했다.
`config/prompts.yaml`에 `furniture_*` 프롬프트 4종을 추가했다.

신규 모듈: `settings.py`, `job_state.py`, `image_prep.py`, `privacy.py`, `runtime.py`,
`vision_stage.py`, `writer_stage.py`, `quality_stage.py`, `output_stage.py`, `pipeline.py`, `cli_jobs.py`, `gui/`.

의존성 추가: fastapi, uvicorn, jinja2, python-multipart, pillow-heif, opencv-python-headless.
얼굴 검출은 YuNet ONNX(232 KB)를 `models/privacy/`에 받았다. **OpenCV 5.x가 Haar cascade를 제거했기 때문**이다.

### 15:50 개인정보 오검출 발견과 대응

주방 사진(`terrytown-kitchen.jpg`)의 **조명 기구 장식**이 얼굴로 검출됐다 (점수 0.767).
크롭해서 눈으로 확인한 결과 명백한 오검출이었다.

단일 임계값으로는 해결되지 않는다. 올리면 진짜 얼굴을 놓치고, 내리면 멀쩡한 사진이 영구 제외된다.
**2단 구조로 바꿨다.** 0.90 이상은 확정 보류(VLM 결과와 무관, VLM 분석 자체를 건너뜀),
0.75~0.90은 잠정 보류로 두고 VLM 2차 검사가 얼굴을 부정하면 해제한다.
실제 인물 사진은 0.91로 확정 보류됐고, 조명 기구는 0.767로 잠정 보류가 됐다.

### 15:55 llama-server 작성 경로 단독 검증

서버 기동 46 s, 섹션 3개 생성 성공 (title 114.7 s / overview 59.9 s / fit_space 81.6 s).
출력이 vision JSON 범위 안에 머물렀고 브랜드·가격을 지어내지 않았다. 언로드 게이트 통과.

### 16:00 품질 게이트 정규식 버그 수정

테스트에서 `720mm입니다`가 치수 금칙에 걸리지 않았다.
`\b\d{3,4}\s*mm\b`의 뒤쪽 `\b`가 한글 앞에서 성립하지 않기 때문이다 (`m`도 `입`도 word 문자).
`(?![a-zA-Z])` 부정 전방탐색으로 바꿨다. 평수 패턴도 같은 이유로 함께 고쳤다.
**실제 운영에서 한국어 문장 속 치수 주장을 통째로 놓치는 버그였다.**

### 16:05 VLM 타임아웃과 게이트 분리

실측 평균 167 s인데 타임아웃이 180 s면 여유가 13 s뿐이다.
사진 하나가 조금 복잡하다는 이유로 분석을 통째로 잃는다.
**타임아웃을 300 s로 올리고, 180 s는 Phase 0 합격 기준으로만 유지**한다. 초과하면 경고 로그와 job 노트로 남긴다.

### 16:05 GUI 상태 표시 수정

`/api/system`이 GUI 자신의 실행만 보고 있어서, CLI가 파이프라인을 돌리는 중에도 `busy: false`를 반환했다.
`state/run.lock`의 PID 생존을 함께 확인하도록 고쳤다. 장비의 실제 상태를 보고해야 하기 때문이다.

### 16:18 하드코딩 잔재 제거

`config/runtime.yaml`에 `thermal.abort_above_celsius`와 `memory.max_swap_used_percent`를 정의해 두고도
`memory_guard.py`가 82.0 / 85.0을 하드코딩하고 있었다. 설정이 읽히지 않는 죽은 값이었다.
`MemoryGuard.__init__`에 기본값 있는 인자로 넣고 파이프라인이 설정값을 넘기도록 고쳤다.
기존 경로(`app/cli.py`)는 기본값이 예전과 같아 동작이 바뀌지 않는다.

같은 맥락으로 `timeouts.llm_total_sec`도 선언만 되고 쓰이지 않았다.
작성 단계 총 예산으로 실제로 적용했다. 초과하면 남은 섹션을 중단하되 만든 것은 버리지 않고,
`FAILED_RECOVERABLE`로 남겨 품질 게이트가 섹션 부족을 잡도록 했다.

### 16:19 VLM 필드 오염과 비문 대응

E2E 실행 중간 산출물을 검토하다 두 가지를 발견했다.

1. VLM이 색상 단어를 하드웨어/수납 필드에 넣었다 (`hardware_visible: ["화이트", "메탈 핸들"]`).
   색상은 `color_tone`에 이미 있으므로, 색상 단어만으로 된 항목을 버리는 규칙을 넣었다.
   목록은 `config/blog.yaml:vision_normalization.color_terms`에 있다.
   "화이트 슬림 바 손잡이"처럼 색상이 포함된 복합어는 유지한다.

2. LLM이 "사진만으로는 확인되지 않습니다"를 무관한 문장 끝에 접속어미로 붙여 비문을 만들었다.
   ("모든 요소는 통일된 톤 안에서 균형 있게 조화를 이루고 있으며, 사진만으로는 확인되지 않습니다")
   프롬프트와 섹션 지침을 "완결된 문장 하나로 따로 쓴다"로 고치고,
   금칙 패턴 `dangling_unverified`를 추가해 이 형태가 나오면 해당 섹션을 재생성한다.

두 수정 모두 회귀 테스트로 고정했다. 이 수정은 진행 중이던 E2E 실행에는 적용되지 않는다
(설정은 프로세스 시작 시 읽힌다). 이후 실행부터 반영된다.
