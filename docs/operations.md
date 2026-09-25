# Operations and releases

This runbook covers operator decisions, not configuration inventories. See [Railway IaC](../.railway/railway.ts) for desired infrastructure and [contributor verification](../CONTRIBUTING.md#verification) for local checks.

## Production readiness

The installation at <https://web-production-ca58.up.railway.app> was verified on **24 September 2026**: migrations were applied, PostgreSQL used TLS and a restricted application role, and the database had no public domain or TCP proxy. No editorial data or users were created; admin was disabled. These are historical observations, not a current health assessment.

Before automatic deployments or real data:

- Give `web.DATABASE_URL` a private URL for a dedicated application role, limited to its database/schema and Django migrations. Never use the superuser URL `Postgres.DATABASE_URL`, local development credentials or the local `CREATEDB` role.
- Set secrets directly in Railway, not Git, logs, PR comments or a copied local `.env`. Production does not load `.env`. Preserve imported Postgres variables without copying them to the application.
- Use exact public hosts and HTTPS CSRF origins. The Railway probe also needs `healthcheck.railway.app` in `ALLOWED_HOSTS`, **not** `CSRF_TRUSTED_ORIGINS`.
- Verify HTTPS, readiness, pre-deploy migrations and **Wait for CI** in the GitHub integration. Do not add a second deploy workflow for the same branch.
- Establish backups, a rehearsed restore, resource limits, alerts and retention before real data. The initial installation did **not** demonstrate this readiness; a database template is not proof of it.

Keep PostgreSQL private. Do not expose Gunicorn through TCP or an untrusted direct origin: Django trusts `X-Forwarded-Proto` replaced by Railway's ingress. The HTTP exception for `/healthz/` exists only for the internal probe, not to expose diagnostics.

A Railway SSH session may run as root even though the web process does not; inspect PID 1's UID rather than the diagnostic shell's. Keep the Gunicorn control socket private. Revoke and remove bootstrap-only keys after verification.

An empty installation is intentional: never seed production with E2E fixtures, automatically create a superuser or add real people merely to populate the interface. Before enabling admin, restrict access to operators and provision strong credentials through a protected channel; remove exposure when no longer needed. See [security limitations](../SECURITY.md), including the absence of native MFA and login rate limiting.

## Review and apply infrastructure

[Railway IaC](https://docs.railway.com/infrastructure-as-code) is **authoritative for the whole project**. Omitting a resource can delete it. Never reduce the file to the web service or export a partial configuration to hide differences.

Use the repository's pinned Node/pnpm versions and locked SDK. The external Railway CLI is separately pinned to **5.62.1** for compatibility with **SDK 3.11.0**; do not substitute an automatically downloaded latest CLI. Install it only if necessary, ensure pnpm's global binaries are on `PATH`, then work from the repository root:

```sh
pnpm install --frozen-lockfile
pnpm add --global @railway/cli@5.62.1 # only if this version is not installed
command -v railway
railway --version # must be 5.62.1
railway login
railway link --project pt-ligacoes --environment production
railway status
make infra-plan
```

Only an authorised maintainer should authenticate. Confirm the linked project and environment: the project name in the file does not override CLI context. Review code and dependencies **before** planning; TypeScript evaluation runs local code with the operator's credentials, so never evaluate an untrusted PR in that session.

1. Ensure required private values already exist on the correct services. `preserve()` retains an existing Railway value; it neither generates secrets nor imports `.env`. Preserve template variables and the database volume; never substitute example secrets.
2. Review the entire plan, including resource identities, storage, networking, variables and all creations/deletions. Detaching, deleting, shrinking or relocating a volume is potentially destructive; a PostgreSQL major upgrade needs a data migration plan, not just an image change. Require a rehearsed restore, defined impact/recovery and specific authorisation.
3. After explicit approval, run `make infra-apply`. It computes a **new** plan: review that confirmation too. If remote state changes or the plan becomes stale, stop and plan again. Never add `--yes` or `--confirm-destructive` to force an unexpected plan through.
4. After applying, verify remote state and a fresh plan with no unexplained differences. Applying IaC can change infrastructure and trigger redeploys; it is not a harmless check. Record only non-secret evidence: commit SHA, deployment state, verified HTTPS URL, observed checks and blockers.

To apply exactly a reviewed plan, the CLI supports `config plan --out <private-file>` and `config apply --plan <private-file>`. Restrict access and store it outside the repository and `.railway/`: it may contain secrets despite redacted terminal output. Never publish it in PRs, logs or public artefacts. Avoid `config pull --include-variables` and `plan --show-values`, which can expose credentials.

Legacy [Config as Code](https://docs.railway.com/config-as-code) is deprecated: new services cannot use it, and existing files stop being read on **1 December 2026**. Do not maintain parallel configuration. Migration also requires removing the remote **Config File** setting; deleting the Git file does not clear it. Consult the [SDK reference](https://docs.railway.com/infrastructure-as-code/reference) when changing IaC.

### Pinned-tool compatibility

These limitations were recorded for CLI **5.62.1** / SDK **3.11.0** during the installation, not reverified here:

- A post-apply plan repeated two representation-only differences: `web.restartPolicyType` from `null` to `ON_FAILURE`, and the Postgres mount from `null` to its existing volume/path. Direct inspection confirmed `ON_FAILURE`/3 and the same volume at `/var/lib/postgresql/data`; the database importer omitted the mount. There were no planned creations/deletions. Keep the desired policy/mount; do **not** dismiss other differences without investigation.
- `service("Postgres")` is deliberate: the `postgres()` helper introduces a public TCP proxy and template variables in this CLI. Preserve the existing private service instead.
- The SDK's version guard reads shell variable `_`. An `env … railway config …` wrapper can make it execute `/usr/bin/env` and incorrectly report an old CLI. Use the Make targets; a direct wrapper can use `env -u _ … railway config …`. Keep the guard enabled and verify the executable version.
- The SDK does not expose `preDeployTimeoutSeconds`; the versioned GNU `timeout` command bounds migrations instead. This is separate from the readiness timeout. Do not invent an SDK field or add legacy configuration to work around it.

## Application deployment and external controls

A merge to `main` deploys the **application** through Railway's GitHub integration, subject to **Wait for CI**. It does **not** evaluate or apply `.railway/railway.ts`; infrastructure changes need the separate reviewed procedure above, coordinated with application compatibility. Keep operator credentials out of contribution CI; do not add Railway tokens or a deploy PAT to GitHub secrets.

Keep Python compatibility metadata at the supported minor range; pin the actual patched runtime in `.python-version` and the Docker image. Requiring a specific patch in `requires-python` blocked GitHub's dependency-graph updater when its interpreter catalogue lagged behind, causing release checks to fail. Do not disable dependency analysis or Wait for CI to work around that mismatch.

As recorded on **24 September 2026**, GitHub protection required a PR, an up-to-date branch, `ci` and `CodeQL`, squash merging, linear history and resolved conversations. It covered administrators and prohibited force pushes/deletion, without requiring an unavailable second maintainer's approval. CodeQL extended scanning, secret scanning, push protection, dependency alerts and private vulnerability reporting were enabled. Recheck these external controls and Railway's Wait for CI periodically: repository YAML cannot establish their live state.

Verify the deployed SHA, deployment state and service readiness. A green workflow or a release tag is neither proof of a healthy deployment nor permission to bypass CI; a tag is not a parallel deployment mechanism.

## Release PRs

`pyproject.toml` owns the application version. Review generated version/lock changes together; Conventional Commit squash titles determine release classification.

The TOML selector `$.package[?(@.name.value=='pt-ligacoes')].version` is intentional: Release Please **17.6.0**, bundled with the pinned v5 action, represents scalars as tagged objects with `value`. Comparing `@.name` directly leaves the lock stale; a fixed package index is unstable. On action upgrades, verify that the generated PR updates both `pyproject.toml` and the root package in `uv.lock` and passes `uv lock --check`.

The pending state is the upstream label **`autorelease: pending`**, including the space after the colon. Check the real generated proposal when upgrading Release Please; a fixture copying the guard's spelling does not establish compatibility.

### One-time App setup

`GITHUB_TOKEN` does not trigger ordinary PR CI for its own changes. Use a private, repository-scoped GitHub App instead of a personal token or repeated close/reopen operations. Complete this setup before merging the App-backed workflow into `main`:

1. [Register a private App](https://github.com/settings/apps/new), without webhooks or user OAuth. Grant repository **Contents**, **Pull requests** and **Issues** read/write; Metadata read is implicit. Do not grant Administration, Actions, Workflows or branch-protection bypass.
2. Install it **only on `TheRockPusher/pt_ligacoes`**. Registration/installation needs the account's GitHub session; existing `gh` authentication is not an App credential.
3. Store its Client ID as repository Actions secret **`RELEASE_APP_CLIENT_ID`**. Store the generated PEM as **`RELEASE_APP_PRIVATE_KEY`** in **Settings → Environments → `release` → Environment secrets**, not as a repository-wide secret. Restrict that environment to the exact **`main` branch**, excluding tags, with no required reviewers or waiting period. This keeps the key out of same-repository PR workflows without imposing a manual release approval. Never put it in `.env`, Git, logs or chat.
4. Enable repository **Allow auto-merge** and preserve the required up-to-date `ci`/`CodeQL` checks, squash merging and protection covering administrators and the App. Code and dependency PRs still need an explicit merge decision.
5. If an old proposal authored by `github-actions[bot]` remains open, close it and remove its branch before the first App-backed run. The guard does not make an exception for the old identity.

The pinned token action issues a short-lived token scoped to this repository and the stated permissions, then revokes it after the job. The job's ordinary `GITHUB_TOKEN` is read-only. Missing credentials fail the workflow; there is no personal-token fallback.

### Automatic publication and its boundary

An eligible merge or **Release Please → Run workflow → main** creates or updates the App's proposal, triggering normal PR CI/CodeQL. A manual run also finds an existing unchanged proposal. Trusted `main` code in `scripts/release_guard.py` validates the App identity and immutable release contents before requesting native squash auto-merge. It permits synchronised, increasing stable versions and changelog changes, not dependency or configuration changes disguised as a release. Pre-release versions require a separately reviewed policy change.

`always-update` is intentional: refresh the generated branch even when release notes have not changed, so an intervening `main` commit does not leave auto-merge blocked by strict up-to-date protection. Request auto-merge immediately after validation, rather than waiting for checks to turn green; GitHub enforces the required checks. A manual dispatch can refresh a waiting proposal without a new releasable commit.

The privileged job never checks out or executes proposal code. Its final SHA recheck and `--match-head-commit` protect the validation-to-enablement transition; they do **not** make a queued branch immutable. Repository writers remain trusted, and required checks apply to the current PR revision. Do not bypass a rejected proposal or failing check. This workflow does not authorise Dependabot or ordinary code PRs.

The App's merge triggers normal `main` checks and Release Please publication. Railway's GitHub integration remains the only application deployment route; there is no separate tag-triggered deploy job. Before calling automation active, observe a real **App proposal → PR checks → protected auto-merge → tag/release → matching healthy Railway deployment** cycle. Unit tests and the presence of credentials do not establish that behaviour.

To pause, first disable auto-merge on any already queued proposal, then disable the Release Please workflow. Disabling the workflow or revoking its key alone does not cancel GitHub's existing auto-merge request. To resume after repairs, re-enable the workflow and dispatch it on `main`; do not grant bypass privileges.

## Migrations and incidents

Prefer schema changes compatible with the previous application version during rollout. Pre-deploy migrations may have completed even if the new release fails readiness: **rolling back the image does not roll back the database**. Before destructive changes, require a backup, rehearsed restore, explicit downtime decision and a specific recovery plan.

For failures, inspect the deployed SHA, deployment state, build/runtime logs and PostgreSQL readiness. Never paste complete variables, cookies or editorial notes into tickets. Gunicorn does not write request access logs, but ingress may; configure its retention and access separately.

For suspected compromise, restrict editorial exposure as needed, revoke credentials/sessions, rotate secrets and preserve minimal evidence with restricted access. Removing a secret from the latest commit leaves it in history: rotate first, then coordinate clean-up. Hiding content may contain an incident but does not remove backups or exports. Follow [SECURITY.md](../SECURITY.md) and the [editorial methodology](methodology.md).
