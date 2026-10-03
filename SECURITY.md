# Security

## Reporting

Report vulnerabilities through [GitHub Private Vulnerability Reporting](https://github.com/TheRockPusher/pt_ligacoes/security/advisories/new). Do not disclose exploits, credentials or personal data in public issues. Include the affected commit, steps to reproduce and observed impact; use redacted or synthetic data.

Test only systems you own or are explicitly authorised to test. This policy does not authorise testing Railway, GitHub, official sources or other users. Stop once you have enough evidence. There is no bounty.

`main` is the only supported version.

## Trust boundaries and limitations

- **Admin**: disabled in production unless `ENABLE_ADMIN=true`. There is no built-in MFA or rate limiting; protect admin access externally before enabling it.
- **Source collectors** fetch only their allowlisted hosts and routes, never arbitrary or user-supplied URLs. Adding a source or broadening an allowlist needs security review. Anyone able to apply an import can publish eligible claims or events automatically, subject to editorial withdrawal; see [methodology](docs/methodology.md).
- **Import API**: disabled unless `IMPORT_API_TOKEN` is at least 32 characters. The bearer token can queue a Parliament apply that publishes mandates; treat it as a secret and rotate it if exposed.
- **TLS**: production trusts `X-Forwarded-Proto` from Railway's proxy. Never expose Gunicorn directly.
- **CSP**: scripts are restricted to `'self'`. Do not add `unsafe-inline` or `unsafe-eval` to `script-src`.
- **Release automation**: the release App's private key lives only in the `main`-restricted `release` GitHub environment; keep required checks in place.

Operational procedures are in [operations](docs/operations.md).
