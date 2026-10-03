# Editorial methodology and personal data

## Purpose and publication

Ligações PT is a non-profit tool for journalists. Public information about politicians that can be scraped easily and repeatedly is in scope, subject to the exclusions below. Never bypass CAPTCHA, WAF or login barriers.

Everything published must be verifiable through a source URL and a minimal supporting passage, with attribution. Verifiable source observations publish automatically, including declared interests, explicit biography roles and name-only profiles; incomplete or incompatible observations remain private. Editors can withdraw incorrect or inappropriate claims. There is no mandatory human-review gate for these imports.

A connection is a typed, dated documentary claim. **It does not imply favouritism, co-ordination or wrongdoing.** A missing connection proves nothing; a shared surname or former employer does not establish a relationship. A profession label or company mention alone establishes neither employment nor a role.

Contacts (meetings, hearings, gifts, hospitality, travel), contracts, subsidies and EU funds record events and participants, not relationship claims or evidence of influence. Events publish only when every participant is public and anchored by an official identifier. Source coverage and access limits belong in [Sources](sources.md).

## Identity

Never merge people by name alone. Reuse an official identity or a uniquely corroborated namesake under the [identity rules](architecture.md#identity). Wikidata and non-effective parliamentary statuses can corroborate identity, never substantiate a public office.

Without sufficient or unambiguous corroboration, keep separate public profiles rather than guess. A name-only person is scoped to the source context; joining profiles requires corroboration, never just a matching name. Profiles known only by a source name carry a provenance badge; it is not a claim of independently verified identity. Organisations use a valid legal-person identifier or an unambiguous match to an anchored name; otherwise they retain the name declared in the source.

Ordinary mapping edits and suggestion acceptance cannot redirect a used source identity. Audited reconciliation is the deliberate exception: it preserves source-owned assertions, evidence, withdrawals and original merge records. Old public URLs redirect only to a public canonical profile.

## Declared interests

Declared interests are **declared by the person, not independently checked**. The site distinguishes them from officially documented connections and dates them by the declaration; consultation time is separate.

`declared_client` means a recipient named in the person's declaration, with an explicit legal-person NIPC. It is not proof that the person personally provided a service, nor evidence of a company-to-client relationship. Historical management roles do not establish current shareholding.

For EpT, retain only permitted public interest fields, respecting opposition and professional secrecy. A declaration has no stable direct permalink: cite the public portal and enough locating context to find the passage, never a guessed URL.

## Exclusions and minimisation

Do not collect or publish:

- Natural-person NIFs, contacts, addresses, birth data, photographs or identity documents.
- Income, assets or spouse/partner holdings.
- Party membership, party offices, candidacies, electoral lists or coalitions, including the EP national party. Only AR and EP parliamentary groups are shown; group membership establishes none of these other affiliations.
- Associative or Masonic affiliation, criminal data or sanction status, including administrative offences.

Never infer kinship from names, addresses, social media, co-occurrence or language models. A family relationship requires documentary evidence and demonstrable public interest; do not collect children's data or excluded sensitive information in private notes either.

Never read, retain or log a natural person's NIF, even if published by the source. Procurement, subsidy and fund datasets retain only legal-person parties; drop natural-person parties and skip events without a remaining legal-person counterpart. Keep only the passage needed to explain the claim, not whole source documents.

## Dates and historical views

Publication date is not a relationship boundary. Leave unknown boundaries unknown. **“Em curso”** means the source explicitly marks the relationship as current; a missing end date alone does not establish that it continues.

An unknown boundary means a relationship is possible on that date, not proven. A truncated result is not a complete account. A derived path (“Como estão ligados?”) joins separately evidenced public claims and, optionally, public events. It is computed on request, never stored as a claim, and excludes hubs by default. It implies no acquaintance, co-ordination or wrongdoing; not finding a path proves nothing.

## Corrections and withdrawal

Report factual disputes by issue with the page URL, the claim and a public reference; report private-data exposure via [SECURITY.md](../SECURITY.md). The responsible person withdraws visibility where appropriate, including dependent claims, and corrects the record. Republication must not obscure a significant correction.

Changed, ceased or returning source observations invalidate dependent publication; qualifying observations can publish again automatically. An editorial withdrawal blocks automatic republication, including after source changes or returns. Hiding an entity, source or evidence affects public visibility, but does not replace explicit withdrawal of a disputed claim or event.

## Retention

- Keep only what explains a claim and its publication or correction; indefinite retention is not the default.
- Approve retention periods for data, import history, backups and logs before handling real data.
- Withdrawal is not deletion; deletion must account for dependencies, individual rights and copies.
- **After a restore, reapply corrections and withdrawals made since the backup.**
- Never commit databases, dumps, source documents or real personal data; fixtures are fictional.
