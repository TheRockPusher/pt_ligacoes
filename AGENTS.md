# Agent guidance

- Use the Makefile, manifests, lockfiles and existing code as the authority for commands, tooling and behaviour. Do not duplicate them in documentation or introduce parallel tooling.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) for collaboration and verification; [design rationale](docs/architecture.md) before changing architectural boundaries.
- Read the [methodology](docs/methodology.md) before changing editorial or identity handling. Never merge people by name alone or publish ambiguous matches. Keep real records, source documents, private notes and credentials out of Git and fixtures.
- Read [operations](docs/operations.md) before deployment, release automation or database recovery. Commits, pushes, publication and infrastructure changes require an explicit user request; a code change is not authorisation to deploy or seed production.
- Preserve ignored project-local configuration in `.agents/`, `.claude/`, `.omp/` and `skills-lock.json`; do not replace it with user-wide setup. Development runs on the VPS, not the user's PC; production belongs on Railway.
- Write documentation in concise British English; keep the public interface in Portuguese. Document non-obvious decisions, constraints and human procedures, not inventories recoverable from code.
