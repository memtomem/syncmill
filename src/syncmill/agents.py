"""Agent abstraction — wraps CLI tools as pure functions (prompt in, result out)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from syncmill.config import AgentConfig, SyncmillConfig
from syncmill.models import TaskResult, TaskStatus

# ANSI escape sequence stripper (pattern from autocli)
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _clean_output(raw: str | bytes | None) -> str:
    """ANSI-strip and trim output that may be str, bytes, or None.

    subprocess.run yields *bytes* in TimeoutExpired.stdout even with text=True,
    so the timeout path must decode before ANSI-stripping.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return _strip_ansi(raw).strip()


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class AgentRunner(Protocol):
    """Any CLI agent that can execute a coding task.

    Agents are pure functions: prompt in, TaskResult out.
    They know nothing about memtomem, other agents, or the Supervisor.

    Two execution paths, same contract and same result:

    * ``execute`` — synchronous (``subprocess.run``). Used by the sequential
      strategies (route, pipeline) and the sync Supervisor.
    * ``aexecute`` — asynchronous (``asyncio.create_subprocess_exec``). The path
      the concurrent strategies (compete, council) await to run agents at once;
      bridge it from sync code with ``memory.run_async`` and fan out via
      ``aexecute_isolated`` (one worktree per agent) or ``aexecute_all`` (one
      shared workdir).
    """

    @property
    def agent_id(self) -> str: ...

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult: ...

    async def aexecute(
        self, prompt: str, workdir: Path, timeout: int
    ) -> TaskResult: ...


# ── Base implementation ──────────────────────────────────────────────────


class CLIAgent:
    """Base class for CLI-based agents using subprocess.run."""

    def __init__(self, agent_id: str, config: AgentConfig):
        self._agent_id = agent_id
        self._config = config

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def _build_command(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def _parse_output(self, stdout: str) -> str:
        """Extract meaningful output from CLI stdout. Override per agent."""
        return _strip_ansi(stdout).strip()

    def _is_error_output(self, stdout: str) -> str | None:
        """Check if CLI output indicates an error. Override per agent.
        Returns error message if error, None if OK."""
        return None

    # ── Result building (shared by the sync + async paths) ─────────────────

    def _build_result(
        self,
        task_id: UUID,
        returncode: int,
        stdout: str,
        stderr: str,
        duration: float,
    ) -> TaskResult:
        """Classify a *completed* process into a TaskResult.

        The single source of truth for success/failure detection, so the sync
        (``execute``) and async (``aexecute``) paths can never drift: a CLI that
        exits 0 while signalling an in-band error (e.g. Claude's
        ``is_error``) fails the same way through either path.
        """
        output = self._parse_output(stdout)
        output_error = self._is_error_output(stdout)
        is_failed = returncode != 0 or output_error is not None
        error_msg = output_error or (_strip_ansi(stderr).strip() or None) if is_failed else None

        return TaskResult(
            task_id=task_id,
            agent_id=self._agent_id,
            status=TaskStatus.FAILED if is_failed else TaskStatus.COMPLETED,
            output=output,
            exit_code=returncode,
            duration_seconds=round(duration, 2),
            error=error_msg,
        )

    def _failure(
        self,
        task_id: UUID,
        *,
        output: str,
        error: str,
        duration: float,
        exit_code: int = -1,
    ) -> TaskResult:
        """Build a FAILED result for a launch/timeout error (no clean exit)."""
        return TaskResult(
            task_id=task_id,
            agent_id=self._agent_id,
            status=TaskStatus.FAILED,
            output=output,
            exit_code=exit_code,
            duration_seconds=round(duration, 2),
            error=error,
        )

    # ── Execution ──────────────────────────────────────────────────────────

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        task_id = uuid4()
        cmd = self._build_command(prompt)
        start = monotonic()

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(workdir),
                timeout=timeout,
                env={**os.environ},
            )
            return self._build_result(
                task_id, proc.returncode, proc.stdout, proc.stderr, monotonic() - start
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run already SIGKILLs the timed-out child before raising;
            # nothing further to terminate here.
            return self._failure(
                task_id,
                output=_clean_output(exc.stdout),
                error=f"Timed out after {timeout}s",
                duration=monotonic() - start,
            )
        except FileNotFoundError:
            return self._failure(
                task_id,
                output="",
                error=f"CLI tool '{cmd[0]}' not found in PATH",
                duration=0.0,
            )
        except OSError as exc:
            # PermissionError, bad cwd, ENOEXEC, etc. — don't crash the task.
            return self._failure(
                task_id,
                output="",
                error=f"Failed to launch '{cmd[0]}': {exc}",
                duration=monotonic() - start,
            )

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        """Async twin of ``execute`` via ``asyncio.create_subprocess_exec``.

        Same prompt-in / TaskResult-out contract and identical success/failure
        classification (both go through ``_build_result``/``_failure``). This is
        the path the concurrent strategies await.

        One deliberate divergence from the sync path: ``asyncio`` discards a
        child's buffered output when we kill it on timeout, so a timed-out
        ``aexecute`` reports empty ``output`` where ``execute`` keeps
        ``TimeoutExpired.stdout``. Everything else is byte-for-byte identical.
        """
        task_id = uuid4()
        cmd = self._build_command(prompt)
        start = monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env={**os.environ},
            )
        except FileNotFoundError:
            return self._failure(
                task_id,
                output="",
                error=f"CLI tool '{cmd[0]}' not found in PATH",
                duration=0.0,
            )
        except OSError as exc:
            return self._failure(
                task_id,
                output="",
                error=f"Failed to launch '{cmd[0]}': {exc}",
                duration=monotonic() - start,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            # wait_for cancelled communicate(); SIGKILL the child and reap it so
            # it can't linger as a zombie.
            await _terminate(proc)
            return self._failure(
                task_id,
                output="",
                error=f"Timed out after {timeout}s",
                duration=monotonic() - start,
            )

        return self._build_result(
            task_id,
            proc.returncode,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
            monotonic() - start,
        )


# ── Claude Code ──────────────────────────────────────────────────────────


class ClaudeAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("claude", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        return [
            "claude",
            "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(self._config.max_turns),
        ]

    def _parse_output(self, stdout: str) -> str:
        """Claude --output-format json → {"type":"result","result":"...",...}"""
        cleaned = _strip_ansi(stdout).strip()
        parsed = _try_parse_json(cleaned)
        if isinstance(parsed, dict):
            return parsed.get("result", "").strip()
        return cleaned

    def _is_error_output(self, stdout: str) -> str | None:
        parsed = _try_parse_json(_strip_ansi(stdout).strip())
        if isinstance(parsed, dict) and parsed.get("is_error"):
            return parsed.get("result", "Unknown Claude error")
        return None


# ── Gemini CLI ───────────────────────────────────────────────────────────


class GeminiAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("gemini", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        return [
            "gemini",
            "-p", prompt,
            "--output-format", "json",
            "--yolo",
        ]

    def _parse_output(self, stdout: str) -> str:
        """Gemini --output-format json returns a JSON object."""
        cleaned = _strip_ansi(stdout).strip()
        parsed = _try_parse_json(cleaned)
        if isinstance(parsed, dict):
            return parsed.get("modelResponse", parsed.get("response", json.dumps(parsed)))
        return cleaned


# ── Codex CLI ────────────────────────────────────────────────────────────


class CodexAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("codex", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        return [
            "codex", "exec",
            prompt,
            "--json",
            "--full-auto",
        ]

    def _parse_output(self, stdout: str) -> str:
        """Codex --json returns JSONL (one JSON object per line)."""
        cleaned = _strip_ansi(stdout).strip()
        messages: list[str] = []
        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                # Codex JSONL: look for agent_message items
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        text = item.get("text", "").strip()
                        if text:
                            messages.append(text)
            except json.JSONDecodeError:
                continue
        return "\n".join(messages) if messages else cleaned


# ── Concurrent execution ───────────────────────────────────────────────────


# How long to wait for a SIGKILLed child to be reaped before giving up. The
# child itself dies within milliseconds; this bound only guards the pathological
# case where it spawned a grandchild that inherited (and still holds) its
# stdout/stderr pipes, which can otherwise wedge proc.wait() indefinitely.
_REAP_TIMEOUT_SECONDS = 5.0


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL a timed-out child and reap it — best-effort, time-bounded, never
    raises, never hangs.

    After ``wait_for`` cancels ``communicate()`` on timeout, nothing drains the
    child's pipes; if the child spawned a grandchild that inherited them (the
    real agent CLIs do — MCP servers, language servers, shells), ``proc.wait()``
    can block forever waiting for those pipes to close. Bounding the reap turns
    that latent hang into a clean give-up — a transport may then be finalized at
    GC (a harmless "Event loop is closed" note on stderr at shutdown), and the
    orphaned grandchild is reparented to init, the same limitation as the sync
    path, which SIGKILLs only the direct child too.
    """
    try:
        proc.kill()
    except ProcessLookupError:
        return  # already exited between the timeout and the kill — nothing to reap
    try:
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        pass  # grandchild still holding the pipes — give up rather than hang
    except Exception:  # pragma: no cover - defensive reap
        pass


async def aexecute_all(
    agents: list[AgentRunner],
    prompt: str,
    workdir: Path,
    timeout: int,
) -> list[TaskResult]:
    """Run several agents on the same prompt concurrently; results in agent order.

    Part of the async foundation the concurrent strategies (compete/council)
    build on. Bridge it from sync orchestration code with ``memory.run_async``.

    Agent isolation is untouched — each agent still receives only
    ``(prompt, workdir, timeout)``. This variant runs every agent in the *same*
    ``workdir``, so it fits only agents meant to share one tree — concurrent
    agents that edit would clobber each other. For the isolated case (the
    compete/council shape), use ``aexecute_isolated``, which gives each agent its
    own worktree path; a single ``workdir`` can't express N worktrees (see the
    per-agent isolation test in test_worktree.py).
    """
    return await asyncio.gather(
        *(agent.aexecute(prompt, workdir, timeout) for agent in agents)
    )


async def aexecute_isolated(
    agents: list[AgentRunner],
    prompt: str,
    worktrees: list[Path],
    timeout_for: Callable[[str], int],
) -> list[TaskResult]:
    """Run several agents on the same prompt CONCURRENTLY, each in its OWN workdir.

    The isolated counterpart to ``aexecute_all`` (which runs every agent in one
    shared ``workdir`` — fine only when they don't edit, since concurrent editors
    would clobber). This is the shape the concurrent *compete* strategy needs:
    every agent edits a separate worktree, so their diffs never collide.

    The caller — a concurrent strategy that owns its worktree topology — acquires
    one fresh worktree per agent UP FRONT and SERIALLY (acquire is sync git that
    contends on the index lock; it must not run inside this fan-out) and passes
    the parallel ``worktrees`` paths here. ``timeout_for`` maps an agent id to its
    configured timeout, so neither ``WorktreePool`` nor ``SyncmillConfig`` leaks
    into the agent layer — agent isolation is untouched, each agent still receives
    only ``(prompt, workdir, timeout)``.

    ``worktrees`` is positionally aligned with ``agents`` (same length); ``gather``
    preserves submission order, so ``results[i]`` lines up with ``agents[i]`` and
    ``worktrees[i]`` — the caller zips them to diff the winner's own tree.

    Like ``aexecute_all`` this uses a plain ``gather`` (no ``return_exceptions``):
    it trusts ``aexecute``'s no-raise contract — every *expected* failure (missing
    binary, bad cwd, timeout) is already converted to a FAILED ``TaskResult``, so
    a competitor that merely fails simply loses. An ``aexecute`` that genuinely
    *raises* signals a programming error; like the sync path it surfaces rather
    than being masked into a FAILED result (don't swallow real bugs).
    """
    if len(worktrees) != len(agents):
        raise ValueError(
            f"aexecute_isolated needs one worktree per agent: "
            f"{len(agents)} agents, {len(worktrees)} worktrees."
        )
    return await asyncio.gather(
        *(
            agent.aexecute(prompt, wt, timeout_for(agent.agent_id))
            for agent, wt in zip(agents, worktrees)
        )
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _try_parse_json(text: str) -> dict | list | None:
    """Try to parse text as JSON. Returns parsed object or None."""
    # Try the whole text first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Try line-by-line for JSONL or mixed output
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith(("{", "[")):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


# ── Registry ─────────────────────────────────────────────────────────────

_AGENT_CONSTRUCTORS: dict[str, type[CLIAgent]] = {
    "claude": ClaudeAgent,
    "gemini": GeminiAgent,
    "codex": CodexAgent,
}


class AgentRegistry:
    """Manages agent instances ordered by config priority."""

    def __init__(self, config: SyncmillConfig):
        unknown = [a for a in config.agent_priority if a not in _AGENT_CONSTRUCTORS]
        if unknown:
            valid = ", ".join(sorted(_AGENT_CONSTRUCTORS))
            raise ValueError(
                f"Unknown agent id(s) in agent_priority: {', '.join(unknown)}. "
                f"Valid agents: {valid}."
            )
        self._agents: list[AgentRunner] = []
        for agent_id in config.agent_priority:
            agent_cfg = config.agent_config(agent_id)
            if not agent_cfg.enabled:
                continue
            self._agents.append(_AGENT_CONSTRUCTORS[agent_id](agent_cfg))

    def available(self) -> list[AgentRunner]:
        """Return enabled agents in priority order."""
        return list(self._agents)

    def get(self, agent_id: str) -> AgentRunner | None:
        """Get a specific agent by id."""
        for agent in self._agents:
            if agent.agent_id == agent_id:
                return agent
        return None
