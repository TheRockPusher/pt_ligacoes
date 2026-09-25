# Contributing

Ligações PT is a public-interest project, not a rumour database. Read the [editorial policy](docs/methodology.md) before proposing sources or identities; keep unnecessary real personal data out of discussions and shared artefacts. Use the [private channel](SECURITY.md) for vulnerabilities or private data exposure.

## Proposals and review

- Work in short-lived branches from `main`, with one coherent change per small PR. Describe the purpose, observable behaviour, risks and evidence actually obtained, not planned checks.
- Obtain review and pass the required checks, including `ci` and `CodeQL`. Maintainers must verify remote protection; deadlines do not justify bypassing it.
- Squash merge with an English Conventional Commits title, such as `feat: filter connections by date`, `fix: withdraw private evidence` or `docs: explain recovery`. Mark breaking changes explicitly; follow the [release procedure](docs/operations.md).
- No licence or implicit contributor agreement has been chosen. Discuss third-party material with maintainers first; do not assume permission to copy it or add a licence unilaterally.

Prefer straightforward code and existing [architectural boundaries](docs/architecture.md), not abstractions, endpoints or background jobs for hypothetical needs. Reuse the repository's toolchain rather than adding competing managers or checks. Fix typing problems locally; do not hide them with `Any` or blanket suppressions. Explain and review any narrow exception.

Include reviewed PostgreSQL migrations with model changes, considering compatibility and recovery for existing data. Never commit secrets or dumps; removing a secret does not revoke it—report and rotate it. Infrastructure changes require the separate review and authorisation in [operations](docs/operations.md); a merge is not permission for destructive changes.

## Verification

Use the [development guide](README.md) and repository automation for commands. Choose checks for the changed surface and record observed results and external blockers, without invented coverage claims.

- Use disposable PostgreSQL, with a test role allowed to create test databases. Never use production credentials or substitute SQLite: database constraints, locking and transactions are part of the contract. An `_e2e` suffix is an accident-prevention guard, not permission to use a real database.
- Fixtures must be deterministic, isolated and explicitly fictitious. Publish through the domain service; direct SQL in adversarial tests is not an authorised editorial workflow. Do not add production demonstration loaders or load fixtures or test accounts into production.
- Test plausible consumer-visible bugs, boundaries and trust failures, not forwarding, copied constants, incidental wording or mock echoes. There is no arbitrary coverage target. Concurrency tests need real database transactions, bounded waits and reliable clean-up; assert persisted behaviour, not merely the absence of exceptions.
- Observe the changed interface in a real browser: include keyboard use, mobile widths, long labels and the HTML alternative to the graph. Basic browser tests are not a full accessibility audit; review zoom, contrast, screen-reader behaviour, focus order and errors as relevant. Do not commit failure traces or screenshots.
- Runtime/container changes need an actual smoke run against disposable infrastructure. Static checks do not show that a page was visited; passing tests establish only exercised cases. Browser checks do not establish editorial accuracy, privacy compliance or backup recoverability.
- Passing tests on the development/CI PostgreSQL major version does not prove production compatibility when versions differ. After an authorised deployment, verify migrations, readiness and public journeys in the real environment without adding test data. A CI container smoke is not evidence of live service health.
- Infrastructure type checks do not establish remote state or apply changes. Follow the reviewed operational process; never expose deployment credentials to contribution PRs.

## Collaboration and authorisation

Assign one integration owner and exclusive file ownership; use separate worktrees for concurrent work. Agree shared interfaces before editing. One owner co-ordinates dependencies, lockfiles, migrations, CI and final integration; overlapping edits require an explicit handover.

Do not run project-wide checks against other contributors' unfinished changes. The integration owner verifies the combined result and observes the changed path. Preserve unexpected changes belonging to others rather than deleting them to tidy a diff.

Agents have no implicit permission to read host secrets, create accounts, push, change infrastructure, publish data or deploy. Grant only necessary access and review their changes. Keep local agent configuration private and ignored.
