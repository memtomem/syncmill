# Changelog

All notable changes to SyncMill are documented here.

## Unreleased

### Added

- `room export` can include raw content on request: `--include` names a
  candidate's `patch`, `output` or `gate-output` (full ids) or a round's
  `prompt`. `--preview` writes nothing and shows each item's integrity basis,
  size, sha256, the bytes to be included (`--raw-max-bytes`, 256 KiB per item
  by default) and its first lines, plus a token; `--confirm TOKEN` reads
  everything again and writes only if the token is unchanged. Markdown gets a
  final section with an escaped display rendering; JSON gets a `raw_content`
  key with base64 bytes. Raw content is never redacted and every surface warns
  that it may contain secrets. Without `--include` the export is unchanged.
- Target recalled context by artifact-kind tags in pipeline, decompose and
  council follow-up phases. Initial phases, route, compete and rooms keep
  unfiltered recall. Human-written `shared` notes need a matching kind tag to
  participate in filtered phases; untagged notes, prior `task-result` entries
  and `board-result:*` summaries are excluded there. Unsupported backends fall
  back to unfiltered recall with a warning and degraded status.
- Add a durable millflow control plane: cross-process execution leases and
  fenced writes, foreground or detached POSIX workers, bounded node logs,
  `workflow status --watch`, `workflow logs --follow`, real safe/force
  process-group cancellation, cumulative deadlines/usage, immutable hashed
  attempt bundles, and workflow jobs in the existing TUI Runs tab. Workflow
  planner/worker subprocesses now receive a provider-scoped environment plus
  exact configured allowlist entries instead of the ambient credential set.
  Schema-3 manifests migrate marked legacy call counts exactly; ambiguous
  schema-1/2 records require a new plan and remain inspectable/cancellable.
  Live local owners and legacy claims without liveness evidence are preserved.
- Add native declarative workflows under `syncmill workflow`: read-only
  Claude/Codex planning, strict bounded DAG validation, explicit plan-then-run
  approval, isolated parallel node execution, SQLite recovery state,
  cooperative cancel/resume/prune operations, and one standard final run
  artifact (reviewable always, applyable when the final node changed files). The
  exact lowercase `millflow:` prefix on `syncmill run` is a planning-only alias.
  What a planner produces is a declarative DAG that is validated and then
  dispatched by syncmill, so nothing it writes is ever run as control code; a
  node's own agent still has whatever its provider's tools allow, which
  `workflow_worker_scope` narrows rather than seals.
- Add an opt-in Toolgraph control-plan preflight. SyncMill projects each
  strategy into a body-free, bounded DAG, submits every mapped principal and
  qualified tool surface in one graph-state-bracketed call, verifies exact
  plan digest and evaluation coverage, and persists `control-plan.json` plus
  `control-preflight.json` under the run id. Toolgraph stays advisory; existing
  SyncMill strict policies own the pre-dispatch block decision. The legacy
  per-identity preflight remains the default and unchanged.
- Extend that control-plane contract to native MillFlow DAGs. Planning freezes
  a body-free `control-plan.json` and its execution bounds; every run or resume
  receives a new durable execution id, rechecks the exact frozen bytes before
  any worktree starts, and records preflight, stop reason, and OTLP trace
  evidence in workflow history. Workflow spans use occurrence ids for retries,
  links for fan-in, and local checkpoint-read spans on resume, so stock
  Tracegraph can ingest the causal graph without a schema change.
### Fixed

- JSON printed by `runs show`, `conversations show`, `room show`,
  `room compare --json`, `room export --format json` (stdout and `--output`)
  and `run --dry-run` no longer passes C1 controls (such as U+009B, CSI), bidi
  controls or other non-printable characters from stored strings to the
  terminal. They are written as `\uXXXX` escapes, so the documents decode to
  the same values; printable non-ASCII text such as Korean stays literal.
- Human-readable output no longer passes terminal controls from agent,
  provider or imported text to the terminal: a run's result (`Files changed`,
  the output and the error), `workflow logs`, `workflow show --prompts`
  prompts and its error line, `board show`/`board list` fields, prompt first
  lines (now including bidi controls), and run and provider session ids in
  `runs list`, `conversations list`, prune, discard, close, apply and recovery
  messages, plus the other manifest fields those listings show. Every `Error:`
  message is escaped the same way, in one place, so an id or exception text
  it quotes cannot drive the terminal either. Line breaks and tabs in multi-line bodies are kept; everything
  else non-printable is shown as an escape, so ANSI colour in an agent's
  output now appears as `\x1b[...` text.
- Every line syncmill prints now goes through one escaping writer
  (`syncmill.terminal`), so no list, confirmation, warning or path can carry a
  terminal control to the screen; a test fails on any direct `click.echo`,
  `print` or `sys.stdout/stderr` write elsewhere in the package.
  Uncaught-exception reports, in any thread, and the TUI job worker's argument
  errors are escaped too. syncmill's own log messages, tracebacks included,
  are printed through their own escaping handler (#202); other libraries' log
  output (memtomem, mcp, uvicorn, asyncio) is not escaped. Under `serve`,
  syncmill's log lines are now plain lines with plain tracebacks instead of
  passing through the MCP server's Rich handler. A root handler configured
  before the CLI starts no longer receives syncmill's records, so its level and
  filters no longer apply to them, and a logging level lowered without adding
  a handler now shows syncmill's INFO messages. `diff` and
  `room diff` still print the stored patch unchanged (#199).
- The TUI Artifacts pane no longer rereads and reparses every run manifest on
  each poll tick. `ArtifactStore.list_detailed` keeps parsed manifests while
  their file signature (device, inode, size, mtime, ctime) is unchanged, so an
  unchanged tick costs one stat per run and no content read for stores up to
  the 4,096-manifest cache bound (above it the uncached remainder is reread
  each tick, never the whole store); additions,
  deletions, in-place state changes and atomic replacements by another
  process are picked up on the next tick, and the cache is bounded. The
  listing now runs in a thread worker — single flight, with refreshes that
  arrive mid-scan coalesced into one rerun and stale or pre-rebind results
  dropped — so a large store no longer blocks input. The detail pane
  still re-reads the one selected run each tick (#168).
- The MCP `shared_read` tool answers `shared memory is currently unavailable`
  when the memory backend fails on that call, instead of reporting the chunk as
  not found. `SyncmillMemory.get_shared` now returns a per-call `SharedRead`
  status (`found` / `not_found` / `unavailable`); a missing id, a malformed id
  and a private chunk stay one indistinguishable `not found` (#169).
- `runs list`, `conversations list` and the TUI Artifacts pane no longer show a
  run store whose manifests are corrupt or refused as a healthy empty store.
  `ArtifactStore.list_detailed` reports each omitted entry with a fixed reason
  category; the CLI names them in a stderr notice and the TUI counts them as
  `DEGRADED: N unreadable`, listing names and reasons when nothing is
  readable. Entry names are escaped for display, nothing is repaired or
  deleted, and a runs root replaced mid-enumeration still raises (#170).
- The MCP `board_list` tool no longer materializes the whole board to answer
  a bounded request. `Board.list_recent` sorts newest-first and applies the
  limit in SQL (the same order and same-timestamp tie-break as the previous
  reversed FIFO slice), backed by a new `created_at` index that existing
  boards gain on open; filters still apply before the limit and the clamp,
  invalid-state and database-error responses are unchanged (#167).
- `workflow prune` no longer deletes a workflow that another process is
  resuming. It chose its candidates outside any transaction, then removed each
  one's directory and row unconditionally, so a `failed` workflow resumed in
  between lost its durable state while running. Each candidate is now decided
  again under a per-workflow file lock that every run holds for its whole span
  and that workflow creation holds exclusively. Prune commits a `pruning`
  tombstone only if the row is unchanged since selection, removes the
  directory outside any transaction, and deletes the row only from that
  tombstone. A prune interrupted part-way leaves the workflow `pruning`, where
  nothing can resume or cancel it and the next prune finishes it (#142).
- Workflow node spans sit where each execution ran. A span was anchored at the
  moment its result was collected, after the slowest node in its batch and
  every earlier node's patch capture, so a node that finished in one second
  beside a sixty-second sibling appeared at second 59. The span length was
  already the node's measured duration; its start is now the instant the
  agent started that measurement, recorded per invocation (#141).
- A workflow invocation reports its own outcome. One blocked by strict
  Toolgraph preflight wrote its reason onto the workflow row and then returned
  that row, so a concurrent invocation that had meanwhile completed the
  workflow got the blocked one's error and the blocked one exited successfully.
  The reason now lives on the blocked execution (shown by `workflow status` as
  `execution_error`), and the CLI decides the exit status from the execution
  the call made rather than the workflow's current state. Workflow writes also
  return the row they committed instead of re-reading it after the write lock
  is released, where another invocation's result could already be visible
  (#140).
- Workflow finalization publishes exactly the final-node bytes it verified. It
  checked the bundle's recorded digests, then read the patch and record again
  to publish them and to compute the artifact's digests. A patch replaced
  between those reads would have been published under digests that attested to
  the replacement. A completed final node whose patch no longer matches its
  record still fails into recovery without publishing anything; that case is
  now covered by a regression test (#144).
- Workflow finalization publishes the final node's patch byte for byte. It
  read the captured patch as text, which translated CRLF line endings to LF, so
  a final node that wrote CRLF lines produced an artifact that differed from
  what it captured and could fail to apply (#143).
- Toolgraph preflight decodes the binary's output with `errors="replace"`, as
  agent launches already do. A non-UTF-8 byte in its stderr raised a
  `UnicodeDecodeError` that the last-resort handler reported as an internal
  error, so strict mode blocked the run as `INTERNAL_ERROR` even under
  `unavailable_policy = "warn"`; the failure is now the exit it was, with its
  stderr in the warning (#138).
- Allocate the recalled context's 3,800-byte UTF-8 budget across rows instead
  of cutting each at 300 characters. Preserve CJK character boundaries, mark
  shortened rows with `...`, and drop the lowest-ranked rows only if their
  minimum representation cannot fit. Coverage takes priority over excerpt
  depth; a wide window does not guarantee complete or current-run results.
- Close workflow SQLite connections after every operation, including rollback
  and setup failures. Transaction context managers alone left connections open
  until garbage collection, allowing repeated workflow operations to exhaust
  the file-descriptor limit.
- Artifact writes and TUI job-state writes no longer depend on `os.fchmod`
  existing. The call is Unix-only, and the `AttributeError` it raises where it
  is absent falls outside the `OSError` boundaries those modules convert, so
  both sites now use the same guarded form as the rest of the codebase. The two
  sites differ in how much this buys. Every Click command refuses anything but
  macOS and Linux before it runs, and the artifact writer refuses again on its
  own first line, so that guard is there for a runtime the policy permits but
  that lacks the call, not for Windows. The TUI job writer is reachable without
  passing either refusal: `python -m syncmill.tui_jobs worker` goes straight to
  the worker, and its failure path records `launch_failed` through that same
  atomic write. A real permission failure on POSIX still surfaces.
- A migration artifact from before destination binding (`schema_version` 1) is
  now refused when it is read rather than by each consumer, so a corrupt one
  explains that it is a legacy artifact instead of reporting an incidental
  digest mismatch. The recovery guidance is unchanged.
## 0.2.0 — 2026-09-12

### Added

- SyncMill runs on memtomem 0.4 and the MCP 2.0 SDK. The 2.0 SDK removes
  `mcp.server.fastmcp`, so the read-only gateway moved to
  `mcp.server.mcpserver.MCPServer`, and `--host`/`--port` became run-time
  transport arguments — the SDK derives its DNS-rebinding policy from the host
  actually passed at run time, so a mutated setting can no longer leave a stale
  loopback policy behind. The two pins move together, because memtomem 0.4
  requires the 2.0 SDK. The adapter's write path also adopts 0.4's locking
  contract: the append and the re-index that follows it share one lock span,
  so the store's own watcher can no longer index a freshly appended file first
  and strand an agent-private chunk in the default namespace, where a
  non-private search would find it.
- Rooms keep each member's provider conversation alive across rounds, so
  provider-side prompt caches stay warm instead of being rebuilt every round.
  Session ids are keyed by member and written to the room row in the same
  transaction that updates the member's workspace, right after the fan-out and
  before any candidate is processed — so they survive workspace replacement by
  a fresh or branch-from lineage swap, recovery cleanup, and a crash, any of
  which deletes the workspace row that would otherwise have been their only
  home. A resuming member is no longer sent its own prior output or
  rounds it has already seen — only what its peers added. `--fresh-session`
  drops the stored ids for one round, and a deliberately fresh member stays
  cold. Cache hit-rate telemetry is recorded per round.
- Prompts can come from a file or standard input. `--prompt-file PATH` (or `-`)
  works on `run`, `room create`, and `room round`, so a multi-line prompt needs
  no shell quoting. `room round` now echoes the prompt it actually runs and
  warns when an omitted prompt falls back to the room's creation prompt, which
  used to happen silently; `room rounds <id>` lists that per-round history.
- `room round --fork` forks each member's provider conversation instead of
  continuing it in place. Provider session forking is a Claude feature, so a
  room with non-Claude members refuses the flag before any work starts.
- Room rounds read and write shared memory like `run` and `board` already did.
  Each round prepends one bounded, best-effort `search_context` result as a
  separable prefix, and an applied candidate is shared back tagged
  `room-result`. This changes what every room round sees, not just how its
  prompt is typed.
- `recover --list` and `recover --discard` surface and reclaim retained
  recovery workspaces, and `board compact --older-than` trims the append-only
  transitions audit. Each is the only way to reclaim its store, and both are
  deliberately manual, since both destroy work. `runs.keep_last` bounds the
  automatic artifact sweep and `[workspace] stale_seconds` bounds the
  orphaned-workspace one.
- The TUI gains a round modal for composing a room round in place, and records
  per-round token and cache telemetry alongside the round history.

- SyncMill is licensed under the Apache License 2.0. The `LICENSE` file, the
  license expression, the author and project URLs, and an alpha development
  classifier now ship in the package metadata, so an installed wheel states its
  own license rather than leaving consumers to guess.

- `CONTRIBUTING.md` and a Contributor License Agreement. The CLA workflow
  comments on a first-time contributor's pull request and publishes a `cla`
  commit status on its head, so replying with the signature turns the pull
  request green — a comment alone cannot, because the signing reply's workflow
  run belongs to the default branch and never touches the run that already
  failed.

- A tag-driven publish workflow. `v*` releases to PyPI and `test-v*` rehearses
  the same distribution version on TestPyPI, both through trusted publishing
  with no stored tokens. `scripts/release_preflight.py` fails closed before
  anything is uploaded: the tag, `pyproject.toml`, `uv.lock` and a dated
  changelog heading must name one version, the tag commit must be on `main`,
  that commit's own main-push CI run must have succeeded, and the built wheel
  and sdist that will actually be uploaded must install into a clean
  environment and import. After publishing, the workflow re-reads the index
  and fails when the bytes it serves differ from the ones just built — uploads
  tolerate a pre-existing filename, so a corrected build re-tagged as a new
  rehearsal would otherwise report a green release the index never received.
  The preflight proves the license file is actually inside both archives at
  the path their metadata declares, rather than merely named by it or present
  somewhere else in the tree and at the location its metadata version
  requires, and the CLA check enumerates contributors from immutable commit
  SHAs, paginated and checked against the reported total, so neither a force-
  push sequence nor a pull request too large for one page can mark a head
  green whose commits were never examined. A commit GitHub cannot attribute to
  an account blocks with an explanation rather than being skipped, since
  skipping it means no signature is ever required for whoever wrote it; runs
  for one pull request are serialized and the signature store is re-read
  immediately before the verdict, so a signature landing mid-check is seen
  rather than overwritten with a stale failure.

### Changed

- Room prompts are validated before a round starts, so some input a script
  could previously submit is now refused. A prompt is bounded by what the
  platform can actually launch a process with, and one carrying a NUL byte or
  terminal control sequences is rejected at the coordinator — every entry
  point, CLI, TUI and detached worker alike, so the failure lands before a
  lease or workspace exists rather than freezing the room afterwards.
- Execution is supported on macOS/Linux. Windows commands fail before side effects;
  root help/version remain available. All submodule repositories are rejected
  before snapshot creation, including clean and initialized submodules.

- Provider stdout/stderr collection is bounded in sync, async, and monitored calls.
  The combined limit defaults to 32 MiB per call (`output_max_bytes` per agent);
  overflow terminates the process group and records failure.

- The orphaned-workspace sweep's age backstop is configurable as
  `[workspace] stale_seconds` (`SYNCMILL_WORKSPACE__STALE_SECONDS`), defaulting
  to the previous week with a one-hour floor. It is only the backstop for a
  reused pid — an ordinary killed run is still collected as soon as its owner is
  gone — and it overrides a live pid, which is why it is bounded rather than
  free-form.

- Repository snapshots stop re-hashing the whole tree. The private index is now
  seeded from the source repository's live index, so `git add -A` re-hashes only
  genuinely dirty paths rather than every tracked file on every run — tens of
  seconds per run on a large repository, previously paid again by every board
  loop iteration and every verified apply. The resulting tree is identical
  either way, and any seeding failure falls back to the old path.
- A provider transcript is parsed once per result instead of five times. The
  accessors that classify output, read telemetry, usage, and the session id each
  did a full strip-and-parse of the whole stream, on the shared event loop — so
  while one verbose agent classified its output, every sibling stopped draining
  its pipes.
- Judge selection no longer re-diffs each candidate in full. It now produces
  only what the prompt keeps — a stat summary plus a byte-bounded diff head read
  off the pipe — rather than staging the whole tree and materializing a complete
  binary patch per candidate to slice 12000 characters out of it. Selection
  semantics are unchanged, and the winner's single applyable patch is still
  captured exactly once.
- Monitor logs are bounded. Each agent's pane log now rotates through two 256 KiB
  segments, the same scheme rooms already used; the pane follows by name, so the
  live view is unaffected.
- The three stores under `~/.syncmill` that could grow without limit can now
  be reclaimed, two of them by hand. Terminal run artifacts are pruned
  automatically to `runs.keep_last` (default 50), best-effort and skipping a
  busy lock; a run that is pending, applying, or holding a recovery workspace
  is never pruned, and neither is the run just saved. The other two are manual
  by design, because both destroy work: `recover --list` and `recover --discard`
  surface and reclaim retained recovery workspaces, which were previously
  immortal by construction — the recovery marker exempts them from every sweep,
  `[workspace] stale_seconds` included, and nothing listed them — and
  `board compact --older-than` trims the append-only transitions audit.
- A per-agent `timeout_seconds` of `0` is now refused at load rather than
  making every run of that agent time out instantly with no explanation. A
  configuration that set it to zero will stop loading.

### Fixed

- Every subprocess the run path waits on now dies with its own process group,
  and Ctrl-C reaches it. Agents, quality gates, isolated apply verification,
  toolgraph preflight, and all 23 git calls in the worktree layer route through
  one owned-group runner with an explicit bound, so a timeout reaches the
  grandchildren — an MCP server holding the pipes, a test runner's workers, a
  prompting git filter — instead of leaving orphans behind a killed direct
  child. Giving the agent its own session also detached it from the terminal's
  foreground group, so for a while an interrupt unwound the run while the agent
  kept going and kept editing its workspace; the interrupt is now delivered to
  the group it created, and one escaping `run_async` no longer leaves the
  fan-out pending on the shared loop for the next call to resume. Agents launch
  with no standard input, so an interactively prompting CLI can never read the
  user's terminal, and a wedged git can no longer hang a run, an apply, or the
  cleanup that follows one. Advisory memory calls on the run path get a
  separate, cooperative 30-second bound: an unreachable backend degrades the
  run rather than stalling it, which is the same best-effort contract memory
  has always had, now with a deadline attached.
- Codex runs work against current `codex-cli` again. The fresh-run shape passed
  `--full-auto`, which codex-cli 0.147.0 rejects outright, so every Codex run
  failed on that version; both the fresh and the resume shapes now pass
  `sandbox_mode` and `approval_policy` as `-c` overrides instead.
- A `board run --loop` worker keeps dispatching. The lease heartbeat retries a
  transient renewal failure on a bounded schedule rather than dying on the
  first sqlite error — one busy-timeout burst used to expire a healthy run's
  lease, let stale recovery reclaim it, and have a second worker redo work that
  was then fence-dropped. Scratch-namespace collection moved off the dispatch
  path, where a contended purge stalled every iteration for its whole budget,
  and a second worker skips a sweep another is already running. A namespace
  that keeps failing to purge backs off and is quarantined with a warning
  instead of blocking every healthy one behind it.
- The destructive verbs act on the project they name. `recover --list` and
  `recover --discard` read the caller's own cwd config, so run from elsewhere
  they could list and delete another project's workspaces; they now resolve the
  target project like every other project command. `board compact --older-than`
  clamped its argument upward from zero, so `-1`, `nan`, or an underflowing
  negative deleted the entire transitions audit; the flag now refuses a
  negative outright and the store checks the sign bit. The automatic prune
  after a save could also delete the artifact just saved, because retention
  ranks by the timestamp stamped when a run begins.
- A refused `syncmill init` leaves the working tree untouched. A refusal part
  way through — a newer `config_version`, an unwritable marker directory, a
  directory sitting where the marker goes, a failed ignore-file append — could
  previously leave `syncmill.toml` already written, so the retry then claimed
  the project was already initialized.
- Rooms use the project's own memory. The room CLI, the detached worker, and
  the TUI read and wrote the ambient `~/.memtomem` store for an initialized
  project left at default memory settings; the project binding now happens in
  the coordinator's constructor, on every path. A lost room-result share is
  also surfaced — as a durable degraded room event, which is the only trail a
  detached apply job leaves, and as a warning on the foreground CLI.
- A recovered memory backend stops reporting itself as degraded. The flag
  latched, so a long-lived `board run --loop` or `syncmill tui` kept the notice
  on screen for the rest of its life after one transient failure; a fully
  successful operation now clears it, as the circuit breaker already did.
- The TUI cannot mint a duplicate run artifact from a second checkpoint
  keypress, and refuses to close a session while its checkpoint is running —
  that would block on the same store lock and freeze the interface.
- Long-lived interfaces stop paying for full scans and stray clones. The two
  audit queries behind `board stats` ran over the never-pruned transitions
  table on every two-second poll and now have indexes; a job's log tail no
  longer looks frozen until the job exits; and the isolated verification clone
  lands under the sweepable workspace root, so a kill mid-verify no longer
  strands it in the temporary directory.
- `room candidates` and `room diff` piped into a pager that exits early now
  exit quietly instead of printing a broken-pipe error.

- Recalled shared memory can no longer strand a run. A NUL byte in the context a
  strategy prepends reached an argv element, where `subprocess` raises a
  `ValueError` no launch handler caught — the run froze as `recovery_required`,
  with a retained workspace, before any agent started. Enrichment now normalizes,
  bounds, and scrubs the recalled block only; the task prompt stays verbatim, and
  both execution paths refuse a NUL prompt outright.

- A live run's workspace survives a long run. The sweep's age backstop overrides a
  live owner pid, and only `acquire` refreshed the marker — but the concurrent
  strategies acquire every worktree up front, so a run longer than
  `[workspace] stale_seconds` froze its own timestamp and another process's
  startup sweep deleted it mid-flight. A pool now refreshes its marker
  periodically for as long as it lives, tolerating a transient write failure, and
  stops when it is collected. The refresh is best-effort: a workspace whose writes
  keep failing for longer than `stale_seconds`, or whose process is suspended that
  long, can still age out.

- The automatic run-artifact retention sweep no longer waits on the apply lock
  (an explicit `runs prune` still blocks). It runs on the
  tail of a finished run, which still holds its worktrees and memory session,
  while `apply --verify` holds the same lock across a clone and every gate
  command. A busy lock is now skipped and repeated by the next run.

- Opening a settled room store is a read, not a write. Its session backfill ran
  inside `BEGIN IMMEDIATE` unconditionally, so every open took the write lock and
  could fail with "database is locked" behind a running round — breaking
  `room list`, the TUI's poll, and a second worker. A migrated, already-merged
  database now opens read-only; schema migration and a genuinely pending backfill
  still write, and that backfill now merges every member of a room in one pass.
  Room store failures now surface as
  `RoomError` rather than a raw traceback, a rollback no longer replaces the error
  that caused it, and a failed recovery freeze no longer discards the round
  failure it was recording.

- `doctor` and `run --dry-run` report the working tree they name. Their `git
  status` was the one git call without an isolated environment, so an ambient
  `GIT_DIR` pointed it at another repository, and a git failure was reported as a
  clean tree. `run --repo` also expands `~` again.

- A decompose synthesis can ask for every subtask it published. The shared
  context window was a fixed 5 while `decompose.max_subtasks` defaults to 8, so a
  default run published more subtask results than the read could return and the
  integrating agent could not see three of its own pieces. `search_context` now
  takes a caller-supplied `top_k` (clamped to 50), and decompose asks for the
  count it just published or the default, whichever is larger — asking for the
  raw count would have narrowed the window for a mostly-failed breakdown. This
  lifts the fixed-five ceiling only. Two ceilings remain by construction — the
  clamp, and the fact that one published result can chunk into several rows —
  and the read is still untagged, contested by cross-run rows, and truncated
  when published, when retrieved, and when composed. See #125, #126, #127.

- Toolgraph `agent_map` identities are shape-checked like `candidate_tools`. They
  occupy the argv token immediately before them, so an option-shaped identity
  redirected toolgraph's own flags.

- A room store failure reaches the CLI as a clean message rather than an empty
  traceback. Wrapping the failure into `RoomError` only achieves that if
  something translates it, and every `room *` command builds its coordinator
  before its own error handling.

- Smaller correctness fixes: an empty scratch sweep no longer clears a prior
  memory degrade; `share_board_result` refuses on a board-scoped view like every
  other shared-namespace method; a crashed run now attempts its memory-session
  close, bounded and best-effort like every other step in that finalizer,
  instead of skipping it; the artifact and breaker state writers no longer leak
  a descriptor when the write fails; and a sync timeout after pipe EOF keeps the
  partial output it documents.

- GC distinguishes failed purge attempts from candidates deferred by its deadline.
  Deferred candidates remain immediately eligible instead of accumulating false
  failures and being quarantined behind a slow namespace.

- Board scratch GC owns one deadline from backend readiness through deletion;
  timeout preserves completed receipts and failure backoff instead of losing
  both to an overlapping supervisor timeout.

- A TUI memory-status probe completing during screen teardown no longer fails
  when its status widgets have already been removed.

- Board scratch GC recognizes exact legacy and memtomem-native day files across
  index roots. Namespace and real source locks protect files-before-rows purge
  against SyncMill writers and in-flight indexers. Incomplete deletion remains
  retryable; source-lock, descriptor-headroom and cooperative time budgets bound
  maintenance. Real-backend process races and crash recovery run in dedicated CI.

- Preserve durable session/room workspaces across creator exit and generic cleanup,
  including legacy run sentinels. Mandatory patch capture uses a private index;
  capture failures preserve recovery workspaces instead of becoming `no_changes`.

- Apply verification now shares quality-gate environment isolation and error
  handling. Gate output is tailed during capture to bound memory use.

- `project_id` no longer trusts a `.syncmill/project.json` the repository
  shipped. The marker is machine-local identity written by `syncmill init`,
  but it lives inside the working tree — so cloning a repo that committed one
  bound this machine's state roots to an id chosen by whoever wrote it,
  including ids with path separators, which walked the artifact store right
  out of the runs root. The marker is now honored only when its id is a single
  conservative path token — Windows device names like `CON` and `COM1`, and a
  trailing period, included, since the id becomes a path component and Win32
  strips or rejects those — AND the file is not
  supplied by Git, meaning named by neither the index nor `HEAD` (staging a
  shipped marker's removal with `git rm --cached` leaves the repository's file
  in the worktree, which an index-only question would call machine-local — and
  for a submodule the ancestor has to be examined on its own, since Git does
  not traverse a gitlink to the file inside it); anything else
  degrades to the path-derived id with a warning, never an error. The same
  gates apply when `init` preserves an existing id, so a re-init cannot
  launder a shipped id into `syncmill.toml`, where the configured value is the
  operator's own trusted input. `init` also adds the marker to
  `.syncmill/.gitignore` and `.git/info/exclude`, so a fresh project cannot
  accidentally commit the file that becoming-tracked would stop it trusting.
  The trackedness question is asked case-insensitively (a repository can commit
  `.syncmill/Project.json`, which the read opens through the lowercase path on
  a macOS or Windows filesystem), with a case-variant hit confirmed against the
  filesystem before it counts — on a case-sensitive one it names a different
  file, and folding it in would discard a legitimate local id. The marker is
  refused outright when any component of
  the marker path is a symlink, since a lexical query and the read would then
  be looking at different files. A tracked gitlink ancestor counts as tracked
  too: a populated `.syncmill` submodule is one index entry at `.syncmill`, so
  nothing names the marker's own path while its content still arrives with the
  clone. A tracked *sibling* does not count — `init` writes a
  `.syncmill/.gitignore` the project is meant to commit — and the listing is
  decoded with `surrogateescape`, so a tracked sibling whose name is not valid
  UTF-8 cannot raise out of a function required to degrade.

- A marker holding valid JSON that is not an object (`[]`, `null`, a bare
  string) no longer crashes every command. Its whole content is
  repository-controllable, and `data.get` on a non-object raises
  `AttributeError` — neither `OSError` nor `ValueError` — so a checkout could
  turn the promised fallback into a traceback.

- `init` refuses to write where the path is not syncmill's to write through —
  both the marker and the `.syncmill/.gitignore` beside it, since a checkout
  can leave one ordinary and track the other as a symlink: a symlinked
  component would land the write and the `chmod` on a file the checkout chose,
  possibly outside the repository, and a `.syncmill`
  submodule would dirty an unrelated repository's working tree. Reading such a
  path only distrusts its content; writing acts on whatever it leads to, so
  this one raises rather than degrading. An ordinary *tracked* marker is not
  refused — rewriting it with a freshly minted id is the upgrade path. An
  unreadable index is refused with a confirmed submodule (the read falls back
  to the path hash, but a write has no fallback), and the component check
  covers Windows directory junctions, which `is_symlink` reports as ordinary.

- `init` now requires the ignore rules to be both physically present in
  `.syncmill/.gitignore` and actually in effect, where before it only looked
  for its own line. Neither test alone is enough: a rule can be present and
  then negated (`!project.json`, or a blanket `!*`), which the membership test
  read as protection while Git left the path exposed; and a path ignored only
  by `.git/info/exclude`, `core.excludesFile`, or the user's global excludes —
  all machine-local — would have committed an empty `.syncmill/.gitignore` and
  left the next clone exposed. Appending satisfies both, since within one file
  the last matching pattern wins.

- `isolated_git_env` now also strips the Git pathspec-semantics variables
  (`GIT_LITERAL_PATHSPECS`, `GIT_GLOB_PATHSPECS`, `GIT_NOGLOB_PATHSPECS`,
  `GIT_ICASE_PATHSPECS`). They do not redirect *which* repository a command
  reads, they redefine what a written pathspec *means* — and syncmill asks git
  two security-relevant pathspec questions, whether a state root is Git-ignored
  and whether the identity marker is tracked, both of which read as a benign
  "no" when an ambient value reinterprets the pathspec. The isolated-session
  launch environment, which enumerates the Git families explicitly rather than
  going through `is_git_routing_var`, strips them too.

- Review-backlog cluster C, part 1 — the rooms/TUI/monitor/telemetry sharp
  edges from the 2026-08 review backlog:
  - `room prune` checks a room's eligibility *before* quarantining its
    candidate artifacts, so considering (and rejecting) a live room no longer
    makes its artifacts vanish for the length of the transaction; and one
    malformed `updated_at` row no longer aborts the whole prune (the row is
    kept unless the count bound claims it).
  - A detached room worker's prompt never reaches argv, where `ps` could read
    it. It travels as an anonymous inherited descriptor: the temporary file is
    unlinked immediately after `mkstemp`, before any plaintext is written, and
    the worker is handed `--prompt-fd` through `pass_fds`, so no path exists
    to be stranded by a crash. The worker argv
    now puts every option before a `--` separator with the positional room id
    last, matching the documented `build_job_argv` contract. `run_round`'s
    crash path also refreshes its workspace listing after a cleanup so it can
    never name a deleted run root as the recovery path.
  - The TUI checkpoint action runs in a thread worker (it takes a blocking
    flock and shells out for a diff — on a large worktree it froze the UI for
    the duration), mirroring the artifact-discard worker. The memory-status
    probe pins its `syncmill status` child to the configured toml's directory
    instead of probing whatever project the ambient cwd holds. The candidate
    Details/Output renderers tolerate explicit `null` manifest fields instead
    of blanking the pane through the poll's catch set, and
    `room candidates`/`room diff` render a corrupt manifest as a clean error
    instead of a traceback.
  - The dashboard's post-write config reload survives a mid-edit malformed
    toml (it caught only `ValidationError` where the TUI catches
    OSError/ValueError too — a decode error killed the app).
  - `monitor.write_result` uses a unique temp file (mkstemp + fsync) instead
    of a fixed `.tmp` sibling two concurrent writers could interleave into,
    and `FileSink` is a context manager so standalone users can't leak its
    per-agent fds on an aborted run. The per-session process-lock map evicts
    entries at zero holders instead of growing for the life of the TUI.
  - Hook telemetry sequence numbers come from a `{spool}.seq` sidecar instead
    of a record count, so a torn spool line can no longer make later records
    reuse a live sequence number (and each event no longer rescans the whole
    spool); a torn line now also marks the telemetry incomplete.
  - The job manager's control socket is bound under a 0o077 umask (no
    pre-chmod window), and control requests verify the token first with one
    generic error for either failure — checking the job id first leaked a
    pre-auth existence oracle.

- Review-backlog cluster C, part 2 — the memory/agents/breaker sharp edges
  from the 2026-08 review backlog:
  - The circuit breaker's `degraded` flag is no longer a one-way latch: it
    reflects the latest persistence outcome, so a transient state-file failure
    at construction can't leave a long-lived process (`board run --loop`)
    permissive forever once later I/O succeeds. `allows()` also does a
    best-effort mtime-guarded reload, picking up other processes' recorded
    failures instead of deciding from the state loaded at construction
    indefinitely.
  - `close_event_loop()` shuts down async generators before closing the
    shared loop (closing without draining left backend resources finalized
    against a dead loop), and the loop's create/close is guarded by a lock so
    two racing threads can't each mint a loop and leak one.
  - A doubly-scoped memory view keeps the true global `shared` for its
    union-read instead of capturing the parent's board-run scratch, and the
    gateway's `search_shared`/`get_shared`/`shared_stats` readers refuse
    (report unavailable) on a scoped view — shared-only by construction, so a
    scoped view handed to the gateway can never serve scratch as shared.
  - The LangGraph adapter's paging loops can no longer spin or hang forever:
    `delete_namespace` gets the mem0 adapter's no-progress guard, and
    `count_namespace` caps iteration against a store that ignores `offset`
    (the page size is now one shared constant).
  - `aexecute_all` routes through `_gather_or_drain` like its siblings, so a
    raising agent cancels and drains the rest instead of leaving them running
    on the reused loop with un-reaped children.
  - `search_multi` rejects a comma-bearing namespace (the comma is the union
    separator; an unvalidated agent id could silently widen the search), and
    the memtomem adapter's `status()` reads its own overrides instead of a
    memtomem private whose rename would misreport version skew as a
    connection failure.

- Review-backlog cluster C, part 3 — the preflight/board/supervisor sharp
  edges plus the drift/cosmetics batch from the 2026-08 review backlog:
  - Strict preflight's persisted evidence can no longer contradict its
    outcome: per-eval artifacts are written before the decision is computed
    (a failed write becomes a blocking reason the on-disk manifest carries),
    the manifest is written last, and a failed manifest write removes the
    partial evidence pile instead of leaving allow-reading producer docs
    behind for a blocked run.
  - `candidate_tools` are shape-validated in every mode, not just strict —
    advisory mode splices them verbatim into the toolgraph argv, so a token
    like `--out` was re-parsed as an option (argv-position confusion).
  - The LLM judge's invocation is now visible in the trace (a `judge:{agent}`
    span — the +1 agent call the RunPlan bound promises) and runs with
    provider sessions disabled, so it never mints a conversation for its
    throwaway worktree — or, on a continuation, appends its ranking prompt to
    the user's seeded conversation history.
  - `applied_team` survives the detached-worker config snapshot: the worker
    re-applies the snapshot's own declared team via `apply_team` (forgery
    still scrubbed), so detached applies and room workers no longer record
    `team: null` in their envelopes.
  - `Board.next_runnable` resolves dependencies from one preloaded done-set
    query instead of O(items × deps) SELECTs under the held write lock, and a
    re-import under a new `--goal` now says "N kept their original goal"
    (goal is first-import-only by design).
  - Drift/cosmetics: `build_run_plan` rejects an unknown strategy instead of
    attributing decompose's topology to it, and reports `selection: n/a` for
    strategies with no select phase; an unknown selection mode raises instead
    of silently degrading to `verified`; `render_banner`'s dead
    `config_mode` branch is gone; a failed `git worktree add` no longer
    reuses its branch/path on retry, `git rev-parse --show-toplevel` failures
    raise instead of resolving to the process cwd, and `cleanup_worktree`'s
    local `shutil` re-import is gone; pipeline's final-stage span uses
    explicit bindings instead of loop-leaked variables; crash and
    preflight-block results carry `run_id` like the success path; the
    gateway's board guard returns a narrowed reference type checkers can see;
    and `teamfile.toml_path()` absolutizes against the cwd at call time,
    making the dashboard/TUI "anchored at launch, immune to cwd drift"
    comments true. It deliberately stops short of resolving symlinks: a
    `syncmill.toml` linked to a shared config elsewhere must keep the link's
    own directory as the project anchor.

- `syncmill init --force` no longer discards your configuration. It wrote the
  template over whatever was there, preserving only `project.id` — so a re-init
  silently dropped gate commands, per-agent settings, the chosen strategy, model
  routing, and team profiles. Forcing is a repair verb: it now fills in the keys
  a project needs and leaves every other key and comment alone. A key it needs
  as a table but that holds something else is refused rather than replaced, a
  `config_version` that is non-positive, not a plain integer, or newer than this
  syncmill writes is refused rather than relabelled (the config model coerces
  `3.0` and
  `"3"` to 3, so a type check that accepted them would have relabelled a schema
  it cannot read), and an unparseable config is moved aside to a `.bak` name that
  does not already exist rather than dropped. A config that cannot be *read* is
  now an error, not a licence to replace it.

- Writing `syncmill.toml` preserves the file's mode. `team save`, `team use`,
  and the dashboard TEAM pane went through a temp file created at 0600, and
  `os.replace` carried that onto the destination — silently narrowing a
  checked-in 0644 project config, which shows up as a spurious mode change in
  git and breaks group-readable checkouts. A file that does not exist yet gets
  what a plain `open()` would give it, from the kernel applying the umask rather
  than syncmill reading it (reading it means setting it to 0 process-wide, and
  the dashboard is threaded). The temp itself is created at the destination's
  mode rather than widened and narrowed afterwards, so a private config is never
  briefly world-readable while it is being written. A destination that cannot be
  inspected is refused rather than treated as absent, which would have
  republished a private config at the umask default. The write stays atomic (and
  the config write `init` performs becomes atomic — it wrote the file directly;
  the marker, ignore file, and Git exclude remain separate steps) and is now
  best-effort
  durable: the mode is set before the fsync that covers it, and the parent
  directory is synced after the rename where the platform allows it. A temp is
  cleaned up on every exit path, not only on `OSError`. `init --force` also
  reserves its `.bak` name atomically, so two concurrent repairs cannot both
  choose it and have the second overwrite the first's backup.

- Migration artifacts are private. `~/.syncmill/migrations` itself was created
  at the default umask — world-readable, while every directory under it holds
  verbatim memory content (`mkdir`'s `mode` applies to the leaf only); the root
  is now tightened on the read paths too, so an installation that only ever
  applies or verifies does not keep the old mode forever, and a root that cannot
  be made private is refused rather than used under a guarantee syncmill is not
  keeping. State writes use a
  unique temp per write instead of one fixed `.tmp` sibling two writers shared,
  and `apply`/`verify`/`rollback` now hold a per-migration lock across the whole
  load-operate-write sequence: each rewrites the entire state document, so an
  overlap let the slower one publish a `targets` map predating the other's work,
  orphaning imported rows from any later retry or rollback. A second command on
  the same migration reports that one is already running rather than waiting —
  orchestration bridges async through one shared event loop, where a blocking
  wait taken on that loop's thread is a deadlock rather than a wait. The lock
  file's own mode survives a restrictive umask, which otherwise let the first
  acquisition leave it unreopenable and lock the migration out permanently. The
  migrations directory is now documented as needing a local filesystem, since
  `flock` over a network mount can let two hosts both import and leave one set of
  rows untracked. `plan` and `apply` close every backend adapter they opened even
  when construction or another close fails, rather than leaving an embedded store
  holding its handle against the retry the failure invites.

- A steady-state board open — an already-WAL, current-schema board with no
  pending ledger backfill — no longer takes the SQLite write lock. (A one-time
  migration still writes: converting a DELETE-mode db to WAL, or adding a column
  to a pre-existing schema.) The scratch-ledger backfills ran on every open,
  including boards with nothing to backfill, and an INSERT takes that lock
  whether or not its SELECT matches a row — so every connection was a writer,
  including the per-run
  lease heartbeat's. Behind a concurrent claim's `BEGIN IMMEDIATE` the heartbeat's
  open waited out `busy_timeout` and could then raise, killing renewal and letting
  a live run's lease expire, after which stale recovery reclaimed the item and
  dispatched the same work a second time. Each backfill is now preceded by a read
  that decides whether the write is needed, so a steady-state open is read-only.

- A board run that loses its lease now says so. The heartbeat returned silently
  when the claim was gone and the fenced write was dropped, so the run completed,
  exited 0, and left no sign its work had been discarded; the reason is now
  surfaced as a yellow notice, per item under `--loop`. The notice tracks whether
  the write actually landed, not whether renewal had trouble — a renewal that
  merely timed out behind a writer can still end with the result recorded, and
  saying otherwise would contradict the board.

- A crashed board item now records why it was blocked. `board show` reads
  `blocked_reason`, which the crash path never set — so the item an operator opens
  first showed an empty `blocked:` line and the cause was visible only in the audit
  history.

- The lease heartbeat starts inside the crash guard. The claim has already
  succeeded by then, so a failure spawning the thread left the item stranded in
  `running` until stale recovery reclaimed it a lease-timeout later; it is now
  blocked like any other crash.

- `Supervisor.close()` closes the board even when the memory close raises,
  instead of leaking the SQLite connection — which accumulates in a long-lived
  process (`syncmill tui`, `board run --loop`).

- `board stats` reads its three queries in one transaction. As separate
  autocommit statements they could straddle a concurrent claim, so the state
  counts came from a pre-claim world while the audit-derived `redispatched` and
  `reclaimed_stale` came from a post-claim one — a reported integrity alarm could
  describe a state that never existed.

- `Board.__init__` raises only `BoardError`, as documented. A db path containing a
  null byte raised `ValueError` straight through the constructor, past the
  `serve` gateway's catch. A reused run id is also reported as such rather than as
  a raw `UNIQUE constraint failed`, which read like board corruption.

- Concurrent `syncmill.toml` writes no longer lose an update. The atomic
  temp+replace only guaranteed no reader saw a half-written file; two writers
  could each parse the document and have the second replace discard the first's
  table — a `team save` racing the dashboard/TUI TEAM pane, or an `init --force`
  merge racing either. Every write now runs inside `teamfile.config_write_lock`,
  a blocking cross-process lock held across each caller's whole read-then-write
  span, not just the write helper's. The lock lives in syncmill's own
  state root, keyed by a digest of the config's real path: derived rather than
  probed from `git` (which would let two writers pick different files and
  serialize nothing), and outside the working tree entirely, so no checkout
  ever shows an untracked lock file or carries one into an agent worktree. The
  lock primitive's `os.fchmod` hardening is now skipped where that call does
  not exist (Windows), since an `AttributeError` there is outside the `OSError`
  boundary and would fail every config write before the lock was taken.

- Two writes to one memory namespace inside the same second no longer collapse
  into a single chunk. memtomem heads an appended entry with a second-resolution
  timestamp by default, and two entries sharing a heading re-chunk into one — so
  the earlier entry's text was swallowed and its chunk id, already returned to
  the caller, was re-minted, leaving the provenance share to look up a chunk that
  no longer existed and silently fall back to a direct write. Back-to-back writes
  are what a pipeline/council/decompose handoff does. Each write now supplies its
  own unique heading. The shared-result payloads also label themselves in bold
  rather than with a `##` heading: memtomem skips the supplied heading when the
  content brings its own, so every task result carried an identical heading —
  making the merge unconditional rather than same-second, and stranding the
  entry's tags in the previous entry's section, where `tag_filter` could not
  reach them.

### Removed

- The unused `_git_changed_files` / `_git_diff_patch` strategy helpers. They had
  no caller, and the patch helper staged the live worktree index — which is what
  `capture_changes` exists to avoid — while being the obvious name to reach for.
  Their coverage moved onto `capture_changes`.

### Documentation

- The README carries installation instructions for the released package and
  links into the documentation absolutely rather than repo-relatively. It is
  the package's description on PyPI, where a repo-relative link resolves
  against pypi.org and 404s; the link guard was widened to check those absolute
  links too, so moving a guide still breaks the build rather than the page.
- Both beginner guides describe memory correctly: an initialized project keeps
  its memory inside the repository, and only an uninitialized checkout uses
  memtomem's global store. The README no longer suggests a foreground fallback
  on Windows, where execution is refused either way.
- Every non-hidden command now appears in the README or a user guide, enforced by
  a new documentation test. It checks command paths, not their options, and
  accepts a mention anywhere in those files including inside a code fence — so it
  guards against a command going wholly undocumented, not against a thin or
  incorrect explanation. It found eleven, among them `board compact`, the only way
  to trim the audit table. Reviewing those eleven by hand then surfaced
  `recover --discard` (the only way to reclaim a retained recovery workspace),
  whose documented invocation was also wrong; the guard does not itself cover
  options.

## 0.1.4 — 2026-08-01

### Changed

- `--repo` now documents which of its two meanings each command uses, and every
  project command's `--help` says so. For `run` it names the repository to work
  on and overrides `repo_root`; for the project commands (`diff`, `apply`,
  `runs *`, `room *`, `doctor`, …) it selects *which project's configuration to
  load*, whose `repo_root` may point at another checkout — the layout where one
  project directs work at another. Behavior is unchanged; only the flag's
  meaning, previously easy to misread on a mutating command, is now stated.
- Documentation is now checked by the test suite: every relative link in a
  tracked Markdown file must resolve, and both user guides must mention every
  non-hidden `run` option.
- The documentation now matches the shipped contracts. `compete`, `council`, and
  `decompose` are described by the `--selection` policy they actually converge
  under (`verified`, `priority`, `judge`) rather than "the first success in
  priority order"; worktree teardown is described as normal but not guaranteed
  (a killed run leaves a workspace for the next run's sweep, and a
  recovery-marked one is kept deliberately); report retention is described as
  bounding the run directories syncmill owns rather than every directory in
  `runs_dir`; and advice to recover a tripped circuit breaker with
  `syncmill breaker reset` is gone — no such command exists, so the recovery is
  the configured cooldown and the half-open attempt that follows it.
  `syncmill.toml.example` also gained the settings it was missing: `[monitor]`,
  `[board] stall_seconds`, and the `[integration.trace]` tool-call capture
  fields.
- Historical raw-research notes moved from the repository root into
  `docs/archive/`. They are kept for provenance and still carry their
  "not the current contract" banner, but no longer sit beside the README as if
  they were current documentation.

### Fixed

- `doctor` now checks the repository it is actually reporting on. With one
  project configured to target another checkout, it snapshotted and
  dirty-checked the *configuration-owning* repository, derived the run-artifact
  namespace from it (naming a store no other command uses), and probed project
  memory under it — creating state in a checkout no run touches. It now resolves
  the operational target from `repo_root` for all four, and prints
  `Target repository:` whenever that differs from the project it read.
- A malformed `syncmill.toml` in a `--repo` target now produces the same
  one-line error for every command, not just `run`. `runs list/prune`, `diff`,
  `apply`, `discard`, `recover`, `continue`, `conversations *`, `room *`,
  `doctor`, and the isolated-session commands each carried their own narrower
  handler, so a TOML syntax error — a `ValueError`, not a validation error —
  escaped as a traceback. They now share one loader with one documented failure
  surface. A path that is not a Git repository at all — missing, a file, or
  simply not a checkout — is now reported as such instead of as invalid
  *configuration*.
- The TUI no longer exits when a repository disappears while an apply
  confirmation is being prepared. Resolving the target shells out to Git, and
  the resulting error escaped the event handler on both the artifact-apply and
  room-apply paths.

## 0.1.3 — 2026-08-01

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

- The monitor's environment snapshot is now consumed by the shim that reads it.
  It holds the launching shell's entire environment — provider keys included —
  and two exit paths (detaching before a run finishes, and a failed attach)
  deliberately keep the run directory, so the file lingered in the temp
  directory until some later monitored run's stale sweep found it a day old,
  which could be never.
- Agent subprocesses no longer inherit syncmill's own run-identity variables.
  An agent that went on to invoke syncmill satisfied the in-tmux monitor's
  self-detection and adopted the parent's run id, overwriting the parent run's
  envelope and result with its own.
- Report retention no longer removes directories it does not own. `runs_dir` is
  free-form configuration, so pointing it at an already-populated directory
  silently deleted unrelated data on the next run. Syncmill now marks the run
  directories it creates and will only remove a marked one — or, for run
  directories written before this version, one whose envelope both carries
  syncmill's own kind and names the directory it sits in. A directory that
  merely contains a generically named file is left alone.
- A malformed `syncmill.toml` in the working directory now produces the same
  one-line error for every command as it already did for `run --repo`, instead
  of a traceback.
- The TUI no longer exits when reading job state fails. The Runs poll was the
  one refresh path without a degrade guard, so an I/O error escaped the interval
  callback and took the whole app down; it now shows a degraded status and keeps
  the last good view, like the board, rooms, and sessions panes. Stopping a job
  whose worker vanished between the poll and the keypress likewise reports that
  the worker is unavailable instead of crashing.
- Board-run scratch memory no longer resurfaces in shared knowledge. Collecting
  a finished board run deleted its database rows but left its text in a markdown
  file shared with the `shared` namespace, so the next write to that file
  re-indexed the deleted text as new content and stamped it `shared`. Every
  write now goes to a per-namespace file, and collecting a scratch namespace
  removes its source files along with its rows. **Scratch written by an earlier
  version is not covered**: those entries sit in a shared daily file and carry
  no namespace, so there is nothing to select them on, and deleting the file
  would take shared knowledge with it. If that matters for your store, remove
  the affected `~/.memtomem/**/{date}.md` files by hand.
- The scratch collector is much less likely to delete a namespace out from under
  a live run. Stale-claim recovery reclaims a run that is merely slow, not only
  one that died, and the collector treated the released lease as permission to
  delete the namespace that run was still using for its stage-to-stage handoff.
  A reclaimed run now gets a grace period of one further lease interval; a run
  that finished normally is collected as before. A run that outlives the grace
  period is still collectable — its board write is already fenced off at that
  point, memory is advisory, and the namespace is recreated on its next write.
- A partly-successful shared write no longer leaves a duplicate copy. When some
  chunks reached the shared namespace and others didn't, the fallback added a
  second full copy beside them and reported no degradation; the incomplete
  result is now surfaced instead.
- Migrated memories are no longer at risk of being swept as orphans. Imported
  chunks recorded a source file path that never existed on disk, which is
  exactly what memtomem's orphan detection deletes; the migration now writes a
  real source file beside its other artifacts.
- Stopping an isolated session whose tmux kill fails no longer republishes it as
  healthy. A session awaiting recovery was rewritten as open, letting attach and
  list treat it as usable and destroying the signal that its worktree was never
  cleaned up; the state it was found in is now restored.
- An agent CLI emitting non-UTF-8 bytes no longer kills the run. The
  synchronous execution path decoded strictly, so a `UnicodeDecodeError` escaped
  every handler and failed the whole run (retaining a recovery workspace); it now
  decodes with replacement characters like the async path always has, restoring
  the documented "both paths classify identically" contract.
- Quality-gate commands now run in the same isolated environment agents get. An
  ambient `GIT_DIR`/`GIT_WORK_TREE` could make a gate validate a different
  repository than the worktree under judgement, and syncmill's Git isolation now
  also strips Git's configuration-injection variables
  (`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_GLOBAL`/`SYSTEM`, `GIT_CONFIG_COUNT` and
  the numbered key/value pairs), which could otherwise run an arbitrary
  `core.hooksPath` script inside an agent-writable worktree.
- Private run workspaces left behind by a killed run are now swept. They live
  under `workspace.root`, outside the repository, where the existing orphan
  cleanup could not see them, so every crash leaked a full control clone
  permanently. The sweep only touches directories carrying syncmill's own
  ownership marker — never a room workspace, an interactive session, or a
  retained recovery workspace — and only once the process that created the
  workspace is gone, which it checks directly rather than guessing from file
  timestamps. A run is therefore never reclaimed while it is running, however
  quiet it has been. The one time-based rule left is a week-long backstop for a
  dead owner whose process id has since been reused by something else.
- A failure while setting up a run (artifact store, live monitor, tracer) no
  longer leaks the workspace: those constructions now sit inside the guard that
  tears it down.
- Runs that fail before the strategy starts (an unsafe repository state, or a
  run-artifact creation failure) now finalize the live monitor, so a monitored
  run reports the real cause instead of an attach error, and a failing report
  writer can no longer replace that cause with an unrelated error.
- A run-artifact creation failure no longer retains an empty "recovery
  workspace" — no agent had run yet, so there was nothing to recover, and the
  retained directory was never cleaned up.
- LLM-judge selection is now best-effort. A failure to acquire the judge's
  worktree or to run the judge fell through as an exception and failed a run that
  already had a gate-verified winner; it now degrades to verified priority, the
  same fallback an unusable verdict already used. `--dry-run`'s
  `maximum_agent_calls` also now counts the judge's call.
- Concurrent subtasks handled by the same agent no longer share one live-monitor
  pane and log file, where their output interleaved byte-wise with nothing to
  tell them apart.
- Trace-only run directories no longer accumulate without bound. With
  `[integration.trace]` enabled and `[integration.report]` disabled, traces
  still land in the report `runs_dir` but nothing swept it — retention rode the
  report writer, which is never built when reporting is off. The Supervisor now
  prunes that tree under the existing `[integration.report] keep_last` whenever
  it is the tracer's output directory, bounding how many run directories are
  retained (best-effort, like all retention: a removal that fails is warned and
  skipped). A custom `[integration.trace] dir` remains user-owned and is never
  swept.
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
