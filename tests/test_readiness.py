"""Detached launchers wait for their worker's readiness byte (#281).

A launcher hands its worker the write end of a pipe; the worker writes one byte
once a later failure would show somewhere else (a job's ``state.json``, the
room), and EOF without that byte means it died first. These tests drive real
processes for the pipe and the job manager, and the real ``run_round`` for the
room's readiness point.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from syncmill import readiness
from syncmill.cli import cli
from syncmill.config import SyncmillConfig, TuiConfig
from syncmill.readiness import Outcome, ReadyWaiter, ready_pipe, signal_ready
from syncmill.rooms import (
    RoomError,
    RoomWorkerLaunch,
    _open_worker_log,
    _prune_worker_logs,
    launch_room_worker,
)
from syncmill.tui_jobs import (
    ArtifactApplySpec,
    JobError,
    JobStore,
    LaunchedJob,
    _socket_path,
)
from test_room_repair import Harness, Member


# ── the pipe ──────────────────────────────────────────────────────────────


def _child(code: str, write_fd: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(write_fd)], pass_fds=(write_fd,)
    )
    os.close(write_fd)
    return proc


_SIGNAL = (
    "import os, sys, time\n"
    "from syncmill.readiness import signal_ready\n"
    "time.sleep(float(os.environ.get('DELAY', '0')))\n"
    "signal_ready(int(sys.argv[1]))\n"
)


def test_a_byte_is_ready_even_from_a_worker_that_exits_at_once():
    waiter, write_fd = ready_pipe()
    proc = _child(_SIGNAL, write_fd)
    assert waiter.wait(5.0) is Outcome.READY
    proc.wait(5)


def test_a_slow_worker_is_ready_within_the_bound(monkeypatch):
    monkeypatch.setenv("DELAY", "0.5")
    waiter, write_fd = ready_pipe()
    proc = _child(_SIGNAL, write_fd)
    started = time.monotonic()
    assert waiter.wait(5.0) is Outcome.READY
    assert time.monotonic() - started >= 0.4
    proc.wait(5)


def test_eof_without_the_byte_is_exited():
    waiter, write_fd = ready_pipe()
    proc = _child("import sys; sys.exit(3)", write_fd)
    assert waiter.wait(5.0) is Outcome.EXITED
    assert proc.wait(5) == 3


def test_no_byte_in_time_is_pending_and_the_worker_is_left_alone():
    waiter, write_fd = ready_pipe()
    proc = _child(
        "import sys, time\n"
        "from syncmill.readiness import signal_ready\n"
        "time.sleep(1.0)\n"
        "signal_ready(int(sys.argv[1]))\n"
        "print('finished', flush=True)\n",
        write_fd,
    )
    assert waiter.wait(0.2) is Outcome.PENDING
    assert proc.poll() is None
    # The late byte hits a closed read end (EPIPE) and the worker still exits 0.
    assert proc.wait(10) == 0


def test_a_long_lived_grandchild_does_not_hide_the_workers_exit(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    waiter, write_fd = ready_pipe()
    proc = _child(
        "import pathlib, subprocess, sys\n"
        "from syncmill.readiness import adopt_ready_fd\n"
        "adopt_ready_fd(int(sys.argv[1]))\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " close_fds=False)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(g.pid))\n"
        "sys.exit(4)\n",
        write_fd,
    )
    try:
        started = time.monotonic()
        assert waiter.wait(10.0) is Outcome.EXITED
        assert time.monotonic() - started < 5.0
        proc.wait(5)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


def test_cancel_ends_the_wait_as_pending():
    waiter, write_fd = ready_pipe()
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    try:
        assert waiter.wait(10.0, cancel=cancel) is Outcome.PENDING
    finally:
        os.close(write_fd)


def test_signal_ready_never_raises():
    waiter, write_fd = ready_pipe()
    waiter.close()
    signal_ready(write_fd)  # EPIPE
    signal_ready(write_fd)  # EBADF: already closed
    signal_ready(None)


def test_log_tail_keeps_the_last_lines(tmp_path):
    log = tmp_path / "boot.log"
    log.write_text("".join(f"line {n}\n" for n in range(100)) + "\n\n")
    assert readiness.log_tail(log, max_lines=3) == ["line 97", "line 98", "line 99"]
    assert readiness.log_tail(tmp_path / "missing.log") == []


# ── the job manager (real processes) ─────────────────────────────────────

_SITECUSTOMIZE = '''
import os, sys, time
mode = os.environ.get("SYNCMILL_TEST_READY_MODE", "")
if mode == "boot-die":
    print("simulated bootstrap failure", file=sys.stderr, flush=True)
    os._exit(3)
if mode == "pre-park":
    # Park the manager as it spawns the job child: before _mark_running.
    import subprocess
    real_popen = subprocess.Popen
    flag = os.environ["SYNCMILL_TEST_READY_FLAG"]

    def parked_popen(argv, *args, **kwargs):
        if "child" in argv:
            with open(flag + ".pid", "w") as handle:
                handle.write(str(os.getpid()))
            time.sleep(60)
        return real_popen(argv, *args, **kwargs)

    subprocess.Popen = parked_popen
elif mode:
    import syncmill.readiness as r
    original = r.signal_ready
    flag = os.environ["SYNCMILL_TEST_READY_FLAG"]

    def signal_ready(fd):
        with open(flag + ".pid", "w") as handle:
            handle.write(str(os.getpid()))
        if mode == "delay":
            time.sleep(0.5)
        elif mode == "park":
            while not os.path.exists(flag + ".release"):
                time.sleep(0.05)
        elif mode == "die":
            os._exit(9)
        original(fd)

    r.signal_ready = signal_ready
'''


@pytest.fixture
def jobs(tmp_path, monkeypatch, git_repo):
    """A real JobStore whose manager runs with a sitecustomize that can delay,
    park or kill it at the readiness point."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    monkeypatch.setenv("PYTHONPATH", f"{site}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    flag = tmp_path / "ready-flag"
    monkeypatch.setenv("SYNCMILL_TEST_READY_FLAG", str(flag))
    store = JobStore(TuiConfig(state_dir=str(tmp_path / "tui")), git_repo)
    store.flag = flag  # type: ignore[attr-defined]
    return store


def _state(row: dict) -> dict:
    return json.loads((Path(row["job_dir"]) / "state.json").read_text())


def _wait_terminal(row: dict, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _state(row)
        if state.get("status") in {"finished", "cancelled", "force_stopped", "launch_failed", "lost"}:
            return state
        time.sleep(0.1)
    raise AssertionError(f"job never finished: {_state(row)}")


def test_ready_arrives_only_once_the_job_is_supervised(jobs, monkeypatch):
    monkeypatch.setenv("SYNCMILL_TEST_READY_MODE", "delay")
    launched = jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    assert launched.waiter.wait(15.0) is Outcome.READY
    # At READY the manager is past `starting` and its control socket answers.
    state = _state(launched.row)
    assert state["status"] != "starting"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.connect(str(_socket_path(launched.row["job_id"])))
    _wait_terminal(launched.row)


def test_a_manager_that_dies_before_readiness_is_exited(jobs, monkeypatch):
    monkeypatch.setenv("SYNCMILL_TEST_READY_MODE", "boot-die")
    launched = jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    assert launched.waiter.wait(15.0) is Outcome.EXITED
    from syncmill.tui_jobs import exited_diagnostic

    summary, tail = exited_diagnostic(launched.row["job_dir"])
    assert "manager.log" in summary
    assert tail == ["simulated bootstrap failure"]
    # The launcher does not write state.json: the row stays what the worker left.
    assert _state(launched.row)["status"] == "starting"


def test_a_manager_exception_before_readiness_reports_its_error(jobs, monkeypatch):
    real_popen = subprocess.Popen

    def unreadable_token(argv, **kwargs):
        job_dir = Path(argv[argv.index("--job-dir") + 1])
        os.chmod(job_dir / "control.token", 0)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr("syncmill.tui_jobs.subprocess.Popen", unreadable_token)
    launched = jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    assert launched.waiter.wait(15.0) is Outcome.EXITED
    from syncmill.tui_jobs import exited_diagnostic

    summary, tail = exited_diagnostic(launched.row["job_dir"])
    assert summary.startswith("PermissionError")
    assert tail == []


def test_a_manager_killed_at_the_readiness_point_is_exited(jobs, monkeypatch):
    monkeypatch.setenv("SYNCMILL_TEST_READY_MODE", "die")
    launched = jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    started = time.monotonic()
    # The job child it already started runs on; it never held the ready fd.
    assert launched.waiter.wait(15.0) is Outcome.EXITED
    assert time.monotonic() - started < 10.0


def test_a_manager_killed_before_it_runs_the_job_is_exited(jobs, monkeypatch):
    monkeypatch.setenv("SYNCMILL_TEST_READY_MODE", "pre-park")
    launched = jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    pid_file = jobs.flag.with_name(jobs.flag.name + ".pid")
    deadline = time.monotonic() + 15
    while not pid_file.exists() or not pid_file.read_text():
        assert time.monotonic() < deadline, "manager never reached the job spawn"
        time.sleep(0.05)
    assert _state(launched.row)["status"] == "starting"
    os.kill(int(pid_file.read_text()), signal.SIGKILL)
    started = time.monotonic()
    assert launched.waiter.wait(10.0) is Outcome.EXITED
    assert time.monotonic() - started < 5.0
    # SIGKILL leaves nothing behind, and the launcher writes nothing either.
    assert _state(launched.row)["status"] == "starting"


def test_exit_diagnostics_survive_a_damaged_state_file(tmp_path):
    from syncmill.tui_jobs import exited_diagnostic

    (tmp_path / "manager.log").write_text("ImportError: boom\n")
    for damaged in (json.dumps({"status": [], "error": "x"}).encode(), b"\xff\xfe not utf-8"):
        (tmp_path / "state.json").write_bytes(damaged)
        summary, tail = exited_diagnostic(tmp_path)
        assert "manager.log" in summary and tail == ["ImportError: boom"]


def test_pending_then_a_late_byte_and_the_job_still_finishes(jobs, monkeypatch):
    monkeypatch.setenv("SYNCMILL_TEST_READY_MODE", "park")
    launched = jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    assert launched.waiter.wait(0.5) is Outcome.PENDING
    jobs.flag.with_name(jobs.flag.name + ".release").write_text("")
    state = _wait_terminal(launched.row)
    # The late signal hit EPIPE and was ignored: the job ran to its end.
    assert state["status"] == "finished"


def test_launch_keeps_its_contract_and_closes_the_waiter(jobs, monkeypatch):
    closed = []
    real_close = ReadyWaiter.close

    def spy(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(ReadyWaiter, "close", spy)
    row = jobs.launch(ArtifactApplySpec(run_id="no-such-run"))
    assert isinstance(row, dict) and row["job_id"]
    assert closed
    _wait_terminal(row)


def test_a_failure_after_the_spawn_says_the_job_started(jobs, monkeypatch):
    def broken_prune():
        raise OSError("disk went away")

    monkeypatch.setattr(jobs, "prune", broken_prune)
    with pytest.raises(JobError, match=r"started, but pruning old jobs failed"):
        jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))


# ── CLI wording ──────────────────────────────────────────────────────────


def _launched(tmp_path: Path, outcome: str) -> LaunchedJob:
    job_dir = tmp_path / "job"
    job_dir.mkdir(exist_ok=True)
    (job_dir / "state.json").write_text(json.dumps({"status": "starting"}))
    (job_dir / "manager.log").write_text("Traceback (most recent call last):\nImportError: boom\n")
    waiter, write_fd = ready_pipe()
    if outcome == "ready":
        signal_ready(write_fd)
    elif outcome == "exited":
        os.close(write_fd)
    else:
        threading.Timer(2.0, os.close, args=(write_fd,)).start()
    return LaunchedJob({"job_id": "abcdef0123456789", "job_dir": str(job_dir)}, waiter)


@pytest.mark.parametrize("outcome", ["ready", "exited", "pending"])
def test_detached_apply_reports_what_readiness_established(
    tmp_path, monkeypatch, git_repo, outcome
):
    from syncmill import cli as cli_module

    monkeypatch.setattr(cli_module, "CLI_READY_TIMEOUT_SECONDS", 0.3)

    class Store:
        def __init__(self, *args, **kwargs):
            pass

        def launch_detached(self, spec, *, resolved_config):
            return _launched(tmp_path, outcome)

    monkeypatch.setattr(cli_module, "JobStore", Store)
    config = SyncmillConfig(repo_root=str(git_repo), runs={"root": str(tmp_path / "runs")})
    if outcome == "exited":
        from click.exceptions import ClickException

        with pytest.raises(ClickException) as caught:
            cli_module._launch_detached_apply(config, git_repo, ArtifactApplySpec(run_id="r"))
        text = cli_module._error_text(caught.value)
        assert text.splitlines()[0].startswith(
            "detached artifact-apply job abcdef01 exited before it started: see "
        )
        assert text.splitlines()[1:] == [
            "  | Traceback (most recent call last):",
            "  | ImportError: boom",
        ]
        return
    printed = []
    monkeypatch.setattr(cli_module, "echo", printed.append)
    cli_module._launch_detached_apply(config, git_repo, ArtifactApplySpec(run_id="r"))
    if outcome == "ready":
        assert printed == [
            "[syncmill] detached artifact-apply job abcdef01 — inspect it in `syncmill tui` (Runs tab)"
        ]
    else:
        assert printed == [
            "[syncmill] detached artifact-apply job abcdef01 started; not confirmed within "
            "0.3s — inspect it in `syncmill tui` (Runs tab)"
        ]


# ── room worker ──────────────────────────────────────────────────────────


def _room_config(tmp_path: Path, git_repo: Path, *, enabled: bool) -> SyncmillConfig:
    return SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "readiness"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": enabled},
    )


def test_a_room_worker_that_refuses_before_owning_the_room_is_exited(tmp_path, git_repo):
    config = _room_config(tmp_path, git_repo, enabled=False)
    started = launch_room_worker(
        git_repo, "room-1", "a prompt that must not reach the log", resolved_config=config
    )
    assert started.waiter.wait(30.0) is Outcome.EXITED
    from syncmill.rooms import room_worker_exit_diagnostic

    summary, tail = room_worker_exit_diagnostic(started)
    assert str(started.log_path) in summary
    assert any("rooms disabled" in line for line in tail)
    assert "must not reach the log" not in started.log_path.read_text()
    assert (started.log_path.stat().st_mode & 0o777) == 0o600


def test_room_launch_failure_closes_every_fd_and_removes_its_files(
    tmp_path, git_repo, monkeypatch
):
    config = _room_config(tmp_path, git_repo, enabled=True)
    before = set(os.listdir("/dev/fd"))

    def no_spawn(*args, **kwargs):
        raise OSError("no processes left")

    monkeypatch.setattr("syncmill.rooms.subprocess.Popen", no_spawn)
    with pytest.raises(OSError, match="no processes left"):
        launch_room_worker(git_repo, "room-1", "p", resolved_config=config)
    assert set(os.listdir("/dev/fd")) <= before | {str(n) for n in range(3)}
    logs = list((tmp_path / "runs").rglob("worker-logs/*.log"))
    configs = list((tmp_path / "runs").rglob("worker-configs/*.json"))
    assert logs == [] and configs == []


def test_pruning_keeps_the_log_of_a_running_worker(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    for n in range(4):
        path = directory / f"old-{n}.log"
        path.write_text("x")
        os.utime(path, (1000 + n, 1000 + n))
    held = directory / "old-0.log"  # the oldest, but in use
    import fcntl

    fd = os.open(held, os.O_RDWR | os.O_APPEND)
    fcntl.flock(fd, fcntl.LOCK_SH)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], stdout=fd
    )
    os.close(fd)
    try:
        _prune_worker_logs(directory, keep=1)
        assert sorted(p.name for p in directory.iterdir()) == ["old-0.log", "old-3.log"]
    finally:
        holder.kill()
        holder.wait()
    _prune_worker_logs(directory, keep=1)
    assert sorted(p.name for p in directory.iterdir()) == ["old-3.log"]


def test_a_launch_log_is_locked_for_as_long_as_its_worker_holds_it(tmp_path):
    directory = tmp_path / "logs"
    path, fd = _open_worker_log(directory, "room-1")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], stdout=fd
    )
    os.close(fd)
    try:
        _prune_worker_logs(directory, keep=0)
        assert path.exists()
    finally:
        holder.kill()
        holder.wait()
    _prune_worker_logs(directory, keep=0)
    assert not path.exists()


def _round_cli(monkeypatch, tmp_path, git_repo, write_fd_action):
    """``room round --detach`` up to a launch whose waiter we control."""
    from syncmill import cli as cli_module

    events = []

    class Store:
        def record_event(self, room_id, kind, payload):
            events.append((kind, payload))

    class Coordinator:
        store = Store()

        def preflight_round(self, *args, **kwargs):
            return {}

        def close(self):
            pass

    log = tmp_path / "worker.log"
    log.write_text("click: Error: resolved room config has rooms disabled\n")

    def launch(*args, **kwargs):
        waiter, write_fd = ready_pipe()
        write_fd_action(write_fd)
        return RoomWorkerLaunch(4242, log, waiter)

    config = _room_config(tmp_path, git_repo, enabled=True)
    monkeypatch.setattr(cli_module, "_room_coordinator", lambda repo: (tmp_path, config, Coordinator()))
    monkeypatch.setattr(cli_module, "launch_room_worker", launch)
    monkeypatch.setattr(cli_module, "git_root", lambda path: tmp_path)
    monkeypatch.setattr(cli_module, "_show_round_lessons", lambda *a, **k: ())
    monkeypatch.setattr(cli_module, "CLI_READY_TIMEOUT_SECONDS", 0.3)
    return events, log


def test_room_round_detach_reports_ready(monkeypatch, tmp_path, git_repo):
    events, log = _round_cli(monkeypatch, tmp_path, git_repo, signal_ready)
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "--detach"])
    assert res.exit_code == 0, res.output
    assert res.output.strip().splitlines()[-1] == "Room worker started: pid 4242"
    assert events == [("worker.launched", {"pid": 4242, "log": str(log)})]


def test_room_round_detach_reports_an_early_exit(monkeypatch, tmp_path, git_repo):
    events, log = _round_cli(monkeypatch, tmp_path, git_repo, os.close)
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "--detach"])
    assert res.exit_code != 0
    assert f"room worker (pid 4242) exited before it took the room: see {log}" in res.output
    assert "  | click: Error: resolved room config has rooms disabled" in res.output


def test_room_round_detach_reports_pending(monkeypatch, tmp_path, git_repo):
    held: list[int] = []
    events, log = _round_cli(monkeypatch, tmp_path, git_repo, held.append)
    try:
        res = CliRunner().invoke(cli, ["room", "round", "room-1", "--detach"])
    finally:
        os.close(held[0])
    assert res.exit_code == 0, res.output
    assert res.output.strip().splitlines()[-1] == (
        f"Room worker started: pid 4242; not confirmed within 0.3s (log: {log})"
    )


def test_room_round_detach_says_the_worker_started_when_recording_fails(
    monkeypatch, tmp_path, git_repo
):
    from syncmill import cli as cli_module

    _round_cli(monkeypatch, tmp_path, git_repo, signal_ready)

    class BrokenStore:
        def record_event(self, *args):
            raise RoomError("database is locked")

    coordinator = cli_module._room_coordinator(git_repo)[2]
    coordinator.store = BrokenStore()
    monkeypatch.setattr(cli_module, "_room_coordinator", lambda repo: (tmp_path, None, coordinator))
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "--detach"])
    assert res.exit_code != 0
    assert "room worker started (pid 4242), but recording its launch failed" in res.output


# ── where run_round signals ownership (the real coordinator) ─────────────


class _Recorder:
    """Records the order of ownership, snapshot capture and agent execution."""

    def __init__(self, harness: Harness, monkeypatch):
        self.harness = harness
        self.order: list[str] = []
        import syncmill.rooms as rooms_module

        real_capture = rooms_module.capture_repository_snapshot

        def capture(*args, **kwargs):
            self.order.append("snapshot")
            return real_capture(*args, **kwargs)

        monkeypatch.setattr(rooms_module, "capture_repository_snapshot", capture)
        for member in harness.agents.values():
            member.on_call = lambda number, workdir: self.order.append("agent")

    def owned(self) -> None:
        events = [e["kind"] for e in self.harness.store.events(self.harness.room_id)]
        self.order.append(f"owned:{events[-1]}")


@pytest.fixture
def make(git_repo, tmp_path):
    made: list[Harness] = []

    def build(*agents: Member) -> Harness:
        harness = Harness(git_repo, tmp_path, list(agents))
        made.append(harness)
        return harness

    yield build
    for harness in made:
        harness.coordinator.close()


def test_a_snapshot_round_signals_after_the_reservation_before_the_snapshot(make, monkeypatch):
    room = make(Member("claude"))
    recorder = _Recorder(room, monkeypatch)
    room.round(on_owned=recorder.owned)
    assert recorder.order == ["owned:workspace.reserved", "snapshot", "agent"]


@pytest.mark.parametrize("lineage", ["fresh", "branch-from"])
def test_fresh_and_branch_rounds_signal_at_the_reservation(make, monkeypatch, lineage):
    room = make(Member("claude"))
    room.round()
    kwargs = {"lineage": lineage}
    if lineage == "branch-from":
        kwargs["base_candidate_id"] = room.candidate("claude")["id"]
    recorder = _Recorder(room, monkeypatch)
    room.round(on_owned=recorder.owned, **kwargs)
    assert recorder.order[:2] == ["owned:workspace.reserved", "snapshot"]
    assert recorder.order.count("agent") == 1 and recorder.order[-1] == "agent"


def test_a_continued_round_signals_after_the_lease_before_any_agent(make, monkeypatch):
    room = make(Member("claude"))
    room.round()
    recorder = _Recorder(room, monkeypatch)
    room.round(on_owned=recorder.owned)
    assert recorder.order == ["owned:round.started", "agent"]


def test_a_repair_round_signals_after_the_lease_before_the_agent(make, monkeypatch):
    room = make(Member("claude"))
    room.round()
    plan = room.coordinator.build_repair_plan(room.candidate("claude")["id"], "fix it")
    recorder = _Recorder(room, monkeypatch)
    room.coordinator.run_round(room.room_id, plan.prompt, target=plan, on_owned=recorder.owned)
    assert recorder.order == ["owned:round.started", "agent"]


def test_a_refused_round_never_signals(make, monkeypatch):
    room = make(Member("claude"))
    room.coordinator.close_room(room.room_id)
    calls = []
    with pytest.raises(RoomError):
        room.round(on_owned=lambda: calls.append(True))
    assert calls == []


def test_a_stale_room_never_signals(make, monkeypatch):
    room = make(Member("claude"))
    room.round()
    (room.repo / "README.md").write_text("changed under the room\n")
    calls = []
    with pytest.raises(RoomError):
        room.round(on_owned=lambda: calls.append(True))
    assert calls == []
    assert room.store.get_room(room.room_id)["state"] == "stale"


def test_a_failing_signal_never_fails_the_round(make):
    room = make(Member("claude"))

    def broken():
        raise RuntimeError("pipe went away")

    result = room.round(on_owned=broken)
    assert result["state"] == "awaiting_decision"


# ── TUI: the wait runs off the Textual thread ────────────────────────────


def _drive(coro):
    return asyncio.run(coro)


class _ReadyJobs:
    """A JobStore stand-in whose waiter reads a real pipe the test controls."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.write_fd: int | None = None
        self.waiters: list[ReadyWaiter] = []

    def launch_detached(self, spec, *, resolved_config=None):
        waiter, self.write_fd = ready_pipe()
        self.waiters.append(waiter)
        job_dir = self.tmp_path / "job"
        job_dir.mkdir(exist_ok=True)
        return LaunchedJob({"job_id": "feedc0de00000000", "job_dir": str(job_dir)}, waiter)

    def list_jobs(self):
        return []

    def active_job(self):
        return None


def _tui(tmp_path: Path, git_repo: Path):
    from syncmill.tui import SyncmillTuiApp

    return SyncmillTuiApp(
        SyncmillConfig(repo_root=str(git_repo), runs={"root": str(tmp_path / "runs")}),
        board=None,
        board_error=None,
        toml_path=git_repo / "syncmill.toml",
        refresh_seconds=3600,
        initial_tab="board",
    )


_BOARD_FIELDS = {"goal": None, "team": None, "strategy": None, "agent": None}


async def _until(pilot, predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await pilot.pause(0.05)


def test_tui_waits_off_its_thread_and_notifies_the_outcome_once(tmp_path, git_repo, monkeypatch):
    waited_on: list[int] = []
    real_wait = ReadyWaiter.wait

    def spy(self, timeout, cancel=None):
        waited_on.append(threading.get_ident())
        return real_wait(self, timeout, cancel)

    monkeypatch.setattr(ReadyWaiter, "wait", spy)

    async def scenario():
        app = _tui(tmp_path, git_repo)
        jobs = _ReadyJobs(tmp_path)
        notes: list[tuple[str, str, int]] = []
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = jobs
            monkeypatch.setattr(
                app,
                "notify",
                lambda message, severity="information", **kw: notes.append(
                    (message, severity, threading.get_ident())
                ),
            )
            ui_thread = threading.get_ident()
            app._launch_job(_BOARD_FIELDS, None)
            await pilot.pause(0.2)
            assert notes == []  # nothing until the worker answers
            signal_ready(jobs.write_fd)
            await _until(pilot, lambda: notes)
            await pilot.pause(0.2)
            assert notes == [("detached board-run job feedc0de", "information", ui_thread)]
            assert waited_on and all(ident != ui_thread for ident in waited_on)
            assert app._readiness_waiters == set()

    _drive(scenario())


def test_tui_reports_an_early_exit_with_the_managers_last_lines(tmp_path, git_repo, monkeypatch):
    import syncmill.tui as tui_module

    prepared_on: list[int] = []
    real_diagnostic = tui_module.exited_diagnostic

    def spy(job_dir):
        prepared_on.append(threading.get_ident())
        return real_diagnostic(job_dir)

    monkeypatch.setattr(tui_module, "exited_diagnostic", spy)

    async def scenario():
        app = _tui(tmp_path, git_repo)
        jobs = _ReadyJobs(tmp_path)
        notes: list[tuple[str, str]] = []
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = jobs
            monkeypatch.setattr(
                app, "notify", lambda message, severity="information", **kw: notes.append((message, severity))
            )
            app._launch_job(_BOARD_FIELDS, None)
            (tmp_path / "job" / "manager.log").write_text("ImportError: boom\n")
            os.close(jobs.write_fd)
            await _until(pilot, lambda: notes)
            message, severity = notes[0]
            assert severity == "error"
            assert message.splitlines()[0].startswith("board-run job feedc0de exited before it started: see ")
            assert message.splitlines()[1:] == ["ImportError: boom"]
            # The log was read off the Textual thread.
            assert prepared_on and prepared_on[0] != threading.get_ident()

    _drive(scenario())


def test_tui_survives_a_diagnostic_that_raises(tmp_path, git_repo, monkeypatch):
    import syncmill.tui as tui_module

    def broken(job_dir):
        raise TypeError("unhashable type: 'list'")

    monkeypatch.setattr(tui_module, "exited_diagnostic", broken)

    async def scenario():
        app = _tui(tmp_path, git_repo)
        jobs = _ReadyJobs(tmp_path)
        notes: list[tuple[str, str]] = []
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = jobs
            monkeypatch.setattr(
                app, "notify", lambda message, severity="information", **kw: notes.append((message, severity))
            )
            app._launch_job(_BOARD_FIELDS, None)
            os.close(jobs.write_fd)
            await _until(pilot, lambda: notes)
            assert notes == [
                ("could not read why the worker stopped: unhashable type: 'list'", "error")
            ]
            assert app.is_running

    _drive(scenario())


def test_tui_exit_closes_pending_waiters_and_drops_late_outcomes(tmp_path, git_repo, monkeypatch):
    notes: list[str] = []

    async def scenario():
        app = _tui(tmp_path, git_repo)
        jobs = _ReadyJobs(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = jobs
            monkeypatch.setattr(app, "notify", lambda message, **kw: notes.append(message))
            app._launch_job(_BOARD_FIELDS, None)
            await pilot.pause(0.2)
            assert len(app._readiness_waiters) == 1
        # Unmounted: the cancel flag is set and every waiter is closed.
        assert app._readiness_cancel.is_set()
        assert app._readiness_waiters == set()
        assert jobs.waiters[0]._fd is None
        # The worker answers only now: nothing reaches the gone app.
        signal_ready(jobs.write_fd)
        time.sleep(0.5)
        # A delivery that still lands after unmount is dropped by the flag.
        app._deliver_readiness("late", "information")

    _drive(scenario())
    assert notes == []


def test_tui_exit_closes_a_waiter_whose_thread_never_started(tmp_path, git_repo, monkeypatch):
    async def scenario():
        app = _tui(tmp_path, git_repo)
        jobs = _ReadyJobs(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = jobs
            monkeypatch.setattr(app, "run_worker", lambda *a, **k: None)  # cancelled before start
            app._launch_job(_BOARD_FIELDS, None)
            assert len(app._readiness_waiters) == 1
        assert app._readiness_waiters == set()
        assert jobs.waiters[0]._fd is None
        os.close(jobs.write_fd)

    _drive(scenario())


def test_the_room_worker_command_signals_when_run_round_reports_ownership(
    monkeypatch, tmp_path
):
    from syncmill import cli as cli_module

    waiter, write_fd = ready_pipe()

    class Coordinator:
        def run_round(self, room_id, prompt, **kwargs):
            kwargs["on_owned"]()
            return {}

        def close(self):
            pass

    monkeypatch.setattr(
        cli_module, "_room_coordinator", lambda repo: (tmp_path, None, Coordinator())
    )
    res = CliRunner().invoke(cli, ["room-worker", "--ready-fd", str(write_fd), "--", "room-1"])
    assert res.exit_code == 0, res.output
    assert waiter.wait(1.0) is Outcome.READY


# ── failures around the spawn ────────────────────────────────────────────


def test_a_close_racing_the_wait_never_reads_a_reused_fd(monkeypatch):
    """Force the race: while ``wait`` is inside ``select``, another thread closes
    the waiter and a new pipe holding a byte is opened. If the close could free
    the number mid-wait, the new pipe would reuse it and ``wait`` would read its
    byte as READY."""
    import syncmill.readiness as readiness_module

    waiter, write_fd = ready_pipe()
    real_select = readiness_module.select.select
    opened: list[int] = []

    def racing_select(rlist, wlist, xlist, timeout):
        if not opened:
            closer = threading.Thread(target=waiter.close)
            closer.start()
            closer.join(0.2)  # blocked while wait holds the fd
            other_read, other_write = os.pipe()
            os.write(other_write, b"R")
            opened.extend([other_read, other_write])
            return real_select(rlist, wlist, xlist, 0)
        return real_select(rlist, wlist, xlist, timeout)

    monkeypatch.setattr(readiness_module.select, "select", racing_select)
    try:
        assert waiter.wait(5.0) is Outcome.PENDING
        assert os.read(opened[0], 1) == b"R"  # nobody else consumed it
    finally:
        for fd in (*opened, write_fd):
            os.close(fd)


def test_a_readiness_pipe_that_cannot_be_made_releases_the_lock(jobs, monkeypatch):
    def no_pipe():
        raise OSError("too many open files")

    with monkeypatch.context() as patched:
        patched.setattr("syncmill.tui_jobs.ready_pipe", no_pipe)
        with pytest.raises(JobError, match="failed to launch TUI worker: too many open files"):
            jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    # The target lock was released: the same target launches now.
    launched = jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    launched.waiter.close()
    _wait_terminal(launched.row)


def test_a_failing_lock_close_still_closes_the_readiness_writer(jobs, monkeypatch):
    real_close = os.close
    closed: list[int] = []
    spawned: dict = {}

    def fake_popen(argv, **kwargs):
        spawned["lock"] = int(argv[argv.index("--lock-fd") + 1])
        spawned["ready"] = int(argv[argv.index("--ready-fd") + 1])
        return SimpleNamespace(pid=1, wait=lambda: 0)

    def close(fd):
        if fd == spawned.get("lock") and fd not in closed:
            closed.append(fd)
            real_close(fd)
            raise OSError("EIO on close")
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr("syncmill.tui_jobs.subprocess.Popen", fake_popen)
    monkeypatch.setattr("syncmill.tui_jobs.os.close", close)
    launched = jobs.launch_detached(ArtifactApplySpec(run_id="no-such-run"))
    launched.waiter.close()
    assert spawned["ready"] in closed


def test_a_room_reaper_that_cannot_start_says_the_worker_started(
    tmp_path, git_repo, monkeypatch
):
    config = _room_config(tmp_path, git_repo, enabled=True)
    monkeypatch.setattr(
        "syncmill.rooms.subprocess.Popen",
        lambda argv, **kw: SimpleNamespace(pid=4242, wait=lambda: 0),
    )

    def no_thread(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr("syncmill.rooms.threading.Thread.start", no_thread)
    with pytest.raises(RoomError, match=r"room worker started \(pid 4242\), but its reaper"):
        launch_room_worker(git_repo, "room-1", "p", resolved_config=config)


def test_a_log_without_its_lock_is_not_used(tmp_path, monkeypatch):
    import syncmill.rooms as rooms_module

    real_flock = rooms_module.fcntl.flock

    def no_shared_lock(fd, operation):
        if operation == rooms_module.fcntl.LOCK_SH:
            raise OSError(errno.ENOLCK, "No locks available")
        return real_flock(fd, operation)

    monkeypatch.setattr(rooms_module.fcntl, "flock", no_shared_lock)
    assert _open_worker_log(tmp_path / "logs", "room-1") == (None, None)
    assert list((tmp_path / "logs").iterdir()) == []


def test_a_detached_workflow_whose_session_close_fails_says_it_started(
    monkeypatch, tmp_path, git_repo
):
    from syncmill import cli as cli_module

    class Record:
        state = "planned"
        repo_root = str(git_repo.resolve())
        manifest = {"spec_sha256": "d"}
        source_fingerprint = "f"

        class spec:
            @staticmethod
            def digest():
                return "d"

    class Supervisor:
        workflow_preflight_warnings: list = []
        registry = None

        class workflow_store:
            @staticmethod
            def load(workflow_id):
                return Record()

        def close(self):
            raise OSError("memory session would not close")

    config = SyncmillConfig(repo_root=str(git_repo), runs={"root": str(tmp_path / "runs")})
    monkeypatch.setattr(cli_module, "_peek_workflow_team", lambda repo, wid: None)
    monkeypatch.setattr(cli_module, "_workflow_supervisor", lambda repo, team: (Supervisor(), config))
    monkeypatch.setattr(cli_module, "validate_workflow_policy", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "planning_calls", lambda record: 0)
    monkeypatch.setattr(cli_module, "fingerprint_matches", lambda root, stored: True)
    waiters: list[ReadyWaiter] = []

    class Jobs:
        def launch_detached(self, spec, *, resolved_config):
            waiter, write_fd = ready_pipe()
            os.close(write_fd)
            waiters.append(waiter)
            return LaunchedJob({"job_id": "0123456789abcdef", "job_dir": str(tmp_path)}, waiter)

    monkeypatch.setattr(cli_module, "_workflow_job_store", lambda *a: Jobs())
    res = CliRunner().invoke(cli, ["workflow", "run", "wf-1", "--detach"])
    assert res.exit_code != 0
    assert "detached workflow-run job 01234567 started, but closing" in res.output
    assert waiters[0]._fd is None


def test_a_failing_readiness_delivery_never_exits_the_tui(tmp_path, git_repo, monkeypatch):
    """CI caught the screen gone before unmount set the cancel flag: whatever
    the delivery raises, the readiness worker must not take the app down."""
    from textual.css.query import NoMatches

    async def scenario():
        app = _tui(tmp_path, git_repo)
        jobs = _ReadyJobs(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = jobs

            def gone(*args, **kwargs):
                raise NoMatches("No nodes match '#room-list'")

            monkeypatch.setattr(app, "notify", gone)
            app._launch_job(_BOARD_FIELDS, None)
            signal_ready(jobs.write_fd)
            # wait_for_complete() would re-raise the worker's error itself.
            await _until(pilot, lambda: all(w.is_finished for w in app.workers))
            await pilot.pause(0.2)
            assert app.is_running
            assert app.return_code is None

    _drive(scenario())
