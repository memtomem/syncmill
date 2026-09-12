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
world-readable migrations root. Cluster C (2026-08-02, PRs #91/#92/#93) then
closed the remainder of the sharp edges — rooms/TUI/monitor/telemetry, then
memory/agents/breaker, then preflight/board/supervisor plus the drift/cosmetics
batch; see the CHANGELOG's three "Review-backlog cluster C" entries for the
item-by-item account. A later pass (2026-08-02) then closed three more: the
unlocked toml read-modify-write, the hardcoded workspace-sweep backstop, and the
second-resolution daily-file headings. A further pass (2026-08-03) closed the
`project_id` marker trust. What still stands is one migration cosmetic.

## Sharp edges worth fixing when the area is next touched

Everything else that was listed here was fixed after 0.1.4 as cluster C
(PRs #91/#92/#93 — see the CHANGELOG). Three of the four that outlived it were
closed on 2026-08-02 and are struck through below for the record; only the last
remains open:

**~~Leaked workspaces can hold real disk for a week.~~ FIXED.** The sweep
collects a workspace as soon as its owning process is gone, so the ordinary
crash is reclaimed on the next run — but a workspace whose owner's pid was
reused waited out the week-long backstop, and a killed default `decompose` run
holds up to a dozen checkouts. The backstop is now `[workspace] stale_seconds`
(`SYNCMILL_WORKSPACE__STALE_SECONDS`), defaulting to the same week with a
one-hour floor: it overrides a live pid, so a shorter window is the one setting
that can delete a running run's workspace.

**~~Daily-file entries are keyed to the second.~~ FIXED.** `memtomem` heads each
appended entry with a second-resolution timestamp, so two writes to ONE
namespace inside the same second produced identical headings, merged into a
single chunk, and re-minted that file's chunk ids — leaving the provenance link
recorded against an id that no longer existed. syncmill now passes an explicit
per-write heading (`memory_backend._entry_title`), which needs no upstream
change; the real smoke's `rapid write` block pins it. The memtomem-side
sub-second heading remains worth doing for every OTHER writer, but syncmill no
longer depends on it.

**~~Config writes take no transaction-wide lock.~~ FIXED.** `_atomic_write` is
atomic per file, but every caller is a whole-document read-modify-write: a
concurrent CLI `team save` and dashboard save was a lost update, and
`init --force` performed an unlocked load → merge-or-back-up → write of the same
file. `teamfile.config_write_lock` now holds a blocking cross-process lock
across load→dump — taken inside each writer AND across every caller's
read-then-write span, which is where the window actually was. (The mode, fsync,
temp-permission, and backup-naming parts of this entry were fixed after 0.1.4.)

**~~`project_id` trusts `.syncmill/project.json` from the working tree~~
FIXED.** Cloning a repo that shipped one bound this machine's state roots to an
id chosen by whoever wrote it — including ids with path separators, which
traversed out of the runs root entirely. The marker is now honored only when it
is a safe single path token AND not tracked by Git (a tracked marker arrived
with the clone, not from this machine's `init`); a rejected marker degrades to
the path-hash id with a warning. `init` git-ignores the marker so a fresh
project can't accidentally commit it. (The `init --force` half of this entry
was fixed after 0.1.4.)

## Drift and cosmetics

The batch listed here was fixed after 0.1.4 as part of cluster C (PR #93; the
`build_backend` verbatim-refusal entry was resolved in PR #92 by documenting the
branch as deliberately defensive for future adapters). The last item —
`memory_migration._load` accepting a `schema_version == 1` that every consumer
then rejected — was fixed as issue #97: `_load` now rejects a legacy artifact at
the boundary, keeping the specific recovery guidance.

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
