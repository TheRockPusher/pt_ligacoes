# Design rationale

This document records trade-offs, not a model, route or deployment inventory. Read the implementation for those details.

- **Keep one application until a concrete need warrants more.** The editorial workload does not justify separate frontend services, queues or shared packages. New processes need an actual consumer and a clear operational owner.
- **Prefer simple serialisation to editorial throughput.** The shared PostgreSQL advisory lock deliberately serialises edits and reviews to prevent stale approval and lock-order inversions. Any replacement must preserve those guarantees across related records, not merely speed up an individual write.
- **Treat publication as revocable permission, not a one-off export.** Future public surfaces must reuse the shared visibility boundary, never expose whole models or private review data, and never treat a slug or UUID as authorisation. Future imports must enter the editorial review process, not publish through SQL, bulk updates or status assignment.
- **Keep the graph supplementary.** A visual association can imply more than the evidence supports. Preserve readable relationships and evidence outside the canvas, explicit temporal uncertainty and the distinction between a displayed subset and complete coverage. Editorial interpretation belongs in the [methodology](methodology.md).

## Work requiring a separate design

Automated collection, identity reconciliation, OCR and inference are not promised capabilities. Before adding them, establish the public-interest purpose, legal basis, human review process and failure behaviour. A source fetcher also needs a dedicated SSRF design; URL validation alone is insufficient. See [security limitations](../SECURITY.md).

Do not add infrastructure speculatively or equate a successful application build with operational readiness. Infrastructure changes and the prerequisites for real data belong in the [operator runbook](operations.md).
