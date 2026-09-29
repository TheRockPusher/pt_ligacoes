# Sources

Source-specific access, reuse and coverage rules. The [dataset registry](../apps/platform/ligacoes/core/catalogue.py) owns dataset keys and the public [Fontes page](/fontes/), which shows published coverage. See the [database architecture](architecture.md), [editorial methodology](methodology.md) and [import procedures](operations.md).

The maintainer has approved reuse of the catalogued sources for this non-profit project; non-commercial conditions are satisfied. This is a project decision, not a claim that every publisher supplies an open licence. Attribute sources, retain the stated restrictions, minimise personal data and never bypass access challenges. No notification is sent to EpT.

## Imported

Grouped by registry status, not a promise that every feed is currently accessible or has public records. **Automatic** means identifier-anchored claims publish on import and editors can withdraw them; **reviewed** means private candidates until editorial review. **Events** are n-ary records, not claims: every party must be a public, identifier-anchored entity before publication. Identity-only and citation-only entries do not themselves produce edges.

All commands below are Django management commands, **dry-run by default**; `--apply` writes. Required arguments and snapshot scope are documented by each command's `--help`. Publisher abbreviations: AR = Assembleia da República; Government = Governo da República Portuguesa; EpT = Entidade para a Transparência; EP = Parlamento Europeu; EC = Comissão Europeia; AD&C = Agência para o Desenvolvimento e Coesão.

| Dataset / official source | Publisher | Contribution | Identifiers | Reuse terms | Import command / publication |
| --- | --- | --- | --- | --- | --- |
| `ar_informacao_base` — [Informação de Base](https://www.parlamento.pt/Cidadania/Paginas/DAInformacaoBase.aspx) | AR | Mandates and parliamentary-group edges | `DepCadId`, legislature-scoped group IDs | [AR attribution](https://www.parlamento.pt/Cidadania/paginas/dadosabertos.aspx) | `import_parliament`; `import_parliament_bodies` for groups; automatic |
| `ar_registo_biografico` — [Registo Biográfico](https://www.parlamento.pt/Cidadania/Paginas/DARegistoBiografico.aspx) | AR | Disclosed biography roles | `DepCadId`; organisations often name-only | AR attribution | `import_parliament`; reviewed |
| `gov_composicao` — [Government composition](https://portugal.gov.pt/gc25/governo/composicao) | Government | Offices and portfolio-structure edges | Official person and portfolio IDs | No published reuse licence | `import_government`; automatic |
| `ept_declaracoes` — [Public declarations](https://entidadetransparencia.pt/) | EpT | Declared activities, roles and company interests | Holder/declaration IDs, verified NIPC | No published licence; public interests only, Lei 52/2019 art. 17 | `import_interests`; reviewed |
| `ar_composicao_orgaos` — [Composição de Órgãos](https://www.parlamento.pt/Cidadania/Paginas/DAComposicaoOrgaos.aspx) | AR | Committee, Mesa and other parliamentary-body roles | `DepCadId`, `DepId`, legislature-scoped organ IDs | AR attribution | `import_parliament_bodies`; automatic |
| `ar_atividade_deputados` — [Atividade dos Deputados](https://www.parlamento.pt/Cidadania/Paginas/DAatividadeDeputado.aspx) | AR | Friendship-group membership and supporting body structure | `DepCadId`, `DepId`, `GplId` | AR attribution | `import_parliament_bodies`; automatic |
| `ar_delegacoes_permanentes` — [Delegações Permanentes](https://www.parlamento.pt/Cidadania/Paginas/DADelegacoesPermanentes.aspx) | AR | Delegation membership edges | Delegation ID, `DepId` mapped to `DepCadId` | AR attribution | `import_parliament_bodies`; automatic |
| `ar_grupos_amizade` — [Grupos Parlamentares de Amizade](https://www.parlamento.pt/Cidadania/Paginas/DAGPA.aspx) | AR | Friendship-group reference data | Legislature-scoped `GplId` | AR attribution | No separate feed import; `import_parliament_bodies` obtains automatic friendship edges from `ar_atividade_deputados` |
| `ar_registo_interesses` — [Historic Registo de Interesses](https://www.parlamento.pt/Cidadania/Paginas/DARegistoBiografico.aspx) | AR | Historic declared roles and company interests | `DepCadId`; organisation names | AR attribution; public interests only | `import_parliament_interests`; reviewed |
| `ept_titulares` — [Holders by entity and office](https://entidadetransparencia.pt/) | EpT | Dated public-office edges | Holder, entity and office IDs | No published reuse licence | `import_ept_offices`; automatic |
| `gov_nomeacoes` — [Gabinete nominations](https://portugal.gov.pt/gc25/governo/nomeacoes) | Government | Staff roles; office structure | Government office IDs, DR act IDs; people name-only | No published reuse licence | `import_government_nominations`; roles reviewed, anchored structure automatic |
| `dr_diario_republica` — [Diário da República](https://diariodarepublica.pt/) | Imprensa Nacional-Casa da Moeda | Appointment/exoneration evidence, not a separate edge feed | Act/document reference | Official texts: CDADC art. 8; free access: DL 83/2016 | Cited by `import_government_nominations`; no standalone import/publication |
| `sioe` — [SIOE+](https://www.sioe.dgaep.gov.pt/) | DGAEP | Public-body identity, supervision and succession; board roles | SIOE code, verified NIPC; board members name-only | No published licence; free access under Lei 104/2019 art. 11 | `import_sioe`; structure automatic, board roles reviewed |
| `gleif_lei` — [LEI records](https://search.gleif.org/) | GLEIF | Organisation identity and consolidation-parent edges | LEI, verified Portuguese registration NIPC | [CC0](https://www.gleif.org/en/meta/lei-data-terms-of-use) | `import_gleif`; automatic |
| `base_contratos` — [BASE contracts](https://dados.gov.pt/pt/datasets/contratos-publicos-portal-base-impic-contratos-de-2012-a-2026/) | IMPIC | Contract events: buyers, suppliers, bidders | Verified NIPC, contract ID | Public domain on dados.gov.pt | `import_base_contracts`; events |
| `base_entidades` — [BASE entities](https://dados.gov.pt/pt/organizations/impic-i-p-instituto-dos-mercados-publicos-do/) | IMPIC | Organisation identity, no independent edges | Verified NIPC | Public domain on dados.gov.pt | `import_base_contracts` creates organisations from contract parties; no separate entity-feed import |
| `igf_subvencoes` — [Public subsidies](https://igf.gov.pt/subvencoes-publicas) | Inspeção-Geral de Finanças | Subsidy/benefit events: grantor and beneficiary | Verified NIPC | Public domain in registry; older annual files may require attribution | `import_igf_subsidies`; events |
| `pt2020_beneficiarios` — [Portugal 2020](https://dados.gov.pt/pt/datasets/datasets-do-portugal-2020/) | AD&C | EU-funding beneficiary events | Verified NIPC, operation reference | CC BY 4.0 | `import_eu_funds`; events |
| `pt2030_operacoes` — [Portugal 2030](https://dados.gov.pt/pt/datasets/datasets-pt2030-03-lista-de-operacoes-pt2030/) | AD&C | EU-funding operation events | Verified NIPC, operation reference | Licence not specified on dados.gov.pt | `import_eu_funds`; events |
| `prr_entidades` — [PRR entities](https://dados.gov.pt/pt/datasets/dataset-estrutura-de-missao-prr-entidades-1/) | Estrutura de Missão Recuperar Portugal | Funding events: beneficiaries, intermediaries, suppliers | Verified NIPC, project reference | Licence not specified on dados.gov.pt | `import_eu_funds`; events |
| `etf_orgaos_sociais` — [Public-company boards](https://www.etf.gov.pt/informacao-sobre-as-empresas) | Entidade do Tesouro e Finanças | Board-role candidates | Company NIPC; people name-only | [Non-profit reuse with attribution](https://www.etf.gov.pt/avisos-legais) | `import_etf_boards`; reviewed |
| `ep_deputados` — [Portuguese MEPs](https://data.europarl.europa.eu/api/v2/meps?country-of-representation=PT) | EP | Mandates, political-group, committee and delegation edges; no national-party affiliation | EP person/organisation IDs | [EP reuse and attribution terms](https://www.europarl.europa.eu/legal-notice/) | `import_european_parliament`; automatic |
| `eu_registo_transparencia` — [EU Transparency Register](https://data.europa.eu/data/datasets/transparency-register) | EC and EP | Organisation identity for meetings, no independent edges | EU Transparency Register ID | [Commission reuse notice](https://eur-lex.europa.eu/eli/dec/2011/833/oj), attribution | `import_eu_contacts --dataset register`; automatic identity resolution |
| `ep_reunioes` — [MEP meetings](https://www.europarl.europa.eu/meps/en/search-meetings) | EP | Meeting events | EP and Transparency Register IDs | EP reuse and attribution terms | `import_eu_contacts --dataset ep-meetings`; events; export currently WAF-blocked |
| `ce_reunioes` — [Commission meetings](https://data.europa.eu/data/datasets/european-commission-meetings-with-interest-representatives) | EC | Commissioner/cabinet meeting events | EC representative and Transparency Register IDs | CC BY 4.0 | `import_eu_contacts --dataset ec-meetings`; events |
| `ar_atividades` — [Atividades](https://www.parlamento.pt/Cidadania/Paginas/DAatividades.aspx) | AR | Hearing/audience events; external-body election roles | Parliamentary/activity IDs; external people and counterparties may be name-only | AR attribution | `import_parliament_activities`; events; election roles reviewed |
| `ar_ofertas_hospitalidades` — [Gifts, travel and hospitality](https://www.parlamento.pt/RegistoDeslocacoesHospitalidades/Paginas/ROfertasDHList.aspx) | AR | Gift, travel and hospitality events | `DepCadId`; provider often free text | AR attribution in registry; project reuse approved | `import_parliament_gifts`; events |

## Upcoming

| Dataset / official source | Publisher | Contribution | Identifiers | Reuse terms | Import / publication |
| --- | --- | --- | --- | --- | --- |
| `rtri` — [RTRI commencement notice](https://www.parlamento.pt/Paginas/2026/agosto/Informacao-sobre-inicio-funcionamento-do-Registo-de-Transparencia-Representacao-Interesses-RTRI.aspx) | AR | Representatives, clients and meetings | Register scheme not published | Open data required by Lei 5-A/2026; terms pending | No importer; full operation from 1 January 2027; publication mode depends on identifiers |
| `gov_audiencias` — [Government audiences, RCM 167/2026](https://files.diariodarepublica.pt/1s/2026/08/15600/0004200044.pdf) | Government | Quarterly audience/meeting events | Government IDs; counterparty scheme pending | Mandatory quarterly publication; terms pending | No importer; files/formats not yet published; intended events |

## Secondary

| Dataset / source | Publisher | Contribution | Identifiers | Reuse terms | Import / publication |
| --- | --- | --- | --- | --- | --- |
| `wikidata` — [Wikidata](https://www.wikidata.org/) | Wikimedia community | Identifier hints and identity suggestions, never evidence or edges | QID; AR, EP, LEI and Transparency Register crosswalks | [CC0](https://www.wikidata.org/wiki/Wikidata:Licensing) | `import_wikidata_crosswalk`, dry-run by default; reviewed hints only |

## Identity and citation rules

- Match organisations only by verified NIPC (legal-person prefix and valid check digit) or another official anchor, never by name. Never read, retain or log natural-person NIFs. Drop natural persons from procurement, subsidy and funding datasets, including their names; drop events without a remaining legal-person counterpart.
- An unknown official person ID matching a public namesake produces a pending identity suggestion before creation. Name-only people (including gabinete staff and SIOE/ETF board members) remain private review candidates. See [methodology](methodology.md) for the separate identity, conversion and publication decisions.
- AR `DepCadId` is the stable person ID (`CadId`, `idCadastro`, `IdCadastroGODE`, web `BID`); `DepId` is legislature-specific. In `AtividadeDeputado.DlP[]`, the field called `DepId` is a delegation ID. Organ/group IDs must include the legislature. There is no universal official person ID shared by these sources.
- Import parliamentary groups, not party membership, party offices, electoral lists or candidacies. Votes and co-authorship are not connections. National-party fields from EP and party-organ rows from EpT are excluded.
- Older AR biography/activity files can omit former deputies absent from the current cadastro; an absent record means **not published**, not “none”. Biography role text is not a complete employment history. Government frontend contracts and the EpT API are undocumented and unversioned.
- EpT has no stable declaration permalinks: cite its portal and exact declaration identifiers. Only public interests are in scope under [Lei 52/2019 art. 17](https://www.pgdlisboa.pt/leis/lei_mostra_articulado.php?nid=3192&tabela=leis), not income, assets, spouse data or associative affiliations. Source declarations expire under [Regulamento 258/2024](https://files.diariodarepublica.pt/2s/2024/03/047000000/0012600137.pdf); citations may cease to resolve.

## Blocked or deferred

Access findings below are not permission to bypass restrictions. Re-check the official route before adding an importer.

| Source | Reason / boundary |
| --- | --- |
| [EP meeting export](https://www.europarl.europa.eu/meps/en/search-meetings) | AWS WAF challenge for non-browser clients; importer exists but the live export is blocked. Not bypassed. |
| [RTRI](https://diariodarepublica.pt/dr/detalhe/lei/5-a-2026-1028061007) and Government quarterly audiences | RTRI fully operational from 1 January 2027; audience files/formats under RCM 167/2026 not yet published. |
| [IRN Publicações](https://registo.justica.gov.pt/Empresas/Publicacoes), [RNPC](https://irn.justica.gov.pt/Servicos/Empresas-e-outras-pessoas-coletivas/Registo-Nacional-de-Pessoas-Coletivas), [certidão permanente](https://registo.justica.gov.pt/Empresas/Pedir-Certidao-Permanente) | Anti-bot controls/no open bulk feed; RNPC formal supply; certidão paid with per-company access code. [IRN terms](https://registo.justica.gov.pt/Termos-e-Condicoes) require authorisation for automated extraction; Código do Registo Comercial arts. 78-B, 78-F and 78-G govern bulk supply. |
| [RCBE](https://rcbe.justica.gov.pt/) | [DL 115/2025](https://files.diariodarepublica.pt/1s/2025/10/20700/0002200025.pdf): demonstrated legitimate interest required from 1 November 2025. No republication. |
| [Banco de Portugal](https://www.bportugal.pt/comunicado/avaliacao-da-idoneidade-dos-membros-dos-orgaos-de-administracao-e-fiscalizacao), [ASF](https://www.asf.com.pt/pt/todas-as-entidades-autorizadas) | BdP anti-bot controls; no published reuse licence identified for board data. |
| [Azores JORAA/Government portals](https://sites01.azores.gov.pt/CID/OrganicasGRA.html) | Cloudflare challenge, not bypassed. The separate [ALRAA deputies dataset](https://dados.gov.pt/datasets/assembleia-legislativa-da-regiao-autonoma-dos-acores-deputados) is open. |
| [CMVM](https://www.cmvm.pt/PInstitucional/PortalInstitucional) | Boards and qualified holdings are document-by-document; no API or reuse licence identified. |
| [CReSAP](https://www.cresap.pt/pareceres-gestor-publico/relato-rios-de-adequacao) | No published reuse licence; behavioural assessments are excluded regardless. |
| [Ciência Vitae](https://api.cienciavitae.pt/docs/), [RENATES](https://renates.dgeec.mec.pt/ws/renatesws.asmx), [DGES](https://www.dges.gov.pt/pt/pagina/pesquisa-de-cursos-e-instituicoes) | CV API requires credentials/holder consent; RENATES exposes birth dates (manual consultation only); DGES has no bulk API and institution/course records do not prove attendance. |
| [Foundations register](https://irn.justica.gov.pt/Servicos/Empresas-e-outras-pessoas-coletivas/Regime-do-Registo-de-Fundacoes/Registo-de-Fundacoes), [IPSS](https://www.seg-social.pt/instituicoes-particulares-de-solidariedade-social-registo), [CASES](https://credencial.cases.pt/pt-PT/0/PCR/Arqui/PCR_Menu_COOPERATIVASCREDENCIADAS) | Foundations now at IRN, no bulk feed; no bulk feed/licence identified for IPSS/CASES. Credentialled cooperatives are only a subset. |
| [DGAL Portal Autárquico](https://portalautarquico.dgal.gov.pt/pt-PT/entidades-locais/sector-empresarial-local/) | Names without NIPC, dates or boards: cross-check only. |
| Government gifts; [TC declarations before 2024](https://www.tribunalconstitucional.pt/tc/ept/) | Request-only consultation, not online import feeds. |

## Not available

- No official party-membership register or public party-member lists; no online Tribunal Constitucional list of party-organ holders. Never infer affiliation from parliamentary group, list, candidacy or “força política”.
- Party and campaign donor identities are withheld by [ECFP](https://www.tribunalconstitucional.pt/tc/contas.html) since 2026.
- No public register of post-office activity authorisations; no Portuguese High-Value Dataset for companies.
- No public official register of association or Freemasonry membership; associative declaration sections are consultation-only under Regulamento 258/2024.
- Wikidata has no NIPC property: P3608 is EU VAT (`PT` + NIPC), not a verified Portuguese company-register record.

## Other civic and secondary sources

These are not authoritative evidence or imported claim feeds.

| Source | Use / limitation |
| --- | --- |
| [OpenSanctions](https://www.opensanctions.org/datasets/pt_parliament/), [EveryPolitician](https://www.opensanctions.org/articles/2026-02-18-every-politician/) | Non-commercial terms; not used. Lack an AR identifier suitable for this crosswalk. |
| [openAR](https://openar.pt/metodologia) | Outbound links only; person ID matches AR cadastro, but name-parsed votes are not connection evidence. |
| [Integrity Watch Portugal](https://integritywatch.transparencia.pt/sobre.php) | Stale secondary material, not a source of truth. |
| [LobbyFacts](https://www.lobbyfacts.eu/about) | Historical Transparency Register snapshots; no data licence identified. |
| [AR DILP governments PDF](https://ficheiros.parlamento.pt/DILP/Publicacoes/Temas/32.GovernosPortugueses/32.pdf) | Rights reserved; outside the AR open-data licence. |
| [Arquivo.pt](https://github.com/arquivo/pwa-technologies/wiki/APIs) | Provenance citations for vanished pages; [non-commercial terms](https://sobre.arquivo.pt/pt/ajuda/termos-e-condicoes/), rate limits, no rehosting. |
