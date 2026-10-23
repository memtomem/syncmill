"""Quality gates (#10) — validate an agent's work in its worktree before it's
accepted.

A gate runs *after* an agent finishes, against the diff it produced in its own
worktree — never as a pre-run liveness probe (that would violate the "no
``health_check`` on agents" invariant). It's reactive, like the rest of
syncmill's fault handling: run, then judge the output.

The gate is **plumbed through the strategy layer, never the agent layer** —
agents stay pure ``execute(prompt, workdir, timeout)`` functions and know nothing
about gates. The Supervisor builds the gate from config and injects it into the
strategy, which decides what a gate verdict means for its topology: RouteStrategy
falls back on failure, PipelineStrategy fails the chain, CompeteStrategy downranks
failers when picking a winner.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path
from time import monotonic
from typing import Collection, Protocol, runtime_checkable

from syncmill.agents import _WORKFLOW_COMMON_ENV, _isolated_process_env, _sync_run
from syncmill.config import GateConfig
from syncmill.models import GateCheck, GateReport
from syncmill.worktree import Worktree

logger = logging.getLogger(__name__)

# Per-check captured output is tailed to this many characters: a failing test run
# can emit megabytes, but only the tail (where the failure is) needs to reach the
# gate error / CLI without bloating a TaskResult.
_OUTPUT_LIMIT = 4000


def _gate_env(
    workdir: Path, allowlist: Collection[str] | None = None
) -> dict[str, str]:
    """The child environment for a gate check — the agents' isolation verbatim.

    Reused rather than rebuilt so "isolated" can only ever mean one thing: git
    routing and config injection stripped, syncmill's own run-identity IPC
    stripped, ``PWD`` pinned to the worktree. A private cross-import (the
    ``session.py`` → ``monitor.py`` precedent); ``agents`` imports nothing from
    here, so the graph stays acyclic.

    With an ``allowlist`` the isolated environment is narrowed further to the
    workflow's common variables plus the listed exact names. Narrowing comes
    after isolation, so no listed name can bring back what isolation removed,
    and no provider credential is ever added implicitly.
    """
    env = _isolated_process_env(Path(workdir))
    if allowlist is None:
        return env
    kept = _WORKFLOW_COMMON_ENV | set(allowlist) | {"PWD"}
    return {key: value for key, value in env.items() if key in kept}


@runtime_checkable
class QualityGate(Protocol):
    """Validates an agent's work in its worktree. Pure verdict: worktree in,
    ``GateReport`` out — like agents, it knows nothing about the Supervisor."""

    def evaluate(self, worktree: Worktree) -> GateReport: ...


class CommandGate:
    """Runs a fixed list of commands in the worktree; passes iff every one exits 0.

    Each command is parsed with ``shlex.split`` and run **without a shell** —
    matching how the rest of syncmill shells out (argv lists, no ``shell=True``).
    Chain checks by configuring multiple commands, not with ``&&``/pipes. A
    command that can't run (missing binary, bad syntax, timeout) counts as a
    failed check rather than crashing the gate — an unverifiable result must not
    be silently accepted, but it also must not abort the whole task.
    """

    def __init__(
        self,
        commands: list[str],
        timeout_seconds: int,
        *,
        total_timeout_seconds: float | None = None,
        env_allowlist: Collection[str] | None = None,
    ):
        self._commands = list(commands)
        self._timeout = timeout_seconds
        self._total_timeout = total_timeout_seconds
        self._env_allowlist = (
            None if env_allowlist is None else frozenset(env_allowlist)
        )
        self._deadline: float | None = None

    def evaluate(self, worktree: Worktree) -> GateReport:
        self._deadline = (
            monotonic() + self._total_timeout
            if self._total_timeout is not None
            else None
        )
        try:
            checks = [self._run_one(cmd, worktree.path) for cmd in self._commands]
        finally:
            self._deadline = None
        report = GateReport.from_checks(checks)
        logger.debug(
            "Gate on %s (%s): %s", worktree.agent_id, worktree.path, report.summary()
        )
        return report

    def _run_one(self, command: str, workdir: Path) -> GateCheck:
        start = monotonic()

        def failed(output: str) -> GateCheck:
            # Every non-clean-exit path reports the same shape — a failed check
            # with the elapsed time — so a launch/decode error is observable, not
            # a zero-duration mystery, and never crashes the gate.
            return GateCheck(
                command, passed=False, exit_code=-1, output=output,
                duration_seconds=round(monotonic() - start, 2),
            )

        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return failed(f"invalid command: {exc}")
        if not argv:
            return failed("empty command")
        timeout = self._timeout
        if self._deadline is not None:
            remaining = self._deadline - monotonic()
            if remaining <= 0:
                return failed("workflow deadline exceeded before gate command")
            timeout = min(timeout, remaining)
        try:
            # _sync_run: the owned-process-group runner — a timeout kills the
            # command's WHOLE tree, not just the direct child (a test runner's
            # spawned workers used to survive as orphans).
            proc = _sync_run(
                argv,
                capture_output=True,
                tail_chars=_OUTPUT_LIMIT,
                # errors="replace": a gate command may emit invalid UTF-8 (test
                # runners, binary diffs). Decoding must degrade to replacement
                # chars, never raise UnicodeDecodeError and abort the task — the
                # same policy the agent layer uses (agents.py).
                text=True,
                errors="replace",
                cwd=str(workdir),
                timeout=timeout,
                # A gate check is usually a test/lint/build command that reads the
                # repository, so an ambient GIT_DIR/GIT_WORK_TREE would have it
                # validate a different tree than the worktree we are judging, and
                # an ambient core.hooksPath would run someone else's script. Same
                # isolation the agent subprocesses get — run-identity IPC
                # included, since a gate command that invokes syncmill would
                # otherwise adopt the parent's monitor identity and overwrite its
                # envelope and result.json.
                env=_gate_env(workdir, self._env_allowlist),
            )
        except subprocess.TimeoutExpired:
            return failed(f"timed out after {timeout:g}s")
        except FileNotFoundError:
            # FileNotFoundError covers both a missing binary and a missing cwd;
            # check the workdir directly so the verdict names the real cause.
            if not Path(workdir).exists():
                return failed(f"working directory not found: {workdir}")
            return failed(f"command not found: {argv[0]}")
        except ValueError as exc:
            # An invalid argument (e.g. an embedded NUL byte) makes subprocess.run
            # raise ValueError, NOT OSError — it would otherwise escape every
            # handler and crash the gate. A bad command is a failed check.
            return failed(f"invalid command argument: {exc}")
        except OSError as exc:
            return failed(f"failed to launch: {exc}")
        combined = (proc.stdout + proc.stderr).strip()
        return GateCheck(
            command,
            passed=proc.returncode == 0,
            exit_code=proc.returncode,
            output=combined[-_OUTPUT_LIMIT:],
            duration_seconds=round(monotonic() - start, 2),
        )


def build_gate(config: GateConfig, *, total_timeout_seconds: float | None = None) -> QualityGate | None:
    """The gate described by ``config``, or ``None`` when no commands are
    configured — the disabled default, where strategies skip gating entirely and
    behavior is unchanged."""
    if not config.commands:
        return None
    return CommandGate(
        config.commands,
        config.timeout_seconds,
        total_timeout_seconds=total_timeout_seconds,
        env_allowlist=config.env_allowlist,
    )
