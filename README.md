# Ligações PT

Documented political profiles and relationships of public interest in Portugal. **A connection is not evidence of wrongdoing.** Read the [editorial methodology](docs/methodology.md) before interpreting or adding information.

The catalogue starts empty; test examples are fictional. The public interface is in Portuguese; repository documentation is in British English.

**Website:** <https://web-production-ca58.up.railway.app> · **Repository:** <https://github.com/TheRockPusher/pt_ligacoes>

## Local development

Needs Git, GNU Make, Bash, uv, Node/pnpm and Docker with Compose. Versions are pinned in [.python-version](.python-version), [.node-version](.node-version), [package.json](package.json) and [pyproject.toml](pyproject.toml).

```sh
make setup
make dev
```

Open <http://127.0.0.1:8000/>. The generated `.env` is local-only; never commit it or reuse it in production.

**Without Docker:** use a dedicated PostgreSQL instance (see [infra/compose.yaml](infra/compose.yaml)) with separate development and browser-test databases, the latter's name ending in `_e2e`. The role needs `CREATEDB` for pytest. Run `make env`, set `DATABASE_URL` and `E2E_DATABASE_URL` in `.env`, then `make install migrate build dev`.

Run `make help` for other commands. Browser tests need `pnpm exec playwright install --with-deps chromium`. For local admin with fictional data:

```sh
bash scripts/with-env.sh uv run --frozen python apps/platform/manage.py createsuperuser
```

## Official-source imports

Official-source importers link identifiers across Portuguese and European institutions, corporate registers, public money and contact records. Eligible official-identifier claims and fully anchored events publish automatically; name-only people, declared interests and biography roles need editorial review. Imports default to dry-run; production seeding needs explicit maintainer authorisation. See [sources](docs/sources.md) for official links and coverage, and [operations](docs/operations.md) for commands, load order and review procedures.

The Portuguese public interface includes `/fontes/` (source coverage and provenance) and `/caminhos/` (“Como estão ligados?”: computed paths, not new claims).

## Documentation

- [Architecture](docs/architecture.md)
- [Sources](docs/sources.md)
- [Methodology](docs/methodology.md)
- [Operations](docs/operations.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

**No licence has been chosen.** Publishing this repository grants no licence to reuse it or third-party material.
