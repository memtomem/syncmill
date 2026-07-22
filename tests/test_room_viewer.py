"""Hermetic room-viewer tests: never launch real tmux.

Every tmux control call funnels through ``room_viewer._tmux`` /
``room_viewer._tmux_attach`` / ``room_viewer.tmux_available``, patched here at
the ROOM_VIEWER lookup site (not ``monitor._tmux``) — room_viewer imports those
names, so the reference to rebind lives in room_viewer's namespace (the
strategies.base ``_git_changed_files`` lesson).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
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
    """Records calls and models session existence + the ownership marker.

    new-session adds a session (with the ``-mk`` temp name), rename-session moves
    it to the final name (carrying its owner option), set-option records the
    owner marker, show-options returns it, kill-session removes, has-session /
    list-sessions reflect. ``=`` forces exact matching like real resolution.
    ``existing`` entries are treated as pre-owned by ``owner_project``."""

    def __init__(self, *, existing=None, new_session_rc: int = 0, owner_project=None,
                 existing_panes: int = 1):
        self.calls: list[tuple] = []
        self.existing: set[str] = set(existing or [])
        self.new_session_rc = new_session_rc
        # session name -> owner-digest value (the @syncmill_room_viewer option)
        from syncmill.room_viewer import _project_digest

        self.owners: dict[str, str] = {}
        self.panes: dict[str, int] = {}
        if existing:
            for name in existing:
                self.panes[name] = existing_panes
                if owner_project is not None:
                    self.owners[name] = _project_digest(owner_project)

    def _match(self, target: str) -> str | None:
        exact = target[1:] if target.startswith("=") else target
        exact = exact.rstrip(":")
        return exact if exact in self.existing else None

    def __call__(self, *args):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "has-session":
            return _ns(0 if self._match(args[args.index("-t") + 1]) else 1)
        if cmd == "new-session":
            if self.new_session_rc != 0:
                return _ns(self.new_session_rc, stderr="boom")
            name = args[args.index("-s") + 1]
            self.existing.add(name)
            self.panes[name] = 1
            return _ns(0, stdout="%0")
        if cmd == "rename-session":
            src = self._match(args[args.index("-t") + 1])
            dst = args[-1]
            if src is None or dst in self.existing:
                return _ns(1, stderr="rename failed")
            self.existing.discard(src)
            self.existing.add(dst)
            if src in self.owners:
                self.owners[dst] = self.owners.pop(src)
            self.panes[dst] = self.panes.pop(src, 1)
            return _ns(0)
        if cmd == "set-option":
            target = self._match(args[args.index("-t") + 1])
            if target is not None and args[-2] == "@syncmill_room_viewer":
                self.owners[target] = args[-1]
            return _ns(0)
        if cmd == "show-options":
            target = self._match(args[args.index("-t") + 1])
            if target is not None and target in self.owners:
                return _ns(0, stdout=self.owners[target])
            return _ns(0, stdout="")
        if cmd == "list-panes":
            target = self._match(args[args.index("-t") + 1])
            n = self.panes.get(target, 0) if target else 0
            return _ns(0, stdout="\n".join(f"%{i}" for i in range(n)))
        if cmd == "list-sessions":
            return _ns(0, stdout="\n".join(sorted(self.existing)))
        if cmd == "kill-session":
            matched = self._match(args[args.index("-t") + 1])
            if matched:
                self.existing.discard(matched)
                self.owners.pop(matched, None)
                self.panes.pop(matched, None)
                return _ns(0)
            return _ns(1)
        if cmd == "split-window":
            target = self._match(args[args.index("-t") + 1])
            if target is not None:
                self.panes[target] = self.panes.get(target, 1) + 1
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
    assert name.startswith("syncmill-room-")
    assert name.endswith("-abc-123-r2")
    assert name == V.viewer_session_name(_PROJ, "abc-123", 2)
    # A different project never shares a name for the same room/round.
    assert V.viewer_session_name("proj-2", "abc-123", 2) != name


def test_project_key_is_injective_across_lossy_ids():
    # The blocker: distinct project ids that a character substitution would
    # collapse (team/a vs team:a) must map to DISTINCT names/keys.
    assert V.viewer_session_name("team/a", "r", 1) != V.viewer_session_name("team:a", "r", 1)
    # The name is always tmux-safe regardless of the raw id.
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
        # Built under a temp name then published with one rename to the final.
        assert fake.names().count("rename-session") == 1
        assert name in fake.existing
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
    # A concurrent creator publishes the final name first; our rename then fails
    # because the name exists. We drop our complete temp and accept the winner
    # (never a spurious duplicate error, never a partial session published).
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    name = V.viewer_session_name(coordinator.project_id, room["id"], 1)

    from syncmill.room_viewer import _project_digest

    class LosesRename(_FakeTmux):
        def __call__(self, *args):
            if args[0] == "rename-session":
                # the winner published a COMPLETE, OWNED final session first
                self.existing.add(name)
                self.owners[name] = _project_digest(coordinator.project_id)
                self.panes[name] = 1  # one member
            return super().__call__(*args)

    fake = LosesRename()
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        assert V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"]) == name
        # our temp was dropped; only the winner's final name remains
        assert not any(s.startswith(name + "-mk") for s in fake.existing)
    finally:
        coordinator.close()


def test_ensure_viewer_refuses_unowned_name_collision(monkeypatch, tmp_path, git_repo):
    # A session squats our exact name but lacks our ownership marker (a user
    # session, or a key collision): never attach it, never kill it — refuse.
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    name = V.viewer_session_name(coordinator.project_id, room["id"], 1)
    fake = _FakeTmux(existing={name})  # unmarked
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        with pytest.raises(V.RoomViewerError, match="not a syncmill viewer"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
        assert name in fake.existing  # untouched
    finally:
        coordinator.close()


def test_ensure_viewer_rebuilds_owned_incomplete_session(monkeypatch, tmp_path, git_repo):
    # An owned session with the wrong pane count (a pane exited, or a prior build
    # was killed mid-flight) is rebuilt, not attached broken.
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude", "codex"))
    name = V.viewer_session_name(coordinator.project_id, room["id"], 1)
    # owned but only 1 pane for a 2-member round → incomplete
    fake = _FakeTmux(existing={name}, owner_project=coordinator.project_id, existing_panes=1)
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        assert V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"]) == name
        # the stale session was killed and a fresh one built (2 panes now)
        assert "kill-session" in fake.names()
        assert fake.panes[name] == 2
    finally:
        coordinator.close()


def test_sweep_reclaims_abandoned_build_temp(monkeypatch, tmp_path, git_repo):
    # A crashed build (SIGKILL) leaves a `-mk` temp with live tail processes; the
    # sweep reclaims it once the final session exists, and also when its room is
    # gone even though the room is otherwise live.
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    proj = coordinator.project_id
    final = V.viewer_session_name(proj, room["id"], 1)
    orphan_temp = f"{final}-mkdeadbeef"
    fake = _FakeTmux(existing={final, orphan_temp}, owner_project=proj)
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        # room is live, but the temp is abandoned (its final exists) → reclaimed
        assert V.sweep_orphan_viewers(coordinator.store, proj) == 1
        assert orphan_temp not in fake.existing
        assert final in fake.existing  # the live final survives
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
        with pytest.raises(V.RoomViewerError, match="no valid log"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
    finally:
        coordinator.close()


def test_ensure_viewer_one_invalid_member_fails_whole_viewer(monkeypatch, tmp_path, git_repo):
    # Two members, one with a drifted log: a partial viewer that silently omits a
    # member reads as complete, so the whole create must fail.
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude", "codex"))
    round_id = coordinator.store.list_candidates(room["id"])[0]["round_id"]
    coordinator.store._conn.execute(
        "UPDATE round_members SET log_path='/etc/passwd' WHERE round_id=? AND member_id='codex'",
        (round_id,),
    )
    fake = _FakeTmux()
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        with pytest.raises(V.RoomViewerError, match="no valid log"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
        # No session was created at all — not even a one-pane partial.
        assert "new-session" not in fake.names()
    finally:
        coordinator.close()


def test_ensure_viewer_rejects_symlinked_log(monkeypatch, tmp_path, git_repo):
    # An in-root symlink that would rebind a member's log to another file is
    # rejected even though its target is inside candidates.root.
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    round_id = coordinator.store.list_candidates(room["id"])[0]["round_id"]
    member = coordinator.store.list_round_members(round_id)[0]
    log = coordinator.store.candidates.root / member["log_path"]
    log.parent.mkdir(parents=True, exist_ok=True)
    other = log.parent / "other.txt"
    other.write_text("elsewhere")
    log.unlink(missing_ok=True)
    log.symlink_to(other)  # in-root symlink indirection
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", _FakeTmux())
    try:
        with pytest.raises(V.RoomViewerError, match="no valid log"):
            V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
    finally:
        coordinator.close()


def test_ensure_viewer_refuses_terminal_room(monkeypatch, tmp_path, git_repo):
    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    coordinator.store._conn.execute(
        "UPDATE rooms SET state='closed' WHERE id=?", (room["id"],)
    )
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", _FakeTmux())
    try:
        with pytest.raises(V.RoomViewerError, match="room is closed"):
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
    fake = _FakeTmux(existing={mine, other_room}, owner_project=proj)
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        assert V.close_room_viewers(coordinator.store, proj, room["id"]) == 1
        assert mine not in fake.existing
        assert other_room in fake.existing  # a different room's viewer is untouched
    finally:
        coordinator.close()


def test_close_spares_a_session_without_our_ownership_marker(monkeypatch, tmp_path, git_repo):
    # A session that matches the name pattern but lacks our owner marker (a
    # coincidental user session, or a key-collision) is never reclaimed.
    coordinator, room = _room_with_round(tmp_path, git_repo)
    proj = coordinator.project_id
    impostor = V.viewer_session_name(proj, room["id"], 1)
    fake = _FakeTmux(existing={impostor})  # owner_project omitted → unmarked
    monkeypatch.setattr(V, "tmux_available", lambda: True)
    monkeypatch.setattr(V, "_tmux", fake)
    try:
        assert V.close_room_viewers(coordinator.store, proj, room["id"]) == 0
        assert impostor in fake.existing  # untouched: no ownership proof
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
    fake = _FakeTmux(existing={mine, foreign, user_session}, owner_project=proj)
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
    fake = _FakeTmux(existing={live, orphan}, owner_project=proj)
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
        # Poll rather than a fixed sleep: tail -F propagation is scheduler-timed.
        deadline = time.monotonic() + 5.0
        captured = ""
        while time.monotonic() < deadline:
            captured = "".join(
                _monitor._tmux("capture-pane", "-p", "-t", pane).stdout for pane in panes
            )
            if "SENTINEL_LINE" in captured:
                break
            time.sleep(0.1)
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


@pytestmark_tmux
def test_real_tmux_external_attach_detach_restores_control(
    isolated_tmux, tmp_path, git_repo
):
    # The external state machine end-to-end over a real PTY: attach_viewer blocks
    # in the foreground until the user detaches (Ctrl-b d), then returns control.
    import pty
    import select
    import time as _time

    coordinator, room = _room_with_round(tmp_path, git_repo, members=("claude",))
    try:
        name = V.ensure_viewer(coordinator.store, coordinator.project_id, room["id"])
        sockdir = os.environ["TMUX_TMPDIR"]
        child_src = (
            "import os, sys; os.environ['TMUX_TMPDIR']=%r; os.environ.pop('TMUX', None);"
            "from syncmill import room_viewer as V;"
            "sys.exit(V.attach_viewer(%r))" % (sockdir, name)
        )
        pid, fd = pty.fork()
        if pid == 0:  # child: run the blocking external attach on a real PTY
            os.execv(sys.executable, [sys.executable, "-c", child_src])
        reaped = False
        try:
            # Wait for the client to attach, then detach it DETERMINISTICALLY from
            # the server side — no dependency on the user's prefix binding.
            got = ""
            attached_deadline = _time.monotonic() + 8.0
            while _time.monotonic() < attached_deadline:
                got = _monitor._tmux(
                    "list-clients", "-t", V._exact(name), "-F", "#{client_name}"
                ).stdout.strip()
                if got:
                    break
                _time.sleep(0.1)
            assert got, "the external attach never produced a tmux client"
            _monitor._tmux("detach-client", "-s", V._exact(name))
            # The child (foreground attach) must now return 0 ON ITS OWN.
            exit_deadline = _time.monotonic() + 8.0
            code = None
            while _time.monotonic() < exit_deadline:
                with contextlib.suppress(OSError):
                    if select.select([fd], [], [], 0.05)[0]:
                        os.read(fd, 4096)  # drain so the child never blocks on the pty
                done, status = os.waitpid(pid, os.WNOHANG)
                if done == pid:  # actually reaped, not the (0,0) still-running signal
                    reaped = True
                    code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                    break
                _time.sleep(0.1)
            assert code == 0, f"external attach did not return cleanly on detach (code={code})"
            # The session survives the detach (it is still running).
            assert V._session_exists(name)
        finally:
            if not reaped:  # never leak the child if the test bailed early
                with contextlib.suppress(Exception):
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
            with contextlib.suppress(Exception):
                os.close(fd)
    finally:
        coordinator.close()
