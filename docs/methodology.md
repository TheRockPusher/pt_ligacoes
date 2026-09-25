# Editorial methodology and personal data

## What a connection means

A connection is a specific, typed claim, placed in time and supported by a documentary passage. **It does not imply favouritism, co-ordination, friendship or wrongdoing.** A missing connection does not prove that none exists. A shared surname, university or employer at different times does not establish a personal relationship.

The catalogue starts empty; the official Parliament importer can now prepare non-public editorial material. It does not classify arbitrary relationships or publish automatically. The scoped AR reuse below is approved for documenting parliamentary representation and relevant public-role biographies, subject to attribution, minimisation and human review. For other sources or expanded uses, establish the public-interest purpose, lawful basis, proportionality and correction process, considering the GDPR, personality rights, copyright and defamation. Public availability alone is not permission for unrestricted reuse. This policy is not legal advice.

## Reviewing a claim

1. **Find the source.** Prefer official or primary documents. Record the title, publisher, URL, access date, publication date if known, and a precise passage reference.
2. **Resolve identity cautiously.** Use enough context without collecting excessive identifiers. Keep namesakes separate; leave uncertain matches in draft.
3. **Distinguish dates.** Document publication does not establish when a relationship began or ended. Leave unknown boundaries unknown; never invent precision for the graph.
4. **Write only what the evidence supports.** Choose the most accurate relationship type and retain relevant context, including termination or dispute. Do not present hypotheses as facts.
5. **Review explicitly.** A reviewer authorised to publish must assess public interest, the source and passage, identity, dates and each element's visibility. Saving a draft is not approval; changes to published content require fresh review before republication.

Sources are references, not guarantees. Ordinary editorial source URLs are not automatically fetched, archived or verified; the bounded AR importer below is the exception for fetching. URLs can change or disappear. Quote only the minimum necessary; do not copy whole documents into private notes or Git.

## Official Parliament import

AR's [open-data reuse conditions](https://www.parlamento.pt/Cidadania/paginas/dadosabertos.aspx) permit reuse with attribution to Assembleia da República. The approved purpose is to document who serves in Parliament and relevant disclosed curricular information, not to aggregate every publicly available personal detail. This scoped permission does not require a new broad legal-purpose assessment for each run; it does not extend to unrelated sources or purposes.

The importer selects the serving roster from dated mandate states, expects 230 MPs by default and requires a unique biography match by AR cadastro identifier. It retains names and parliamentary identifiers, constituency, supplied parliamentary-group intervals, the selected mandate state/dates, profession, qualifications and disclosed roles. It excludes birth details, contact information, photographs and other unselected fields; raw payloads are not persisted. Biography role text is source material, not a verified organisation identity, a complete employment history or permission to infer association membership. Supplied group intervals must not be presented as a current affiliation when they have already ended.

Dry-run fetches and validates without database writes; only explicit `--apply` persists private source revisions and non-public draft public-office claims. Revisions retain minimised fields, source URLs, retrieval/as-of dates and a content fingerprint separately from editorial prose. Repeat observations do not duplicate revisions. Changed or ceased observations withdraw affected claims; returning observations require fresh review rather than restoring approval. The importer does not merge manually entered profiles by name, infer company/association joins or publish any record. Review all relevant entities, sources, passages and visibility before publication. See the [local command procedure](../README.md#official-parliament-import).

Incomplete counts, missing/ambiguous biography matches, unsafe downloads and relevant schema/date ambiguities stop the import; apply is atomic. Do not bypass these failures by accepting partial rosters or weakening the expected-count guard. Source corrections still need editorial assessment and the withdrawal/retention procedures below. EpT access verification and the deferred integrations are documented in [source research](source-research.md), not enabled importers.

## Family, sensitive information and inference

The `family` type is for documented relationships with a demonstrable public interest, not family-tree generation. **Never infer kinship automatically** from names, addresses, social media, co-occurrence, photographs, schools or language models. Family claims need heightened editorial and legal scrutiny, especially for people without public roles.

Do not collect private contact details, home addresses, identity documents, credentials, children's data or special-category data without strict necessity and an appropriate lawful basis. Public availability does not remove these duties. `private_notes` must not become a store of sensitive information.

## Corrections and withdrawal

For a non-sensitive factual dispute, open an issue with the page URL, disputed claim and a public reference supporting the correction. Add no unnecessary personal data. Report private information exposure or vulnerabilities through the [private security channel](../SECURITY.md).

The responsible person must assess the request, withdraw visibility as a precaution where appropriate, correct the claim and arrange fresh review. Withdrawal must also cover dependent claims that no longer qualify for publication. Republication must not obscure a significant correction. There is no guaranteed response time or automated request-management process.

## Retention and minimisation

- Keep only passages and metadata needed to explain a claim and its review. Reassess usefulness, currency and lawfulness on correction, withdrawal or republication; indefinite retention must not be the default.
- Approve retention periods by category and purpose **before collecting real data**. There is no universally justified period or automatic purge.
- Public withdrawal is not deletion: editorial data and review events may remain. Authorised deletion or anonymisation must account for dependencies, audit obligations, individual rights, exports and relevant copies.
- Configure and document separate backup and log retention periods, with restricted access, before handling real data. **After a restore, reapply corrections and withdrawals made since the backup.** See [operations](operations.md).
- Never commit databases, dumps, source documents, private-content screenshots or real personal data. Tests, demonstrations and reproductions must use explicitly fictitious names.

## Reading a historical view

Unknown date boundaries mean a relationship remains possible in a historical view, not that activity on that exact date is proven. A limited or truncated result is not a complete account of a person's relationships.
