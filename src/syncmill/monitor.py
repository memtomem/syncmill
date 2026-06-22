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
import tempfile
import time
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

import click

from syncmill.agents import stream_to
from syncmill.config import MonitorConfig
from syncmill.models import TaskResult, TaskStatus

# ── Shared: the outer↔inner handshake, tmux plumbing, result hand-off ──────

#: Env var the OUTER launcher sets on the INNER process, naming the shared run
#: dir. INTERNAL IPC, not user config — deliberately outside ``SYNCMILL_MONITOR__*``.
INNER_ENV = "SYNCMILL_MONITOR_DIR"
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


def _tmux_attach(session: str) -> None:
    """Attach the real terminal to ``session`` — FOREGROUND, blocks until the user
    detaches (Ctrl-b d) or the session ends.

    Must inherit all three std streams (NO ``capture_output``, NO ``stdin=PIPE``)
    or tmux can't draw the UI and hangs. Nothing may write the controlling
    terminal between the call and its return.
    """
    subprocess.run(["tmux", "attach-session", "-t", session])


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
        "gate_summary": result.gate.summary() if result.gate is not None else None,
        "gate_passed": result.gate.passed if result.gate is not None else None,
    }


def write_result(run_dir: str | Path, result: TaskResult) -> None:
    """Atomically write the inner's result to ``<run_dir>/result.json`` (temp +
    ``os.replace``), so the OUTER never reads a half-written file."""
    path = Path(run_dir) / _RESULT_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(_result_to_json(result)))
    os.replace(tmp, path)


def read_result(run_dir: str | Path) -> dict | None:
    """Read ``<run_dir>/result.json``; None if absent or unreadable (the inner
    hasn't finished, or never wrote it)."""
    try:
        return json.loads((Path(run_dir) / _RESULT_FILE).read_text())
    except (OSError, ValueError):
        return None


def render_result(data: dict) -> int:
    """Echo a result.json dict the way the CLI prints a live result, and return the
    process exit code (0 completed, 1 failed). Shared by the OUTER (after detach)
    and the CLI's normal path, so a monitored and unmonitored run print identically.
    """
    if data.get("status") == str(TaskStatus.COMPLETED):
        click.secho(
            f"[{data.get('agent_id')}] Completed in {data.get('duration_seconds')}s",
            fg="green",
        )
        if data.get("gate_summary") is not None:
            click.secho(
                f"[syncmill] Quality gate: passed ({data['gate_summary']})", fg="green"
            )
        if data.get("files_changed"):
            click.echo(f"[syncmill] Files changed: {', '.join(data['files_changed'])}")
        click.echo()
        click.echo(data.get("output") or "")
        return 0
    click.secho("[syncmill] Failed", fg="red")
    if data.get("error"):
        click.echo(data["error"])
    return 1


def render_task_result(result: TaskResult) -> int:
    """Render a live ``TaskResult`` through the same path as a monitored run's
    ``result.json`` (:func:`render_result`), so the monitored and unmonitored CLI
    displays are byte-identical — the user sees the same thing either way."""
    return render_result(_result_to_json(result))


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


class FileSink:
    """An :class:`~syncmill.agents.OutputSink` that appends each agent's streamed
    chunks to ``<run_dir>/agent-<id>.log`` via a single non-blocking ``os.write``.

    No Python buffering and no ``fsync``: ``feed`` runs inside the agent's drain
    loop on the shared event loop, so it must not block (a slow/NFS/full-disk dir
    must not stall every other agent's drain *and* memtomem's I/O) and must never
    raise out — a dropped write loses a log chunk, never the run. The fd is opened
    once per agent in ``O_APPEND`` mode and reused, so a council member's output
    across propose/critique/synthesize accumulates in one log.
    """

    def __init__(self, run_dir: Path):
        self._run_dir = run_dir
        self._fds: dict[str, int] = {}

    def path_for(self, agent_id: str) -> Path:
        return self._run_dir / f"agent-{_safe(agent_id)}.log"

    def _fd(self, agent_id: str) -> int | None:
        fd = self._fds.get(agent_id)
        if fd is None:
            try:
                fd = os.open(
                    str(self.path_for(agent_id)),
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                    0o644,
                )
            except OSError:
                return None
            self._fds[agent_id] = fd
        return fd

    def feed(self, agent_id: str, chunk: bytes) -> None:
        fd = self._fd(agent_id)
        if fd is None:
            return
        try:
            os.write(fd, chunk)
        except OSError:
            pass  # best-effort: a failed log write never breaks the run

    def close(self) -> None:
        for fd in self._fds.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self._fds.clear()


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
        write_result(self._run_dir, result)
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
    inner_argv: list[str], *, session_prefix: str = "syncmill"
) -> int:
    """Run ``inner_argv`` (a reconstructed ``syncmill run …``) inside tmux, attach
    live, and return the run's exit code.

    Builds a detached session whose single control pane runs ``inner_argv`` with
    ``SYNCMILL_MONITOR_DIR`` set — argv passed DIRECTLY and the env via tmux's
    ``-e`` flag, so the user's login shell never re-parses the command (no
    injection, works under fish/csh which lack ``VAR=val cmd``). The inner adds its
    own live panes. After ``tmux attach`` returns (the user detached), it reads
    ``result.json``: present → tear the session down, render, return the code;
    absent → the user detached before completion, so it leaves the session +
    run dir intact (the run continues), prints how to re-attach, and returns 0.

    Raises :class:`MonitorLaunchError` if the session can't be created, so the CLI
    can fall back to a normal run rather than failing the task.
    """
    run_dir = Path(tempfile.mkdtemp(prefix="syncmill-monitor-"))
    (run_dir / SENTINEL_NAME).write_text("")  # mark the inner process
    session = f"{session_prefix}-{uuid4().hex[:8]}"

    created = _tmux(
        "new-session", "-d", "-s", session, "-x", "220", "-y", "50",
        "-e", f"{INNER_ENV}={run_dir}",
        *inner_argv,
    )
    if created.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise MonitorLaunchError(created.stderr.strip() or "tmux new-session failed")

    # Visible pane titles (best-effort; harmless if the user's tmux overrides it).
    _tmux("set-option", "-t", session, "pane-border-status", "top")
    _tmux("set-option", "-t", session, "pane-border-format", " #{pane_title} ")
    _tmux("select-pane", "-t", session, "-T", "syncmill (control)")

    _tmux_attach(session)  # FOREGROUND — blocks until the user detaches

    data = _await_result(run_dir)
    if data is None:
        click.secho(
            "[syncmill] detached before the run finished — it continues in tmux.\n"
            f"           re-attach: tmux attach -t {session}\n"
            f"           result:    {run_dir / _RESULT_FILE}",
            fg="yellow",
        )
        return 0

    _tmux("kill-session", "-t", session)
    code = render_result(data)
    shutil.rmtree(run_dir, ignore_errors=True)
    return code


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
