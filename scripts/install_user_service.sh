#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"

sed "s|%h/rpi-photo-blog-agent|$ROOT|g" "$ROOT/systemd/rpi-photo-blog-agent.service" > "$SERVICE_DIR/rpi-photo-blog-agent.service"
systemctl --user daemon-reload
echo "PASS: user service installed: $SERVICE_DIR/rpi-photo-blog-agent.service"
echo "INFO: 실행 인자는 .env의 AGENT_ARGS 또는 systemctl --user start rpi-photo-blog-agent.service로 관리하세요."
