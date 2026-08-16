# GitHub 게시 절차

## 게시 전 확인

```bash
cd ~/rpi-photo-blog-agent
git status --short
git ls-files | sort
git check-ignore -v .env models/llm/*.gguf runs/agent.lock outputs/smoke-success.md
git grep -nEi 'api[_-]?key|secret|password|token|github_pat_|ghp_' -- ':!docs/GITHUB_PUBLISH.md'
pytest -q
```

다음이 commit 대상에 없어야 한다.

- `.env`
- GGUF와 mmproj
- `third_party/llama.cpp` checkout/build
- `.venv-blog-agent`
- `runs/`, `outputs/`, `logs/`의 운영 데이터
- 권리가 확인되지 않은 입력 사진

## GitHub 저장소 연결

GitHub에서 빈 저장소를 만든 뒤 아래 명령을 실행한다. `<OWNER>`를 실제 계정 또는 organization으로 바꾼다.

```bash
git remote add origin git@github.com:<OWNER>/rpi-photo-blog-agent.git
git push -u origin main
```

HTTPS를 쓰는 경우:

```bash
git remote add origin https://github.com/<OWNER>/rpi-photo-blog-agent.git
git push -u origin main
```

현재 패키지는 원격 저장소 주소나 GitHub 인증을 임의로 가정하지 않는다. push 전에 repository visibility와 라이선스를 소유자가 결정해야 한다.

## 공개 후 확인

1. README의 SVG 3개가 GitHub에서 렌더링되는지 확인한다.
2. Actions 또는 ARM64 runner에서 unit test를 실행한다.
3. release note에 검증 hardware, 모델 quantization, `llama.cpp` commit을 기록한다.
4. model weight가 repository 또는 release asset에 올라가지 않았는지 확인한다.
5. 실사진·raw run을 issue에 첨부하지 않도록 운영 지침을 공유한다.
