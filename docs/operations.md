# Operação, CI e releases

## Variáveis reais

A aplicação não carrega `.env` em produção. Os comandos locais usam `scripts/with-env.sh`; Railway fornece variáveis ao processo. Nunca guardar segredos nos ficheiros de configuração versionados, logs ou comentários de PR.

| Variável | Uso |
| --- | --- |
| `DJANGO_SETTINGS_MODULE` | Produção por omissão: `config.settings.production`; desenvolvimento explícito: `config.settings.development`; testes: `config.settings.test` |
| `SECRET_KEY` | Obrigatória em produção; única, aleatória, pelo menos 50 caracteres, com diversidade de caracteres e sem prefixo `django-insecure-` |
| `ALLOWED_HOSTS` | Obrigatória em produção: hostnames públicos exatos separados por vírgulas; sem esquema, porta, localhost ou wildcard |
| `DATABASE_URL` | Obrigatória: URL PostgreSQL com utilizador, host e base; usar rede privada e credenciais de produção dedicadas |
| `CSRF_TRUSTED_ORIGINS` | Lista de origens exatas; em produção apenas HTTPS, sem caminhos ou wildcards; configurar apenas as necessárias |
| `ENABLE_ADMIN` | Apenas `true` ativa a rota; omissão em produção é `false` |
| `PORT` | Porta de escuta Gunicorn, fornecida por Railway; omissão `8000` |
| `WEB_CONCURRENCY` | Processos Gunicorn; omissão `2`, com quatro threads cada; ajustar só com medição |
| `POSTGRES_PASSWORD` | Apenas PostgreSQL Compose local; gerada por `make env` |
| `E2E_DATABASE_URL` | Apenas navegador/testes; base separada terminada em `_e2e` |

Os settings de teste têm segredo determinístico próprio e não devem servir produção. O gerador `.env` ativa o admin **local** para trabalho editorial; não cria contas. Não reutilizar estes valores ou o utilizador local com `CREATEDB` na base de produção.

## Railway: um serviço web e PostgreSQL

`railway.json` e `infra/Dockerfile` descrevem um único container Django/Gunicorn não-root. Os recursos Vite são compilados na imagem; `infra/start.sh` valida configuração, executa `collectstatic` e inicia Gunicorn. Não há serviço frontend ou processos de background separados. O comando de pré-deploy aplica `python apps/platform/manage.py migrate --noinput`; `/healthz/` verifica prontidão real da base e devolve apenas `ok` ou `unavailable`.

Configuração externa que o operador tem de confirmar:

1. Um projeto Railway dedicado, serviço PostgreSQL privado e serviço web ligado ao repositório correto, branch `main`, contexto de build na raiz e Dockerfile `infra/Dockerfile`.
2. Referência da variável `DATABASE_URL` para PostgreSQL, `SECRET_KEY` forte gerada fora de Git, domínio explícito em `ALLOWED_HOSTS`, origem HTTPS em `CSRF_TRUSTED_ORIGINS` e `ENABLE_ADMIN=false`.
3. Domínio HTTPS, caminho de healthcheck `/healthz/`, migração de pré-deploy e **Wait for CI** ativado na integração GitHub. Não adicionar um segundo workflow que faça upload/deploy da mesma branch.
4. GitHub branch protection/ruleset exige PR e o check agregado `ci`, com squash merge. Estas opções são estado da plataforma, não são ativadas apenas por existir YAML no repositório.
5. Backups e ensaio de restauro, limites de recursos, alertas e retenção compatíveis com a operação real. Não assumir que o template PostgreSQL os ativou.

Gunicorn não deve ser publicado por TCP nem acessível diretamente por uma origem não fiável. A produção confia em `X-Forwarded-Proto` substituído pelo ingresso Railway, força HTTPS e utiliza cookies seguros e HSTS. A exceção de redirecionamento de `/healthz/` permite a sonda interna HTTP; ela não revela diagnósticos.

A primeira instalação vazia é intencional. Não carregar a fixture E2E, criar superutilizador automaticamente ou introduzir pessoas reais para fazer a aplicação parecer preenchida. Caso seja necessário abrir o admin, limite primeiro o acesso a operadores, crie credenciais fortes por canal protegido e retire a exposição quando desnecessária. Não há MFA nativo nem rate limiting de login implementado; não trate um URL oculto como controlo de acesso.

## CI e decisão de deploy

O workflow de CI executa verificação de lock, lint/formatação, tipos, checks/migrações Django, pytest com PostgreSQL, auditoria de dependências, jornadas Chromium desktop/mobile, compilação de imagem e smoke do container real. Um job separado procura segredos no histórico Git. O check `ci` só fica verde se os jobs exigidos tiverem sucesso. O scan não substitui revisão de diffs nem garante que nunca houve um segredo exposto.

O workflow de release tem permissões de escrita limitadas à criação de PR/tag/release; não executa código do PR. O CI de contribuições mantém permissões de leitura e não recebe segredos Railway. Actions e imagens base estão fixadas por SHA/digest. O lock pnpm utiliza atraso mínimo de disponibilidade de pacotes; exceções exatas devem ser justificadas e revistas.

Um merge humano em `main` desencadeia CI. Railway só deve implantar esse commit depois de a integração confirmar os checks exigidos. **Uma tag de release não é autorização para ignorar CI e não é um mecanismo paralelo de deploy.** Confirme no painel o SHA e o estado de cada deploy; a presença deste workflow não comprova a saúde do serviço remoto.

## Versões e Release Please

A versão canónica da aplicação está em `pyproject.toml`. Release Please mantém a PR de release, notas e versão correspondente no lock/manifesto configurados; o mantenedor deve rever que continuam coerentes. Títulos Conventional Commits dos squash merges alimentam a classificação da release. Não aumentar manualmente versões em ficheiros independentes sem necessidade.

O token padrão `GITHUB_TOKEN` não provoca automaticamente os eventos normais de CI quando o bot abre ou atualiza a PR de release. O processo sem PAT é explícito:

1. Permitir nas definições de Actions a criação de PRs pelo workflow.
2. Depois de **cada atualização do bot**, um mantenedor fecha e reabre a PR de release para provocar `pull_request` CI.
3. Rever o diff e esperar pelo check obrigatório `ci` dessa PR. Um `workflow_dispatch` genérico não substitui o check exigido no commit da PR.
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
