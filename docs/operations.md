# Operations

Operator procedures the code cannot express. Linking and storage: [architecture](architecture.md); official links and access limits: [sources](sources.md); editorial rules: [methodology](methodology.md); security boundaries: [SECURITY.md](../SECURITY.md); desired infrastructure: [`.railway/railway.ts`](../.railway/railway.ts).

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

## Official-source imports (local and production)

Run management commands through `apps/platform/manage.py` against the intended database. All importers default to dry-run; `--apply` writes atomically per complete snapshot (BASE per year, IGF per file, EU funds per programme). Review dry-run counts before applying. **Production seeding requires explicit maintainer authorisation**; permission to change code or deploy is not permission to import real data.

Recommended load order, so identities and institutions exist before dependent records:

1. `import_parliament` — roster for each intended legislature.
2. `import_parliament_bodies` — bodies for each legislature.
3. `import_parliament_interests` — historical AR interests.
4. `import_government --government gc21` through `gc25`.
5. `import_government_nominations` — gabinete nominations for those Governments.
6. `import_ept_offices` — EpT holder offices.
7. `import_sioe` — use `--cache-dir` outside Git; keep the same `--as-of` date when resuming.
8. `import_gleif`, then `import_wikidata_crosswalk` (hints only).
9. `import_base_contracts`, `import_igf_subsidies`, `import_eu_funds`, then `import_etf_boards`.
10. `import_european_parliament`.
11. `import_eu_contacts --dataset register`, then `import_eu_contacts --dataset ec-meetings`. The default `all` also requests the blocked EP meeting export; do not bypass its challenge.
12. `import_parliament_activities`, then `import_parliament_gifts`.

For EpT declarations, run `import_interests --holder-id …` only after reviewing that holder's identity mapping. Use each command's `--help` for required scope arguments and available limits; source coverage and unavailable exports are documented in [sources](sources.md).

Applied snapshots are retry-safe: official keys prevent duplicates, older snapshots are rejected, and editorial withdrawals persist. Resume a failed sequence at its failed scope; earlier committed files/programmes remain applied. SIOE's minimised response cache also resumes collection. Keep the original reference date on retries. These are operator retries, not automatic worker retries.

Event totals and counterparts on profiles, maps and paths are read from derived summary tables (`EventEntitySummary`, `EventPairSummary`) that `sync_events`, event withdrawal and entity or source visibility changes keep current inside their own transaction. After restoring a database copy or editing events, entities or sources outside the application (raw SQL), run `rebuild_event_summaries` (optionally `--dataset`); it takes minutes on millions of events. Requests with an observed date (`?at=`) still aggregate the events live, so they are slower for very large profiles.

Editorial work in the admin:

- **Identity suggestions and mappings:** staff with `core.review_sourceidentity` accept or reject pending suggestions using evidence beyond a name, or review a source identity mapping. Accepting a suggestion creates the reviewed mapping; re-run the relevant importer to resolve its pending claims. Used mappings are immutable.
- **Candidates:** staff with `core.review_sourceobservation` convert current source observations into private draft relationships. For a name-only subject, choose a verified existing person as well as the organisation. Conversion does not publish; use the relationship action *Rever e publicar relações selecionadas* after review.
- **Biography candidates:** the offline action *Extrair candidatas profissionais das biografias retidas* extracts candidates from retained Parliament biographies for that same review.
- **Events:** inspect records and participants in the event admin. Events use automatic publication, not candidate conversion; staff with `core.withdraw_event` use *Retirar os eventos selecionados* to withdraw them permanently from subsequent imports.

Only `import_parliament` has the queue/API/worker route below; other importers run directly. Automatic claim publication and private-candidate boundaries are defined in [methodology](methodology.md).

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

## Releases

- `pyproject.toml` owns the version; Release Please maintains `CHANGELOG.md`. Conventional Commit squash titles determine the release type.
- The `uv.lock` selector in `release-please-config.json` compares `@.name.value` on purpose (Release Please represents TOML scalars as objects). After upgrading the action, check that the release PR still updates both `pyproject.toml` and `uv.lock`.
- `.github/workflows/release-please.yml` authors release PRs with a GitHub App, so normal PR CI runs. `scripts/release_guard.py` (trusted `main` code) validates the PR, then requests squash auto-merge; required checks still apply.
- App setup: private App, repository Contents, Pull requests and Issues read/write only, installed only on this repository. Repository secret `RELEASE_APP_CLIENT_ID`; private key as `RELEASE_APP_PRIVATE_KEY` in the `release` environment (restricted to `main`). Enable repository auto-merge.
- To pause: first cancel auto-merge on any queued release PR, then disable the workflow. Disabling the workflow or revoking the key does not cancel an existing auto-merge.

## Incidents

- Check the deployed SHA, deployment state, logs and database readiness. Never paste variables, cookies or editorial notes into tickets.
- For suspected compromise, limit exposure, revoke sessions and rotate secrets first; removing a secret from Git does not remove it from history. Follow [SECURITY.md](../SECURITY.md).
