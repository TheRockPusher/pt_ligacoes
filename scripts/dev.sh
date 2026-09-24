#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export DJANGO_SETTINGS_MODULE=config.settings.development
pnpm build
pnpm dev &
assets_pid=$!
uv run --frozen python apps/platform/manage.py runserver 127.0.0.1:8000 --noreload &
server_pid=$!
cleanup() {
  kill "$assets_pid" "$server_pid" 2>/dev/null || true
  wait "$assets_pid" "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
wait -n "$assets_pid" "$server_pid"
