# Review backlog — 2026-08-01

A full-codebase review split across three areas (core orchestration; memory,
board, and persistence; CLI, TUI, and integrations) produced 3 critical, 12
major, and ~40 minor findings. Three further adversarial passes over the
resulting fixes found more — several of them cases where the prose claimed more
than the code delivered, and two of them regressions the fixes themselves
introduced. Notably, the workspace sweep's liveness check was rewritten twice
after review found each timestamp-based version capable of deleting a live run's
workspace; it now asks whether the owning process is still alive instead of
inferring it. Every round's critical and major findings are addressed except the
two limitations named below; see the CHANGELOG's dated 0.1.3 section.

This file is the remainder: findings judged real but not worth blocking the
0.1.3 cut, plus the ones that turned out to be non-issues. Each entry names the
code, states the claim in one line, and says what would have to be true for it
to matter. Line numbers drift — grep the symbol.

These are sharp edges, latent footguns, and cosmetic drift rather than
known-broken behavior. Two deliberate limitations are NOT listed below because
they are documented where they bite, in the CHANGELOG, CLAUDE.md, and the EN/KO
user guides: board
scratch written before the per-namespace-file change cannot be swept from the
old shared daily file (its entries carry no namespace), and the scratch
collector's grace period narrows the live-run deletion race rather than closing
it.

The board/lease cluster — the schema backfill's write on every open, the silent
lost lease, the heartbeat started outside the crash guard, the unguarded
`Supervisor.close()`, the missing crash `blocked_reason`, `Board.stats()`'s three
separate reads, and `Board.__init__`'s `ValueError` — was fixed after 0.1.4; see
the CHANGELOG. So was the data-safety cluster: `init --force` overwriting
`syncmill.toml`, the toml write narrowing a checked-in config to 0600, and the
world-readable migrations root. The rest stands, including the two residues
noted inline below (the toml write still takes no lock, and `project_id` still
trusts a committed marker).

## Sharp edges worth fixing when the area is next touched

**Leaked workspaces can hold real disk for a week.** The sweep collects a
workspace as soon as its owning process is gone, so the ordinary crash is
reclaimed on the next run — but a workspace whose owner's pid was reused waits
out the week-long backstop. A killed default `decompose` run holds up to a dozen
checkouts, so on a large tree that tail can be tens of gigabytes if it recurs.
This is now surfaced in both user guides; making the backstop configurable
remains a possible follow-up if anyone hits it.

**Daily-file entries are keyed to the second.** `memtomem` heads each appended
entry with a second-resolution timestamp, so two writes to ONE namespace inside
the same second produce identical headings, merge into a single chunk, and
re-mint that file's chunk ids. A provenance link recorded against a re-minted id
dangles. This predates the per-namespace-file change (two rapid writes to one
agent namespace always collided) and the opt-in real smoke now documents it
inline. Fixing it means either sub-second headings or a per-write discriminator,
both of which are memtomem-side.

**`breaker.degraded` is a one-way latch** (`breaker.py`, `_degrade`). A transient
failure reading the state file at construction makes the breaker permissive for
the whole process and never clears, even after every later save succeeds. Only
matters in a long-lived process (`syncmill tui`, `board run --loop`).

**Breaker state is read once, at `Supervisor.__init__`** (`breaker.py`). In a
long-lived process the in-memory state diverges from disk indefinitely; the
"last writer wins" reasoning in the docstring assumes one-shot CLI runs.

**Config writes take no transaction-wide lock.** `teamfile._atomic_write` is
atomic per file, but every caller is a whole-document read-modify-write: a
concurrent CLI `team save` and dashboard save is a lost update, and
`init --force` now performs an unlocked load → merge-or-back-up → write of the
same file. Closing it means holding a lock across load→dump, which is every
caller's shape, not just the write helper's. (The mode, fsync, temp-permission,
and backup-naming parts of this entry were fixed after 0.1.4.)

**`applied_team` does not survive a config snapshot** (`config._scrub_applied_team`
+ `cli._config_from_snapshot`). The field is forced to `None` at every
construction including `model_validate_json`, so a detached apply or room worker
launched from a team-selected session records `team: null` in its envelope.

**`dashboard._reload_teams` catches only `ValidationError`** where the TUI's
equivalent catches `(ValidationError, OSError, ValueError)` and explains why: a
mid-edit malformed toml must keep the cached view, not kill the app.

**`tui.action_checkpoint` blocks Textual's event loop.** `IsolatedSessionStore.checkpoint`
takes a flock, shells out for a diff, and writes the artifact store; the
neighbouring discard action threads exactly this kind of work with a comment
saying why. On a large worktree the UI freezes for the duration.

**Strict preflight's persisted evidence can contradict its outcome**
(`preflight.py`). The manifest's `decision`/`block_reasons` are built before the
persist step, and the persist-failure branch flips the returned block only — so
the per-eval artifacts on disk can read `allow` for a run that was correctly
blocked.

**`telemetry_hook` derives `sequence` from a count that skips unparseable lines.**
One torn write makes every later record reuse an existing sequence number, and
the reader sorts by it. Also an O(n²) rescan per event.

**Advisory preflight does not shape-validate `candidate_tools`** (`preflight.py`).
Only `mode=strict` runs that validator, so in advisory mode a token like `--out`
is spliced into the toolgraph argv as an option. No shell is involved, so this is
argv-position confusion, not command injection.

**`project_id` trusts `.syncmill/project.json` from the working tree**
(`project.py`), so cloning a repo that ships one binds this machine's state roots
to an id chosen by whoever wrote it. (The `init --force` half of this entry was
fixed after 0.1.4.)

**LangGraph paging loops have no iteration cap** (`memory_backend.py`,
`count_namespace`/`delete_namespace`). A store whose `asearch` ignores `offset`
spins forever; the mem0 adapter has a no-progress guard, these don't.

**`rooms.prune_rooms` moves candidate artifacts before checking eligibility**, so
a concurrent read of a room that turns out ineligible sees a missing artifact
during the restore window. `prune_terminal` also parses `updated_at` unguarded —
one malformed row aborts the whole prune.

**`rooms.run_round`'s crash path can name a deleted run root**: `existing` is
captured before workspace cleanup and only refreshed inside the pool branch, so a
lineage that cleaned up and then failed early records a recovery path the
operator cannot act on.

**`board.import_review_candidates` ignores `goal` when matching provenance**, so
re-importing a report under a new goal reports "skipped" while the item keeps its
original goal and never appears under the new one.

**`isolated_session._PROCESS_LOCKS` is never evicted** — one `threading.Lock` per
session id ever touched, in a long-lived TUI process.

**`monitor.FileSink` never closes its fds** outside `finish()`, and
`monitor.write_result` uses a fixed `.tmp` sibling rather than the mkstemp
recipe used everywhere else.

**`aexecute_all` bypasses `_gather_or_drain`** (`agents.py`), the cancel-and-drain
tail its siblings share and the docstring calls safety-critical. It has no caller
in `src/`, which is exactly why it is a footgun for the next concurrent strategy.

**`memory._loop` is not thread-safe** and `close_event_loop()` discards a loop
backends may still hold resources against. Only main-thread callers exist today.

**`memory.scoped()` raises `ValueError`** — the one exception to "every
`SyncmillMemory` method returns a safe default" — and a doubly-scoped view would
capture the parent's board-run scope as its "global shared", union-reading
another run's scratch. Not reachable today (only the Supervisor calls it, on the
unscoped memory).

**`search_shared`/`get_shared`/`shared_stats` are shared-only by name, not by
construction** (`memory.py`): they resolve `self._shared_ns`, which on a scoped
view IS the board-run scratch. Handing a scoped view to `build_gateway` would
serve scratch as shared and pass the namespace check. Nothing does that today.

**`Board.next_runnable` is O(items × deps) queries** under a held RESERVED lock.

**Uninstrumented judge call**: the LLM judge's execution leaves no span and mints
a provider session for a throwaway worktree, so a whole agent invocation is
invisible in the trace.

## Drift and cosmetics

- `model_routing.render_banner` has a dead branch: `applied` is always `False`
  for auto decisions, so `config_mode` can never be the printed label.
- `planning.build_run_plan`'s bare `else` attributes an unknown strategy to
  decompose's topology instead of failing.
- `config.selection.mode` is inert for route/pipeline, but `--dry-run` prints
  `selection: judge` for them anyway.
- `strategies/base.py`'s unknown selection mode degrades to `verified` via an
  implicit `!=` rather than an explicit branch.
- `worktree.py`: a failed `git worktree add` does not advance `_next_slot`, so a
  retry reuses the same branch and path; `git rev-parse --show-toplevel`'s exit
  code is unchecked (an empty result resolves to the process cwd); and
  `cleanup_worktree` re-imports `shutil` locally.
- `pipeline.py`'s final-stage span is built from loop-leaked variables — correct
  only because the guards above make an empty loop impossible.
- `_crash_result` never sets `run_id` where the success path does.
- `memory_backend.build_backend`'s `verbatim_storage` refusal is unreachable
  (all three backends leave the field at its default), `search_multi` joins
  namespaces with commas without validating them, and `status()` reads a
  memtomem private so a rename reports version skew as a connection failure.
- `memory_migration._load` accepts `schema_version == 1`, which every consumer
  path then rejects.
- `gateway.py` calls board methods on a `Board | None` narrowed only through a
  helper, so type checkers cannot see it.
- `teamfile.toml_path()` returns a relative path, so the comment claiming a
  cwd-drift-immune resolved path is wrong; `_project_root`/`_job_project_root`
  derive the project anchor from the process cwd.
- `tui._probe_memory` runs `syncmill status` without `--repo`, probing the
  ambient cwd config rather than the TUI's pinned toml.
- `rooms.launch_room_worker` puts the positional id before options with no `--`
  separator (the opposite of what `build_job_argv` documents) and passes the
  prompt in argv, visible in `ps`.
- `tui_collaboration` raises `TypeError` on a manifest with an explicit
  `"output": null`, degrading only because `TypeError` happens to be in the room
  poll's catch set.
- `cli.py`'s `room candidates`/`room diff` catch only `RoomError`, so a manifest
  missing an expected key tracebacks.
- The control socket is bound and then chmod'ed 0600 (the window is covered only
  by the 0700 runtime dir), and `_handle_control` checks the job id before the
  constant-time token compare — a harmless pre-auth existence oracle.

## Reviewed and found not to be problems

- **`WorktreePool.supports_snapshots` looks vestigial but is load-bearing for
  tests.** It is a hardcoded `True`, so the non-snapshot pool is unreachable in
  production — but most of the supervisor suite monkeypatches `WorktreePool` with
  a plain function, which makes the `getattr` fall back to `False`. Removing the
  probe would silently re-route those tests.
- **`aexecute_each`'s new `labels` parameter does not weaken agent purity.** The
  label rides an ambient ContextVar the fan-out sets per item, exactly like the
  existing output-sink seam; `AgentRunner.aexecute` still takes only
  `(prompt, workdir, timeout)`.
- **Every winner passes the gate in all three selection modes**, so
  `render_result`'s unconditional "Quality gate: passed" is not a false claim.
- **The invariants the reviewers were asked to check held**, except for worktree
  teardown (fixed): agents stay pure, the Supervisor remains the only component
  touching both agents and memory, memory never aborts a run, the breaker records
  one raw pre-gate status per agent per run, gate rejections never trip it,
  capability matching only re-orders, the shared-loop rule is respected, `serve`
  keeps stdout clean, `--monitor` always falls back while `session` never does,
  strict preflight requires report recording, retention leaves a custom
  `trace.dir` alone, the TUI never orchestrates in-process, and `_inner_argv`
  forwards exactly what it documents.
