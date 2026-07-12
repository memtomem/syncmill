"""CLI entry point for syncmill."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click
from pydantic import ValidationError

from syncmill.agents import _AGENT_CONSTRUCTORS
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
from syncmill.config import SyncmillConfig, apply_team, resolve_goal_team
from syncmill.dashboard import DEFAULT_REFRESH_SECONDS, run_dashboard
from syncmill.memory import close_event_loop, run_async
from syncmill.models import Task, TaskStatus
from syncmill.monitor import (
    MonitorLaunchError,
    _result_to_json,
    emit_patch_file,
    inner_dir,
    launch_monitored_run,
    render_task_result,
    tmux_available,
)
from syncmill.report import list_reports, load_envelope, render_report
from syncmill.review_candidates import (
    DEFAULT_GOAL as REVIEW_CANDIDATE_DEFAULT_GOAL,
    ReviewCandidateError,
    item_specs as review_item_specs,
    load_report as load_review_candidate_report,
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
from syncmill.teamfile import (
    TeamFileError,
    build_team,
    file_team_names,
    parse_capability_specs,
    render_team,
    save_team,
    set_default_team,
    toml_path,
    validate_team_save,
)
from syncmill.worktree import cleanup_orphaned_worktrees


@click.group()
@click.version_option(package_name="syncmill")
def cli():
    """syncmill — Multi-agent orchestration with shared memory."""


@cli.command("telemetry-hook", hidden=True)
@click.option("--provider", type=click.Choice(["kimi-code", "antigravity"]), required=True)
@click.option("--event", type=click.Choice(["pre", "post", "success", "failure"]), required=True)
@click.option("--spool", type=click.Path(path_type=Path), required=True)
@click.option("--tool-map", type=click.Path(path_type=Path, exists=True), default=None)
def telemetry_hook(provider: str, event: str, spool: Path, tool_map: Path | None) -> None:
    """Internal fail-open adapter for workspace-local provider hooks."""
    from syncmill.telemetry_hook import collect_hook_event

    try:
        payload = json.load(sys.stdin)
        mapping = json.loads(tool_map.read_text()) if tool_map is not None else None
        if not isinstance(payload, dict) or (mapping is not None and not isinstance(mapping, dict)):
            raise ValueError("hook input must be an object")
        collect_hook_event(
            provider=provider,
            event=event,
            spool=spool,
            payload=payload,
            tool_map=mapping,
        )
    except Exception:
        # Hooks are observability only and must never change an agent outcome.
        marker = Path(f"{spool}.incomplete")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("collector_error", encoding="utf-8")
        os.chmod(marker, 0o600)


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
    team: str | None = None,
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
    if team:
        # UNLIKE --monitor/--emit-patch, --team MUST be forwarded: it is config
        # selection (like --strategy) — omitting it would have the inner rebuild
        # a team-less config and silently run the wrong lineup.
        argv += ["--team", team]
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
    team: str | None = None,
    emit_patch: str | None = None,
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
        team=team,
    )
    try:
        # --emit-patch is deliberately NOT in inner_argv (like --monitor): the
        # OUTER writes the patch from result.json, relative to the user's cwd.
        # report_runs_dir lets the OUTER print the envelope path the INNER wrote
        # (existence-gated there) — None when reporting is off for the OUTER.
        return launch_monitored_run(
            inner_argv,
            session_prefix=config.monitor.session_prefix,
            emit_patch=emit_patch,
            report_runs_dir=(
                config.integration.report.runs_dir
                if config.integration.report.enabled
                else None
            ),
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
    arrive via ``SYNCMILL_DEFAULT_STRATEGY`` or a team profile's ``strategy``,
    which bypass that. Without this, ``Supervisor(config)`` would surface the
    typo as a raw ``ValueError`` traceback (``_make_strategy``) rather than a
    one-line message.
    """
    if config.default_strategy not in available_strategies():
        raise click.BadParameter(
            f"unknown strategy: {config.default_strategy!r}. "
            f"Valid: {', '.join(available_strategies())}",
            param_hint="'--strategy' / '-s', SYNCMILL_DEFAULT_STRATEGY, "
            "or the team profile",
        )


def _load_config() -> SyncmillConfig:
    """Build the resolved config, converting a ``pydantic.ValidationError`` into
    a one-line CLI error.

    Config-level validators (a typo'd top-level toml key, a structurally broken
    ``[teams.<name>]`` profile — empty ``agents``, a goal owned by two teams, …)
    fire at *construction*, before any per-command validation like
    ``_validate_strategy`` can run — without this wrapper they'd escape every
    command as a raw traceback (the same rationale as ``_validate_strategy``).
    """
    try:
        return SyncmillConfig()
    except ValidationError as exc:
        raise click.ClickException(f"invalid configuration:\n{exc}")


def _apply_team(config: SyncmillConfig, team_name: str) -> None:
    """Overlay the named team profile onto ``config`` with friendly CLI errors.

    Validates BEFORE mutating: an unknown team name lists the configured teams,
    and a profile naming an agent id syncmill doesn't know fails here — cleanly,
    and (on ``board run``) before any claim is taken — rather than surfacing as
    ``AgentRegistry``'s raw ``ValueError`` from ``Supervisor(config)``. Free-form
    *capability* tokens are never validated (the matcher's graceful-fallback
    contract); only agent ids are.
    """
    team = config.teams.get(team_name)
    if team is None:
        available = ", ".join(sorted(config.teams)) or "(none configured)"
        raise click.BadParameter(
            f"unknown team: {team_name!r}. Available: {available}",
            param_hint="'--team'",
        )
    unknown = [a for a in team.agents if a not in _AGENT_CONSTRUCTORS]
    if unknown:
        raise click.ClickException(
            f"team {team_name!r} names unknown agent(s): {', '.join(unknown)}. "
            f"Valid agents: {', '.join(sorted(_AGENT_CONSTRUCTORS))}"
        )
    apply_team(config, team_name)


def _apply_default_team(config: SyncmillConfig) -> str | None:
    """Overlay the persisted ``default_team`` (set by ``team use``), if any.

    Returns the applied team name, or ``None`` when no default is configured
    (an empty string counts as unset — the ``-r ""`` house rule). A dangling
    name — deliberately NOT rejected at config construction, so it can't brick
    `team use --clear` — fails HERE with the fix spelled out, rather than
    silently running the wrong lineup. A wrapper rather than an ``origin=``
    param on ``_apply_team``: the error genuinely differs (no flag was passed,
    so ``BadParameter`` pointing at ``--team`` would mislead).
    """
    name = config.default_team or None
    if name is None:
        return None
    if name not in config.teams:
        raise click.ClickException(
            f"default_team {name!r} (from syncmill.toml / SYNCMILL_DEFAULT_TEAM) "
            f"is not a configured team. Fix: `syncmill team use --clear`, or save "
            f"the profile with `syncmill team save {name} -a <agent> ...`."
        )
    _apply_team(config, name)
    return name


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
    "--team",
    default=None,
    help="Run with a named team profile from [teams.<name>] in syncmill.toml "
    "(or SYNCMILL_TEAMS) — its agent subset/order becomes the lineup, and its "
    "capabilities/strategy apply when set. An explicit --strategy still wins.",
)
@click.option(
    "--monitor",
    is_flag=True,
    help="Live tmux monitor: run a concurrent strategy (compete/council/decompose) "
    "inside a tmux session with one live pane per agent, auto-attached. No-ops with "
    "a notice for sequential strategies, a non-TTY stdout, or a missing tmux.",
)
@click.option(
    "--emit-patch",
    "emit_patch",
    default=None,
    type=click.Path(dir_okay=False, writable=True),
    help="Write the winner's full diff (untracked files included) to this file "
    "before its worktree is torn down — syncmill never applies changes to your "
    "tree itself; review the patch and apply it with `git apply <file>`.",
)
def run(
    prompt: str,
    agent: str | None,
    exclude: tuple[str, ...],
    timeout: int | None,
    repo: str | None,
    capability: tuple[str, ...],
    strategy: str | None,
    team: str | None,
    monitor: bool,
    emit_patch: str | None,
):
    """Execute a task through the agent orchestration pipeline."""
    # Build from env + syncmill.toml + defaults, then layer the explicit CLI
    # flags on top (each only when actually passed) — so `--repo` left unset lets
    # repo_root come from the file/env instead of clobbering it with the default.
    config = _load_config()
    if repo:  # ignore an empty `-r ""` like exclude/capability do, so it falls
        config.repo_root = repo  # back to repo_root from the file/env, not cwd
    team = team or None  # ignore an empty `--team ""` the same way
    default_applied = False
    if team is None:
        # No explicit --team → fall back to the persisted default_team (set by
        # `team use`). Resolving it into the SAME local `team` variable BEFORE
        # _launch_monitor_if_requested means a monitored run forwards it as an
        # explicit --team in the inner argv — deterministic, no _inner_argv change.
        team = _apply_default_team(config)
        default_applied = team is not None
    else:
        # The profile overlays agent_priority/capabilities/default_strategy, so
        # everything below (--strategy, --timeout, -x validation, the registry/
        # matcher/strategy built in Supervisor.__init__) sees the TEAM lineup.
        _apply_team(config, team)
    if strategy:
        config.default_strategy = strategy  # explicit flag beats the profile
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
        team=team,
        emit_patch=emit_patch,
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
            if team:
                suffix += f" — team: {team} (default)" if default_applied else f" — team: {team}"
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

        # Surface advisory-preflight warnings the same way — advisory means the
        # run went ahead regardless (unmapped identities, policy rejections, a
        # failed/timed-out toolgraph probe).
        for line in supervisor.preflight_warnings:
            click.secho(f"[syncmill] preflight: {line}", fg="yellow")

        # Display result via the shared renderer — byte-identical to a monitored
        # run's result.json render, so --monitor changes nothing the user sees.
        click.echo()
        exit_code = render_task_result(result)
        if emit_patch is not None and not emit_patch_file(
            emit_patch, _result_to_json(result)
        ):
            exit_code = exit_code or 1  # explicit export failed — never exit 0
        # Same line/position as the monitored OUTER prints after render_result —
        # the byte-parity invariant. None when reporting is off or the write
        # degraded (best-effort — never affects the exit code).
        if supervisor.last_report_path is not None:
            click.echo(f"[syncmill] report: {supervisor.last_report_path}")
        if exit_code != 0:
            sys.exit(exit_code)
    finally:
        supervisor.close()
        close_event_loop()


@cli.command()
def agents():
    """Show registered agents and their status."""
    config = _load_config()
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


@cli.group("team")
def team_group():
    """Named team profiles — a savable agent subset + capability/strategy bundle.

    Declared as [teams.<name>] tables in syncmill.toml (or SYNCMILL_TEAMS JSON);
    select one per run with `run --team` / `board run --team`, automatically via
    a goal the profile declares (`board run --goal`), or persistently via
    `team use` (the default_team key). `team save` / `team use` write the
    project syncmill.toml comment-preservingly (tomlkit round-trip); `list` /
    `show` stay read-only.
    """


@team_group.command("list")
def team_list():
    """List configured team profiles."""
    config = _load_config()
    if not config.teams:
        click.echo(
            "[syncmill] no teams configured — add [teams.<name>] tables to "
            "syncmill.toml (or SYNCMILL_TEAMS)."
        )
        return
    click.echo("Configured teams:\n")
    for name in sorted(config.teams):
        team = config.teams[name]
        agents_shown = " → ".join(team.agents)
        strategy = team.strategy or f"(default: {config.default_strategy})"
        goals = ", ".join(team.goals) if team.goals else "—"
        caps = f"{len(team.capabilities)} agent(s)" if team.capabilities else "—"
        click.secho(f"  {name}", bold=True, nl=False)
        click.echo(
            f"  agents: {agents_shown}  strategy: {strategy}  goals: {goals}"
            f"  capabilities: {caps}"
        )


@team_group.command("show")
@click.argument("name")
def team_show(name: str):
    """Show one team profile in full (agents with status, strategy, goals)."""
    config = _load_config()
    team = config.teams.get(name)
    if team is None:
        available = ", ".join(sorted(config.teams)) or "(none configured)"
        raise click.BadParameter(
            f"unknown team: {name!r}. Available: {available}", param_hint="NAME"
        )
    # The shared renderer — byte-identical to the dashboard TEAM pane's detail
    # (the render_item/render_board_stats precedent).
    click.echo(render_team(name, team, config))


@team_group.command("save")
@click.argument("name")
@click.option(
    "--agent",
    "-a",
    "agents",
    multiple=True,
    required=True,
    help="Agent id, in team priority order (repeatable, at least one).",
)
@click.option(
    "--strategy",
    "-s",
    type=click.Choice(available_strategies()),
    default=None,
    help="Team default strategy; omit to inherit default_strategy.",
)
@click.option(
    "--goal",
    "-g",
    "goals",
    multiple=True,
    help="Board goal this team owns (repeatable; a goal may belong to at most "
    "one team).",
)
@click.option(
    "--capability",
    "-c",
    "capabilities",
    multiple=True,
    help="Team-scoped capability declaration, 'agent=tok1,tok2' (repeatable; "
    "replaces [capabilities.agents] when the team is selected).",
)
def team_save(
    name: str,
    agents: tuple[str, ...],
    strategy: str | None,
    goals: tuple[str, ...],
    capabilities: tuple[str, ...],
):
    """Create/update [teams.NAME] in syncmill.toml (comment-preserving).

    The flags define the FULL profile — re-saving an existing team replaces its
    table wholesale (so omitted goals/capabilities are cleared), while every
    other line of the file survives byte-for-byte.
    """
    config = _load_config()
    try:
        caps = parse_capability_specs(capabilities)
    except TeamFileError as exc:
        raise click.BadParameter(str(exc), param_hint="'--capability' / '-c'")
    try:
        team = build_team(agents=agents, strategy=strategy, goals=goals, capabilities=caps)
    except TeamFileError as exc:
        raise click.BadParameter(str(exc), param_hint="'--agent' / '-a' / '--goal' / '-g'")
    path = toml_path()
    try:
        # Both views: the resolved config (next _load_config must not die) AND
        # the file standing alone (an env override must not mask a dup-goal
        # that outlives it) — the _apply_team BadParameter/ClickException split:
        # flag-shaped input errors above, config/write failures from here on.
        validate_team_save(config, name, team, path=path)
        save_team(path, name, team)
    except TeamFileError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"[syncmill] saved team '{name}' to {path}")
    if "SYNCMILL_TEAMS" in os.environ:
        click.secho(
            "[syncmill] note: SYNCMILL_TEAMS is set and deep-merges over the "
            "file — the resolved profile may differ from what was just saved.",
            fg="yellow",
        )


@team_group.command("use")
@click.argument("name", required=False)
@click.option(
    "--clear",
    is_flag=True,
    help="Remove the persisted default_team key instead of setting it.",
)
def team_use(name: str | None, clear: bool):
    """Persist NAME as default_team in syncmill.toml (or --clear to unset).

    `run` / `board run` auto-apply the default when no explicit --team is
    passed (precedence: --team > goal→team mapping > default_team).
    """
    if clear == (name is not None):
        raise click.UsageError("pass exactly one of NAME or --clear")
    path = toml_path()
    if clear:
        # Deliberately no _load_config(): clearing is the FIX for a broken
        # default and must work even when the resolved config is unloadable.
        try:
            set_default_team(path, None)
        except TeamFileError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"[syncmill] cleared default_team in {path}")
        return
    config = _load_config()
    if name not in config.teams:
        available = ", ".join(sorted(config.teams)) or "(none configured)"
        raise click.BadParameter(
            f"unknown team: {name!r}. Available: {available}", param_hint="NAME"
        )
    try:
        file_teams = file_team_names(path)
        set_default_team(path, name)
    except TeamFileError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"[syncmill] default_team = '{name}' saved to {path}")
    if name not in file_teams:
        # codex design-review Major: the team resolves via SYNCMILL_TEAMS only —
        # the persisted default dangles as soon as that env is absent.
        click.secho(
            f"[syncmill] note: team '{name}' is not defined in {path} (it "
            f"resolves via SYNCMILL_TEAMS) — without that env the default "
            f"dangles; `syncmill team save {name} ...` makes it durable.",
            fg="yellow",
        )
    if "SYNCMILL_DEFAULT_TEAM" in os.environ:
        click.secho(
            "[syncmill] note: SYNCMILL_DEFAULT_TEAM is set and overrides the "
            "file — the persisted default takes effect once it is unset.",
            fg="yellow",
        )


@cli.group("report")
def report_group():
    """Run-envelope reports — one join artifact per run under [integration.report]
    runs_dir (default ~/.syncmill/runs/{run_id}/envelope.json).

    Writing is opt-in (SYNCMILL_INTEGRATION__REPORT__ENABLED=true); `list` /
    `show` are read-only viewers over whatever exists, so they work either way
    (the `team list`/`show` posture — refusing to show existing reports because
    writing is off would be hostile).
    """


@report_group.command("list")
def report_list():
    """List recorded run envelopes, newest first."""
    config = _load_config()
    docs = list_reports(config.integration.report.runs_dir)
    if not docs:
        click.echo(
            "[syncmill] no run reports found — enable recording with "
            "SYNCMILL_INTEGRATION__REPORT__ENABLED=true (or [integration.report] "
            "enabled=true in syncmill.toml)."
        )
        return
    click.echo("Recorded runs (newest first):\n")
    for doc in docs:
        result = doc.get("result") or {}
        click.echo(
            f"  {doc.get('run_id')}  {doc.get('created_at')}  "
            f"{doc.get('strategy')}  {result.get('status')}"
            f"  {result.get('agent_id') or '—'}"
        )


@report_group.command("show")
@click.argument("run_id")
def report_show(run_id: str):
    """Show one run's envelope in full."""
    config = _load_config()
    doc = load_envelope(config.integration.report.runs_dir, run_id)
    if doc is None:
        raise click.ClickException(
            f"no report found for run {run_id!r} under "
            f"{config.integration.report.runs_dir}"
        )
    click.echo(render_report(doc))


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
    """Run the read-only MCP gateway over shared memory and the work-board.

    Exposes six read-only tools — shared_search/shared_read/shared_stats over shared
    knowledge, plus board_list/board_get/board_stats over the work-board (when
    enabled) — to external MCP clients (Claude Code/Desktop, Cursor, …) so they can
    READ syncmill's shared knowledge and board state. This is a one-way,
    consumer-facing surface: syncmill agents never touch MCP and the Supervisor
    remains the sole writer (there is no write tool for memory or the board).

    Default transport is stdio (the process speaks MCP on stdin/stdout); pass
    --http for streamable-http at http://HOST:PORT/mcp.
    """
    # Lazy imports (like `status`) so importing the CLI doesn't pull FastMCP.
    from syncmill.gateway import build_gateway
    from syncmill.memory import SyncmillMemory

    config = _load_config()
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
    """Show the configured memory backend's connection status."""
    config = _load_config()
    from syncmill.memory import SyncmillMemory

    memory = SyncmillMemory(config.memory)
    try:
        info = run_async(memory.status())

        backend = config.memory.backend
        if info.get("connected"):
            click.secho(f"{backend}: connected", fg="green")
            detail_keys = (
                ("db_path",)
                if backend == "memtomem"
                else ("data_dir", "vector_store", "collection", "embedder")
            )
            for key in detail_keys:
                if key in info:
                    click.echo(f"  {key}: {info[key]}")
        else:
            click.secho(f"{backend}: disconnected", fg="red")
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
    config = _load_config()
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
    config = _load_config()
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


@board.command("import-review-candidates")
@click.argument(
    "input_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False, readable=True),
)
@click.option(
    "--goal",
    default=REVIEW_CANDIDATE_DEFAULT_GOAL,
    show_default=True,
    help="Goal assigned when a candidate is first imported.",
)
def board_import_review_candidates(input_path: Path, goal: str):
    """Import a Tracegraph v1 report as human-only review items."""
    config = _load_config()
    _require_board(config)
    try:
        report = load_review_candidate_report(input_path)
    except ReviewCandidateError as exc:
        raise click.ClickException(str(exc))

    bd = _open_board(config)
    try:
        result = bd.import_review_candidates(review_item_specs(report), goal=goal)
        click.secho(
            f"[syncmill] imported {result.imported} review candidate(s); "
            f"skipped {result.skipped} existing",
            fg="green",
        )
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


@board.command("list")
@click.option("--state", default=None, type=click.Choice(_BOARD_STATES), help="Filter by state.")
@click.option("--goal", default=None, help="Filter by goal.")
def board_list(state: str | None, goal: str | None):
    """List work items (optionally filtered by --state and/or --goal)."""
    config = _load_config()
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
    config = _load_config()
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
@click.option(
    "--team",
    default=None,
    help="Run with a named team profile from [teams.<name>] (see `syncmill run "
    "--team`). Beats any goal→team mapping.",
)
@click.option(
    "--goal",
    default=None,
    help="Scope the claim (and --loop) to items with exactly this goal; if a team "
    "profile declares the goal, that team is applied automatically.",
)
def board_run(
    item_id: str | None,
    loop: bool,
    strategy: str | None,
    agent: str | None,
    team: str | None,
    goal: str | None,
):
    """Claim and run a board item through the orchestration pipeline.

    Without --id, claims the next dispatchable item (deps done, not human-required).
    --loop drains every dispatchable item. The agent's optional board update is
    parsed back and recorded; a completed run lands in `review` (or `done` only when
    independently verified), a failed run in `blocked`.
    """
    if loop and item_id:
        raise click.UsageError("--id and --loop are mutually exclusive")
    if item_id and goal:
        raise click.UsageError("--id and --goal are mutually exclusive")
    goal = goal or None  # ignore an empty `--goal ""`, like -x ""/-r ""
    team = team or None
    config = _load_config()
    _require_board(config)
    # Resolve the team BEFORE building the Supervisor — one Supervisor (one lineup)
    # per invocation. Precedence: explicit --team > the goal→team mapping > none.
    via_goal: str | None = None
    if team is None and goal:
        team = resolve_goal_team(config, goal)
        via_goal = goal if team else None
    if team is None and item_id:
        # --id alone: the goal→team mapping is config declaring "this team owns
        # this goal", so the targeted manual-dispatch path honors it too (else
        # team assignment would be path-dependent). A read-only peek — no claim
        # taken; a missing item resolves no team and surfaces later as the
        # existing "not claimable" message.
        bd = _open_board(config)
        try:
            peeked = bd.get(item_id)
        finally:
            bd.close()
        if peeked is not None and peeked.goal:
            team = resolve_goal_team(config, peeked.goal)
            via_goal = peeked.goal if team else None
    via_default = False
    if team is None:
        # Last in the precedence chain: --team > --goal mapping > --id peek >
        # default_team (`team use`) > none.
        team = _apply_default_team(config)
        via_default = team is not None
    elif team:
        _apply_team(config, team)
    if team:
        via = f" (via goal {via_goal!r})" if via_goal else (" (default)" if via_default else "")
        click.echo(f"[syncmill] team: {team}{via}")
    # Mutate default_strategy BEFORE building the Supervisor (it selects the strategy
    # in __init__) — the same pre-construction mutation `run` uses for --strategy;
    # the explicit flag beats the team profile's strategy.
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
        goal_suffix = f" for goal {goal!r}" if goal else ""
        if loop:
            ran = 0
            any_failed = False
            while True:
                result = supervisor.run_item(agent_id=agent, goal=goal)
                if result is None:
                    break
                ran += 1
                ok = result.status is TaskStatus.COMPLETED
                any_failed = any_failed or not ok
                click.secho(
                    f"[syncmill] ran item → {result.agent_id}: {result.status.value}",
                    fg="green" if ok else "yellow",
                )
                if getattr(supervisor, "last_preflight_blocked", False):
                    click.secho(
                        "[syncmill] board loop stopped after strict preflight block",
                        fg="yellow",
                    )
                    break
            click.echo(f"[syncmill] board run drained {ran} item(s){goal_suffix}")
            _surface_board_degraded(supervisor)
            if any_failed:
                # A drained-but-some-failed run is a nonzero outcome for CI/cron,
                # matching the single-run path's failure exit (the items are also
                # recorded as `blocked`, visible via `board list`).
                sys.exit(1)
        else:
            result = supervisor.run_item(item_id=item_id, agent_id=agent, goal=goal)
            if result is None:
                msg = (
                    f"item not claimable (missing, or not in 'planned'): {item_id}"
                    if item_id is not None
                    else f"nothing to run — no dispatchable item{goal_suffix}"
                )
                click.secho(f"[syncmill] {msg}", fg="yellow")
                return
            _surface_board_degraded(supervisor)
            click.echo()
            exit_code = render_task_result(result)
            # The same report line plain `run` prints (the board block is already
            # stamped by run_item); the --loop path deliberately doesn't — many
            # runs, no single per-run render to anchor it to.
            if supervisor.last_report_path is not None:
                click.echo(f"[syncmill] report: {supervisor.last_report_path}")
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
    config = _load_config()
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
    config = _load_config()
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
    config = _load_config()
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
    config = _load_config()
    bd = _open_board(config)
    try:
        click.echo(render_board_stats(bd.stats()))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


# ── dashboard (Phase 3: live TUI over the work-board) ──────────────────────


@cli.command("dashboard")
@click.option(
    "--refresh",
    default=DEFAULT_REFRESH_SECONDS,
    type=float,
    show_default=True,
    help="Poll interval in seconds for board reads (min 0.5).",
)
def dashboard(refresh: float):
    """Live TUI over the work-board — 보드 관제.

    A polling item table + detail pane + stats overlay, with human-path edits and
    read-only team/report panes bound to keys. A separate observer/editor process:
    it touches the board, team config, and recorded run envelopes — never memory,
    agents, or the MCP gateway. Requires the board enabled and an interactive
    terminal (no fallback — the TUI IS the feature, like `session`).
    """
    config = _load_config()
    _require_board(config)
    # TTY before opening the db — the cheap gate first, and a non-interactive
    # launch shouldn't create/touch board.db at all.
    if not _stdout_isatty():
        raise click.ClickException(
            "dashboard needs an interactive terminal (stdout is not a TTY)."
        )
    try:
        bd = build_board(config.board)
    except BoardError as exc:
        raise click.ClickException(str(exc))
    try:
        # Config feeds the TEAM + REPORT panes; the ONCE-resolved toml path is the
        # TEAM write target, immune to an in-app cwd drift.
        run_dashboard(bd, refresh_seconds=refresh, config=config, toml_path=toml_path())
    finally:
        bd.close()
