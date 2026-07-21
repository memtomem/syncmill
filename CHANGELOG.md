# Changelog

All notable changes to SyncMill are documented here.

## Unreleased

### Added

- Opt-in Compare & Apply rooms with persistent isolated member worktrees,
  detached rounds, immutable candidate manifests, explicit selection, source and
  digest drift checks, and application through the standard run-artifact path.
- Project-scoped isolated interactive sessions with verified tmux attachment,
  artifact checkpoints, and explicit workspace cleanup. The original direct
  interactive session commands remain compatible.
- Sessions and Rooms tabs in the full TUI, including explicit confirmations for
  candidate selection, room application, and isolated-session closure.
- CI coverage for Python 3.12 through 3.14 plus an opt-in POSIX/tmux process job.

- Optional `syncmill[phoenix]` trace streaming via batched OTLP/HTTP. The
  canonical OTLP-JSON file remains intact, streaming is best-effort, and the
  run envelope records Phoenix export and drop counts without trace bodies.
  A requested stream whose optional extra is not installed is reported as
  `unavailable`, distinct from an attempted export that fails.

### Fixed

- Room safety contracts now bind quality gates to real worktrees, include clean
  HEAD identity in source fingerprints, cross-check candidate manifests against
  SQLite digests, and use compare-and-swap confirmation for TUI selection/apply.
- Apply bookkeeping failures now compensate by rolling the source patch back;
  irreconcilable source/artifact or Room-finalization failures enter explicit
  recovery state instead of reporting a reusable selection.
- Isolated-session checkpoints use a strict temporary Git index, preserve the
  interactive session's staging state, fail instead of emitting false
  `no_changes` artifacts, and serialize checkpoint/close lifecycle changes.
- Detached TUI Room workers receive a private snapshot of the resolved config;
  Room refresh reloads configuration, stale-room pruning cleans persistent
  worktrees first, and disappearing providers no longer strand a running lease.
- `room create` now accepts the documented `-a` alias. The POSIX CI job performs
  a real tmux create, PTY attach/detach, reconnect, and cleanup cycle.

- Durable TUI workers now clean up their child process group and release the
  per-board lock if the detached manager dies. Limited mode also survives path
  resolution failures and rebinds its job registry after configuration recovery.
- Nested configuration reads only the documented `SYNCMILL_*` environment
  namespace; ambient bare variables such as `ENABLED`, `DB_PATH`, and
  `STATE_DIR` no longer alter SyncMill settings.
- POSIX work-board databases and SQLite sidecars are created and maintained as
  private `0600` files. Newly created board directories use `0700`.
- Migration artifacts now bind apply, verify, and rollback to the planned
  physical destination store. A changed path, endpoint, collection, or history
  database fails closed before opening the destination or changing the ledger.
- Legacy schema-v1 migration artifacts without a destination binding are no
  longer adopted from the current working-directory configuration. Unapplied
  artifacts must be replanned; already-applied artifacts require manual recovery
  from `state.json` target IDs using the original destination settings.

## 0.1.1 — 2026-07-12

First formal GitHub release, closing the pluggable memory-backend roadmap.

### Added

- Staged, source-retaining memory migration with `plan`, `apply`, `verify`, and
  guarded `rollback` commands.
- Bidirectional migration among memtomem, Mem0, and persistent LangGraph
  `AsyncSqliteStore` backends.
- Optional `syncmill[langgraph]` backend using local multilingual FastEmbed and
  SQLite persistence without LLM calls.
- Persistent work board, bilingual dashboard and user guides, named team
  profiles, read-only MCP gateway, run envelopes, quality gates, circuit
  breaker, live monitoring, and durable interactive sessions.

### Changed

- Mem0 is hardened for verbatim storage, exact Qdrant lifecycle operations,
  reliable teardown, explicit local-lock guidance, and first-import telemetry
  semantics.
- Memory status output uses backend-specific safe-field allowlists.
- Migration reconciliation is resumable and linear-time, with immutable
  digests, source-drift checks, empty-target gates, and exact verification
  receipts.

### Operations

- Migration snapshots contain plaintext memory and are retained until manually
  removed; protect `~/.syncmill/migrations/` accordingly.
- Changes to Mem0, LangGraph, or migration SDK boundaries require their opt-in
  real integration smokes before release.
