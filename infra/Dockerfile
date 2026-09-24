# syntax=docker/dockerfile:1
FROM node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS assets
WORKDIR /app
RUN npm install --global pnpm@12.6.0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY vite.config.ts tsconfig.json ./
COPY frontend/src ./frontend/src
COPY frontend/public ./frontend/public
COPY apps/platform/templates ./apps/platform/templates
RUN pnpm build

FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS dependencies
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
RUN python -m pip install --no-cache-dir uv==0.12.18
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/apps/platform \
    PATH=/app/.venv/bin:$PATH \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    PORT=8000
WORKDIR /app
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && chmod 0700 /home/app
COPY --from=dependencies /app/.venv /app/.venv
COPY apps/platform ./apps/platform
COPY --from=assets /app/frontend/dist ./frontend/dist
COPY infra/start.sh infra/healthcheck.py infra/gunicorn.conf.py ./infra/
RUN mkdir -p /app/apps/platform/staticfiles && chown app:app /app/apps/platform/staticfiles
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "/app/infra/healthcheck.py"]
CMD ["sh", "/app/infra/start.sh"]
