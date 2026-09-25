# Contribuir

Este é um projeto de interesse público, não uma base de rumores. Antes de acrescentar fontes ou identidades, leia a [metodologia](docs/methodology.md). Discussões, fixtures, reproduções, screenshots e logs partilhados não devem incluir dados pessoais reais desnecessários. Vulnerabilidades seguem o [canal privado](SECURITY.md), não uma issue pública.

## Fluxo trunk-based

- `main` é a linha de integração. Use uma branch curta por mudança coerente; evite branches de release duradouras.
- Abra uma PR pequena, descrevendo objetivo, comportamento observável, riscos e verificações realmente executadas. Não apresente testes planeados como testes passados.
- Reveja PRs de código e dependências e exija os checks `ci` e `CodeQL` verdes. Mantenedores devem preservar essas exigências na proteção remota de `main`; não contorne checks para cumprir uma data.
- Faça squash merge. O título da PR torna-se o commit de integração e segue Conventional Commits, por exemplo `feat: filtrar relações por data`, `fix: retirar evidência não pública` ou `docs: explicar restauro`.
- Assinale breaking changes de forma explícita. Release Please usa os commits de integração. PRs exclusivamente de versão/changelog criadas pela App entram em auto-merge protegido depois de validação e checks, sem fechar/reabrir ou merge manual de rotina; veja o [procedimento de release](docs/operations.md). Isto não ativa auto-merge de dependências.

Não há licença escolhida nem acordo de contribuição implícito definido aqui. Discuta a política de licença com os responsáveis antes de contribuir material de terceiros; não acrescente uma licença por presunção nem copie código sem autorização compatível.

## Ambiente e provas

Comece por `make setup` e `make dev`, ou pelo percurso PostgreSQL externo do [README](README.md). Use uv e pnpm; não crie lockfiles de gestores concorrentes.

Antes da PR, conforme a superfície alterada:

```sh
make check test
pnpm exec playwright install --with-deps chromium
make e2e
make audit
```

Alterações no runtime/container exigem também `make container` e um smoke num ambiente descartável. `scripts/container-smoke.sh` destina-se ao runner Linux/DB descartável do CI, não a uma base de produção. Alterações visuais precisam de observação real da interface, incluindo viewport móvel, teclado e alternativa ao grafo. Testes não substituem essa observação. Registe qualquer dependência externa indisponível e a verificação que ficou por fazer.

Alterações Railway pertencem ao único `.railway/railway.ts` de todo o projeto, com SDK fixado no lock pnpm e CLI externa na versão exata documentada. Não acrescente configuração por serviço, partials ou segredos literais. Descreva os recursos afetados e riscos de eliminação/volume na PR; o mantenedor segue o [plan/apply revisto](docs/operations.md) com autenticação privada. `preserve()` só conserva valores já existentes. O merge pode implantar a aplicação por GitHub/Wait for CI, mas não aplica IaC nem autoriza automaticamente alterações destrutivas. Não introduza workflow privilegiado, token Railway ou PAT de deploy no GitHub.

Prefira código direto, módulos com responsabilidades claras e reutilização dos seletores/serviço de publicação. Não acrescente abstrações, endpoints, filas ou jobs para necessidades hipotéticas.

### Ferramentas Python: uma responsabilidade por ferramenta

**uv** é o único gestor de dependências/ambiente Python. **Ruff 0.16.9** é o único formatador Python e também verifica lint/imports; não acrescente Black, isort ou outro pipeline paralelo. `make format` aplica os fixes seguros de lint e escreve a formatação; `make lint` e `make check-format` verificam respetivamente regras e formatação sem alterar ficheiros. A formatação de exemplos Python em docstrings está ativada (`docstring-code-format = true`).

A seleção estável `E4`, `E7`, `E9`, `F`, `I`, `UP`, `B`, `SIM`, `C4`, `DJ`, `S`, `RUF` procura erros de correção, imports inconsistentes, sintaxe desatualizada, bugs comuns, complexidade evitável, compreensões desnecessárias, problemas Django/segurança e supressões não usadas. É uma seleção explícita e revista, não `ALL`: não ativamos preview, unsafe fixes nem regras de whitespace/`E501` que concorram com o formatador. A [documentação do linter](https://docs.astral.sh/ruff/linter/) recomenda seleção explícita e explica a diferença entre correções seguras e inseguras; a [documentação do formatador](https://docs.astral.sh/ruff/formatter/) explica a formatação e os exemplos em docstrings.

**Pyrefly 1.3.1**, com `django-stubs` simples, é o verificador de tipos Python. O seu [suporte nativo a Django](https://pyrefly.org/en/docs/django/) infere modelos, campos e relações sem um plugin adicional; cobre um subconjunto do ORM, não todos os comportamentos dinâmicos. A configuração usa explicitamente o preset `default`, verifica corpos de funções sem anotações e infere retornos, abrangendo Python escrito à mão na aplicação, testes, scripts e infraestrutura; apenas migrações numeradas geradas ficam excluídas. `make typecheck` verifica Python; `make check` executa-o e inclui TypeScript através de `pnpm check`. Prefira anotações honestas e correções locais; não silencie categorias inteiras nem esconda erros com `Any` ou supressões globais. Uma exceção pontual deve indicar a limitação e ser revista; supressões de tipos não usadas são assinaladas.

Estas referências justificam a escolha de ferramentas; não são evidência de que os checks deste repositório tenham sido executados.

## Contratos de dados e migrações

- Todo o caminho público usa a mesma fronteira de publicação. Não serialize modelos inteiros, notas privadas, utilizadores revisores ou coleções de evidência sem filtro.
- Conteúdo editado tem de ser revisto de novo. Uma importação futura não pode publicar com SQL, `bulk_update` ou atribuição de estado.
- Trate slugs/UUIDs como públicos, nunca como autorização. Verifique a visibilidade ao resolver um identificador.
- Gere e reveja migrações PostgreSQL; inclua-as na mesma PR do modelo. Planeie compatibilidade e rollback para dados existentes.
- Não coloque credenciais, dumps ou documentos-fonte em Git. Remover um ficheiro não revoga um segredo; comunique e rode-o.
- Fixtures são determinísticas, isoladas e explicitamente fictícias. Não crie um comando de produção para carregar demonstrações.

## Trabalho com agentes e worktrees

Cada tarefa tem um dono de integração e um conjunto de ficheiros exclusivo. Para trabalho concorrente, use worktrees Git separados, por exemplo:

```sh
git worktree add ../pt-ligacoes-grafo -b feat/grafo-acessivel
```

Comunique interfaces partilhadas antes de editar: esquema ORM, URLs/contextos de templates, seletores de navegador e comandos. Um único responsável coordena dependências, lockfiles, migrações, configuração de CI e integração final. Não deixe dois agentes reescrever o mesmo ficheiro sem transferência explícita de propriedade.

Agentes não recebem autorização implícita para ler segredos do host, criar contas, fazer push, alterar infraestrutura, publicar dados ou realizar deploy. O responsável fornece apenas o acesso necessário e revê o diff. Configuração local `.agents/`, `.claude/`, `.omp/` e `skills-lock.json` não pertence à publicação deste repositório; preserve o conteúdo local e mantenha-o ignorado.

Enquanto slices independentes estão a ser editados, não execute gates globais sobre alterações incompletas de outros agentes. O dono de integração executa a verificação final depois de integrar, resolve conflitos e observa o caminho alterado. Alterações inesperadas pertencem ao autor respetivo: não as apague para limpar um diff. Registe resultados reais e limitações, sem métricas de cobertura inventadas.
