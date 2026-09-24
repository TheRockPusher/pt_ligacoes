# Ligações PT

Perfis e relações documentadas de interesse público em Portugal. Uma ligação **não é prova de irregularidade**. Cada relação pública exige revisão e evidência pública; a interface distingue datas conhecidas de períodos incertos.

A aplicação começa deliberadamente **sem pessoas ou relações reais**. Não há importação automática, dados de demonstração em produção, registo público, uploads ou API pública de escrita. Os exemplos dos testes são explicitamente fictícios. Consulte a [metodologia](docs/methodology.md) antes de introduzir informação.

## Arquitetura

Monólito modular: Python 3.13, Django 5.2 LTS e PostgreSQL (17 no desenvolvimento/CI; 18 na configuração pretendida de produção Railway); HTML renderizado no servidor, HTMX, TypeScript, Tailwind e Cytoscape. Vite compila os recursos que Django/WhiteNoise serve no mesmo processo de aplicação. Não há servidor frontend separado em produção, filas, Nx ou Turborepo.

| Diretório | Responsabilidade |
| --- | --- |
| `apps/platform/ligacoes/core/` | Entidades, fontes, relações, evidência e publicação auditada |
| `apps/platform/ligacoes/public/` | Projeções públicas filtradas, páginas e grafo |
| `apps/platform/config/` | Configurações explícitas por ambiente e segurança HTTP |
| `apps/platform/templates/`, `frontend/src/` | Interface portuguesa e recursos locais |
| `tests/`, `tests/e2e/` | Invariantes PostgreSQL e navegação com dados fictícios |
| `infra/`, `scripts/`, `.railway/`, `.github/workflows/` | Container, comandos locais, IaC do projeto e automação |

Detalhes: [arquitetura](docs/architecture.md), [operação e releases](docs/operations.md), [segurança](SECURITY.md), [contribuição](CONTRIBUTING.md).

## Desenvolvimento local

Pré-requisitos: Git, GNU Make, Bash, Docker com Compose (ou PostgreSQL externo), **uv 0.12.18**, **Node 24.21.0** e **pnpm 12.6.0**. Python **3.13.15** está fixado em `.python-version`; uv pode provisioná-lo. As versões autoritativas são os ficheiros de ferramentas e os lockfiles, não uma instalação global pré-existente.

```sh
git clone https://github.com/TheRockPusher/pt_ligacoes.git
cd pt_ligacoes
make setup
make dev
```

Abra <http://127.0.0.1:8000/>. `make setup` gera `.env` com credenciais aleatórias exclusivamente locais, instala dependências bloqueadas, inicia PostgreSQL em `127.0.0.1:5432`, aplica migrações e compila os recursos. Um `.env` existente é preservado e recebe permissões `0600`. `.env.example` é referência, não um conjunto de credenciais utilizável. Nunca copie `.env` para Railway ou Git.

`make dev` executa Django e recompila recursos quando o frontend muda. Reinicie o comando após alterações Python; o servidor usa `--noreload`. O catálogo vazio é um resultado esperado, não uma falha de ligação à base de dados.

### PostgreSQL sem Docker

Use uma instalação PostgreSQL 17 local ou uma instância de desenvolvimento dedicada. Crie duas bases, `pt_ligacoes` e `pt_ligacoes_e2e`, e um utilizador proprietário. Para pytest, esse utilizador precisa de poder criar a base isolada `test_pt_ligacoes` (`CREATEDB` no ambiente de desenvolvimento, **não** no utilizador de produção).

```sh
make env
# Edite DATABASE_URL e E2E_DATABASE_URL no .env para a instância dedicada.
make install
make migrate
make build
make dev
```

`DATABASE_URL` aceita apenas PostgreSQL; não existe fallback SQLite. `E2E_DATABASE_URL` tem de identificar uma base cujo nome termine em `_e2e`. O servidor de navegador recusa usar a base normal para os seus dados fictícios. `make db-down` termina o PostgreSQL Compose sem apagar o volume. Alterar a palavra-passe no `.env` não altera credenciais de um volume PostgreSQL já inicializado.

### Comandos

| Comando | Efeito |
| --- | --- |
| `make check` | Coerência do lock, Ruff, Pyrefly, TypeScript, verificações Django e migrações em falta |
| `make format` | Aplica apenas fixes seguros do lint e formata Python com Ruff |
| `make lint` | Verifica as regras Ruff selecionadas, sem reescrever ficheiros |
| `make check-format` | Verifica a formatação Ruff, sem reescrever ficheiros |
| `make typecheck` | Verifica tipos Python com Pyrefly; TypeScript integra `make check` via `pnpm check` |
| `make test` | Testes pytest numa base PostgreSQL isolada |
| `make build` | Recursos de produção e manifesto Vite |
| `make e2e` | Compilação e Playwright desktop/mobile numa base E2E dedicada |
| `make audit` | Avisos de dependências Python/JavaScript; exige acesso aos serviços de advisories |
| `make secrets` | Gitleaks no histórico Git completo; requer Docker |
| `make container` | Imagem de produção não-root; requer Docker |
| `make migrate` | Migrações no ambiente selecionado pelo `.env`/variáveis |

Antes da primeira execução Playwright:

```sh
pnpm exec playwright install --with-deps chromium
make e2e
```

Em máquinas sem privilégios para instalar bibliotecas do sistema, prepare essas dependências através do administrador ou de um runner adequado. Não substitua a verificação de navegador por uma alegação de sucesso. [Guia de testes](docs/testing.md).

## Rotas públicas

- `/`: pesquisa e paginação de entidades públicas, até 100 caracteres na pesquisa.
- `/entidades/<slug>/`: perfil e relações revistas; `?at=AAAA-MM-DD` filtra períodos conhecidos inclusivamente.
- `/entidades/<slug>/grafo/`: JSON do mesmo recorte público, até 100 relações, com indicação de truncagem.
- `/evidencias/<uuid>/`: passagem, referência e fonte de uma relação pública.
- `/metodologia/`: limites e critérios de leitura.
- `/healthz/`: prontidão da base de dados, sem detalhes de ligação.

Datas desconhecidas não são inventadas: uma relação sem um limite pode permanecer no recorte, com aviso visível. Datas inválidas devolvem HTTP 400. O grafo é complementar; as relações e ligações à evidência continuam legíveis sem JavaScript.

## Configuração e publicação

Produção é a configuração por omissão e falha sem `SECRET_KEY`, `ALLOWED_HOSTS` e `DATABASE_URL` válidos. Desenvolvimento requer `DJANGO_SETTINGS_MODULE=config.settings.development`. O admin só tem rota quando `ENABLE_ADMIN=true`; está desligado por omissão em produção e não há superutilizador semeado. Publicar exige a permissão `core.publish_relationship` e revisão explícita, não apenas editar um estado no formulário.

Para experimentar o fluxo editorial **apenas no ambiente local**, com dados explicitamente fictícios, crie uma conta manualmente:

```sh
bash scripts/with-env.sh uv run --frozen python apps/platform/manage.py createsuperuser
```

Depois aceda a `/admin/` no servidor local. A criação é interativa: nenhuma password está no código e este comando não faz parte do deploy. Uma entidade/fonte marcada pública não publica por si uma relação; a ação de revisão tem de satisfazer os critérios de evidência e atribuição.

[Operação](docs/operations.md) documenta variáveis, Railway, cópias de segurança, CI e releases. Um único `.railway/railway.ts` descreve todo o projeto; alterações de infraestrutura exigem `plan` revisto e `apply` explícito pelo mantenedor, sem segredos no código. Pushes em `main` implantam a aplicação pela integração GitHub do Railway com **Wait for CI**, mas **não aplicam IaC**. Não há token Railway/PAT de deploy nos secrets GitHub. A configuração no repositório não prova que uma opção externa do GitHub/Railway esteja ativada: confirme-a antes de permitir deploys automáticos.

## Contribuir, segurança e licença

Use branches curtos, PRs revistos e squash merge com títulos Conventional Commits; consulte [CONTRIBUTING.md](CONTRIBUTING.md). Não inclua dados pessoais reais em fixtures, screenshots ou issues. Reporte vulnerabilidades em privado segundo [SECURITY.md](SECURITY.md).

**Ainda não foi escolhida uma licença.** A publicação do código não concede por si só uma licença open source. Não foi acrescentado um ficheiro de licença nem presumida uma autorização de reutilização.
