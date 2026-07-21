"""Agent abstraction — wraps CLI tools as pure functions (prompt in, result out)."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from time import monotonic
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from syncmill.config import AgentConfig, SyncmillConfig
from syncmill.git_env import GIT_REPOSITORY_ROUTING_ENV
from syncmill.models import (
    TaskResult,
    TaskStatus,
    ToolCallRecord,
    ToolOutcome,
    ToolTelemetry,
)

# ANSI escape sequence stripper (pattern from autocli)
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CWD_ROUTING_ENV = {"OLDPWD", "CDPATH"}


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


def _isolated_process_env(workdir: Path) -> dict[str, str]:
    """Build an environment that cannot route work outside ``workdir``."""
    excluded = GIT_REPOSITORY_ROUTING_ENV | _CWD_ROUTING_ENV | {"PWD"}
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    env["PWD"] = str(workdir.resolve())
    return env


# ── Live-output observability seam (#16) ──────────────────────────────────
#
# An AMBIENT seam, NOT a change to the agent contract: ``aexecute`` consults a
# ``ContextVar`` (default ``None`` → the unchanged ``communicate()`` fast path)
# and, when a sink is present, tees each output chunk to it as the agent streams
# — so a monitor can show live output in tmux while the agent stays a pure
# ``(prompt, workdir, timeout)`` function that knows nothing about the Supervisor
# (the way it already "knows" ``os.environ`` and stdout). The ``OutputSink``
# Protocol lives HERE, not in ``monitor.py``, precisely so ``agents.py`` imports
# nothing from ``monitor`` (the monitor imports this) and the graph stays acyclic.


@runtime_checkable
class OutputSink(Protocol):
    """A best-effort observer of an agent's incremental output (#16).

    ``feed`` receives raw output bytes (the same bytes ``aexecute`` accumulates
    for the result) as they stream, keyed by agent id so a single sink can route
    several concurrent agents. Contract: it **must not block and must never
    raise** — it runs inside the agent's drain loop on the shared event loop, so a
    blocking or throwing sink would stall or break the run (``aexecute`` guards
    the call defensively regardless).
    """

    def feed(self, agent_id: str, chunk: bytes) -> None: ...


_OUTPUT_SINK: contextvars.ContextVar[OutputSink | None] = contextvars.ContextVar(
    "syncmill_output_sink", default=None
)

_TOOL_CAPTURE: contextvars.ContextVar[
    tuple[bool, dict[str, dict[str, str]]]
] = contextvars.ContextVar("syncmill_tool_capture", default=(False, {}))

_PROVIDER_SESSIONS: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "syncmill_provider_sessions", default=None
)
_ACTIVE_PROVIDER_SESSION: contextvars.ContextVar[tuple[str | None, bool, bool]] = (
    contextvars.ContextVar("syncmill_active_provider_session", default=(None, False, False))
)


@contextlib.contextmanager
def provider_session_scope(
    enabled: bool = True,
    seed: dict[str, str] | None = None,
    fork: bool = False,
) -> Iterator[None]:
    """Keep provider-native sessions affine to agent + isolated worktree."""
    token = _PROVIDER_SESSIONS.set(
        {"sessions": {}, "seed": dict(seed or {}), "fork": fork}
        if enabled else None
    )
    try:
        yield
    finally:
        _PROVIDER_SESSIONS.reset(token)


@contextlib.contextmanager
def capture_tool_calls(
    enabled: bool, tool_name_map: dict[str, dict[str, str]] | None = None
) -> Iterator[None]:
    """Enable typed provider-event capture for agent tasks in this context."""
    token = _TOOL_CAPTURE.set((enabled, tool_name_map or {}))
    try:
        yield
    finally:
        _TOOL_CAPTURE.reset(token)


def normalize_tool_key(raw_name: str, mapping: dict[str, str] | None = None) -> str | None:
    """Resolve a provider MCP name to strict canonical ``server::tool`` form."""
    mapped = (mapping or {}).get(raw_name)
    candidate = mapped or raw_name
    if candidate.startswith("mcp__") and mapped is None:
        parts = candidate[5:].split("__")
        if len(parts) != 2:
            return None
        candidate = f"{parts[0]}::{parts[1]}"
    if (
        candidate.count("::") != 1
        or any(not part for part in candidate.split("::"))
        or any(ch.isspace() for ch in candidate)
    ):
        return None
    return candidate


def _json_lines(stdout: str) -> tuple[list[dict], int]:
    events: list[dict] = []
    malformed = 0
    for raw in _strip_ansi(stdout).splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            malformed += 1
    return events, malformed


def _claude_events(stdout: str) -> tuple[list[dict], int]:
    """Normalize any Claude ``-p`` transcript shape into an ordered event list.

    Claude emits three wire shapes depending on flags and CLI version: a single
    JSON object (``--output-format json``), JSONL (``--output-format
    stream-json``), and — since 2.1.x — a top-level JSON *array* of those same
    events. Parsing each shape separately is what let an array-shaped exit-0
    rate-limit response read as a success: the dict-only checks never saw the
    ``is_error``/``session_id``/``result`` fields buried in the array.

    Returns ``(events, malformed_line_count)``; the count is meaningful only for
    the JSONL shape, where a dropped line means genuinely lost telemetry.
    """
    cleaned = _strip_ansi(stdout).strip()
    if not cleaned:
        return [], 0
    parsed = None
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return [parsed], 0
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)], 0
    return _json_lines(cleaned)


def _claude_error_message(event: dict) -> str | None:
    """Extract a human-readable error from an event, or ``None`` if it is not one.

    Two error encodings exist: the in-band ``is_error`` flag on a result event
    (Claude can exit 0 while setting it), and a top-level ``type:"error"`` event
    carrying the provider's message — the shape a 429 takes in the array
    transcript. The message is preserved verbatim rather than classified; rate
    limiting is deliberately not given retry semantics here, so a rate-limited
    agent fails like any other and the existing breaker/fallback paths apply.
    """
    if event.get("is_error") and event.get("type") in (None, "result"):
        result = event.get("result")
        return result if isinstance(result, str) and result.strip() else "Unknown Claude error"
    if event.get("type") == "error":
        error = event.get("error")
        message = error.get("message") if isinstance(error, dict) else event.get("message")
        return message if isinstance(message, str) and message.strip() else "Unknown Claude error"
    return None


@contextlib.contextmanager
def stream_to(sink: OutputSink | None) -> Iterator[None]:
    """Route agents' incremental output to ``sink`` for the duration of the block.

    A monitor wraps a concurrent strategy's fan-out in this; every agent task the
    fan-out creates inherits the ``ContextVar`` (asyncio copies the current
    context when it creates a Task, and ``run_async`` drives the loop from this
    same synchronous frame), so each ``aexecute`` tees to ``sink``. ``None`` (the
    default) restores the unchanged fast path — used by no-op callers/tests.
    """
    token = _OUTPUT_SINK.set(sink)
    try:
        yield
    finally:
        _OUTPUT_SINK.reset(token)


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class AgentRunner(Protocol):
    """Any CLI agent that can execute a coding task.

    Agents are pure functions: prompt in, TaskResult out.
    They know nothing about memory backends, other agents, or the Supervisor.

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

    def _parse_tool_telemetry(self, stdout: str) -> ToolTelemetry:
        return ToolTelemetry(source=self._agent_id, complete=True)

    def _provider_session_from_output(self, stdout: str) -> str | None:
        return None

    def _session_for(self, workdir: Path) -> tuple[str | None, bool, str]:
        state = _PROVIDER_SESSIONS.get()
        key = f"{self._agent_id}\0{workdir.resolve()}"
        if state is None:
            return None, False, key
        sessions = state["sessions"]
        if key in sessions:
            return sessions[key], True, key
        seeded = state["seed"].get(self._agent_id)
        if seeded:
            sessions[key] = seeded
            return seeded, True, key
        if self._agent_id == "claude":
            session_id = str(uuid4())
            sessions[key] = session_id
            return session_id, False, key
        return None, False, key

    def _attach_provider_session(
        self, result: TaskResult, stdout: str, key: str, initial: str | None
    ) -> None:
        session_id = self._provider_session_from_output(stdout) or initial
        state = _PROVIDER_SESSIONS.get()
        if session_id and state is not None:
            state["sessions"][key] = session_id
        if session_id:
            result.provider_session_id = session_id
            result.conversation_id = session_id

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

        capture_enabled, _ = _TOOL_CAPTURE.get()
        telemetry = self._parse_tool_telemetry(stdout) if capture_enabled else None

        return TaskResult(
            task_id=task_id,
            agent_id=self._agent_id,
            status=TaskStatus.FAILED if is_failed else TaskStatus.COMPLETED,
            output=output,
            exit_code=returncode,
            duration_seconds=round(duration, 2),
            error=error_msg,
            tool_telemetry=telemetry,
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
        session_id, resume, session_key = self._session_for(workdir)
        state = _PROVIDER_SESSIONS.get()
        token = _ACTIVE_PROVIDER_SESSION.set(
            (session_id, resume, bool(state and state.get("fork")))
        )
        try:
            cmd = self._build_command(prompt)
        finally:
            _ACTIVE_PROVIDER_SESSION.reset(token)
        start = monotonic()

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(workdir),
                timeout=timeout,
                env=_isolated_process_env(workdir),
            )
            result = self._build_result(
                task_id, proc.returncode, proc.stdout, proc.stderr, monotonic() - start
            )
            self._attach_provider_session(result, proc.stdout, session_key, session_id)
            return result
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

        Two deliberate divergences from the sync path:

        * ``asyncio`` discards a child's buffered output when we kill it on
          timeout, so a timed-out ``aexecute`` reports empty ``output`` where
          ``execute`` keeps ``TimeoutExpired.stdout``.
        * When a live-monitor sink is active (#16), output is teed chunk-by-chunk
          to the sink as it streams (``_communicate_teed``) instead of read in one
          shot by ``communicate()``. The ``TaskResult`` is identical either way
          (same accumulated bytes), so the contract doesn't change — but on a
          *teed timeout* the sink (hence the tmux log) has already seen the
          pre-timeout bytes even though the result's ``output`` stays empty: the
          log is a live VIEW, the result is the CONTRACT. With NO sink (the
          default), this is byte-for-byte the original ``communicate()`` path.
        """
        task_id = uuid4()
        session_id, resume, session_key = self._session_for(workdir)
        state = _PROVIDER_SESSIONS.get()
        token = _ACTIVE_PROVIDER_SESSION.set(
            (session_id, resume, bool(state and state.get("fork")))
        )
        try:
            cmd = self._build_command(prompt)
        finally:
            _ACTIVE_PROVIDER_SESSION.reset(token)
        start = monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=_isolated_process_env(workdir),
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

        # Default (sink None): the original communicate(). With a sink, tee each
        # chunk to it as it streams while accumulating the same bytes for the
        # result — so a live monitor sees output as it happens.
        sink = _OUTPUT_SINK.get()
        comm = (
            proc.communicate()
            if sink is None
            else _communicate_teed(proc, sink, self._agent_id)
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(comm, timeout=timeout)
        except asyncio.TimeoutError:
            # wait_for cancelled communicate()/the tee; SIGKILL the child and reap
            # it so it can't linger as a zombie.
            await _terminate(proc)
            if sink is not None:
                # The pane already shows the pre-timeout bytes; annotate so it
                # self-explains (the result's output stays empty — see docstring).
                with contextlib.suppress(Exception):
                    sink.feed(
                        self._agent_id,
                        f"\n[syncmill] timed out after {timeout}s\n".encode(),
                    )
            return self._failure(
                task_id,
                output="",
                error=f"Timed out after {timeout}s",
                duration=monotonic() - start,
            )
        except asyncio.CancelledError:
            # Cancelled mid-flight — e.g. a sibling in the concurrent fan-out
            # (aexecute_isolated) raised and is draining us. SIGKILL+reap our child
            # first so no agent subprocess outlives the cancelled task, then let
            # the cancellation propagate (never swallow it). `proc.kill()` inside
            # `_terminate` fires before any await, so the signal lands even if the
            # reap is itself cut short.
            await _terminate(proc)
            raise

        # proc.returncode is an int on every normal path; it can be None ONLY when a
        # teed bounded reap timed out (a grandchild holding the pipes). Coerce that to
        # -1 — the codebase's "no clean exit" sentinel — so exit_code stays an int and
        # the run classifies FAILED (we never confirmed a clean exit).
        returncode = proc.returncode if proc.returncode is not None else -1
        result = self._build_result(
            task_id,
            returncode,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
            monotonic() - start,
        )
        self._attach_provider_session(
            result,
            stdout_b.decode("utf-8", errors="replace"),
            session_key,
            session_id,
        )
        return result


# ── Claude Code ──────────────────────────────────────────────────────────


class ClaudeAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("claude", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        capture_enabled, _ = _TOOL_CAPTURE.get()
        session_id, resume, fork = _ACTIVE_PROVIDER_SESSION.get()
        return [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json" if capture_enabled else "json",
            *(["--verbose"] if capture_enabled else []),
            "--max-turns", str(self._config.max_turns),
            *(
                ["--resume", session_id]
                if session_id and resume
                else ["--session-id", session_id]
                if session_id
                else []
            ),
            *(["--fork-session"] if fork and resume else []),
        ]

    def _provider_session_from_output(self, stdout: str) -> str | None:
        for event in reversed(_claude_events(stdout)[0]):
            value = event.get("session_id")
            if isinstance(value, str) and value:
                return value
        return None

    def _parse_output(self, stdout: str) -> str:
        """Last result text across object / array / JSONL transcripts alike."""
        cleaned = _strip_ansi(stdout).strip()
        events, _ = _claude_events(stdout)
        results = [
            event["result"].strip()
            for event in events
            if event.get("type") == "result"
            and isinstance(event.get("result"), str)
            and event["result"].strip()
        ]
        if results:
            return results[-1]
        # A result event that carries no usable text means "Claude answered with
        # nothing", which is not the same as "this output was never JSON" — only
        # the latter falls back to the raw transcript.
        if any(event.get("type") == "result" for event in events):
            return ""
        texts = [
            event["result"].strip()
            for event in events
            if isinstance(event.get("result"), str) and event["result"].strip()
        ]
        return texts[-1] if texts else cleaned

    def _is_error_output(self, stdout: str) -> str | None:
        for event in reversed(_claude_events(stdout)[0]):
            message = _claude_error_message(event)
            if message is not None:
                return message
        return None

    def _parse_tool_telemetry(self, stdout: str) -> ToolTelemetry:
        events, malformed = _claude_events(stdout)
        mapping = _TOOL_CAPTURE.get()[1].get(self._agent_id, {})
        pending: dict[str, tuple[int, str]] = {}
        calls: list[ToolCallRecord] = []
        unqualified = 0
        sequence = 0
        for event in events:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    raw_name, call_id = item.get("name"), item.get("id")
                    if not isinstance(raw_name, str) or not raw_name.startswith("mcp__"):
                        continue
                    tool_key = normalize_tool_key(raw_name, mapping)
                    if tool_key is None or not isinstance(call_id, str):
                        unqualified += 1
                        continue
                    sequence += 1
                    pending[call_id] = (sequence, tool_key)
                elif item.get("type") == "tool_result":
                    call_id = item.get("tool_use_id")
                    entry = pending.pop(call_id, None) if isinstance(call_id, str) else None
                    if entry is None:
                        continue
                    outcome = (
                        ToolOutcome.FAILED
                        if item.get("is_error") is True
                        else ToolOutcome.SUCCEEDED
                    )
                    calls.append(ToolCallRecord(entry[0], entry[1], outcome))
        for sequence_no, tool_key in pending.values():
            calls.append(ToolCallRecord(sequence_no, tool_key, ToolOutcome.UNKNOWN))
        calls.sort(key=lambda call: call.sequence)
        return ToolTelemetry(
            source=self._agent_id,
            complete=malformed == 0 and unqualified == 0 and not pending,
            calls=tuple(calls),
            malformed_count=malformed,
            unqualified_count=unqualified,
        )


# ── Kimi Code CLI (kimi) ────────────────────────────────────────────────────


class KimiCodeAgent(CLIAgent):
    """Kimi Code. The product is ``kimi-code`` (so is the agent id) but the
    binary is ``kimi``. In ``--prompt`` mode it auto-approves tool use (the
    ``--yolo``/``--auto`` flags are *rejected* alongside ``--prompt``), and
    ``--output-format stream-json`` emits JSONL, one event per line:

        {"role":"assistant","tool_calls":[...]}        # tool call — no text
        {"role":"tool","tool_call_id":...,"content":...} # tool result
        {"role":"assistant","content":"..."}            # assistant text ← keep
        {"role":"meta","type":"session.resume_hint",...,"content":...} # ignore

    We keep only ``role == "assistant"`` lines that carry a text ``content`` —
    the ``meta`` resume-hint line also has a ``content`` field, so filtering on
    role (not merely on the presence of ``content``) is what excludes it.
    Errors surface as a non-zero exit (like Codex), so no ``_is_error_output``.
    """

    def __init__(self, config: AgentConfig | None = None):
        super().__init__("kimi-code", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        session_id, resume, _ = _ACTIVE_PROVIDER_SESSION.get()
        return [
            "kimi",
            "-p", prompt,
            "--output-format", "stream-json",
            *(["--session", session_id] if session_id and resume else []),
        ]

    def _provider_session_from_output(self, stdout: str) -> str | None:
        for event in reversed(_json_lines(stdout)[0]):
            if event.get("role") != "meta" or event.get("type") != "session.resume_hint":
                continue
            for key in ("session_id", "session", "id"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    return value
            content = event.get("content")
            if isinstance(content, str):
                match = re.search(
                    r"(?:--session|-S|-r)(?:=|\s+)([^\s]+)", content
                )
                if match:
                    return match.group(1).strip("'\"")
        return None

    def _parse_output(self, stdout: str) -> str:
        """kimi --output-format stream-json returns JSONL; collect the assistant
        text messages in order."""
        cleaned = _strip_ansi(stdout).strip()
        messages: list[str] = []
        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("role") != "assistant":
                continue
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                messages.append(content.strip())
        return "\n".join(messages) if messages else cleaned

    def _parse_tool_telemetry(self, stdout: str) -> ToolTelemetry:
        events, malformed = _json_lines(stdout)
        mapping = _TOOL_CAPTURE.get()[1].get(self._agent_id, {})
        pending: dict[str, tuple[int, str]] = {}
        calls: list[ToolCallRecord] = []
        unqualified = 0
        sequence = 0
        for event in events:
            if event.get("role") == "assistant" and isinstance(event.get("tool_calls"), list):
                for item in event["tool_calls"]:
                    if not isinstance(item, dict):
                        continue
                    function = item.get("function")
                    raw_name = (
                        function.get("name")
                        if isinstance(function, dict)
                        else item.get("name")
                    )
                    call_id = item.get("id") or item.get("tool_call_id")
                    if not isinstance(raw_name, str) or not raw_name.startswith("mcp__"):
                        continue
                    tool_key = normalize_tool_key(raw_name, mapping)
                    if tool_key is None or not isinstance(call_id, str):
                        unqualified += 1
                        continue
                    sequence += 1
                    pending[call_id] = (sequence, tool_key)
            elif event.get("role") == "tool":
                call_id = event.get("tool_call_id")
                entry = pending.pop(call_id, None) if isinstance(call_id, str) else None
                if entry is None:
                    continue
                status = event.get("status")
                if event.get("is_error") is True or status in {"failed", "error"}:
                    outcome = ToolOutcome.FAILED
                elif event.get("is_error") is False or status in {
                    "completed", "success", "succeeded"
                }:
                    outcome = ToolOutcome.SUCCEEDED
                else:
                    outcome = ToolOutcome.UNKNOWN
                calls.append(ToolCallRecord(entry[0], entry[1], outcome))
        for sequence_no, tool_key in pending.values():
            calls.append(ToolCallRecord(sequence_no, tool_key, ToolOutcome.UNKNOWN))
        calls.sort(key=lambda call: call.sequence)
        return ToolTelemetry(
            source=self._agent_id,
            complete=(
                malformed == 0
                and unqualified == 0
                and not pending
                and all(call.outcome != ToolOutcome.UNKNOWN for call in calls)
            ),
            calls=tuple(calls),
            malformed_count=malformed,
            unqualified_count=unqualified,
        )


# ── Codex CLI ────────────────────────────────────────────────────────────


class CodexAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("codex", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        session_id, resume, _ = _ACTIVE_PROVIDER_SESSION.get()
        if session_id and resume:
            return [
                "codex", "exec", "resume", session_id, prompt,
                "--json",
            ]
        return [
            "codex", "exec",
            prompt,
            "--json",
            "--full-auto",
        ]

    def _provider_session_from_output(self, stdout: str) -> str | None:
        for event in _json_lines(stdout)[0]:
            if event.get("type") == "thread.started":
                value = event.get("thread_id") or event.get("threadId")
                if isinstance(value, str) and value:
                    return value
        return None

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

    def _parse_tool_telemetry(self, stdout: str) -> ToolTelemetry:
        events, malformed = _json_lines(stdout)
        calls: list[ToolCallRecord] = []
        seen: set[str] = set()
        unqualified = 0
        sequence = 0
        for event in events:
            if event.get("type") != "item.completed":
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
                continue
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id in seen:
                continue
            if isinstance(item_id, str):
                seen.add(item_id)
            server, tool = item.get("server"), item.get("tool")
            tool_key = (
                normalize_tool_key(f"{server}::{tool}")
                if isinstance(server, str) and isinstance(tool, str)
                else None
            )
            if tool_key is None:
                unqualified += 1
                continue
            status = item.get("status")
            outcome = (
                ToolOutcome.SUCCEEDED
                if status == "completed"
                else ToolOutcome.FAILED
                if status == "failed"
                else ToolOutcome.UNKNOWN
            )
            sequence += 1
            calls.append(ToolCallRecord(sequence, tool_key, outcome))
        return ToolTelemetry(
            source=self._agent_id,
            complete=(
                malformed == 0
                and unqualified == 0
                and all(call.outcome != ToolOutcome.UNKNOWN for call in calls)
            ),
            calls=tuple(calls),
            malformed_count=malformed,
            unqualified_count=unqualified,
        )


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


async def _communicate_teed(
    proc: asyncio.subprocess.Process, sink: OutputSink, agent_id: str
) -> tuple[bytes, bytes]:
    """``proc.communicate()`` that also tees each chunk to ``sink`` as it streams.

    The live-monitor (#16) counterpart to ``communicate()``: it drains stdout and
    stderr *concurrently* (the same reason ``communicate()`` does — reading one to
    EOF before the other can deadlock on a full pipe buffer), feeding every chunk
    to ``sink`` and accumulating the identical bytes, so the resulting
    ``TaskResult`` is unchanged. ``sink.feed`` is guarded — a misbehaving sink
    must never break the run (its contract is "must not block and must never
    raise", but we defend anyway).

    The reap after both pipes EOF is **bounded** (``wait_for`` +
    ``_REAP_TIMEOUT_SECONDS``), exactly like ``_terminate``: a child that spawned
    a grandchild holding the pipes could otherwise wedge ``proc.wait()`` forever
    on the monitored happy path — the very hang the sync/async paths already pay
    to bound. A reap that times out leaves ``proc.returncode`` ``None``, which
    ``_build_result`` classifies as a failure (non-zero), the safe outcome.
    """

    async def _drain(stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:  # pragma: no cover - PIPE always gives a reader
            return b""
        chunks: list[bytes] = []
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            # `except Exception` (not BaseException) deliberately: a CancelledError
            # from a timeout/sibling-drain must propagate, never be swallowed here.
            try:
                sink.feed(agent_id, chunk)
            except Exception:
                pass
        return b"".join(chunks)

    stdout_b, stderr_b = await asyncio.gather(_drain(proc.stdout), _drain(proc.stderr))
    try:
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        pass  # grandchild still holding the pipes — give up rather than hang
    return stdout_b, stderr_b


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

    Result classification trusts ``aexecute``'s no-raise contract — every
    *expected* failure (missing binary, bad cwd, timeout) is already converted to
    a FAILED ``TaskResult``, so a competitor that merely fails simply loses. An
    ``aexecute`` that genuinely *raises* signals a programming error; like the
    sync path it surfaces rather than being masked into a FAILED result (don't
    swallow real bugs) — but only after the siblings are cancelled and drained, so
    none outlive the failed batch on the reused loop (see ``_gather_or_drain``).
    """
    if len(worktrees) != len(agents):
        raise ValueError(
            f"aexecute_isolated needs one worktree per agent: "
            f"{len(agents)} agents, {len(worktrees)} worktrees."
        )
    tasks = [
        asyncio.ensure_future(agent.aexecute(prompt, wt, timeout_for(agent.agent_id)))
        for agent, wt in zip(agents, worktrees)
    ]
    return await _gather_or_drain(tasks)


async def aexecute_each(
    agents: list[AgentRunner],
    prompts: list[str],
    worktrees: list[Path],
    timeout_for: Callable[[str], int],
) -> list[TaskResult]:
    """Run several agents CONCURRENTLY with a DISTINCT prompt each, one per worktree.

    The per-item-prompt counterpart to ``aexecute_isolated`` (which broadcasts ONE
    prompt to every agent). This is the shape ``DecomposeStrategy`` needs: each
    subtask is a *different* prompt routed to a (possibly different) agent in its own
    isolated worktree, so the subtasks' diffs never collide.

    ``agents``, ``prompts``, and ``worktrees`` are positionally aligned (same
    length); ``gather`` preserves submission order, so ``results[i]`` lines up with
    ``agents[i]`` / ``prompts[i]`` / ``worktrees[i]``. Agent isolation is untouched —
    each agent still receives only ``(prompt, workdir, timeout)``, with
    ``timeout_for`` mapping an agent id to its configured timeout, so neither the
    ``WorktreePool`` nor ``SyncmillConfig`` leaks into the agent layer.

    Shares ``aexecute_isolated``'s no-raise / cancel-drain contract (via
    ``_gather_or_drain``): every *expected* failure is already a FAILED
    ``TaskResult``, and an ``aexecute`` that genuinely *raises* is surfaced — but
    only after the siblings are cancelled and drained, so none outlive the failed
    batch on the reused loop.
    """
    if not (len(agents) == len(prompts) == len(worktrees)):
        raise ValueError(
            f"aexecute_each needs aligned agents/prompts/worktrees: {len(agents)} "
            f"agents, {len(prompts)} prompts, {len(worktrees)} worktrees."
        )
    tasks = [
        asyncio.ensure_future(agent.aexecute(prompt, wt, timeout_for(agent.agent_id)))
        for agent, prompt, wt in zip(agents, prompts, worktrees)
    ]
    return await _gather_or_drain(tasks)


async def _gather_or_drain(
    tasks: list[asyncio.Future[TaskResult]],
) -> list[TaskResult]:
    """Gather concurrent agent tasks; on ANY exception cancel + drain the siblings
    before re-raising the original unchanged.

    The shared tail of ``aexecute_isolated``/``aexecute_each``. A plain ``gather``
    would propagate the first exception while the siblings keep running, leaving
    live tasks/subprocesses on the *reused* event loop that could resume mid-cleanup
    (after the caller tore their worktrees down). Cancelling and draining first —
    ``aexecute`` reaps its child on cancellation — guarantees none outlive the failed
    batch. This subtle, safety-critical logic lives in one place so the two fan-out
    helpers can't drift.
    """
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


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
    "codex": CodexAgent,
    "kimi-code": KimiCodeAgent,
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
