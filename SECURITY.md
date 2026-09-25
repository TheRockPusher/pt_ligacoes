# Security

## Report privately

Use [GitHub Private Vulnerability Reporting](https://github.com/TheRockPusher/pt_ligacoes/security/advisories/new). Do not put exploits, credentials, exposed personal data or private editorial content in public issues. There is no guaranteed response time or alternative security email address.

Include the affected commit and environment, prerequisites, affected path, reproducible steps and observed impact. Distinguish observations from hypotheses; use synthetic examples or redacted evidence, never complete secrets. A suggested fix is welcome.

If private reporting is unavailable, open only a neutral issue asking maintainers to enable it, **without identifying the vulnerability or affected people**. This file does not enable the GitHub feature.

Test only systems you own or are explicitly authorised to test. This policy does not authorise attacks on Railway, GitHub, external sources or users. Avoid further data access, bulk enumeration, disruption and persistent changes; stop once you have sufficient evidence. There is no bounty programme or promise of legal immunity.

## Support and response

`main` is the supported line; historical releases have no promised backports. Pinned dependencies are not permanently safe. Confirmed vulnerabilities call for containment, a reviewed fix, appropriate regression tests, secret rotation where needed and co-ordinated disclosure without unnecessary identifying details.

Personal data exposure also requires assessment of applicable legal and notification duties. Removing a page cannot recall caches, backups, exports or third-party copies. Follow [operations](docs/operations.md) for incident handling and [editorial policy](docs/methodology.md) for correction, retention and restore obligations.

## Threats and limitations

Protect the integrity of public claims, confidential drafts, notes and reviews, editorial credentials, the database and the build/deployment chain. Threats include malicious visitors or input, compromised editorial accounts, review bypasses, malicious contributions or dependency updates, and infrastructure misconfiguration.

- Evidence still needs human judgement and, where appropriate, legal review. Neither accuracy, completeness nor freedom from reputational harm is guaranteed.
- There is no built-in MFA, anti-bot defence, distributed rate limiting, WAF, intrusion detection or automated incident response. Public editorial access needs additional operational safeguards. Per-query limits do not prevent sustained requests.
- SQL operators can bypass content-invalidation hooks and alter data or audit history. Review history is not cryptographically tamper-proof.
- Ordinary editorial source URLs are not fetched by the server. The operator-invoked Parliament importer is a narrow exception: fixed official HTTPS hosts/routes and query shapes, public-address DNS checks with a pinned connection and hostname-verified TLS, bounded same-host redirects revalidated before connection, time/size limits, rejection of compressed responses and JSON/schema validation. It does not accept arbitrary URLs, cookies or proxy configuration. These controls are not a general-purpose fetch service; any new source needs its own reviewed fetching boundary. Following an editorial link still leaves the controlled origin, and URL validation alone cannot prevent DNS changes.
- The importer explicitly requires TLS 1.2 or newer while retaining certificate and hostname verification; do not rely on runtime-default protocol minima or disable verification to restore source access.
- Import dry-run still makes outbound requests. Direct CLI dry-run writes no database records; admin/GitHub dry-run writes queue/history metadata but no editorial records. Explicit apply validates a complete roster and biography snapshot before atomically persisting minimised private revisions and draft claims; it cannot publish them. Unsafe or incomplete snapshots fail closed, and database failures roll back. Source changes and departures invalidate affected approval, but source accuracy, safe interpretation and publication remain human responsibilities. Preserve field minimisation and avoid payloads or identifying details in diagnostics.
- Admin import requests require an active staff account with `core.run_import`, CSRF protection and explicit confirmation for draft apply. The worker rechecks the requester's authority before execution and apply. History is read-only, and import permission does not grant publication authority. Protect admin access externally before enabling it; permission checks do not replace MFA or login-abuse controls.
- The import API is a separate bearer-only boundary, independent of admin enablement. An empty `IMPORT_API_TOKEN` disables it; a configured token must be at least 32 characters. The token permits only complete-snapshot validation/draft-apply requests and safe aggregate status, not arbitrary sources, shell commands, deployment, database access or publication. It nevertheless authorises real private editorial writes: protect and rotate it as a secret. Cookie sessions do not authenticate the API; its CSRF exemption does not extend to admin.
- Only the narrow app `IMPORT_API_TOKEN` may be stored for this operation in GitHub's exact-main-only protected `production-import` environment. Never store Railway, deployment or database credentials there or in contribution CI. The client accepts an exact HTTPS origin and refuses redirects so credentials cannot follow an unexpected destination. Environment protection, reviewer/access policy and external abuse controls must be configured and verified separately; workflow YAML alone does not establish them. See [activation and rotation](docs/operations.md#controlled-parliament-imports).
- Graph rendering requires inline styles. Do not weaken script restrictions with `unsafe-inline` or `unsafe-eval` to work around frontend errors.
- Transport security depends on a trusted TLS proxy; direct Gunicorn exposure or untrusted forwarded headers breaks that boundary. Platform accounts, backups, log retention and restores require human oversight; see [operational safeguards](docs/operations.md), including explicit authorisation for destructive infrastructure changes.
- Dependency audits and secret scans detect known vulnerabilities and patterns, not every attack. A thorough review reduces uncertainty; it does not certify the absence of flaws. Repository configuration is not proof of current remote controls or service health.
- Release automation trusts repository writers and a narrowly scoped App, not arbitrary bot proposals. Keep the App key in the main-only `release` environment and preserve required checks; cancel queued auto-merge explicitly when pausing or revoking credentials. See the [release boundary](docs/operations.md#automatic-publication-and-its-boundary).

See [architecture](docs/architecture.md) for application trust boundaries and [verification](CONTRIBUTING.md#verification) for what checks can establish.
