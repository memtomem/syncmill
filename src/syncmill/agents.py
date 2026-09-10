"""Agent abstraction — wraps CLI tools as pure functions (prompt in, result out)."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import codecs
import io
import locale
import selectors
import json
import os
import re
import signal
import subprocess
from collections.abc import Callable, Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import NamedTuple, Protocol, runtime_checkable
from uuid import UUID, uuid4

from syncmill.platform_support import require_supported_platform
from syncmill.config import AgentConfig, SyncmillConfig
from syncmill.git_env import is_git_routing_var
from syncmill.models import (
    TaskResult,
    TaskStatus,
    TokenUsage,
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


#: syncmill's own run-identity IPC, stripped from every agent subprocess.
#: Duplicated as literals rather than imported so ``agents`` keeps importing
#: nothing from ``monitor`` (the monitor imports this module; the reverse would
#: cycle) — ``session.py`` strips the same pair for the same reason. An agent
#: that inherits them and then invokes syncmill itself would satisfy the inner
#: monitor's self-detection and adopt the parent's run id, overwriting the
#: parent's envelope and result.json with its own nested run.
_RUN_IPC_ENV = {"SYNCMILL_MONITOR_DIR", "SYNCMILL_RUN_ID"}

#: Launch each agent CLI as its own session/process-group leader (POSIX only —
#: the flag is unsupported elsewhere). Two effects, both load-bearing: a timeout
#: kill can reach the child's *grandchildren* (real agent CLIs spawn MCP/language
#: servers that inherit the stdout/stderr pipes — killing only the direct child
#: leaves them holding the pipes, wedging the drain until the full timeout, and
#: alive after syncmill exits); and the child is detached from the controlling
#: terminal, so a CLI that prompts interactively (auth expiry, "continue? [y/N]")
#: can never read the user's tty — under a concurrent fan-out N agents would all
#: race for it. ``rooms.py``/``tui_jobs.py`` already launch their children this
#: way; this aligns the agent paths.
_START_NEW_SESSION = os.name == "posix"

#: C0 controls other than tab/newline/CR, plus DEL and the C1 range: the
#: characters a terminal can interpret as control sequences (ESC/CSI/OSC), and
#: — for NUL specifically — the ones no argv element may contain at all.
PROMPT_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def scrub_prompt_text(text: str) -> str:
    """Drop characters that cannot ride an argv element (or a terminal) safely.

    A NUL survives strict UTF-8 but makes ``Popen``/``create_subprocess_exec``
    raise ``ValueError("embedded null byte")`` on the argv element — an
    exception class the launch handlers do not catch, so it escapes the strategy
    and freezes the whole run as ``recovery_required`` before any agent starts.
    Terminal controls (ESC/CSI/OSC, C1) are dropped for the same reason
    ``rooms._validate_prompt_text`` rejects them: recalled text is untrusted and
    is echoed by the CLI/TUI display paths.

    ``rooms`` VALIDATES (a human typed the room prompt, so a rejection is
    actionable); this SCRUBS, because its input is best-effort recalled memory
    that no user can correct.
    """
    return PROMPT_CONTROL_CHARS.sub("", text)


def _drop_transcript_caches() -> None:
    """Release the transcript memo once a result is built.

    The single-entry caches exist only to collapse the five accessor calls of
    ONE result-construction span into one parse; after the result is attached
    they would otherwise pin the last (potentially tens-of-MB) transcript plus
    its parsed event graph for the lifetime of a long-lived process (`board run
    --loop`, the TUI) — codex round-1 major.
    """
    _claude_events.cache_clear()
    _json_lines.cache_clear()


class OutputLimitExceeded(RuntimeError):
    """The provider's combined raw output exceeded its configured budget."""


def _bounded_sync_run(
    cmd: list[str], *, timeout: int, output_max_bytes: int | None,
    tail_chars: int | None, **kwargs,
) -> subprocess.CompletedProcess:
    """Drain both pipes in fixed chunks, retaining only the requested budget."""
    text_mode = kwargs.pop("text", False)
    text_mode = kwargs.pop("universal_newlines", False) or text_mode
    encoding = kwargs.pop("encoding", None) or locale.getencoding()
    errors = kwargs.pop("errors", None) or "strict"
    kwargs.pop("capture_output", None)
    kwargs.setdefault("start_new_session", _START_NEW_SESSION)
    buffers = [bytearray(), bytearray()]
    tails = ["", ""]
    decoders = [
        io.IncrementalNewlineDecoder(codecs.getincrementaldecoder(encoding)(errors), True)
        for _ in range(2)
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    total = 0
    deadline = monotonic() + timeout

    def output(index: int):
        if tail_chars is not None:
            return tails[index]
        raw = bytes(buffers[index])
        if text_mode:
            return raw.decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n")
        return raw

    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((proc.stdout, proc.stderr)):
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout, output(0), output(1))
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 4096)
                    index = key.data
                    total += len(chunk)
                    if output_max_bytes is not None and total > output_max_bytes:
                        raise OutputLimitExceeded(f"output limit exceeded ({output_max_bytes} bytes)")
                    if tail_chars is not None:
                        decoded = decoders[index].decode(chunk, final=not chunk)
                        tails[index] = (tails[index] + decoded)[-tail_chars:]
                    else:
                        buffers[index].extend(chunk)
                    if not chunk:
                        selector.unregister(key.fileobj)
            # Both pipes are at EOF but the leader may still be running (a
            # grandchild closed the inherited fds first). Popen.wait builds its
            # own TimeoutExpired with output=None, which would drop the bytes we
            # already drained — re-raise with them so this path keeps the same
            # "sync timeouts retain their bounded partial stdout" contract the
            # deadline branch above honors.
            try:
                proc.wait(timeout=max(0.001, deadline - monotonic()))
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(
                    cmd, timeout, output(0), output(1)
                ) from None
    except BaseException:
        with contextlib.suppress(Exception):
            _kill_process_group(proc, owned=True)
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=_REAP_TIMEOUT_SECONDS)
        raise
    finally:
        _close_process_streams(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, output(0), output(1))


def _sync_run(
    cmd: list[str], *, timeout: int, output_max_bytes: int | None = None,
    tail_chars: int | None = None, **kwargs,
) -> subprocess.CompletedProcess:
    """``subprocess.run(capture_output=True)`` that group-kills on timeout.

    ``subprocess.run`` kills only the DIRECT child on timeout and never exposes
    the ``Popen``, so the pid needed for a group kill is unreachable — MCP/
    language-server grandchildren survived a sync-path timeout indefinitely
    (codex round-1 major). This drop-in retains the ``Popen``: on timeout it
    SIGKILLs the child's whole process group first, then the child, collects
    whatever output was produced (bounded — a surviving pipe holder must not
    wedge the reap), and re-raises ``TimeoutExpired`` with that output so the
    caller's handling is unchanged. Raises ``FileNotFoundError``/``OSError``
    from the launch exactly like ``subprocess.run``.

    The NEUTRAL owned-group runner: gates, isolated apply verification, and
    toolgraph preflight route their timeout-controlled subprocesses through it
    too (codex round-7 — their bare ``subprocess.run(timeout=...)`` killed only
    the direct child, orphaning spawned workers). ``capture_output`` is
    accepted for run()-compatibility and ignored — this runner always pipes —
    and ``start_new_session`` defaults on (POSIX) so the group kill has a group.
    """
    require_supported_platform()
    if output_max_bytes is not None and output_max_bytes < 1:
        raise ValueError("output_max_bytes must be positive")
    if tail_chars is not None and tail_chars < 1:
        raise ValueError("tail_chars must be positive")
    if output_max_bytes is not None or tail_chars is not None:
        return _bounded_sync_run(
            cmd, timeout=timeout, output_max_bytes=output_max_bytes,
            tail_chars=tail_chars, **kwargs,
        )
    # Deliberately NOT a `with Popen(...)` block: Popen.__exit__ ends with an
    # UNBOUNDED wait(), so an unreapable child (a grandchild pinning the pipes
    # past the bounded reap) would hang this frame on the way out — defeating
    # the very timeout contract this wrapper exists to provide (codex round-5).
    # Streams are closed explicitly instead; a reap that times out leaves at
    # worst a zombie, reaped at process exit — the bounded give-up, not a hang.
    kwargs.pop("capture_output", None)  # always captured below
    kwargs.setdefault("start_new_session", _START_NEW_SESSION)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs
    )
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as first:
            _kill_process_group(proc, owned=True)  # we launched this group
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=_REAP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # Grandchild still holds the pipes — keep whatever partial
                # buffers the FIRST timeout captured rather than dropping them.
                stdout, stderr = first.stdout, first.stderr
            raise subprocess.TimeoutExpired(
                cmd, timeout, output=stdout, stderr=stderr
            ) from None
        except BaseException:
            # A Ctrl-C (or any interrupt) unwinds this frame, but
            # start_new_session detached the child from the terminal's
            # foreground group — the SIGINT that reached US never reached IT,
            # so without an explicit kill the agent (and its grandchildren)
            # keeps running, editing a workspace the user believes cancelled
            # (codex round-2 blocker, reproduced). Kill the tree, reap
            # bounded, then propagate.
            with contextlib.suppress(Exception):
                _kill_process_group(proc, owned=True)
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=_REAP_TIMEOUT_SECONDS)
            raise
    finally:
        _close_process_streams(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _close_process_streams(proc: object) -> None:
    """Close a Popen's owned pipes without waiting on the process (never raises)."""
    for name in ("stdout", "stderr", "stdin"):
        stream = getattr(proc, name, None)
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


def _kill_process_group(proc: object, *, owned: bool = False) -> bool:
    """Best-effort SIGKILL of the child's own process group (grandchildren too).

    Meaningful only when the child was launched with ``start_new_session`` — it
    then leads a group whose pgid equals its pid. Returns True when a group
    signal was actually sent; never raises.

    ``owned=True`` (every in-module caller — they launched the child with
    ``start_new_session`` themselves) kills the group even after the direct
    child was REAPED (``returncode`` set): a fast-exiting leader whose
    grandchild still holds the pipes leaves that grandchild as the group's
    surviving member, and skipping here is a guaranteed leak (codex round-6,
    reproduced). This is safe because POSIX reserves a pid from reuse while a
    process group with that id still has members — so as long as our
    grandchildren live, the pgid provably still names OUR group, and once they
    are gone the killpg is an ESRCH no-op. The residue is the narrow window
    where the group empties AND the pid is recycled as a new group's id between
    the reap and this call — traded for closing the reproduced leak.

    ``owned=False`` (an arbitrary process we did not launch) keeps the strict
    guard: a reaped child's pid may already belong to someone else, and a group
    kill to a recycled pid is the one failure mode worse than an orphan.
    """
    if not _START_NEW_SESSION:
        return False
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False
    if getattr(proc, "returncode", None) is not None and not owned:
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        return False
    return True


def _isolated_process_env(workdir: Path) -> dict[str, str]:
    """Build an environment that cannot route work outside ``workdir``."""
    excluded = _CWD_ROUTING_ENV | _RUN_IPC_ENV | {"PWD"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in excluded and not is_git_routing_var(key)
    }
    env["PWD"] = str(workdir.resolve())
    return env


# ── Live-output observability seam (#16) ──────────────────────────────────
#
# An AMBIENT seam, NOT a change to the agent contract: ``aexecute`` consults a
# ``ContextVar`` (default ``None`` → bounded capture without a live sink)
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

#: The sink key for THIS execution, when it needs to differ from the agent id.
#: A fan-out normally runs each agent once, so the agent id identifies the stream;
#: DecomposeStrategy's round-robin legitimately runs one agent several times AT
#: THE SAME TIME, and those executions would otherwise interleave byte-wise into a
#: single log and share one pane. Ambient like ``_OUTPUT_SINK`` (a Task copies the
#: context at creation, so each fan-out item carries its own), so the agent
#: contract stays ``(prompt, workdir, timeout)``.
_SINK_LABEL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "syncmill_sink_label", default=None
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

# Difficulty-based model routing: the run's effective provider→selector map
# (the decision's ``effective_selectors`` — an immutable mapping keyed by real
# agent id, ``None`` meaning "omit the model flag"). Default ``None`` preserves
# today's command builders byte-for-byte; the built-in builders each read only
# their own agent-id entry. Kept selector-shaped (not the whole decision) so
# this module never imports ``model_routing``.
_MODEL_SELECTORS: contextvars.ContextVar[Mapping[str, str | None] | None] = (
    contextvars.ContextVar("syncmill_model_selectors", default=None)
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


@contextlib.contextmanager
def model_routing_scope(selectors: Mapping[str, str | None]) -> Iterator[None]:
    """Expose a run's effective model selectors to the built-in builders.

    The Supervisor opens exactly one scope per top-level run, around the single
    strategy execution — every phase/fallback/fan-out inside inherits it
    (``asyncio`` task creation copies the current context), so the decision is
    compute-once by construction. A run with routing off opens no scope at all.
    """
    token = _MODEL_SELECTORS.set(selectors)
    try:
        yield
    finally:
        _MODEL_SELECTORS.reset(token)


def _model_selector(agent_id: str) -> str | None:
    """The active scope's selector for one agent, or ``None`` (omit the flag)."""
    selectors = _MODEL_SELECTORS.get()
    if selectors is None:
        return None
    return selectors.get(agent_id)


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


@lru_cache(maxsize=1)
def _json_lines(stdout: str) -> tuple[list[dict], int]:
    # Memoized for the same reason as _claude_events: the codex/kimi accessors
    # each re-derive the JSONL events from the same transcript string within one
    # synchronous span. Callers must not mutate the returned list.
    events: list[dict] = []
    malformed = 0
    for raw in _strip_ansi(stdout).splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            # RecursionError: a pathologically-nested line must count as
            # malformed, not abort the run (see _claude_events).
            malformed += 1
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            malformed += 1
    return events, malformed


class _ClaudeTranscript(NamedTuple):
    """Events recovered from a Claude transcript, plus how it decoded.

    ``structured`` records that the payload *was* a JSON transcript, which is
    strictly more than ``events`` being non-empty: ``[]``, ``["scalar"]``, or a
    truncated stream can decode as JSON yet yield no usable event. Callers need
    that distinction because the raw transcript may only ever be surfaced as
    output when the payload was never JSON at all — otherwise event bodies
    (system, session, tool results) leak into ``TaskResult.output``.
    """

    events: list[dict]
    malformed: int
    structured: bool


@lru_cache(maxsize=1)
def _claude_events(stdout: str) -> _ClaudeTranscript:
    """Normalize any Claude ``-p`` transcript shape into an ordered event list.

    Memoized (one entry): ``_build_result`` + ``_attach_provider_session`` call
    five accessors that each re-derive the transcript from the SAME string, in
    one synchronous no-await span — so one entry collapses five full
    ANSI-strip + JSON parses of a potentially tens-of-MB transcript into one,
    and a span parsing transcript A can never interleave with one parsing B.
    This also stops a verbose agent from blocking the shared event loop 5× as
    long as necessary (siblings stop draining their pipes while it parses).
    Pure function of its input, so the cache can never serve stale data.

    Claude emits three wire shapes depending on flags and CLI version: a single
    JSON object (``--output-format json``), JSONL (``--output-format
    stream-json``), and — since 2.1.x — a top-level JSON *array* of those same
    events. Parsing each shape separately is what let an array-shaped exit-0
    rate-limit response read as a success: the dict-only checks never saw the
    ``is_error``/``session_id``/``result`` fields buried in the array.

    ``malformed`` counts dropped events — an unparseable JSONL line, a non-dict
    array member, or a payload that decoded to a bare scalar (counted as 1) — so
    telemetry built from the survivors cannot claim to be complete.
    """
    cleaned = _strip_ansi(stdout).strip()
    if not cleaned:
        return _ClaudeTranscript([], 0, True)
    parsed = None
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # RecursionError (pathologically-nested stdout such as "[" * 100000) is
        # a RuntimeError, not a ValueError — catch it explicitly so a hostile
        # transcript degrades to the JSONL/plain-text path instead of aborting
        # the run (the parse_board_update lesson).
        parsed = None
    if isinstance(parsed, dict):
        return _ClaudeTranscript([parsed], 0, True)
    if isinstance(parsed, list):
        events = [item for item in parsed if isinstance(item, dict)]
        return _ClaudeTranscript(events, len(parsed) - len(events), True)
    if parsed is not None:
        # Valid JSON, but a bare scalar — structured yet carrying no events.
        return _ClaudeTranscript([], 1, True)
    events, malformed = _json_lines(cleaned)
    # JSONL is only "structured" if at least one line decoded; otherwise this is
    # plain text (a crash message, a usage banner) and belongs in output as-is.
    return _ClaudeTranscript(events, malformed, bool(events))


def _malformed_transcript_error(stdout: str) -> str | None:
    """Flag zero-exit stdout that LOOKS like JSON yet yields no usable event.

    A payload opening with ``{``/``[`` announced a structured transcript; if
    nothing decodes into a usable provider event, that is a protocol failure —
    route/compete must not crown it a winner as raw "output". Genuine plain
    text (a crash message, a usage banner) keeps the compatible fallthrough:
    it never claimed to be JSON.
    """
    cleaned = _strip_ansi(stdout).strip()
    if cleaned.startswith(("{", "[")):
        return "malformed provider transcript: no usable events"
    return None


def _claude_error_message(event: dict) -> str | None:
    """Extract a human-readable error from an event, or ``None`` if it is not one.

    Two error encodings exist: the in-band ``is_error`` flag on a result event
    (Claude can exit 0 while setting it), and a top-level ``type:"error"`` event
    carrying the provider's message — the shape a 429 takes in the array
    transcript. The message is preserved verbatim rather than classified; rate
    limiting is deliberately not given retry semantics here, so a rate-limited
    agent fails like any other and the existing breaker/fallback paths apply.
    """
    if event.get("is_error") is True and event.get("type") in (None, "result"):
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

    def _usage_from_output(self, stdout: str) -> TokenUsage | None:
        """Best-effort token/cache telemetry from the CLI transcript (None when
        the provider emits none). Overridden per provider; callers guard it."""
        return None

    def _session_for(self, workdir: Path) -> tuple[str | None, bool, str]:
        state = _PROVIDER_SESSIONS.get()
        key = f"{self._agent_id}\0{workdir.resolve()}"
        if state is None:
            return None, False, key
        sessions = state["sessions"]
        if key in sessions:
            return sessions[key], True, key
        # The seed map is keyed by agent id for the Supervisor paths, but a
        # fan-out that labels its items (rooms seed by MEMBER id) looks its own
        # label up first — so two members sharing one provider can never collide
        # on a single seeded session. Unlabeled callers (and decompose's subtask
        # labels, which never appear in a seed) fall through to the agent id.
        # The seed map is keyed by agent id for the Supervisor paths, but a
        # fan-out that labels its items (rooms seed by MEMBER id) is looked up
        # by its label ALONE — never falling back to the agent id, or one
        # member of a duplicate-provider room would inherit another member's
        # provider-keyed seed. Unlabeled callers keep the agent-id key.
        label = _SINK_LABEL.get()
        seeded = state["seed"].get(label) if label else state["seed"].get(
            self._agent_id
        )
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

        # Usage is best-effort telemetry, never load-bearing: a malformed or
        # absent usage payload yields None, and a parser bug must never turn a
        # completed run into a crash — hence the broad guard.
        try:
            usage = self._usage_from_output(stdout)
        except Exception:
            usage = None

        return TaskResult(
            task_id=task_id,
            agent_id=self._agent_id,
            status=TaskStatus.FAILED if is_failed else TaskStatus.COMPLETED,
            output=output,
            exit_code=returncode,
            duration_seconds=round(duration, 2),
            error=error_msg,
            tool_telemetry=telemetry,
            usage=usage,
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
        if "\x00" in prompt:
            # subprocess rejects a NUL in an argv element with ValueError, which
            # no launch handler catches — it would escape into the Supervisor's
            # crash path and freeze the run as recovery_required without any
            # agent having run. Callers scrub recalled text
            # (``strategies.base._enrich_prompt``); this is the deterministic
            # backstop. Checked rather than caught, so a genuinely unexpected
            # ValueError still surfaces as the programming error it is.
            return self._failure(
                uuid4(),
                output="",
                error="prompt contains a NUL byte, which cannot be passed to a "
                      "provider process",
                duration=0.0,
            )
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
            proc = _sync_run(
                cmd,
                text=True,
                # No tty, no inherited stdin: an agent CLI that prompts must fail
                # fast on EOF rather than have N concurrent agents race for the
                # user's terminal. Own process group so grandchildren (MCP
                # servers) are signalable and can't read the tty either.
                stdin=subprocess.DEVNULL,
                start_new_session=_START_NEW_SESSION,
                # An agent CLI can emit bytes that are not valid UTF-8 (a latin-1
                # path in an error message, a binary blob). Under the default
                # strict decoding that raises UnicodeDecodeError — a ValueError,
                # which none of the handlers below catch — so it would escape
                # execute entirely and fail the whole run. The async twin already
                # decodes with errors="replace"; this keeps the two paths'
                # classification identical, as the aexecute docstring promises.
                errors="replace",
                cwd=str(workdir),
                timeout=timeout,
                env=_isolated_process_env(workdir),
                output_max_bytes=self._config.output_max_bytes,
            )
            result = self._build_result(
                task_id, proc.returncode, proc.stdout, proc.stderr, monotonic() - start
            )
            self._attach_provider_session(result, proc.stdout, session_key, session_id)
            _drop_transcript_caches()
            return result
        except OutputLimitExceeded as exc:
            return self._failure(
                task_id, output="", error=str(exc), duration=monotonic() - start,
            )
        except subprocess.TimeoutExpired as exc:
            # _sync_run signals the whole process GROUP and then waits, bounded,
            # for the direct child. Grandchildren are signalled but not awaited,
            # so "nothing left to terminate here" is about this frame's
            # responsibility, not a guarantee that every descendant is reaped.
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

        Both paths enforce the combined raw-output budget while reading. A
        monitor sink receives those same chunks and a failure marker on timeout
        or overflow. Async timeouts return empty output; sync timeouts retain
        their bounded partial stdout.
        """
        require_supported_platform()
        if "\x00" in prompt:
            # subprocess rejects a NUL in an argv element with ValueError, which
            # no launch handler catches — it would escape into the Supervisor's
            # crash path and freeze the run as recovery_required without any
            # agent having run. Callers scrub recalled text
            # (``strategies.base._enrich_prompt``); this is the deterministic
            # backstop. Checked rather than caught, so a genuinely unexpected
            # ValueError still surfaces as the programming error it is.
            return self._failure(
                uuid4(),
                output="",
                error="prompt contains a NUL byte, which cannot be passed to a "
                      "provider process",
                duration=0.0,
            )
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
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                env=_isolated_process_env(workdir),
                start_new_session=_START_NEW_SESSION,
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

        sink = _OUTPUT_SINK.get()
        sink_key = _SINK_LABEL.get() or self._agent_id
        comm = _communicate_teed(proc, sink, sink_key, self._config.output_max_bytes)

        try:
            stdout_b, stderr_b = await asyncio.wait_for(comm, timeout=timeout)
        except OutputLimitExceeded as exc:
            await _terminate(proc)
            if sink is not None:
                with contextlib.suppress(Exception):
                    sink.feed(sink_key, f"\n[syncmill] {exc}\n".encode())
            return self._failure(
                task_id, output="", error=str(exc), duration=monotonic() - start,
            )
        except asyncio.TimeoutError:
            # wait_for cancelled communicate()/the tee; SIGKILL the child and reap
            # it so it can't linger as a zombie.
            await _terminate(proc)
            if sink is not None:
                # The pane already shows the pre-timeout bytes; annotate so it
                # self-explains (the result's output stays empty — see docstring).
                with contextlib.suppress(Exception):
                    sink.feed(
                        sink_key,
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
        except BaseException:
            await _terminate(proc)
            raise

        # A completed wait supplies an exit status. Be defensive for custom
        # subprocess adapters: no confirmed exit must never mean success.
        returncode = proc.returncode if proc.returncode is not None else -1
        # Decode ONCE: a second decode of a tens-of-MB transcript doubles peak
        # memory and CPU for nothing — both consumers want the same string.
        stdout_s = stdout_b.decode("utf-8", errors="replace")
        result = self._build_result(
            task_id,
            returncode,
            stdout_s,
            stderr_b.decode("utf-8", errors="replace"),
            monotonic() - start,
        )
        self._attach_provider_session(result, stdout_s, session_key, session_id)
        _drop_transcript_caches()
        return result


def _usage_int(value: object) -> int | None:
    """A non-negative int from an untrusted usage field, else None (bools are
    ints in Python — excluded explicitly)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# ── Claude Code ──────────────────────────────────────────────────────────


class ClaudeAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("claude", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        capture_enabled, _ = _TOOL_CAPTURE.get()
        session_id, resume, fork = _ACTIVE_PROVIDER_SESSION.get()
        selector = _model_selector(self._agent_id)
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
            *(["--model", selector] if selector else []),
        ]

    def _provider_session_from_output(self, stdout: str) -> str | None:
        for event in reversed(_claude_events(stdout).events):
            value = event.get("session_id")
            if isinstance(value, str) and value:
                return value
        return None

    def _usage_from_output(self, stdout: str) -> TokenUsage | None:
        # The terminal `result` event carries `usage` (input_tokens EXCLUDES the
        # cache tokens, which are reported beside it) plus a top-level
        # total_cost_usd. Normalize total_input to the FULL prompt size.
        for event in reversed(_claude_events(stdout).events):
            if event.get("type") != "result":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                return None
            input_tokens = _usage_int(usage.get("input_tokens"))
            cache_read = _usage_int(usage.get("cache_read_input_tokens"))
            cache_creation = _usage_int(usage.get("cache_creation_input_tokens"))
            output_tokens = _usage_int(usage.get("output_tokens"))
            if input_tokens is None and output_tokens is None:
                return None
            total_input = (
                (input_tokens or 0) + (cache_read or 0) + (cache_creation or 0)
                if input_tokens is not None
                else None
            )
            cost = event.get("total_cost_usd")
            return TokenUsage(
                total_input_tokens=total_input,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                total_cost_usd=(
                    float(cost)
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                    else None
                ),
            )
        return None

    def _parse_output(self, stdout: str) -> str:
        """Last result text across object / array / JSONL transcripts alike."""
        cleaned = _strip_ansi(stdout).strip()
        events, _, structured = _claude_events(stdout)
        for event in reversed(events):
            # The SAME terminal-success boundary as _is_error_output: once a
            # definitive success exists, its own result text (possibly empty)
            # IS the output — an earlier recovered error's prose must never be
            # published as the successful run's output.
            if event.get("type") == "result" and event.get("is_error") is False:
                result = event.get("result")
                return result.strip() if isinstance(result, str) else ""
        results = [
            event["result"].strip()
            for event in events
            if event.get("type") == "result"
            and isinstance(event.get("result"), str)
            and event["result"].strip()
        ]
        if results:
            return results[-1]
        texts = [
            event["result"].strip()
            for event in events
            if isinstance(event.get("result"), str) and event["result"].strip()
        ]
        if texts:
            return texts[-1]
        # The payload decoded but carried no result text: Claude answered with
        # nothing, errored, or was truncated. Returning the raw transcript here
        # would put system/session/tool-body events into TaskResult.output, so
        # only genuinely unstructured output falls back to the raw text.
        return "" if structured else cleaned

    def _is_error_output(self, stdout: str) -> str | None:
        events = _claude_events(stdout).events
        for event in reversed(events):
            # A definitive successful terminal result ends the scan: any error
            # events *before* it (e.g. a transient 429 the CLI retried past)
            # were recovered from, and failing the run on them would discard
            # finished work. Strict `is False` mirrors the strict `is True` in
            # _claude_error_message — a missing/None is_error decides nothing.
            if event.get("type") == "result" and event.get("is_error") is False:
                return None
            message = _claude_error_message(event)
            if message is not None:
                return message
        if not events:
            return _malformed_transcript_error(stdout)
        return None

    def _parse_tool_telemetry(self, stdout: str) -> ToolTelemetry:
        events, malformed, _ = _claude_events(stdout)
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
    Errors surface as a non-zero exit (like Codex); the one zero-exit failure
    ``_is_error_output`` flags is a JSON-shaped payload with no usable events
    (a malformed transcript).
    """

    def __init__(self, config: AgentConfig | None = None):
        super().__init__("kimi-code", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        session_id, resume, _ = _ACTIVE_PROVIDER_SESSION.get()
        selector = _model_selector(self._agent_id)
        # The model flag leads (`kimi -m SELECTOR -p PROMPT ...`) — the argv
        # shape pinned by the design doc's §6 provider table.
        return [
            "kimi",
            *(["-m", selector] if selector else []),
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

    def _usage_from_output(self, stdout: str) -> TokenUsage | None:
        # Kimi documents no usage event today; scan meta events defensively for
        # a usage dict of int fields so telemetry lights up if the CLI grows one.
        for event in reversed(_json_lines(stdout)[0]):
            if event.get("role") != "meta":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            input_tokens = _usage_int(usage.get("input_tokens"))
            output_tokens = _usage_int(usage.get("output_tokens"))
            if input_tokens is None and output_tokens is None:
                continue
            cache_read = _usage_int(
                usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens")
            )
            return TokenUsage(
                total_input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
            )
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
            except (json.JSONDecodeError, RecursionError):
                # RecursionError: a pathologically-nested line is malformed,
                # never a run-aborting crash (see _claude_events).
                continue
            if not isinstance(event, dict) or event.get("role") != "assistant":
                continue
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                messages.append(content.strip())
        return "\n".join(messages) if messages else cleaned

    def _is_error_output(self, stdout: str) -> str | None:
        # Kimi signals failure via a non-zero exit; the one zero-exit failure
        # shape is a payload that claimed to be JSONL yet decoded to nothing.
        events, _ = _json_lines(stdout)
        if events:
            return None
        return _malformed_transcript_error(stdout)

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
        selector = _model_selector(self._agent_id)
        # `-m` precedes the positionals in both shapes (§6 provider table):
        # `codex exec -m SEL PROMPT ...` / `codex exec resume -m SEL SESSION ...`.
        model = ["-m", selector] if selector else []
        # Sandbox/approval ride `-c` config overrides on BOTH shapes. Verified
        # against codex-cli 0.147.0: `codex exec` REJECTS `--full-auto`
        # outright ("unexpected argument"), `exec resume` accepts neither it
        # nor `-s`, and `on-failure` is no longer an approval policy (the
        # values are untrusted/on-request/never — `never` is the
        # non-interactive one). `-c key=value` is the long-standing portable
        # spelling, so fresh and resumed members run under identical, valid
        # permissions on old and new CLIs alike.
        sandbox = [
            "-c", 'sandbox_mode="workspace-write"',
            "-c", 'approval_policy="never"',
        ]
        if session_id and resume:
            return [
                "codex", "exec", "resume", *model, *sandbox,
                session_id, prompt,
                "--json",
            ]
        return [
            "codex", "exec",
            *model,
            *sandbox,
            prompt,
            "--json",
        ]

    def _provider_session_from_output(self, stdout: str) -> str | None:
        for event in _json_lines(stdout)[0]:
            if event.get("type") == "thread.started":
                value = event.get("thread_id") or event.get("threadId")
                if isinstance(value, str) and value:
                    return value
        return None

    def _usage_from_output(self, stdout: str) -> TokenUsage | None:
        # Codex emits one `turn.completed` event per turn with a `usage` dict
        # whose input_tokens INCLUDES cached_input_tokens — sum across turns.
        total_input = output_tokens = cache_read = 0
        seen = False
        for event in _json_lines(stdout)[0]:
            if event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            input_tokens = _usage_int(usage.get("input_tokens"))
            if input_tokens is None:
                continue
            seen = True
            total_input += input_tokens
            output_tokens += _usage_int(usage.get("output_tokens")) or 0
            cache_read += _usage_int(usage.get("cached_input_tokens")) or 0
        if not seen:
            return None
        return TokenUsage(
            total_input_tokens=total_input,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
        )

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
            except (json.JSONDecodeError, RecursionError):
                # RecursionError: a pathologically-nested line is malformed,
                # never a run-aborting crash (see _claude_events).
                continue
            if not isinstance(event, dict):
                continue  # a valid-JSON scalar/list line carries no event
            # Codex JSONL: look for agent_message items
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text", "")
                    if isinstance(text, str) and text.strip():
                        messages.append(text.strip())
        return "\n".join(messages) if messages else cleaned

    def _is_error_output(self, stdout: str) -> str | None:
        # Codex signals failure via a non-zero exit; the one zero-exit failure
        # shape is a payload that claimed to be JSONL yet decoded to nothing.
        events, _ = _json_lines(stdout)
        if events:
            return None
        return _malformed_transcript_error(stdout)

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
    can block forever waiting for those pipes to close. The group kill below
    reaches those grandchildren (the child leads its own group under
    ``_START_NEW_SESSION``), and bounding the reap turns any residual hang into
    a clean give-up — a transport may then be finalized at GC (a harmless
    "Event loop is closed" note on stderr at shutdown). On platforms without
    process groups the orphaned grandchild is reparented to init, the same
    limitation as the sync path.
    """
    # Group first (reaches grandchildren — including after the leader was
    # already reaped: owned=True, see _kill_process_group), then the direct
    # kill — which also preserves the exact pre-group-kill semantics for a
    # child that exited on its own between the timeout and this call.
    _kill_process_group(proc, owned=True)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()

    async def discard(stream):
        if stream is not None:
            while await stream.read(4096):
                pass

    # Discard in fixed chunks: paused asyncio pipe buffers otherwise prevent
    # wait() from finishing even after the entire process group has died.
    drains = [
        asyncio.ensure_future(discard(getattr(proc, "stdout", None))),
        asyncio.ensure_future(discard(getattr(proc, "stderr", None))),
        asyncio.ensure_future(proc.wait()),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*drains), timeout=_REAP_TIMEOUT_SECONDS)
    except Exception:
        pass
    finally:
        # gather may already be done after one reader failed. Cancel its
        # children explicitly so a broken pipe cannot leave sibling drains alive.
        for task in drains:
            task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(*drains, return_exceptions=True)


async def _communicate_teed(
    proc: asyncio.subprocess.Process, sink: OutputSink | None, agent_id: str,
    output_max_bytes: int | None = None,
) -> tuple[bytes, bytes]:
    """Drain both streams with one byte budget and an optional live sink.

    The caller owns the overall timeout and kills/reaps on any failure. The
    sibling reader is cancelled and awaited before errors leave this function.
    """
    total = 0

    async def _drain(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal total
        if stream is None:  # pragma: no cover - PIPE always gives a reader
            return b""
        chunks: list[bytes] = []
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            total += len(chunk)
            if output_max_bytes is not None and total > output_max_bytes:
                raise OutputLimitExceeded(f"output limit exceeded ({output_max_bytes} bytes)")
            chunks.append(chunk)
            # `except Exception` (not BaseException) deliberately: a CancelledError
            # from a timeout/sibling-drain must propagate, never be swallowed here.
            try:
                if sink is not None:
                    sink.feed(agent_id, chunk)
            except Exception:
                pass
        return b"".join(chunks)

    # Cancel the sibling drain if one raises: a bare gather propagates the first
    # exception while the OTHER drain keeps running as an orphan holding a pipe
    # fd (plus an "exception was never retrieved" warning at loop close).
    drains = (
        asyncio.ensure_future(_drain(proc.stdout)),
        asyncio.ensure_future(_drain(proc.stderr)),
    )
    try:
        stdout_b, stderr_b = await asyncio.gather(*drains)
    except BaseException:
        for t in drains:
            t.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(*drains, return_exceptions=True)
        raise
    # The caller bounds the entire invocation, including exit after pipe EOF.
    await proc.wait()
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

    Shares ``_gather_or_drain`` with its siblings: a genuinely-raising
    ``aexecute`` surfaces only after the sibling tasks are cancelled and
    drained, so none outlive the failed batch on the reused module loop.
    """
    tasks = [
        asyncio.ensure_future(agent.aexecute(prompt, workdir, timeout))
        for agent in agents
    ]
    return await _gather_or_drain(tasks)


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
    # Eager scheduling with a caller-supplied timeout_for: if it raises on item
    # k, items 0..k-1 are already live — cancel + drain them before surfacing
    # (the _gather_or_drain contract, extended to the construction phase).
    tasks: list[asyncio.Future[TaskResult]] = []
    try:
        for agent, wt in zip(agents, worktrees):
            tasks.append(
                asyncio.ensure_future(
                    agent.aexecute(prompt, wt, timeout_for(agent.agent_id))
                )
            )
    except BaseException:
        await _cancel_and_drain(tasks)
        raise
    return await _gather_or_drain(tasks)


async def aexecute_each(
    agents: list[AgentRunner],
    prompts: list[str],
    worktrees: list[Path],
    timeout_for: Callable[[str], int],
    labels: list[str] | None = None,
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
    if labels is not None and len(labels) != len(agents):
        raise ValueError(
            f"aexecute_each needs one label per agent: {len(labels)} labels, "
            f"{len(agents)} agents."
        )
    # A Task copies the current context when it is created, so setting the label
    # immediately before each ensure_future gives every item its own — without it
    # the round-robin's repeated agent id would merge concurrent streams.
    tasks = []
    try:
        for i, (agent, prompt, wt) in enumerate(zip(agents, prompts, worktrees)):
            token = _SINK_LABEL.set(labels[i] if labels is not None else None)
            try:
                tasks.append(
                    asyncio.ensure_future(
                        agent.aexecute(prompt, wt, timeout_for(agent.agent_id))
                    )
                )
            finally:
                _SINK_LABEL.reset(token)
    except BaseException:
        # A raising timeout_for on item k must not strand items 0..k-1 as live
        # ownerless subprocesses (the aexecute_isolated construction contract).
        await _cancel_and_drain(tasks)
        raise
    return await _gather_or_drain(tasks)


async def _cancel_and_drain(tasks: list[asyncio.Future[TaskResult]]) -> None:
    """Cancel already-scheduled fan-out tasks and await them out (never raises)."""
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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
