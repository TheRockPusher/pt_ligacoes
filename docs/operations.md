# Operations

Operator procedures the code cannot express. Editorial rules: [methodology](methodology.md); security boundaries: [SECURITY.md](../SECURITY.md); desired infrastructure: [`.railway/railway.ts`](../.railway/railway.ts).

## Production baseline

- `web.DATABASE_URL` is a private URL for a restricted application role. Never give either app process the Postgres superuser URL (`Postgres.DATABASE_URL`).
- Secrets live only in Railway, never in Git, logs, PRs or a copied `.env`.
- `ALLOWED_HOSTS` lists the exact public hosts plus `healthcheck.railway.app` (for Railway's probe; not a CSRF origin). `CSRF_TRUSTED_ORIGINS` lists exact HTTPS origins.
- Keep PostgreSQL private and Gunicorn reachable only through Railway's ingress, whose forwarded protocol header Django trusts.
- Establish backups, a rehearsed restore and retention before any real data.
- Never seed production with fixtures or auto-created superusers. Keep admin off until access is restricted to operators.

## Infrastructure

`.railway/railway.ts` is authoritative for the **whole** Railway project: omitting a resource can delete it.

- Use the locked SDK and Railway CLI **5.62.1** (known to work with it), not the latest CLI.
- Link the CLI to project `pt-ligacoes`, environment `production`, then `make infra-plan` and review the whole plan. Evaluating the file runs local code with your credentials: never plan an untrusted branch.
- `make infra-apply` only with explicit maintainer authorisation; it re-plans, so review again. Never force a destructive plan with `--yes`/`--confirm-destructive`; volume changes and PostgreSQL major upgrades need a rehearsed restore first.
- `preserve()` keeps existing Railway values but creates nothing: set secrets in Railway first.
- Do not use legacy Railway Config as Code.
- The SDK's CLI version guard reads the shell variable `_`, so `env … railway config …` wrappers misreport the version; use the Make targets. The SDK lacks a pre-deploy timeout, hence GNU `timeout` in the pre-deploy command.

## Application deployment

- A merge to `main` deploys `web` and `imports-worker` via Railway's GitHub integration with **Wait for CI**. This is the only deployment route (no deploy workflows, tag deploys or Railway tokens in GitHub), and it does not apply IaC.
- Rolling back the image does not roll back pre-deploy migrations. Prefer migrations compatible with the previous release; destructive ones need a backup, rehearsed restore and explicit downtime decision.
- Keep `requires-python` at a minor range and pin the patch in `.python-version` and the Docker image; a patch pin in metadata broke GitHub's dependency-graph updater.

## Parliament imports in production

Applied imports auto-publish mandates; validation-only runs write no editorial data. Withdraw a wrong claim with the relationship admin action *Retirar publicação*; later imports never republish it. Hiding an entity withdraws all its claims.

- **Worker:** `imports-worker` runs the same image with `run_import_worker` and `APP_PROCESS=import-worker`; private, no domain or cron. Only `web` migrates (see comments in `.railway/railway.ts`).
- **Admin:** requires `ENABLE_ADMIN=true` on `web` and an active staff user with `core.run_import`. The worker re-checks that authority before running.
- **API:** `/ops/imports/` is enabled by `IMPORT_API_TOKEN` on `web` (at least 32 characters; empty disables it). It is independent of `ENABLE_ADMIN`.
- **GitHub:** `.github/workflows/import-data.yml` enqueues and polls through the API. It needs a `production-import` environment restricted to `main`, variable `IMPORT_BASE_URL` (exact HTTPS web origin) and environment secret `IMPORT_API_TOKEN`. Never put Railway or database credentials in GitHub. Run `dry_run` before `apply`.

Recovery:

- One queued/running job at a time. Read existing history before dispatching again: a workflow rerun is a new request, and a workflow timeout or cancellation does not cancel the durable job.
- Never edit statuses, delete history or clear locks by hand. A crashed job is marked failed by the next worker, with no automatic retry.
- Token rotation has no overlap: pause dispatches, change the token in Railway, then update the GitHub secret. Revoking the token or disabling admin does not cancel queued jobs.

## Government and EpT enrichment

Direct management commands only (`import_government`, `import_interests`), independent of the Parliament queue, API and worker. Dry-run is the default; `--apply` writes.

- **Government:** apply auto-publishes office claims, like Parliament mandates. Claims linked to an existing private entity stay drafts.
- **Identity:** source-ID-to-entity mappings are reviewed under *correspondências de identidade* (`core.review_sourceidentity`) with evidence beyond a name. EpT needs a reviewed holder mapping before collection. Used mappings are immutable.
- **EpT and biography candidates:** staff with `core.review_sourceobservation` convert candidates into private draft relationships, then publish them with the relationship publication action.
- An offline admin action, *Extrair candidatas profissionais das biografias retidas*, extracts professional-role candidates from already-retained Parliament biographies for the same review.

## Releases

- `pyproject.toml` owns the version; Release Please maintains `CHANGELOG.md`. Conventional Commit squash titles determine the release type.
- The `uv.lock` selector in `release-please-config.json` compares `@.name.value` on purpose (Release Please represents TOML scalars as objects). After upgrading the action, check that the release PR still updates both `pyproject.toml` and `uv.lock`.
- `.github/workflows/release-please.yml` authors release PRs with a GitHub App, so normal PR CI runs. `scripts/release_guard.py` (trusted `main` code) validates the PR, then requests squash auto-merge; required checks still apply.
- App setup: private App, repository Contents, Pull requests and Issues read/write only, installed only on this repository. Repository secret `RELEASE_APP_CLIENT_ID`; private key as `RELEASE_APP_PRIVATE_KEY` in the `release` environment (restricted to `main`). Enable repository auto-merge.
- To pause: first cancel auto-merge on any queued release PR, then disable the workflow. Disabling the workflow or revoking the key does not cancel an existing auto-merge.

## Incidents

- Check the deployed SHA, deployment state, logs and database readiness. Never paste variables, cookies or editorial notes into tickets.
- For suspected compromise, limit exposure, revoke sessions and rotate secrets first; removing a secret from Git does not remove it from history. Follow [SECURITY.md](../SECURITY.md).
