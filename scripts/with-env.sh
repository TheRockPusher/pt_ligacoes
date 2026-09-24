#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# Only developer commands call this wrapper; production never sources a file.
if [[ "${CI:-}" != "true" && -f .env ]]; then
  if [[ -L .env ]]; then
    printf '%s\n' 'Refusing a symlink at .env.' >&2
    exit 1
  fi
  set -a
  source .env
  set +a
fi
exec "$@"
