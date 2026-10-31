"""tmux live monitoring of concurrent agent runs (#16, v0.3).

A live view of a concurrent run: each agent's output streams into its own tmux
pane while it runs. This is NOT a strategy and NOT an agent — it's an
observability layer the Supervisor builds per run and threads through the
strategy (like the gate/breaker), reusing the ambient ``OutputSink`` seam in
``agents.py`` so agents stay pure ``(prompt, workdir, timeout)`` functions.

**Outer / inner split** (so auto-attach can coexist with the deliberately sync,
main-thread orchestration — a single sync process can't both run the fan-out and
hold the foreground for ``tmux attach``):

* The **OUTER** ``syncmill run`` (a normal invocation with ``--monitor``) builds a
  detached tmux session whose control pane runs the REAL orchestration by
  re-invoking ``syncmill run`` — same argv — with ``SYNCMILL_MONITOR_DIR`` set
  (the INNER process). It then ``tmux attach``es (foreground, blocks) and, on
  detach, reads the inner's ``result.json`` and prints/exits with its code.
* The **INNER** process detects itself via :func:`inner_dir` (env var + sentinel),
  builds a :class:`TmuxFileMonitor`, runs the orchestration normally, tees each
  agent's output to a per-agent log, lazily adds a live ``tail -F`` pane per agent
  it actually runs (no outer/inner drift), and writes ``result.json`` at the end.

Every process stays sync + single-threaded. All tmux interaction funnels through
:func:`_tmux` / :func:`_tmux_attach` and tmux discovery through
:func:`tmux_available`, so the whole module is hermetically testable without ever
launching tmux. The module is intentionally FLAT (one file, like ``gateway.py``);
the OUTER launcher and INNER monitor halves are banner-separated and the OUTER
half imports nothing from the INNER half, so a future split is a clean cut.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from syncmill.agents import stream_to
from syncmill.command_errors import command_error
from syncmill.artifacts import printable_name, printable_text
from syncmill.config import MonitorConfig
from syncmill.models import TaskResult, TaskStatus
from syncmill.terminal import NO_COMMAND, echo, printable_command, secho, shell_arg

# ── Shared: the outer↔inner handshake, tmux plumbing, result hand-off ──────

#: Env var the OUTER launcher sets on the INNER process, naming the shared run
#: dir. INTERNAL IPC, not user config — deliberately outside ``SYNCMILL_MONITOR__*``.
INNER_ENV = "SYNCMILL_MONITOR_DIR"
#: Env var carrying the OUTER-minted run_id to the INNER's ``resolve_run_id``
#: (report.py). INTERNAL IPC like ``INNER_ENV`` — it rides the same 0600 env
#: snapshot (never ``_inner_argv``, which stays deterministic/id-free), and the
#: resolver only honors it when ``inner_dir()`` says this process IS the inner,
#: so a stray exported ``SYNCMILL_RUN_ID`` can never leak into a normal run.
RUN_ID_ENV = "SYNCMILL_RUN_ID"
#: Sentinel file the OUTER writes into the run dir. ``inner_dir`` requires BOTH the
#: env var and this file, so a stray exported ``SYNCMILL_MONITOR_DIR`` can never
#: masquerade a normal run as the inner orchestration.
SENTINEL_NAME = ".syncmill-inner"
_RESULT_FILE = "result.json"

#: Grace window the OUTER polls for ``result.json`` after ``tmux attach`` returns,
#: covering the race where the user detaches just as the inner is writing it.
#: Module-level so tests can shrink it to 0.
_RESULT_GRACE_SECONDS = 2.0
_RESULT_POLL_INTERVAL = 0.1

#: Env vars NOT forwarded into the inner PANE via the snapshot: ``TMUX``/``TMUX_PANE``
#: identify the pane (forwarding the outer's would point the inner at the wrong one),
#: and ``TERM`` is set by tmux for the pane (forwarding the outer's can corrupt
#: terminal rendering inside the control pane / agent CLIs). MUST stay in lockstep
#: with the set ``_ENV_SHIM`` preserves: anything skipped here but not re-kept by the
#: shim would be lost from the inner's env entirely (neither snapshotted nor inherited).
_SNAPSHOT_SKIP_ENV = frozenset({"TMUX", "TMUX_PANE", "TERM"})

#: Env vars stripped from the ATTACH client only: dropping ``TMUX``/``TMUX_PANE`` lets
#: ``attach-session`` work from inside an existing tmux session (it otherwise refuses
#: to nest). ``TERM`` is KEPT — the attaching client needs the caller's terminal type
#: to render, so stripping it would break a normal ``--monitor`` attach.
_ATTACH_STRIP_ENV = frozenset({"TMUX", "TMUX_PANE"})

#: A tiny exec shim run as the pane's process: it REPLACES the pane's inherited
#: environment with the OUTER's ``0600`` snapshot, preserving only the tmux-owned
#: pane vars (``TERM``/``TMUX``/``TMUX_PANE``), then ``execvp``s the real inner argv.
#: Replacing (not ``update``-ing) is load-bearing: a new tmux session inherits the
#: tmux **server's** env, which can be stale (a server started hours ago in another
#: project). An additive ``update`` would leave any stale ``SYNCMILL_*``/auth/PATH
#: var that the outer client lacks, silently changing the inner run's config — so we
#: clear first, giving the in-tmux INNER EXACTLY the outer's environment (PATH,
#: ``SYNCMILL_*`` config, agent auth) plus the pane's own terminal vars. Secrets ride
#: the file, never the tmux command-line argv, and the launch argv stays tiny (a
#: large env can't blow the exec arg limit).
#:
#: The shim SELF-UNLINKS the snapshot, in a ``finally`` before parsing it (the
#: ``session.py`` recipe). The file holds the outer's entire environment —
#: provider API keys, tokens, everything — and the run dir outlives the shim on
#: two paths: a detach before the run finishes and a failed attach both keep it
#: so the inner can still write ``result.json``. Left in place, that plaintext
#: dump sits in the temp dir until some LATER monitored run's stale sweep finds
#: it a day old, which may be never. The parent cannot delete it instead: the
#: detached pane's shim runs asynchronously after ``new-session`` returns.
_ENV_SHIM = (
    "import os,sys,json\n"
    "keep={k:os.environ[k] for k in ('TERM','TMUX','TMUX_PANE') if k in os.environ}\n"
    "p=os.path.join(sys.argv[1],'env')\n"
    "try:\n"
    "    data=open(p).read()\n"
    "finally:\n"
    "    try: os.unlink(p)\n"
    "    except OSError: pass\n"
    "snap=json.loads(data)\n"
    "os.environ.clear(); os.environ.update(snap); os.environ.update(keep)\n"
    "os.execvp(sys.argv[2], sys.argv[2:])\n"
)


class MonitorLaunchError(RuntimeError):
    """The OUTER tmux session couldn't be created. The CLI catches this and falls
    back to a normal (unmonitored) run — a tmux hiccup must never abort the task."""


def inner_dir() -> str | None:
    """The run dir iff THIS process is the in-tmux INNER orchestration, else None.

    The single source of truth for "am I the inner process?", read at call time
    (never frozen at import). Requires BOTH the env var the OUTER set AND the
    sentinel file it wrote — belt-and-suspenders against a stray exported
    ``SYNCMILL_MONITOR_DIR``. Both the CLI's outer-launch gate and
    :func:`build_monitor` route through here so they can never disagree (a drift
    would silently run the inner with no monitor → no ``result.json`` → the outer
    reports the wrong exit code).
    """
    run_dir = os.environ.get(INNER_ENV)
    if not run_dir:
        return None
    if not (Path(run_dir) / SENTINEL_NAME).is_file():
        return None
    return run_dir


def tmux_available() -> bool:
    """True if a ``tmux`` binary is on PATH — the only liveness check, and a pure
    PATH lookup (no probe). Patched in tests so the suite never depends on a real
    tmux being installed (it is on the dev box, absent in CI)."""
    return shutil.which("tmux") is not None


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a tmux CONTROL command, capturing output. Never raises on tmux's own
    non-zero exit — callers inspect ``returncode``. tmux-missing is guarded
    upstream by :func:`tmux_available`, so a bare launch failure here is
    exceptional and surfaces as ``FileNotFoundError`` only if that guard was
    skipped."""
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _tmux_attach(session: str) -> int:
    """Attach the real terminal to ``session`` — FOREGROUND, blocks until the user
    detaches (Ctrl-b d) or the session ends; returns tmux's exit code.

    Must inherit all three std streams (NO ``capture_output``, NO ``stdin=PIPE``)
    or tmux can't draw the UI and hangs. Runs with ``TMUX``/``TMUX_PANE`` UNSET so
    ``attach-session`` works even when ``--monitor`` is invoked from INSIDE an
    existing tmux session (it otherwise refuses to nest). The caller inspects the
    return code so a real attach failure isn't mistaken for a clean detach.
    """
    env = {k: v for k, v in os.environ.items() if k not in _ATTACH_STRIP_ENV}
    return subprocess.run(
        ["tmux", "attach-session", "-t", session], env=env
    ).returncode


def _result_to_json(result: TaskResult) -> dict:
    """The single result.json contract (inner writes / outer reads). Flattens the
    gate verdict to a string + bool — never the ``GateReport`` object — and keeps
    only what the OUTER needs to reproduce the CLI's result display."""
    return {
        "status": str(result.status),
        "agent_id": result.agent_id,
        "output": result.output,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "files_changed": list(result.files_changed),
        "error": result.error,
        "failure_kind": result.failure_kind,
        "gate_summary": result.gate.summary() if result.gate is not None else None,
        "gate_passed": result.gate.passed if result.gate is not None else None,
        # Already a plain str (or None) — no flattening needed. Carried so the
        # OUTER can honor `--emit-patch` from result.json after a monitored run
        # (json escapes any surrogateescape'd non-UTF-8 bytes losslessly).
        "patch": result.patch,
    }


def write_result(run_dir: str | Path, result: TaskResult) -> None:
    """Atomically write the inner's result to ``<run_dir>/result.json`` (a
    UNIQUE temp in the same dir — the report mkstemp recipe, not a fixed
    ``.tmp`` sibling two concurrent writers would interleave into — then
    ``os.replace``), so the OUTER never reads a half-written file."""
    path = Path(run_dir) / _RESULT_FILE
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(_result_to_json(result)))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_result(run_dir: str | Path) -> dict | None:
    """Read ``<run_dir>/result.json``; None if absent or unreadable (the inner
    hasn't finished, or never wrote it)."""
    try:
        return json.loads((Path(run_dir) / _RESULT_FILE).read_text())
    except (OSError, ValueError):
        return None


# What to do next when the provider, not the task, stopped the run (#114).
# Keyed by ``FailureKind`` value; an unknown or missing kind prints nothing.
_FAILURE_HINTS = {
    "provider_auth": (
        "The provider rejected the agent's credentials. Log in to that agent's "
        "CLI again, then retry; `syncmill doctor` checks each agent."
    ),
    "provider_rate_limit": (
        "The provider rate-limited the agent. Wait a little and retry, or pick "
        "another agent with -a."
    ),
    "provider_unavailable": (
        "The provider reported an error on its side. Retry later, or pick "
        "another agent with -a."
    ),
}


def render_result(data: dict) -> int:
    """Echo a result.json dict the way the CLI prints a live result, and return the
    process exit code (0 completed, 1 failed). Shared by the OUTER (after detach)
    and the CLI's normal path, so a monitored and unmonitored run print identically.

    result.json is read back without schema validation, so its one-line fields
    go through ``printable_name``; only output and error stay multi-line.
    """
    if data.get("status") == str(TaskStatus.COMPLETED):
        agent = printable_name(str(data.get("agent_id")))
        duration = printable_name(str(data.get("duration_seconds")))
        secho(f"[{agent}] Completed in {duration}s", fg="green")
        if data.get("gate_summary") is not None:
            summary = printable_name(str(data["gate_summary"]))
            secho(f"[syncmill] Quality gate: passed ({summary})", fg="green")
        if data.get("files_changed"):
            names = ", ".join(printable_name(str(f)) for f in data["files_changed"])
            echo(f"[syncmill] Files changed: {names}")
        echo()
        echo(printable_text(str(data.get("output") or "")))
        return 0
    secho("[syncmill] Failed", fg="red")
    if data.get("error"):
        echo(printable_text(str(data["error"])))
    # result.json is unvalidated: only a string kind is looked up.
    kind = data.get("failure_kind")
    hint = _FAILURE_HINTS.get(kind) if isinstance(kind, str) else None
    if hint is not None:
        secho(f"[syncmill] {hint}", fg="yellow")
    return 1


def render_task_result(result: TaskResult) -> int:
    """Render a live ``TaskResult`` through the same path as a monitored run's
    ``result.json`` (:func:`render_result`), so the monitored and unmonitored CLI
    displays are byte-identical — the user sees the same thing either way."""
    return render_result(_result_to_json(result))


def emit_patch_file(path: str | Path, data: dict) -> bool:
    """Write the winner's collected ``patch`` from a result.json-shaped dict to
    ``path`` (the ``run --emit-patch`` recovery path). Lives beside
    :func:`render_result` for the same reason: it consumes the one result
    contract, shared by the CLI's normal path and the OUTER's monitored path so
    the two behave identically.

    Returns ``False`` only when a patch existed but could not be written (the
    one failure of the user's explicit export request — callers turn it into a
    non-zero exit); the nothing-to-emit cases (failed run, no diff, non-git
    workspace) print a notice and return ``True``. Encodes with
    ``surrogateescape`` to round-trip ``capture_changes``'s byte-exact decode.
    """
    if data.get("status") != str(TaskStatus.COMPLETED):
        secho(
            "[syncmill] --emit-patch: run did not complete — no patch written",
            fg="yellow",
        )
        return True
    patch = data.get("patch")
    if not patch:
        secho(
            "[syncmill] --emit-patch: no diff to emit (the winner changed no "
            "files, or its workspace was not a git worktree)",
            fg="yellow",
        )
        return True
    try:
        Path(path).write_text(patch, encoding="utf-8", errors="surrogateescape")
    except OSError as exc:
        secho(
            f"[syncmill] --emit-patch: failed to write {printable_name(str(path))}: {exc}",
            fg="red",
        )
        return False
    command = printable_command("git", "apply", args=[path])  # copy/paste-safe with spaces
    secho(
        f"[syncmill] Patch written: {printable_name(str(path))} — apply with: {command}",
        fg="green",
    )
    return True


# ── INNER monitor: tee output to per-agent logs + live tmux panes ──────────


@runtime_checkable
class Monitor(Protocol):
    """Live observability of a concurrent run, threaded through the strategy layer
    (like the gate/breaker). A concurrent strategy announces each :meth:`phase`,
    wraps each fan-out in :meth:`streaming` (which both opens the output seam and
    ensures a live pane exists per agent about to run), reports each agent's final
    :meth:`status`, and the Supervisor calls :meth:`finish` once with the winner.
    A ``None`` monitor (the default / sequential strategies) means no monitoring.
    """

    def phase(self, name: str) -> None: ...

    def streaming(self, agent_ids: list[str]) -> contextlib.AbstractContextManager: ...

    def status(self, agent_id: str, status: TaskStatus) -> None: ...

    def finish(self, result: TaskResult) -> None: ...


def _safe(agent_id: str) -> str:
    """A filename-safe form of an agent id (the real ids — claude/codex/kimi-code —
    are already safe; this only guards against a path separator sneaking in)."""
    return agent_id.replace("/", "_").replace(os.sep, "_")


#: Bytes per rotated log segment (two segments per agent → 2× this retained).
#: Bounds a runaway agent from filling the run dir while keeping the most
#: recent output for the tmux pane; small enough that rotation is cheap.
_LOG_SEGMENT_BYTES = 256 * 1024


class FileSink:
    """An :class:`~syncmill.agents.OutputSink` that appends each agent's streamed
    chunks to ``<run_dir>/agent-<id>.log`` via a single non-blocking ``os.write``.

    No Python buffering and no ``fsync``: ``feed`` runs inside the agent's drain
    loop on the shared event loop, so it must not block (a slow/NFS/full-disk dir
    must not stall every other agent's drain *and* memtomem's I/O) and must never
    raise out — a dropped write loses a log chunk, never the run. The fd is opened
    once per agent in ``O_APPEND`` mode and reused, so a council member's output
    across propose/critique/synthesize accumulates in one log.

    The log is *bounded* (the rooms ``RoundLogSink`` rotation): two rotated
    segments of at most :data:`_LOG_SEGMENT_BYTES` each, rolled by a cheap
    ``os.replace`` of the active segment onto its ``.1`` sibling. The pane's
    ``tail -F`` follows by name, so rotation is transparent to the live view.
    Unlike the rooms sink this one never creates the run dir (a missing dir
    stays a silent no-op — the monitor owns the dir's lifecycle, not the sink).
    """

    def __init__(self, run_dir: Path):
        self._run_dir = run_dir
        self._fds: dict[str, int] = {}
        self._written: dict[str, int] = {}

    def path_for(self, agent_id: str) -> Path:
        return self._run_dir / f"agent-{_safe(agent_id)}.log"

    def _fd(self, agent_id: str) -> int | None:
        fd = self._fds.get(agent_id)
        if fd is not None:
            return fd
        try:
            fd = os.open(
                str(self.path_for(agent_id)),
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o644,
            )
        except OSError:
            return None
        self._fds[agent_id] = fd
        try:
            self._written[agent_id] = os.fstat(fd).st_size
        except OSError:
            self._written[agent_id] = 0
        return fd

    def _rotate(self, agent_id: str) -> int | None:
        """Roll the active segment onto its ``.1`` sibling and reopen empty.

        Returns ``None`` when the roll fails: the active segment is already at
        the cap, so reopening it and continuing to append would grow the log
        past the bound. Dropping the chunk (best-effort) keeps the bound; the
        next feed retries the rotate.
        """
        old = self._fds.pop(agent_id, None)
        if old is not None:
            with contextlib.suppress(OSError):
                os.close(old)
        path = self.path_for(agent_id)
        try:
            os.replace(str(path), str(path.with_suffix(path.suffix + ".1")))
        except OSError:
            return None  # active segment stays full; the bound holds
        self._written[agent_id] = 0
        return self._fd(agent_id)

    def feed(self, agent_id: str, chunk: bytes) -> None:
        fd = self._fd(agent_id)
        if fd is None:
            return
        # A single chunk larger than a whole segment would blow the bound even
        # right after a rotate, so keep only its most-recent tail — the segment
        # cap holds regardless of how the tee sizes its writes.
        if len(chunk) > _LOG_SEGMENT_BYTES:
            chunk = chunk[-_LOG_SEGMENT_BYTES:]
        if self._written.get(agent_id, 0) + len(chunk) > _LOG_SEGMENT_BYTES:
            fd = self._rotate(agent_id)
            if fd is None:
                return
        try:
            written = os.write(fd, chunk)
        except OSError:
            return  # best-effort: a failed log write never breaks the run
        # Account for the bytes actually written; a short write must not let the
        # tracked size drift below the real file size and defeat the cap.
        self._written[agent_id] = self._written.get(agent_id, 0) + written

    def close(self) -> None:
        for fd in self._fds.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self._fds.clear()
        self._written.clear()

    # Context-manager protocol so standalone users (outside TmuxFileMonitor,
    # whose finish() is the only in-module close site) can't leak the
    # per-agent O_APPEND fds when the monitored run aborts early.
    def __enter__(self) -> "FileSink":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class TmuxFileMonitor:
    """The INNER-side :class:`Monitor`: tees agent output to per-agent logs and
    drives the tmux panes from inside the session (``$TMUX`` is inherited).

    Panes are created LAZILY and inner-driven — one ``tail -F`` pane the first time
    an agent actually runs (via :meth:`streaming`), so the live panes track the
    real runnable set (after capability re-ordering + the breaker's OPEN filter)
    with zero outer/inner drift and no blank panes for skipped agents. Phase
    headers and per-agent outcomes are appended to ``status.log`` (the planner's
    sync phase, which can't stream, is status-only there). On :meth:`finish` it
    writes ``result.json`` and keeps the control pane's result on screen
    (``remain-on-exit``) while the ``tail`` panes hold the session open until the
    user detaches.
    """

    def __init__(self, run_dir: Path, config: MonitorConfig):
        self._run_dir = run_dir
        self._config = config  # reserved (session_prefix etc. are outer-only today)
        self._sink = FileSink(run_dir)
        self._status_path = run_dir / "status.log"
        self._seen: set[str] = set()  # agents that already have a pane attempt
        self._in_tmux = bool(os.environ.get("TMUX"))

    # -- strategy-facing API --------------------------------------------------

    def phase(self, name: str) -> None:
        self._log_status(f"── phase: {name} ──")

    def streaming(self, agent_ids: list[str]) -> contextlib.AbstractContextManager:
        for agent_id in agent_ids:
            self._ensure_pane(agent_id)
        return stream_to(self._sink)

    def status(self, agent_id: str, status: TaskStatus) -> None:
        self._log_status(f"[{status}] {agent_id}")

    def finish(self, result: TaskResult) -> None:
        # Best-effort: a failed result.json write (full disk, vanished run dir) must
        # not break the Supervisor's cleanup — and the sink fds always get closed.
        try:
            write_result(self._run_dir, result)
        except OSError:
            pass
        finally:
            self._sink.close()
        if not self._in_tmux:
            return
        # Keep THIS (control) pane visible after the inner process exits so the
        # printed result stays on screen; the tail panes keep the session alive
        # until the user detaches (the auto-attach UX), at which point the OUTER
        # reads result.json and tears the session down.
        with contextlib.suppress(Exception):
            _tmux("set-option", "-p", "remain-on-exit", "on")
            _tmux("select-pane", "-T", "syncmill — run complete · detach: Ctrl-b d")

    # -- internals ------------------------------------------------------------

    def _log_status(self, line: str) -> None:
        with contextlib.suppress(OSError):
            with self._status_path.open("a") as fh:
                fh.write(line + "\n")

    def _ensure_pane(self, agent_id: str) -> None:
        if agent_id in self._seen or not self._in_tmux:
            return
        self._seen.add(agent_id)  # record the attempt up front; never retry per phase
        log = self._sink.path_for(agent_id)
        # Seed the log so the file exists (tail opens cleanly) and the pane shows a
        # header immediately rather than an empty box.
        self._sink.feed(agent_id, f"══ {agent_id} ══\n".encode())
        # Pane setup is best-effort: a tmux launch failure (fork EAGAIN, PATH
        # mutation) must NEVER abort the run — it would surface inside the
        # strategy's `with _monitor_streaming` block on the main thread.
        with contextlib.suppress(Exception):
            res = _tmux(
                "split-window", "-d", "-P", "-F", "#{pane_id}",
                "tail", "-n", "+1", "-F", str(log),
            )
            if res.returncode != 0:
                return
            pane_id = res.stdout.strip()
            if pane_id:
                _tmux("select-pane", "-t", pane_id, "-T", agent_id)
            _tmux("select-layout", "tiled")


def build_monitor(config: MonitorConfig, *, monitor_dir: str | None) -> Monitor | None:
    """The live monitor when ``monitor_dir`` is set (this is the in-tmux INNER
    process), else ``None`` — so normal runs and the OUTER launcher never build
    one, and monitoring is off out of the box.

    Pure in its inputs (no env read — the caller passes ``inner_dir()``), mirroring
    ``build_gate``/``build_breaker``. ``config.monitor.enabled`` gates the OUTER
    tmux launch in the CLI, not this.
    """
    if not monitor_dir:
        return None
    return TmuxFileMonitor(Path(monitor_dir), config)


# ── OUTER launcher: build the tmux session, run the inner, attach ──────────


def launch_monitored_run(
    inner_argv: list[str],
    *,
    session_prefix: str = "syncmill",
    emit_patch: str | None = None,
    report_runs_dir: str | Path | None = None,
) -> int:
    """Run ``inner_argv`` (a reconstructed ``syncmill run …``) inside tmux, attach
    live, and return the run's exit code.

    Builds a detached session whose control pane runs ``inner_argv`` through a tiny
    ``sys.executable`` exec shim (:data:`_ENV_SHIM`) that loads the outer env from a
    ``0600`` snapshot and ``execvp``s it — so the inner runs with the outer's full
    environment (no secrets in argv) regardless of the tmux server's stale env, and
    argv is passed DIRECTLY (no shell re-parse → no injection, works under fish/csh).
    Then it ``tmux attach``es (foreground, with ``TMUX`` unset so it works from inside
    tmux too). After attach returns it reads ``result.json``: present → tear the
    session down, render, return the code; absent → either a clean detach-before-
    completion (the run continues; print re-attach info, return 0) or a failed attach
    (non-zero tmux rc → surface it, return non-zero).

    ``emit_patch`` is the ``--emit-patch`` destination: the flag is deliberately
    NOT forwarded to the inner (``_inner_argv`` omits it, like ``--monitor``) —
    the OUTER honors it here from result.json's ``patch`` field, so the file is
    written relative to the user's own invocation, not inside the tmux pane.

    ``report_runs_dir`` is the OUTER's view of ``[integration.report] runs_dir``
    (``None`` when reporting is off for the OUTER): after rendering the result it
    prints the run-envelope's path — gated on the envelope actually EXISTING,
    because the OUTER can't know whether the INNER's config had reporting enabled
    (toml discovery is cwd-relative and the pane's cwd may differ). A plain path
    in, the literal filename here — no ``report`` import (acyclic).

    Raises :class:`MonitorLaunchError` if the session can't be created, so the CLI
    can fall back to a normal run rather than failing the task.
    """
    _sweep_stale_run_dirs()  # bound the detach-early tempdir leak (best-effort)
    try:
        run_dir = Path(tempfile.mkdtemp(prefix="syncmill-monitor-"))
    except OSError as exc:
        # mkdtemp itself can fail (missing/unwritable TMPDIR) BEFORE run_dir exists —
        # convert to MonitorLaunchError so the CLI falls back to a normal unmonitored
        # run rather than crashing the task (the monitor is a convenience, never
        # required). This sits OUTSIDE the setup/launch try below precisely because
        # that block's except rmtrees run_dir, which has not been created yet here.
        raise MonitorLaunchError(str(exc)) from exc
    session = f"{session_prefix}-{uuid4().hex[:8]}"

    # Set up + launch under one guard so ANY setup failure (disk full on the
    # sentinel/snapshot write — OSError; a non-UTF8 env var — ValueError; the
    # exec/arg-limit on new-session — OSError) becomes a MonitorLaunchError the CLI
    # falls back on, rather than crashing the task. The pane runs:
    # <outer python> -c <shim> <run_dir> <inner argv…> — sys.executable is an
    # absolute interpreter path (resolves regardless of the pane's PATH); the shim
    # restores the outer env (incl. PATH) and execs the real `syncmill`.
    # The run's stable id — the OUTER mints it because it never builds a Supervisor
    # (the resolver's "fresh" path, performed here on the inner's behalf); the inner's
    # `resolve_run_id` picks it up from the snapshot env, gated on `inner_dir()`.
    run_id = str(uuid4())
    try:
        (run_dir / SENTINEL_NAME).write_text("")  # mark the inner process
        # outer env the shim restores (0600, secrets safe) + the minted run_id
        _write_env_snapshot(run_dir, run_id)
        shim_cmd = [sys.executable, "-c", _ENV_SHIM, str(run_dir), *inner_argv]
        created = _tmux(
            "new-session", "-d", "-s", session, "-x", "220", "-y", "50", *shim_cmd
        )
    except (OSError, ValueError) as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise MonitorLaunchError(str(exc)) from exc
    if created.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise command_error(MonitorLaunchError, "tmux new-session failed", created.stderr)

    # Visible pane titles (best-effort; harmless if the user's tmux overrides it).
    _tmux("set-option", "-t", session, "pane-border-status", "top")
    _tmux("set-option", "-t", session, "pane-border-format", " #{pane_title} ")
    _tmux("select-pane", "-t", session, "-T", "syncmill (control)")

    attach_rc = _tmux_attach(session)  # FOREGROUND — blocks until the user detaches

    data = _await_result(run_dir)
    if data is not None:
        _tmux("kill-session", "-t", session)
        code = render_result(data)
        if emit_patch is not None and not emit_patch_file(emit_patch, data):
            code = code or 1  # explicit export failed — never exit 0
        if report_runs_dir is not None:
            # The INNER (the real Supervisor) wrote the envelope under the
            # OUTER-minted run_id; existence is the only truthful enablement gate.
            # Best-effort like every report surface: expanduser raises RuntimeError
            # with no HOME, exists() can raise OSError — neither may abort the
            # monitored run's exit path or skip the run-dir cleanup below.
            try:
                envelope = (
                    Path(report_runs_dir).expanduser() / run_id / "envelope.json"
                )
                if envelope.exists():
                    echo(f"[syncmill] report: {printable_name(str(envelope))}")
            except (OSError, RuntimeError, ValueError):
                secho(
                    "[syncmill] report path unavailable — continuing", fg="yellow"
                )
        shutil.rmtree(run_dir, ignore_errors=True)
        return code

    # No result yet — the run continues (or attach failed); either way there is no
    # patch to export yet. Point at result.json's `patch` field so a detach-early
    # user can still extract it once the inner finishes. The hint must be a
    # byte-preserving extractor (NOT `jq -r`, which replaces the JSON-escaped
    # surrogateescape bytes of a non-UTF-8 patch with U+FFFD, silently corrupting
    # it) — mirror emit_patch_file's surrogateescape write exactly.
    if emit_patch is not None:
        source, dest = str(run_dir / _RESULT_FILE), emit_patch
        source_arg, dest_arg = shell_arg(source), shell_arg(dest)
        if source_arg and dest_arg:
            extractor = (
                "python3 -c 'import json,sys; "
                'open(sys.argv[2],"w",encoding="utf-8",errors="surrogateescape")'
                '.write(json.load(open(sys.argv[1]))["patch"] or "")\' '
                f"{source_arg} {dest_arg}"
            )
            secho(
                "[syncmill] --emit-patch: no result yet — once finished, extract with: "
                f"{extractor}",
                fg="yellow",
            )
        else:
            secho(
                "[syncmill] --emit-patch: no result yet — once finished, write the "
                f"`patch` field of {printable_name(source)} to {printable_name(dest)} "
                f"({NO_COMMAND})",
                fg="yellow",
            )

    # Distinguish a failed attach from a clean detach-before-finish. The session
    # name carries the configured prefix and the result path the temp root.
    reattach = printable_command("tmux", "attach", options=["-t", session])
    shown_result = printable_name(str(run_dir / _RESULT_FILE))
    if attach_rc != 0:
        secho(
            f"[syncmill] could not attach to the live monitor (tmux exit {attach_rc}) "
            f"— the run continues in tmux.\n"
            f"           re-attach: {reattach}\n"
            f"           result:    {shown_result}",
            fg="red",
        )
        return 1
    secho(
        "[syncmill] detached before the run finished — it continues in tmux.\n"
        f"           re-attach: {reattach}\n"
        f"           result:    {shown_result}",
        fg="yellow",
    )
    return 0


#: A monitor run dir older than this is assumed orphaned (a detach-before-completion
#: that nobody reaped) and swept on the next launch — far longer than any real run,
#: so a still-attached/running inner's dir is never touched.
_STALE_RUN_DIR_SECONDS = 24 * 3600


def _sweep_stale_run_dirs() -> None:
    """Remove orphaned ``syncmill-monitor-*`` dirs from prior detach-early runs.

    The detach-before-completion path deliberately leaves its run dir so the still-
    running inner can finish writing ``result.json``; nothing else reaps it. This
    age-bounded sweep keeps that from accumulating in ``/tmp`` without a GC registry
    (and without risking a live run — the threshold dwarfs any real run). Fully
    best-effort: any error is ignored.

    Requires the inner ``SENTINEL_NAME`` file before removing anything — the prefix
    match alone would rmtree any same-prefixed dir syncmill didn't create, so the
    sentinel (written into every real run dir at launch) is the proof of ownership.
    """
    tmp = Path(tempfile.gettempdir())
    try:
        entries = list(tmp.glob("syncmill-monitor-*"))
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if (
                entry.is_dir()
                and (entry / SENTINEL_NAME).is_file()
                and time.time() - entry.stat().st_mtime > _STALE_RUN_DIR_SECONDS
            ):
                shutil.rmtree(entry, ignore_errors=True)


def _write_env_snapshot(run_dir: Path, run_id: str) -> None:
    """Write the OUTER process's environment to ``<run_dir>/env`` (mode ``0600``).

    The exec shim (:data:`_ENV_SHIM`) loads this so the in-tmux INNER runs with the
    outer's environment — load-bearing because a new tmux session inherits the
    (possibly stale) tmux **server** env, not the outer client's, so a venv
    ``syncmill``, the user's PATH, their ``SYNCMILL_*`` config, and agent auth would
    otherwise be invisible in the pane. A FILE (not ``-e`` argv) keeps secrets out
    of the tmux command-line and the launch argv tiny. The tmux-owned vars (TERM,
    TMUX, TMUX_PANE) are excluded so the pane keeps its own; ``SYNCMILL_MONITOR_DIR``
    is added so the inner detects itself, and ``SYNCMILL_RUN_ID`` carries the
    OUTER-minted run_id to the inner's ``resolve_run_id`` (assigned AFTER the
    comprehension, so a stale exported ``SYNCMILL_RUN_ID`` in the outer's own env
    is overwritten, never forwarded).
    """
    snapshot = {
        k: v for k, v in os.environ.items() if k not in _SNAPSHOT_SKIP_ENV
    }
    snapshot[INNER_ENV] = str(run_dir)
    snapshot[RUN_ID_ENV] = run_id
    fd = os.open(
        str(run_dir / "env"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(fd, "w") as fh:
        json.dump(snapshot, fh)


def _await_result(run_dir: Path) -> dict | None:
    """Poll for the inner's ``result.json`` for a short grace window, then give up
    (the user detached before it finished)."""
    deadline = time.monotonic() + _RESULT_GRACE_SECONDS
    while True:
        data = read_result(run_dir)
        if data is not None:
            return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(_RESULT_POLL_INTERVAL)
