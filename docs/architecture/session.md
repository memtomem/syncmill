# Durable interactive sessions (`session.py`, #16-sibling, #17, v0.4)

> Design record for `session.py`, moved verbatim from `CLAUDE.md`. `CLAUDE.md` keeps a
> short rule list for this module; this file holds the full reasoning, history and test map.

> **Known stale statements** — checked against the code on 2026-10-21. The text below
> is kept verbatim; where it disagrees with these notes, the code (and `CLAUDE.md`) win.
>
> - The shim "differences" from `monitor._ENV_SHIM` — the two shims are now identical (`monitor._ENV_SHIM == session._SESSION_ENV_SHIM`); monitor's shim also unlinks before parsing.
> - The imported primitives and the snapshot exclusion also include `RUN_ID_ENV`, and `_write_session_env_snapshot` takes an `excluded_env` parameter; the text omits both.

The `session` command group (`start`/`list`/`attach`/`stop`) makes the interactive-collaboration mode **durable**: it launches an agent's **bare interactive CLI** (`claude`/`codex`/`kimi`) inside a dedicated tmux session that survives detach (`Ctrl-b d` / terminal close / SSH drop) and **reattaches by deterministic name** — the v0.4 acceptance criterion. The **complement** to #16: where `--monitor` is an *ephemeral* view of a *programmatic* concurrent run, `session` is a *long-lived* host for a *human-driven interactive* agent. Maintainer-approved (AskUserQuestion): (1) scope = the four-verb lifecycle; (2) a new **flat `session.py`** (gateway/monitor precedent) that *reuses* monitor's tmux primitives; (3) a **standalone `_INTERACTIVE` map** (`{agent_id: [binary]}`) for the launch command.

Core invariants: **not an agent, not a strategy, touches neither agents nor memory** — pure tmux + process launch (so the "Supervisor is the only thing touching both agents and memory" invariant is untouched). The interactive session NEVER enters `AgentRunner.execute`/`aexecute` (that would re-incur programmatic billing and break agent purity), so the per-agent launch command is a standalone `_INTERACTIVE` map **deliberately separate from** `CLIAgent._build_command` (it duplicates the `kimi-code → kimi` binary fact on purpose — interactive mode does **not** route through the registry, so it can run an agent that is `enabled=false` for orchestration; the CLI validates the requested agent against `_INTERACTIVE` keys, **not** `registry.available()`). `session.py` **reuses monitor's primitives** via `from syncmill.monitor import _tmux, _tmux_attach, _SNAPSHOT_SKIP_ENV, INNER_ENV` (`monitor.py` stays **UNCHANGED**) — private cross-module imports, the established `cli.py`→`supervisor._matched_order` pattern, so tests patch the **lookup site** `session._tmux` (not `monitor._tmux` — patch where a name is LOOKED UP, not where it is defined). It defines its **own `SessionLaunchError`** rather than reusing `MonitorLaunchError` (whose contract is "the CLI falls back to a normal run on catch" — but `session` has **no fallback**, it errors: `session` IS the tmux feature; the CLI gates `tmux_available()` as a hard error, and `_stdout_isatty()` for the foreground-attaching `start`/`attach`).

Durability mechanism: **deterministic names** `syncmill-session-<agent>` (not the monitor's uuid suffix) are the reattach key — `start` recomputes the name, and if the session exists it skips creation and attaches (reattach); else it creates a **detached** `new-session -d -s <name> -c <workdir>` running the bare binary through a session-specific exec shim, then attaches. No `remain-on-exit` (unlike the monitor — the session **should** vanish when the user quits the agent). The working dir is the **resolved `repo_root`** (the real tree — interactive edits land there, per the collaboration contract; the CLI validates `is_dir()` because tmux `-c` silently falls back to `$HOME`). Like the monitor, the detached session would inherit the stale tmux-**server** env, so the outer's environment is restored from a **`0600` snapshot file** (never argv — no secrets in `ps`) via **`_SESSION_ENV_SHIM`** — a *session-specific* shim (NOT `monitor._ENV_SHIM`) with two deliberate differences, both codex-design-review-driven: (a) it **self-unlinks the env file in a `finally` immediately after the read, BEFORE `json.loads`** — the detached pane's shim runs *asynchronously after* `new-session` returns, so the parent can't delete the file without a use-after-free; the shim consuming it is race-free and shrinks the secret window even on a corrupt snapshot; the parent only `rmtree`s the run dir on the **failure** paths (where the shim never started); (b) the snapshot **excludes `INNER_ENV`** as well as `_SNAPSHOT_SKIP_ENV`, so an already-exported `SYNCMILL_MONITOR_DIR` can't reach the interactive process and false-trip `monitor.inner_dir`. **Lifecycle ops match EXACT known names** (`{session_name(a) for a in _INTERACTIVE}`), never the `syncmill-session-` prefix — so `list`/`stop --all` can't show or kill a monitor run even if a user sets `SYNCMILL_MONITOR__SESSION_PREFIX=syncmill-session` (a destructive-op collision codex flagged). `#{session_attached}` is a client **count**, so attached = `int > 0`. A sentinel-guarded (`.syncmill-session`, distinct from monitor's `.syncmill-inner`), age-bounded `_sweep_stale_session_dirs` reaps orphaned run dirs (the #16 R5 ownership lesson). Hermetic tests (`tests/test_session.py`) never launch real tmux (patch `session._tmux`/`_tmux_attach`/`_sweep_stale_session_dirs`); the shim is exercised in a real tmux-free `python -c` child (env applied + file unlinked, incl. a corrupt-env case), and the gate/arg-shape surface in `tests/test_cli.py` via `_patch_session_seams`. **#17 = last v0.4 item.** No new config table (don't pre-abstract — the session prefix is a module constant).

## Isolated sessions: pane cwd check (2026-11-01)

`isolated_session.py` reuses `_create_session` to start a project-scoped
session in its own worktree (`new-session -c <worktree>`).
`IsolatedSessionStore.attach` refuses unless the session has exactly one pane
and that pane's `#{pane_current_path}` is the worktree.

A new pane is a fork of the tmux server, so its process starts in the
**server's** cwd and only then chdirs to `-c`. tmux 3.4 did the chdir right
away (its systemd cgroup move ran in the server before it answered). tmux 3.6a
moves that systemd scope call (`systemd_move_to_new_cgroup`, a D-Bus
StartTransientUnit plus a job wait that tmux caps at one second) into the
**child, before the chdir**, and the server answers `new-session` without
waiting for it. For up to about a second after `create`, `pane_current_path`
can therefore report the server's cwd. The `posix-process` CI job hit this when
`ubuntu-latest` moved from ubuntu-24.04 (tmux 3.4) to ubuntu-26.04 (tmux 3.6a)
(#246, root cause from #247).

`attach` polls for up to `_PANE_CWD_SETTLE_SECONDS` (3 s). The acceptance
condition is unchanged: a cwd that never becomes the worktree is still refused
after the deadline, more than one pane is refused at once, and a failing
`list-panes` raises at once. An empty path is polled like any other mismatch.
