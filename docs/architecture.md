# Architecture

Design decisions and invariants only; the code is the inventory of models, routes and deployment settings.

## Application shape

- One Django project on PostgreSQL. No separate frontend services, broker or shared packages until a concrete need exists.
- Slow official-source requests run in an import worker using the same image and database as the web service. It is not a general task platform.

## Editorial integrity

- Editorial writes and reviews are serialised by one PostgreSQL advisory lock. This prevents stale approvals and lock-order problems across related records; any replacement must keep that guarantee.
- Publication is a revocable permission, not an export. Every public surface (pages, evidence list, graph, connection counts) uses the shared visibility rule in [`public/selectors.py`](../apps/platform/ligacoes/public/selectors.py). A slug or UUID is never authorisation; private review data is never exposed.
- Editing a published relationship, its entities, sources or evidence invalidates its approval.
- The graph is supplementary: readable relationships and evidence stay available outside it, with temporal uncertainty and the difference between a displayed subset and complete coverage. Editorial rules are in the [methodology](methodology.md).
- Colour in the public interface encodes only the relationship kind, from a palette kept away from Portuguese party colours; entity kinds are shapes. Parties are never shown in their own colours. The palette lives in `frontend/src/styles.css` and the graph reads it from there.

## Imports

- Imports are operator-invoked, never scheduled. Parliament mandates and Government offices come from official identifiers, so apply publishes them automatically (`publish_imported`, recorded with no reviewer); editors withdraw afterwards, and a withdrawn (`rejected`) claim is never republished by imports. Other candidates need explicit editorial conversion and publication.
- Complete snapshots are validated before any write. Apply is atomic under the editorial lock; a failure leaves no partial import.
- Minimised, fingerprinted source revisions are kept privately, separate from editable claims. Changes, departures and returns invalidate linked approval rather than silently updating prose; only official office claims are then republished. An older snapshot cannot replace a newer one.
- Identities are matched only via official source identifiers (`SourceIdentity`), never by name. A used mapping cannot be retargeted or deleted.
- Parliament imports requested from the admin or API are queued as `ImportRun` rows in PostgreSQL. A constraint allows one queued or running job; request IDs are idempotent. Interrupted jobs fail without automatic retry; recovery needs a new request.

## Government and EpT enrichment

- Management commands outside the Parliament queue. Government offices auto-publish; EpT and biography-role rows stay private candidates until converted to a draft and explicitly published.
- Technical access to a public site is not by itself permission for automated reuse; checking source terms is the operator's responsibility.

## Deferred work

Party membership, company registers, other membership imports, automatic cross-source identity inference and OCR each need their own design (access and reuse, purpose, lawful basis, minimisation, review, failure behaviour). See [source research](source-research.md) and [operations](operations.md).
