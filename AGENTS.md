# Repository Guidelines

## Product and Evidence

Citizen-readable Portuguese political profiles, backed by documented company, organisational, education and publicly relevant family relationships. A connection does not establish wrongdoing. Shared universities do not imply acquaintance; missing records do not prove no relationship exists.

The application has an editorial domain, public directory/profiles/evidence/date-filtered graph, migrations, tests and deployment configuration. It does not yet collect sources, reconcile identities automatically or contain production political data. Never merge people solely by name or publish ambiguous matches automatically.

Keep source publication/retrieval dates separate from relationship dates. Preserve unknown dates and evidence excerpts. Source documents, production records, private notes and credentials do not belong in Git or public fixtures.

## Architecture and Ownership

One synchronous Django monolith backed by PostgreSQL. Django templates and HTMX serve pages; Vite builds TypeScript/Cytoscape/Tailwind assets, served by WhiteNoise in production. No separate frontend server or shared package layer.

| Path | Responsibility |
| --- | --- |
| `apps/platform/config/` | Settings, middleware, routing and WSGI |
| `apps/platform/ligacoes/core/` | Models, migrations, editorial permissions/publication and admin |
| `apps/platform/ligacoes/public/` | Public selectors, views, asset loading and template tags |
| `apps/platform/templates/` | Accessible Portuguese server-rendered interface |
| `frontend/src/`, `frontend/public/` | Browser interactions, styles and self-hosted public assets |
| `tests/`, `tests/e2e/` | PostgreSQL behavioral/security regressions and Playwright journeys |
| `infra/`, `scripts/`, `.railway/railway.ts` | Containers, local helpers and whole-project Railway IaC |
| `.github/workflows/`, `docs/` | CI/releases and architecture/methodology/operations |

Domain rules belong outside views/templates/graph widgets. Public selectors are the shared visibility authority; browser state must never determine publication.

## Tooling and Commands

Use **uv only for project Python dependencies**, **Ruff for Python formatting/lint**, **Pyrefly for Python typing**, and **pnpm for browser and Railway IaC tooling**. Versions and locks are authoritative in `pyproject.toml`, `uv.lock`, `package.json`, `pnpm-lock.yaml`, `.python-version` and `.node-version`. Do not introduce mypy, Black, standalone isort, another package manager, or a monorepo build framework.

Run commands from the repository root:

- `make setup`: generate a private local environment, install locked dependencies, start Docker PostgreSQL, migrate and build.
- `make install`, `make db-up`, `make migrate`: individual setup steps.
- `make dev`: Django and asset watcher on `127.0.0.1:8000`.
- `make format`: safe Ruff fixes and formatting; never enable unsafe fixes indiscriminately.
- `make lint`, `make check-format`, `make typecheck`: Ruff lint, Ruff format verification and Pyrefly.
- `make check`: lock consistency, those Python gates, TypeScript, Django checks and migration drift.
- `make test`: PostgreSQL pytest suite; no SQLite substitute.
- `bash scripts/with-env.sh uv run --frozen pytest tests/test_publication_concurrency.py`: focused regression.
- `make build`: production browser assets.
- `pnpm exec playwright install --with-deps chromium`, then `make e2e`: isolated desktop/mobile browser journeys.
- `make audit`, `make secrets`, `make container`: dependency advisories, full-history Gitleaks and production image build. The last two require Docker.

Local helpers source the trusted `.env` only outside CI; never print it. Tests need disposable PostgreSQL credentials with `CREATEDB`; E2E additionally requires a separate database ending `_e2e`. Never use production database credentials for either.

## Publication and Security Invariants

- `publish_relationship` authorizes a freshly loaded reviewer and publishes persisted, validated facts with public endpoints and supporting public evidence/source.
- Supported editorial writes share `editorial_transaction`, a PostgreSQL transaction-scoped advisory lock acquired before row locks. Preserve this ordering. Public reads do not acquire it.
- Content edits invalidate approval and retain audit events. Evidence deletion must use its current persisted parent, not an old instance's relationship.
- Do not mutate editorial content via bulk updates/deletes or raw SQL: those bypass model review hooks. Guarded internal publication writes are deliberate exceptions.
- Production settings fail closed; admin is disabled by default. Never seed an account or synthetic records into production.
- Escape text, validate external links, and preserve restrictive script CSP/HTMX settings. There is no source URL fetcher; adding one requires a separate SSRF design.
- Keep queries/results bounded and unknown dates explicit. Do not turn source failures into an empty-success result.

## Workflow and Verification

Use short branches and squash merges. Coordinate schemas, migrations and lockfiles; do not commit, publish releases or change deployments without a user request. CI and deployment configuration are not proof of a successful remote run: inspect the actual result and deployed SHA.

The live `main` protection requires both `ci` and `CodeQL`, with up-to-date PRs and no administrator/App bypass. Release Please uses a private repository-scoped GitHub App (`RELEASE_APP_CLIENT_ID` variable, `RELEASE_APP_PRIVATE_KEY` secret); its events start normal PR checks. Only App-authored, metadata-only release PRs may enter protected auto-merge after `scripts/release_guard.py` validates immutable versions/diff. Never check out PR code with this credential, add a personal-token fallback, or automate Dependabot merges through this workflow. App registration/installation is an account prerequisite, and actual unattended publication/deployment must be observed before claiming activation. See `docs/operations.md`, including the pinned TOML updater's tagged-scalar selector.

Store the App PEM only in the GitHub `release` environment secret, whose deployment branch policy permits exactly the `main` branch (no tags); never copy it to repository-wide secrets available to same-repository PR workflows. This environment has no manual approval gate. The Client ID is a nonsecret repository Actions variable.

Railway has one whole-project `.railway/railway.ts`, using the pinned `railway/iac` SDK and CLI. Do not add partial ownership or legacy per-service JSON/TOML configuration. Follow the reviewed manual plan/apply procedure in `docs/operations.md`; an application deploy does not evaluate IaC. Keep secrets in private Railway variables and use `preserve()` only for existing values. Omission can delete resources; preserve the imported Postgres variables and volume. Never blanket-confirm destructive plans. Production intent is PostgreSQL 18, one replica per service in Amsterdam/EU West, private database networking and a restricted application role; local/CI stays PostgreSQL 17. GitHub integration plus Wait for CI deploys `main`; do not add a privileged IaC workflow, Railway GitHub secret or deploy PAT.

Test consumer-visible permissions, privacy, evidence provenance, invalidation/concurrency, dates and errors with deterministic fictional fixtures. Exercise the actual profile → graph → evidence → date-filter interface, including keyboard/mobile and empty/error states. Observe visual output; passing unit tests alone is insufficient. No arbitrary coverage percentage is required.

Preserve local project-scoped Railway/agent configuration under `.agents/`, `.claude/`, `.omp/` and `skills-lock.json`; it is ignored, not public project source. Do not replace it with user-wide agent setup. Development runs on the VPS; production belongs on Railway. Read `docs/operations.md` before touching deployment, release automation or database recovery.
