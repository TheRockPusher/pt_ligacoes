# Official source research

Catalogue of official sources, their access and reuse status, and known blockers. Wire contracts live in the connectors under `apps/platform/ligacoes/core/`. A public, working endpoint is not permission to reuse it.

## Implemented

- **Assembleia da República**: [Informação Base](https://www.parlamento.pt/Cidadania/Paginas/DAInformacaoBase.aspx) (identities, constituencies, parliamentary groups, dated mandate states) and [Registo Biográfico](https://www.parlamento.pt/Cidadania/Paginas/DARegistoBiografico.aspx) (disclosed curricular information). [Reuse permitted with attribution](https://www.parlamento.pt/Cidadania/paginas/dadosabertos.aspx) to Assembleia da República. Biographies are not a complete employment history, and role text has no reliable organisation identifiers or dates, so extracted roles stay private candidates for human resolution.
- **Government composition**: [portugal.gov.pt](https://portugal.gov.pt/gc25/governo/composicao), read through the site's internal public frontend contracts. These are undocumented, unversioned and carry no reuse licence.
- **Entidade para a Transparência (EpT)**: [public access portal](https://entidadetransparencia.pt/), read through its undocumented public API. There are no stable declaration permalinks, so citations reference the portal plus exact declaration identifiers. Resolve reuse conditions with [EpT](https://www.tribunalconstitucional.pt/tc/ept/contactos.html). The [public-access regulation (Regulamento 258/2024)](https://files.diariodarepublica.pt/2s/2024/03/047000000/0012600137.pdf) separates published interests from requested consultation. Income/assets and associative affiliations are out of scope.

## Deferred

| Source | Provides | Blocker |
| --- | --- | --- |
| [IRN Publicações](https://registo.justica.gov.pt/Empresas/Publicacoes) | Dated legal-person registration events | Anti-bot controls; no open bulk API |
| [RNPC](https://irn.justica.gov.pt/Servicos/Empresas-e-outras-pessoas-coletivas/Registo-Nacional-de-Pessoas-Coletivas) | Legal identity, NIPC, form, status | Formal supply routes only; no free bulk feed |
| [Certidão permanente](https://registo.justica.gov.pt/Empresas/Pedir-Certidao-Permanente) | Current registered position | Paid; access code per company |
| [RCBE](https://rcbe.justica.gov.pt/) | Declared beneficial ownership | Requires authentication |
| [BASE entities](https://dados.gov.pt/pt/datasets/contratos-publicos-portal-base-impic-entidades/) | Public-procurement participants (public domain) | Not a company register; [REST API](https://www.base.gov.pt/APIBase2) needs a token |

[IRN terms](https://registo.justica.gov.pt/Termos-e-Condicoes) prohibit automated extraction without authorisation: obtain a supply/reuse arrangement first, and never bypass challenges. Match legal persons by verified NIPC, never by name alone.

Also deferred:

- [DGES](https://www.dges.gov.pt/pt/pagina/pesquisa-de-cursos-e-instituicoes): higher-education institutions; no bulk API; does not prove attendance.
- [SIOE](https://www.sioe.dgaep.gov.pt/): public-organisation identity and hierarchy; confirm the current service endpoint and conditions first.
- [CASES](https://credencial.cases.pt/pt-PT/0/PCR/ARQUI/PCR_Menu_COOPERATIVASCREDENCIADAS): credentialled cooperatives only, a subset.
- [Foundations and public-utility entities](https://www.gov.pt/guias/fundacoes-e-pessoas-coletivas-de-utilidade-publica): no operational bulk feed.
- Association and Freemasonry membership: no public official register exists, and the associative-affiliation section of declarations is excluded from public access by Regulamento 258/2024. Never infer membership.

## Secondary, not authoritative

- [Integrity Watch Portugal](https://integritywatch.transparencia.pt/sobre.php): stale (last updated 2023); not a source of truth.
- [openAR](https://openar.pt/metodologia): possible future outbound links. Identity mapping and whether a vote belongs to an individual or a parliamentary group are unverified.
