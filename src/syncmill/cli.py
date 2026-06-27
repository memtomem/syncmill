"""CLI entry point for syncmill."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from syncmill.board import (
    BoardError,
    Evidence,
    WorkState,
    build_board,
    render_board,
    render_board_stats,
    render_decision,
    render_decisions,
    render_item,
)
from syncmill.config import SyncmillConfig
from syncmill.memory import close_event_loop, run_async
from syncmill.models import Task, TaskStatus
from syncmill.monitor import (
    MonitorLaunchError,
    inner_dir,
    launch_monitored_run,
    render_task_result,
    tmux_available,
)
from syncmill.session import (
    SessionLaunchError,
    attach_session,
    default_interactive_agent,
    interactive_agents,
    list_sessions,
    start_session,
    stop_sessions,
)
from syncmill.strategies import CONCURRENT_STRATEGIES, available_strategies
from syncmill.supervisor import Supervisor, _matched_order
from syncmill.worktree import cleanup_orphaned_worktrees


@click.group()
@click.version_option(package_name="syncmill")
def cli():
    """syncmill — Multi-agent orchestration with shared memory."""


def _stdout_isatty() -> bool:
    """Whether stdout is an interactive terminal — a patchable seam (``CliRunner``
    always reports False), so the monitor's TTY gate is hermetically testable."""
    return sys.stdout.isatty()


def _inner_argv(
    *,
    prompt: str,
    agent: str | None,
    exclude: list[str],
    timeout: int | None,
    repo: str | None,
    capability: list[str],
    strategy: str | None,
) -> list[str]:
    """Reconstruct the ``syncmill run …`` argv for the in-tmux INNER process
    DETERMINISTICALLY from the resolved CLI params — never ``sys.argv`` (wrong
    under ``CliRunner`` / ``python -m``). ``--monitor`` is deliberately OMITTED: the
    inner is marked by ``SYNCMILL_MONITOR_DIR`` (+ sentinel), so omitting it removes
    any re-launch recursion risk even if monitoring is also enabled via env.

    Options come first, then a ``--`` end-of-options marker, then the prompt LAST, so
    a dash-prefixed prompt (e.g. ``syncmill run -- "--fix-auth"``) round-trips intact
    rather than being mis-parsed as an option by the inner's click parser."""
    argv = ["syncmill", "run"]
    if agent:
        argv += ["--agent", agent]
    for x in sorted(exclude):
        argv += ["--exclude", x]
    if timeout:
        argv += ["--timeout", str(timeout)]
    if repo:
        argv += ["--repo", repo]
    for c in capability:
        argv += ["--capability", c]
    if strategy:
        argv += ["--strategy", strategy]
    argv += ["--", prompt]
    return argv


def _install_inner_signal_handlers() -> None:
    """In the in-tmux INNER process, turn SIGHUP/SIGTERM into ``SystemExit`` so the
    Supervisor's ``finally`` (worktree cleanup, memory session end) still runs when
    the user kills the tmux server out from under the run — protecting the worktree
    invariant directly rather than leaning only on the next run's orphan sweep."""
    import signal

    def _bail(signum, frame):  # noqa: ANN001 - signal handler signature
        raise SystemExit(130)

    for sig in (signal.SIGHUP, signal.SIGTERM):
        try:
            signal.signal(sig, _bail)
        except (ValueError, OSError):  # not main thread / unsupported — best effort
            pass


def _launch_monitor_if_requested(
    config: SyncmillConfig,
    *,
    prompt: str,
    agent: str | None,
    exclude: list[str],
    timeout: int | None,
    repo: str | None,
    capability: list[str],
    strategy: str | None,
) -> int | None:
    """If ``--monitor`` was requested AND viable, run the whole orchestration inside
    tmux and return its exit code; otherwise return ``None`` so the caller falls
    through to a normal (unmonitored) run in THIS process.

    Viable means: monitoring enabled, this is NOT already the inner process, the
    strategy is concurrent (the seam is async-only), stdout is a TTY, and tmux is
    installed. Each missing precondition prints a one-line notice and falls through
    — the monitor is a convenience, never a hard requirement.
    """
    if not config.monitor.enabled or inner_dir() is not None:
        return None  # not requested, or we ARE the inner orchestration
    sname = config.default_strategy
    if sname not in CONCURRENT_STRATEGIES:
        click.secho(
            f"[syncmill] --monitor applies only to concurrent strategies "
            f"({', '.join(sorted(CONCURRENT_STRATEGIES))}); running '{sname}' "
            f"without the live monitor.",
            fg="yellow",
        )
        return None
    if not _stdout_isatty():
        click.secho(
            "[syncmill] --monitor needs an interactive terminal (stdout is not a "
            "TTY); running without the live monitor.",
            fg="yellow",
        )
        return None
    if not tmux_available():
        click.secho(
            "[syncmill] --monitor needs tmux installed and on PATH; running "
            "without the live monitor.",
            fg="yellow",
        )
        return None
    inner_argv = _inner_argv(
        prompt=prompt,
        agent=agent,
        exclude=exclude,
        timeout=timeout,
        repo=repo,
        capability=capability,
        strategy=strategy,
    )
    try:
        return launch_monitored_run(
            inner_argv, session_prefix=config.monitor.session_prefix
        )
    except MonitorLaunchError as exc:
        click.secho(
            f"[syncmill] tmux session failed to start ({exc}); running without "
            f"the live monitor.",
            fg="yellow",
        )
        return None


def _validate_strategy(config: SyncmillConfig) -> None:
    """Reject an unknown ``default_strategy`` with a clean CLI error.

    ``--strategy`` is already gated by ``click.Choice``, but the value can also
    arrive via ``SYNCMILL_DEFAULT_STRATEGY``, which bypasses that. Without this,
    ``Supervisor(config)`` would surface the typo as a raw ``ValueError``
    traceback (``_make_strategy``) rather than a one-line message.
    """
    if config.default_strategy not in available_strategies():
        raise click.BadParameter(
            f"unknown strategy: {config.default_strategy!r}. "
            f"Valid: {', '.join(available_strategies())}",
            param_hint="'--strategy' / '-s' or SYNCMILL_DEFAULT_STRATEGY",
        )


@cli.command()
@click.argument("prompt")
@click.option("--agent", "-a", default=None, help="Force a specific agent (no fallback)")
@click.option(
    "--exclude",
    "-x",
    multiple=True,
    help="Exclude an agent from the run (repeatable) — e.g. drive it "
    "interactively yourself while syncmill orchestrates the rest.",
)
@click.option("--timeout", "-t", default=None, type=int, help="Override timeout in seconds")
@click.option(
    "--repo",
    "-r",
    default=None,
    help="Repository root path (overrides repo_root from syncmill.toml / "
    "SYNCMILL_REPO_ROOT; default: current directory).",
)
@click.option(
    "--capability",
    "-c",
    multiple=True,
    help="Capability hint for this task (repeatable) — routes an agent declaring "
    "a matching capability ahead of raw priority order, falling back to priority "
    "when nothing matches. Requires declared capabilities "
    "(SYNCMILL_CAPABILITIES__AGENTS); a no-op under --agent (one forced agent has "
    "no ordering to influence).",
)
@click.option(
    "--strategy",
    "-s",
    type=click.Choice(available_strategies()),
    default=None,
    help="Orchestration strategy (default: route). 'route' tries agents in "
    "priority order with fallback; 'pipeline' chains them as sequential "
    "stages, each building on the previous via shared memory; 'compete' runs "
    "all agents concurrently in isolated worktrees and keeps the "
    "highest-priority one that succeeds; 'council' has agents propose, critique "
    "over rounds, then synthesize a converged result, deliberating via shared "
    "memory; 'decompose' splits the task into subtasks, runs them across agents "
    "concurrently, then synthesizes the results into one outcome.",
)
@click.option(
    "--monitor",
    is_flag=True,
    help="Live tmux monitor: run a concurrent strategy (compete/council/decompose) "
    "inside a tmux session with one live pane per agent, auto-attached. No-ops with "
    "a notice for sequential strategies, a non-TTY stdout, or a missing tmux.",
)
def run(
    prompt: str,
    agent: str | None,
    exclude: tuple[str, ...],
    timeout: int | None,
    repo: str | None,
    capability: tuple[str, ...],
    strategy: str | None,
    monitor: bool,
):
    """Execute a task through the agent orchestration pipeline."""
    # Build from env + syncmill.toml + defaults, then layer the explicit CLI
    # flags on top (each only when actually passed) — so `--repo` left unset lets
    # repo_root come from the file/env instead of clobbering it with the default.
    config = SyncmillConfig()
    if repo:  # ignore an empty `-r ""` like exclude/capability do, so it falls
        config.repo_root = repo  # back to repo_root from the file/env, not cwd
    if strategy:
        config.default_strategy = strategy
    if monitor:
        config.monitor.enabled = True  # per-run opt-in, like --strategy
    _validate_strategy(config)
    if timeout:
        # Apply the override to every configured agent — agent_config returns the
        # real (mutable) field, so this stays correct as the lineup changes.
        for agent_id in config.agent_priority:
            config.agent_config(agent_id).timeout_seconds = timeout

    exclude_set = {x for x in exclude if x}  # drop empty `-x ""` values
    unknown = exclude_set - set(config.agent_priority)
    if unknown:
        raise click.BadParameter(
            f"unknown agent(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(config.agent_priority)}",
            param_hint="'--exclude' / '-x'",
        )
    if agent and agent in exclude_set:
        raise click.BadParameter(
            f"agent '{agent}' is both forced (-a) and excluded (-x)",
            param_hint="'--exclude' / '-x'",
        )

    cap_list = [c for c in capability if c]  # drop empty -c "" values, like exclude_set

    # In the in-tmux INNER process, make tmux-server death run the Supervisor's
    # cleanup (harmless no-op in a normal run).
    if inner_dir() is not None:
        _install_inner_signal_handlers()

    # Live tmux monitor (#16): if --monitor was requested AND viable, run the whole
    # orchestration inside tmux and exit with its code. Otherwise fall through to a
    # normal run in this process (the inner re-invocation takes this same path, but
    # with inner_dir() set it never re-launches — see _launch_monitor_if_requested).
    monitor_code = _launch_monitor_if_requested(
        config,
        prompt=prompt,
        agent=agent,
        exclude=list(exclude_set),
        timeout=timeout,
        repo=repo,
        capability=cap_list,
        strategy=strategy,
    )
    if monitor_code is not None:
        sys.exit(monitor_code)

    # Cleanup any orphaned worktrees from crashed runs. Use the resolved
    # config.repo_root (which may come from syncmill.toml / env), not the raw
    # flag, so cleanup and the Supervisor's worktree pool target the same root.
    repo_path = Path(config.repo_root).resolve()
    cleanup_orphaned_worktrees(repo_path)

    supervisor = Supervisor(config)
    task = Task(prompt=prompt, tags=cap_list)

    try:
        click.echo(f"[syncmill] Executing task {str(task.id)[:8]}...")
        if agent:
            click.echo(f"[syncmill] Agent: {agent} (forced, no fallback)")
        else:
            # Show the order the strategy will actually see: re-order through the
            # Supervisor's OWN matcher via the same _matched_order helper it uses,
            # so the banner is the SAME computation as the run — not a parallel
            # re-derivation that could drift. Identity (raw priority) when no
            # matcher is configured or the task carries no hint.
            available = [
                a for a in supervisor.registry.available()
                if a.agent_id not in exclude_set
            ]
            names = [a.agent_id for a in _matched_order(available, task, supervisor.matcher)]
            shown = " → ".join(names) if names else "(none)"
            suffix = f" — excluded: {', '.join(sorted(exclude_set))}" if exclude_set else ""
            if cap_list:
                suffix += f" — capability hint: {', '.join(cap_list)}"
            mode = {
                "pipeline": "pipeline",
                "compete": "compete",
                "council": "council",
                "decompose": "decompose",
            }.get(config.default_strategy, "priority fallback")
            click.echo(f"[syncmill] Agents: {shown} ({mode}){suffix}")

        result = supervisor.run(task, agent_id=agent, exclude=exclude_set)

        # Surface degraded memory without failing the task
        if supervisor.memory.degraded:
            click.secho(
                f"[syncmill] memory unavailable — continuing without shared context "
                f"({supervisor.memory.last_error})",
                fg="yellow",
            )

        # Surface a degraded circuit breaker the same way — its state file was
        # unreadable, so it tracked nothing this run, but the task still ran.
        if supervisor.breaker is not None and supervisor.breaker.degraded:
            click.secho(
                f"[syncmill] circuit breaker state unavailable — not tracking "
                f"failures ({supervisor.breaker.last_error})",
                fg="yellow",
            )

        # Display result via the shared renderer — byte-identical to a monitored
        # run's result.json render, so --monitor changes nothing the user sees.
        click.echo()
        exit_code = render_task_result(result)
        if exit_code != 0:
            sys.exit(exit_code)
    finally:
        supervisor.close()
        close_event_loop()


@cli.command()
def agents():
    """Show registered agents and their status."""
    config = SyncmillConfig()
    _validate_strategy(config)
    supervisor = Supervisor(config)

    try:
        click.echo("Registered agents (priority order):\n")
        for i, agent_id in enumerate(config.agent_priority, 1):
            agent_cfg = config.agent_config(agent_id)
            agent = supervisor.registry.get(agent_id)
            status = "enabled" if agent else "disabled"
            color = "green" if agent else "red"

            click.echo(f"  {i}. ", nl=False)
            click.secho(agent_id, fg=color, bold=True, nl=False)
            click.echo(f" [{status}]", nl=False)
            click.echo(f"  timeout={agent_cfg.timeout_seconds}s, max_turns={agent_cfg.max_turns}")
    finally:
        supervisor.close()
        close_event_loop()


@cli.command()
@click.option(
    "--http",
    is_flag=True,
    help="Serve over HTTP (streamable-http) instead of stdio. Exposes shared "
    "memory over the network with NO authentication — bind to a trusted host only.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    help="Bind host for --http (default: 127.0.0.1, loopback). A non-loopback host "
    "exposes shared memory to the network.",
)
@click.option(
    "--port", default=8000, type=int, help="Bind port for --http (default: 8000)."
)
def serve(http: bool, host: str, port: int):
    """Run the read-only MCP gateway over the shared memory namespace.

    Exposes three read-only tools — shared_search, shared_read, shared_stats — to
    external MCP clients (Claude Code/Desktop, Cursor, …) so they can READ
    syncmill's shared knowledge. This is a one-way, consumer-facing surface:
    syncmill agents never touch MCP and the Supervisor remains the sole writer.

    Default transport is stdio (the process speaks MCP on stdin/stdout); pass
    --http for streamable-http at http://HOST:PORT/mcp.
    """
    # Lazy imports (like `status`) so importing the CLI doesn't pull FastMCP.
    from syncmill.gateway import build_gateway
    from syncmill.memory import SyncmillMemory

    config = SyncmillConfig()
    memory = SyncmillMemory(config.memory)

    # CRITICAL: under stdio, stdout is the JSON-RPC channel — EVERY human-facing
    # line must go to stderr (err=True) or it corrupts the protocol stream. The
    # command writes nothing to stdout itself.
    if memory.degraded:
        click.secho(
            f"[syncmill] memory unavailable — gateway will report shared reads as "
            f"unavailable ({memory.last_error})",
            fg="yellow",
            err=True,
        )
    # Build the read-only board for the board_* tools (None when disabled). The board
    # is load-bearing, but the gateway is a best-effort READ surface for it: a corrupt
    # board degrades the board_* tools rather than killing the shared-memory gateway.
    # Thread the error through so the tools report it as "unavailable" (enabled but
    # broken), NOT "not enabled" — the disabled-vs-broken distinction clients need.
    board = None
    board_error = None
    try:
        board = build_board(config.board)
    except BoardError as exc:
        board_error = str(exc)
        click.secho(
            f"[syncmill] work-board unavailable — board_* tools will report it ({exc})",
            fg="yellow",
            err=True,
        )
    try:
        mcp = build_gateway(
            memory, board=board, board_error=board_error, host=host, port=port
        )
        if http:
            if host not in ("127.0.0.1", "localhost", "::1"):
                click.secho(
                    f"[syncmill] WARNING: binding {host}:{port} exposes read-only "
                    f"shared memory to the network with NO authentication — use a "
                    f"trusted network or an authenticated proxy.",
                    fg="red",
                    err=True,
                )
            else:
                click.secho(
                    f"[syncmill] serving shared memory over HTTP at "
                    f"http://{host}:{port}/mcp (read-only, no auth, loopback only)",
                    fg="yellow",
                    err=True,
                )
            mcp.run(transport="streamable-http")
        else:
            click.secho(
                "[syncmill] serving shared memory over stdio (read-only)",
                fg="green",
                err=True,
            )
            mcp.run(transport="stdio")
    finally:
        # Close memory off a fresh loop (mcp.run's anyio loop has finished). Not
        # run_async — the serve path stays off syncmill's shared module loop.
        import anyio

        anyio.run(memory.close)
        if board is not None:
            board.close()  # sync sqlite close (idempotent, best-effort)


@cli.command()
def status():
    """Show memtomem connection status and recent sessions."""
    config = SyncmillConfig()
    from syncmill.memory import SyncmillMemory

    memory = SyncmillMemory(config.memory)
    try:
        info = run_async(memory.status())

        if info.get("connected"):
            click.secho("memtomem: connected", fg="green")
            click.echo(f"  db_path: {info.get('db_path')}")
        else:
            click.secho("memtomem: disconnected", fg="red")
            click.echo(f"  error: {info.get('error')}")
    finally:
        run_async(memory.close())
        close_event_loop()


def _require_tmux() -> None:
    """Gate every ``session`` subcommand on tmux being installed. Unlike
    ``run --monitor`` (which falls back to a normal run), there is NO fallback —
    ``session`` IS the tmux feature — so a missing tmux is a hard, clean error."""
    if not tmux_available():
        raise click.ClickException(
            "session needs tmux installed and on PATH (no fallback — it is the "
            "tmux feature)."
        )


def _require_tty() -> None:
    """Gate the attaching subcommands (start/attach) on stdout being a TTY — tmux
    attach needs the foreground."""
    if not _stdout_isatty():
        raise click.ClickException(
            "session needs an interactive terminal (stdout is not a TTY)."
        )


@cli.group("session")
def session():
    """Durable interactive agent sessions in tmux.

    Run an agent's INTERACTIVE CLI (claude/codex/kimi) in a dedicated tmux session
    that survives detach (Ctrl-b d) and reattaches by name — drive one agent
    yourself (no programmatic billing) while `syncmill run --exclude <agent>`
    orchestrates the rest, the two collaborating via the `shared` memory namespace.
    Every subcommand REQUIRES tmux (it IS the tmux feature; no fallback).
    """


@session.command("start")
@click.argument("agent", required=False, default=None)
@click.option(
    "--repo",
    "-r",
    default=None,
    help="Repository root path for the session's working directory (overrides "
    "repo_root from syncmill.toml / SYNCMILL_REPO_ROOT; default: current directory).",
)
def session_start(agent: str | None, repo: str | None):
    """Launch (or reattach to) a durable interactive session for AGENT.

    AGENT defaults to the first agent in agent_priority that has an interactive CLI.
    The agent is validated against the interactive lineup, NOT the enabled registry,
    so you can drive an agent that is disabled for orchestration. Needs a TTY + tmux.
    """
    _require_tmux()
    _require_tty()
    config = SyncmillConfig()
    if repo:  # truthy guard so `-r ""` falls back to repo_root from file/env, not cwd
        config.repo_root = repo
    agent_id = agent or default_interactive_agent(config.agent_priority)
    if agent_id is None:
        raise click.ClickException(
            "no interactive agent available — agent_priority has no agent with an "
            f"interactive CLI (known: {', '.join(interactive_agents())})."
        )
    if agent_id not in interactive_agents():
        raise click.BadParameter(
            f"unknown agent: {agent_id!r}. Valid: {', '.join(interactive_agents())}",
            param_hint="AGENT",
        )
    workdir = Path(config.repo_root).resolve()
    if not workdir.is_dir():
        # tmux -c silently falls back to $HOME for a missing dir — reject up front.
        raise click.ClickException(f"repo root is not a directory: {workdir}")
    try:
        sys.exit(start_session(agent_id, str(workdir)))
    except SessionLaunchError as exc:
        raise click.ClickException(f"session failed to start ({exc})")


@session.command("list")
def session_list():
    """List active syncmill interactive sessions (name + attached state)."""
    _require_tmux()
    sessions = list_sessions()
    if not sessions:
        click.echo("[syncmill] no active sessions")
        return
    for info in sessions:
        click.echo(f"  {info.name}  ({'attached' if info.attached else 'detached'})")


@session.command("attach")
@click.argument("target")
def session_attach(target: str):
    """Reattach to a session (TARGET = agent id or full session name)."""
    _require_tmux()
    _require_tty()
    try:
        sys.exit(attach_session(target))
    except SessionLaunchError as exc:
        raise click.ClickException(str(exc))


@session.command("stop")
@click.argument("target", required=False, default=None)
@click.option("--all", "all_", is_flag=True, help="Stop all syncmill interactive sessions.")
def session_stop(target: str | None, all_: bool):
    """Kill a session (TARGET = agent id or session name) or all with --all."""
    if all_ and target:
        raise click.UsageError("TARGET and --all are mutually exclusive")
    if not all_ and not target:
        raise click.UsageError("give a TARGET or --all")
    _require_tmux()
    killed = stop_sessions(target, all_)
    click.echo(f"[syncmill] stopped {killed} session(s)")


# ── board (Phase 1: persistent work-board) ─────────────────────────────────

#: Selectable work-item states for the CLI (the human-path / list filter).
_BOARD_STATES = [s.value for s in WorkState]
# `running` is owned by the claim lease (set only by `board run`), never by the
# human update path — offering it would mint a lease-less, undispatchable item.
_BOARD_UPDATE_STATES = [s.value for s in WorkState if s is not WorkState.RUNNING]


def _require_board(config: SyncmillConfig) -> None:
    """Gate every ``board`` subcommand on the board being enabled. Like
    ``_require_tmux``, there is NO fallback — the board is the feature — so a
    disabled board is a clean, actionable error rather than a silent no-op."""
    if not config.board.enabled:
        raise click.ClickException(
            "the work-board is disabled — enable it with SYNCMILL_BOARD__ENABLED=true "
            "(or [board] enabled=true in syncmill.toml)."
        )


def _open_board(config: SyncmillConfig):
    """Require + open the board for a non-run board command, converting a corrupt /
    unwritable board (``BoardError``) into a clean ``ClickException``."""
    _require_board(config)
    try:
        return build_board(config.board)
    except BoardError as exc:
        raise click.ClickException(str(exc))


@cli.group("board")
def board():
    """Persistent agent work-board — durable, structured records of team work.

    A work item carries a goal, task, state (planned/running/blocked/review/done),
    dependencies, evidence, and a handoff note. `board run` claims the next runnable
    item and orchestrates it through the normal pipeline; agents never touch the
    board (the Supervisor mediates). DISABLED by default — enable with
    SYNCMILL_BOARD__ENABLED=true.
    """


@board.command("add")
@click.argument("task")
@click.option("--goal", default="", help="Goal/epic this item belongs to (for grouping).")
@click.option(
    "--depends-on",
    "depends_on",
    multiple=True,
    help="Item id this item depends on (repeatable) — it won't dispatch until every "
    "dependency is done.",
)
@click.option(
    "--human-required",
    is_flag=True,
    help="Mark the item as needing a human — it is excluded from auto-dispatch.",
)
def board_add(task: str, goal: str, depends_on: tuple[str, ...], human_required: bool):
    """Add a new planned work item with TASK as its instruction."""
    config = SyncmillConfig()
    bd = _open_board(config)
    try:
        item = bd.add(
            task,
            goal=goal,
            depends_on=[d for d in depends_on if d],
            human_required=human_required,
        )
        click.secho(f"[syncmill] added item {item.id}", fg="green")
        click.echo(render_item(item))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


@board.command("list")
@click.option("--state", default=None, type=click.Choice(_BOARD_STATES), help="Filter by state.")
@click.option("--goal", default=None, help="Filter by goal.")
def board_list(state: str | None, goal: str | None):
    """List work items (optionally filtered by --state and/or --goal)."""
    config = SyncmillConfig()
    bd = _open_board(config)
    try:
        items = bd.list(state=state, goal=goal)
        click.echo(render_board(items))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


@board.command("show")
@click.argument("item_id")
def board_show(item_id: str):
    """Show a work item in full (state, evidence, handoff, and audit history)."""
    config = SyncmillConfig()
    bd = _open_board(config)
    try:
        item = bd.get(item_id)
        if item is None:
            raise click.ClickException(f"no such item: {item_id}")
        click.echo(render_item(item, bd.transitions(item_id)))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


@board.command("run")
@click.option(
    "--id",
    "item_id",
    default=None,
    help="Run a specific item id (default: the next dispatchable item).",
)
@click.option(
    "--loop",
    is_flag=True,
    help="Drain the board: keep claiming and running dispatchable items until none "
    "remain.",
)
@click.option(
    "--strategy",
    "-s",
    type=click.Choice(available_strategies()),
    default=None,
    help="Orchestration strategy for the run(s) (default: route). Same strategies as "
    "`syncmill run`.",
)
@click.option("--agent", "-a", default=None, help="Force a specific agent (no fallback).")
def board_run(item_id: str | None, loop: bool, strategy: str | None, agent: str | None):
    """Claim and run a board item through the orchestration pipeline.

    Without --id, claims the next dispatchable item (deps done, not human-required).
    --loop drains every dispatchable item. The agent's optional board update is
    parsed back and recorded; a completed run lands in `review` (or `done` only when
    independently verified), a failed run in `blocked`.
    """
    if loop and item_id:
        raise click.UsageError("--id and --loop are mutually exclusive")
    config = SyncmillConfig()
    _require_board(config)
    # Mutate default_strategy BEFORE building the Supervisor (it selects the strategy
    # in __init__) — the same pre-construction mutation `run` uses for --strategy.
    if strategy:
        config.default_strategy = strategy
    _validate_strategy(config)

    repo_path = Path(config.repo_root).resolve()
    cleanup_orphaned_worktrees(repo_path)
    supervisor = Supervisor(config)
    try:
        # Validate --agent BEFORE claiming anything: an unknown/disabled agent makes
        # the run FAIL, and run_item has already claimed the item by then — so an
        # unvalidated typo would durably claim + block a real backlog item (and under
        # --loop, block the whole backlog). A bad agent name is a caller error, not a
        # work outcome. (Plain `run` is harmless on the same typo — no durable state.)
        if agent and supervisor.registry.get(agent) is None:
            valid = ", ".join(a.agent_id for a in supervisor.registry.available())
            raise click.BadParameter(
                f"unknown or disabled agent: {agent!r}. Valid (enabled): {valid}",
                param_hint="'--agent' / '-a'",
            )
        if loop:
            ran = 0
            any_failed = False
            while True:
                result = supervisor.run_item(agent_id=agent)
                if result is None:
                    break
                ran += 1
                ok = result.status is TaskStatus.COMPLETED
                any_failed = any_failed or not ok
                click.secho(
                    f"[syncmill] ran item → {result.agent_id}: {result.status.value}",
                    fg="green" if ok else "yellow",
                )
            click.echo(f"[syncmill] board run drained {ran} item(s)")
            _surface_board_degraded(supervisor)
            if any_failed:
                # A drained-but-some-failed run is a nonzero outcome for CI/cron,
                # matching the single-run path's failure exit (the items are also
                # recorded as `blocked`, visible via `board list`).
                sys.exit(1)
        else:
            result = supervisor.run_item(item_id=item_id, agent_id=agent)
            if result is None:
                msg = (
                    f"item not claimable (missing, or not in 'planned'): {item_id}"
                    if item_id is not None
                    else "nothing to run — no dispatchable item"
                )
                click.secho(f"[syncmill] {msg}", fg="yellow")
                return
            _surface_board_degraded(supervisor)
            click.echo()
            exit_code = render_task_result(result)
            if exit_code != 0:
                sys.exit(exit_code)
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        supervisor.close()
        close_event_loop()


def _surface_board_degraded(supervisor) -> None:
    """Mirror plain `run`'s degraded notices for a board run: a board run's one
    cross-link share to global `shared` (share_board_result) is best-effort, so a
    degraded memtomem means that summary silently didn't land — surface it. The
    breaker is equally live during a board run."""
    if supervisor.memory.degraded:
        click.secho(
            f"[syncmill] memory degraded — the board-result summary may not have "
            f"reached shared ({supervisor.memory.last_error})",
            fg="yellow",
        )
    if supervisor.breaker is not None and supervisor.breaker.degraded:
        click.secho(
            f"[syncmill] circuit breaker state unavailable — not tracking failures "
            f"({supervisor.breaker.last_error})",
            fg="yellow",
        )


@board.command("update")
@click.argument("item_id")
@click.option(
    "--state",
    default=None,
    type=click.Choice(_BOARD_UPDATE_STATES),
    help="Transition the item to this state (done requires --evidence/--sign-off).",
)
@click.option(
    "--evidence",
    default=None,
    help="Attach a passing evidence ref (e.g. a CI/test URL) — counts toward the "
    "done gate.",
)
@click.option(
    "--sign-off",
    "sign_off",
    is_flag=True,
    help="Record a human sign-off (verification for --state done).",
)
@click.option("--note", default="", help="Audit note recorded with this update.")
@click.option(
    "--clear-human-required",
    "clear_human_required",
    is_flag=True,
    help="Clear the human-required flag (so the item can auto-dispatch again).",
)
def board_update(
    item_id: str,
    state: str | None,
    evidence: str | None,
    sign_off: bool,
    note: str,
    clear_human_required: bool,
):
    """Human-path update for an item: transition state, attach evidence, sign off.

    Marking an item `done` REQUIRES verification — pass --evidence <ref> or
    --sign-off; a bare `--state done` is rejected.
    """
    if state is None and not evidence and not sign_off and not clear_human_required:
        raise click.UsageError(
            "nothing to update — give --state, --evidence/--sign-off, or "
            "--clear-human-required"
        )
    config = SyncmillConfig()
    bd = _open_board(config)
    try:
        now = time.time()
        rows: list[Evidence] = []
        if evidence:
            rows.append(Evidence(kind="manual", ref=evidence, passed=True, at=now, by="human"))
        if sign_off:
            rows.append(Evidence(kind="human", ref="sign-off", passed=True, at=now, by="human"))
        # `transition` handles state, evidence, and the human-required flag in one
        # txn. With no explicit --state we pass `state=None`: transition keeps the
        # CURRENT state, resolved INSIDE its transaction (not from a stale pre-read
        # here), so a concurrent claim can't be clobbered by a stale annotate target.
        # The done gate only fires for an actual move to done.
        hr = False if clear_human_required else None
        updated = bd.transition(
            item_id, state, actor="human", note=note,
            evidence=rows or None, human_required=hr,
        )
        click.secho(f"[syncmill] updated item {item_id}", fg="green")
        click.echo(render_item(updated))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


@board.command("decide")
@click.argument("text")
@click.option(
    "--scope",
    default="",
    help="Goal this decision applies to (default: global — applies to every item). A "
    "scoped decision is injected only into items with that goal.",
)
@click.option(
    "--supersedes",
    default=None,
    help="Id of an active decision this replaces — it stops being injected (it is "
    "retired, not deleted).",
)
def board_decide(text: str, scope: str, supersedes: str | None):
    """Record a team decision that agents must NOT re-litigate.

    Active (non-superseded) decisions are injected DETERMINISTICALLY, in full, into
    every board run for the matching goal — never via fuzzy memory recall.
    """
    config = SyncmillConfig()
    bd = _open_board(config)
    try:
        decision = bd.add_decision(text, scope=scope, supersedes=supersedes)
        click.secho(f"[syncmill] recorded decision {decision.id}", fg="green")
        click.echo(render_decision(decision))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


@board.command("decisions")
@click.option(
    "--goal",
    default=None,
    help="Show decisions applying to this goal (global + goal-scoped). Default: all "
    "active decisions.",
)
def board_decisions(goal: str | None):
    """List the active (non-superseded) team decisions."""
    config = SyncmillConfig()
    bd = _open_board(config)
    try:
        click.echo(render_decisions(bd.active_decisions(goal=goal)))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


@board.command("stats")
def board_stats_cmd():
    """Show work-board team-health stats (duplicate work, stalls, false-done)."""
    config = SyncmillConfig()
    bd = _open_board(config)
    try:
        click.echo(render_board_stats(bd.stats()))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()
