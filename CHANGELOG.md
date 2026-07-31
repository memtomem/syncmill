# Changelog

All notable changes to SyncMill are documented here.

## Unreleased

### Added

- **Difficulty-based model routing (experimental, default off).** A new
  `[model_routing]` config section maps `low`/`medium`/`high` tiers to
  user-defined provider model selectors, passed as `claude --model` /
  `codex -m` / `kimi -m`. `syncmill run --difficulty low|medium|high` applies
  the mapped selector for the final eligible lineup (even under `mode = "off"`);
  `mode = "observe"` (or `--difficulty auto`) classifies the prompt with the
  deterministic `prompt-complexity-v1` heuristic and records the canonical
  decision under the run plan's `model_routing` block without passing any
  model flag. One immutable decision per top-level run — every strategy phase,
  fallback, and fan-out shares it via a reset-safe ContextVar scope; agent
  ordering, capability matching, gates, breaker, and winner selection are
  untouched. `syncmill continue` preserves **selector affinity** (the parent
  run's stored selector, or no flag for observe/legacy parents, even after
  config changes); a corrupt stored routing block fails closed. Monitored runs
  forward `--difficulty` to the inner process; board runs deliberately do not
  route in this release. Automatic `enforce` mode is reserved pending the
  calibration gates in `docs/difficulty-model-routing-plan.md`.
- `syncmill doctor --strict` — require every enabled priority provider to be
  present and, where supported, authenticated (Kimi Code exposes no
  non-interactive auth probe, so its executable being found is its readiness
  boundary).
- Korean TUI screen guide (`docs/tui-guide.ko.md`) covering all seven tabs and
  eleven screen states with deterministic SVG captures rendered by
  `scripts/render_tui_guide.py`, and a `/tmp` sandbox walkthrough
  (`docs/tmp-test-guide.ko.md`) for trying SyncMill against a disposable
  repository — both pinned by documentation-contract tests. The English and
  Korean user guides were rewritten around an install-to-first-success flow.
- Opt-in real-terminal test suites: a PTY mount/exit smoke for the TUI
  (`SYNCMILL_TEST_TUI_PTY=1`, wired into CI) and a paid/networked live Codex
  TUI canary (`SYNCMILL_TEST_TUI_LIVE_CODEX=1`, never run in CI).

### Changed

- `doctor` now succeeds when at least one enabled provider is ready;
  per-provider problems (missing binary, failed auth) are reported as stderr
  warnings instead of failing the check. Use `--strict` for the previous
  all-provider enforcement. A lineup with no ready enabled agent — including
  an all-disabled lineup — is now fatal (`no enabled agent is ready`);
  previously an all-disabled lineup printed `Ready.`. An unknown agent id in
  `agent_priority` is also fatal (the registry would reject the whole lineup
  at run time), never a relaxed provider warning.
- TUI force-stop (`K` on Runs/Operations) now opens a confirmation modal bound
  to the exact job id captured at confirmation time — a selection change while
  the modal is open can no longer redirect the SIGKILL.
- Creating a room in the TUI no longer asks for a provider-call confirmation:
  creation only records the prompt and members; starting a round (`x`) remains
  the confirmed action that makes provider calls.
- `init` now lists every created project file (`syncmill.toml`,
  `.syncmill/project.json`, `.syncmill/.gitignore`) and notes that none are
  staged automatically.
- The reconciler's `skipped_live_owner` detail no longer names the recorded
  `apply_owner` as the lock holder — a busy apply lock may belong to an
  unrelated concurrent operation, and the recorded owner's process may already
  be gone. The owner is now reported as a hint.

### Fixed

- TUI job-store lock probes take a shared lock instead of transiently acquiring
  the exclusive one, so concurrent poll ticks no longer report each other as a
  live worker. (A probe still momentarily conflicts with a launch — flock has
  no acquire-free query — and launches deliberately stay fail-fast: any
  wait-and-retry would let a finishing worker through as a duplicate launch.)
- Detached apply workers receive their run/room id after a `--` separator, so
  an id beginning with `-` can no longer be re-parsed as an option.
- A room viewer whose build is interrupted by the room going terminal
  (close/apply/prune sweeping its temp session) now reports
  `room is <state>; nothing to view` instead of the raw tmux error the sweep
  caused; a genuine publication race still surfaces the tmux message.

- Symlink-loop or otherwise unresolvable repository roots in apply/recovery
  paths now raise one stable `ArtifactError` contract across Python 3.12–3.14
  (Python 3.13 changed non-strict `Path.resolve` to return a partially
  unresolved path instead of raising `RuntimeError`).
- The Linux TUI job guardian treats a zombie-only process group as complete
  instead of deadlocking against a parent that has not yet reaped the child.
- `run --repo` no longer reads the invoking directory's `syncmill.toml` for a
  valid Git target: a target without its own config resolves env > defaults at
  the target (previously the caller's project id, state roots, and strategy
  leaked into the target's run), and a target config that fails to load is a
  hard `invalid target project configuration` error instead of a silent
  fallback.

## 0.1.2 — 2026-07-22

Compare & Apply rooms, durable detached apply workers, and the full-TUI
operations slice.

### Added

- Opt-in Compare & Apply rooms with persistent isolated member worktrees,
  detached rounds, immutable candidate manifests, explicit selection, source and
  digest drift checks, and application through the standard run-artifact path.
- Per-member round progress records with private bounded logs, so a detached
  round's members can be observed live without touching orchestration.
- Read-only tmux viewer for comparison-room rounds: `syncmill room attach
  <room-id> [--round N]` (and the TUI Rooms `t` key) open a session with one
  `tail -F` pane per member over the round's private logs. Observation-only;
  a round never auto-attaches. External attach blocks and returns on detach;
  inside tmux it uses a non-blocking `switch-client` (return with Prefix + L).
  Viewers are ownership-scoped by room id, reclaimed on room close/prune, and
  swept when their room is gone or terminal; a missing tmux server never
  affects a running round.
- Project-scoped isolated interactive sessions with verified tmux attachment,
  artifact checkpoints, and explicit workspace cleanup. The original direct
  interactive session commands remain compatible.
- Durable detached apply workers: `syncmill apply <run-id> --detach` and the
  TUI artifact/room applies run through a typed, project-scoped job manager in
  its own process group, surviving the launching process; progress is
  inspectable from `syncmill tui`.
- `syncmill recover [<run-id>]`: reconcile a standalone artifact apply
  interrupted by a crash to completed, not-applied, or explicit
  manual-recovery state. Room applies recover via `room recover`.
- Sessions and Rooms tabs in the full TUI, including explicit confirmations for
  candidate selection, room application, and isolated-session closure.
- `syncmill tui --tab auto|board|runs|reports|teams|status|sessions|rooms`;
  `auto` (the default) lands on the first open surface (Rooms → Board → Runs →
  Status), and an explicit `--tab` is always honored. The global footer is
  replaced by a contextual, availability-gated per-tab action bar.
- The TUI Runs tab splits into Operations (detached job workers) and Artifacts
  (run artifacts: diff, detached apply, discard) sub-tabs; an isolated-session
  checkpoint lands on Runs → Artifacts with its run selected.
- Room candidates gain Diff/Output/Details views (d/o/m) with paged,
  digest-bound diffs; failed candidates open on Output.
- CI coverage for Python 3.12 through 3.14 plus an opt-in POSIX/tmux process job.
- Optional `syncmill[phoenix]` trace streaming via batched OTLP/HTTP. The
  canonical OTLP-JSON file remains intact, streaming is best-effort, and the
  run envelope records Phoenix export and drop counts without trace bodies.
  A requested stream whose optional extra is not installed is reported as
  `unavailable`, distinct from an attempted export that fails.

### Fixed

- Claude transcripts are normalized through one event parser covering the single
  JSON object, top-level JSON array (Claude 2.1.x), and JSONL shapes alike. The
  array shape previously defeated result, `is_error`, session-id, and tool
  telemetry extraction, so an exit-0 in-band rate-limit (429) response was
  reported as a successful run; it now fails with the provider message intact.
  A transient error event the CLI recovered from (a later terminal result with
  `is_error:false`) no longer fails the run — and the success event's own text
  is the output, never an earlier error's prose.
- A zero-exit provider response that *looks like* JSON (opens with `{`/`[`) yet
  decodes to no usable event is now a FAILED "malformed provider transcript"
  for all three agents, where it previously completed with the raw text as
  output — route/compete no longer crown a broken transcript the winner.
  Genuine plain-text output keeps the compatible fallthrough.
- TUI polling preserves interaction state by construction: refreshes
  signature-skip unchanged rows and details, and focus moves only on explicit
  tab switches — a live poll never steals selection, scroll, or input.
- Detached-manager finalization is a one-way latch: the guardian's clean exit
  gates on a manager done-ack, fails closed on cleanup errors, and control
  signal/intent writes are serialized — a dying manager can no longer strand
  its child process group or the per-board lock.
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
- Room and run application now serialize durable state transitions across
  processes, bind candidate source identity to SQLite, recover interrupted
  `applying` rooms explicitly, bound aggregate checkpoint prompts by bytes, and
  continuously reap detached Room workers.
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

### Changed

- The package version is single-sourced from distribution metadata. `pyproject`,
  `syncmill.__version__`, `syncmill --version`, and the Phoenix tracer
  instrumentation version had drifted (0.2.0 / 0.1.1 / 0.2.0); all now report
  the same number, corrected to 0.1.2 for this release.

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
