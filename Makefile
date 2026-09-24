SHELL := /bin/bash
.DEFAULT_GOAL := help
RUN := bash scripts/with-env.sh
COMPOSE := docker compose --env-file .env -f infra/compose.yaml

.PHONY: help env install setup db-up db-down migrate dev format lint typecheck check-format check test build e2e audit secrets container infra-plan infra-apply

help:
	@printf '%s\n' 'make setup     Generate local env, install locked deps, start PostgreSQL, migrate, build' 'make dev       Local Django plus compiled asset watcher at 127.0.0.1:8000' 'make format    Safe Ruff fixes and Python formatting' 'make lint      Stable Ruff correctness, Django and security rules' 'make typecheck Pyrefly over Python application, tests, scripts and infrastructure' 'make check     Lock consistency, lint, format check, Pyrefly, TypeScript, Django, migration drift' 'make test      PostgreSQL pytest suite (creates isolated test database)' 'make build     Production browser assets' 'make e2e       Playwright with dedicated E2E_DATABASE_URL' 'make audit     Python and JavaScript dependency advisories' 'make secrets   Scan full Git history with digest-pinned Gitleaks (Docker)' 'make container Build the production Django image (Docker)'
	@printf '%s\n' 'make infra-plan  Preview whole-project changes in the linked Railway environment' 'make infra-apply Review and apply infrastructure changes interactively'

env:
	python3 scripts/dev-env.py

install:
	uv sync --frozen
	pnpm install --frozen-lockfile

setup: env install
	$(MAKE) db-up
	$(MAKE) migrate
	$(MAKE) build

db-up:
	$(COMPOSE) up -d --wait postgres

db-down:
	$(COMPOSE) down

migrate:
	$(RUN) uv run --frozen python apps/platform/manage.py migrate --noinput

dev:
	$(RUN) bash scripts/dev.sh

format:
	uv run --frozen ruff check --fix .
	uv run --frozen ruff format .

lint:
	uv run --frozen ruff check .

typecheck:
	uv run --frozen pyrefly check

check-format:
	uv run --frozen ruff format --check .

check:
	uv lock --check
	$(MAKE) lint check-format typecheck
	pnpm check
	$(RUN) env DJANGO_SETTINGS_MODULE=config.settings.test uv run --frozen python apps/platform/manage.py check
	$(RUN) env DJANGO_SETTINGS_MODULE=config.settings.test uv run --frozen python apps/platform/manage.py makemigrations --check --dry-run

test:
	$(RUN) env DJANGO_SETTINGS_MODULE=config.settings.test uv run --frozen pytest

build:
	pnpm build

e2e: build
	$(RUN) pnpm e2e

audit:
	uv run --frozen pip-audit --strict
	pnpm audit --audit-level moderate

secrets:
	bash scripts/secrets.sh

container:
	docker build --file infra/Dockerfile --tag pt-ligacoes:local .

infra-plan:
	railway config plan --file .railway/railway.ts

infra-apply:
	railway config apply --file .railway/railway.ts
