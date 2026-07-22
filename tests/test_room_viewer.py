"""Hermetic room-viewer tests: never launch real tmux.

Every tmux control call funnels through ``room_viewer._tmux`` /
``room_viewer._tmux_attach`` / ``room_viewer.tmux_available``, patched here at
the ROOM_VIEWER lookup site (not ``monitor._tmux``) — room_viewer imports those
names, so the reference to rebind lives in room_viewer's namespace (the
strategies.base ``_git_changed_files`` lesson).
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from conftest import FakeAgent
from syncmill import room_viewer as V
from syncmill.config import SyncmillConfig
from syncmill.models import TaskResult, TaskStatus
from syncmill.rooms import RoomCoordinator


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


# ── names / parsing ─────────────────────────────────────────────────────────


def test_viewer_session_name_deterministic():
    assert V.viewer_session_name("abc-123", 2) == "syncmill-room-abc-123-r2"
    assert V.viewer_session_name("abc-123", 2) == V.viewer_session_name("abc-123", 2)


def test_room_id_recovered_from_viewer_name():
    room_id = "3ca4485a-1111-2222-3333-444455556666"
    name = V.viewer_session_name(room_id, 7)
    assert V._room_id_from_name(name) == room_id
    assert V._room_id_from_name("not-a-viewer") is None
    assert V._room_id_from_name("syncmill-room-noround") is None


# ── ensure_viewer ─────────────────────────────────────────────────────────────


def test_ensure_viewer_requires_tmux(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    monkeypatch.setattr(V, "tmux_available", lambda: False)
    try:
        with pytest.raises(V.RoomViewerError, match="tmux is not available"):
            V.ensure_viewer(coordinator.store, room["id"])
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
        name = V.ensure_viewer(coordinator.store, room["id"])
        assert name == V.viewer_session_name(room["id"], 1)
        # one new-session + one split-window for the second member.
        assert fake.names().count("new-session") == 1
        assert fake.names().count("split-window") == 1
        # Idempotent: a second call reattaches, creating nothing new.
        before = len(fake.calls)
        assert V.ensure_viewer(coordinator.store, room["id"]) == name
        assert "new-session" not in fake.names()[before:]
    finally:
        coordinator.close()


def test_ensure_viewer_unknown_round_and_room(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", _FakeTmux())
    try:
        with pytest.raises(V.RoomViewerError, match="no round 9"):
            V.ensure_viewer(coordinator.store, room["id"], round_number=9)
        from syncmill.rooms import RoomError

        with pytest.raises(RoomError, match="unknown room"):
            V.ensure_viewer(coordinator.store, "no-such-room")
    finally:
        coordinator.close()


def test_ensure_viewer_reports_terminal_unavailable_without_progress(
    monkeypatch, tmp_path, git_repo
):
    # A round with no per-member rows (pre-migration) has nothing live to follow.
    coordinator, room = _room_with_round(tmp_path, git_repo)
    round_id = coordinator.store.list_candidates(room["id"])[0]["round_id"]
    coordinator.store._conn.execute(
        "DELETE FROM round_members WHERE round_id=?", (round_id,)
    )
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", _FakeTmux())
    try:
        with pytest.raises(V.RoomViewerError, match="terminal unavailable"):
            V.ensure_viewer(coordinator.store, room["id"])
    finally:
        coordinator.close()


# ── attach state machines ─────────────────────────────────────────────────────


def test_attach_external_uses_foreground_attach(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    attached = []
    monkeypatch.setattr(V, "_tmux_attach", lambda name: attached.append(name) or 0)
    monkeypatch.setattr(V, "_tmux", lambda *a: pytest.fail("switch-client used externally"))
    assert V.attach_viewer("syncmill-room-x-r1") == 0
    assert attached == ["=syncmill-room-x-r1"]


def test_attach_inside_tmux_uses_switch_client(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    calls = []
    monkeypatch.setattr(V, "_tmux", lambda *a: calls.append(a) or _ns(0))
    monkeypatch.setattr(V, "_tmux_attach", lambda name: pytest.fail("attach used in tmux"))
    assert V.attach_viewer("syncmill-room-x-r1") == 0
    assert calls == [("switch-client", "-E", "-t", "=syncmill-room-x-r1")]


# ── teardown / sweep ─────────────────────────────────────────────────────────


def test_close_room_viewers_scoped_to_exact_room(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    other = f"syncmill-room-otherroom-{'0'*8}-r1"
    fake = _FakeTmux(existing={V.viewer_session_name(room["id"], 1), other})
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        assert V.close_room_viewers(coordinator.store, room["id"]) == 1
        assert V.viewer_session_name(room["id"], 1) not in fake.existing
        assert other in fake.existing  # a different room's viewer is untouched
    finally:
        coordinator.close()


def test_sweep_reclaims_orphan_and_terminal_viewers(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo)
    live = V.viewer_session_name(room["id"], 1)
    orphan = f"syncmill-room-{'dead0000-0000-0000-0000-000000000000'}-r1"
    fake = _FakeTmux(existing={live, orphan})
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        # A live (non-terminal) room's viewer survives; the orphan is reclaimed.
        assert V.sweep_orphan_viewers(coordinator.store) == 1
        assert live in fake.existing
        assert orphan not in fake.existing
        # Once the room is terminal, its viewer becomes reclaimable too.
        coordinator.store._conn.execute(
            "UPDATE rooms SET state='closed' WHERE id=?", (room["id"],)
        )
        assert V.sweep_orphan_viewers(coordinator.store) == 1
        assert live not in fake.existing
    finally:
        coordinator.close()
