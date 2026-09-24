#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
: "${E2E_DATABASE_URL:?Set E2E_DATABASE_URL to a dedicated PostgreSQL database ending _e2e}"
export DATABASE_URL="$E2E_DATABASE_URL"
export DJANGO_SETTINGS_MODULE=config.settings.test
export PYTHONPATH=apps/platform
uv run --frozen python -c 'import os; from urllib.parse import urlsplit; url = urlsplit(os.environ["DATABASE_URL"]); assert url.scheme in ("postgres", "postgresql") and url.path.endswith("_e2e"), "E2E requires a dedicated PostgreSQL database ending _e2e"'
uv run --frozen python apps/platform/manage.py migrate --noinput
uv run --frozen python tests/e2e/seed.py
exec uv run --frozen python apps/platform/manage.py runserver 127.0.0.1:8000 --insecure --noreload
