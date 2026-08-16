# Source manifest와 공개 경계

## 저장소에 포함하는 자체 소스

- `app/*.py`: orchestration, runner, parser, memory guard, model discovery, CLI
- `scripts/*.sh`, `scripts/*.py`: 설치, 빌드, 진단, 모델 다운로드, 실행, retry
- `tests/*.py`: unit 및 fake-process integration tests
- `config/*.yaml`: prompt와 logging 계약
- `systemd/*.service`: 선택적 user service
- `Makefile`, `pytest.ini`, `requirements.txt`, `.env.example`
- `docs/`, `HANDOFF.md`, `README.md`: 설계, incident, evidence, 게시 절차
- `inputs/example.jpg`: 합성 테스트 이미지
- `fixtures/smoke/`: 실제 성공 smoke 입력과 CC BY 2.0 attribution metadata
- `examples/smoke-success.md`: 검수된 smoke 출력 샘플

## 저장소에서 의도적으로 제외하는 항목

| 패턴 | 이유 | 재현 방법 |
|---|---|---|
| `.env` | 장비별 설정·경로 | `.env.example` 복사 |
| `.venv-blog-agent/` | platform별 binary | `scripts/bootstrap.sh` |
| `models/**/*.gguf*` | 약 13 GiB, 모델별 라이선스 | `scripts/download_models.py` |
| `third_party/llama.cpp/*` | upstream 전체와 build 약 314 MiB | `scripts/build_llama_cpp.sh` + `.llama-cpp-version` |
| `runs/*` | raw prompt, 사진, 절대 경로, 로그 | 로컬 실행 시 생성 |
| `outputs/*` | 운영 산출물 | 승인 샘플만 `examples/`로 복제 |
| `logs/*.log` | 로컬 운영 로그 | 실행 시 생성 |
| `inputs/downloaded-test.jpg` | 외부에서 받은 smoke 입력, 공개 권리 미확인 | 사용자가 자신의 이미지 제공 |

이 제외는 “소스 누락”이 아니다. 자체 구현 소스는 전부 포함하고, 생성물·외부 dependency·모델 weight·비공개 입력만 재현 스크립트 또는 명시적 evidence로 대체한다.

## 외부 dependency pin

- llama.cpp commit: `.llama-cpp-version`
- Python package: `requirements.txt`
- 모델 repository와 quantization: `.env.example`

모델 weight는 Git LFS에도 자동 업로드하지 않는다. 각 모델 카드의 라이선스를 별도로 확인해야 한다.
