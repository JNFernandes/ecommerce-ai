# Architecture Decision Records

Architectural decisions for `ecommerce-ai` are recorded here, as the project constitution requires
(Documentation Standards).

## Conventions

- One decision per file, named `NNNN-short-title.md` with a four-digit sequential number.
- Numbers are never reused or renumbered. To reverse a decision, write a new ADR and set the old
  one's status to `Superseded by NNNN`. The superseded ADR is kept.
- Start from [`template.md`](template.md) (Context / Decision / Consequences).
- Add every new ADR to the index below in the same PR.
- A runtime dependency that duplicates a capability of the stack in the constitution requires an
  ADR before the PR can be merged.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-token-passthrough.md) | Call the platform with the caller's token, not a service account | Accepted |
| [0002](0002-human-in-the-loop-for-mutating-tools.md) | Human-in-the-loop approval for mutating tools | Accepted |
| [0003](0003-fallback-and-cost-caps.md) | Deterministic fallbacks, timeouts and cost caps for LLM steps | Accepted |
| [0004](0004-evals-as-release-gate.md) | Evals as a release gate | Accepted |
| [0005](0005-layered-guardrails.md) | Layered, deterministic guardrails around the chat model | Accepted |
| [0006](0006-vendor-neutral-tracing.md) | Vendor-neutral tracing behind an attribute allowlist | Accepted |
