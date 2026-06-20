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
from typing import Protocol, runtime_checkable

from syncmill.config import GateConfig
from syncmill.models import GateCheck, GateReport
from syncmill.worktree import Worktree

logger = logging.getLogger(__name__)

# Per-check captured output is tailed to this many characters: a failing test run
# can emit megabytes, but only the tail (where the failure is) needs to reach the
# gate error / CLI without bloating a TaskResult.
_OUTPUT_LIMIT = 4000


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

    def __init__(self, commands: list[str], timeout_seconds: int):
        self._commands = list(commands)
        self._timeout = timeout_seconds

    def evaluate(self, worktree: Worktree) -> GateReport:
        checks = [self._run_one(cmd, worktree.path) for cmd in self._commands]
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
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                # errors="replace": a gate command may emit invalid UTF-8 (test
                # runners, binary diffs). Decoding must degrade to replacement
                # chars, never raise UnicodeDecodeError and abort the task — the
                # same policy the agent layer uses (agents.py).
                text=True,
                errors="replace",
                cwd=str(workdir),
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return failed(f"timed out after {self._timeout}s")
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


def build_gate(config: GateConfig) -> QualityGate | None:
    """The gate described by ``config``, or ``None`` when no commands are
    configured — the disabled default, where strategies skip gating entirely and
    behavior is unchanged."""
    if not config.commands:
        return None
    return CommandGate(config.commands, config.timeout_seconds)
