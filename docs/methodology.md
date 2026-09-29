# Editorial methodology and personal data

## What a connection means

A connection is a typed claim, placed in time and supported by a documentary passage. **It does not imply favouritism, co-ordination or wrongdoing.** A missing connection does not prove none exists; a shared surname or former employer does not establish a relationship.

Contacts (meetings, hearings, gifts, hospitality, travel) and contracts, subsidies or EU funds record that a contact or transaction occurred; they do not show influence or who decided. These are events with participants and roles, not relationship claims. They publish automatically only when every party is a public entity anchored by an official identifier; editors can withdraw them. Criminal or sanction status, including administrative offences, is never a relationship or graph edge (GDPR Art. 10; Lei 58/2019).

Official-identifier claims publish automatically within the scope below; editors withdraw wrong claims afterwards. Name-only persons, declared interests and biography roles remain private candidates until editorial review. Any new source or expanded use needs an established public-interest purpose, lawful basis, proportionality and correction process. Public availability alone is not permission to process personal data. The maintainer treats reuse permissions for the catalogued sources as granted for this non-profit project; that does not remove these safeguards or permit bypassing access challenges.

## Political affiliation

Only parliamentary groups are shown: Assembleia da República groups and European Parliament political groups. Never import or display party membership, party office, candidacies, electoral lists or coalitions, or the EP national party. Group membership implies none of these. Political affiliation is special-category data (GDPR Art. 9); Constitution art. 35(3) restricts computerised processing of party affiliation.

## Reviewing a claim

1. **Source:** prefer official or primary documents; cite a precise passage and quote only the minimum.
2. **Identity:** never merge people or organisations by name alone; leave uncertain matches in draft.
3. **Dates:** publication date is not a relationship boundary. Leave unknown boundaries unknown.
4. **Content:** write only what the evidence supports. Employment, directorship, public office and professional activity are distinct; a profession label or company mention alone establishes none of them.
5. **Review:** an authorised reviewer explicitly approves public interest, source, identity, dates and visibility for manual and candidate-derived claims. A saved draft is not approval; changed content needs fresh review.

## Official sources in scope

- **Automatic claims:** AR mandates, committees, parliamentary groups, delegations and friendship groups; Government offices and portfolio structure; EpT holder offices; SIOE supervision and succession; GLEIF consolidation parents; EP mandates and memberships. Publication requires official identifiers and public entities; an existing private entity is never made public by an importer.
- **Reviewed candidates:** name-only people (gabinete staff, SIOE/ETF board members and AR external-body elections), declared interests (EpT declarations and AR historic registo de interesses), and biography roles. A name or profession alone establishes neither identity nor employment.
- **Assembleia da República:** open data reused with attribution. Retain parliamentary identifiers, constituency, group intervals, profession, qualifications and disclosed roles; exclude birth details, contacts and photographs. Role text is not verified employment.
- **EpT declarations:** one already-reviewed holder at a time, never a name search. Only public professional activity and the declarant's company interests; no income, assets, associations, party organs or spouse holdings. Only fields of the public registo de interesses may be republished (Lei 52/2019 art. 17(14)). No stable declaration permalink exists; evidence cites the public portal with locating context, never a guessed URL. Do not notify EpT.

Identity review, candidate conversion and publication are separate permissions. Official links, coverage and access limits belong in [Sources](sources.md); the linking model is described in [Architecture](architecture.md).

## Identities and source changes

- Link automatically only through official identifiers, never names. Before creating a person from a new official identifier, check public namesakes: pending `IdentitySuggestion` records block creation and keep claims private until an editor decides.
- Name-only people never create entities automatically. Wikidata provides identity hints and suggestions, never claim evidence or automatic matches.
- A source identity mapping (scheme + official ID) is immutable once used; it cannot be changed to move claims.
- Changed, ceased or returning observations invalidate approval of dependent claims without rewriting editorial prose. Official-identifier claims may qualify again for automatic publication; other claims need fresh review. Imports never republish an editor-withdrawn claim. Editors assess and explain significant corrections.

## Family and sensitive information

Use `family` only for documented relationships of demonstrable public interest. **Never infer kinship** from names, addresses, social media, co-occurrence or language models. Do not collect contact details, home addresses, identity documents, children's data or special-category data without strict necessity and a lawful basis, including in private notes.

## Corrections and withdrawal

Report factual disputes by issue with the page URL, the claim and a public reference; report private-data exposure via [SECURITY.md](../SECURITY.md). The responsible person withdraws visibility as a precaution where appropriate (including dependent claims), corrects and arranges fresh review. Republication must not obscure a significant correction.

## Retention and minimisation

- Keep only what explains a claim and its review; indefinite retention is not the default.
- Never read, retain or log a natural person's NIF, or store birth data, even where a source publishes them. Procurement, subsidy and fund datasets retain only legal-person parties; drop natural-person parties entirely and skip an event without a remaining legal-person counterpart.
- Approve retention periods for data, import history, backups and logs before handling real data.
- Withdrawal is not deletion; deletion must account for dependencies, individual rights and copies.
- **After a restore, reapply corrections and withdrawals made since the backup.**
- Never commit databases, dumps, source documents or real personal data; fixtures are fictional.

## Reading a historical view

An unknown date boundary means a relationship is possible on that date, not proven. A truncated result is not a complete account.

A derived path (“Como estão ligados?”) joins separately evidenced published claims and, optionally, public events. It is computed on request, never stored as a claim, and excludes hubs by default. It implies no acquaintance, co-ordination or wrongdoing; not finding a path proves nothing.
