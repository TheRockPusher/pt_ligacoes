# Arquitetura e fronteiras de confiança

## Uma aplicação, uma base de dados

O repositório contém um monólito modular Django e o seu frontend compilado. PostgreSQL é o armazenamento durável. HTMX melhora a pesquisa e Cytoscape apresenta o grafo; nenhum deles decide se uma relação pode ser publicada. Vite é uma ferramenta de compilação, não um serviço de produção. Django/WhiteNoise serve o HTML e os recursos locais. Não há broker, workers, scraping automático, uploads, serviço de pesquisa externo ou inferência por IA.

O diretório `core` contém os modelos e o serviço transacional de publicação. `public` contém seletores de leitura, vistas e URLs. Configuração de processo e transporte está em `config`; templates não devem consultar coleções de evidência não filtradas.

## Modelo editorial

- **Entity**: identidade editorial com UUID interno, slug público único, nome, tipo e visibilidade. Pessoas, empresas, organizações e universidades não são misturadas por semelhança de nomes.
- **Source**: referência externa, título, editor, momento de consulta e data de publicação opcional. Guardar a referência não descarrega a página nem garante a sua permanência.
- **Relationship**: afirmação tipificada e dirigida entre duas entidades, descrição, limites temporais opcionais e estado `draft`, `published` ou `rejected`.
- **Evidence**: passagem que suporta uma relação, ligada a uma fonte, com referência de página opcional e visibilidade própria.
- **ReviewEvent**: registo de publicação/invalidação e atribuição de revisão. Não aparece na projeção pública. A imutabilidade na aplicação não é um log à prova de adulteração por um administrador da base de dados.

`description` é texto editorial potencialmente público. `private_notes`, utilizadores revisores e respetivos identificadores não são apresentados no HTML nem serializados no JSON público. Informação não necessária deve ser omitida, não escondida como se o campo privado fosse um cofre.

## Publicação e retirada

`ligacoes.core.services.publish_relationship` volta a carregar os registos persistidos numa transação e exige uma pessoa revisora ativa com a permissão `core.publish_relationship`, entidades públicas e pelo menos uma evidência pública sobre fonte pública. Verifica auto-relações e intervalos invertidos, atribui a revisão e acrescenta o evento de auditoria. Não se publica com uma simples alteração do campo `status`.

Uma edição editorial numa relação publicada invalida a revisão e regressa a `draft`. Alterações relevantes de entidade, fonte ou evidência também invalidam as relações afetadas. Alterar apenas notas privadas não equivale a aprovar uma nova afirmação. É necessária revisão de novo para voltar a publicar.

À escala editorial atual, de baixo débito, `Entity.save`, `Source.save`, `Relationship.save`, `Evidence.save`/`delete` e `publish_relationship` partilham um único advisory lock transacional PostgreSQL, com namespace próprio. O lock é adquirido antes dos locks de linha e mantido até ao commit da transação exterior; serializa edições e revisões para impedir comparações obsoletas e inversões da ordem de locks. A eliminação de evidência consulta a relação persistida, não a referência de uma instância antiga. Os seletores e pedidos públicos de leitura não adquirem este lock editorial.

Cada leitura pública volta a verificar o estado, a atribuição da revisão, a visibilidade das duas entidades e a existência de evidência/fonte pública. Assim, ocultar uma entidade, fonte ou evidência retira a afirmação mesmo que uma alteração em massa tenha ultrapassado os hooks editoriais. Uma referência de evidência só é acessível se essa evidência, a fonte e a relação forem todas publicamente visíveis. UUIDs e slugs são identificadores, **não** autorização.

Operações bulk, `QuerySet.update`/`delete` e SQL direto não são fluxos editoriais suportados: podem contornar a invalidação de conteúdo e esta serialização, embora as restrições de base de dados e os filtros de visibilidade permaneçam. A exceção são as escritas internas dos serviços já protegidas pela mesma transação e lock. Não conceder acesso SQL a quem só precisa de revisão. Qualquer futura importação tem de reutilizar os pontos de entrada editoriais suportados e provocar revisão, nunca fazer uma publicação por atualização em massa.

## Uma projeção pública consistente

`ligacoes.public.selectors.public_relationships(at=None)` é a origem partilhada para perfil e grafo. Filtra dinamicamente visibilidade e faz prefetch apenas de evidências públicas sobre fontes públicas. O limite é 100 relações por perfil/grafo e 10 evidências por relação no recorte pré-carregado; o grafo assinala truncagem. A lista de entidades usa páginas de 24. A pesquisa tem um máximo de 100 caracteres. Os limites controlam trabalho por pedido, não constituem proteção completa contra abuso de tráfego.

O filtro `at` exige `AAAA-MM-DD`; limites conhecidos são inclusivos. Um limite desconhecido não exclui automaticamente a relação nem passa a ser uma data confirmada. A interface avisa dessa incerteza. O total do grafo é o recorte apresentado, não uma contagem universal de todas as relações existentes.

## Fronteiras de segurança

1. **Internet → Django**: apenas leitura pública; autorização editorial só no admin opcional. CSRF permanece ativo, métodos de escrita públicos são recusados e a configuração de produção valida hosts e transporte.
2. **Conteúdo editorial → navegador**: HTML é escapado; JSON transporta dados, não código. Não há scripts de terceiros, scripts inline ou `eval`. O grafo não deve inserir nomes através de HTML. CSP permite scripts apenas da própria origem; os estilos inline necessários ao canvas/grafo são uma concessão explícita.
3. **URL de fonte → mundo externo**: apenas referências HTTP(S), sem credenciais e sem endpoints locais/literais privados. Não há fetch no servidor. Validação sintática não prova que um domínio nunca resolve para uma rede privada nem que o documento é verdadeiro ou seguro.
4. **Railway → Gunicorn**: TLS termina no proxy de confiança. A aplicação confia no cabeçalho de protocolo reescrito por esse proxy. Expor Gunicorn diretamente à Internet destruiria essa suposição.
5. **GitHub → deploy**: código não confiável de PR não recebe segredos de produção. O job de release com escrita não faz checkout/execução de código do PR. A integração GitHub do Railway com Wait for CI implanta a aplicação de `main`, não a infraestrutura. Não existe token Railway/PAT de deploy no GitHub; as opções externas têm de ser verificadas.
6. **Mantenedor → infraestrutura**: o único `.railway/railway.ts` declara todo o projeto, não uma configuração por serviço ou um partial. A CLI avalia-o apenas em `config plan`/`config apply`, com revisão e autorização explícitas. `preserve()` mantém valores privados já existentes, não provisiona segredos. Omissões podem eliminar recursos, incluindo armazenamento persistente; consulte o [procedimento de operação](operations.md).

A topologia pretendida de produção é `pt-ligacoes`/`production`, com `web` e `Postgres`, uma réplica de cada em Amsterdam/EU West (`ams`/`europe-west4`) e `postgres-volume` montado em `/var/lib/postgresql/data`. PostgreSQL usa a imagem `ghcr.io/railwayapp-templates/postgres-ssl:18`, sem domínio público nem TCP proxy. A aplicação usa um role restrito, não o superutilizador do template. Desenvolvimento e CI usam PostgreSQL 17. Esta descrição é intenção de configuração, não evidência de estado remoto.

## Crescimento sem infraestrutura especulativa

A aplicação vazia é útil para validar navegação e processo, não é uma promessa de cobertura jornalística. Não há identidade reconciliada automaticamente, extração, OCR, crawler ou integração com bases públicas. Acrescentar estas capacidades exigiria requisitos, análise jurídica, limites de rede e testes próprios. Só criar módulos, pacotes ou processos adicionais quando houver consumidores e responsabilidade concreta.
