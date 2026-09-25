# Ligações PT

Documented political profiles and relationships of public interest in Portugal. **A connection is not evidence of wrongdoing.** Read the [editorial methodology](docs/methodology.md) before interpreting or adding information.

The catalogue starts deliberately empty; test examples are fictional. The public interface is in Portuguese, while repository documentation is in British English.

**Website:** <https://web-production-ca58.up.railway.app> · **Repository:** <https://github.com/TheRockPusher/pt_ligacoes>

## Local development

Install Git, GNU Make, Bash, uv, Node/pnpm and Docker with Compose (or use a dedicated PostgreSQL instance). Use the repository's tool pins and lockfiles rather than copying version numbers from documentation: [.python-version](.python-version), [.node-version](.node-version), [package.json](package.json) and [pyproject.toml](pyproject.toml).

```sh
git clone https://github.com/TheRockPusher/pt_ligacoes.git
cd pt_ligacoes
make setup
make dev
```

Open <http://127.0.0.1:8000/>. An empty directory is expected. Restart the development server after Python changes.

The generated `.env` is for local use only; never copy it into Git or production. [.env.example](.env.example) is a reference, not usable credentials. Changing a password in `.env` does not update an existing PostgreSQL volume's credentials.

### Without Docker

Use a dedicated development PostgreSQL instance matching [infra/compose.yaml](infra/compose.yaml). Create separate development and browser-test databases, with the latter's name ending in `_e2e`. The development role needs `CREATEDB` for pytest's disposable database; never use production credentials.

```sh
make env
# Set DATABASE_URL and E2E_DATABASE_URL in .env for your development instance.
make install
make migrate
make build
make dev
```

### Next steps

- Run `make help` or read the [Makefile](Makefile) for commands; see [verification guidance](CONTRIBUTING.md#verification) for choosing meaningful checks.
- Before browser checks, install Chromium and its system dependencies with `pnpm exec playwright install --with-deps chromium`. An administrator must supply system libraries if your account cannot install them.
- To try editorial review **locally with fictional records**, create an account interactively, then visit `/admin/`:

  ```sh
  bash scripts/with-env.sh uv run --frozen python apps/platform/manage.py createsuperuser
  ```

## Official Parliament import

The `import_parliament` management command imports only the official Assembleia da República (AR) roster and relevant biographies. It selects serving MPs from dated mandate states and joins biographies by AR cadastro identifier, never by name. Company/association joins, EpT ingestion and openAR voting links are not included; see the [source research and deferred integrations](docs/source-research.md).

Use the ignored, project-local `.env` and a dedicated local database with migrations applied. Check its `DATABASE_URL` before applying: the command uses the configured database, not an enforced local-only connection. No user-wide configuration, production credentials or real records belong in Git.

```sh
# Fetch and validate only: dry-run is the default.
bash scripts/with-env.sh uv run --frozen python apps/platform/manage.py import_parliament

# Explicitly persist the validated snapshot as non-public editorial material.
bash scripts/with-env.sh uv run --frozen python apps/platform/manage.py import_parliament --apply
```

Defaults are legislature `XVII`, today's local date and exactly 230 serving MPs. Use `--legislature`, `--as-of YYYY-MM-DD` and `--expected-count` only for a deliberately selected scope; do not lower the expected count to conceal an incomplete source. `--dry-run` explicitly selects the default mode and cannot be combined with `--apply`. Dry-run still fetches the official sources but makes no database writes.

An unsafe, ambiguous or incomplete snapshot fails rather than partially importing; database failures roll back the apply transaction. Applied imports retain minimised private source revisions and draft public-office claims, not whole source documents. Unchanged observations do not create duplicate revisions; changed or ceased observations withdraw affected claims for fresh review without overwriting editorial prose. **Importing never publishes automatically.** Review the source, identities, dates, fields and visibility through the existing editorial process before any publication; running locally is not authorisation to deploy or seed production. See the [methodology](docs/methodology.md#official-parliament-import) and [fetching security boundary](SECURITY.md#threats-and-limitations).

## Documentation

- [Contributing](CONTRIBUTING.md): review, verification and collaboration.
- [Editorial methodology](docs/methodology.md): evidence, identities, corrections and retention.
- [Source research](docs/source-research.md): official AR reuse, EpT access verification and deferred company, association and openAR work.
- [Design rationale](docs/architecture.md): trade-offs and boundaries for future work.
- [Operations](docs/operations.md): operator procedures, infrastructure and releases.
- [Security](SECURITY.md): private reporting and remaining limitations.
- [Changelog](CHANGELOG.md): release history.

**No licence has been chosen.** Publishing this repository does not itself grant an open-source licence or permission to reuse third-party material.
