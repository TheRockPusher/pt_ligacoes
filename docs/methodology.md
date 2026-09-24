# Metodologia editorial e tratamento de dados

## O que uma ligação significa

Uma ligação representa uma afirmação específica, tipificada, contextualizada no tempo e suportada por uma passagem documental. **Não implica favorecimento, coordenação, amizade ou irregularidade.** A ausência de uma ligação não prova a sua inexistência. Frequentar a mesma universidade, trabalhar na mesma organização em épocas diferentes ou ter o mesmo apelido não demonstra uma relação pessoal.

O catálogo inicial não contém registos reais. Não há ingestão, classificação ou publicação automática. Antes de acrescentar informação real, uma pessoa responsável deve definir finalidade de interesse público, fundamento jurídico, proporcionalidade e processo de correção, considerando RGPD, direitos de personalidade, direitos de autor e difamação. Este documento não substitui aconselhamento jurídico.

## Processo mínimo por afirmação

1. **Identificar a fonte**: preferir documento oficial ou primário e registar título, editor, URL, data de consulta, data de publicação quando conhecida e referência concreta da passagem.
2. **Reconciliar a identidade**: usar elementos contextuais suficientes, sem recolher identificadores excessivos. Homónimos permanecem separados até haver prova; sem correspondência segura, a relação fica em rascunho.
3. **Distinguir datas**: a data de publicação do documento não é automaticamente o início ou fim da relação. Manter limites desconhecidos como nulos; nunca inventar dia/mês para tornar um grafo mais completo.
4. **Redigir com precisão**: usar o tipo de relação mais fiel, limitar a descrição ao que a evidência demonstra e preservar contexto relevante, incluindo cessação ou contestação. Não apresentar hipótese como facto.
5. **Rever explicitamente**: verificar interesse público, fonte e passagem, identidade, datas e visibilidade de cada elemento. A pessoa revisora precisa da permissão de publicação. Um rascunho não é publicável só por estar guardado.
6. **Rever alterações**: corrigir conteúdo publicado invalida a revisão afetada. A nova versão só regressa à superfície pública depois de nova revisão.

As fontes são referências, não garantias. A aplicação não descarrega, arquiva ou verifica automaticamente documentos; uma URL pode mudar ou desaparecer. A passagem deve ser mínima e necessária. Não se devem copiar documentos inteiros para notas privadas ou Git.

## Família, informação sensível e inferências

O tipo `family` existe para uma relação documentada e de interesse público demonstrável; não autoriza gerar árvores familiares. **Não inferir parentesco automaticamente** de nomes, moradas, redes sociais, coocorrência, fotografia, escola ou modelo de linguagem. Relações familiares exigem avaliação editorial e jurídica reforçada, especialmente quando envolvem pessoas sem funções públicas.

Não recolher contactos privados, moradas residenciais, documentos de identificação, credenciais, dados de menores ou categorias especiais sem uma necessidade estrita e fundamento apropriado. A existência de informação numa página pública não elimina estes deveres. Não fazer do campo `private_notes` um depósito de informação sensível.

## Retificação e retirada

Para uma divergência factual não sensível, abra uma issue com o URL da página, a afirmação contestada e referência pública que suporte a correção. Não inclua dados pessoais adicionais. Para exposição de informação privada ou vulnerabilidade, use o canal privado em [SECURITY.md](../SECURITY.md).

A pessoa responsável deve avaliar o pedido, retirar preventivamente visibilidade quando adequado, corrigir a afirmação e submeter a nova revisão. Ocultar a entidade, fonte ou evidência torna inacessíveis as relações dependentes que deixem de satisfazer os critérios públicos. Publicar novamente não deve apagar a necessidade de explicar uma correção relevante. A aplicação não implementa prazos de resposta nem um sistema automático de gestão de pedidos.

## Retenção e minimização

- Conservar apenas passagens e metadados necessários para explicar a afirmação e a sua revisão. Reavaliar utilidade, atualidade e legalidade quando houver correção, retirada ou nova publicação; não manter informação indefinidamente por omissão editorial.
- A política concreta de prazos deve ser aprovada **antes de dados reais**, por categoria e finalidade. Não existe neste código um prazo universal justificado nem um job automático de expurgo.
- Retirada pública não é eliminação: dados editoriais e eventos de revisão podem continuar na base. Eliminação e anonimização têm de considerar as dependências, obrigações de auditoria e direitos das pessoas; uma operação autorizada deve abranger exportações e cópias relevantes.
- Backups e logs têm ciclos de retenção próprios. Configure-os, documente-os e limite acesso antes de operar com dados reais. Uma reposição deve voltar a aplicar retiradas e correções posteriores ao backup.
- Nunca versionar bases de dados, dumps, documentos-fonte, screenshots de conteúdo privado ou dados pessoais reais. Testes, demonstrações e reproduções usam nomes explicitamente fictícios.

## Como ler o recorte temporal

`?at=AAAA-MM-DD` mantém relações cujo início conhecido não seja posterior à data e cujo fim conhecido não seja anterior. Os limites são inclusivos. Limites desconhecidos permanecem possíveis nesse recorte e são assinalados: a visualização não confirma atividade numa data exata sem prova temporal. Um conjunto limitado/truncado também não autoriza concluir que o catálogo contém todas as relações de uma pessoa.
