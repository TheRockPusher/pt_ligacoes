# Design rationale

This document records trade-offs, not a model, route or deployment inventory. Read the implementation for those details.

- **Keep one application until a concrete need warrants more.** The editorial workload does not justify separate frontend services, a broker or shared packages. The import worker uses the same application image and PostgreSQL database to keep slow official-source requests outside web requests; it is not a general-purpose task platform.
- **Prefer simple serialisation to editorial throughput.** The shared PostgreSQL advisory lock deliberately serialises edits and reviews to prevent stale approval and lock-order inversions. Any replacement must preserve those guarantees across related records, not merely speed up an individual write.
- **Treat publication as revocable permission, not a one-off export.** Public surfaces must reuse the shared visibility boundary, never expose whole models or private review data, and never treat a slug or UUID as authorisation. Imports enter the editorial review process, not publication through SQL, bulk updates or status assignment.
- **Keep the graph supplementary.** A visual association can imply more than the evidence supports. Preserve readable relationships and evidence outside the canvas, explicit temporal uncertainty and the distinction between a displayed subset and complete coverage. Editorial interpretation belongs in the [methodology](methodology.md).

## Bounded official Parliament import

The importer is operator-invoked through the direct management command, protected admin or narrow bearer API, not a crawler or scheduled publication job. It discovers only the selected legislature's roster and biography downloads from two fixed AR catalogues. The [approved scope](methodology.md#official-parliament-import) is the serving parliamentary roster and relevant curricular fields; reuse requires attribution to Assembleia da República.

Fetching and complete-snapshot validation precede editorial writes. Dated mandate states select serving MPs; admin/API requests require exactly 230, and every selected cadastro identifier must have one biography. The direct CLI retains its explicit expected-count option. There are no partial roster/biography operations. Stable AR identifiers avoid name-based reconciliation, including with existing manually entered profiles. Raw downloads stay in memory; only the field allowlist and provenance are retained.

Dry-run is the default. Explicit apply uses the existing editorial transaction and lock, saving private, fingerprinted source revisions separately from editable claims. Identical observations reuse their revision; changes, departures and returns invalidate affected approval rather than silently updating or resurrecting published claims. Older as-of snapshots cannot replace newer imports. Validation failures leave no partial import, and database failures roll back the transaction. Human review remains the only route to publication.

The fetcher has its own narrow [SSRF boundary](../SECURITY.md#threats-and-limitations); accepting an editorial source URL does not authorise fetching it. Expanding the host, route or field allowlist is a design change, not routine source entry.

## Durable, narrow import control

Admin and API requests validate and enqueue `ImportRun` records in the existing PostgreSQL database; they never launch a subprocess or fetch a snapshot in the web request. Validation-only remains the default, but these requests write operational history, unlike direct CLI dry-run. The same UUID and normalised request/actor return the existing run; a changed request conflicts. A database constraint permits only one queued/running import globally.

A separate same-image worker owns a PostgreSQL session advisory lock across claim, fetch and completion, without holding an editorial transaction open during networking. Only that lock's owner can fail an abandoned running job. Apply commits draft changes and successful run metadata in one editorial transaction under the existing editorial lock. Crashes cannot leave a partial successful apply; interrupted jobs fail without automatic retry. Recovery requires operator assessment and an explicitly new request, not replay of a terminal UUID.

Admin authority is checked at request, execution and apply; read-only history does not confer execution or publication permission. The API uses an independent, disabled-by-default import-only bearer token and exposes aggregate results, never source material or requester identities. GitHub's manual workflow is a client of this boundary, not a deployment or database client. No release, PR, push or schedule triggers imports. External environment protection, worker migration ordering and recovery are [operational prerequisites](operations.md#controlled-parliament-imports), not guarantees made by repository configuration.

## Work requiring a separate design

Collection beyond the scoped AR importer, cross-source identity reconciliation, OCR and inference require a separate design covering source-specific access/reuse conditions, public-interest purpose, lawful basis, minimisation, human review and failure behaviour. The approved AR scope does not need a new generic legal-purpose gate for each run; nor does it establish universal permission to reuse public data. EpT was verified read-only, not integrated; company/association joins and openAR links remain deferred in the [source research](source-research.md).

Do not add infrastructure speculatively or equate a successful application build with operational readiness. Infrastructure changes and the prerequisites for real data belong in the [operator runbook](operations.md).
