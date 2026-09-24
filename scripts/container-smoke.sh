#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
: "${DATABASE_URL:?Set DATABASE_URL to disposable PostgreSQL before container smoke}"
: "${SECRET_KEY:?Set a random SECRET_KEY before container smoke}"
image=pt-ligacoes:local
name="pt-ligacoes-smoke-${GITHUB_RUN_ID:-$$}"
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Linux CI only. No local .env or production URL is loaded by this script.
docker run --rm --network host \
  -e DATABASE_URL -e SECRET_KEY -e ALLOWED_HOSTS=smoke.example.invalid \
  "$image" timeout --kill-after=5s 300s python apps/platform/manage.py migrate --noinput
docker run --detach --name "$name" --network host \
  -e DATABASE_URL -e SECRET_KEY -e ALLOWED_HOSTS=smoke.example.invalid \
  -e ENABLE_ADMIN=false -e PORT=8001 "$image"
for attempt in {1..45}; do
  if docker exec "$name" python infra/healthcheck.py \
    && docker exec "$name" gunicornc -c "show stats" --json; then
    curl --fail --silent --show-error --output /dev/null \
      --header 'Host: smoke.example.invalid' \
      --header 'X-Forwarded-Proto: https' http://127.0.0.1:8001/
    favicon_path="$(docker exec "$name" python -c 'import django; django.setup(); from django.contrib.staticfiles.storage import staticfiles_storage; print(staticfiles_storage.url("favicon.svg"))')"
    curl --fail --silent --show-error --output /dev/null \
      --header 'Host: smoke.example.invalid' \
      --header 'X-Forwarded-Proto: https' "http://127.0.0.1:8001${favicon_path}"
    printf '%s\n' 'Production image served the homepage, fingerprinted favicon, database readiness and private Gunicorn control socket.'
    exit 0
  fi
  sleep 2
done
docker logs "$name"
printf '%s\n' 'Production container did not become healthy.' >&2
exit 1
