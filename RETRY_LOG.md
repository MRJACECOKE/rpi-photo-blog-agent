# RETRY LOG

전략을 바꿔 다시 시도한 기록. 같은 명령을 아무 변화 없이 반복한 것은 여기에 쓰지 않는다 (그런 일은 하지 않는다).
파이프라인 실행 중 발생한 재시도는 job json의 `retries` 배열에도 기계 판독용으로 남는다.

---

## 2026-08-17 15:33 KST — Phase 0 벤치 harness

```
Attempt 1 / Stage: PHASE_0_BENCH
Failure: 벤치 스크립트가 exit 127로 즉시 종료
Root cause: /usr/bin/time 이 이 이미지에 설치돼 있지 않음. GNU time(1) 부재.
Action: 셸 + `time -v` 방식을 버리고 psutil 기반 자체 샘플러(bench/bench.py)로 교체.
        peak RSS, MemAvailable, swap, 온도를 1초 간격으로 직접 표본 추출.
Result: SUCCESS. time(1)보다 얻는 지표가 더 많아졌다 (MemAvailable 최저값, swap 사용량, 온도 곡선).
```

---

## 2026-08-17 15:41 KST — VLM 이미지 크기

```
Attempt 1 / Stage: PHASE_0_BENCH (VLM)
Failure: 이미지 1장 분석에 227.9s. 합격 기준 180s 초과.
Root cause: 1024px 입력에서 mtmd 이미지 인코딩에만 107.9s가 소요됨. 인코딩 시간이 픽셀 수에 비례.
Action: 입력 긴 변을 1024px -> 768px로 낮추고 max_tokens를 700 -> 420으로 조정.
Result: SUCCESS (159.8s). 운영 입력 크기를 768px로 확정하고 config/models.yaml에 실측값과 함께 기록.
        재시도용 해상도는 640px로 둔다.
```

---

## 2026-08-17 15:44 KST — VLM 출력 언어

```
Attempt 1 / Stage: PHASE_0_BENCH (VLM)
Failure: 한국어 프롬프트를 줬는데 JSON 값이 전부 영어로 출력됨
         ("cabinets", "kitchen", "U-shaped", "marble").
Root cause: Qwen2.5-VL-3B는 지시 준수력이 약해 한국어 지시만으로 출력 언어가 고정되지 않는다.
Action: 필드별로 허용되는 한국어 열거값을 프롬프트에 명시.
Result: 부분 성공. 언어는 한국어로 고정됐으나 새로운 실패가 나타남 (아래 항목).
```

---

## 2026-08-17 15:47 KST — VLM 예시 복사

```
Attempt 2 / Stage: PHASE_0_BENCH (VLM)
Failure: 값이 한국어로 나왔지만 프롬프트에 넣은 예시 JSON을 그대로 베낌.
         notable_points에 "40자 이내 한국어 문장"이라는 지시문 자체가 값으로 출력됨.
Root cause: 완성된 예시 JSON을 주면 3B 모델이 이미지를 보는 대신 예시를 복사한다.
Action: 완성 예시를 제거하고 키 이름 + 허용값 + 각 필드 설명만 나열하는 형태로 재작성.
        "지시문을 값으로 출력하지 마라"를 명시. 파서에도 placeholder 복사 검사를 추가.
Result: SUCCESS (159.8s). 실제 관찰에 근거한 한국어 값이 나왔다.
        남은 문제로 notable_points 중복이 있어 파서에서 중복 제거로 처리.
```

---

## 2026-08-17 15:27 KST — llama-server 부재

```
Attempt 1 / Stage: 설계 (STAGE_4 작성 방식 결정)
Failure: 섹션 단위 생성을 하려는데 llama-server 실행 파일이 빌드 산출물에 없음.
Root cause: 기존 빌드는 llama-completion / llama-mtmd-cli 타깃만 만들었다.
            libllama-server-impl.so 는 있으나 실행 파일 링크가 빠져 있었다.
Action: 다른 commit의 llama-server(/home/pi/llm/... 의 build 9986)를 쓰지 않고,
        고정된 commit 0cea362에서 `cmake --build build --target llama-server`로 링크만 추가.
        런타임 commit 일관성을 유지하는 쪽을 택했다.
Result: SUCCESS. version: 1 (0cea362)로 확인.
```

---

## 2026-08-17 15:50 KST — 얼굴 검출 오탐

```
Attempt 1 / Stage: STAGE_1_PREP (개인정보 검사)
Failure: 사람이 없는 주방 사진(terrytown-kitchen.jpg)이 PRIVACY_HOLD로 분류됨.
Root cause: YuNet이 조명 기구 장식을 얼굴로 검출 (점수 0.767, 프레임 상단 경계에 걸침).
            크롭해서 육안 확인한 결과 명백한 오검출.
Action: 단일 임계값을 버리고 2단 구조로 변경.
        0.90 이상 = 확정 보류 (VLM 결과와 무관, VLM 분석 자체를 건너뜀)
        0.75~0.90 = 잠정 보류, VLM 2차 검사가 얼굴을 부정하면 해제
        임계값을 그냥 올리는 방식은 진짜 얼굴을 놓치므로 채택하지 않았다.
Result: SUCCESS. E2E 실행에서 검증됨.
        실제 인물 사진: 규칙 기반 0.91 + VLM 확인 -> PRIVACY_HOLD 유지
        조명 기구 오탐: 규칙 기반 0.767 + VLM 부정 -> 보류 해제
```

---

## 2026-08-17 16:00 KST — 치수 금칙 정규식이 한국어에서 동작하지 않음

```
Attempt 1 / Stage: STAGE_5_QUALITY
Failure: "상부장 높이는 720mm입니다" 가 dimension_claim 금칙 패턴에 걸리지 않음.
Root cause: 패턴이 `\b\d{3,4}\s*mm\b` 였다. 뒤의 \b는 단어 경계를 요구하는데
            'm'과 '입'이 모두 word 문자라 경계가 성립하지 않는다.
            영어 문장에서만 동작하고 한국어 문장에서는 통째로 놓치는 버그였다.
Action: `\d{3,4}\s*mm(?![a-zA-Z])` 로 변경. 평수 패턴 `\d{1,2}\s*평(?![가-힣])` 도 같은 이유로 수정.
Result: SUCCESS. 회귀 테스트 test_dimension_claim_is_blocked 로 고정.
```

---

## 2026-08-17 16:05 KST — VLM 타임아웃 여유 부족

```
Attempt 1 / Stage: STAGE_2_VISION
Failure: 실행 중 관측된 이미지당 소요 시간이 150.4~174.7s인데 타임아웃이 180s.
Root cause: Phase 0 합격 기준(180s)을 그대로 실행 타임아웃으로 쓴 설계 오류.
            여유가 5~30s뿐이라 조금 복잡한 사진 하나 때문에 분석을 통째로 잃는다.
Action: 타임아웃을 300s로 분리하고, 180s는 게이트 기준으로만 유지.
        게이트 초과 시 경고 로그와 job 노트로 남기되 실행은 계속한다.
Result: SUCCESS. 두 값의 역할이 분리됐다.
```

---

## 2026-08-17 16:41 KST — 품질 지표가 한국어에서 오작동

```
Attempt 1 / Stage: STAGE_5_QUALITY
Failure: 정상적인 생성 본문이 "3-gram 자기중복률 0.352 > 0.180"으로 거부됨.
         그러나 동일 본문에 중복 문장은 0건이었다 (79문장 전부 고유).
Root cause: 문자 단위 3-gram을 썼다. 한국어는 조사와 어미("습니다", "되어", "있는")가
            문자 3-gram 반복을 지배해서 정상 글도 높은 값이 나온다.
            결정적 증거: 사람이 직접 쓴 이 저장소의 문서들이 0.200~0.338로
            자기 자신의 기준을 통과하지 못했다. 임계값이 아니라 지표가 틀렸다.
            실측 분포(문자 3-gram):
              생성 본문 0.352 · 검수 통과 샘플 0.199 · 사람이 쓴 문서 0.200~0.338
Action: 지표를 단어 3-gram으로 교체하고 임계값을 0.15로 재설정.
        단어 기준 실측 분포:
              사람이 쓴 문서 0.004 · 검수 샘플 0.026 · 생성 본문 0.029 · 의도적 반복 0.966
        → 패딩과 정상 글이 명확히 분리된다.
Result: SUCCESS. 재검사에서 중복 경보가 사라지고, 대신 진짜 결함인
        dangling_unverified 비문 1건이 잡혔다. 게이트가 의도대로 동작함을 확인.
        회귀 테스트 test_trigram_metric_does_not_flag_normal_korean_prose 로 고정.
```

## 2026-08-17 16:47 KST — 결함 있는 산출물 재생성

```
Attempt 1 / Stage: STAGE_4_WRITE
Failure: 첫 완주 산출물의 design_points 섹션에 비문이 있었다.
         "모든 요소는 통일된 톤 안에서 균형 있게 조화를 이루고 있으며, 사진만으로는 확인되지 않습니다"
Root cause: 섹션 지침이 "확인되지 않은 항목은 '사진만으로는 확인되지 않습니다'로 처리"였는데,
            모델이 이 표현을 무관한 문장 끝에 접속어미로 붙였다. 지침 문구 자체의 결함.
Action: ① 지침을 "완결된 문장 하나로 따로 쓴다. 다른 문장 끝에 덧붙이지 않는다"로 수정
        ② 금칙 패턴 dangling_unverified 추가 (생성 중 해당 섹션만 재생성시킴)
        ③ 같은 job을 재실행. PREP/VISION은 resume으로 재사용되므로 작성 단계만 다시 돈다.
        ④ 이 재실행을 네트워크 네임스페이스 분리 상태에서 수행해 오프라인 동작도 함께 검증.
Result: (진행 중)
```
