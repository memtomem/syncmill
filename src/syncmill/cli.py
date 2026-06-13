"""CLI entry point for syncmill."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from syncmill.config import SyncmillConfig
from syncmill.memory import close_event_loop, run_async
from syncmill.models import Task, TaskStatus
from syncmill.supervisor import Supervisor
from syncmill.worktree import cleanup_orphaned_worktrees


@click.group()
@click.version_option(package_name="syncmill")
def cli():
    """syncmill — Multi-agent orchestration with shared memory."""


@cli.command()
@click.argument("prompt")
@click.option("--agent", "-a", default=None, help="Force a specific agent (no fallback)")
@click.option("--timeout", "-t", default=None, type=int, help="Override timeout in seconds")
@click.option("--repo", "-r", default=".", help="Repository root path")
def run(prompt: str, agent: str | None, timeout: int | None, repo: str):
    """Execute a task through the agent orchestration pipeline."""
    config = SyncmillConfig(repo_root=repo)
    if timeout:
        config.claude.timeout_seconds = timeout
        config.gemini.timeout_seconds = timeout
        config.codex.timeout_seconds = timeout

    # Cleanup any orphaned worktrees from crashed runs
    repo_path = Path(repo).resolve()
    cleanup_orphaned_worktrees(repo_path)

    supervisor = Supervisor(config)
    task = Task(prompt=prompt)

    try:
        click.echo(f"[syncmill] Executing task {str(task.id)[:8]}...")
        if agent:
            click.echo(f"[syncmill] Agent: {agent} (forced, no fallback)")
        else:
            agents = supervisor.registry.available()
            names = [a.agent_id for a in agents]
            click.echo(f"[syncmill] Agents: {' → '.join(names)} (priority fallback)")

        result = supervisor.run(task, agent_id=agent)

        # Surface degraded memory without failing the task
        if supervisor.memory.degraded:
            click.secho(
                f"[syncmill] memory unavailable — continuing without shared context "
                f"({supervisor.memory.last_error})",
                fg="yellow",
            )

        # Display result
        click.echo()
        if result.status == TaskStatus.COMPLETED:
            click.secho(f"[{result.agent_id}] Completed in {result.duration_seconds}s", fg="green")
            if result.files_changed:
                click.echo(f"[syncmill] Files changed: {', '.join(result.files_changed)}")
            click.echo()
            click.echo(result.output)
        else:
            click.secho(f"[syncmill] Failed", fg="red")
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
