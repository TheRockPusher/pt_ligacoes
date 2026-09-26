# Official source research

Research checked on 25 September 2026. This is a source/access assessment, not permission to deploy, publish or collect every field. Government composition and EpT public-interest connectors are implemented but live-gated: no automated reuse approval for either source was obtained or created by this work. Party membership, registry enrichment and association/other membership imports remain deferred.

## Parliamentary backbone

- [Informação Base](https://www.parlamento.pt/Cidadania/Paginas/DAInformacaoBase.aspx): legislature-scoped JSON/XML, identities, constituencies, parliamentary groups and dated mandate states. The XVII payload had 1,446 records including substitutes; 230 were currently serving. Filter dated effective states, never take the first 230 rows.
- [Registo Biográfico](https://www.parlamento.pt/Cidadania/Paginas/DARegistoBiografico.aspx): join `CadId` to `DepCadId`. The inspected XVII file contained 339 biographies covering all 230 serving MPs. Profession, education and roles are disclosed curricular information, not a complete employment history. Role text does not provide consistent organisation identifiers or dates.
- [Reuse conditions](https://www.parlamento.pt/Cidadania/paginas/dadosabertos.aspx): Parliament explicitly permits reuse of its open datasets with attribution to Assembleia da República. Historical datasets are refreshed monthly; a current-legislature refresh guarantee was not found.

The implementation's live schema check also found integral numeric IDs represented as JSON floats, and parliamentary-group intervals ending on 24 September in the 25 September snapshot. Canonicalise exact integral IDs without rounding; retain supplied group intervals rather than inventing a currently active affiliation.

AR imports now extract private professional-role candidates automatically from the retained role allowlist; an authorised admin action can backfill the latest already-loaded records of current MPs without network collection. The source role/cadastro IDs, revision, biography URL and original retrieval time remain provenance. Organisation identity, relationship type and effective dates require human resolution; previous/current markers are not date boundaries. This is not automatic extraction of a complete employment history.

## Government composition — implemented, live-gated

The [official XXV Government composition](https://portugal.gov.pt/gc25/governo/composicao) was inspected through the public page, Next assets/profile data and read-only browser/JavaScript GraphQL requests. The connector discovers the current Next build/deployment, public Sitecore context and eight composition template selectors; it does not hard-code an observed deployment. The frontend queries `https://edge-platform.sitecorecloud.io/v1/content/api/graphql/v1?sitecoreContextId=<discovered-public-context>`. These are internal public frontend contracts, not a documented versioned API or reuse licence.

An explicit 25 September 2026 composition query returned `data.childs` with `total=60`, `pageInfo.hasNext=false` and 60 results: one prime minister, 16 ministers and 43 secretaries of state. **This is a dated observation, not a permanent expected count.** The inspected frontend timeline selected 14 April 2026 despite the September URL; the connector therefore supplies the requested as-of date explicitly rather than trusting displayed URL state.

Observed fields include appointment GUID `results.id`, person GUID `official.jsonValue.id`, `official.fields.FullName.value`, compact `startDate`/`endDate` timestamps, `isOfficialHidden`, `governmentRole`, and ministry-page ancestor GUIDs, `GovernmentTitle` and template IDs. Non-PM profiles expose `OfficialsHistory` and `context.officialInfo.allOfficials` for identity/category/date consistency checks. The PM's `/acerca` page does not expose that history contract; its appointment-page identity is used instead. Source cessation dates are exclusive and are normalised to the inclusive last-serving day, while retaining the raw boundary in the passage.

The connector exhausts bounded pagination, checks actual source totals, GUID uniqueness, offices, dates and profile consistency, and fails closed on unknown templates/routes, disagreement or incomplete results. It can create private source-ID people/portfolio institutions and draft public-office claims; a reviewed mapping is required to link to an existing AR or other person. There is no name join, party-membership inference or Government biography retention.

The GraphQL server rejected `ItemField.targetItem`; the supported `official.jsonValue` response is therefore projected immediately to the needed ID/name fields. Extra profile/person fields remain transient. No raw response or real source fixture was retained. Before live use, record source-specific `government_office` approval covering purpose, reuse-authorisation/legal-basis reference and retention/review conditions. Successful public requests are not that approval.


## Current interests: EpT — implemented, live-gated

[Parliament's gateway](https://www.parlamento.pt/RegistoInteresses/Paginas/deputados-e-membrosgoverno.aspx) directs current interests to [EpT public access](https://entidadetransparencia.pt/). Initial access research exercised nine anonymous, read-only JSON POST requests successfully, without credentials or challenge bypass. Connector research additionally inspected the publicly served Vue/source-map contract and nine public API POST schema observations. The frontend's API is not a documented, supported external integration contract, and neither research pass establishes automated reuse approval.

The tested base is `https://api.entidadetransparencia.pt`. Initial selector calls to `/publicquery/getallentities`, `/publicquery/getallroles` and `/publicquery/getallholders` established the XVII Assembleia cohort and the two deputy role labels. `/publicquery` returned paginated officeholder rows; `/publicquery/search` returned one holder's declaration list; `/publicquery/getdeclaration` returned one detail. That initial pass inspected only three officeholder pages, one declaration list and one detail, not all declarations.

Observed entity ID `4508` and deputy role IDs `8`/`232` are discovery results, not permanent constants. Search bodies use entity/role/holder filters, `pageNumber` and `sortBy`; the declaration-list call uses arrays for `EntityId` and `RoleId`. Discover current selectors rather than hard-code these identifiers. Frontend behaviour was checked against its publicly served application/chunk scripts; no versioned schema or rate-limit guarantee was found.

The scoped selectors contained 263 distinct holder IDs. Of Parliament's 230 currently serving MPs, 221 had exact full-name candidates and 225 had candidates after case/accent/whitespace normalisation. These are **candidate counts, not identity joins or declaration-completeness statistics**. Five unmatched names do not prove missing declarations; presidential-only roles were not enumerated. EpT uses its own identifiers; no AR cadastro identifier/crosswalk was present in the inspected schemas.

Details use a GUID-keyed field tree with visibility/redaction flags and empty template rows. The implemented connector requires an already-reviewed EpT holder-to-person mapping before collection or application; it does not use the name candidates above. It discovers holder-specific selectors, validates complete pagination for that holder, fetches only published declarations, verifies exact holder/declaration identity and rechecks the unfiltered holder scope before applying. One holder per invocation is the supported scope, not whole-population coverage. A used identity's source/ID/entity mapping remains immutable; authorised re-attestation may renew the review of that unchanged mapping without moving claims.

### Observed wire contract and minimised projection

- Selector POSTs to `/publicquery/getallentities` and `/publicquery/getallroles` use `HolderId`; `/publicquery/search` uses `HolderId`, `EntityId`/`RoleId` arrays, `pageNumber` and `sortBy='Id asc'`; `/publicquery/getdeclaration` uses `Id` and `isDraft:false`.
- Responses use `code=0,data`. List `data` contains `items,pageNumber,pageSize,pageCount,total`; an empty scope was observed with `pageCount=0,total=0,pageNumber=1`. Detail fields are lowercase, including `holderId,id,stateType,natureType,submitedDate,relatedDeclarationId,data,oppositionKeys,unavailableSections`.
- Published state is `8`. Modified state `32` was observed in the same holder list and excluded; other shipped nonpublic states `1,2,4,16,64` are also excluded without fetching their details.
- Professional-activity table `2c6d9562-dea5-488b-9afe-42519c636daf` supplies literal activity/role (`col1`), organisation (`col2`), nature/area (`col3`), start (`col6`) and end (`col7`). Headquarters, remuneration, unused template cells and the mixed NIF/NIPC field are excluded.
- Company table `9b58c917-b14c-4eed-9b3c-40c3d99d45da` supplies company (`col1`), nature (`col6`: civil/commercial) and ownership category (`col8`). Only declarant sole/co-ownership values `1`/`2` support shareholding candidates; spouse/partner values `3`/`4` do not. Addresses, amounts, percentages and mixed tax identifiers are excluded. Column and ownership semantics were verified from shipped Vue code and Portuguese labels; no populated real company row was copied or retained.
- Projection requires positive `isVisible` throughout the fixed root/interest-section/table/row/cell ancestry, no restriction reason/justification and no matching `unavailableSections.sectionKey`. Any nonempty `oppositionKeys` suppresses the entire declaration rather than guessing an unobserved opposition schema. Empty template rows never count as interests.

Only the minimised professional/company passages survive as private candidates; the connector does not retain raw responses, associative affiliations, income/assets, sensitive identifiers or request-only/private sections. Literal professional roles are not automatic employment/directorship claims, and company mentions are not proof of shareholding. An editor must resolve the organisation, type and supported dates with a documented rationale before conversion to a draft relationship. Publication remains a separate explicit review.

The frontend formats timestamps in the viewer's local timezone. Where the encoded date and Europe/Lisbon civil date disagree, the candidate preserves the literal timestamp but leaves the effective date unknown for review. Submission date is filing context, never an inferred activity or office boundary.

No stable canonical declaration permalink was found: the frontend generates randomised AES-obfuscated route tokens. Citations use the [public portal/search](https://entidadetransparencia.pt/) with exact declaration/holder/entity/board/role/row reference, holder label, institution/public role and filing date. The passage explicitly says the URL is not a direct declaration link; references are bounded without silent truncation.

Complete holder snapshots drive scoped change/withdrawal handling. Missing, changed or returning observations withdraw dependent claim approval and public evidence and require fresh review; they do not restore publication or affect unrelated holders. Incomplete pagination, changing remote scope, schema ambiguity and identity mismatch fail without applying a partial snapshot. Live dry-run and apply remain blocked until an active recorded `declared_interest` source approval covers purpose, reuse-authorisation/legal-basis reference and retention/review conditions. Resolve access/reuse conditions with [EpT](https://www.tribunalconstitucional.pt/tc/ept/contactos.html); technical integration is not an assertion that permission has been obtained.

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

Official source documentation, two parliamentary JSON payloads in memory, representative directory responses and access restrictions were inspected. Government and EpT browser/frontend/API inspection supplied the structural evidence above; only structural/aggregate observations were recorded, not real declaration values or raw source responses. No registry corpus, paid certificates, private membership lists or production records were collected. Some other JavaScript interfaces could not be exercised because browser startup failed; documented exports are not reported as tested downloads.

Fictional connector/application, admin candidate conversion, explicit publication and public profile/graph/readable-evidence flows were exercised locally for the three implemented enrichment areas. This is not evidence of a live approved collection, source reuse authorisation or population-wide factual completeness. No real source records were imported or published, and no deployment was performed.
