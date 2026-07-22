"""Hermetic room-viewer tests: never launch real tmux.

Every tmux control call funnels through ``room_viewer._tmux`` /
``room_viewer._tmux_attach`` / ``room_viewer.tmux_available``, patched here at
the ROOM_VIEWER lookup site (not ``monitor._tmux``) — room_viewer imports those
names, so the reference to rebind lives in room_viewer's namespace (the
strategies.base ``_git_changed_files`` lesson).
"""

from __future__ import annotations

import os
import shutil
import time
import types
from pathlib import Path

import pytest

from conftest import FakeAgent
from syncmill import monitor as _monitor
from syncmill import room_viewer as V
from syncmill.config import SyncmillConfig
from syncmill.models import TaskResult, TaskStatus
from syncmill.rooms import RoomCoordinator

_TMUX_OPT_IN = os.environ.get("SYNCMILL_TEST_ROOM_VIEWER_TMUX") == "1"
pytestmark_tmux = pytest.mark.skipif(
    not (_TMUX_OPT_IN and shutil.which("tmux")),
    reason="set SYNCMILL_TEST_ROOM_VIEWER_TMUX=1 with tmux installed to run real-tmux tests",
)


class WritingAgent(FakeAgent):
    def __init__(self, agent_id: str, *, status: TaskStatus = TaskStatus.COMPLETED):
        super().__init__(agent_id, status=status, output=f"out {agent_id}", exit_code=0)

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        (workdir / f"{self.agent_id}.txt").write_text("x", encoding="utf-8")
        return self.execute(prompt, workdir, timeout)


def _config(tmp_path: Path) -> SyncmillConfig:
    return SyncmillConfig(
        project={"id": "viewer-tests"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True, "max_members": 3, "max_parallel": 3},
    )


def _ns(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeTmux:
    """Records calls and models session existence (new-session adds, has-session
    reflects, list-sessions renders, kill-session removes), with ``=`` exact
    matching like the real target resolution."""

    def __init__(self, *, existing=None, new_session_rc: int = 0):
        self.calls: list[tuple] = []
        self.existing: set[str] = set(existing or [])
        self.new_session_rc = new_session_rc

    def _match(self, target: str) -> str | None:
        if target.startswith("="):
            exact = target[1:]
            return exact if exact in self.existing else None
        return target if target in self.existing else None

    def __call__(self, *args):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "has-session":
            return _ns(0 if self._match(args[args.index("-t") + 1]) else 1)
        if cmd == "new-session":
            if self.new_session_rc != 0:
                return _ns(self.new_session_rc, stderr="boom")
            self.existing.add(args[args.index("-s") + 1])
            return _ns(0)
        if cmd == "list-sessions":
            return _ns(0, stdout="\n".join(sorted(self.existing)))
        if cmd == "kill-session":
            matched = self._match(args[args.index("-t") + 1])
            if matched:
                self.existing.discard(matched)
                return _ns(0)
            return _ns(1)
        if cmd == "split-window":
            return _ns(0, stdout="%1")
        return _ns(0)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _room_with_round(tmp_path, git_repo, members=("claude", "codex")):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent(m) for m in members]
    )
    room = coordinator.create("compare", list(members))
    coordinator.run_round(room["id"])
    return coordinator, room


_PROJ = "proj-1"


# ── names / parsing ─────────────────────────────────────────────────────────


def test_viewer_session_name_is_project_scoped_and_deterministic():
    name = V.viewer_session_name(_PROJ, "abc-123", 2)
    assert name == "syncmill-room-proj-1-abc-123-r2"
    assert name == V.viewer_session_name(_PROJ, "abc-123", 2)
    # A different project never shares a name for the same room/round.
    assert V.viewer_session_name("proj-2", "abc-123", 2) != name


def test_project_id_sanitized_in_name():
    # A project id with tmux-hostile characters is made name-safe.
    name = V.viewer_session_name("a.b:c d", "room1", 1)
    assert "." not in name and ":" not in name and " " not in name


def test_room_id_recovered_from_viewer_name_within_project():
    room_id = "3ca4485a-1111-2222-3333-444455556666"
    name = V.viewer_session_name(_PROJ, room_id, 7)
    assert V._room_id_from_name(name, _PROJ) == room_id
    # A name from ANOTHER project is not ours to parse.
    assert V._room_id_from_name(name, "other-proj") is None
    assert V._room_id_from_name("not-a-viewer", _PROJ) is None


# ── ensure_viewer ─────────────────────────────────────────────────────────────


def test_ensure_viewer_requires_tmux(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    monkeypatch.setattr(V, "tmux_available", lambda: False)
    try:
        with pytest.raises(V.RoomViewerError, match="tmux is not available"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
    finally:
        coordinator.close()


def test_ensure_viewer_creates_pane_per_member_and_is_idempotent(
    monkeypatch, tmp_path, git_repo
):
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude", "codex"))
    fake = _FakeTmux()
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        name = V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
        assert name == V.viewer_session_name(coordinator.project_id, room["id"], 1)
        assert fake.names().count("new-session") == 1
        assert fake.names().count("split-window") == 1
        before = len(fake.calls)
        assert V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"]) == name
        assert "new-session" not in fake.names()[before:]
    finally:
        coordinator.close()


def test_ensure_viewer_split_failure_is_atomic(monkeypatch, tmp_path, git_repo):
    # A partial viewer (only some members) is worse than none: a split failure
    # kills the session and raises, so a later attach recreates it cleanly.
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude", "codex"))

    class SplitFails(_FakeTmux):
        def __call__(self, *args):
            if args[0] == "split-window":
                self.calls.append(args)
                return _ns(1, stderr="split boom")
            return super().__call__(*args)

    fake = SplitFails()
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        with pytest.raises(V.RoomViewerError, match="split boom"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
        # The half-built session was torn down.
        assert V.viewer_session_name(coordinator.project_id, room["id"], 1) not in fake.existing
        assert "kill-session" in fake.names()
    finally:
        coordinator.close()


def test_ensure_viewer_race_accepts_concurrent_winner(monkeypatch, tmp_path, git_repo):
    # If new-session fails but the session now exists (a concurrent attach won),
    # accept it rather than raising a spurious duplicate error.
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    name = V.viewer_session_name(coordinator.project_id, room["id"], 1)

    class LosesRace(_FakeTmux):
        def __call__(self, *args):
            if args[0] == "new-session":
                self.calls.append(args)
                self.existing.add(name)  # the winner created it
                return _ns(1, stderr="duplicate session")
            return super().__call__(*args)

    fake = LosesRace()
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        assert V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"]) == name
    finally:
        coordinator.close()


def test_ensure_viewer_unknown_round_and_room(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", _FakeTmux())
    try:
        with pytest.raises(V.RoomViewerError, match="no round 9"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"], round_number=9)
        from syncmill.rooms import RoomError

        with pytest.raises(RoomError, match="unknown room"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, "no-such-room")
    finally:
        coordinator.close()


def test_ensure_viewer_reports_terminal_unavailable_without_progress(
    monkeypatch, tmp_path, git_repo
):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    round_id = coordinator.store.list_candidates(room["id"])[0]["round_id"]
    coordinator.store._conn.execute("DELETE FROM round_members WHERE round_id=?", (round_id,))
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", _FakeTmux())
    try:
        with pytest.raises(V.RoomViewerError, match="terminal unavailable"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
    finally:
        coordinator.close()


def test_ensure_viewer_rejects_drifted_log_path(monkeypatch, tmp_path, git_repo):
    # A log_path in the DB that differs from the canonical recomputed path is not
    # followed (fail-closed against an absolute/traversal/symlink value).
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    round_id = coordinator.store.list_candidates(room["id"])[0]["round_id"]
    coordinator.store._conn.execute(
        "UPDATE round_members SET log_path=? WHERE round_id=?",
        ("/etc/passwd", round_id),
    )
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", _FakeTmux())
    try:
        with pytest.raises(V.RoomViewerError, match="no valid member logs"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
    finally:
        coordinator.close()


# ── attach state machines ─────────────────────────────────────────────────────


def test_attach_external_uses_foreground_attach(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    attached = []
    monkeypatch.setattr(V, "_tmux_attach", lambda name: attached.append(name) or 0)
    monkeypatch.setattr(V, "_tmux", lambda *a: pytest.fail("switch-client used externally"))
    assert V.attach_viewer("syncmill-room-p-x-r1") == 0
    assert attached == ["=syncmill-room-p-x-r1"]


def test_attach_inside_tmux_uses_switch_client(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    calls = []
    monkeypatch.setattr(V, "_tmux", lambda *a: calls.append(a) or _ns(0))
    monkeypatch.setattr(V, "_tmux_attach", lambda name: pytest.fail("attach used in tmux"))
    assert V.attach_viewer("syncmill-room-p-x-r1") == 0
    assert calls == [("switch-client", "-E", "-t", "=syncmill-room-p-x-r1")]


# ── teardown / sweep ─────────────────────────────────────────────────────────


def test_close_room_viewers_scoped_to_exact_room(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    proj = coordinator.project_id
    mine = V.viewer_session_name(proj, room["id"], 1)
    other_room = V.viewer_session_name(proj, "otherroom-0000", 1)
    fake = _FakeTmux(existing={mine, other_room})
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        assert V.close_room_viewers(coordinator.store, proj, room["id"]) == 1
        assert mine not in fake.existing
        assert other_room in fake.existing  # a different room's viewer is untouched
    finally:
        coordinator.close()


def test_sweep_never_touches_another_projects_viewers(monkeypatch, tmp_path, git_repo):
    # The blocker: sweeping project A must not reclaim project B's live viewers
    # just because B's rooms are absent from A's DB.
    coordinator, room = _room_with_round(tmp_path, git_repo)
    proj = coordinator.project_id
    mine = V.viewer_session_name(proj, room["id"], 1)
    foreign = V.viewer_session_name("some-other-project", "live-room-1234", 1)
    user_session = "my-work"  # a plain user session, not a viewer at all
    fake = _FakeTmux(existing={mine, foreign, user_session})
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        # This project's room is live, so nothing of ours is reclaimed either.
        assert V.sweep_orphan_viewers(coordinator.store, proj) == 0
        assert fake.existing == {mine, foreign, user_session}
    finally:
        coordinator.close()


def test_sweep_reclaims_orphan_and_terminal_viewers(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    proj = coordinator.project_id
    live = V.viewer_session_name(proj, room["id"], 1)
    orphan = V.viewer_session_name(proj, "dead0000-0000-0000-0000-000000000000", 1)
    fake = _FakeTmux(existing={live, orphan})
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        assert V.sweep_orphan_viewers(coordinator.store, proj) == 1
        assert live in fake.existing
        assert orphan not in fake.existing
        coordinator.store._conn.execute(
            "UPDATE rooms SET state='closed' WHERE id=?", (room["id"],)
        )
        assert V.sweep_orphan_viewers(coordinator.store, proj) == 1
        assert live not in fake.existing
    finally:
        coordinator.close()


def test_teardown_and_sweep_noop_without_tmux(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    monkeypatch.setattr(V, "tmux_available", lambda: False)
    monkeypatch.setattr(V, "_tmux", lambda *a: pytest.fail("tmux called when unavailable"))
    try:
        assert V.close_room_viewers(coordinator.store, coordinator.project_id, room["id"]) == 0
        assert V.sweep_orphan_viewers(coordinator.store, coordinator.project_id) == 0
    finally:
        coordinator.close()


# ── opt-in real tmux (CI: Ubuntu/macOS) ──────────────────────────────────────
#
# Gated on SYNCMILL_TEST_ROOM_VIEWER_TMUX=1 and a tmux binary (see the skip mark
# at the top). Runs against an ISOLATED tmux server (a private TMUX_TMPDIR socket
# dir) so it never touches the developer's own sessions, killed on teardown.


@pytest.fixture
def isolated_tmux(monkeypatch):
    """A private tmux server via a SHORT TMUX_TMPDIR, killed on teardown.

    The socket dir must be short: an AF_UNIX path is capped near 104 bytes on
    macOS, and pytest's tmp_path is far too long, so this uses a brief unique
    dir under the system temp root rather than a nested fixture path.
    """
    import tempfile
    import uuid

    sockdir = Path(tempfile.gettempdir()) / f"smv-{uuid.uuid4().hex[:8]}"
    sockdir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(sockdir))
    monkeypatch.delenv("TMUX", raising=False)  # not inside any client
    try:
        yield
    finally:
        _monitor._tmux("kill-server")
        shutil.rmtree(sockdir, ignore_errors=True)


@pytestmark_tmux
def test_real_tmux_viewer_one_pane_per_member_and_tails_log(
    isolated_tmux, tmp_path, git_repo
):
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude", "codex"))
    try:
        name = V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
        # The session exists with exactly one pane per member.
        assert V._session_exists(name)
        panes = _monitor._tmux(
            "list-panes", "-t", V._exact(name), "-F", "#{pane_id}"
        ).stdout.split()
        assert len(panes) == 2
        # tail -F reflects a subsequent append to a member log.
        members = coordinator.store.list_round_members(
            coordinator.store.list_candidates(room["id"])[0]["round_id"]
        )
        log = coordinator.store.candidates.root / members[0]["log_path"]
        with open(log, "a") as fh:
            fh.write("SENTINEL_LINE\n")
            fh.flush()
        time.sleep(0.6)
        captured = "".join(
            _monitor._tmux("capture-pane", "-p", "-t", pane).stdout for pane in panes
        )
        assert "SENTINEL_LINE" in captured
    finally:
        coordinator.close()


@pytestmark_tmux
def test_real_tmux_attach_nonexistent_target_fails(isolated_tmux):
    # switch-client / attach to a missing session returns nonzero (surfaced by
    # the CLI/TUI as an attach error), never a silent success.
    assert V.attach_viewer("syncmill-room-nope-nope-r1") != 0


@pytestmark_tmux
def test_real_tmux_close_room_viewers_reclaims(isolated_tmux, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    try:
        name = V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
        assert V._session_exists(name)
        assert V.close_room_viewers(coordinator.store, coordinator.project_id, room["id"]) == 1
        assert not V._session_exists(name)
    finally:
        coordinator.close()
