# Official source research

Research checked on 25 September 2026. This is a source/access assessment, not permission to deploy, publish or collect every field. Company and association joins are deliberately deferred.

## Parliamentary backbone

- [Informação Base](https://www.parlamento.pt/Cidadania/Paginas/DAInformacaoBase.aspx): legislature-scoped JSON/XML, identities, constituencies, parliamentary groups and dated mandate states. The XVII payload had 1,446 records including substitutes; 230 were currently serving. Filter dated effective states, never take the first 230 rows.
- [Registo Biográfico](https://www.parlamento.pt/Cidadania/Paginas/DARegistoBiografico.aspx): join `CadId` to `DepCadId`. The inspected XVII file contained 339 biographies covering all 230 serving MPs. Profession, education and roles are disclosed curricular information, not a complete employment history. Role text does not provide consistent organisation identifiers or dates.
- [Reuse conditions](https://www.parlamento.pt/Cidadania/paginas/dadosabertos.aspx): Parliament explicitly permits reuse of its open datasets with attribution to Assembleia da República. Historical datasets are refreshed monthly; a current-legislature refresh guarantee was not found.

The implementation's live schema check also found integral numeric IDs represented as JSON floats, and parliamentary-group intervals ending on 24 September in the 25 September snapshot. Canonicalise exact integral IDs without rounding; retain supplied group intervals rather than inventing a currently active affiliation.

## Current interests: EpT API verification

[Parliament's gateway](https://www.parlamento.pt/RegistoInteresses/Paginas/deputados-e-membrosgoverno.aspx) directs current interests to [EpT public access](https://entidadetransparencia.pt/). Nine anonymous, read-only JSON POST requests were exercised successfully, without credentials or challenge bypass. The frontend's API is not a documented, supported external integration contract.

The tested base is `https://api.entidadetransparencia.pt`. Selector calls to `/publicquery/getallentities`, `/publicquery/getallroles` and `/publicquery/getallholders` established the XVII Assembleia cohort and the two deputy role labels. `/publicquery` returned paginated officeholder rows; `/publicquery/search` returned one holder's declaration list; `/publicquery/getdeclaration` returned one detail. Only three officeholder pages, one declaration list and one detail were inspected, not all declarations.

Observed entity ID `4508` and deputy role IDs `8`/`232` are discovery results, not permanent constants. Search bodies use entity/role/holder filters, `pageNumber` and `sortBy`; the declaration-list call uses arrays for `EntityId` and `RoleId`. Discover current selectors rather than hard-code these identifiers. Frontend behaviour was checked against its publicly served application/chunk scripts; no versioned schema or rate-limit guarantee was found.

The scoped selectors contained 263 distinct holder IDs. Of Parliament's 230 currently serving MPs, 221 had exact full-name candidates and 225 had candidates after case/accent/whitespace normalisation. These are **candidate counts, not identity joins or declaration-completeness statistics**. Five unmatched names do not prove missing declarations; presidential-only roles were not enumerated. EpT uses its own identifiers; no AR cadastro identifier/crosswalk was present in the inspected schemas.

Details use a GUID-keyed field tree with visibility/redaction flags and empty template rows. Do not persist or republish raw responses, count template rows as interests, infer corporate identifiers from mixed NIF/NIPC fields, or consume request-only sections. An automatic connector is not included: first establish a supported public-field projection, reviewed identity crosswalk, reuse conditions and correction/withdrawal behaviour with [EpT](https://www.tribunalconstitucional.pt/tc/ept/contactos.html).

The [public-access rules](https://files.diariodarepublica.pt/2s/2024/03/047000000/0012600137.pdf) distinguish published interests from requested consultation. Income/assets and the separate associative-affiliation section are not ordinary anonymous ingestion material. Historical AR/Tribunal Constitucional records are separate from the platform introduced in March 2024; full historical migration was not established.

## Companies and other legal persons — deferred

| Source | Useful information | Access and limitations |
| --- | --- | --- |
| [IRN Publicações](https://registo.justica.gov.pt/Empresas/Publicacoes) | Dated company and other legal-person registration/publication events | Free public consultation; legacy search contains anti-bot controls. No documented open bulk API verified. An appointment event does not prove a position is still held. |
| [RNPC / FCPC](https://irn.justica.gov.pt/Servicos/Empresas-e-outras-pessoas-coletivas/Registo-Nacional-de-Pessoas-Coletivas) | Legal identity, NIPC, legal form, activity and status | Covers associations, foundations, cooperatives and other entities as well as companies. Formal data-supply/certificate routes exist; no free entity-level bulk feed verified. |
| [Certidão permanente](https://registo.justica.gov.pt/Empresas/Pedir-Certidao-Permanente) | Current registered position and, depending on product, supporting filings | Paid to obtain; consultation requires an access code. No certificate was purchased or retrieved. |
| [RCBE](https://rcbe.justica.gov.pt/) | Declared ultimate beneficial ownership/control | Anonymous consultation redirected to authentication. Not a verified anonymous API; beneficial ownership differs from registered representation/shareholding. |
| [BASE entities](https://dados.gov.pt/pt/datasets/contratos-publicos-portal-base-impic-entidades/) | Entities participating in public procurement | Catalogue metadata verifies weekly JSON/XLSX resources and a public-domain licence. Not the entire company register; procurement does not establish MP employment or ownership. [REST access](https://www.base.gov.pt/APIBase2) requires a token. |

[IRN terms](https://registo.justica.gov.pt/Termos-e-Condicoes), section 4, expressly prohibit automated extraction without authorisation. Section 3 also sets reuse conditions. Obtain an authorised supply/reuse arrangement before implementing registry automation; do not bypass challenges or treat internal frontend calls as permission.

Use NIPC for legal-person matching where actually supplied and verified. Never merge a person or organisation by name alone. Retain effective dates separately from registration/publication dates. A legal-person record is not a membership roster.

## Specialist organisation directories — deferred

- [DGES](https://www.dges.gov.pt/pt/pagina/pesquisa-de-cursos-e-instituicoes): institution/course directories; representative 2026 HTML records worked. Preserve leading zeros and distinguish schools/faculties from their legal entities. No documented anonymous bulk API found. The directory does not prove that an MP attended or graduated.
- [SIOE](https://www.sioe.dgaep.gov.pt/): public-organisation identity, NIPC, hierarchy and history. Public CSV/Excel export is documented but was not exercised. The [June 2026 SOAP specification](https://www.sioe.dgaep.gov.pt/SIOE%20Faced%20WebServices%20v2_1.pdf) describes an unauthenticated public service; its documented production WSDL failed DNS resolution here. Confirm the current endpoint and conditions before integration; private employee services are out of scope.
- [CASES](https://credencial.cases.pt/pt-PT/0/PCR/ARQUI/PCR_Menu_COOPERATIVASCREDENCIADAS): public credentialled-cooperative rows with NIPC and validity dates were accessible. This is a subset, not all cooperatives or all social-economy entities. Export controls were not exercised.
- [Foundations and public-utility entities](https://www.gov.pt/guias/fundacoes-e-pessoas-coletivas-de-utilidade-publica): government guidance links recognition/status procedures and registers. The linked central search returned an error; no operational bulk feed verified. Use dated official acts for corroboration rather than assuming permanent status.

No comprehensive official public Freemasonry membership register was found. The separate associative-affiliation section of mandatory declarations is excluded from anonymous public access by [Regulamento 258/2024, Article 15](https://files.diariodarepublica.pt/2s/2024/03/047000000/0012600137.pdf). Requested consultation is not an openly reusable membership feed. Do not infer membership from names, events, addresses or related organisations.

## Secondary services — not authoritative imports

[Integrity Watch Portugal](https://integritywatch.transparencia.pt/sobre.php) demonstrates combining AR biographies and interests, but its About page reports XV-legislature coverage and a last database update of 18 September 2023. It is not the current source of truth.

[openAR](https://openar.pt/metodologia) is an independent service, potentially useful as a future outbound link to parliamentary activity, laws and voting information. No deputy-link identifier contract was verified and no link integration is included. Before adding links, verify identity mapping, stable URLs, coverage and whether a displayed vote belongs to an individual or a parliamentary group; do not attribute group positions to individuals without evidence.

## Scope of verification

Official source documentation, two parliamentary JSON payloads in memory, representative directory responses and access restrictions were inspected. No registry corpus, paid certificates, private membership lists or production records were collected. Some JavaScript interfaces could not be exercised because browser startup failed; documented exports are not reported as tested downloads.
