# Testes e verificação

## PostgreSQL, sem substitutos silenciosos

`make test` executa pytest/pytest-django com `config.settings.test` e a ligação `DATABASE_URL`. Django cria uma base isolada de teste; o utilizador de desenvolvimento/CI precisa de `CREATEDB`. Não aponte testes para credenciais de produção e não acrescente SQLite como fallback: constraints, locking e comportamento transacional fazem parte do contrato.

Desenvolvimento e CI usam PostgreSQL **17**; a configuração pretendida de produção Railway usa PostgreSQL **18**. Passar a suite em 17 não comprova por si a operação em 18: após um deploy autorizado, o mantenedor verifica migrações, prontidão e percursos públicos no ambiente real, sem carregar fixtures nem criar contas de teste em produção.

Exemplo de execução focada, usando apenas o ambiente local:

```sh
bash scripts/with-env.sh uv run --frozen pytest tests/test_publication.py
bash scripts/with-env.sh uv run --frozen pytest tests/test_publication_concurrency.py
bash scripts/with-env.sh uv run --frozen pytest tests/test_public_surfaces.py
```

A suite cobre invariantes de consumidor, não snapshots de código: permissão e revisão de publicação, evidência necessária, retirada dinâmica, rascunhos e IDs inacessíveis, projeção sem campos privados, intervalos conhecidos/desconhecidos, entradas inválidas, paginação/limites, escaping e configuração de produção fail-closed. Os testes de configuração importam settings num processo isolado, sem ligação à base e sem iniciar aplicação de produção.

Os dados de `tests/conftest.py` são sintéticos, explicitamente fictícios e criados por teste. A publicação válida usa o serviço de domínio. Alterações SQL diretas nos testes de retirada são deliberadas: demonstram que as leituras não dependem apenas de hooks `save`. Não são um exemplo de fluxo editorial autorizado.

`tests/test_publication_concurrency.py` usa transações reais (`django_db(transaction=True)`) e ligações PostgreSQL separadas por thread. As regressões verificam a eliminação de evidência através de uma instância antiga após mudança de relação e nova revisão, a sobreposição de uma gravação antiga sem alterações com uma edição/revisão e a concorrência entre publicação e edição de fonte/entidade. Conferem conteúdo persistido, retirada/projeção pública e eventos de auditoria, não apenas a ausência de exceções. A coordenação pausa apenas o agendamento de validação de uma instância e observa o PID bloqueador em `pg_stat_activity`; aceita tanto um commit concorrente como a espera pelo lock para não prender o teste na correção. Esperas e consultas têm limites, os sinais são libertados em `finally` e cada ligação de thread é fechada. Não há substituição de SQL, validação ou publicação por mocks.

`tests/test_assets.py` protege a transição entre builds: depois de o watcher substituir o manifesto e remover o bundle antigo, a página tem de referenciar o bundle novo sem reiniciar Django. A verificação visual do grafo deve incluir nomes longos e diferentes larguras de ecrã, não apenas o estado `ready`.

## Jornadas reais de navegador

```sh
pnpm exec playwright install --with-deps chromium
make e2e
```

Playwright usa Chromium desktop e viewport/dispositivo móvel. O comando `scripts/e2e-server.sh` exige `E2E_DATABASE_URL`, verifica o sufixo `_e2e`, aplica migrações **nessa base**, chama `tests/e2e/seed.py` com settings de teste e inicia Django em `127.0.0.1:8000`. Não reutiliza silenciosamente um servidor existente. Termine `make dev` antes desta verificação, porque usa a mesma porta.

A fixture é apenas um helper em `tests/`, não um comando de produção. Também recusa settings diferentes de teste ou base sem sufixo `_e2e`. Os registos têm slugs `e2e-*`, nomes fictícios e revisão sintética sem password de login utilizável. Incluem uma relação com datas, uma com limites desconhecidos, conteúdo hostil para verificar escaping e uma entidade privada. Não executa limpeza global de dados. Este sufixo é uma barreira contra erro operacional, não autorização para usar uma base real com nome semelhante.

As jornadas verificam pesquisa → perfil → grafo → evidência, filtro temporal, navegação por teclado, ausência de overflow horizontal no viewport móvel e texto hostil inerte. A lista HTML de relações é a alternativa acessível ao canvas. Testes básicos de teclado/mobile **não são uma auditoria WCAG completa**; reveja também zoom, contraste, leitor de ecrã, ordem de foco e mensagens de erro ao alterar a interface.

Seletores estáveis: nomes acessíveis nos campos/botões; `.evidence-link` para ligações à evidência; `data-testid="relationship-graph"` com `data-state` `loading`, `ready`, `empty` ou `error`. Os testes não dependem de coordenadas do grafo ou de ordem aleatória do layout. Traces/screenshots de falhas ficam em `test-results/e2e` e não devem ser versionados.

## O que uma verificação permite afirmar

`make check` verifica análise estática e drift de migrações; não prova que uma página foi navegada. Pytest prova os casos exercitados; não prova que não existem outras falhas. O navegador observa interações reais; não substitui revisão de privacidade, autenticidade das fontes ou operação de backups. O smoke de produção do CI verifica a imagem contra PostgreSQL descartável, não a saúde futura de Railway.

A verificação TypeScript da IaC não aplica infraestrutura nem comprova ausência de drift. O `railway config plan` compara o único `.railway/railway.ts` com o ambiente ligado; o mantenedor revê todos os recursos, variáveis preservadas e eventuais eliminações antes de autorizar `config apply`, conforme [operação](operations.md). CI verde e um deploy GitHub bem-sucedido não significam que mudanças IaC foram aplicadas. Não disponibilize credenciais Railway a PRs para executar esta verificação.

Não existe um limiar de cobertura escolhido arbitrariamente. Adicione regressões para bugs plausíveis e limites de confiança; não teste apenas encaminhamento, cópias de constantes, texto incidental ou mocks que devolvem o valor que o teste acabou de fornecer. Mudanças na interface precisam também de observação visual real. Reporte comandos/resultados realmente executados e qualquer bloqueio externo; este documento não é um relatório de testes passados.
