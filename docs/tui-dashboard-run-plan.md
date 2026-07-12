<!-- Status: IMPLEMENTED. Integrated and validated 2026-07-12 from the dashboard-run R1 plan and the later durable `syncmill tui` decisions. -->

# `syncmill tui` — board-centered durable TUI

## Outcome

Add a separate `syncmill tui` command with five persistent tabs: **Board, Runs,
Reports, Teams, Status**. Keep `syncmill dashboard` unchanged as the small,
board-required observer/editor. The TUI never runs the Supervisor on Textual's
event loop; board execution belongs to a detached, reconnectable process manager.

## Locked behavior

- `syncmill tui [--refresh FLOAT]` requires a TTY but opens in limited mode when
  the board is disabled or broken. It must not create a disabled board DB.
- Board covers add, Tracegraph candidate import, decisions, update/stats, selected
  execution, and goal/all queue execution. Standalone `syncmill run` stays CLI-only.
- One TUI-managed worker is allowed per canonical board DB. External board runners
  remain legal and the Board's atomic claim remains the item-level guard.
- `q` closes only the UI. The worker and its log continue, and the next TUI process
  reconnects through the job registry and authenticated control socket.
- Safe stop is SIGINT to the CLI child's whole process group. After ten seconds an
  explicit second confirmation may SIGKILL it. There is no automatic SIGTERM step.
- Job logs are decoded readable text, not a byte-parity UI promise. stdout/stderr are
  merged, drained in chunks, stored in two rolling segments (10 MiB total by
  default), and displayed with replacement decoding and bounded line/cache sizes.

## Public configuration

```toml
[tui]
state_dir = "~/.syncmill/tui"
keep_jobs = 20
log_max_bytes = 10485760
```

Every field is environment-overridable through `SYNCMILL_TUI__*`.

## Architecture

1. The Textual app owns Board reads/human writes, tab state, report/team rendering,
   job launch requests, log tailing, and short memory-status probes.
2. A pure board job spec builds only these existing CLI contracts:
   - `board run --id ITEM [--team/--strategy/--agent]`
   - `board run --loop [--goal GOAL] [--team/--strategy/--agent]`
3. The launcher acquires a POSIX `flock` keyed by the resolved board DB and starts
   `python -m syncmill.tui_jobs worker` with a new session.
4. The manager owns versioned spec/state files, the lock, a token-authenticated Unix
   socket, rolling log, and a same-interpreter Click child. The child is its own
   process-group leader, so SIGINT/SIGKILL reaches agent descendants too.
5. Manager and child both retain the lock FD. If the manager dies while the child
   remains, a new launch fails closed instead of trusting a stale PID.
6. `Board.renew_claim` and a Supervisor heartbeat update only `claimed_at` through a
   lease-fenced CAS, preventing healthy long jobs from stale redispatch.

## Tabs and keys

- Global: `1..5`, `r`, `?`, `q`.
- Board: `f` filter, `a` add, `i` import, `d` decisions, `e` edit, `u` unblock,
  `h` clear human-required, `s` stats, `x` selected run, `g` queue run.
- Runs: `f` follow, `k` safe stop, `K` force stop after the grace period.
- Reports: newest-first shared `render_report`, read-only.
- Teams: `a` create, `e` edit, `u` default toggle, shared teamfile validation/write.
- Status: config-based agent state plus cancellation-safe `syncmill status` probe;
  memory probing is deferred while a managed worker is active.

## Safety and recovery

- Job directories are `0700`; spec, state, token, and log files are `0600`; JSON
  updates are temp-file + `os.replace`.
- Environment values and board task bodies are not copied into the job registry.
- PID/PGID values are informational. Control requires the matching job id and random
  token over the Unix socket.
- Lock held + dead socket is unreachable and blocks a second worker. Lock released
  without a terminal receipt becomes `lost`; the TUI does not invent a Board state.
- Graceful SIGINT uses Supervisor finalizers and records the claimed item as blocked,
  human-required, with an operator-cancel audit note. Forced termination may leave a
  lease or dirty worktree requiring later inspection; automatic deletion is not
  claimed.
- Durable execution is POSIX-only. Other platforms keep browsing/editing and disable
  execution with an actionable explanation.

## Validation

- Preserve all existing dashboard/CLI/board/supervisor/report/team tests.
- Headless Textual tests cover limited mode, tabs, Board writes, forms, and selection.
- Job tests cover argv, permissions, lock races, manager receipts, reconnect/lost
  reconciliation, log rotation, socket authentication, and safe/forced cancellation.
- Heartbeat tests prove a live claim survives its lease window and stale recovery
  resumes after heartbeat termination.
- Default automation uses fake children/agents only. Real paid-agent execution is an
  explicit manual smoke test.

## V1 exclusions

Standalone `run`, session/tmux attach, MCP serve, memory search/write/migration,
patch application, per-agent live panes/JSONL IPC, multiple TUI workers per board,
remote workers, and automatic resume/retry are out of scope.
