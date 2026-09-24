# Segurança

## Reportar em privado

Use **[GitHub Private Vulnerability Reporting](https://github.com/TheRockPusher/pt_ligacoes/security/advisories/new)** na secção Security → Report a vulnerability deste repositório. Não publique uma issue com exploração, credenciais, dados pessoais expostos ou conteúdo editorial privado. Não há endereço de email de segurança inventado nem prazo de resposta garantido neste projeto.

Inclua, com minimização de dados:

- versão/commit e ambiente afetado;
- pré-condições, caminho afetado e passos reprodutíveis;
- impacto observado e distinção entre observação e hipótese;
- exemplo sintético ou evidência redigida, sem segredos completos;
- uma sugestão de correção, se a tiver.

Se o botão privado não estiver disponível, não converta o relatório numa publicação de detalhes. Pode abrir uma issue neutra a solicitar que os mantenedores disponibilizem o canal privado, **sem identificar a vulnerabilidade ou pessoas afetadas**. O canal depende da configuração GitHub; a presença deste ficheiro não o ativa.

Teste apenas em sistemas que lhe pertencem ou para os quais recebeu autorização. Uma política pública não autoriza ataques a Railway, GitHub, fontes externas ou utilizadores. Evite acesso adicional a dados, enumeração em massa, indisponibilidade e alterações persistentes. Pare assim que tiver evidência suficiente. Não existe programa de recompensa ou promessa de imunidade jurídica.

## Suporte e resposta

A linha mantida é `main`; versões históricas não têm backports prometidos. Não confunda uma dependência fixada com uma dependência permanentemente segura. Uma vulnerabilidade confirmada deve levar a contenção, correção revista, testes de regressão apropriados, rotação de segredos quando aplicável e comunicação coordenada. Não será publicado um detalhe identificável desnecessário para explicar a correção.

Exposição de dados pessoais exige tratar as obrigações legais e de notificação aplicáveis, além da correção técnica. Retirar uma página não remove informação de caches, backups, exportações ou repositórios de terceiros.

## Modelo de ameaça

### Ativos e adversários

Os ativos principais são a integridade das afirmações públicas, a confidencialidade de rascunhos/notas/revisões, credenciais editoriais, base de dados e cadeia de build/deploy. Consideramos visitantes maliciosos, URLs/nomes/passagens hostis, contas editoriais comprometidas, alterações que contornam revisão, contribuições/updates de dependências maliciosos e erros de configuração de infraestrutura.

### Controlos implementados

- Publicação transacional com revalidação de permissão ativa, entidades públicas e evidência sobre fonte pública. Estado publicado não basta: as leituras verificam novamente revisão e visibilidade.
- Edição editorial invalida revisão. Evidência não pública não é projetada; resolver UUID/slug não concede acesso a conteúdo privado.
- Templates escapam texto, grafo trata rótulos como dados, CSP restringe scripts à própria origem. HTMX não pode avaliar expressões nem executar tags de script; não há CDN de runtime.
- Fontes aceitam apenas HTTP(S), sem credenciais ou endpoints locais/literais não públicos. O servidor **não faz fetch** das URLs de fontes.
- Rotas públicas não aceitam escrita; CSRF continua ativo. Não há signup, uploads ou URLs de media públicos. Admin está desligado por omissão em produção.
- Configuração de produção falha sem segredo forte, hosts exatos e PostgreSQL explícito. HTTPS/cookies seguros/HSTS, cabeçalhos contra framing e content sniffing, container não-root e base de dados privada fazem parte do desenho operacional.
- Coleções e pesquisa limitadas, timeouts de base/processo, CI com PostgreSQL e navegador, auditorias de dependências e scan de histórico. Ações/imagens fixadas e tokens mínimos reduzem a superfície de supply chain.
- Infraestrutura declarada num único `.railway/railway.ts`, sem valores secretos. `preserve()` depende de valores privados previamente configurados em Railway. Deploy da aplicação via GitHub/Wait for CI não aplica IaC; plan/apply requerem revisão do mantenedor, sem token Railway/PAT de deploy nos secrets GitHub.
- Proteção remota de `main` com PR/checks `ci` e `CodeQL`, incluindo administradores, CodeQL extended, secret scanning/push protection e canal privado de vulnerabilidades. A configuração inicial foi confirmada no GitHub; o mantenedor deve preservar e rever estes controlos.

### Limitações que permanecem

- Não há garantia de exatidão editorial, completude ou ausência de danos reputacionais. Evidência exige juízo humano e revisão jurídica quando apropriado.
- Não há MFA, defesa anti-bot, rate limiting distribuído, WAF, deteção de intrusão ou resposta automática a incidentes implementados. Acesso editorial público exige controlos operacionais adicionais.
- Um operador com acesso SQL pode contornar hooks de invalidação de conteúdo e adulterar dados/auditoria. O histórico de revisão não é um registo criptograficamente inviolável.
- A validação de URL não fixa DNS nem impede um domínio público de mudar. Não existe fetch hoje; acrescentá-lo exige nova defesa SSRF com controlo de DNS, redirects, endereços de saída, limites e tipos de conteúdo. Abrir um link externo continua a ser navegação para uma origem não controlada.
- CSP admite estilos inline necessários à renderização do grafo. As permissões de script são mais restritas; não adicionar `unsafe-inline` ou `unsafe-eval` para contornar um erro de frontend.
- O proxy TLS é uma fronteira de confiança. Expor Gunicorn diretamente ou aceitar cabeçalhos encaminhados de clientes não fiáveis invalida a política de transporte.
- Limites por consulta não impedem uma sequência arbitrariamente grande de pedidos. Backups, retenção de logs da plataforma, segurança de contas GitHub/Railway e restauros requerem configuração e acompanhamento humanos.
- IaC controla todo o projeto: omitir um recurso pode eliminá-lo. Mudanças de localização, remoção ou redução de volumes podem afetar dados e exigem autorização específica e recuperação planeada, nunca confirmação destrutiva indiscriminada. Rede PostgreSQL privada, role restrito da aplicação, região EU West e Wait for CI foram confirmados na instalação inicial; o ficheiro versionado não garante que permaneçam ativos. As duas diferenças conhecidas do importador IaC estão documentadas em [operação](docs/operations.md) e não autorizam ignorar outras alterações.
- Auditorias detetam vulnerabilidades conhecidas e padrões de segredos, não todos os ataques. Uma revisão profunda reduz incerteza, não certifica ausência de falhas.

Ver [arquitetura](docs/architecture.md), [operação](docs/operations.md) e [metodologia](docs/methodology.md) para responsabilidades e fronteiras adicionais.
