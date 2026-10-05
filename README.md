# Ligações PT

Ligações PT is a non-profit tool for journalists investigating political profiles and relationships of public interest in Portugal. **A connection is not evidence of wrongdoing.** Published information must be verifiable through source links and minimal supporting passages; read the [editorial methodology](docs/methodology.md) before interpreting or adding it. The public interface is in Portuguese; repository documentation is in British English.

**Website:** <https://web-production-ca58.up.railway.app> · **Repository:** <https://github.com/TheRockPusher/pt_ligacoes>

## Local development

Needs Git, GNU Make, Bash, uv, Node/pnpm and Docker with Compose. Versions are pinned in [.python-version](.python-version), [.node-version](.node-version), [package.json](package.json) and [pyproject.toml](pyproject.toml).

```sh
make setup
make dev
```

Open <http://127.0.0.1:8000/>. The generated `.env` is local-only; never commit it or reuse it in production.

**Without Docker:** use a dedicated PostgreSQL instance (see [infra/compose.yaml](infra/compose.yaml)) with separate development and browser-test databases, the latter's name ending in `_e2e`. The role needs `CREATEDB` for pytest. Run `make env`, set `DATABASE_URL` and `E2E_DATABASE_URL` in `.env`, then `make install migrate build dev`.

Run `make help` for other commands. Browser tests need `pnpm exec playwright install --with-deps chromium`. To create a local administrator:

```sh
bash scripts/with-env.sh uv run --frozen python apps/platform/manage.py createsuperuser
```

## Official-source imports

Official sources feed the catalogue through a weekly refresh pipeline. Verifiable observations publish automatically; declared interests remain labelled as self-declared, not independently checked. See [sources](docs/sources.md) for coverage and [operations](docs/operations.md) for refresh intervals, import procedures and load order. The public `/fontes/` page shows coverage and provenance; `/caminhos/` computes paths, not new claims.

Run `make acceptance` for the end-to-end HTTP check of the public journalist experience, including connection paths, source coverage, freshness and evidence verifiability. It targets production by default; use `make acceptance ACCEPTANCE_URL=http://127.0.0.1:8000` to check a local instance.

## Documentation

- [Architecture](docs/architecture.md)
- [Sources](docs/sources.md)
- [Methodology](docs/methodology.md)
- [Operations](docs/operations.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

**No licence has been chosen.** Publishing this repository grants no licence to reuse it or third-party material.
