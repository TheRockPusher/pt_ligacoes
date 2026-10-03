# Operations

Operator procedures the code cannot express. Linking and storage: [architecture](architecture.md); official links and access limits: [sources](sources.md); editorial rules: [methodology](methodology.md); security boundaries: [SECURITY.md](../SECURITY.md); desired infrastructure: [`.railway/railway.ts`](../.railway/railway.ts).

## Production baseline

- `web.DATABASE_URL` is a private URL for a restricted application role, shared by `imports-worker` and `imports-refresh`. Never give an application process the Postgres superuser URL (`Postgres.DATABASE_URL`).
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

- A merge to `main` deploys `web`, `imports-worker` and `imports-refresh` via Railway's GitHub integration with **Wait for CI**; it does not apply IaC.
- Only `web` runs `migrate --noinput` in pre-deploy, with a five-minute command deadline. Wait for successful migrations before applying imports; if the worker exhausts retries while migrations are pending, restart it after web migration succeeds. For a local database, run `python apps/platform/manage.py migrate --noinput` first.
- Rolling back the image does not roll back pre-deploy migrations. Prefer migrations compatible with the previous release; destructive ones need a backup, rehearsed restore and explicit downtime decision.

When upgrading an existing database from shared biography scopes, migrations retire the legacy observations and invalidate their publication without discarding withdrawals. Reimport the relevant Parliament legislatures to republish biographies in source-owned legislature scopes. The reconciliation migration creates its audit, decision and redirect tables; it does not merge existing profiles. Linking runs after the source imports, not during migration.

## Official-source imports (local and production)

Run management commands through `apps/platform/manage.py` against the intended database. `refresh_sources` defaults to dry-run; `--apply` writes complete snapshots. Steps run sequentially: AR → Government → EpT, with the Wikidata crosswalk before EP and `link_identities` last to reconcile corroborated profiles after all available context. Do not reverse dependencies or run competing refreshes, direct applies or queued applies against the same database during first load or recovery.

The private Railway cron service `imports-refresh` runs `refresh_sources --apply` daily at **02:30 UTC**, using the restricted web database credentials. Historical AR scopes, SIOE, each BASE year and each EU-funds programme have a seven-day minimum interval after a successful apply. Independent later steps continue after a failure; identity-dependent steps are skipped if their prerequisite family failed. Any failed step makes the command exit non-zero; earlier successful snapshots stay committed.

Bulk import transactions have separate limits: up to **30 minutes** waiting for the editorial lock, then **15 minutes per SQL statement** and for idle-in-transaction periods. These transaction-local limits are not a whole-import deadline and do not extend ordinary editorial transactions. After a lock or statement timeout, let the active apply finish and retry the affected scope; never clear the lock or start competing applies.

### First production load

After deployment and successful web migrations, open a shell in the running worker:

```sh
railway ssh --service imports-worker
nohup sh -c 'python apps/platform/manage.py refresh_sources --apply --initial; result=$?; printf "\nexit=%s\n" "$result"; exit "$result"' > /tmp/refresh-initial.log 2>&1 < /dev/null &
```

Run this once to load BASE from 2012 onwards and bypass minimum refresh intervals; ordinary refreshes cover the current and previous year. Allow hours: EpT declarations and SIOE dominate. Read `/tmp/refresh-initial.log` for failures, prerequisite skips, the final `link_identities` result and the exit status; `nohup` survives shell disconnection, not a service restart or redeployment.

Resume failed steps and their prerequisite-skipped dependants rather than restarting the whole sequence. `--only` retains the built-in dependency order, not the order of selector arguments. Examples of scoped retries:

```sh
python apps/platform/manage.py refresh_sources --apply --only parliament:XVI
python apps/platform/manage.py refresh_sources --apply --initial --only base_contracts:2012
```

After the source retries succeed, rerun final reconciliation; `--only` omits it unless explicitly selected:

```sh
python apps/platform/manage.py link_identities
python apps/platform/manage.py link_identities --apply
```

`--only` and `--skip` accept one or more family names or scoped names, such as `parliament:XVI`; `--only` bypasses minimum intervals, and `--skip` excludes matching steps. Include `--initial` when selecting a BASE year older than the ordinary refresh window. Use `--cache-dir` for SIOE's minimised response cache outside Git; retain that directory for retries (the default is under the system temporary directory). For direct importer retries, retain the original `--as-of` date where supported. See `refresh_sources --help` for selectors and [sources](sources.md) for access limits; never bypass challenges.

After deployments and completed imports, run `make acceptance` for the public end-to-end check. Override the target with `make acceptance ACCEPTANCE_URL=https://your-public-host`.

After restoring a database copy or changing events, entities or sources through raw SQL, run `python apps/platform/manage.py rebuild_event_summaries` (optionally `--dataset`) before relying on event totals or paths. Application writes maintain these summaries transactionally.

## Editorial operations

Publication is automatic; candidate conversion is not the normal import workflow. The publication and privacy boundaries belong in [methodology](methodology.md).

- **Withdrawals:** staff with `core.publish_relationship` use *Retirar publicação das relações selecionadas*; staff with `core.withdraw_event` use *Retirar os eventos selecionados*. These withdrawals block republication by later imports.
- **Advisory identity suggestions:** staff with `core.review_sourceidentity` accept or reject pending suggestions using corroboration beyond a name. Separate profiles need not wait for a decision. Ordinary suggestion acceptance can redirect only unused mappings; it is not a merge of already-used profiles. Rejected suggestions and explicit distinct-person decisions block automatic reconciliation.
- **Audited reconciliation:** `link_identities` without `--apply` shows proposed matches; `--apply` rechecks and commits each merge under its own editorial lock, recomputing until no eligible match remains. It is also the final refresh step. Use this path, never manual edits to used mappings; source-owned claims, evidence and withdrawals survive, with immutable merge provenance and privacy-aware public redirects as described in [architecture](architecture.md).

## Parliament queue/API

Only Parliament mandate imports use this durable queue; `refresh_sources` and the other importers run directly. Each request imports the selected legislature's complete effective-period history; `as_of` is snapshot currency, not a historical roster filter. The API result field `serving` counts effective periods.

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
