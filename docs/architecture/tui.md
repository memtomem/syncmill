# Full TUI command center (`tui.py` + `tui_jobs.py`)

> Design record for `tui.py` + `tui_jobs.py`, moved verbatim from `CLAUDE.md`. `CLAUDE.md` keeps a
> short rule list for this module; this file holds the full reasoning, history and test map.

> **Known stale statements** — checked against the code on 2026-10-21. The text below
> is kept verbatim; where it disagrees with these notes, the code (and `CLAUDE.md`) win.
>
> - "The manager owns per-board `flock`" — locks are per target (`_target_lock_key`; a board run is keyed by its board db), and artifact-apply jobs exist alongside board jobs.

`syncmill tui` is deliberately separate from the compatibility `dashboard` app. It
has Board/Runs/Reports/Teams/Status/Sessions/Rooms tabs and may open with no Board
connection; a disabled or broken board is a limited-mode page, never an excuse to
create a DB. `--tab auto` (default) opens the first ENABLED-and-OPENED surface in
the order Rooms → Board → Runs → Status; an explicit `--tab` is always honored
(store bindings are independent — a corrupt Rooms SQLite must not disable the
filesystem-only Artifacts view). Runs nests Operations (JobStore) / Artifacts
(ArtifactStore, the SAME `project_id(root, config.project.id)` identity the
session store and `syncmill runs list` use) sub-tabs; artifact `g` apply goes
through a detached `ArtifactApplySpec` worker (never in-process), `k` discard runs
in a thread worker (blocking repository apply lock). A Sessions checkpoint lands
on Runs → Artifacts with the run selected. Rooms candidates have d/o/m
Diff/Output/Details views (Diff paged + digest-bound over the WHOLE file via
`RoomStore.load_candidate_patch_head`; failed candidates open on Output). The
global Footer is replaced by a per-tab action bar (two lines, availability-gated);
no text is rendered as markup: `tui_render.PlainStatic`/`PlainLabel`/`plain`/
`plain_select`, `OptionList(markup=False)`, and both Apps' `notify` default to
`markup=False` (a `[/x]` in an error raised while the toast rendered; `[enabled]`
was silently eaten) — `test_tui_render.markup_sinks` is the AST guard. Textual
passes ESC/C1/bidi to the terminal (it strips only BEL/BS/VT/FF/CR), so
`run_tui`/`run_dashboard` call `tui_render.install_render_guard()`, which replaces
`Strip.render` (every compositor update's terminal boundary) to substitute each
dangerous character with a SAME-WIDTH one (U+200B, or U+FFFD where an old rich
`UNICODE_VERSION` table gives it a cell) — never delete: layout is computed before
this point and deletion changes widths through ZWJ grapheme joins — and to drop a
link whose URL is unsafe. Fatal output bypasses `Strip.render`, so each App sets
`self.error_console = tui_render.error_console()` (escaped, uncoloured). The
guard only makes characters invisible; the body renderers (`render_artifact`,
`_artifact_diff_text`, `render_candidate_diff`/`_output`, the job log,
`render_room`/`render_session`) show them as the CLI does — `printable_text` for
bodies (patches decoded `surrogateescape`), `printable_name` for one-line fields,
escaped BEFORE splitting lines, on LF only. The widget funnels do the same for
everything else: `PlainStatic` escapes a string with `printable_text` on
construction and `update()` (idempotent on renderer output), `PlainLabel`/
`PlainOption`/`plain` with `printable_name`, both Apps' `notify` escape the message
and title, and `CollaborationConfirmModal` escapes its title/detail while callers
wrap each interpolated value in `printable_name` (a path with LF must not forge a
confirmation field). Editable values (`Input`, `TextArea`) are NOT escaped: the value is what
gets submitted, and escaping would rename a team on save — they stay as stored and
the render guard keeps them inert. The one exception is the round-prompt TextArea
prefill, which stays `escape_controls`-ed because the coordinator rejects controls
in a new prompt, so a raw prefill could not be submitted. Polling
preserves state by construction: `_sync_options`/`_update_static` signature-skip
unchanged content, and `_focus_primary` is only ever called on tab switches.
The Artifacts pane also stops REREADING on an unchanged tick (#168): the
listing runs in a thread worker (`group="artifact-listing"`) under single
flight: the in-flight flag is released ONLY when that thread's result reaches
the event loop (Textual cannot cancel a running thread, so clearing it early
let a second scan start beside the first); a refresh or
`_invalidate_artifact_listing` (rebind, checkpoint landing, discard) arriving
mid-scan marks the listing dirty and is coalesced into one rerun at
completion; results carry a generation so a stale or pre-rebind result is
dropped, never rendered; not `exclusive`, whose cancel could drop a
not-yet-started worker with the flag still set. `ArtifactStore.list_detailed` serves a manifest from its
per-instance parsed cache while the file's `(dev, ino, size, mtime_ns,
ctime_ns)` signature is unchanged (a stat per run, zero content reads; bounded
by `_LISTING_CACHE_MAX=4096` with eviction AFTER the traversal so a store
above the cap rereads only its uncached head, never thrashes). The detail pane
is deliberately NOT cached: it re-reads the ONE selected run every tick
(`load` + `read_result`, or `patch` in diff view — a constant independent of
N). A render-skip key was tried and removed after two review rounds found it
stuck on a placeholder and on a transient sidecar error; the regression tests
for both stay.
Rooms Details (`m`) and Compare (`c`) render from an **evidence snapshot**
(#172): a `room_compare.RoomComparison` of every round of one room, built only
on request (`c`, the first `m`, the `r` key in those views — never polling, never
tab activation) by `build_room_evidence` in a `room-evidence` thread worker that
opens its OWN `RoomStore` (SQLite is thread-affine), shown with `evidence as of
HH:MM:SS` and a `room changed since this snapshot` hint. A room change discards
it (the `_selected_room_id` property setter retires the generation and drops a
queued request); single flight, requests bound to their (room, store root), a
queued rerun never rendered intermediately, and "building" shown only while a
build for that room actually runs or waits. Tab activation and `r` rebind the stores
(`_rebind_collaboration`); the snapshot survives that and is dropped only when
the store root changes or the store becomes unavailable. Both
views read rows from the snapshot (no artifact read on the event loop), so their
gate/eligibility/usage/blocker wording is the CLI's; the Output view (`o`)
interprets none of it.
EVERY poll path is degrade-guarded (`_ROOM_POLL_ERRORS`/`_SESSION_POLL_ERRORS`/
`_JOB_POLL_ERRORS`) — an unguarded read escapes the interval callback and kills
the app, so a store failure must show DEGRADED and keep the last good view.
`JobStore.control` likewise converts everything to `JobError` (its callers catch
only that): the manager can vanish between the poll and the keypress, taking the
token file and leaving a truncated socket reply.
Board reads/writes stay on Textual's thread because SQLite is thread-affine.
Orchestration never runs inside Textual: a pure `BoardJobSpec` becomes the existing
`board run --id` or `board run --loop [--goal]` argv, and `JobStore` launches a
detached POSIX manager. The manager owns per-board `flock`, atomic 0600 job state,
two-segment bounded logs, a token-authenticated Unix socket, and a same-interpreter
Click child in its own process group. `q` only exits the UI. Explicit stop is
SIGINT-first; explicit force after ten seconds is SIGKILL and never fabricates a
Board transition. PID fields are informational only. `Board.renew_claim` plus the
Supervisor's separate-connection heartbeat prevents a healthy run from crossing the
lease timeout and being dispatched twice. Default tests use fake children only.
