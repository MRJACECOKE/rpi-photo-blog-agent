# Phase 0 — 하드웨어/모델 실측 결과

측정 일시: 2026-08-17 KST
장비: Raspberry Pi 5 Model B Rev 1.1 / 16 GB (MemTotal 16,214 MiB) / aarch64 / Debian, kernel 6.18.34-rpt-rpi-2712
저장장치: **microSD (`/dev/mmcblk0`, 118.7 GiB, 사용률 81%, 여유 22 GiB)** — NVMe 없음
스왑: zram0 2 GiB (`/dev/zram0`, priority 100). SD 카드 스왑은 사용하지 않음.
CPU: 4 코어. 측정 시작 시 `vcgencmd get_throttled` = `0x0`, 온도 49.4 °C.
런타임: llama.cpp commit `0cea36222fe9bac5ebfc45716c9eef11f37046c4` (`.llama-cpp-version` 고정), GCC 14.2.0.
측정 도구: `bench/bench.py` (GNU `time -v`가 이 이미지에 없어 psutil 기반 자체 샘플러로 대체).

## 합격 기준 (Gate)

| 기준 | 임계값 | 결과 |
|---|---|---|
| 모델 로드 성공, OOM 없음 | 필수 | PASS |
| 생성 속도 | >= 1.5 tok/s | PASS (벤치 4.79 / **실운영 2.38~3.83**) |
| swap 폭주 없음 | 지속 증가 없음 | PASS (swap 사용 0 MB) |
| VLM 이미지 1장 분석 | <= 180 s | PASS (159.8 s @ 768 px) |

## LLM 실측

| model | quant | ctx | 로드시간(s) | peak RSS(MB) | prompt tok/s | gen tok/s | swap 사용 | 최고온도 | 판정 |
|---|---|---|---|---|---|---|---|---|---|
| Qwen3-30B-A3B-Instruct-2507 (MoE, 활성 3B) | UD-IQ2_M (10.10 GiB) | 4096 | 137.84 (cold) | 10,726 | 8.57 | **4.79** | 0 MB | 61.1 °C | **PASS** |
| 같은 모델 (page cache warm) | UD-IQ2_M | 4096 | **1.85** (warm) | 10,728 | 8.73 | 4.81 | 0 MB | 61.7 °C | PASS |

> **주의: 이 표의 4.79 tok/s는 격리 벤치 값이다.** 프롬프트가 71토큰으로 짧고 ctx가 4096일 때의 수치다.
> 실제 파이프라인에서는 컨텍스트가 누적돼 **2.4~3.8 tok/s**로 떨어진다. 아래 "실운영 실측" 절을 볼 것.
> tier 결정에는 문제가 없으나(게이트 1.5 tok/s), 여유 폭을 이 표만 보고 판단하면 안 된다.

원본 기록: `bench/raw/tierA-iq2m-c4096/record.json`, `bench/raw/tierA-iq2m-warm/record.json`

### 해석

- **peak RSS 10.7 GB는 물리 점유가 아니다.** llama.cpp가 GGUF를 mmap하므로 대부분이 파일 백업 페이지이고,
  실행 중 `MemAvailable`은 14,287 MiB 아래로 내려가지 않았다. 이것이 16 GB에서 10 GiB급 모델이 도는 이유다.
- **cold load 137.8 s vs warm load 1.85 s.** 차이는 전부 microSD에서 10.1 GiB를 읽는 시간이다.
  한 번 읽히면 page cache에 남아 재적재가 사실상 공짜다.
- **prompt eval 8.57 tok/s가 진짜 병목이다.** 1,200 토큰 프롬프트를 매번 다시 처리하면 섹션당 약 140 s가 든다.
  섹션 12개를 one-shot CLI로 생성하면 프리필만 28분이다.
  → **결론: 작성 단계는 `llama-server`로 모델을 한 번만 올리고 KV 캐시를 재사용한다.**
  `llama-server` 실행 파일이 빌드 산출물에 없어(공유 라이브러리 `libllama-server-impl.so`만 존재)
  동일 commit에서 `cmake --build build --target llama-server`로 링크만 추가 수행했다.

## 실운영 실측 (벤치와 다르다 — 이쪽이 실제 수치다)

위 LLM 표는 **격리 벤치 조건**(ctx 4096, 프롬프트 71토큰)의 값이다.
실제 파이프라인은 ctx 8192에 컨텍스트가 누적되므로 처리량이 더 낮다.
아래는 job `20260817-e2e-kitchen` 실행 중 `llama-server`가 직접 기록한 값이다.

| 섹션 | 생성 토큰 | prompt eval (tok/s) | **생성 (tok/s)** | 섹션 소요(s) |
|---|---|---|---|---|
| title (기저 프롬프트 1,342토큰 프리필 포함) | 28 | 7.12 | **3.83** | 195.8 |
| overview | 140 | — | — | 69.0 |
| fit_space | 187 | 5.37 | **3.26** | 86.3 |
| design_points | 195 | 5.01 | **3.03** | 98.2 |
| material_hardware | 275 | 4.49 | **2.70** | 137.6 |
| storage_design | 240 | 3.93 | **2.52** | 129.4 |
| pros | 157 | 3.53 | **2.45** | 102.1 |
| cons | 193 | 3.31 | **2.38** | 122.3 |

### 두 가지 중요한 사실

**1. 프롬프트 캐시가 실제로 동작한다.**
기저 프롬프트가 1,342토큰인데, 두 번째 섹션부터 새로 처리하는 토큰은 **134~169개뿐**이다.
캐시가 없었다면 섹션마다 1,342토큰 + 누적분을 다시 처리해야 했다.
로그의 `selected slot by LCP similarity, sim_best = 0.907`가 공통 프리픽스 재사용을 보여 준다.

**2. 처리량이 컨텍스트 누적에 따라 단조 감소한다.**
생성 속도가 3.83 → 3.26 → 3.03 → 2.70 → 2.52 → 2.45 → **2.38 tok/s**로 계속 떨어진다.
KV 캐시가 커지면서 attention 비용이 늘기 때문이다. prompt eval도 7.12 → 3.31로 같은 경향이다.

이것이 운영상 의미하는 바:

- 게이트(1.5 tok/s)는 통과하지만 **여유가 벤치 수치가 시사하는 것보다 훨씬 작다.**
  벤치만 보면 3.2배 여유로 보이지만, 마지막 섹션 기준 실제 여유는 약 1.6배다.
- **섹션을 더 늘리거나 분량 상한을 올리면 게이트에 닿을 수 있다.**
  `config/blog.yaml:sections`를 늘릴 때는 이 감쇠를 감안해야 한다.
- 그래서 `config/runtime.yaml:timeouts.llm_total_sec`(작성 단계 총 예산)를 실제로 적용해 두었다.
  예산을 넘기면 남은 섹션을 중단하고 `FAILED_RECOVERABLE`로 남긴다. 무한정 느려지게 두지 않는다.

## VLM 실측

| model | quant | 입력 크기 | 이미지 인코딩(s) | 전체(s) | peak RSS(MB) | min MemAvailable(MB) | 최고온도 | 출력 품질 | 판정 |
|---|---|---|---|---|---|---|---|---|---|
| Qwen2.5-VL-3B-Instruct | Q4_K_M + mmproj Q8_0 | 1024 px | 107.9 | 227.9 | 5,213 | 11,677 | 68.8 °C | 값이 **영어**로 출력됨 | **FAIL (시간 초과 + 언어)** |
| 같은 모델, 한국어 예시 프롬프트 | 동일 | 768 px | — | 132.2 | 5,034 | 11,845 | 68.8 °C | 예시 JSON을 **그대로 복사** | **FAIL (placeholder 복사)** |
| 같은 모델, 열거형 프롬프트(최종) | 동일 | 768 px | — | **159.8** | 5,038 | 11,764 | 68.8 °C | 실제 관찰 기반 한국어 | **PASS** |

원본 기록: `bench/raw/vlm-qwen25vl3b-1024/`, `bench/raw/vlm-3b-768-ko/`, `bench/raw/vlm-3b-768-ko2/`

### 해석

- 이미지 인코딩 시간이 픽셀 수에 비례한다. 1024 px에서 107.9 s가 걸려 180 s 게이트를 넘겼다.
  768 px로 내리면 전체가 159.8 s로 들어온다. → **운영 입력 크기를 1024 px가 아닌 768 px로 확정**한다.
  실패 시 재시도 해상도는 640 px다.
- Qwen2.5-VL-3B는 지시 준수력이 약하다. 두 번의 실패에서 확인한 것:
  1. 한국어 프롬프트만으로는 값이 영어로 나온다 → 필드별 **한국어 열거형 값을 명시**해야 한다.
  2. 채워진 예시 JSON을 주면 그 예시를 그대로 베낀다 → **완성된 예시를 주면 안 되고**, 키와 허용값만 나열해야 한다.
  이 두 가지는 `config/prompts.yaml`의 `furniture_vision_system`에 반영했고,
  파서에서 placeholder 복사와 중복 항목을 별도로 검사한다.
- 최종 프롬프트 출력에서도 `notable_points`에 중복 문장이 나왔다. 파서에서 중복 제거한다.

## 모델 tier 확정과 근거

```
LLM  확정: Qwen3-30B-A3B-Instruct-2507 UD-IQ2_M (MoE 총 30B / 활성 3B), llama-server
     근거: 벤치 4.79 tok/s, 실운영 2.38~3.83 tok/s. 최악 구간에서도 게이트(1.5)의 약 1.6배.
           OOM·swap 없음. MemAvailable 14 GB 유지.
VLM  확정: Qwen2.5-VL-3B-Instruct Q4_K_M + mmproj Q8_0, 입력 768 px
     근거: 768 px에서 159.8 s로 180 s 게이트 통과. 1024 px는 227.9 s로 미달이라 입력 크기를 낮췄다.
```

### 지시서의 tier 사다리와 다른 점, 그리고 이유

- 지시서 Tier A는 dense 30B Q4_K_M(약 18 GiB)를 상정했다. **16 GB에서 적재 불가라 사다리에서 제외**했다.
  대신 같은 30B급이지만 MoE 구조라 활성 파라미터가 3B인 `Qwen3-30B-A3B`를 IQ2_M(10.1 GiB)로 쓴다.
  이 경로는 이전 세션에서 Ollama Q4(17.28 GiB)가 OOM으로 죽은 뒤 확정된 것이며, 그 이력은 `RETRY_LOG.md`에 있다.
- 지시서 VLM Tier A는 Qwen2.5-VL-**7B**였다. 장비에 내려받힌 것은 **3B**이고,
  3B가 게이트를 통과했으므로 7B(약 5~6 GiB 추가 다운로드)를 받지 않았다.
  남은 디스크가 22 GiB뿐이고 3B가 기준을 충족하므로 근거 있는 하향으로 기록한다.
  7B로 올릴 경우 인코딩 시간이 3B의 1024 px 사례처럼 180 s를 넘길 위험이 크다.

## 환경 사전 조치 확인

- `swapon --show`: zram0 2 GiB만 사용. **SD 카드 스왑 없음** (수명/속도 이유로 추가하지 않음).
- `/sys/block/mmcblk0/queue/rotational` = 0 (비회전). NVMe는 장착되어 있지 않다.
- 측정 전후 `vcgencmd get_throttled` = `throttled=0x0`. 스로틀링 관측되지 않음.
- 전 측정 구간 최고 온도 68.8 °C (VLM). 스로틀 임계보다 충분히 낮다.

## 디스크 여유에 대한 경고

`/`가 81% 사용 중이고 여유는 22 GiB다. 모델을 추가로 내려받지 않는 것이 이번 tier 결정의 제약 조건 중 하나다.
`work/` 캐시가 쌓이면 GUI에서 정리 대상을 안내하되 자동 삭제하지 않는다.
