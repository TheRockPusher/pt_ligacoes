# Security

## Reporting

Report vulnerabilities through [GitHub Private Vulnerability Reporting](https://github.com/TheRockPusher/pt_ligacoes/security/advisories/new). Do not disclose exploits, credentials or personal data in public issues. Include the affected commit, steps to reproduce and observed impact; use redacted or synthetic data.

Test only systems you own or are explicitly authorised to test. This policy does not authorise testing Railway, GitHub, official sources or other users. Stop once you have enough evidence. There is no bounty.

`main` is the only supported version.

## Trust boundaries and limitations

- **Admin**: disabled in production unless `ENABLE_ADMIN=true`. There is no built-in MFA or rate limiting; protect admin access externally before enabling it.
- **Importers** fetch only fixed, allowlisted official hosts and routes (Parliament, Government, Entidade para a Transparência). They never fetch arbitrary or user-supplied URLs; adding a source or broadening an allowlist needs security review. Applied Parliament and Government imports publish official office claims automatically, so anyone able to run an apply can publish them.
- **Import API**: disabled unless `IMPORT_API_TOKEN` is at least 32 characters. The bearer token can trigger an apply that publishes mandates; treat it as a secret and rotate it if exposed.
- **TLS**: production trusts `X-Forwarded-Proto` from Railway's proxy. Never expose Gunicorn directly.
- **CSP**: scripts are restricted to `'self'`. Do not add `unsafe-inline` or `unsafe-eval` to `script-src`.
- **Release automation**: the release App's private key lives only in the `main`-restricted `release` GitHub environment; keep required checks in place.
- Published connections are documentary claims, not evidence of wrongdoing. Source observations publish automatically with editorial withdrawal; see [methodology](docs/methodology.md).

Operational procedures are in [operations](docs/operations.md).
