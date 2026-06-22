"""CLI entry point for syncmill."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from syncmill.config import SyncmillConfig
from syncmill.memory import close_event_loop, run_async
from syncmill.models import Task
from syncmill.monitor import (
    MonitorLaunchError,
    inner_dir,
    launch_monitored_run,
    render_task_result,
    tmux_available,
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
    any re-launch recursion risk even if monitoring is also enabled via env."""
    argv = ["syncmill", "run", prompt]
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
    try:
        mcp = build_gateway(memory, host=host, port=port)
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
