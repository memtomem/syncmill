"""CLI entry point for syncmill."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import click
from pydantic import ValidationError

from syncmill import __version__
from syncmill.platform_support import UnsupportedPlatformError, require_supported_platform
from syncmill.agents import _AGENT_CONSTRUCTORS, AgentRegistry
from syncmill.command_errors import command_error
from syncmill.artifacts import (
    ApplyReconciliation,
    ArtifactError,
    ArtifactRecoveryRequired,
    ArtifactStore,
    SkippedRun,
    is_room_bound,
    printable_name,
    printable_text,
    terminal_json,
    workflow_approved,
    workflow_origin_id,
)
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
from syncmill.git_env import isolated_git_env
from syncmill.memory import close_event_loop, run_async
from syncmill.model_routing import (
    CORRUPT_ROUTING_ERROR,
    AllUnmappedError,
    ClassifierError,
    build_decision,
    render_banner,
)
from syncmill.matcher import build_matcher
from syncmill.models import Task, TaskStatus
from syncmill.isolated_session import IsolatedSessionError, IsolatedSessionStore
from syncmill.planning import build_run_plan
from syncmill.monitor import (
    MonitorLaunchError,
    _result_to_json,
    emit_patch_file,
    inner_dir,
    launch_monitored_run,
    render_task_result,
    tmux_available,
)
from syncmill.project import (
    ProjectDiscoveryError,
    ProjectError,
    configure_project_memory,
    git_root,
    initialize_project,
    load_project_config,
    project_id,
)
from syncmill.report import list_reports, load_envelope, render_report
from syncmill.review_candidates import (
    DEFAULT_GOAL as REVIEW_CANDIDATE_DEFAULT_GOAL,
    ReviewCandidateError,
    item_specs as review_item_specs,
    load_report as load_review_candidate_report,
)
from syncmill.room_viewer import (
    RoomViewerError,
    attach_viewer,
    ensure_viewer,
    in_tmux,
    sweep_orphan_viewers,
)
from syncmill.room_compare import (
    _GATE_TEXT,
    UnknownRoundError,
    _gate_verdict,
    build_room_comparison,
    comparison_to_dict,
    render_review_markdown,
    render_room_comparison,
)
from syncmill.room_raw import (
    DEFAULT_MAX_BYTES,
    WARNING as RAW_WARNING,
    collect_raw,
    json_section,
    parse_include,
    preview_token,
    render_markdown_section,
    render_preview,
    scope_of,
)
from syncmill.rooms import RoomCoordinator, RoomError, launch_room_worker
from syncmill.tui_jobs import (
    ArtifactApplySpec,
    JobError,
    JobStore,
    RoomApplySpec,
    WorkflowRunSpec,
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
    config_write_lock,
    file_team_names,
    parse_capability_specs,
    render_team,
    save_team,
    set_default_team,
    toml_path,
    validate_team_save,
)
from syncmill.terminal import (
    echo,
    echo_document,
    echo_patch,
    install_console_guards,
    printable_command,
    secho,
)
from syncmill.worktree import (
    WorkspaceError,
    capture_repository_snapshot,
    cleanup_orphaned_worktrees,
    discard_retained_workspace,
    list_retained_workspaces,
    repository_fingerprint,
    sweep_orphaned_workspaces,
)
from syncmill.workflow import (
    WORKFLOW_DOCUMENT_MAX_BYTES,
    WorkflowError,
    WorkflowRecord,
    WorkflowStore,
    canonical_workflow_json,
    planning_calls,
    read_workflow_document,
    render_workflow,
    validate_workflow_policy,
    stored_workflow_approval,
)


def _shown(value: object) -> str:
    """A value that fills part of one output line: an id, state, name or path.
    ``printable_name`` also escapes LF, so the value cannot start a line of its
    own (#198); multi-line bodies use ``printable_text`` instead."""
    return printable_name(str(value))


class _MultilineError(click.ClickException):
    """An error whose line breaks are syncmill's own: each line is escaped on
    its own, so a value inside one cannot start another."""

    def __init__(self, *lines: str):
        super().__init__("\n".join(printable_name(line) for line in lines))


def _described(head: str, exc: BaseException) -> str:
    """``head`` and the error behind it. A pydantic report goes on the lines
    below (the error boundary re-renders it); anything else stays on one line."""
    if isinstance(exc, ValidationError):
        return f"{head}:\n{exc}"
    return f"{head}: {exc}"


# Click 8.2+ reports a command run without its required arguments by raising
# this error with the generated help as its message; 8.1 prints the help itself.
_NO_ARGS_HELP = getattr(click.exceptions, "NoArgsIsHelpError", ())

# How many causes and contexts the error boundary follows before it gives up
# looking for a multi-line body and prints the message on one line.
_CHAIN_LIMIT = 32


def _error_chain(exc: BaseException) -> list[BaseException] | None:
    """``exc`` and every exception behind it, through both ``__cause__`` and
    ``__context__``; ``None`` when there are more than ``_CHAIN_LIMIT``."""
    seen: set[int] = set()
    found: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if len(seen) > _CHAIN_LIMIT:
            return None
        found.append(current)
        pending.extend((current.__context__, current.__cause__))
    return found


def _validation_lines(error: ValidationError) -> list[str]:
    """A pydantic report, rebuilt line by line. Its own text keeps an LF from a
    dict key (in the location) or from a validator's message as is."""
    count = error.error_count()
    plural = "" if count == 1 else "s"
    lines = [f"{count} validation error{plural} for {printable_name(error.title)}"]
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"])
        if location:
            lines.append(printable_name(location))
        lines.append("  " + printable_name(f"{item['msg']} [type={item['type']}]"))
    return lines


def _output_lines(output: str) -> list[str]:
    """A command's output as a labelled block: every line carries the same
    prefix, so a line inside it never looks like one of syncmill's own."""
    return ["  | " + printable_name(line) for line in output.split("\n")]


def _error_text(exc: click.ClickException) -> str:
    """The message of ``exc`` as the terminal shows it (#198).

    One line, unless syncmill built the lines itself: a ``_MultilineError``;
    click's generated help for a command run without arguments; or a message
    that ends with a pydantic report or a command's output found behind it,
    which keeps its head on one line and gets the body re-rendered below. The
    body must match exactly, so a reworded message falls back to one line."""
    message = exc.message
    if isinstance(exc, _MultilineError):
        return message
    if isinstance(exc, _NO_ARGS_HELP):
        return printable_text(message)
    for cause in _error_chain(exc) or ():
        output = getattr(cause, "command_output", None)
        if isinstance(cause, ValidationError):
            body, lines = str(cause), _validation_lines(cause)
        elif isinstance(output, str) and output:
            body, lines = output, _output_lines(output)
        else:
            continue
        if not message.endswith(body):
            continue
        head = message[: len(message) - len(body)].removesuffix("\n")
        return "\n".join(([printable_name(head)] if head else []) + lines)
    return printable_name(message)


@contextlib.contextmanager
def _escaped_errors():
    try:
        yield
    except click.ClickException as exc:
        if not getattr(exc, "_syncmill_shown", False):
            exc.message = _error_text(exc)
            exc._syncmill_shown = True  # type: ignore[attr-defined]
        raise


class _EscapingContext(click.Context):
    """The root context. Click's ``main`` closes it after ``invoke`` returns,
    running close callbacks there, so their errors are escaped here. Both
    entry points: ``__exit__`` does not go through ``close``."""

    def __exit__(self, exc_type, exc_value, tb):
        with _escaped_errors():
            return super().__exit__(exc_type, exc_value, tb)

    def close(self) -> None:
        with _escaped_errors():
            super().close()


class _EscapingGroup(click.Group):
    """The root group: every error message leaves through ``_error_text``.

    Error messages carry ids, paths and exception text that can come from a
    store, an agent or a provider (``unknown or corrupt run: <id>``). Click's
    ``main`` reports a ClickException raised by either of these two methods:
    ``make_context`` parses the root options, ``invoke`` runs every subcommand
    (including parsing its own options). Escaping both, and the root context's
    teardown, covers every message at once.

    The program name (``info_name``, from ``sys.argv[0]``) is escaped here too:
    click prints it, outside any error message, in every ``Usage:`` line and in
    ``--version``, and an executable or symlink can be named with any bytes."""

    context_class = _EscapingContext

    def make_context(self, info_name, args, parent=None, **extra):
        if info_name is not None:
            info_name = printable_name(info_name)
        with _escaped_errors():
            return super().make_context(info_name, args, parent=parent, **extra)

    def invoke(self, ctx: click.Context):
        with _escaped_errors():
            return super().invoke(ctx)


@click.group(cls=_EscapingGroup)
@click.version_option(version=__version__, package_name="syncmill")
def cli():
    """syncmill — Multi-agent orchestration with shared memory."""
    install_console_guards()
    try:
        require_supported_platform()
    except UnsupportedPlatformError as exc:
        raise click.ClickException(str(exc)) from exc


# One string for every project command's --repo, because the flag's meaning is
# easy to misread as "the repository to act on" — and on a mutating command
# (apply, discard, room apply) that misreading picks the wrong tree. It selects
# the project whose CONFIG is read; that config's repo_root is the tree touched.
_PROJECT_REPO_HELP = (
    "Project configuration root (default: current directory). The operation "
    "targets that project's resolved repo_root, which may be another checkout."
)

# The hidden workers are spawned by syncmill itself with `--resolved-config`, so
# their --repo is the ALREADY-resolved operational repository — the opposite of
# what the project-selector wording says. Internal, never typed by a user.
_WORKER_REPO_HELP = "Internal: the resolved operational repository."


def _target_project_config(
    repo: Path, *, repo_root_override: str | None = None
) -> tuple[Path, SyncmillConfig]:
    """Load a target project's config, or fail with a one-line error.

    Every command that resolves a project from ``--repo`` loads the target's
    ``syncmill.toml`` through here (the one documented exception is the legacy
    ``session start``, which only takes ``--repo`` as a working directory and
    never reads the target's config), so "an invalid target configuration is
    reported, never a silent fallback" is one promise with one implementation
    instead of a per-command ``except`` tuple that drifts.

    Two failure classes, deliberately worded differently. DISCOVERY — the path
    isn't a Git repository at all — is not a configuration problem, so its
    ``ProjectError`` is reported verbatim. Everything raised while READING the
    project is framed as an invalid target configuration: ``ValidationError`` (a
    bad value), a ``ProjectError`` from the config's own paths (a ``runs.root``
    that isn't Git-ignored), ``ValueError`` — the load-bearing one, since
    ``tomllib.TOMLDecodeError`` is a ``ValueError`` raised by the settings source
    and a narrower catch let a half-edited toml escape as a traceback — plus
    ``OSError`` (unreadable file) and ``RuntimeError`` (dead HOME).

    ``--repo`` means different things to different commands, deliberately. To
    ``run`` it names the repository to WORK ON, so it passes an override and beats
    an ambient ``SYNCMILL_REPO_ROOT``. To the project commands it names WHICH
    PROJECT's configuration to load; that config's ``repo_root`` then resolves
    normally, which is what lets owner A's project direct work at repository B
    (the relative-collaboration layout, pinned by the session/room checkpoint
    tests). Pinning ``repo_root`` to the flag here would retarget them at
    themselves and silently break that.
    """
    try:
        return load_project_config(repo, repo_root_override=repo_root_override)
    except ProjectDiscoveryError as exc:
        raise click.ClickException(str(exc)) from exc
    except (ProjectError, ValidationError, OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(
            _described("invalid target project configuration", exc)
        ) from exc


def _project_artifacts(repo: Path) -> tuple[Path, SyncmillConfig, ArtifactStore]:
    _, config = _target_project_config(repo)
    target = Path(config.repo_root).expanduser().resolve()
    try:
        store = ArtifactStore(
            Path(config.runs.root), project_id(target, config.project.id)
        )
    except ArtifactError as exc:
        raise click.ClickException(str(exc)) from exc
    return target, config, store


def _reconciliation_line(report: ApplyReconciliation) -> str:
    run = printable_name(report.run_id)
    detail = printable_name(str(report.detail))
    if report.outcome == "completed":
        receipt = printable_name(str(report.receipt))
        return f"Run {run}: completed — interrupted apply finished (receipt {receipt})"
    if report.outcome == "not_applied":
        return f"Run {run}: not applied — restored to pending"
    if report.outcome == "manual_recovery":
        return f"Run {run}: manual recovery required — {detail}"
    if report.outcome == "skipped_room_bound":
        return f"Run {run}: skipped — room-bound; use 'syncmill room recover'"
    if report.outcome in ("skipped_live_owner", "skipped_not_applying"):
        return f"Run {run}: skipped — {detail}"
    return f"Run {run}: error — {detail}"


def _echo_reconciliations(reports: list[ApplyReconciliation]) -> None:
    for report in reports:
        echo(_reconciliation_line(report))


def _run_apply(
    store: ArtifactStore,
    root: Path,
    config: SyncmillConfig,
    run_id: str,
    verify: bool,
) -> None:
    """Apply one run after self-healing a same-run interrupted apply.

    The best-effort pre-pass finishes or unwinds a crashed apply of *this* run
    before a fresh attempt: a completed strand short-circuits with a friendly
    message instead of the misleading ``run is 'applied', not pending`` error;
    a not-applied strand is restored to pending and the apply proceeds; an
    undecidable strand is frozen, and the apply then fails cleanly.
    """
    target = next(
        (
            report
            for report in store.reconcile_interrupted_applies(run_id=run_id)
            if report.run_id == run_id
        ),
        None,
    )
    if target is not None and target.outcome in (
        "completed",
        "not_applied",
        "manual_recovery",
    ):
        echo(_reconciliation_line(target))
    if target is not None and target.outcome == "completed":
        return
    try:
        manifest = store.load(run_id).manifest
    except ArtifactError:
        manifest = {}  # unknown/corrupt run: `store.apply` reports it properly
    _heal_workflow_approval(config, root, run_id, manifest, store)
    try:
        receipt = store.apply(
            run_id,
            root,
            repository_fingerprint(root),
            verify_commands=config.gate.commands if verify else None,
            verify_timeout=config.gate.timeout_seconds,
            verify_env_allowlist=config.gate.env_allowlist,
            # Verify clones land under the swept workspace root, so a SIGKILL
            # mid-verify leaves a reclaimable workspace, not tempdir residue.
            verify_workspace_root=Path(config.workspace.root).expanduser(),
        )
    except ArtifactRecoveryRequired as exc:
        # Distinct from an ordinary apply failure: the run needs an operator —
        # point at the reconciler instead of a bare error line.
        raise _MultilineError(
            str(exc),
            "To reconcile this apply, run: "
            + printable_command("syncmill", "recover", args=[run_id]),
        ) from exc
    except (ArtifactError, WorkspaceError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    echo(f"Applied run {printable_name(run_id)}")
    echo(f"Receipt: {printable_name(str(receipt))}")


def _configure_project_memory(config: SyncmillConfig, root: Path) -> None:
    """Move untouched legacy memory defaults into an initialized project
    (shared with RoomCoordinator, which binds itself in its constructor)."""
    configure_project_memory(config, root)


@cli.command("init")
@click.argument("path", default=".", type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Replace an existing syncmill.toml.")
def init_project(path: Path, force: bool) -> None:
    """Initialize a Git project with a safe, beginner-friendly baseline."""
    try:
        config_path, pid = initialize_project(path, force=force)
    except ProjectError as exc:
        raise click.ClickException(str(exc)) from exc
    echo(f"Initialized syncmill project {_shown(pid)}")
    echo(f"Config: {_shown(config_path)}")
    echo(f"Project marker: {_shown(config_path.parent / '.syncmill' / 'project.json')}")
    echo(f"Local ignore: {_shown(config_path.parent / '.syncmill' / '.gitignore')}")
    echo("These project files are not staged automatically.")
    echo("Next: syncmill doctor")


@cli.command()
@click.option(
    "--repo", "repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
@click.option(
    "--strict",
    is_flag=True,
    help="Fail when any enabled priority agent is missing or unauthenticated.",
)
def doctor(repo: Path, strict: bool) -> None:
    """Check repository readiness and configured agent executables."""
    root, config = _target_project_config(repo)
    # `--repo` selects the project whose CONFIG is read; that config's repo_root
    # names the repository actually worked on, and the two differ in the
    # relative-collaboration layout. Everything below that inspects a repository
    # — the snapshot, the dirty check, the artifact namespace — must use the
    # OPERATIONAL target, or `doctor --repo A` reports A ready while runs from A
    # would have gone to B.
    target = Path(config.repo_root).expanduser().resolve()
    fatal_problems: list[str] = []
    provider_problems: list[str] = []
    ready_providers = 0
    echo(f"Project: {_shown(root)}")
    if target != root:
        echo(f"Target repository: {_shown(target)}")
    echo(f"Config version: {config.config_version}")
    for agent_id in config.agent_priority:
        if agent_id not in _AGENT_CONSTRUCTORS:
            # AgentRegistry rejects unknown ids before running ANY agent, so a
            # mistyped id is a fatal config error — never a relaxed provider
            # warning that lets doctor report Ready for an unrunnable lineup.
            valid = ", ".join(sorted(_AGENT_CONSTRUCTORS))
            echo(f"Agent {_shown(agent_id)}: unknown id")
            fatal_problems.append(
                f"unknown agent id in agent_priority: {_shown(agent_id)} (valid: {valid})"
            )
            continue
        binary = {"kimi-code": "kimi"}.get(agent_id, agent_id)
        found = shutil.which(binary)
        label = found or "missing"
        echo(f"Agent {agent_id}: {_shown(label)}")
        if config.agent_config(agent_id).enabled and found is None:
            provider_problems.append(f"enabled agent binary not found: {binary}")
        elif config.agent_config(agent_id).enabled and agent_id in {"claude", "codex"}:
            auth_command = (
                [binary, "auth", "status", "--json"]
                if agent_id == "claude"
                else [binary, "login", "status"]
            )
            try:
                auth = subprocess.run(
                    auth_command, capture_output=True, text=True, timeout=10
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                provider_problems.append(
                    f"could not verify {agent_id} authentication: {exc}"
                )
            else:
                if auth.returncode == 0:
                    echo(f"Agent {agent_id} authentication: ready")
                    ready_providers += 1
                else:
                    provider_problems.append(f"{agent_id} is not authenticated")
        elif config.agent_config(agent_id).enabled:
            # Kimi Code does not expose a stable non-interactive auth probe.
            # Finding the executable is therefore the readiness boundary.
            ready_providers += 1
    probe_id = f"doctor-{uuid4()}"
    snapshot = None
    try:
        snapshot = capture_repository_snapshot(
            target, probe_id, Path(config.workspace.root)
        )
        echo("Repository snapshot: ready")
        echo(
            "Working tree: "
            + (
                "dirty (will be preserved)"
                if subprocess_status_dirty(target)
                else "clean"
            )
        )
    except WorkspaceError as exc:
        fatal_problems.append(str(exc))
        echo(f"Repository snapshot: blocked ({_shown(exc)})")
    finally:
        if snapshot is not None:
            shutil.rmtree(snapshot.run_root, ignore_errors=True)
    try:
        # project_id(target, …) — the same identity `_project_artifacts` and
        # `runs list` compute, so doctor reports the store those commands use.
        store = ArtifactStore(
            Path(config.runs.root), project_id(target, config.project.id)
        )
        echo(f"Run artifacts: writable ({_shown(store.root)})")
    except (OSError, ArtifactError) as exc:
        fatal_problems.append(f"run artifact store is not writable: {exc}")
    # Probe the directory a RUN would actually write to. `run` calls this same
    # helper against the configured target, so doctor must too: probing
    # `<config owner>/.syncmill/memory` reported the wrong tree's writability
    # AND created project state in a checkout no run touches.
    _configure_project_memory(config, target)
    memory_dir = (
        Path(config.memory.memory_dir).expanduser()
        if config.memory.memory_dir
        # Not an initialized project: memory stays in the ambient store beside
        # the configured db, never under the repository.
        else Path(config.memory.db_path).expanduser().parent
    )
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
        probe = memory_dir / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        echo(f"Project memory: writable ({_shown(memory_dir)})")
    except OSError as exc:
        fatal_problems.append(f"project memory is not writable: {exc}")
    if provider_problems:
        echo("Provider warnings:", err=True)
        for problem in provider_problems:
            echo(f"- {_shown(problem)}", err=True)
    if ready_providers == 0:
        fatal_problems.append("no enabled agent is ready")
    if strict:
        fatal_problems.extend(provider_problems)
    if fatal_problems:
        raise _MultilineError(
            "doctor found problems:", *(f"- {problem}" for problem in fatal_problems)
        )
    secho("Ready.", fg="green")


def subprocess_status_dirty(root: Path) -> bool:
    """Whether ``root``'s working tree has uncommitted changes.

    ``env=isolated_git_env()`` like every other git call in the package: run
    from inside a hook or a ``git rebase --exec``, an ambient GIT_DIR /
    GIT_WORK_TREE / GIT_INDEX_FILE would otherwise point this at the ambient
    repository's index and report a clean/dirty verdict for a DIFFERENT tree
    than the one named — beside a snapshot line that is correctly isolated.

    A failed git is an error, not a clean tree: swallowing the exit code
    reported an unreadable or rejected repository ("dubious ownership") as
    having nothing to preserve.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root),
        capture_output=True, text=True, env=isolated_git_env(),
    )
    if proc.returncode != 0:
        raise command_error(
            WorkspaceError,
            f"git status failed in {root} (exit {proc.returncode})",
            proc.stderr,
        )
    return bool(proc.stdout.strip())


@cli.command("strategies")
def strategies_command() -> None:
    """List orchestration strategies without starting a run."""
    descriptions = {
        "route": "try agents in priority order with fallback",
        "pipeline": "run sequential stages in one evolving workspace",
        "compete": "run isolated candidates and select a verified winner",
        "council": "propose, critique, and synthesize across agents",
        "decompose": "plan subtasks, execute them, then synthesize",
    }
    for name in available_strategies():
        echo(f"{name:10} {descriptions.get(name, '')}")


# What ``_peek_workflow_team`` returns for a workflow created with no team: the
# workflow keeps that lineup, so ``default_team`` must not be applied to it.
_PLANNED_WITHOUT_TEAM = object()


def _workflow_config(repo: str | None, team: str | object | None) -> SyncmillConfig:
    """The project config and team a workflow command resolves, with no side
    effect: no store, workspace, memory or sweep. ``workflow validate`` uses
    exactly this; every other command builds its Supervisor on top of it."""
    if repo:
        _, config = _target_project_config(Path(repo).expanduser().resolve())
    else:
        config = _load_config()
    if team is _PLANNED_WITHOUT_TEAM:
        # Created with no team: keep that lineup, so no default_team either.
        pass
    elif team:
        _apply_team(config, team)
    else:
        _apply_default_team(config)
    _validate_strategy(config)
    return config


def _workflow_supervisor(
    repo: str | None, team: str | object | None
) -> tuple[Supervisor, SyncmillConfig]:
    """Resolve the same target/team boundary for every workflow command.

    ``--repo`` here is a PROJECT SELECTOR (``_PROJECT_REPO_HELP``), not the
    operational repository: it names the project whose configuration to load,
    and that project's own ``repo_root`` decides which checkout is worked on.
    Loading through ``_load_run_config`` — and then overwriting ``repo_root``
    with the selector — would break a project configured against a sibling
    checkout: its workflows would be stored under a different project identity
    than the one ``workflow show`` and the printed ``syncmill diff --repo``
    later compute, so the artifact would land where nothing looks for it.
    """
    config = _workflow_config(repo, team)
    repo_path = Path(config.repo_root).expanduser().resolve()
    _configure_project_memory(config, repo_path)
    cleanup_orphaned_worktrees(repo_path)
    sweep_orphaned_workspaces(
        Path(config.workspace.root), older_than=config.workspace.stale_seconds
    )
    return Supervisor(config), config


def _echo_workflow_preflight(supervisor: Supervisor) -> None:
    """Print advisory preflight warnings from a workflow operation.

    A warning that nobody prints is not advisory, it is lost: unmapped
    identities and Toolgraph failures are the whole tier-1 value of running the
    probe at all. Mirrors how `run` surfaces `Supervisor.preflight_warnings`.
    """
    for warning in supervisor.workflow_preflight_warnings:
        secho(f"[syncmill] preflight: {_shown(warning)}", fg="yellow", err=True)


def _plan_workflow(
    goal: str, *, repo: str | None, team: str | None, planner: str | None
) -> None:
    supervisor, _ = _workflow_supervisor(repo, team)
    try:
        echo("[syncmill] Planning workflow (no execution)...")
        record = supervisor.plan_workflow(goal, planner_agent=planner)
        echo(render_workflow(record))
        echo()
        echo("No workflow node was executed and no source file was changed.")
        repo_args = ["--repo", repo] if repo else []
        echo(
            "Review:  "
            + printable_command(
                "syncmill", "workflow", "show", args=[record.workflow_id], options=repo_args
            )
        )
        echo(
            "Execute: "
            + printable_command(
                "syncmill", "workflow", "run", args=[record.workflow_id], options=repo_args
            )
        )
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        # In the `finally`, because a FAILED plan is when these warnings matter
        # most: a strict-preflight refusal or an unmapped identity is the whole
        # reason the probe ran, and echoing only on success drops it exactly
        # then.
        _echo_workflow_preflight(supervisor)
        supervisor.close()
        close_event_loop()


@cli.group("workflow")
def workflow_group() -> None:
    """Plan and run durable, bounded native agent workflows."""


@workflow_group.command("plan")
@click.argument("goal")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option("--team", default=None, help="Apply a configured team profile.")
@click.option(
    "--planner",
    type=click.Choice(["claude", "codex"]),
    default=None,
    help="Read-only planning agent (default: workflow.planner_agent).",
)
def workflow_plan(goal: str, repo: str | None, team: str | None, planner: str | None) -> None:
    """Create and persist a plan. This command never executes its nodes."""
    _plan_workflow(goal, repo=repo, team=team, planner=planner)


_DOCUMENT_HELP = "Plan document (schema-v1 JSON), or - for stdin."


def _read_workflow_document(document: str):
    """Read at most one byte past the limit, so an oversized document is
    refused without reading it whole, then validate it as data."""
    try:
        if document == "-":
            raw = sys.stdin.buffer.read(WORKFLOW_DOCUMENT_MAX_BYTES + 1)
        else:
            with Path(document).expanduser().open("rb") as handle:
                raw = handle.read(WORKFLOW_DOCUMENT_MAX_BYTES + 1)
    except OSError as exc:
        raise click.ClickException(
            f"cannot read workflow document {_shown(document)}: {exc.strerror or exc}"
        ) from exc
    try:
        return read_workflow_document(raw)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc


def _write_replacing(path: Path, data: bytes) -> None:
    """Write ``data`` to a new 0600 file beside ``path``, then rename it over
    ``path``: a reader sees the old file or the new one, never part of one."""
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _echo_created_workflow(record: WorkflowRecord, repo: str | None) -> None:
    echo(render_workflow(record))
    echo()
    echo("No provider was called, no workflow node was executed and no source file was changed.")
    repo_args = ["--repo", repo] if repo else []
    echo(
        "Review:  "
        + printable_command(
            "syncmill", "workflow", "show", args=[record.workflow_id], options=repo_args
        )
    )
    echo(
        "Execute: "
        + printable_command(
            "syncmill", "workflow", "run", args=[record.workflow_id], options=repo_args
        )
    )


@workflow_group.command("validate")
@click.argument("document")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option("--team", default=None, help="Apply a configured team profile.")
def workflow_validate(document: str, repo: str | None, team: str | None) -> None:
    """Check a plan document against the project's agents and limits.

    Calls no provider and writes nothing: no workflow is stored and no
    workspace is created.
    """
    spec = _read_workflow_document(document)
    config = _workflow_config(repo, team)
    try:
        validate_workflow_policy(spec, config, AgentRegistry(config), planning_calls=0)
    except (WorkflowError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    echo(f"valid workflow schema=1 digest={spec.digest()}")


@workflow_group.command("create")
@click.option("--file", "document", required=True, help=_DOCUMENT_HELP)
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option("--team", default=None, help="Apply a configured team profile.")
def workflow_create(document: str, repo: str | None, team: str | None) -> None:
    """Store a reviewed plan document as a new workflow. Never executes it."""
    spec = _read_workflow_document(document)
    supervisor, _ = _workflow_supervisor(repo, team)
    try:
        _echo_created_workflow(supervisor.create_workflow(spec), repo)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        supervisor.close()
        close_event_loop()


@workflow_group.command("revise")
@click.argument("workflow_id")
@click.option("--file", "document", required=True, help=_DOCUMENT_HELP)
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option(
    "--team",
    default=None,
    help="Apply a configured team profile (default: the revised workflow's team).",
)
def workflow_revise(
    workflow_id: str, document: str, repo: str | None, team: str | None
) -> None:
    """Store a reviewed plan document as the next version of a workflow.

    The revision is a new workflow with its own id; the one it revises is not
    modified. Never executes either.
    """
    spec = _read_workflow_document(document)
    supervisor, _ = _workflow_supervisor(
        repo, team or _peek_workflow_team(repo, workflow_id)
    )
    try:
        record = supervisor.create_workflow(spec, parent_workflow_id=workflow_id)
        _echo_created_workflow(record, repo)
        echo(f"Revises {_shown(workflow_id)}, which was not modified.")
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        supervisor.close()
        close_event_loop()


@workflow_group.command("export")
@click.argument("workflow_id")
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write to this new file (mode 0600); an existing file is refused "
    "without --force. Default: stdout.",
)
@click.option("--force", is_flag=True, help="Replace an existing --output file.")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
def workflow_export(
    workflow_id: str, output: Path | None, force: bool, repo: str | None
) -> None:
    """Print a workflow's plan as a document `create` and `revise` accept."""
    if force and output is None:
        raise click.UsageError("--force needs --output")
    supervisor, _ = _workflow_supervisor(repo, None)
    try:
        payload = canonical_workflow_json(supervisor.workflow_store.load(workflow_id).spec)
        if len(payload) > WORKFLOW_DOCUMENT_MAX_BYTES:
            raise click.ClickException(
                f"workflow {_shown(workflow_id)} is larger than a plan document "
                f"may be ({WORKFLOW_DOCUMENT_MAX_BYTES} bytes), so it cannot be exported"
            )
        if output is None:
            echo_document(payload)
        elif force:
            _write_replacing(output.expanduser(), payload)
        else:
            _write_exclusive(output.expanduser(), payload)
    except FileExistsError as exc:
        raise click.ClickException(
            f"{_shown(output)} already exists; pass --force to replace it"
        ) from exc
    except OSError as exc:
        raise click.ClickException(
            f"cannot write {_shown(output)}: {exc.strerror or exc}"
        ) from exc
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        supervisor.close()
        close_event_loop()


@workflow_group.command("lineage")
@click.argument("workflow_id")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
def workflow_lineage(workflow_id: str, repo: str | None) -> None:
    """List every surviving version of a workflow's plan, oldest first."""
    supervisor, _ = _workflow_supervisor(repo, None)
    try:
        for record in supervisor.workflow_store.lineage(workflow_id):
            marker = "*" if record.workflow_id == workflow_id else " "
            echo(
                f"{marker} v{record.plan_version}  {_shown(record.workflow_id)}  "
                f"{_shown(record.plan_origin):8}  {_shown(record.state):18} "
                f"parent={_shown(record.parent_workflow_id or '-')}  "
                f"digest={record.spec.digest()}"
            )
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        supervisor.close()
        close_event_loop()


@workflow_group.command("list")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
def workflow_list(repo: str | None) -> None:
    """List durable workflows for a project."""
    supervisor, _ = _workflow_supervisor(repo, None)
    try:
        for record in supervisor.workflow_store.list():
            goal = " ".join(record.spec.goal.splitlines())[:120]
            echo(
                f"{_shown(record.workflow_id)}  {_shown(record.state):18} "
                f"{_shown(record.created_at)}  {goal}"
            )
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        supervisor.close()
        close_event_loop()


def _observe_workflow_job(config, store: WorkflowStore, workflow_id: str) -> str | None:
    """Reconcile the latest linked job; never follow an already dead manager."""
    if os.name != "posix":
        return None
    jobs = _workflow_job_store(config, Path(config.repo_root).expanduser().resolve())
    jobs.active_jobs()  # reconcile vanished owners through the existing lock checks
    row = next((item for item in jobs.list_jobs()
                if item.get("op") == "workflow-run"
                and (item.get("request") or {}).get("workflow_id") == workflow_id), None)
    if row is None or row.get("status") not in {
        "finished", "cancelled", "force_stopped", "launch_failed", "lost"
    }:
        return None
    record = store.load(workflow_id)
    if record.job_id == row["job_id"]:
        store.reconcile_job_terminal(workflow_id, str(row["job_id"]),
                                     "failed" if row["status"] == "finished" else row["status"])
    elif record.job_id is None and record.state == "planned":
        # A configuration/startup failure happened BEFORE a workflow claim.
        # Keep the plan runnable, but do not wait forever for this dead job.
        return (
            f"workflow job {_shown(row['job_id'])} ended in {_shown(row['status'])} "
            f"before claiming the plan"
        )
    return None


def _show_workflow(
    workflow_id: str, repo: str | None, *, watch: bool = False, prompts: bool = False
) -> None:
    supervisor, config = _workflow_supervisor(repo, None)
    try:
        last_revision = -1
        while True:
            job_error = _observe_workflow_job(config, supervisor.workflow_store, workflow_id)
            supervisor.workflow_store.recover_expired(
                workflow_id, timeout_seconds=config.workflow.lease_timeout_seconds
            )
            record = supervisor.workflow_store.load(workflow_id)
            if record.revision != last_revision:
                if last_revision >= 0:
                    echo("---")
                echo(render_workflow(record, prompts=prompts))
                last_revision = record.revision
            if job_error:
                echo(f"[syncmill] {_shown(job_error)}")
            if not watch or job_error or record.state in _WORKFLOW_CLI_TERMINAL:
                return
            time.sleep(0.5)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        supervisor.close()
        close_event_loop()


@workflow_group.command("show")
@click.argument("workflow_id")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option(
    "--prompts/--no-prompts",
    default=True,
    help="Include each node's prompt and timeout (the text sent to agents).",
)
def workflow_show(workflow_id: str, repo: str | None, prompts: bool) -> None:
    """Show the validated DAG, node prompts, and current node states."""
    _show_workflow(workflow_id, repo, prompts=prompts)


@workflow_group.command("status")
@click.argument("workflow_id")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option("--watch", is_flag=True, help="Refresh until the workflow is terminal.")
def workflow_status(workflow_id: str, repo: str | None, watch: bool) -> None:
    """Show workflow state, optionally following durable revisions."""
    _show_workflow(workflow_id, repo, watch=watch)


def _peek_workflow_team(repo: str | None, workflow_id: str) -> str | object | None:
    """The team a workflow was planned under, read WITHOUT taking the claim.

    Team selection must not be path-dependent, so this is a read-only peek
    before the Supervisor is built — the ``board run --id`` recipe. Returns the
    team's name, ``_PLANNED_WITHOUT_TEAM`` when creation recorded that no team
    applied, or ``None`` for a row too old to say, which follows
    ``default_team``. A failed read is an error, not ``None``: guessing would
    run a workflow recorded without a team under the default one.
    """
    try:
        if repo:
            _, config = _target_project_config(Path(repo).expanduser().resolve())
        else:
            config = _load_config()
        target = Path(config.repo_root).expanduser().resolve()
        store = WorkflowStore(
            Path(config.runs.root), project_id(target, config.project.id)
        )
        record = store.load(workflow_id)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise click.ClickException(
            f"cannot read workflow {_shown(workflow_id)}: {_shown(exc)}"
        ) from exc
    if record.team is None and record.team_recorded:
        return _PLANNED_WITHOUT_TEAM
    return record.team


_WORKFLOW_CLI_TERMINAL = {"completed", "failed", "cancelled", "recovery_required"}


def _workflow_log_segments(store: WorkflowStore, workflow_id: str, node_id: str):
    current, previous = store.node_log_paths(workflow_id, node_id)
    return _log_segments(previous, current)


def _log_segments(previous: Path, current: Path):
    segments = []
    for path in (previous, current):
        try:
            with path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                segments.append(((stat.st_dev, stat.st_ino), handle.read()))
        except FileNotFoundError:
            pass
    return segments


def _workflow_log_bytes(store: WorkflowStore, workflow_id: str, node_id: str) -> bytes:
    return b"".join(raw for _, raw in _workflow_log_segments(store, workflow_id, node_id))


@workflow_group.command("logs")
@click.argument("workflow_id")
@click.option("--node", "node_id", default=None, help="Limit output to one node.")
@click.option("--follow", is_flag=True, help="Follow logs until terminal state.")
@click.option("--tail", type=click.IntRange(min=1), default=None, help="Show last N lines.")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
def workflow_logs(
    workflow_id: str,
    node_id: str | None,
    follow: bool,
    tail: int | None,
    repo: str | None,
) -> None:
    """Read bounded private live logs for workflow nodes."""
    supervisor, config = _workflow_supervisor(repo, None)
    try:
        record = supervisor.workflow_store.load(workflow_id)
        node_ids = [node.id for node in record.spec.nodes]
        if node_id is not None:
            if node_id not in node_ids:
                raise click.ClickException(f"unknown workflow node: {node_id!r}")
            node_ids = [node_id]
        offsets: dict[str, dict[tuple[int, int], int]] = {}
        while True:
            for item in node_ids:
                segments = _workflow_log_segments(supervisor.workflow_store, workflow_id, item)
                prior = offsets.get(item)
                if prior is None:
                    output = b"".join(raw for _, raw in segments)
                    if tail is not None:
                        output = b"".join(output.splitlines(keepends=True)[-tail:])
                else:
                    output = b"".join(raw[prior.get(identity, 0):] for identity, raw in segments)
                offsets[item] = {identity: len(raw) for identity, raw in segments}
                if output:
                    if len(node_ids) > 1:
                        echo(f"== {printable_name(item)} ==")
                    echo(
                        printable_text(output.decode("utf-8", errors="replace")),
                        nl=False,
                    )
            job_error = _observe_workflow_job(config, supervisor.workflow_store, workflow_id) if follow else None
            if follow:
                supervisor.workflow_store.recover_expired(
                    workflow_id, timeout_seconds=config.workflow.lease_timeout_seconds
                )
            record = supervisor.workflow_store.load(workflow_id)
            if job_error:
                echo(f"[syncmill] {_shown(job_error)}")
            if not follow or job_error or record.state in _WORKFLOW_CLI_TERMINAL:
                return
            time.sleep(0.25)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        _echo_workflow_preflight(supervisor)  # see `_plan_workflow`
        supervisor.close()
        close_event_loop()


def _render_workflow_completion(record, repo: str | None) -> None:
    echo(render_workflow(record))
    # Judge THIS invocation by its own execution: the row's state is whoever
    # wrote it last, and a run blocked before claiming can find it completed
    # by another invocation.
    try:
        execution = record.invocation()
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    if execution is not None:
        status = execution.get("status")
        error = execution.get("error")
    else:
        status = record.state
        error = record.manifest.get("error")
    if status == "cancelled":
        raise click.ClickException(f"workflow {record.workflow_id} was cancelled")
    if status != "completed":
        raise click.ClickException(error or f"workflow ended in {status}")
    assert record.final_run_id is not None
    echo()
    repo_args = ["--repo", repo] if repo else []
    echo(f"[syncmill] final run: {_shown(record.final_run_id)}")
    echo("[syncmill] review: "
         + printable_command("syncmill", "diff", args=[record.final_run_id], options=repo_args))
    echo("[syncmill] apply: "
         + printable_command("syncmill", "apply", args=[record.final_run_id], options=repo_args))


def _execute_workflow_direct(
    workflow_id: str,
    repo: str | None,
    *,
    resume: bool,
    owner: str | None = None,
    job_id: str | None = None,
) -> None:
    supervisor, _ = _workflow_supervisor(repo, _peek_workflow_team(repo, workflow_id))
    verb = "Resuming" if resume else "Executing"
    try:
        echo(f"[syncmill] {verb} workflow {_shown(workflow_id)}...")
        record = supervisor.run_workflow(
            workflow_id, resume=resume, owner=owner, job_id=job_id
        )
        _render_workflow_completion(record, repo)
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        _echo_workflow_preflight(supervisor)
        supervisor.close()
        close_event_loop()


def _workflow_job_store(config: SyncmillConfig, root: Path) -> JobStore:
    project_root = _apply_project_root(config, root)
    board_db = config.board.db_path if config.board.enabled else None
    return JobStore(config.tui, project_root, board_db=board_db)


def _workflow_active_job(store: JobStore, workflow_id: str) -> dict | None:
    for row in store.active_jobs():
        request = row.get("request") or {}
        if row.get("op") == "workflow-run" and request.get("workflow_id") == workflow_id:
            return row
    return None


def _follow_workflow_job(
    store: JobStore,
    workflow_store: WorkflowStore,
    row: dict,
    workflow_id: str,
) -> None:
    """Attach the foreground CLI to a durable manager without owning it."""
    job_id = str(row["job_id"])
    log_path = Path(row["job_dir"]) / "console.log"
    offsets: dict[tuple[int, int], int] = {}

    def emit_logs() -> None:
        nonlocal offsets
        segments = _log_segments(log_path.with_name("console.log.1"), log_path)
        for identity, raw in segments:
            offset = offsets.get(identity, 0)
            if offset > len(raw):
                offset = 0
            chunk = raw[offset:]
            if chunk:
                echo(chunk.decode("utf-8", errors="replace"), nl=False)
        offsets = {identity: len(raw) for identity, raw in segments}

    try:
        while True:
            emit_logs()
            store.active_jobs()
            current = next(
                (item for item in store.list_jobs() if item.get("job_id") == job_id),
                None,
            )
            if current is None:
                raise click.ClickException("workflow job record disappeared")
            if current.get("status") in {
                "finished",
                "cancelled",
                "force_stopped",
                "launch_failed",
                "lost",
            }:
                emit_logs()
                if current.get("status") != "finished" or current.get("exit_code") != 0:
                    raise click.ClickException(
                        current.get("error")
                        or f"workflow job ended in {current.get('status')}"
                    )
                return
            time.sleep(0.1)
    except KeyboardInterrupt:
        with contextlib.suppress(WorkflowError):
            workflow_store.cancel(workflow_id, expected_job_id=job_id)
        with contextlib.suppress(JobError):
            store.control(job_id, "stop")
        raise click.ClickException(
            f"cancellation requested for job {job_id} of workflow {workflow_id}; "
            "inspect it with `syncmill workflow status`"
        ) from None


def _execute_workflow(
    workflow_id: str, repo: str | None, *, resume: bool, detach: bool, team: str | None = None
) -> None:
    effective_team = team or _peek_workflow_team(repo, workflow_id)
    supervisor, config = _workflow_supervisor(repo, effective_team)
    try:
        workflow_store = supervisor.workflow_store
        record = workflow_store.load(workflow_id)
        expected = {"failed", "recovery_required", "running"} if resume else {"planned"}
        if record.state not in expected:
            action = "resume" if resume else "run"
            raise click.ClickException(
                f"cannot {action} workflow {workflow_id} from state {record.state!r}"
            )
        root = Path(config.repo_root).expanduser().resolve()
        if record.repo_root != str(root):
            raise click.ClickException("workflow belongs to a different repository")
        if record.manifest.get("spec_sha256") != record.spec.digest():
            raise click.ClickException(
                "workflow spec digest does not match durable state"
            )
        try:
            validate_workflow_policy(
                record.spec,
                config,
                supervisor.registry,
                planning_calls=planning_calls(record),
            )
        except WorkflowError as exc:
            raise click.ClickException(str(exc)) from exc
        if repository_fingerprint(root) != record.source_fingerprint:
            raise click.ClickException(
                "repository changed since planning; create a new workflow plan"
            )
        if os.name != "posix":
            if detach:
                raise click.ClickException(
                    "detached workflow execution requires a POSIX platform"
                )
            executed = supervisor.run_workflow(workflow_id, resume=resume)
            _render_workflow_completion(executed, repo)
            return
        store = _workflow_job_store(config, root)
        try:
            row = store.launch(
                WorkflowRunSpec(workflow_id=workflow_id, resume=resume),
                resolved_config=config,
            )
        except JobError as exc:
            raise click.ClickException(str(exc)) from exc
    finally:
        _echo_workflow_preflight(supervisor)
        supervisor.close()
        close_event_loop()
    if detach:
        echo(
            f"[syncmill] detached workflow-run job {row['job_id'][:8]} — "
            "inspect with `syncmill workflow status --watch` or the TUI Runs tab"
        )
        return
    _follow_workflow_job(store, workflow_store, row, workflow_id)


@workflow_group.command("run")
@click.argument("workflow_id")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option("--detach", is_flag=True, help="Run in a durable detached worker.")
@click.option("--team", default=None, help="Override the planned team.")
def workflow_run(workflow_id: str, repo: str | None, detach: bool, team: str | None) -> None:
    """Explicitly approve and execute a previously planned workflow."""
    _execute_workflow(workflow_id, repo, resume=False, detach=detach, team=team)


@workflow_group.command("resume")
@click.argument("workflow_id")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option("--detach", is_flag=True, help="Resume in a durable detached worker.")
@click.option("--team", default=None, help="Override the planned team.")
def workflow_resume(workflow_id: str, repo: str | None, detach: bool, team: str | None) -> None:
    """Resume a failed or interrupted workflow from durable node results."""
    _execute_workflow(workflow_id, repo, resume=True, detach=detach, team=team)


@workflow_group.command("cancel")
@click.argument("workflow_id")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option(
    "--force",
    is_flag=True,
    help="Force-stop after a prior safe cancellation and its grace period.",
)
def workflow_cancel(workflow_id: str, repo: str | None, force: bool) -> None:
    """Cancel a workflow and signal its durable process group when active."""
    supervisor, config = _workflow_supervisor(repo, None)
    try:
        current = supervisor.workflow_store.load(workflow_id)
        root = Path(config.repo_root).expanduser().resolve()
        job_store = _workflow_job_store(config, root) if os.name == "posix" else None
        job = _workflow_active_job(job_store, workflow_id) if job_store else None
        if force:
            if current.state != "cancelling":
                raise click.ClickException(
                    "force stop requires a prior safe cancellation request"
                )
            if job is None:
                raise click.ClickException("no active workflow process to force-stop")
            try:
                job_store.control(str(job["job_id"]), "force")
            except JobError as exc:
                raise click.ClickException(str(exc)) from exc
            echo(f"Force-stop requested for {_shown(workflow_id)}.")
            return
        record = supervisor.workflow_store.cancel(workflow_id)
        if record.state == "cancelling" and job is not None:
            try:
                job_store.control(str(job["job_id"]), "stop")
            except JobError as exc:
                raise click.ClickException(
                    f"workflow cancellation recorded, but process control failed: {exc}"
                ) from exc
            echo(f"Cancellation requested for {_shown(record.workflow_id)}.")
        else:
            echo(f"Cancelled {record.workflow_id}.")
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        supervisor.close()
        close_event_loop()


@workflow_group.command("prune")
@click.option("--repo", default=None, type=click.Path(), help=_PROJECT_REPO_HELP)
@click.option("--keep", type=click.IntRange(min=0), default=None)
@click.option("--retention-days", type=click.IntRange(min=1), default=None)
@click.option("--dry-run", is_flag=True)
def workflow_prune(
    repo: str | None, keep: int | None, retention_days: int | None, dry_run: bool
) -> None:
    """Prune old terminal workflows; active workflows are always retained."""
    supervisor, config = _workflow_supervisor(repo, None)
    try:
        removed = supervisor.workflow_store.prune(
            keep=config.workflow.keep_workflows if keep is None else keep,
            retention_days=(
                config.workflow.retention_days
                if retention_days is None
                else retention_days
            ),
            dry_run=dry_run,
        )
        prefix = "Would prune" if dry_run else "Pruned"
        echo(f"{prefix} {len(removed)} workflow(s).")
        for workflow_id in removed:
            echo(_shown(workflow_id))
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        supervisor.close()
        close_event_loop()


@cli.group("runs")
def runs_group() -> None:
    """Inspect durable run artifacts."""


@runs_group.command("list")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def runs_list(repo: Path) -> None:
    _, _, store = _project_artifacts(repo)
    try:
        listing = store.list_detailed()
    except ArtifactError as exc:
        raise click.ClickException(str(exc)) from exc
    for artifact in listing.artifacts:
        manifest = artifact.manifest
        echo(
            f"{printable_name(artifact.run_id)}  "
            f"{printable_name(str(manifest.get('state', '?'))):12} "
            f"{printable_name(str(manifest.get('agent_id', '?'))):10} "
            f"{printable_name(str(manifest.get('created_at', '')))}"
        )
    _warn_skipped_runs(listing.skipped)


def _warn_skipped_runs(skipped: list[SkippedRun]) -> None:
    """Name the run-store entries a listing omitted (#170), on stderr so the
    stdout rows stay script-parseable. Never touches the entries."""
    if not skipped:
        return
    noun = "directory" if len(skipped) == 1 else "directories"
    entries = ", ".join(
        f"{printable_name(entry.name)} ({entry.reason})" for entry in skipped
    )
    secho(
        f"[syncmill] {len(skipped)} run {noun} could not be read and "
        f"{'is' if len(skipped) == 1 else 'are'} omitted: {entries}. "
        "Inspect with: syncmill runs show <run-id>",
        fg="yellow",
        err=True,
    )


@runs_group.command("show")
@click.argument("run_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def runs_show(run_id: str, repo: Path) -> None:
    _, _, store = _project_artifacts(repo)
    try:
        echo(terminal_json(store.load(run_id).manifest))
    except ArtifactError as exc:
        raise click.ClickException(str(exc)) from exc


@runs_group.command("prune")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
@click.option("--keep", default=50, type=click.IntRange(min=0), show_default=True)
@click.option("--dry-run", is_flag=True)
def runs_prune(repo: Path, keep: int, dry_run: bool) -> None:
    """Prune old terminal runs; pending runs are always protected."""
    _, _, store = _project_artifacts(repo)
    try:
        removed = store.prune(keep, dry_run=dry_run)
    except ArtifactError as exc:
        raise click.ClickException(str(exc)) from exc
    action = "Would remove" if dry_run else "Removed"
    echo(f"{action} {len(removed)} run(s)")
    for run_id in removed:
        echo(printable_name(run_id))


@cli.command("diff")
@click.argument("run_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def diff_run(run_id: str, repo: Path) -> None:
    """Print the preserved winner patch for a run.

    At a terminal, control characters and invalid UTF-8 are shown escaped;
    redirected output is the stored patch byte for byte."""
    _, _, store = _project_artifacts(repo)
    try:
        echo_patch(store.patch(run_id))
    except (ArtifactError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


def _heal_workflow_approval(
    config: SyncmillConfig, root: Path, run_id: str, manifest: dict, store: ArtifactStore
) -> None:
    """Re-derive a workflow artifact's missing approval, or refuse it.

    A workflow stamps `plan.approved` on its final artifact after the
    lease-fenced write that commits its completion, and `ArtifactStore` refuses
    an unapproved workflow-origin artifact — that check is the authority, and it
    lives at the locked apply seam so no caller can go around it.

    What is left here is the narrow case where the producer died BETWEEN those
    two writes: the workflow is committed but its artifact never learned so.
    Only the CLI can settle that, because only it may open the workflow store.

    The question it asks is not "is some workflow completed?" but "did THIS
    workflow finalize THIS run?" — the artifact names the workflow, so a check
    that stops at the state can be satisfied by any copied or hand-written
    manifest pointing at someone else's genuinely completed workflow. The
    workflow's own `final_run_id` is the only side of that binding the artifact
    does not control. Every other answer refuses:

    - a row in any other state — the cancel-in-the-window case this exists for;
    - a row that finalized a DIFFERENT run, or none;
    - no row at all — `workflow prune` deletes terminal rows of EVERY outcome,
      including cancelled, so absence is not evidence of approval. A workflow
      that completed normally stamped its artifact at the time and never
      reaches this path;
    - a store that cannot be read — "the database would not open" is not
      evidence either, and this stands in front of patching the user's tree.
    """
    workflow_id = workflow_origin_id(manifest)
    if workflow_id is None or workflow_approved(manifest):
        return
    try:
        if not workflow_id:
            raise WorkflowError("the artifact names no workflow id")
        approval = stored_workflow_approval(
            Path(config.runs.root), project_id(root, config.project.id), workflow_id
        )
    except (WorkflowError, OSError, ValueError, sqlite3.Error) as exc:
        raise click.ClickException(
            f"run {run_id} came from a workflow whose state could not be read "
            f"({exc}). Refusing to apply a workflow result that cannot be shown "
            "to be approved."
        ) from exc
    if approval is None:
        raise click.ClickException(
            f"run {run_id} came from workflow {workflow_id}, which is no longer "
            "on record. Its result is not approved for apply."
        )
    state, final_run_id = approval
    if state != "completed":
        raise click.ClickException(
            f"run {run_id} came from workflow {workflow_id}, which is {state!r}, "
            "not completed. Its result is not approved for apply."
        )
    if final_run_id != run_id:
        raise click.ClickException(
            f"run {run_id} claims workflow {workflow_id}, but that workflow "
            f"finalized {final_run_id or 'no run'}. Its result is not approved "
            "for apply."
        )
    try:
        store.approve_workflow_run(run_id, root, workflow_id=workflow_id)
    except (ArtifactError, OSError) as exc:
        raise click.ClickException(
            f"run {run_id} is approved by workflow {workflow_id}, but that "
            f"could not be recorded on the artifact ({exc})."
        ) from exc


@cli.command("apply")
@click.argument("run_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
@click.option(
    "--verify", is_flag=True,
    help="Verify configured gates in isolation before applying to the source.",
)
@click.option(
    "--detach", is_flag=True,
    help="Run the apply in a durable detached worker (survives this process; "
    "inspect via `syncmill tui`). Source mutation is still serialized on the "
    "repository apply lock.",
)
def apply_run(run_id: str, repo: Path, verify: bool, detach: bool) -> None:
    """Apply a pending winner patch after strict repository validation."""
    root, config, store = _project_artifacts(repo)
    if detach:
        # Preflight the run before spawning: a typo'd id or a structurally
        # hollow manifest (missing fields, digest-mismatched patch) must fail
        # here with a clean non-zero exit, not exit 0 and die in the
        # background log (the `room apply --detach` selection-fence
        # precedent). A room-bound run is rejected up front too — the
        # worker's apply would refuse it anyway (Room lifecycle bypass), so
        # fail fast with the redirect.
        try:
            artifact = store.validate_applicable(run_id)
        except ArtifactError as exc:
            raise click.ClickException(str(exc)) from exc
        _heal_workflow_approval(config, root, run_id, artifact.manifest, store)
        if is_room_bound(artifact.manifest):
            raise click.ClickException(
                f"run {run_id} is room-bound; use 'syncmill room apply'"
            )
        state = artifact.manifest.get("state")
        if state not in ("pending", "applying"):
            # pending applies; applying is the crash-recovery path the
            # worker's reconcile pre-pass resolves. Every other state is
            # immutable for apply — the worker is guaranteed to fail, so
            # refuse before reporting a successful detach. The worker still
            # revalidates under the apply lock.
            raise click.ClickException(
                f"run {run_id} is {state!r}; nothing to apply"
            )
        _launch_detached_apply(
            config, root, ArtifactApplySpec(run_id=run_id, verify=verify)
        )
        return
    _run_apply(store, root, config, run_id, verify)


@cli.command("discard")
@click.argument("run_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def discard_run(run_id: str, repo: Path) -> None:
    """Mark a pending run as intentionally discarded."""
    _, _, store = _project_artifacts(repo)
    try:
        store.discard(run_id)
    except ArtifactError as exc:
        raise click.ClickException(str(exc)) from exc
    echo(f"Discarded run {printable_name(run_id)}")


@cli.command("recover")
@click.argument("run_id", required=False)
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
@click.option(
    "--list", "list_retained", is_flag=True,
    help="List recovery-retained RUN workspaces under workspace.root (#118); "
    "they are exempt from every automatic sweep and only leave via --discard.",
)
@click.option(
    "--discard", "discard_name", default=None, metavar="WORKSPACE",
    help="Delete ONE recovery-retained run workspace by its listed name. "
    "Explicit/human only — refuses anything not carrying both syncmill's run "
    "sentinel and its recovery marker. Room workspaces are out of scope here: "
    "run 'syncmill room recover' when needed, inspect, then 'syncmill room "
    "close' to reclaim.",
)
def recover_run(
    run_id: str | None, repo: Path, list_retained: bool, discard_name: str | None
) -> None:
    """Reconcile standalone artifact applies interrupted by a crash.

    Classifies each stranded 'applying' run from its receipt, patch digest, and
    source fingerprint: a completed apply is finished, an untouched source is
    restored to pending, and anything undecidable is frozen for manual recovery.
    Room-bound runs are left to 'syncmill room recover'. With --list/--discard
    it instead surfaces (or explicitly reclaims) the recovery-retained
    workspaces that no sweep ever touches.
    """
    if list_retained and discard_name is not None:
        raise click.UsageError("--list and --discard are mutually exclusive")
    if (list_retained or discard_name is not None) and run_id is not None:
        raise click.UsageError("--list/--discard do not take a RUN_ID argument")
    if list_retained or discard_name is not None:
        # --repo selects WHICH project's config is read (the _target_project_config
        # rule every project command follows). Reading the caller's cwd config here
        # would point a DESTRUCTIVE verb at another project's workspace.root.
        _, target_config = _target_project_config(repo)
        workspace_root = Path(target_config.workspace.root)
        if list_retained:
            retained = list_retained_workspaces(workspace_root)
            if not retained:
                echo("No retained recovery workspaces")
                return
            for ws in retained:
                echo(
                    f"{_shown(ws.name)}  ({ws.age_seconds / 86400:.1f}d)  {_shown(ws.path)}"
                )
            return
        try:
            removed = discard_retained_workspace(workspace_root, discard_name)
        except WorkspaceError as exc:
            raise click.ClickException(str(exc)) from exc
        echo(f"Discarded retained workspace {_shown(removed)}")
        return
    _, _, store = _project_artifacts(repo)
    reports = store.reconcile_interrupted_applies(run_id=run_id)
    actionable = [
        report
        for report in reports
        if report.outcome
        not in ("skipped_live_owner", "skipped_room_bound", "skipped_not_applying")
    ]
    if run_id is None and not actionable:
        if not reports:
            echo("No interrupted artifact apply to recover")
            return
        # All-skip is NOT an all-clear: a room-bound or lock-held strand still
        # exists, and suppressing its row would hide the `room recover`
        # redirect. Print the honest skip lines and fall through.
    _echo_reconciliations(reports)
    # Every report is printed, but a failed reconciliation (e.g. a missing
    # repository) must surface a non-zero exit so automation and the TUI can
    # detect it rather than read it as a clean recovery.
    if any(report.outcome == "error" for report in reports):
        raise SystemExit(1)


@cli.command("continue")
@click.argument("run_id")
@click.argument("prompt")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
@click.option("--fork", is_flag=True, help="Fork a Claude session before continuing.")
def continue_run(run_id: str, prompt: str, repo: Path, fork: bool) -> None:
    """Continue the winning provider session from a durable run."""
    root, config, store = _project_artifacts(repo)
    try:
        parent = store.load(run_id)
        manifest = parent.manifest
        session_id = manifest.get("provider_session_id")
        agent_id = manifest.get("agent_id")
        if not isinstance(session_id, str) or not session_id:
            raise ArtifactError("run has no resumable provider session")
        if manifest.get("conversation_closed"):
            raise ArtifactError("conversation is closed")
        if not isinstance(agent_id, str) or agent_id not in _AGENT_CONSTRUCTORS:
            raise ArtifactError("run has no valid winning agent")
        if fork and agent_id != "claude":
            raise ArtifactError("--fork is currently supported only for Claude")
        state = manifest.get("state")
        if state not in {"pending", "applied", "no_changes"}:
            raise ArtifactError(f"cannot continue a run in state {state!r}")
        seed_patch = None
        if state == "pending":
            if repository_fingerprint(root) != manifest.get("source_fingerprint"):
                raise ArtifactError(
                    "working tree changed since the pending run; apply or discard it first"
                )
            seed_patch = store.patch(run_id)
    except (ArtifactError, WorkspaceError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc

    # Selector affinity (§6.2): hand the parent's routing block (if any) to the
    # Supervisor, which inherits the winner's effective selector policy. Only a
    # genuinely ABSENT block is legacy ("resume with no model flag"); a plan
    # that is present-but-not-a-dict, or a routing key present-but-null, is
    # corrupt metadata and fails closed (§9) — never silently downgraded to
    # legacy. A present non-null block flows to inherit_decision, which owns
    # the strict field validation.
    parent_plan = manifest.get("plan")
    if parent_plan is None:
        inherited_routing = None
    elif not isinstance(parent_plan, dict):
        raise click.ClickException(CORRUPT_ROUTING_ERROR)
    else:
        inherited_routing = parent_plan.get("model_routing")
        if inherited_routing is None and "model_routing" in parent_plan:
            raise click.ClickException(CORRUPT_ROUTING_ERROR)

    config.repo_root = str(root)
    config.default_strategy = "route"
    _configure_project_memory(config, root)
    # A continuation creates a private workspace like any other run, so it owes
    # the same sweep — otherwise a continue-only automation that keeps getting
    # killed leaks a full clone every time, with nothing ever collecting them.
    sweep_orphaned_workspaces(
        Path(config.workspace.root), older_than=config.workspace.stale_seconds
    )
    supervisor = Supervisor(config)
    task = Task(prompt=prompt)
    try:
        result = supervisor.run(
            task,
            agent_id=agent_id,
            resume_session_id=session_id,
            parent_run_id=run_id,
            seed_patch=seed_patch,
            fork_session=fork,
            inherited_routing=inherited_routing,
        )
        echo()
        exit_code = render_task_result(result)
        if supervisor.last_routing_decision is not None:
            echo(
                f"[syncmill] {render_banner(supervisor.last_routing_decision)}"
            )
        if result.status == TaskStatus.COMPLETED and result.run_id:
            if state == "pending":
                store.supersede(run_id, result.run_id)
            echo(f"[syncmill] run: {_shown(result.run_id)}")
            echo(f"[syncmill] artifact: {_shown(result.artifact_path)}")
            if result.patch:
                echo("[syncmill] review: " + printable_command("syncmill", "diff", args=[result.run_id]))
                echo("[syncmill] apply: " + printable_command("syncmill", "apply", args=[result.run_id]))
        if exit_code:
            raise click.exceptions.Exit(exit_code)
    finally:
        supervisor.close()
        close_event_loop()


@cli.group("conversations")
def conversations_group() -> None:
    """Inspect and close provider-native conversations."""


@conversations_group.command("list")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def conversations_list(repo: Path) -> None:
    _, _, store = _project_artifacts(repo)
    try:
        listing = store.list_detailed()
    except ArtifactError as exc:
        raise click.ClickException(str(exc)) from exc
    for artifact in listing.artifacts:
        manifest = artifact.manifest
        if manifest.get("provider_session_id"):
            status = "closed" if manifest.get("conversation_closed") else "open"
            echo(
                f"{printable_name(artifact.run_id)}  {status:6} "
                f"{printable_name(str(manifest.get('agent_id', '?'))):10} "
                f"{printable_name(str(manifest.get('provider_session_id')))}"
            )
    _warn_skipped_runs(listing.skipped)


@conversations_group.command("show")
@click.argument("run_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def conversations_show(run_id: str, repo: Path) -> None:
    _, _, store = _project_artifacts(repo)
    try:
        artifact = store.load(run_id)
        result = json.loads(store.read_result(run_id))
    except (ArtifactError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    echo(terminal_json({"manifest": artifact.manifest, "result": result}))


@conversations_group.command("close")
@click.argument("run_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def conversations_close(run_id: str, repo: Path) -> None:
    _, _, store = _project_artifacts(repo)
    try:
        store.close_conversation(run_id)
    except ArtifactError as exc:
        raise click.ClickException(str(exc)) from exc
    echo(f"Closed conversation for run {printable_name(run_id)}")


# ── durable multi-agent comparison rooms ─────────────────────────────────


_PROMPT_FILE_HELP = (
    "Read the prompt from FILE, or `-` for stdin — multi-line prompts without "
    "shell quoting. Mutually exclusive with the positional PROMPT."
)


def _resolve_prompt(
    prompt: str | None, prompt_file: Path | None, *, required: bool
) -> str | None:
    """The effective prompt from the positional arg or ``--prompt-file``/stdin.

    Mutually exclusive; a file is decoded utf-8/surrogateescape (the room-worker
    fd pattern) and bounded by the room argv byte cap. ``required`` distinguishes
    `room create` (a prompt is mandatory) from `room round` (omitting it falls
    back to the room's creation prompt — the caller warns)."""
    from syncmill.rooms import _ROOM_PROMPT_MAX_BYTES, _prompt_arg_units

    if prompt is not None and prompt_file is not None:
        raise click.ClickException("PROMPT and --prompt-file are mutually exclusive")
    if prompt_file is not None:
        # Read at most cap+1 bytes: the size check must not require buffering an
        # arbitrarily large file (or an unbounded stdin stream) first.
        limit = _ROOM_PROMPT_MAX_BYTES + 1
        if str(prompt_file) == "-":
            raw = sys.stdin.buffer.read(limit)
        else:
            try:
                with prompt_file.open("rb") as handle:
                    raw = handle.read(limit)
            except OSError as exc:
                raise click.ClickException(str(exc)) from exc
        if len(raw) > _ROOM_PROMPT_MAX_BYTES:
            raise click.ClickException(
                "prompt exceeds the safe process argument byte limit"
            )
        try:
            # STRICT decode, not surrogateescape: a lone surrogate survives into
            # the room prompt and SQLite refuses to persist it, surfacing as an
            # uncaught UnicodeEncodeError deep in the round instead of a clean
            # "your file isn't UTF-8" here.
            prompt = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise click.ClickException(
                f"prompt is not valid UTF-8 (byte offset {exc.start})"
            ) from exc
        # NUL survives a strict decode but can never ride an argv element —
        # subprocess raises "embedded null byte" AFTER the round is underway
        # (freezing a room as recovery_required before any agent runs).
        if "\x00" in prompt:
            raise click.ClickException(
                f"prompt contains a NUL byte (offset {raw.index(0)}), which "
                "cannot be passed to a provider process"
            )
        # PLATFORM accounting, not raw bytes: `run --prompt-file` never passes
        # through RoomCoordinator's validation, and on Windows a quote-heavy
        # file that fits the byte cap can double past the CreateProcess limit
        # under list2cmdline quoting.
        if _prompt_arg_units(prompt, os.name) > _ROOM_PROMPT_MAX_BYTES:
            raise click.ClickException(
                "prompt exceeds the safe process argument byte limit"
            )
    # A blank prompt is not a prompt: for an optional round it must take the
    # documented fallback path (with its warning) rather than running an empty
    # instruction, and for a required one it is an error.
    if prompt is not None and not prompt.strip():
        if required:
            raise click.ClickException("the prompt is empty")
        prompt = None
    if prompt is None and required:
        raise click.ClickException("a prompt is required (PROMPT or --prompt-file)")
    return prompt



def _first_line(text: str, limit: int = 80) -> str:
    """The prompt's first line, trimmed for one-line echoes/tables.

    Non-printable characters (C0/C1 controls, bidi overrides) are visibly
    escaped through ``printable_text`` (never emitted raw): a stored prompt is
    untrusted display data, and echoing e.g. an OSC 52 sequence verbatim would
    let `room round`/`room rounds` rewrite the operator's clipboard or title.
    The stored prompt itself stays raw — only this display surface escapes.
    """
    line = text.strip().splitlines()[0] if text.strip() else ""
    line = printable_text(line)
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _usage_compact_from_manifest(manifest: dict) -> str | None:
    """The candidate manifest's optional ``usage`` block as a compact display
    string (None when absent/malformed — usage is best-effort telemetry)."""
    from syncmill.models import TokenUsage

    usage = TokenUsage.from_mapping(manifest.get("usage"))
    if usage is None:
        return None
    try:
        return usage.format_compact()
    # OverflowError: an absurd (but non-negative) int still reaches float
    # division inside format_compact.
    except (TypeError, ValueError, OverflowError):
        return None


def _room_coordinator(repo: Path) -> tuple[Path, SyncmillConfig, RoomCoordinator]:
    root, config = _target_project_config(repo)
    if not config.rooms.enabled:
        raise click.ClickException(
            "rooms are disabled — enable [rooms] enabled=true or "
            "SYNCMILL_ROOMS__ENABLED=true"
        )
    target = Path(config.repo_root).expanduser().resolve()
    try:
        coordinator = RoomCoordinator(config, target)
    except RoomError as exc:
        # Every `room *` command builds its coordinator HERE, before its own
        # try/except. Wrapping store-open failures into RoomError (rather than
        # letting sqlite errors escape) only reaches the user if something
        # translates it: without this, a corrupt or unwritable room database
        # exited non-zero with an unhandled RoomError and an EMPTY message.
        raise click.ClickException(str(exc)) from exc
    return target, config, coordinator


def _config_from_snapshot(repo: Path, resolved_config: Path) -> tuple[Path, SyncmillConfig]:
    """Consume a detached-worker config snapshot: read then UNLINK (race-free —
    the worker owns it), rebuild the config from the pinned JSON ONLY (never
    reloading mutable ambient TOML/env), and verify it targets this repository."""
    try:
        raw = resolved_config.read_text(encoding="utf-8")
    finally:
        resolved_config.unlink(missing_ok=True)
    try:
        config = SyncmillConfig.model_validate_json(raw)
        root = git_root(repo.resolve())
    except (ValidationError, ProjectError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if Path(config.repo_root).resolve() != root:
        raise click.ClickException("resolved worker config belongs to another repository")
    # Carry the launching session's team through the snapshot: the anti-forgery
    # scrub validator nulls `applied_team` at EVERY construction (including
    # model_validate_json here), so a detached apply / room worker launched
    # from a team-selected session recorded `team: null` in its envelope.
    # Re-stamp via apply_team — the only sanctioned stamper — and only for a
    # team the snapshot itself declares, so nothing forgeable slips through.
    try:
        carried = json.loads(raw).get("applied_team")
    except ValueError:
        carried = None
    if isinstance(carried, str) and carried in config.teams:
        apply_team(config, carried)
    return root, config


def _apply_project_root(config: SyncmillConfig, root: Path) -> Path:
    """The canonical project root the detached apply job anchors on — git root when
    available (a non-git project falls back to the resolved repo_root)."""
    try:
        return git_root(root)
    except ProjectError:
        return root


def _launch_detached_apply(
    config: SyncmillConfig, root: Path, spec: ArtifactApplySpec | RoomApplySpec
) -> None:
    project_root = _apply_project_root(config, root)
    board_db = config.board.db_path if config.board.enabled else None
    try:
        store = JobStore(config.tui, project_root, board_db=board_db)
        row = store.launch(spec, resolved_config=config)
    except JobError as exc:
        raise click.ClickException(str(exc)) from exc
    echo(
        f"[syncmill] detached {spec.op} job {row['job_id'][:8]} — "
        "inspect it in `syncmill tui` (Runs tab)"
    )


@cli.group("room")
def room_group() -> None:
    """Run isolated agents, compare every candidate, and apply one explicitly."""


@room_group.command("create")
@click.argument("prompt", required=False)
@click.option("--agent", "-a", "agents", multiple=True, help="Built-in agent (repeatable).")
@click.option(
    "--prompt-file",
    default=None,
    type=click.Path(path_type=Path, allow_dash=True),
    help=_PROMPT_FILE_HELP,
)
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_create(
    prompt: str | None,
    agents: tuple[str, ...],
    prompt_file: Path | None,
    repo: Path,
) -> None:
    resolved = _resolve_prompt(prompt, prompt_file, required=True)
    assert resolved is not None  # required=True guarantees it
    _, _, coordinator = _room_coordinator(repo)
    try:
        room = coordinator.create(resolved, list(agents) or None)
    except RoomError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()
    echo(room["id"])
    echo(
        f"Created room with {len(room['members'])} member(s). "
        f"Run: syncmill room round {room['id']}"
    )


@room_group.command("list")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_list(repo: Path) -> None:
    _, _, coordinator = _room_coordinator(repo)
    try:
        for room in coordinator.store.list_rooms():
            echo(
                f"{_shown(room['id'])}  {_shown(room['state']):18} "
                f"rounds:{_shown(room['round_count'])} "
                f"members:{len(room['members'])} {_shown(room['created_at'])}"
            )
    finally:
        coordinator.close()


@room_group.command("show")
@click.argument("room_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_show(room_id: str, repo: Path) -> None:
    _, _, coordinator = _room_coordinator(repo)
    try:
        room = coordinator.store.get_room(room_id)
        room["candidates"] = coordinator.store.list_candidates(room_id)
        room["events"] = coordinator.store.events(room_id)
        echo(terminal_json(room))
    except RoomError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()


@room_group.command("rounds")
@click.argument("room_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_rounds(room_id: str, repo: Path) -> None:
    """List a room's rounds with the prompt each one actually ran."""
    _, _, coordinator = _room_coordinator(repo)
    try:
        coordinator.store.get_room(room_id)
        for row in coordinator.store.list_rounds(room_id):
            echo(
                f"{_shown(row['number']):>3}  {_shown(row['state']):12} "
                f"{_shown(row['created_at'])}  "
                f"{_first_line(row['prompt'])}"
            )
    # A pager/`head` exiting early raises BrokenPipeError out of click.echo —
    # re-raise for Click's own EPIPE handling (the candidates pattern).
    except BrokenPipeError:
        raise
    # sqlite3.Error too: list_rounds is a bare SQL read, so a corrupt/locked db
    # must render as a clean error rather than a traceback.
    except (
        RoomError, KeyError, TypeError, AttributeError, OSError, ValueError,
        sqlite3.Error,
    ) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()


@room_group.command("round")
@click.argument("room_id")
@click.argument("prompt", required=False)
@click.option("--detach", is_flag=True, help="Run in a detached POSIX worker.")
@click.option(
    "--lineage",
    type=click.Choice(["continue-own", "fresh", "branch-from"]),
    default="continue-own",
    show_default=True,
)
@click.option("--base-candidate", default=None, help="Eligible base for branch-from.")
@click.option(
    "--fresh-session",
    is_flag=True,
    help="Start new provider conversations this round; stored session ids are "
    "dropped (loses warm prompt caches).",
)
@click.option(
    "--fork",
    is_flag=True,
    help="Fork each member's provider conversation instead of continuing it "
    "in place (claude members only).",
)
@click.option(
    "--prompt-file",
    default=None,
    type=click.Path(path_type=Path, allow_dash=True),
    help=_PROMPT_FILE_HELP,
)
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_round(
    room_id: str,
    prompt: str | None,
    detach: bool,
    lineage: str,
    base_candidate: str | None,
    fresh_session: bool,
    fork: bool,
    prompt_file: Path | None,
    repo: Path,
) -> None:
    prompt = _resolve_prompt(prompt, prompt_file, required=False)
    root, config, coordinator = _room_coordinator(repo)
    try:
        # Echo the prompt that will actually run; an omitted prompt silently
        # reused the room-creation prompt before — now it warns.
        echoed: str | None = prompt
        if prompt is None:
            secho(
                "[syncmill] no round prompt given — reusing the room creation prompt",
                fg="yellow",
                err=True,
            )
            # The LOOKUP is best-effort (a store hiccup must not pre-empt the
            # round's own, better error) — but the echo itself stays OUTSIDE the
            # suppression so a closed pipe still reaches Click's EPIPE handling
            # instead of silently launching provider work for a dead consumer.
            echoed = None
            with contextlib.suppress(Exception):
                echoed = coordinator.store.get_room(room_id)["prompt"]
        if echoed is not None:
            echo(f"Round prompt: {_first_line(echoed)}")
        if lineage == "branch-from" and not base_candidate:
            raise RoomError("branch-from requires a base candidate")
        if lineage != "branch-from" and base_candidate:
            raise RoomError("base candidate is valid only with branch-from")
        if fork:
            # Session forking is a claude-only provider feature (the same guard
            # `continue --fork` applies) — reject before any work starts.
            room = coordinator.store.get_room(room_id)
            non_claude = sorted(
                {
                    member["provider_id"]
                    for member in room["members"]
                    if member["provider_id"] != "claude"
                }
            )
            if non_claude:
                raise RoomError(
                    "--fork is supported only for claude members; room has: "
                    + ", ".join(non_claude)
                )
        if detach:
            coordinator.preflight_round(
                room_id,
                prompt,
                lineage=lineage,
                base_candidate_id=base_candidate,
                fresh_session=fresh_session,
                fork=fork,
            )
            pid = launch_room_worker(
                root,
                room_id,
                prompt,
                lineage=lineage,
                base_candidate_id=base_candidate,
                fresh_session=fresh_session,
                fork=fork,
                resolved_config=config,
                config_base=git_root(repo.resolve()),
            )
            coordinator.store.record_event(room_id, "worker.launched", {"pid": pid})
            echo(f"Room worker started: pid {pid}")
            return
        room = coordinator.run_round(
            room_id,
            prompt,
            lineage=lineage,
            base_candidate_id=base_candidate,
            fresh_session=fresh_session,
            fork=fork,
        )
        echo(f"Room {_shown(room_id)}: {_shown(room['state'])}")
        for candidate in coordinator.store.list_candidates(room_id):
            # Manifest-only read, degraded per candidate: usage is best-effort
            # display, so a historical candidate's missing/corrupt artifact
            # must neither fail the round that just committed nor pull every
            # candidate's full patch+output into memory.
            try:
                usage = _usage_compact_from_manifest(
                    coordinator.store.load_candidate_manifest(candidate["id"])
                )
            except RoomError:
                usage = None
            echo(
                f"{_shown(candidate['id'])}  {_shown(candidate['member_id']):10} "
                f"{_shown(candidate['state']):9} eligible:{bool(candidate['eligible'])}"
                + (f"  {usage}" if usage else "")
            )
    # ProjectError covers a repository that disappeared between the coordinator
    # opening and this call: git_root reports an unusable path that way rather
    # than letting subprocess's OSError through.
    except (RoomError, WorkspaceError, ProjectError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()


@room_group.command("candidates")
@click.argument("room_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_candidates(room_id: str, repo: Path) -> None:
    _, _, coordinator = _room_coordinator(repo)
    try:
        coordinator.store.get_room(room_id)
        for candidate in coordinator.store.list_candidates(room_id):
            manifest = coordinator.store.load_candidate(candidate["id"])
            # The read model's verdict: a candidate that never completed is
            # "not run", and a null gate is "no gate report" — never one `n/a`.
            gate = _gate_verdict(
                state=str(candidate["state"]),
                eligible=bool(candidate["eligible"]),
                manifest=manifest,
                patch_nonempty=bool(manifest.get("patch")),
            )
            gate_label = _GATE_TEXT.get(gate.verdict, gate.verdict).replace(" ", "-")
            usage = _usage_compact_from_manifest(manifest)
            echo(
                f"{_shown(candidate['id'])}  {_shown(candidate['member_id']):10} "
                f"{_shown(candidate['state']):9} gate:{gate_label:14} "
                f"patch:{_shown(manifest['patch_bytes'])}B"
                + (f"  {usage}" if usage else "")
            )
    # A pager/`head` exiting early raises BrokenPipeError (an OSError) out of
    # click.echo — re-raise it so Click's own EPIPE handling silences it,
    # instead of printing a spurious "Error: Broken pipe".
    except BrokenPipeError:
        raise
    # Beyond RoomError: a corrupt manifest (missing key, null field, scalar
    # gate) must render as a clean error, not a traceback — the same union the
    # TUI codified as _ROOM_POLL_ERRORS.
    except (RoomError, KeyError, TypeError, AttributeError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()


@room_group.command("compare")
@click.argument("room_id")
@click.option(
    "--round", "round_number", type=click.IntRange(min=1), default=None,
    help="Compare this round (default: the latest round).",
)
@click.option(
    "--all-rounds", is_flag=True,
    help="Show every round as its own group plus a cross-round gate summary.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the versioned JSON read model.")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_compare(
    room_id: str, round_number: int | None, all_rounds: bool, as_json: bool, repo: Path
) -> None:
    """Compare a room's candidates on recorded evidence (read-only)."""
    if round_number is not None and all_rounds:
        raise click.UsageError("--round and --all-rounds are mutually exclusive")
    _, _, coordinator = _room_coordinator(repo)
    try:
        comparison = build_room_comparison(
            coordinator.store,
            room_id,
            round_number=round_number,
            all_rounds=all_rounds,
            source_check=lambda: repository_fingerprint(coordinator.repo_root),
        )
        if as_json:
            echo(_comparison_json(comparison))
        else:
            echo(render_room_comparison(comparison))
    # A pager/`head` exiting early raises BrokenPipeError (an OSError) out of
    # click.echo — re-raise for Click's own EPIPE handling (see candidates).
    except BrokenPipeError:
        raise
    except UnknownRoundError as exc:
        raise click.BadParameter(printable_name(str(exc)), param_hint="--round") from exc
    # Per-candidate and per-round damage is rendered inside the comparison;
    # what reaches here is room-level (unknown room or round, a corrupt room
    # row). The message can quote stored values and argv, so it is escaped.
    except (RoomError, KeyError, TypeError, AttributeError, OSError, ValueError) as exc:
        raise click.ClickException(printable_name(str(exc))) from exc
    finally:
        coordinator.close()


def _comparison_json(comparison) -> str:
    """The one JSON serialization of a comparison: ``room compare --json`` and
    ``room export --format json`` print the same document."""
    return terminal_json(comparison_to_dict(comparison))


def _write_exclusive(path: Path, data: bytes) -> None:
    """Create ``path`` (mode 0600) and write ``data``; an existing file — or a
    symlink, even a dangling one — is refused, never replaced. A failed write
    removes the file this call created."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@room_group.command("export")
@click.argument("room_id")
@click.option(
    "--round", "round_number", type=click.IntRange(min=1), default=None,
    help="Export this round (default: the latest round).",
)
@click.option(
    "--all-rounds", is_flag=True,
    help="Export every round plus the cross-round gate summary.",
)
@click.option(
    "--format", "fmt", type=click.Choice(["markdown", "json"]), default="markdown",
    show_default=True,
    help="markdown: a portable review summary; json: the `room compare --json` document.",
)
@click.option(
    "--output", "output", type=click.Path(path_type=Path, dir_okay=False), default=None,
    help="Write to this new file (mode 0600); an existing file is refused. Default: stdout.",
)
@click.option(
    "--include", "includes", multiple=True, metavar="KIND:TARGET",
    help="Also include raw content: patch:CANDIDATE, output:CANDIDATE, "
    "gate-output:CANDIDATE (full ids) or prompt:ROUND. Repeatable. "
    "Needs --preview first, then --confirm TOKEN.",
)
@click.option(
    "--preview", is_flag=True,
    help="Show what --include would add, with sizes, digests and a token; write nothing.",
)
@click.option(
    "--confirm", "confirm", default=None, metavar="TOKEN",
    help="Write the --include content, only if it still matches the preview's token.",
)
@click.option(
    "--raw-max-bytes", type=click.IntRange(min=1), default=None,
    help=f"Bytes included per raw item (default {DEFAULT_MAX_BYTES}); "
    "digests always cover the whole artifact.",
)
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_export(
    room_id: str,
    round_number: int | None,
    all_rounds: bool,
    fmt: str,
    output: Path | None,
    includes: tuple[str, ...],
    preview: bool,
    confirm: str | None,
    raw_max_bytes: int | None,
    repo: Path,
) -> None:
    """Export a room's comparison as a body-free review summary (read-only).

    Recorded evidence only: no prompt, agent output or patch content, unless
    named with --include and confirmed after a --preview."""
    if round_number is not None and all_rounds:
        raise click.UsageError("--round and --all-rounds are mutually exclusive")
    if not includes:
        if preview or confirm is not None or raw_max_bytes is not None:
            raise click.UsageError(
                "--preview, --confirm and --raw-max-bytes apply only with --include"
            )
    elif preview == (confirm is not None):
        raise click.UsageError(
            "raw content takes two steps: run with --preview, then rerun the same "
            "command with --confirm TOKEN"
        )
    if preview and output is not None:
        raise click.UsageError("--preview writes nothing; drop --output")
    if confirm is not None and not _SHA256_HEX.fullmatch(confirm):
        raise click.BadParameter("must be 64 lowercase hex characters", param_hint="--confirm")
    try:
        specs = [parse_include(value) for value in includes]
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--include") from exc
    max_bytes = DEFAULT_MAX_BYTES if raw_max_bytes is None else raw_max_bytes
    _, _, coordinator = _room_coordinator(repo)
    try:
        comparison = build_room_comparison(
            coordinator.store,
            room_id,
            round_number=round_number,
            all_rounds=all_rounds,
            source_check=lambda: repository_fingerprint(coordinator.repo_root),
        )
        items = token = None
        if specs:
            items = collect_raw(coordinator.store, comparison, specs, max_bytes)
            token = preview_token(
                comparison.room_id,
                scope_of(comparison, all_rounds=all_rounds),
                max_bytes,
                items,
            )
            if preview:
                echo(render_preview(items, token, comparison.room_id), nl=False)
                return
            if token != confirm:
                raise click.ClickException(
                    "the content changed since the preview, or --include, the round "
                    "scope or --raw-max-bytes differ; run --preview again"
                )
        if fmt == "json":
            if items is None:
                text = _comparison_json(comparison) + "\n"
            else:
                # ASCII-escaped: the raw-inclusive document must not hand
                # stored control characters to a terminal (#194 tracks the
                # body-free documents, whose bytes stay unchanged here).
                document = comparison_to_dict(comparison)
                document["raw_content"] = json_section(items, token)
                text = json.dumps(document, indent=2, ensure_ascii=True) + "\n"
        else:
            text = render_review_markdown(
                comparison,
                generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                appendix=None if items is None else render_markdown_section(items, token),
            )
        if output is None:
            echo(text, nl=False)
        else:
            try:
                _write_exclusive(output, text.encode("utf-8"))
            except FileExistsError as exc:
                raise click.ClickException(
                    f"{printable_name(str(output))} already exists; "
                    "export never overwrites (choose a new path)"
                ) from exc
            echo(f"Wrote {printable_name(str(output))}", err=True)
        if items is not None:
            echo(f"WARNING: {RAW_WARNING}", err=True)
            echo(f"Raw content confirmed with token {token}", err=True)
    except BrokenPipeError:
        raise
    except UnknownRoundError as exc:
        raise click.BadParameter(printable_name(str(exc)), param_hint="--round") from exc
    except (RoomError, KeyError, TypeError, AttributeError, OSError, ValueError) as exc:
        raise click.ClickException(printable_name(str(exc))) from exc
    finally:
        coordinator.close()


@room_group.command("diff")
@click.argument("candidate_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_diff(candidate_id: str, repo: Path) -> None:
    """Print a candidate's patch.

    At a terminal, control characters and invalid UTF-8 are shown escaped;
    redirected output is the stored patch byte for byte."""
    _, _, coordinator = _room_coordinator(repo)
    try:
        artifact = coordinator.store.load_candidate(candidate_id)
        echo_patch(artifact["patch"])
    # A pager/`head` exiting early raises BrokenPipeError (an OSError) out of
    # click.echo — re-raise for Click's own EPIPE handling (see candidates).
    except BrokenPipeError:
        raise
    # A corrupt artifact (missing/null patch) is a clean error, not a
    # traceback — the _ROOM_POLL_ERRORS union.
    except (RoomError, KeyError, TypeError, AttributeError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _read_rationale(rationale: str | None, rationale_file: Path | None) -> str | None:
    if rationale is not None and rationale_file is not None:
        raise click.UsageError("--rationale and --rationale-file are mutually exclusive")
    if rationale_file is None:
        return rationale
    try:
        # Decoded from bytes: read_text() would translate \r and \r\n to \n
        # before the control-character check could refuse them.
        return rationale_file.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise click.BadParameter(
            f"cannot read {printable_name(str(rationale_file))}: {exc}",
            param_hint="--rationale-file",
        ) from exc


@room_group.command("select")
@click.argument("room_id")
@click.argument("candidate_id")
@click.option(
    "--expected-revision", type=click.IntRange(min=0), default=None,
    help="Refuse unless the room is still at this revision (from `room compare`). "
    "Without it, the selection is made against the room's current state.",
)
@click.option(
    "--expected-digest", default=None, metavar="SHA256",
    help="Refuse unless the candidate's patch sha256 is exactly this value.",
)
@click.option(
    "--rationale", default=None,
    help="Why this candidate was chosen (recorded with the selection).",
)
@click.option(
    "--rationale-file", type=click.Path(path_type=Path, dir_okay=False), default=None,
    help="Read the rationale from a UTF-8 file.",
)
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_select(
    room_id: str,
    candidate_id: str,
    expected_revision: int | None,
    expected_digest: str | None,
    rationale: str | None,
    rationale_file: Path | None,
    repo: Path,
) -> None:
    """Select a candidate after verifying its artifact (source is unchanged)."""
    if expected_digest is not None and not _SHA256_HEX.fullmatch(expected_digest):
        raise click.BadParameter(
            "must be 64 lowercase hex characters", param_hint="--expected-digest"
        )
    text = _read_rationale(rationale, rationale_file)
    _, _, coordinator = _room_coordinator(repo)
    try:
        room = coordinator.select(
            room_id,
            candidate_id,
            expected_revision=expected_revision,
            expected_patch_sha256=expected_digest,
            rationale=text,
            surface="cli",
        )
    except RoomError as exc:
        raise click.ClickException(printable_name(str(exc))) from exc
    finally:
        coordinator.close()
    # Every line comes from the snapshot the selection committed, never from
    # the request or a later read, so a concurrent reselection cannot mix in.
    record = json.loads(room["selection_json"])
    echo(
        f"Selected {_shown(record['candidate_id'])} for room {_shown(room_id)} "
        f"at revision {_shown(record['revision'])}"
    )
    echo(f"Patch sha256: {_shown(record['patch_sha256'])} (artifact verified)")
    if record["confirmed_revision"] is None:
        echo("Confirmed revision: not supplied (selected against the current state)")
    else:
        echo(f"Confirmed revision: {_shown(record['confirmed_revision'])}")
    echo("Rationale: " + ("recorded" if record["rationale"] else "none"))
    echo(
        "Source unchanged. Apply separately with: "
        + printable_command("syncmill", "room", "apply", args=[room_id])
    )


@room_group.command("apply")
@click.argument("room_id")
@click.option("--verify", is_flag=True, help="Run configured gates in isolation first.")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
@click.option(
    "--detach", is_flag=True,
    help="Run the apply in a durable detached worker (survives this process; "
    "inspect via `syncmill tui`). Source mutation is still serialized on the "
    "repository apply lock.",
)
@click.option(
    "--expected-revision", type=click.IntRange(min=0), default=None,
    help="Refuse unless the room is still at this revision.",
)
def room_apply(
    room_id: str, verify: bool, repo: Path, detach: bool, expected_revision: int | None
) -> None:
    root, config, coordinator = _room_coordinator(repo)
    if detach:
        # Fence the detached worker on the selection current at launch: a room
        # can be re-selected before the worker runs, and the worker must apply
        # the candidate the user confirmed here, not whatever is selected later
        # (the TUI confirm path captures the same pair).
        try:
            room = coordinator.store.get_room(room_id)
            candidate_id = room.get("selected_candidate_id")
            if not candidate_id:
                raise RoomError(
                    f"room {room_id} has no selected candidate — "
                    "select one first with `syncmill room select`"
                )
            revision = int(room["revision"])
            if expected_revision is not None and revision != expected_revision:
                raise RoomError("room changed after confirmation; review the apply again")
        except RoomError as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            coordinator.close()  # the detached worker opens its own coordinator
        _launch_detached_apply(
            config,
            root,
            RoomApplySpec(
                room_id=room_id,
                verify=verify,
                expected_candidate_id=candidate_id,
                expected_revision=revision,
            ),
        )
        return
    try:
        run_id, receipt = coordinator.apply(
            room_id, verify=verify, expected_revision=expected_revision
        )
    except (RoomError, ArtifactError, WorkspaceError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()
    echo(f"Applied room {_shown(room_id)} as run {_shown(run_id)}")
    echo(f"Receipt: {_shown(receipt)}")
    if coordinator.last_share_warning:
        # The source applied; only the documented room-result knowledge write
        # was lost — say so instead of completing silently without it.
        secho(
            f"[syncmill] memory: {_shown(coordinator.last_share_warning)}",
            fg="yellow",
            err=True,
        )


@room_group.command("attach")
@click.argument("room_id")
@click.option("--round", "round_number", type=int, default=None, help="Round number (default: latest).")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_attach(room_id: str, round_number: int | None, repo: Path) -> None:
    """Open a read-only tmux viewer of a round's per-member output (one pane each)."""
    _, _, coordinator = _room_coordinator(repo)
    try:
        # Reclaim any viewer left behind by a since-closed/pruned room first
        # (the session-start-sweeps-stale-dirs precedent), best-effort.
        with contextlib.suppress(Exception):
            sweep_orphan_viewers(coordinator.store, coordinator.project_id)
        try:
            name = ensure_viewer(
                coordinator.store, coordinator.project_id, room_id, round_number
            )
        except (RoomViewerError, RoomError) as exc:
            raise click.ClickException(str(exc)) from exc
        if in_tmux():
            secho(
                f"[syncmill] switching to viewer '{_shown(name)}' — return with Prefix + L",
                fg="yellow",
                err=True,
            )
        else:
            secho(
                f"[syncmill] attaching to viewer '{_shown(name)}' — detach with Ctrl-b d",
                fg="yellow",
                err=True,
            )
        rc = attach_viewer(name)
        if rc != 0:
            raise click.ClickException(f"could not attach to viewer '{name}' (tmux exit {rc})")
    finally:
        coordinator.close()


@room_group.command("close")
@click.argument("room_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_close(room_id: str, repo: Path) -> None:
    _, _, coordinator = _room_coordinator(repo)
    try:
        # close_room reclaims the room's viewers at the lifecycle boundary.
        coordinator.close_room(room_id)
    except RoomError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()
    echo(f"Closed room {_shown(room_id)}")


@room_group.command("recover")
@click.argument("room_id", required=False)
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_recover(room_id: str | None, repo: Path) -> None:
    """Recover dead room workers and interrupted room applies."""
    _, _, coordinator = _room_coordinator(repo)
    try:
        if room_id is not None:
            coordinator.store.get_room(room_id)
        recovered = coordinator.recover(room_id)
    except (RoomError, ArtifactError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()
    if recovered:
        for recovered_id in recovered:
            echo(f"Recovered room {_shown(recovered_id)}; inspect or close it explicitly")
    else:
        echo("No dead room worker or interrupted apply to recover")


@room_group.command("prune")
@click.option("--dry-run", is_flag=True, help="List rooms without deleting them.")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def room_prune(dry_run: bool, repo: Path) -> None:
    """Prune terminal rooms using [rooms] count and age retention bounds."""
    _, config, coordinator = _room_coordinator(repo)
    try:
        removed = coordinator.prune_terminal(
            keep=config.rooms.keep_rooms,
            retention_days=config.rooms.retention_days,
            dry_run=dry_run,
        )
    finally:
        coordinator.close()
    action = "Would prune" if dry_run else "Pruned"
    if not removed:
        echo(f"{action} 0 rooms")
        return
    echo(f"{action} {len(removed)} room(s):")
    for room_id in removed:
        echo(f"  {_shown(room_id)}")


@cli.command("room-worker", hidden=True)
@click.argument("room_id")
@click.option("--prompt", default=None)
@click.option("--prompt-fd", default=None, type=int, hidden=True)
@click.option(
    "--lineage",
    type=click.Choice(["continue-own", "fresh", "branch-from"]),
    default="continue-own",
)
@click.option("--base-candidate", default=None)
@click.option("--fresh-session", is_flag=True, hidden=True)
@click.option("--fork", is_flag=True, hidden=True)
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_WORKER_REPO_HELP
)
@click.option(
    "--resolved-config",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    hidden=True,
)
def room_worker(
    room_id: str,
    prompt: str | None,
    prompt_fd: int | None,
    lineage: str,
    base_candidate: str | None,
    fresh_session: bool,
    fork: bool,
    repo: Path,
    resolved_config: Path | None,
) -> None:
    """Detached worker entrypoint; public callers use ``room round --detach``."""
    if prompt_fd is not None:
        # The launcher keeps the prompt off argv (visible in `ps`) AND off the
        # filesystem: it rides an inherited fd of an already-unlinked temp
        # file, so there is no plaintext path to strand if this worker dies at
        # any point. Consuming the fd closes the only remaining handle.
        with os.fdopen(prompt_fd, "rb") as handle:
            prompt = handle.read().decode("utf-8", errors="surrogateescape")
    if resolved_config is None:
        _, _, coordinator = _room_coordinator(repo)
    else:
        root, config = _config_from_snapshot(repo, resolved_config)
        if not config.rooms.enabled:
            raise click.ClickException("resolved room config has rooms disabled")
        coordinator = RoomCoordinator(config, root)
    try:
        coordinator.run_round(
            room_id,
            prompt,
            lineage=lineage,
            base_candidate_id=base_candidate,
            fresh_session=fresh_session,
            fork=fork,
        )
    finally:
        coordinator.close()


@cli.command("apply-worker", hidden=True)
@click.argument("run_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_WORKER_REPO_HELP
)
@click.option("--verify", is_flag=True)
@click.option(
    "--resolved-config",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    hidden=True,
)
def apply_worker(
    run_id: str, repo: Path, verify: bool, resolved_config: Path | None
) -> None:
    """Detached apply worker entrypoint; public callers use ``apply --detach``."""
    if resolved_config is None:
        root, config, store = _project_artifacts(repo)
    else:
        root, config = _config_from_snapshot(repo, resolved_config)
        store = ArtifactStore(
            Path(config.runs.root), project_id(root, config.project.id)
        )
    _run_apply(store, root, config, run_id, verify)


@cli.command("workflow-worker", hidden=True)
@click.argument("workflow_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_WORKER_REPO_HELP
)
@click.option("--resume", is_flag=True)
@click.option(
    "--resolved-config",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    hidden=True,
)
def workflow_worker(
    workflow_id: str,
    repo: Path,
    resume: bool,
    resolved_config: Path | None,
) -> None:
    """Durable worker entrypoint; callers use ``workflow run --detach``."""
    job_id = os.environ.get("SYNCMILL_TUI_JOB_ID")
    owner = f"{os.getpid()}-{job_id or uuid4().hex}"
    if resolved_config is None:
        _execute_workflow_direct(
            workflow_id,
            str(repo),
            resume=resume,
            owner=owner,
            job_id=job_id,
        )
        return
    root, config = _config_from_snapshot(repo, resolved_config)
    _configure_project_memory(config, root)
    supervisor = Supervisor(config)
    try:
        echo(
            f"[syncmill] {'Resuming' if resume else 'Executing'} workflow "
            f"{_shown(workflow_id)}..."
        )
        record = supervisor.run_workflow(
            workflow_id,
            resume=resume,
            owner=owner,
            job_id=job_id,
        )
        _render_workflow_completion(record, str(root))
    except WorkflowError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        _echo_workflow_preflight(supervisor)
        supervisor.close()
        close_event_loop()


@cli.command("room-apply-worker", hidden=True)
@click.argument("room_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_WORKER_REPO_HELP
)
@click.option("--verify", is_flag=True)
@click.option("--expected-candidate", default=None)
@click.option("--expected-revision", type=int, default=None)
@click.option(
    "--resolved-config",
    default=None,
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    hidden=True,
)
def room_apply_worker(
    room_id: str,
    repo: Path,
    verify: bool,
    expected_candidate: str | None,
    expected_revision: int | None,
    resolved_config: Path | None,
) -> None:
    """Detached room-apply worker entrypoint; callers use ``room apply --detach``."""
    if resolved_config is None:
        root, config, coordinator = _room_coordinator(repo)
    else:
        root, config = _config_from_snapshot(repo, resolved_config)
        if not config.rooms.enabled:
            raise click.ClickException("resolved room config has rooms disabled")
        coordinator = RoomCoordinator(config, root)
    try:
        run_id, receipt = coordinator.apply(
            room_id,
            verify=verify,
            expected_candidate_id=expected_candidate,
            expected_revision=expected_revision,
        )
    except (RoomError, ArtifactError, WorkspaceError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        coordinator.close()
    echo(f"Applied room {_shown(room_id)} as run {_shown(run_id)}")
    echo(f"Receipt: {_shown(receipt)}")


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
        # Hooks are observability only and must never change an agent outcome —
        # which has to hold for the RECOVERY path too. An unwritable spool dir
        # made this handler raise, so the hook exited non-zero and could fail
        # the very agent turn the rule protects. The marker is a best-effort
        # breadcrumb; losing it is strictly better than failing the turn.
        with contextlib.suppress(Exception):
            marker = Path(f"{spool}.incomplete")
            marker.parent.mkdir(parents=True, exist_ok=True)
            # 0600 AT CREATION, not after: a world-readable window is exactly
            # what the other spool writers open with a mode for.
            fd = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("collector_error")
            os.chmod(marker, 0o600)  # pre-existing marker: tighten it too


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
    selection: str | None = None,
    no_continue: bool = False,
    difficulty: str | None = None,
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
    if selection:
        argv += ["--selection", selection]
    if no_continue:
        argv += ["--no-continue"]
    if difficulty:
        # Forwarded raw (like --team): the INNER owns the Supervisor and the
        # durable artifact, so it — never the outer — resolves the decision.
        argv += ["--difficulty", difficulty]
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
    selection: str | None = None,
    no_continue: bool = False,
    difficulty: str | None = None,
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
        secho(
            f"[syncmill] --monitor applies only to concurrent strategies "
            f"({', '.join(sorted(CONCURRENT_STRATEGIES))}); running '{sname}' "
            f"without the live monitor.",
            fg="yellow",
        )
        return None
    if not _stdout_isatty():
        secho(
            "[syncmill] --monitor needs an interactive terminal (stdout is not a "
            "TTY); running without the live monitor.",
            fg="yellow",
        )
        return None
    if not tmux_available():
        secho(
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
        selection=selection,
        no_continue=no_continue,
        difficulty=difficulty,
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
        secho(
            f"[syncmill] tmux session failed to start ({_shown(exc)}); running without "
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

    The catch matches ``_load_run_config``'s: a half-edited ``syncmill.toml`` in
    cwd raises ``tomllib.TOMLDecodeError`` — a ``ValueError`` from the settings
    source, not a ``ValidationError`` — and an unreadable file or a dead HOME
    raise ``OSError``/``RuntimeError``. Every command builds its config through
    here, so a narrower catch tracebacks most of the CLI on a typo'd config.
    """
    try:
        return SyncmillConfig()
    except (ValidationError, ValueError, OSError, RuntimeError) as exc:
        raise click.ClickException(_described("invalid configuration", exc))


def _load_run_config(repo: str | None) -> SyncmillConfig:
    """Load an initialized target project's config even when invoked elsewhere."""
    if repo:
        try:
            root = git_root(Path(repo).expanduser().resolve())
        except ProjectError:
            pass  # strict workspace validation later produces the user-facing error
        else:
            # Even without a target syncmill.toml this must resolve env >
            # defaults AT THE TARGET: falling through to _load_config() would
            # read the CALLER's cwd toml and lend its project id, state roots,
            # and gates to the target (whose later diff/apply then computes the
            # target namespace and cannot find the artifact). The explicit
            # --repo flag beats an ambient SYNCMILL_REPO_ROOT. The loader-failure
            # surface (a malformed/unreadable target toml, a dead HOME) is
            # _target_project_config's, shared with every other --repo command.
            return _target_project_config(root, repo_root_override=str(root))[1]
    return _load_config()


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
        available = ", ".join(_shown(t) for t in sorted(config.teams)) or "(none configured)"
        raise click.BadParameter(
            f"unknown team: {team_name!r}. Available: {available}",
            param_hint="'--team'",
        )
    unknown = [a for a in team.agents if a not in _AGENT_CONSTRUCTORS]
    if unknown:
        raise click.ClickException(
            f"team {team_name!r} names unknown agent(s): {', '.join(_shown(a) for a in unknown)}. "
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
            f"the profile with: "
            f"{printable_command('syncmill', 'team', 'save', args=[name], options=['-a', 'AGENT'])}"
        )
    _apply_team(config, name)
    return name


@cli.command()
@click.argument("prompt", required=False)
@click.option(
    "--prompt-file",
    default=None,
    type=click.Path(path_type=Path, allow_dash=True),
    help=_PROMPT_FILE_HELP,
)
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
    "all agents concurrently in isolated worktrees and converges under the "
    "selection policy; 'council' has agents propose, critique over rounds, "
    "then synthesize a converged result, deliberating via shared "
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
    "before its worktree is torn down. `syncmill run` never auto-applies changes; "
    "review retained runs with `syncmill diff` and apply them with `syncmill apply` "
    "(or apply this compatibility copy with `git apply <file>`).",
)
@click.option("--dry-run", is_flag=True, help="Validate and print the bounded run plan only.")
@click.option(
    "--selection",
    type=click.Choice(["verified", "priority", "judge"]),
    default=None,
    help="Convergence policy for compete/council/decompose (default: verified). "
    "'verified' selects the first gate-passing completed candidate in priority "
    "order; 'priority' checks only the highest-priority completed candidate; "
    "'judge' ranks gate-passing candidates and falls back to verified priority "
    "if judging fails. Inert for route/pipeline.",
)
@click.option(
    "--no-continue",
    is_flag=True,
    help="Disable provider-native session continuation for this run.",
)
@click.option(
    "--difficulty",
    type=click.Choice(["auto", "low", "medium", "high"]),
    default=None,
    help="Model routing: apply the configured [model_routing.models] selector "
    "for an explicit tier, or 'auto' to classify this run (observe). "
    "Default: config model_routing.mode (off = no routing).",
)
def run(
    prompt: str | None,
    prompt_file: Path | None,
    agent: str | None,
    exclude: tuple[str, ...],
    timeout: int | None,
    repo: str | None,
    capability: tuple[str, ...],
    strategy: str | None,
    team: str | None,
    monitor: bool,
    emit_patch: str | None,
    dry_run: bool,
    selection: str | None,
    no_continue: bool,
    difficulty: str | None,
):
    """Execute a task through the agent orchestration pipeline."""
    # Resolve the prompt FIRST (positional or --prompt-file/stdin) — resolved
    # before _launch_monitor_if_requested, so a monitored run's inner argv
    # carries the file-resolved text positionally with no extra forwarding.
    resolved_prompt = _resolve_prompt(prompt, prompt_file, required=True)
    assert resolved_prompt is not None
    prompt = resolved_prompt
    if prompt.startswith("millflow:"):
        goal = prompt[len("millflow:") :].strip()
        if not goal:
            raise click.BadParameter(
                "millflow: must be followed by a workflow goal",
                param_hint="'prompt'",
            )
        incompatible = []
        if agent:
            incompatible.append("--agent")
        if any(exclude):
            incompatible.append("--exclude")
        if timeout is not None:
            incompatible.append("--timeout")
        if any(capability):
            incompatible.append("--capability")
        if strategy is not None:
            incompatible.append("--strategy")
        if monitor:
            incompatible.append("--monitor")
        if emit_patch is not None:
            incompatible.append("--emit-patch")
        if dry_run:
            incompatible.append("--dry-run")
        if selection is not None:
            incompatible.append("--selection")
        if no_continue:
            incompatible.append("--no-continue")
        if difficulty is not None:
            incompatible.append("--difficulty")
        if incompatible:
            raise click.BadParameter(
                "millflow planning does not accept: " + ", ".join(incompatible),
                param_hint="'prompt'",
            )
        _plan_workflow(goal, repo=repo, team=team, planner=None)
        return
    # Build from env + syncmill.toml + defaults, then layer the explicit CLI
    # flags on top (each only when actually passed) — so `--repo` left unset lets
    # repo_root come from the file/env instead of clobbering it with the default.
    config = _load_run_config(repo)
    if repo:  # ignore an empty `-r ""` like exclude/capability do, so it falls
        # back to repo_root from the file/env, not cwd. EXPANDED: consumers
        # below `.resolve()` without `.expanduser()`, so a quoted `~/work/app`
        # (from a Makefile, a config, a subprocess list) used to resolve to
        # `$PWD/~/work/app` and fail as "not a Git repository".
        config.repo_root = str(Path(repo).expanduser())
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
    if selection:
        config.selection.mode = selection
    if no_continue:
        config.conversations.enabled = False
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

    if dry_run:
        repo_path = Path(config.repo_root).resolve()
        run_id = f"dry-run-{uuid4()}"
        snapshot = None
        try:
            snapshot = capture_repository_snapshot(
                repo_path, run_id, Path(config.workspace.root)
            )
            lineup = [
                aid for aid in config.agent_priority
                if config.agent_config(aid).enabled and aid not in exclude_set
            ]
            # Lineup parity with the real Supervisor: a forced agent must be a
            # known, enabled agent, and an empty lineup is a rejected run — the
            # dry-run must reach the same verdict, not preview a plan (or build
            # a routing decision) for a run that could never start. The
            # non-forced lineup goes through the SAME _matched_order used by the
            # real run (a display matcher over id shims — matching only reads
            # .agent_id), so -c hints reorder the previewed plan and the routing
            # decision projects onto the genuine post-matcher lineup.
            if agent:
                if (
                    agent not in config.agent_priority
                    or not config.agent_config(agent).enabled
                ):
                    raise click.ClickException(
                        f"Agent '{agent}' not found or not enabled"
                    )
                lineup = [agent]
            elif not lineup:
                error = "No agents available"
                if exclude_set:
                    error += f" (excluded: {', '.join(sorted(exclude_set))})"
                raise click.ClickException(error)
            else:
                dry_matcher = build_matcher(config.capabilities, config.agent_priority)
                dry_task = Task(prompt=prompt, tags=cap_list)
                shims = [types.SimpleNamespace(agent_id=aid) for aid in lineup]
                lineup = [
                    shim.agent_id
                    for shim in _matched_order(shims, dry_task, dry_matcher)
                ]
            # Preview the run's canonical routing decision in the plan JSON,
            # with the SAME verdicts the real run would reach: an all-unmapped
            # explicit tier is a hard error here too (dry-run exists to catch
            # exactly that before an agent starts), while an observe classifier
            # crash degrades to a plan without the block (the real run's §9
            # observe policy).
            routing_dict = None
            try:
                routing = build_decision(
                    config.model_routing,
                    prompt=prompt,
                    lineup=lineup,
                    requested=difficulty,
                )
                routing_dict = routing.as_dict() if routing is not None else None
            except AllUnmappedError as exc:
                raise click.ClickException(str(exc)) from exc
            except ClassifierError as exc:
                secho(
                    f"[syncmill] model routing classifier failed ({_shown(exc)}); "
                    "plan shown without a decision",
                    fg="yellow",
                    err=True,
                )
            plan = build_run_plan(config, lineup, model_routing=routing_dict)
            echo(terminal_json(plan.as_dict()))
            echo(f"repository: {_shown(snapshot.source_root)}")
            echo(
                "working_tree: "
                + ("dirty (captured)" if subprocess_status_dirty(repo_path) else "clean")
            )
            echo("No agent was started and no source file was changed.")
            return
        except WorkspaceError as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            if snapshot is not None:
                shutil.rmtree(snapshot.run_root, ignore_errors=True)

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
        selection=selection,
        no_continue=no_continue,
        difficulty=difficulty,
    )
    if monitor_code is not None:
        sys.exit(monitor_code)

    # Cleanup any orphaned worktrees from crashed runs. Use the resolved
    # config.repo_root (which may come from syncmill.toml / env), not the raw
    # flag, so cleanup and the Supervisor's worktree pool target the same root.
    repo_path = Path(config.repo_root).resolve()
    # v2 projects keep historical orchestration memory inside the project.
    # Explicit memory paths still win; this only replaces the legacy ambient
    # defaults for initialized projects.
    _configure_project_memory(config, repo_path)
    cleanup_orphaned_worktrees(repo_path)
    # The snapshot workspaces live outside the repo, so the sweep above cannot
    # see them; without this a killed run leaks its control clone forever.
    sweep_orphaned_workspaces(
        Path(config.workspace.root), older_than=config.workspace.stale_seconds
    )

    supervisor = Supervisor(config)
    task = Task(prompt=prompt, tags=cap_list)

    try:
        echo(f"[syncmill] Executing task {str(task.id)[:8]}...")
        if agent:
            echo(f"[syncmill] Agent: {_shown(agent)} (forced, no fallback)")
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
            excluded = ", ".join(_shown(e) for e in sorted(exclude_set))
            suffix = f" — excluded: {excluded}" if exclude_set else ""
            if cap_list:
                suffix += f" — capability hint: {', '.join(_shown(c) for c in cap_list)}"
            if team:
                shown_team = _shown(team)
                suffix += (
                    f" — team: {shown_team} (default)" if default_applied
                    else f" — team: {shown_team}"
                )
            mode = {
                "pipeline": "pipeline",
                "compete": "compete",
                "council": "council",
                "decompose": "decompose",
            }.get(config.default_strategy, "priority fallback")
            echo(f"[syncmill] Agents: {shown} ({mode}){suffix}")

        result = supervisor.run(
            task, agent_id=agent, exclude=exclude_set, difficulty=difficulty
        )

        # Surface a degraded model-routing classifier like memory/breaker — the
        # observe recommendation is advisory, so the run went ahead without it.
        if supervisor.last_routing_warning is not None:
            secho(
                f"[syncmill] {_shown(supervisor.last_routing_warning)}", fg="yellow"
            )

        # Surface degraded memory without failing the task
        if supervisor.memory.degraded:
            secho(
                f"[syncmill] memory unavailable — continuing without shared context "
                f"({_shown(supervisor.memory.last_error)})",
                fg="yellow",
            )

        # Surface a degraded circuit breaker the same way — its state file was
        # unreadable, so it tracked nothing this run, but the task still ran.
        if supervisor.breaker is not None and supervisor.breaker.degraded:
            secho(
                f"[syncmill] circuit breaker state unavailable — not tracking "
                f"failures ({_shown(supervisor.breaker.last_error)})",
                fg="yellow",
            )

        # Surface advisory-preflight warnings the same way — advisory means the
        # run went ahead regardless (unmapped identities, policy rejections, a
        # failed/timed-out toolgraph probe).
        for line in supervisor.preflight_warnings:
            secho(f"[syncmill] preflight: {_shown(line)}", fg="yellow")

        # Display result via the shared renderer — byte-identical to a monitored
        # run's result.json render, so --monitor changes nothing the user sees.
        echo()
        exit_code = render_task_result(result)
        if emit_patch is not None and not emit_patch_file(
            emit_patch, _result_to_json(result)
        ):
            exit_code = exit_code or 1  # explicit export failed — never exit 0
        # Same line/position as the monitored OUTER prints after render_result —
        # the byte-parity invariant. None when reporting is off or the write
        # degraded (best-effort — never affects the exit code).
        if supervisor.last_report_path is not None:
            echo(f"[syncmill] report: {_shown(supervisor.last_report_path)}")
        # Compact routing banner (§3.4) — only when routing was used this run;
        # a default-off run prints nothing here.
        if supervisor.last_routing_decision is not None:
            echo(
                f"[syncmill] {render_banner(supervisor.last_routing_decision)}"
            )
        artifact_path = getattr(supervisor, "last_artifact_path", None)
        if artifact_path is not None and result.run_id is not None:
            echo(f"[syncmill] run: {_shown(result.run_id)}")
            echo(f"[syncmill] artifact: {_shown(artifact_path)}")
            if result.patch:
                echo("[syncmill] review: " + printable_command("syncmill", "diff", args=[result.run_id]))
                echo("[syncmill] apply: " + printable_command("syncmill", "apply", args=[result.run_id]))
        recovery_workspace = getattr(supervisor, "recovery_workspace", None)
        if recovery_workspace is not None:
            secho(
                f"[syncmill] recovery workspace retained: "
                f"{_shown(recovery_workspace)}",
                fg="yellow",
            )
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
        echo("Registered agents (priority order):\n")
        for i, agent_id in enumerate(config.agent_priority, 1):
            agent_cfg = config.agent_config(agent_id)
            agent = supervisor.registry.get(agent_id)
            status = "enabled" if agent else "disabled"
            color = "green" if agent else "red"

            echo(f"  {i}. ", nl=False)
            secho(agent_id, fg=color, bold=True, nl=False)
            echo(f" [{status}]", nl=False)
            echo(f"  timeout={agent_cfg.timeout_seconds}s, max_turns={agent_cfg.max_turns}")
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
        echo(
            "[syncmill] no teams configured — add [teams.<name>] tables to "
            "syncmill.toml (or SYNCMILL_TEAMS)."
        )
        return
    echo("Configured teams:\n")
    for name in sorted(config.teams):
        team = config.teams[name]
        agents_shown = " → ".join(_shown(a) for a in team.agents)
        strategy = (
            _shown(team.strategy) if team.strategy
            else f"(default: {_shown(config.default_strategy)})"
        )
        goals = ", ".join(_shown(g) for g in team.goals) if team.goals else "—"
        caps = f"{len(team.capabilities)} agent(s)" if team.capabilities else "—"
        secho(f"  {_shown(name)}", bold=True, nl=False)
        echo(
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
        available = ", ".join(_shown(t) for t in sorted(config.teams)) or "(none configured)"
        raise click.BadParameter(
            f"unknown team: {name!r}. Available: {available}", param_hint="NAME"
        )
    # The shared renderer — byte-identical to the dashboard TEAM pane's detail
    # (the render_item/render_board_stats precedent).
    echo(render_team(name, team, config))


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
        # Validation reads the file, so it shares the write lock with the save:
        # otherwise a concurrent writer could land a conflicting team in between
        # and this save would persist a combination neither view ever validated.
        with config_write_lock(path):
            validate_team_save(config, name, team, path=path)
            save_team(path, name, team)
    except TeamFileError as exc:
        raise click.ClickException(str(exc))
    echo(f"[syncmill] saved team '{_shown(name)}' to {_shown(path)}")
    if "SYNCMILL_TEAMS" in os.environ:
        secho(
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
        echo(f"[syncmill] cleared default_team in {_shown(path)}")
        return
    config = _load_config()
    if name not in config.teams:
        available = ", ".join(_shown(t) for t in sorted(config.teams)) or "(none configured)"
        raise click.BadParameter(
            f"unknown team: {name!r}. Available: {available}", param_hint="NAME"
        )
    try:
        # One transaction: the warning below reports on the same file state the
        # default was written against.
        with config_write_lock(path):
            file_teams = file_team_names(path)
            set_default_team(path, name)
    except TeamFileError as exc:
        raise click.ClickException(str(exc))
    echo(f"[syncmill] default_team = '{_shown(name)}' saved to {_shown(path)}")
    if name not in file_teams:
        # codex design-review Major: the team resolves via SYNCMILL_TEAMS only —
        # the persisted default dangles as soon as that env is absent.
        secho(
            f"[syncmill] note: team '{_shown(name)}' is not defined in {_shown(path)} (it "
            f"resolves via SYNCMILL_TEAMS) — without that env the default "
            f"dangles; "
            f"{printable_command('syncmill', 'team', 'save', args=[name], options=['-a', 'AGENT'])}"
            f" makes it durable.",
            fg="yellow",
        )
    if "SYNCMILL_DEFAULT_TEAM" in os.environ:
        secho(
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
        echo(
            "[syncmill] no run reports found — enable recording with "
            "SYNCMILL_INTEGRATION__REPORT__ENABLED=true (or [integration.report] "
            "enabled=true in syncmill.toml)."
        )
        return
    echo("Recorded runs (newest first):\n")
    for doc in docs:
        result = doc.get("result") or {}
        echo(
            f"  {_shown(doc.get('run_id'))}  {_shown(doc.get('created_at'))}  "
            f"{_shown(doc.get('strategy'))}  {_shown(result.get('status'))}"
            f"  {_shown(result.get('agent_id') or '—')}"
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
    echo(render_report(doc))


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
    # Lazy imports (like `status`) so importing the CLI doesn't pull the MCP SDK.
    from syncmill.gateway import build_gateway
    from syncmill.memory import SyncmillMemory

    config = _load_config()
    memory = SyncmillMemory(config.memory)

    # CRITICAL: under stdio, stdout is the JSON-RPC channel — EVERY human-facing
    # line must go to stderr (err=True) or it corrupts the protocol stream. The
    # command writes nothing to stdout itself.
    if memory.degraded:
        secho(
            f"[syncmill] memory unavailable — gateway will report shared reads as "
            f"unavailable ({_shown(memory.last_error)})",
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
        secho(
            f"[syncmill] work-board unavailable — board_* tools will report it ({_shown(exc)})",
            fg="yellow",
            err=True,
        )
    try:
        mcp = build_gateway(memory, board=board, board_error=board_error)
        if http:
            if host not in ("127.0.0.1", "localhost", "::1"):
                # Doubles as the operator cue that the SDK's automatic DNS-rebinding
                # protection engages only for loopback run-time hosts.
                secho(
                    f"[syncmill] WARNING: binding {_shown(host)}:{port} exposes read-only "
                    f"shared memory to the network with NO authentication — use a "
                    f"trusted network or an authenticated proxy.",
                    fg="red",
                    err=True,
                )
            else:
                secho(
                    f"[syncmill] serving shared memory over HTTP at "
                    f"http://{host}:{port}/mcp (read-only, no auth, loopback only)",
                    fg="yellow",
                    err=True,
                )
            # mcp 2.0: host/port are run-time transport kwargs (the SDK derives the
            # loopback DNS-rebinding policy from this host), not constructor args.
            mcp.run(transport="streamable-http", host=host, port=port)
        else:
            secho(
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


@cli.group("memory")
def memory_group() -> None:
    """Long-term memory maintenance commands."""


@memory_group.group("migration")
def memory_migration() -> None:
    """Plan, apply, verify, or roll back a backend migration."""


@memory_migration.command("plan")
@click.option(
    "source", "--from",
    type=click.Choice(["memtomem", "mem0", "langgraph"]), required=True,
)
@click.option(
    "target", "--to",
    type=click.Choice(["memtomem", "mem0", "langgraph"]), required=True,
)
@click.option("namespaces", "--namespace", multiple=True)
def memory_migration_plan(source: str, target: str, namespaces: tuple[str, ...]) -> None:
    """Create an immutable migration snapshot; never writes destination memories."""
    from syncmill.memory_migration import MigrationError, migration_root, plan_migration

    config = _load_config()
    try:
        migration_id, manifest = run_async(
            plan_migration(config, source, target, namespaces)  # type: ignore[arg-type]
        )
    except MigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        close_event_loop()
    echo(f"[syncmill] migration planned: {migration_id}")
    echo(f"  records: {_shown(manifest['record_count'])}")
    echo(f"  artifact: {_shown(migration_root() / migration_id)}")


@memory_migration.command("apply")
@click.argument("migration_id")
def memory_migration_apply(migration_id: str) -> None:
    """Apply or resume a planned source-retaining migration."""
    from syncmill.memory_migration import MigrationError, apply_migration, migration_root

    try:
        receipt = run_async(apply_migration(_load_config(), migration_id))
    except MigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        close_event_loop()
    echo(f"[syncmill] migration applied + verified: {migration_id}")
    echo(f"  records: {_shown(receipt['verified_count'])}")
    echo(f"  receipt: {_shown(migration_root() / migration_id / 'receipt.json')}")


@memory_migration.command("verify")
@click.argument("migration_id")
def memory_migration_verify(migration_id: str) -> None:
    """Recheck every ledger-owned destination record."""
    from syncmill.memory_migration import MigrationError, migration_root, verify_migration

    try:
        receipt = run_async(verify_migration(_load_config(), migration_id))
    except MigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        close_event_loop()
    echo(f"[syncmill] migration verified: {migration_id}")
    echo(f"  records: {_shown(receipt['verified_count'])}")
    echo(f"  receipt: {_shown(migration_root() / migration_id / 'receipt.json')}")


@memory_migration.command("rollback")
@click.argument("migration_id")
@click.option("yes", "--yes", is_flag=True, help="Confirm deletion of unchanged migrated target rows.")
def memory_migration_rollback(migration_id: str, yes: bool) -> None:
    """Delete only unchanged target rows recorded by this migration."""
    from syncmill.memory_migration import MigrationError, rollback_migration

    if not yes:
        raise click.UsageError("rollback requires --yes")
    try:
        result = run_async(rollback_migration(_load_config(), migration_id))
    except MigrationError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        close_event_loop()
    echo(f"[syncmill] migration rolled back: {migration_id}")
    echo(f"  deleted: {result['deleted']}")


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
            secho(f"{backend}: connected", fg="green")
            safe_status_keys = {
                "memtomem": ("db_path",),
                "mem0": ("data_dir", "vector_store", "collection", "embedder"),
                "langgraph": ("db_path", "store", "embedder"),
            }
            for key in safe_status_keys.get(backend, ()):
                if key in info:
                    value = info[key]
                    echo(f"  {key}: {_shown(value)}")
        else:
            secho(f"{backend}: disconnected", fg="red")
            echo(f"  error: {_shown(info.get('error'))}")
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
        raise click.ClickException(f"session failed to start: {exc}") from exc


@session.command("open")
@click.argument("agent", required=False, default=None)
@click.option(
    "--workspace",
    type=click.Choice(["isolated", "direct"]),
    default="isolated",
    show_default=True,
)
@click.option(
    "--repo", "repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
@click.option("--no-attach", is_flag=True, help="Start detached and return immediately.")
def session_open(agent: str | None, workspace: str, repo: Path, no_attach: bool) -> None:
    """Open a project-scoped isolated session; direct mode is explicit."""
    _require_tmux()
    root, config = _target_project_config(repo)
    root = Path(config.repo_root).expanduser().resolve()
    agent_id = agent or default_interactive_agent(config.agent_priority)
    if agent_id is None or agent_id not in interactive_agents():
        raise click.BadParameter(
            f"unknown interactive agent: {agent_id!r}. "
            f"Valid: {', '.join(interactive_agents())}",
            param_hint="AGENT",
        )
    if workspace == "direct":
        if no_attach:
            raise click.UsageError("--no-attach is supported only for isolated sessions")
        _require_tty()
        try:
            raise click.exceptions.Exit(start_session(agent_id, str(root)))
        except SessionLaunchError as exc:
            raise click.ClickException(f"session failed to start: {exc}") from exc
    if not no_attach:
        _require_tty()
    store = IsolatedSessionStore(config, root)
    try:
        value = store.create(agent_id)
        echo(
            f"[syncmill] isolated session {value['session_id']} in "
            f"{_shown(value['worktree_path'])}"
        )
        if no_attach:
            return
        raise click.exceptions.Exit(store.attach(value["session_id"]))
    except (IsolatedSessionError, SessionLaunchError, WorkspaceError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@session.command("isolated-list")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def session_isolated_list(repo: Path) -> None:
    root, config = _target_project_config(repo)
    try:
        target = Path(config.repo_root).expanduser().resolve()
        values = IsolatedSessionStore(config, target).list()
    except (ProjectError, ValidationError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    if not values:
        echo("[syncmill] no project-scoped isolated sessions")
        return
    for value in values:
        stale = " stale" if value.get("source_stale") else ""
        live = "live" if value.get("live") else "stopped"
        echo(
            f"{value['session_id']}  {_shown(value['agent_id']):10} {_shown(value['state']):18} "
            f"{live}{stale}"
        )


@session.command("attach-isolated")
@click.argument("session_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def session_attach_isolated(session_id: str, repo: Path) -> None:
    _require_tmux()
    _require_tty()
    root, config = _target_project_config(repo)
    try:
        target = Path(config.repo_root).expanduser().resolve()
        raise click.exceptions.Exit(IsolatedSessionStore(config, target).attach(session_id))
    except (ProjectError, ValidationError, IsolatedSessionError) as exc:
        raise click.ClickException(str(exc)) from exc


@session.command("checkpoint")
@click.argument("session_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def session_checkpoint(session_id: str, repo: Path) -> None:
    root, config = _target_project_config(repo)
    try:
        target = Path(config.repo_root).expanduser().resolve()
        run_id = IsolatedSessionStore(config, target).checkpoint(session_id)
    except (ProjectError, ValidationError, IsolatedSessionError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    echo(f"Checkpoint: {_shown(run_id)}")
    echo("Review: " + printable_command("syncmill", "diff", args=[run_id]))
    echo("Apply: " + printable_command("syncmill", "apply", args=[run_id]))


@session.command("close")
@click.argument("session_id")
@click.option(
    "--repo", default=".", type=click.Path(path_type=Path), help=_PROJECT_REPO_HELP
)
def session_close(session_id: str, repo: Path) -> None:
    _require_tmux()
    root, config = _target_project_config(repo)
    try:
        target = Path(config.repo_root).expanduser().resolve()
        IsolatedSessionStore(config, target).close_session(session_id)
    except (ProjectError, ValidationError, IsolatedSessionError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    echo(f"Closed isolated session {session_id}")


@session.command("list")
def session_list():
    """List active syncmill interactive sessions (name + attached state)."""
    _require_tmux()
    sessions = list_sessions()
    if not sessions:
        echo("[syncmill] no active sessions")
        return
    for info in sessions:
        echo(f"  {info.name}  ({'attached' if info.attached else 'detached'})")


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
    echo(f"[syncmill] stopped {killed} session(s)")


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
        secho(f"[syncmill] added item {item.id}", fg="green")
        echo(render_item(item))
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
        message = (
            f"[syncmill] imported {result.imported} review candidate(s); "
            f"skipped {result.skipped} existing"
        )
        if result.goal_kept:
            # Goal is assigned only on FIRST import — say so, or a re-import
            # under a new --goal reads as if the new goal took effect.
            message += f" ({result.goal_kept} kept their original goal)"
        secho(message, fg="green")
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
        echo(render_board(items))
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
        echo(render_item(item, bd.transitions(item_id)))
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
        echo(f"[syncmill] team: {_shown(team)}{via}")
    # Mutate default_strategy BEFORE building the Supervisor (it selects the strategy
    # in __init__) — the same pre-construction mutation `run` uses for --strategy;
    # the explicit flag beats the team profile's strategy.
    if strategy:
        config.default_strategy = strategy
    _validate_strategy(config)

    repo_path = Path(config.repo_root).resolve()
    cleanup_orphaned_worktrees(repo_path)
    sweep_orphaned_workspaces(
        Path(config.workspace.root), older_than=config.workspace.stale_seconds
    )
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
                secho(
                    f"[syncmill] ran item → {result.agent_id}: {result.status.value}",
                    fg="green" if ok else "yellow",
                )
                _surface_board_lease_warning(supervisor)
                if getattr(supervisor, "last_preflight_blocked", False):
                    secho(
                        "[syncmill] board loop stopped after strict preflight block",
                        fg="yellow",
                    )
                    break
            echo(f"[syncmill] board run drained {ran} item(s){goal_suffix}")
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
                    f"item not claimable (missing, or not in 'planned'): {_shown(item_id)}"
                    if item_id is not None
                    else f"nothing to run — no dispatchable item{goal_suffix}"
                )
                secho(f"[syncmill] {msg}", fg="yellow")
                return
            _surface_board_lease_warning(supervisor)
            _surface_board_degraded(supervisor)
            echo()
            exit_code = render_task_result(result)
            # The same report line plain `run` prints (the board block is already
            # stamped by run_item); the --loop path deliberately doesn't — many
            # runs, no single per-run render to anchor it to.
            if supervisor.last_report_path is not None:
                echo(f"[syncmill] report: {_shown(supervisor.last_report_path)}")
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
        secho(
            f"[syncmill] memory degraded — the board-result summary may not have "
            f"reached shared ({_shown(supervisor.memory.last_error)})",
            fg="yellow",
        )
    if supervisor.breaker is not None and supervisor.breaker.degraded:
        secho(
            f"[syncmill] circuit breaker state unavailable — not tracking failures "
            f"({_shown(supervisor.breaker.last_error)})",
            fg="yellow",
        )


def _surface_board_lease_warning(supervisor) -> None:
    """Tell the operator when a board run's work was discarded.

    `last_board_warning` is set (and reset) per `run_item`, so this is called once
    per item — inside the `--loop` body, not after it. Without it a run whose lease
    was reclaimed mid-flight exits 0, prints a normal result, and leaves no sign
    that the fence dropped its board write."""
    warning = getattr(supervisor, "last_board_warning", None)
    if warning:
        secho(
            f"[syncmill] {_shown(warning)} — its result was NOT recorded on the board",
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
        secho(f"[syncmill] updated item {_shown(item_id)}", fg="green")
        echo(render_item(updated))
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
        secho(f"[syncmill] recorded decision {decision.id}", fg="green")
        echo(render_decision(decision))
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
        echo(render_decisions(bd.active_decisions(goal=goal)))
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
        echo(render_board_stats(bd.stats()))
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


@board.command("compact")
@click.option(
    "--older-than",
    "older_than",
    default=30 * 86400,
    show_default=True,
    # Nonnegative: a negative window would mean "older than the future", i.e.
    # trim the entire audit — refused at the flag, like the store refuses it.
    type=click.FloatRange(min=0),
    help="Trim audit rows older than this many seconds; their stats aggregates "
    "(redispatched claim counts, stale reclaims) are folded into rollups first, "
    "so `board stats` is identical before and after.",
)
def board_compact_cmd(older_than: float):
    """Aggregate-then-trim the append-only transitions audit (#118).

    Manual only — nothing runs this automatically. Recent rows (newer than
    --older-than) always survive, so `board show` keeps recent history.
    """
    # FloatRange(min=0) admits signed negative zero (an underflowing negative
    # literal like -1e-999), which would trim as 0 — the whole audit — from an
    # input the user wrote as negative. Refuse it at the flag too; the store
    # re-checks independently.
    if math.copysign(1.0, older_than) < 0:
        raise click.BadParameter(
            "--older-than must be a nonnegative number of seconds",
            param_hint="--older-than",
        )
    config = _load_config()
    bd = _open_board(config)
    try:
        trimmed = bd.compact_transitions(older_than_seconds=older_than)
        echo(f"Compacted {trimmed} audit row(s)")
    except BoardError as exc:
        raise click.ClickException(str(exc))
    finally:
        bd.close()


# ── dashboard (Phase 3: live TUI over the work-board) ──────────────────────


@cli.command("tui")
@click.option(
    "--refresh",
    default=DEFAULT_REFRESH_SECONDS,
    type=float,
    show_default=True,
    help="Poll interval in seconds for TUI reads (min 0.5).",
)
@click.option(
    "--tab",
    default="auto",
    show_default=True,
    type=click.Choice(
        ("auto", "board", "runs", "reports", "teams", "status", "sessions", "rooms")
    ),
    help=(
        "Initial tab. `auto` opens the first available surface "
        "(Rooms → Board → Runs → Status)."
    ),
)
def tui(refresh: float, tab: str):
    """Full-screen Board/Runs/Reports/Teams/Status/Sessions/Rooms command center.

    Unlike ``dashboard``, this command opens in a limited mode when the board is
    disabled or unavailable. Board executions run in a detached durable worker,
    so quitting the UI does not cancel them. All commands support macOS/Linux;
    detached execution and Runs control use POSIX process groups and locks.
    """
    if not _stdout_isatty():
        raise click.ClickException(
            "tui needs an interactive terminal (stdout is not a TTY)."
        )
    config = _load_config()
    board_error: str | None = None
    bd = None
    if config.board.enabled:
        try:
            bd = build_board(config.board)
        except BoardError as exc:
            board_error = str(exc)
    from syncmill.tui import run_tui

    try:
        run_tui(
            config,
            board=bd,
            board_error=board_error,
            toml_path=toml_path(),
            refresh_seconds=refresh,
            initial_tab=tab,
        )
    finally:
        if bd is not None:
            bd.close()


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
