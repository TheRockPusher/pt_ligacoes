# Editorial methodology and personal data

## What a connection means

A connection is a specific, typed claim, placed in time and supported by a documentary passage. **It does not imply favouritism, co-ordination, friendship or wrongdoing.** A missing connection does not prove that none exists. A shared surname, university or employer at different times does not establish a personal relationship.

Official-source importers prepare non-public editorial material, never automatic publication. The scoped AR reuse below is approved for documenting parliamentary representation and relevant public-role biographies, subject to attribution, minimisation and human review. Government composition and EpT public-interest connectors are implemented, but **live collection, including dry-run, remains blocked until source-specific approval is recorded**. Implementation and public read-only source research are not evidence of permission for automated reuse. For each additional source or expanded use, establish the public-interest purpose, lawful basis, proportionality and correction process, considering the GDPR, personality rights, copyright and defamation. Public availability alone is not permission for unrestricted reuse. This policy is not legal advice.

## Reviewing a claim

1. **Find the source.** Prefer official or primary documents. Record the title, publisher, URL, access date, publication date if known, and a precise passage reference.
2. **Resolve identity cautiously.** Use enough context without collecting excessive identifiers. Keep namesakes separate; leave uncertain matches in draft.
3. **Distinguish dates.** Document publication does not establish when a relationship began or ended. Leave unknown boundaries unknown; never invent precision for the graph.
4. **Write only what the evidence supports.** Choose the most accurate relationship type and retain relevant context, including termination or dispute. Do not present hypotheses as facts.
5. **Review explicitly.** A reviewer authorised to publish must assess public interest, the source and passage, identity, dates and each element's visibility. Saving a draft is not approval; changes to published content require fresh review before republication.

Sources are references, not guarantees. Ordinary editorial source URLs are not automatically fetched, archived or verified; only the bounded official-source importers described below fetch their allowlisted routes. URLs can change or disappear. Quote only the minimum necessary; do not copy whole documents into private notes or Git.

## Official Parliament import

AR's [open-data reuse conditions](https://www.parlamento.pt/Cidadania/paginas/dadosabertos.aspx) permit reuse with attribution to Assembleia da República. The approved purpose is to document who serves in Parliament and relevant disclosed curricular information, not to aggregate every publicly available personal detail. This scoped permission does not require a new broad legal-purpose assessment for each run; it does not extend to unrelated sources or purposes.

The importer selects the serving roster from dated mandate states, expects 230 MPs by default and requires a unique biography match by AR cadastro identifier. It retains names and parliamentary identifiers, constituency, supplied parliamentary-group intervals, the selected mandate state/dates, profession, qualifications and disclosed roles. It excludes birth details, contact information, photographs and other unselected fields; raw payloads are not persisted. Biography role text is source material, not a verified organisation identity, a complete employment history or permission to infer association membership. Supplied group intervals must not be presented as a current affiliation when they have already ended.

Direct CLI dry-run fetches and validates without database writes. Admin/GitHub validation-only requests instead retain operational queue/history metadata, but do not change editorial records. Admin and GitHub require a complete roster and biography snapshot with exactly 230 serving MPs; neither offers partial snapshots or count overrides. Draft apply requires explicit confirmation from an authorised operator; the direct CLI uses `--apply`. Import permission is not permission to publish.

Apply persists private source revisions and non-public draft public-office claims. Revisions retain minimised fields, source URLs, retrieval/as-of dates and a content fingerprint separately from editorial prose. Repeat observations do not duplicate revisions. Changed or ceased observations withdraw affected claims; returning observations require fresh review rather than restoring approval. The importer does not merge manually entered profiles by name, infer company/association joins or publish any record. Review all relevant entities, sources, passages and visibility before publication. See the [local procedure](../README.md#official-parliament-import) and [controlled operations](operations.md#controlled-parliament-imports).

Incomplete counts, missing/ambiguous biography matches, unsafe downloads and relevant schema/date ambiguities stop the import; apply is atomic. Do not bypass these failures by accepting partial rosters or weakening the expected-count guard. Source corrections still need editorial assessment and the withdrawal/retention procedures below.

### Professional-role candidates from biographies

AR imports automatically extract private candidates from the retained `CadCargosFuncoes` role text, including when the current record is otherwise unchanged. An authorised admin action can also backfill candidates from the latest already-retained record of currently imported MPs, without fetching the source again. Superseded records cannot be used to reactivate historical observations. Each candidate preserves the source role/cadastro identifiers, source revision, biography URL and **original retrieval time**; conversion or backfill is not a new source visit.

The previous/current role marker is citation context, not an effective date. A profession label alone does not establish employment. A reviewer must identify an existing organisation, choose the supported relationship type, document the identity/type/date rationale and leave unsupported dates blank. Employment, directorship, public office and professional activity are distinct claims; consultancy must not automatically become employment or shareholding. Conversion produces an ordinary private relationship, source and evidence for separate publication review, not an approved employment history.

## Government composition and portfolios

The Government connector discovers the official site's Next/Sitecore composition contract for the selected government and as-of date. Its scope covers the prime minister, ministers, secretaries of state and their official portfolios, using source appointment/person/portfolio identifiers rather than name matching. Complete pagination and source/profile consistency checks are required; an observed composition count is not a permanent expected count. Unknown or incomplete source structures fail without applying a partial roster.

Government source IDs can create new private people and portfolio institutions. Linking an official ID to an existing AR or manually entered person requires an explicit reviewed mapping; matching names are insufficient. The prime minister uses the source's appointment-page identity rather than a fabricated personal-history identifier. Office observations can prepare private `public_office` relationships to the source-identified portfolio. They do not establish party membership, private employment or other affiliations, and the connector does not retain whole Government biographies.

The source's cessation boundary is exclusive. The importer normalises it to the domain's inclusive last-serving day while retaining the original boundary in the evidence passage. Missing boundaries remain unknown; the date of retrieval is not an appointment date.

## EpT public declared interests

The EpT connector operates on **one already-reviewed holder identity per invocation**, not a name search or a population-wide reconciliation. It discovers that holder's entity/role selectors, validates the complete declaration-list pagination and checks that the holder scope did not change during collection. Only published declaration details with matching holder/declaration identifiers qualify. This is completeness of a bounded holder query, not a claim that all MPs, all historic declarations or all interests are represented.

The strict public/visible projection retains only substantive professional-activity rows and explicit company interests attributable to the declarant. Visibility must hold through the section/table/row/cell ancestry; restrictions, unavailable sections and opposition suppress material rather than inviting inference. Empty template rows, associative affiliations, income, assets, private or request-only sections, addresses, tax identifiers and other unselected fields are excluded. Spouse/partner ownership does not become the declarant's shareholding; a company mention alone is insufficient. No raw declaration is retained.

Activity wording, organisation text and supported dates remain private candidates until human identity/type/date resolution. Declaration submission dates remain separate from effective activity dates. Ambiguous civil-date conversion leaves the effective boundary unknown and retains the literal source timestamp for review.

No stable direct declaration permalink was established: the public frontend generates obfuscated route tokens. Evidence therefore links to the public portal/search and retains the exact declaration, holder, institution/public-role, row and filing-date context needed to locate the passage. It explicitly states that the URL is not a direct declaration link. Do not replace this with a guessed permanent URL.

## Source approval, identities and scoped changes

Before any live Government or EpT request, an authorised operator must record an active, source-specific approval with purpose, reuse-authorisation or legal-basis reference, allowlisted category, retention/review conditions, approving actor/time and a future review deadline. Collection approval, identity review, candidate conversion and publication are separate permissions. Fictional offline parsing/application does not grant live permission; no Government or EpT automated reuse approval was obtained by this implementation.

Source identity mappings use official source plus external ID. Once used, the mapping keys, target entity and first-use marker cannot be changed or deleted to move existing claims. An authorised reviewer may re-attest the **unchanged mapping**, renewing the review actor/time and documented contextual basis. This supports fresh review without permitting a name-based merge.

Observations are synchronised within a complete source scope: Government composition, one EpT holder or one AR member. Identical content is idempotent; older snapshots cannot replace newer ones. A changed, withdrawn, ceased or returning observation removes approval from its dependent relationship and public evidence, clears candidate review and requires fresh conversion/review before publication. It neither silently rewrites editorial prose nor withdraws unrelated source scopes. A corrected source is not an automatic correction of the public narrative; editors must assess and explain the change.

Party membership, company-register enrichment, association/other membership imports and other deferred sources remain outside these connectors. Access evidence and limitations are recorded in [source research](source-research.md); collection and review procedures are in [controlled enrichment operations](operations.md#controlled-enrichment-imports).

## Family, sensitive information and inference

The `family` type is for documented relationships with a demonstrable public interest, not family-tree generation. **Never infer kinship automatically** from names, addresses, social media, co-occurrence, photographs, schools or language models. Family claims need heightened editorial and legal scrutiny, especially for people without public roles.

Do not collect private contact details, home addresses, identity documents, credentials, children's data or special-category data without strict necessity and an appropriate lawful basis. Public availability does not remove these duties. `private_notes` must not become a store of sensitive information.

## Corrections and withdrawal

For a non-sensitive factual dispute, open an issue with the page URL, disputed claim and a public reference supporting the correction. Add no unnecessary personal data. Report private information exposure or vulnerabilities through the [private security channel](../SECURITY.md).

The responsible person must assess the request, withdraw visibility as a precaution where appropriate, correct the claim and arrange fresh review. Withdrawal must also cover dependent claims that no longer qualify for publication. Republication must not obscure a significant correction. There is no guaranteed response time or automated request-management process.

## Retention and minimisation

- Keep only passages and metadata needed to explain a claim and its review. Reassess usefulness, currency and lawfulness on correction, withdrawal or republication; indefinite retention must not be the default.
- Approve retention periods by category and purpose **before collecting real data**. There is no universally justified period or automatic purge.
- Include import history in that policy: it records request identity, admin requester where applicable, scope/date, mode, status, timestamps, aggregate counts and a safe failure message. It is operational evidence, not a source archive, proof of editorial approval or immutable audit trail. Restrict access; read-only admin screens are not a retention or deletion mechanism.
- Public withdrawal is not deletion: editorial data and review events may remain. Authorised deletion or anonymisation must account for dependencies, audit obligations, individual rights, exports and relevant copies.
- Configure and document separate backup and log retention periods, with restricted access, before handling real data. **After a restore, reapply corrections and withdrawals made since the backup.** See [operations](operations.md).
- Never commit databases, dumps, source documents, private-content screenshots or real personal data. Tests, demonstrations and reproductions must use explicitly fictitious names.

## Reading a historical view

Unknown date boundaries mean a relationship remains possible in a historical view, not that activity on that exact date is proven. A limited or truncated result is not a complete account of a person's relationships.
