#!/usr/bin/env bash
# 로컬 GUI 기동. config/runtime.yaml의 gui.host / gui.port를 따른다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=.venv-blog-agent/bin/python
HOST=$($PY -c "import yaml;print(yaml.safe_load(open('config/runtime.yaml'))['gui']['host'])")
PORT=$($PY -c "import yaml;print(yaml.safe_load(open('config/runtime.yaml'))['gui']['port'])")

echo "GUI: http://${HOST}:${PORT}  (같은 LAN의 다른 기기에서는 파이 IP로 접속)"
exec .venv-blog-agent/bin/uvicorn app.gui.app:app --host "$HOST" --port "$PORT" --log-level info
