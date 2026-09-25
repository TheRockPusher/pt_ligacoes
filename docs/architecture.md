# Design rationale

This document records trade-offs, not a model, route or deployment inventory. Read the implementation for those details.

- **Keep one application until a concrete need warrants more.** The editorial workload does not justify separate frontend services, queues or shared packages. New processes need an actual consumer and a clear operational owner.
- **Prefer simple serialisation to editorial throughput.** The shared PostgreSQL advisory lock deliberately serialises edits and reviews to prevent stale approval and lock-order inversions. Any replacement must preserve those guarantees across related records, not merely speed up an individual write.
- **Treat publication as revocable permission, not a one-off export.** Public surfaces must reuse the shared visibility boundary, never expose whole models or private review data, and never treat a slug or UUID as authorisation. Imports enter the editorial review process, not publication through SQL, bulk updates or status assignment.
- **Keep the graph supplementary.** A visual association can imply more than the evidence supports. Preserve readable relationships and evidence outside the canvas, explicit temporal uncertainty and the distinction between a displayed subset and complete coverage. Editorial interpretation belongs in the [methodology](methodology.md).

## Bounded official Parliament import

The management command is an operator-invoked importer, not a crawler, public endpoint or scheduled publication job. It discovers only the selected legislature's roster and biography downloads from two fixed AR catalogues. The [approved scope](methodology.md#official-parliament-import) is the serving parliamentary roster and relevant curricular fields; reuse requires attribution to Assembleia da República.

Fetching and complete-snapshot validation precede any writes. Dated mandate states select serving MPs; the expected count defaults to 230, and every selected cadastro identifier must have one biography. Stable AR identifiers avoid name-based reconciliation, including with existing manually entered profiles. Raw downloads stay in memory; only the field allowlist and provenance are retained.

Dry-run is the default. Explicit apply uses the existing editorial transaction and lock, saving private, fingerprinted source revisions separately from editable claims. Identical observations reuse their revision; changes, departures and returns invalidate affected approval rather than silently updating or resurrecting published claims. Older as-of snapshots cannot replace newer imports. Validation failures leave no partial import, and database failures roll back the transaction. Human review remains the only route to publication.

The fetcher has its own narrow [SSRF boundary](../SECURITY.md#threats-and-limitations); accepting an editorial source URL does not authorise fetching it. Expanding the host, route or field allowlist is a design change, not routine source entry.

## Work requiring a separate design

Collection beyond the scoped AR importer, cross-source identity reconciliation, OCR and inference require a separate design covering source-specific access/reuse conditions, public-interest purpose, lawful basis, minimisation, human review and failure behaviour. The approved AR scope does not need a new generic legal-purpose gate for each run; nor does it establish universal permission to reuse public data. EpT was verified read-only, not integrated; company/association joins and openAR links remain deferred in the [source research](source-research.md).

Do not add infrastructure speculatively or equate a successful application build with operational readiness. Infrastructure changes and the prerequisites for real data belong in the [operator runbook](operations.md).
