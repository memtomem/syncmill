"""CLI entry point for syncmill."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from syncmill.config import SyncmillConfig
from syncmill.memory import close_event_loop, run_async
from syncmill.models import Task, TaskStatus
from syncmill.strategies import available_strategies
from syncmill.supervisor import Supervisor, _matched_order
from syncmill.worktree import cleanup_orphaned_worktrees


@click.group()
@click.version_option(package_name="syncmill")
def cli():
    """syncmill — Multi-agent orchestration with shared memory."""


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
def run(
    prompt: str,
    agent: str | None,
    exclude: tuple[str, ...],
    timeout: int | None,
    repo: str | None,
    capability: tuple[str, ...],
    strategy: str | None,
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

    # Cleanup any orphaned worktrees from crashed runs. Use the resolved
    # config.repo_root (which may come from syncmill.toml / env), not the raw
    # flag, so cleanup and the Supervisor's worktree pool target the same root.
    repo_path = Path(config.repo_root).resolve()
    cleanup_orphaned_worktrees(repo_path)

    supervisor = Supervisor(config)
    cap_list = [c for c in capability if c]  # drop empty -c "" values, like exclude_set
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

        # Display result
        click.echo()
        if result.status == TaskStatus.COMPLETED:
            click.secho(f"[{result.agent_id}] Completed in {result.duration_seconds}s", fg="green")
            if result.gate is not None:
                click.secho(
                    f"[syncmill] Quality gate: passed ({result.gate.summary()})",
                    fg="green",
                )
            if result.files_changed:
                click.echo(f"[syncmill] Files changed: {', '.join(result.files_changed)}")
            click.echo()
            click.echo(result.output)
        else:
            click.secho("[syncmill] Failed", fg="red")
            if result.error:
                click.echo(result.error)
            sys.exit(1)
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
