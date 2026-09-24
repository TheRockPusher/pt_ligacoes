# Operação, CI e releases

## Variáveis reais

A aplicação não carrega `.env` em produção. Os comandos locais usam `scripts/with-env.sh`; Railway fornece variáveis ao processo. Nunca guardar segredos nos ficheiros de configuração versionados, logs ou comentários de PR.

| Variável | Uso |
| --- | --- |
| `DJANGO_SETTINGS_MODULE` | Produção por omissão: `config.settings.production`; desenvolvimento explícito: `config.settings.development`; testes: `config.settings.test` |
| `SECRET_KEY` | Obrigatória em produção; única, aleatória, pelo menos 50 caracteres, com diversidade de caracteres e sem prefixo `django-insecure-` |
| `ALLOWED_HOSTS` | Obrigatória em produção: hostnames públicos exatos separados por vírgulas; sem esquema, porta, localhost ou wildcard |
| `DATABASE_URL` | Obrigatória: URL PostgreSQL privada com role restrito da aplicação; não usar o superutilizador de `Postgres.DATABASE_URL` |
| `CSRF_TRUSTED_ORIGINS` | Lista de origens exatas; em produção apenas HTTPS, sem caminhos ou wildcards; configurar apenas as necessárias |
| `ENABLE_ADMIN` | Apenas `true` ativa a rota; omissão em produção é `false` |
| `PORT` | Porta de escuta Gunicorn, fornecida por Railway; omissão `8000` |
| `WEB_CONCURRENCY` | Processos Gunicorn; omissão `2`, com quatro threads cada; ajustar só com medição |
| `POSTGRES_PASSWORD` | No desenvolvimento, gerada por `make env` para Compose; a credencial independente do serviço Postgres de produção permanece apenas em Railway |
| `E2E_DATABASE_URL` | Apenas navegador/testes; base separada terminada em `_e2e` |

Os settings de teste têm segredo determinístico próprio e não devem servir produção. O gerador `.env` ativa o admin **local** para trabalho editorial; não cria contas. Não reutilizar estes valores ou o utilizador local com `CREATEDB` na base de produção.

## Railway: um serviço web e PostgreSQL

O único `.railway/railway.ts` usa o SDK TypeScript `railway/iac` e descreve **todo o projeto**, incluindo serviços, variáveis preservadas e volume. Não é um partial nem configuração por serviço. `infra/Dockerfile` constrói um único container Django/Gunicorn não-root. Os recursos Vite são compilados na imagem; `infra/start.sh` valida configuração, executa `collectstatic` e inicia Gunicorn. Não há serviço frontend ou processos de background separados.

Configuração aplicada e confirmada na instalação de **2026-09-24**; alterações futuras exigem nova confirmação remota:

| Recurso | Contrato de produção |
| --- | --- |
| Projeto/ambiente | `pt-ligacoes` / `production` |
| `web` | Fonte `TheRockPusher/pt_ligacoes`, branch `main`, contexto de build na raiz e Dockerfile `infra/Dockerfile`; uma réplica em Amsterdam/EU West (`europe-west4`) |
| Pré-deploy/start | `timeout --kill-after=5s 300s python apps/platform/manage.py migrate --noinput`; `sh infra/start.sh` |
| Prontidão/restart | `/healthz/`, timeout 120 s; reinício on-failure, máximo 3 tentativas |
| `Postgres` | Imagem `ghcr.io/railwayapp-templates/postgres-ssl:18`; uma réplica na mesma região, sem domínio público nem TCP proxy |
| `postgres-volume` | Preservar volume e dados existentes, em Amsterdam/EU West (`ams`/`europe-west4`), montado em `Postgres` em `/var/lib/postgresql/data` |

PostgreSQL **17** continua a ser a versão de desenvolvimento/CI; não alterar os seus comandos ou credenciais para apontar à produção. `/healthz/` verifica prontidão real da base e devolve apenas `ok` ou `unavailable`.

A instalação pública está em <https://web-production-ca58.up.railway.app>. Foram observados PostgreSQL **18.6**, migrações aplicadas, ligação TLS com o role `pt_ligacoes` à sua própria base, sem privilégios de superutilizador/criação de bases ou roles/replicação/bypass RLS, e ausência de domínio/TCP proxy no Postgres. Não foram criados utilizadores, fontes, entidades, relações ou evidência em produção; o admin permanece desligado. Backups/restauro ensaiado e limites operacionais continuam a ser pré-condições para introduzir dados reais, não resultados desta instalação.

O SDK fixado não expõe `preDeployTimeoutSeconds`. O limite é aplicado pelo comando versionado com GNU `timeout`: termina a migração aos 300 s e força a sua paragem 5 s depois se necessário. O smoke Docker executa o mesmo comando; não há campo inventado nem configuração legada paralela. O timeout de healthcheck de 120 s é um parâmetro distinto.

Antes de permitir deploys automáticos, confirmar também:

1. Um role de aplicação dedicado, sem `SUPERUSER`, `CREATEDB` ou `CREATEROLE`, com os privilégios necessários apenas à sua base/esquema e às migrações Django. Configurar a sua URL privada em `web.DATABASE_URL`; **não** referenciar a URL de superutilizador `Postgres.DATABASE_URL`. Preservar as variáveis importadas do template Postgres sem as copiar para a aplicação.
2. `SECRET_KEY` forte gerada fora de Git, domínio explícito em `ALLOWED_HOSTS`, origem HTTPS em `CSRF_TRUSTED_ORIGINS` e `ENABLE_ADMIN=false`. A sonda Railway também exige o host exato `healthcheck.railway.app` em `ALLOWED_HOSTS`, não em `CSRF_TRUSTED_ORIGINS`. Configurar valores privados diretamente em Railway; não copiar `.env` local.
3. Domínio HTTPS do `web`, healthcheck, pré-deploy e **Wait for CI** ativado na integração GitHub. Não adicionar um segundo workflow que faça upload/deploy da mesma branch.
4. GitHub branch protection exige PR, branch atualizada e os checks `ci` (GitHub Actions) e `CodeQL` (GitHub code scanning), com squash merge, histórico linear e resolução de conversas. Está aplicada também a administradores, sem force push/eliminação; não exige uma segunda aprovação indisponível num projeto com um único mantenedor. Estas opções são estado da plataforma, não são ativadas apenas por existir YAML no repositório.
5. Backups e ensaio de restauro, limites de recursos, alertas e retenção compatíveis com a operação real. Não assumir que o template PostgreSQL os ativou.

Gunicorn não deve ser publicado por TCP nem acessível diretamente por uma origem não fiável. A produção confia em `X-Forwarded-Proto` substituído pelo ingresso Railway, força HTTPS e utiliza cookies seguros e HSTS. A exceção de redirecionamento de `/healthz/` permite a sonda interna HTTP; ela não revela diagnósticos.

O processo web usa UID **10001**; aplicação e dependências permanecem propriedade de root. Apenas os diretórios necessários são graváveis pelo utilizador da aplicação, incluindo `/home/app` (`0700`) e o diretório de estáticos. Gunicorn usa um socket **Unix** privado em `/home/app/.gunicorn/gunicorn.ctl` (`0600`), não uma porta de controlo pública. Uma sessão administrativa Railway SSH pode ter UID 0: confirme o UID de PID 1, não confunda a sessão de diagnóstico com o processo web. Chaves criadas apenas para bootstrap devem ser revogadas e removidas após a verificação.

A primeira instalação vazia é intencional. Não carregar a fixture E2E, criar superutilizador automaticamente ou introduzir pessoas reais para fazer a aplicação parecer preenchida. Caso seja necessário abrir o admin, limite primeiro o acesso a operadores, crie credenciais fortes por canal protegido e retire a exposição quando desnecessária. Não há MFA nativo nem rate limiting de login implementado; não trate um URL oculto como controlo de acesso.

### IaC: revisão e aplicação explícitas

[Infrastructure as Code](https://docs.railway.com/infrastructure-as-code) TypeScript está disponível de forma geral; consulte a [referência do SDK](https://docs.railway.com/infrastructure-as-code/reference). O [Config as Code legado](https://docs.railway.com/config-as-code), `railway.json`/`railway.toml`, está descontinuado: **serviços novos não o podem usar**, e os ficheiros existentes deixam de ser lidos em **2026-12-01**. Não manter configuração dupla. Ao migrar um serviço antigo, o operador também tem de remover a definição remota de Config File; apagar o ficheiro no Git não limpa essa definição.

Execute a partir da raiz, com Node/pnpm nas versões fixadas no repositório. `pnpm install --frozen-lockfile` instala o SDK **`railway` 3.11.0**, fixado em `package.json`/`pnpm-lock.yaml` e importado como `railway/iac`. A **CLI 5.62.1** é uma ferramenta externa ao repositório, não uma dependência da aplicação; instale essa versão exata via pnpm e confirme o executável escolhido pelo `PATH`. Se já estiver instalada nessa versão, não é necessário reinstalá-la. O diretório global de binários pnpm tem de estar no `PATH` (configure-o com `pnpm setup` se necessário). Apenas um mantenedor autorizado autentica e liga a sua sessão privada ao projeto/ambiente:

```sh
pnpm install --frozen-lockfile
pnpm add --global @railway/cli@5.62.1
command -v railway
railway --version # confirmar 5.62.1 antes de continuar
railway login
railway link --project pt-ligacoes --environment production
railway status
make infra-plan
```

`make infra-plan` e `make infra-apply` chamam respetivamente `railway config plan --file .railway/railway.ts` e `railway config apply --file .railway/railway.ts`, sem flags de confirmação automática. Não usam uma CLI obtida dinamicamente em cada execução.

Confirme projeto e ambiente antes de avaliar IaC: o nome no ficheiro não substitui a ligação da CLI. Reveja previamente o código e as dependências, pois a avaliação TypeScript executa código local com a sessão do operador; não execute IaC de uma PR não fiável com credenciais.

Reveja o plano completo, em particular nomes/identidades dos recursos, imagem, região/réplicas, mounts, variáveis, networking e qualquer criação/eliminação. Depois de revisão e autorização explícitas, o mantenedor executa:

```sh
make infra-apply
```

O `apply` normal calcula um plano novo e pede confirmação: reveja também esse plano, não assuma que corresponde ao anterior. Se o estado remoto mudar ou o plano ficar obsoleto, interrompa e volte a planear/rever. Para fixar exatamente um plano revisto, a CLI permite `config plan --out <ficheiro-privado>` e `config apply --plan <ficheiro-privado>`; guarde o artefacto fora do repositório e de `.railway/`, com acesso restrito. Pode conter segredos mesmo quando a saída do terminal está redigida. Não o publique em PRs, logs ou artefactos públicos.

`preserve()` significa **manter o valor já existente em Railway**, não criar um segredo, usar um fallback ou importar o `.env`. Antes do primeiro apply, os valores privados necessários têm de existir no serviço correto. Preserve todas as variáveis importadas do template Postgres e o volume; nunca substitua segredos por texto de exemplo. Não use `config pull --include-variables` nem `plan --show-values` neste fluxo: podem expor credenciais.

**O ficheiro é autoritativo para todo o projeto: omitir um recurso pode eliminá-lo.** Não o reduza ao serviço web nem exporte um partial para esconder diferenças. Desanexar, apagar, reduzir ou mudar a localização de um volume pode afetar dados e é destrutivo; mudar a versão major PostgreSQL também exige um plano de migração de dados, não apenas trocar a imagem. Antes destas operações, exigir backup/restauro ensaiado, impacto e recuperação definidos e autorização específica. Nunca usar confirmação destrutiva em lote ou acrescentar `--yes`/`--confirm-destructive` para fazer um plano inesperado passar.

Aplicar IaC pode desencadear alterações/redeploys de infraestrutura; não é um check inofensivo. Depois de um apply autorizado, confirme o estado remoto e um novo plano sem diferenças inesperadas. Registe apenas evidência não secreta: SHA, estado do deploy, URL HTTPS validada, checks observados e eventuais bloqueios.

Limitações observadas na combinação **CLI 5.62.1 / SDK 3.11.0**:

- Depois do apply, o plano repete duas diferenças de representação: `web.restartPolicyType` de `null` para `ON_FAILURE` e o mount do volume Postgres de `null` para o volume/caminho existentes. A consulta direta confirmou restart `ON_FAILURE`/3 e o mesmo volume anexado em `/var/lib/postgresql/data`; o importador classifica Postgres como database e omite o mount. O plano não indicou criação ou eliminação. Não remover a política/mount desejados nem aceitar qualquer outra diferença como inofensiva.
- Postgres usa `service("Postgres")`, não o helper `postgres()`: nesta CLI o helper introduz um TCP proxy público e variáveis de template. Preservar o serviço privado e o volume existentes é deliberado.
- O guard de versão do SDK consulta a variável de shell `_`. Um wrapper `env … railway config …` pode fazê-lo executar `/usr/bin/env` e alegar incorretamente uma CLI antiga. Use os comandos Make, cujo Bash resolve a CLI corretamente; num wrapper direto, `env -u _ … railway config …` mantém o guard real em vez de o desativar. Confirme sempre `railway --version`.

### Aplicação e infraestrutura são decisões diferentes

Pushes/merges em `main` implantam **a aplicação** através da integração GitHub do Railway com **Wait for CI**. A CLI é quem avalia e aplica IaC: um deploy GitHub **não lê nem aplica automaticamente** `.railway/railway.ts`. Mudanças nesse ficheiro requerem o fluxo manual revisto acima, coordenado com a compatibilidade da aplicação.

Não existe workflow privilegiado de plan/apply automático nem token Railway/PAT de deploy nos secrets GitHub. A sessão privada do mantenedor serve apenas as operações explicitamente autorizadas; CI de contribuições não recebe essas credenciais. A presença de IaC, CI ou documentação não prova que houve um apply/deploy bem-sucedido nem que a integração remota está configurada.

## CI e decisão de deploy

O workflow de CI executa verificação de lock, lint/formatação, tipos, checks/migrações Django, pytest com PostgreSQL, auditoria de dependências, jornadas Chromium desktop/mobile, compilação de imagem e smoke do container real. Um job separado procura segredos no histórico Git. O check `ci` só fica verde se os jobs exigidos tiverem sucesso. O scan não substitui revisão de diffs nem garante que nunca houve um segredo exposto.

GitHub CodeQL está também ativo em configuração `extended` para Python, JavaScript/TypeScript e Actions, com o check `CodeQL` obrigatório. Secret scanning, push protection, alertas de dependências e Private Vulnerability Reporting foram ativados. O token padrão de Actions tem leitura; apenas o job de release recebe a escrita necessária. Estes controlos remotos devem ser revistos periodicamente.

O workflow de release tem permissões de escrita limitadas à criação de PR/tag/release; não executa código do PR. O CI de contribuições mantém permissões de leitura e não recebe segredos Railway. Actions e imagens base estão fixadas por SHA/digest. O lock pnpm utiliza atraso mínimo de disponibilidade de pacotes; exceções exatas devem ser justificadas e revistas.

Um merge humano em `main` desencadeia CI. Railway só deve implantar esse commit depois de a integração confirmar os checks exigidos. **Uma tag de release não é autorização para ignorar CI e não é um mecanismo paralelo de deploy.** Confirme no painel o SHA e o estado de cada deploy; a presença deste workflow não comprova a saúde do serviço remoto.

## Versões e Release Please

A versão canónica da aplicação está em `pyproject.toml`. Release Please mantém a PR de release, notas e versão correspondente no lock/manifesto configurados; o mantenedor deve rever que continuam coerentes. Títulos Conventional Commits dos squash merges alimentam a classificação da release. Não aumentar manualmente versões em ficheiros independentes sem necessidade.

O extra-file TOML seleciona `$.package[?(@.name.value=='pt-ligacoes')].version`: o parser do Release Please **17.6.0**, incluído na action v5 fixada, representa os escalares como objetos com `value`. Comparar `@.name` diretamente não seleciona o pacote e deixa o lock antigo; o check `uv lock --check` bloqueia essa PR. Não fixar o índice do pacote, que muda com as dependências. Ao atualizar a action, confirmar na PR gerada que `pyproject.toml` e o pacote raiz de `uv.lock` mudam juntos.

O token padrão `GITHUB_TOKEN` não provoca automaticamente os eventos normais de CI quando o bot abre ou atualiza a PR de release. O processo sem PAT é explícito:

1. Permitir nas definições de Actions a criação de PRs pelo workflow.
2. Depois de **cada atualização do bot**, um mantenedor fecha e reabre a PR de release para provocar `pull_request` CI.
3. Rever o diff e esperar pelos checks obrigatórios `ci` e `CodeQL` dessa PR. Um `workflow_dispatch` genérico não substitui o check exigido no commit da PR.
4. Fazer squash merge humano. Release Please publica a tag/release segundo a configuração; o merge em `main` continua sujeito ao CI e ao Wait for CI do Railway.

Não adicionar PAT de larga permissão para ocultar esta limitação. Não há promessa de uma release publicada antes de a automação remota ser observada.

## Atualizações de dependências

Dependabot está configurado apenas para os ecossistemas cuja compatibilidade é conhecida nesta configuração: GitHub Actions, Docker e Docker Compose. Não há promessa de PRs automáticas de Python ou pnpm para versões de ferramentas que o serviço ainda não suporta.

Os mantenedores gerem upgrades Python/browser:

```sh
uv lock --upgrade
pnpm update --latest --interactive
make check test e2e audit
```

O primeiro comando respeita os intervalos Python declarados. O segundo permite selecionar explicitamente atualizações, incluindo alterações de major: não as aceite em lote sem rever `package.json`, lockfiles, changelogs e compatibilidade. Atualize também pins de ferramentas, CI e imagens quando necessário. CI audita dependências nos eventos configurados e numa execução semanal; advisories podem aparecer entre execuções e falhas de rede não são resultados limpos.

## Migrações, rollback e incidentes

Preferir alterações de esquema compatíveis com a versão anterior durante a transição. Uma migração no pré-deploy pode ter sido aplicada mesmo se a nova versão falhar no healthcheck. Reverter a imagem não reverte a base automaticamente. Antes de uma alteração destrutiva: backup, restauro ensaiado, decisão explícita sobre indisponibilidade e plano específico de recuperação.

Para falhas: observar o SHA implantado, estado de deploy, logs de build/runtime e prontidão PostgreSQL. Não imprimir variáveis completas, cookies ou notas editoriais em tickets. Gunicorn não regista access logs de pedidos; a plataforma de ingresso pode fazê-lo, pelo que a sua retenção e acesso têm de ser configurados separadamente.

Para comprometimento: desligar exposição editorial quando necessário, revogar credenciais/sessões, rodar segredos e preservar evidência mínima com acesso restrito. Remover um segredo do último commit não o remove do histórico; rode-o primeiro e coordene a limpeza. Para retirada de conteúdo, ocultar dados pode ser uma contenção imediata, mas não elimina backups ou exportações. Consulte [SECURITY.md](../SECURITY.md) e a [metodologia](methodology.md).
