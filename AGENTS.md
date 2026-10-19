# Working in Watcher

If a local `docs/handoff.md` exists, read it first; it contains machine/session state and is deliberately Git-ignored. Then read [docs/implementation.md](docs/implementation.md). Consult [docs/design.md](docs/design.md) and [docs/open-questions.md](docs/open-questions.md) for accepted requirements and their implementation dispositions.

- Product direction is accepted and the first version is implemented. Continue from the existing application; do not restart implementation or the product interview. Remaining work is validation, hardening, and user-directed follow-up.
- Stay close to Prefect's existing behavior, APIs, deployments, dashboard, and notification blocks. Build a small macOS integration around it.
- Make routine implementation decisions through research and experiments. Ask the maintainer only when concrete evidence exposes a material change to accepted behavior, scope, or complexity.
- Simplicity and maintainability matter. Resource efficiency is a preference, not a hard budget or a reason for elaborate infrastructure.
- Keep actual private tasks, configuration, and authentication files in a selectable workspace outside this repository. Live execution data, logs, and code caches reside in the Git-ignored `.watcher-runtime/<workspace-id>/` inside this application checkout; portable snapshots remain in the external workspace. Never version private runtime data. Only generic examples, templates, and synthetic test fixtures belong in versioned application files.
- Use official documentation/source for version-sensitive Prefect behavior. Distinguish confirmed behavior, proposed mechanisms, and unverified assumptions.
- Verify meaningful integration and failure scenarios, especially lifecycle controls, snapshots/restoration, file reloads, and long-downtime catch-up. Keep documentation aligned with actual implementation and report limitations honestly.

- Keep machine-specific observations, private task details, local paths, and session transcripts in ignored local handoffs rather than public documentation.
- Preserve `.watcher-runtime/` when cleaning build artifacts. It holds persistent private data; never use `git clean -fdx` on an operational checkout.
- Do not restart a running Watcher, rerun setup/tests, or execute private tasks merely because a session begins or ends. Use isolated synthetic workspaces for validation requested by a change.
