# ADR-0004 — MillFlow freezes control evidence at the execution boundary

Status: Accepted · Date: 2026-08-12

## Context

MillFlow already owns a validated DAG, SQLite state transitions, bounded retries,
worktree isolation, and recovery. Replacing that scheduler with another agent or
workflow framework would create two authorities for readiness, retries, and
completion. The missing layer is evidence: the graph approved at planning time
must be the graph checked immediately before execution, and a run/resume must be
diagnosable without placing prompts, outputs, patches, or paths in telemetry.

Toolgraph control-plan v1 already describes body-free prospective topology.
SyncMill's OTLP shape already gives Tracegraph a logical parent plus same-trace
links. The integration can therefore use existing contracts without changing
either consumer's schema.

## Decision

- Planning projects the validated MillFlow DAG to `toolgraph.control-plan` v1,
  canonicalizes it once, and stores the exact bytes plus SHA-256 digest beside
  the workflow. Prompts, goals, commands, outputs, patches, repository paths,
  model selectors, and credentials are excluded.
- The projection declares explicit start/end, root fan-out, dependency joins,
  final validation, failure paths, per-node retry/timeout bounds, total calls,
  run-wide parallelism, and zero control cycles. Code owns every edge.
- Every `run` and `resume` gets a fresh durable `wfx-...` execution id. SyncMill
  verifies the stored digest and current principal/tool binding, then submits the
  exact frozen bytes to Toolgraph before creating execution worktrees. Strict
  findings block with zero workflow-agent calls.
- Frozen deadline, parallelism, output, and node-timeout bounds are ceilings.
  Later configuration may tighten them but cannot silently loosen an approved
  workflow. Identity or candidate-tool drift requires a new plan.
- Workflow history stores the execution id, action, frozen-plan digest,
  preflight decision, stop reason, trace receipt, and timestamps. The final
  applyable run artifact references the frozen control-plan digest.
- OTLP spans use unique occurrence ids (`wfnode:<node>:attempt:<n>`) and stable
  names (`workflow:<node>`). The first consumed dependency is the logical
  parent; additional fan-in dependencies are same-trace links. A retry is caused
  by its previous failed attempt. Resume never emits a foreign-trace edge:
  reused durable results become local body-free `checkpoint-read:<node>` spans.
- The run root carries Toolgraph's existing digest, graph generation, and
  producer verdict attributes. Trace export remains fail-open; strict preflight
  remains fail-closed.

## Consequences

The approved prospective graph, enforcement result, actual occurrence graph,
and final artifact can be joined without a Toolgraph or Tracegraph migration.
Tracegraph normalization remains intentionally lossy for multi-parent joins: one
cause is primary and the rest are links, while the raw OTLP file preserves both.

This does not add an infrastructure durable-execution engine, cross-execution
span links, per-tool-call mediation, side-effect idempotency, or runtime proof
that every possible control-plan edge was taken. MillFlow's SQLite state handles
application recovery; callers must still give externally mutating tools their
own idempotency contracts. Rollback is configuration-only for new plans:
disable `control_plan_enabled` or Toolgraph. A workflow already carrying frozen
evidence continues to verify that artifact rather than silently discarding it.
