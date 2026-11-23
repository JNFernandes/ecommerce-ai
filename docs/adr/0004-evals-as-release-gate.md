# 0004. Evals as a release gate

- **Status**: Accepted
- **Date**: 2026-11-23

## Context

Unit tests use a fake LLM, so they cannot catch a prompt, model or tool change that makes the real
system worse or unsafe: leaking another user's data, calling a forbidden tool, skipping an approval
interrupt or obeying injected instructions. Quality drifts silently without a measured baseline.

## Decision

We will keep a versioned eval suite in this repository, run with verdict
(https://github.com/JNFernandes/verdict): at least 30 cases covering order lookups, triage
explanations, cancel approval interrupts, forbidden tool use, ownership leaks and prompt
injection, with at least one case per category. CI fails when the composite score drops below the
agreed threshold or regresses by more than 5% against the last recorded run on `main`. The
threshold and baseline live in the repository and change only through a PR. Any failing safety
case blocks the merge regardless of the score. Every change to a prompt, the model id, a tool or
the gatekeeper attaches a suite run to its PR.

## Consequences

- Behaviour changes are measured before they merge, and safety regressions cannot ship.
- CI needs an LLM key and incurs token cost for eval runs; the suite must stay small and focused.
- The threshold is still to be agreed (`TODO(EVAL_THRESHOLD)`), in the story that adds the suite.
- Live-model results vary, so the 5% tolerance and the baseline must be revisited as the suite
  grows.
