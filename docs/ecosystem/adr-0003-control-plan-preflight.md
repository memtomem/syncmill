# ADR-0003 — SyncMill emits a body-free bounded control plan

Status: Accepted · Date: 2026-08-12

## Context

The legacy Toolgraph integration invokes one advisory preflight per mapped
identity. It proves selector evidence but does not describe the orchestration
topology whose execution SyncMill is about to start. A graph-shaped exchange is
useful only if responsibility stays honest: SyncMill knows the strategy and its
upper bounds; Toolgraph knows graph-backed principal/tool policy; neither one is
a durable workflow engine in this integration.

## Decision

- `integration.toolgraph.control_plan_enabled` is an explicit opt-in and is
  disabled by default. The legacy path stays unchanged when it is false.
- SyncMill projects route, pipeline, compete, council, and decompose into
  `toolgraph.control-plan` schema v1. The plan declares only producer/run ids,
  principals and qualified candidate tools, typed nodes and edges, routing
  ownership, validator presence, and call/parallel/time/cycle upper bounds.
- Prompts, output, patches, gate commands, model selectors, memory bodies,
  credentials, and filesystem paths never enter the exchange artifact.
- Toolgraph validates and evaluates the whole plan under one collision-safe
  graph-state bracket. SyncMill verifies the exact plan digest, run/profile,
  complete principal and candidate coverage, known findings/reject reasons,
  and the non-null graph-state token.
- Toolgraph remains advisory. SyncMill applies the existing unknown, drift, and
  unavailable policies; in strict mode a structural finding or invalid evidence
  blocks before memory registration, worktree creation, or agent execution.
- Validated input and output bytes are persisted as `control-plan.json` and
  `control-preflight.json` under the same run id as the envelope.

## Consequences

The batch removes cross-generation identity fan-out and makes the prospective
control topology auditable. It does not prove actual runtime conformance, add
checkpoints or idempotency, resume a run, mediate individual tool calls, or
change OpenTelemetry emission. Those are separately versioned execution and
observability concerns.

Rollback is one field: set `control_plan_enabled = false` to return to the
existing preflight adapter, or `enabled = false` to disable Toolgraph entirely.
