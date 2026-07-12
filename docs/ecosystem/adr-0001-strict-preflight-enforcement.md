# ADR-0001 — SyncMill owns strict Toolgraph preflight enforcement

Status: Accepted · Date: 2026-07-12

## Context

Toolgraph produces deterministic advisory evidence for one identity. SyncMill
owns orchestration and is therefore the only layer that can honestly stop a run.
The existing `profile` chooses Toolgraph's reject set; the reserved SyncMill
`mode = "strict"` decides whether that set is enforced.

## Decision

- Toolgraph keeps its advisory artifact and exit-code contract. SyncMill writes
  a separate strict aggregate and enforces it before memory, worktree, or agent
  execution.
- Strict evaluates every distinct mapped identity that can participate. All
  validated artifacts must echo the request and share one graph generation.
- The selected profile remains independent of enforcement mode. Every rejection
  emitted by that profile blocks, except `DRIFTED`, which warns by default.
- Missing mappings, unknown agents/tools, and ambiguous tools block by default;
  drift warns by default; operational unavailability blocks by default. Each
  category has an explicit `block|warn` policy override.
- Malformed, unsupported, mismatched, incomplete, or cross-generation evidence
  always blocks and has no fail-open override.

## Consequences

Strict is opt-in and requires reporting plus a non-empty qualified ambient tool
surface. It is a pre-dispatch posture gate over the stored graph snapshot, not
per-call MCP mediation and not a graph-freshness guarantee. Roll back immediately
with `mode = "advisory"` or `enabled = false`.
