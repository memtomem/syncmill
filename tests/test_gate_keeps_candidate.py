"""A candidate is what the agent produced, not what its gate wrote (#268).

These drive real Git, real worktrees and a real ``CommandGate``: the defect is in
the order of capture and gate, which a stubbed capture cannot show.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from syncmill.config import SyncmillConfig
from syncmill.models import Task, TaskStatus
from syncmill.rooms import RoomCoordinator, RoomError
from syncmill.strategies import _make_strategy
from syncmill.strategies.base import _collect_files_changed, _gate_accepts, _select_winner
from syncmill.supervisor import Supervisor
from syncmill.worktree import (
    PatchCaptureError,
    WorkspaceError,
    Worktree,
    capture_changes,
    rebuild_worktree,
    trusted_admin_dir,
)
from conftest import FakeAgent, FakeGate, FakeMemory, FakeRegistry


def _gate_writes(name: str = "gate-debris.txt") -> str:
    code = f"open({name!r}, 'w').write('from the gate')"
    return subprocess.list2cmdline([sys.executable, "-c", code])


class _EditingAgent(FakeAgent):
    """Writes ``agent.txt`` in its worktree, through both execution paths."""

    def execute(self, prompt: str, workdir: Path, timeout: int):
        (workdir / "agent.txt").write_text("from the agent\n", encoding="utf-8")
        return super().execute(prompt, workdir, timeout)


def _run_config(git_repo: Path, tmp_path: Path, strategy: str, gate: list[str]) -> SyncmillConfig:
    return SyncmillConfig(
        repo_root=str(git_repo),
        default_strategy=strategy,
        workspace={"root": str(tmp_path / "workspaces")},
        runs={"root": str(tmp_path / "runs")},
        agent_priority=["codex"],
        gate={"commands": gate},
    )


def _supervisor(config: SyncmillConfig, strategy: str) -> Supervisor:
    sup = Supervisor.__new__(Supervisor)
    sup._config = config
    sup._registry = FakeRegistry([_EditingAgent("codex", output="[]")])
    sup._memory = FakeMemory()
    sup._breaker = None
    sup._matcher = None
    sup._board = None
    sup._strategy = _make_strategy(strategy)
    sup.last_report_path = None
    return sup


@pytest.mark.parametrize("strategy", ["route", "compete", "pipeline", "council", "decompose"])
def test_run_winner_patch_excludes_what_the_gate_wrote(strategy, git_repo, tmp_path):
    config = _run_config(git_repo, tmp_path, strategy, [_gate_writes()])
    result = _supervisor(config, strategy).run(Task(prompt="go"))

    assert result.status is TaskStatus.COMPLETED
    assert result.gate is not None and result.gate.passed
    assert result.files_changed == ["agent.txt"]
    assert "gate-debris.txt" not in (result.patch or "")


def test_run_artifact_and_apply_exclude_what_the_gate_wrote(git_repo, tmp_path):
    from syncmill.artifacts import ArtifactStore

    config = _run_config(git_repo, tmp_path, "route", [_gate_writes()])
    result = _supervisor(config, "route").run(Task(prompt="go"))
    project_runs = next((tmp_path / "runs").iterdir())
    manifest = json.loads((project_runs / "runs" / result.run_id / "manifest.json").read_text())
    assert manifest["files_changed"] == ["agent.txt"]

    store = ArtifactStore(tmp_path / "runs", project_runs.name)
    store.apply(result.run_id, git_repo, source_fingerprint=manifest["source_fingerprint"])
    assert (git_repo / "agent.txt").exists()
    assert not (git_repo / "gate-debris.txt").exists()


def test_a_gated_run_keeps_the_agents_own_files_when_the_gate_edits_them(git_repo, tmp_path):
    # A gate that "fixes" a file validated the fixed bytes, but the candidate is
    # what the agent wrote. A gate validates; it does not contribute.
    code = "open('agent.txt', 'w').write('reformatted by the gate')"
    gate = [subprocess.list2cmdline([sys.executable, "-c", code])]
    result = _supervisor(_run_config(git_repo, tmp_path, "route", gate), "route").run(
        Task(prompt="go")
    )
    assert "+from the agent" in (result.patch or "")
    assert "reformatted by the gate" not in (result.patch or "")


def test_gate_rejection_leaves_no_capture_on_the_failed_result(git_repo):
    (git_repo / "agent.txt").write_text("x\n", encoding="utf-8")
    worktree = Worktree(git_repo, None, "codex", True)
    result = FakeAgent("codex").execute("go", git_repo, 1)

    assert _gate_accepts(result, worktree, FakeGate(passes=False)) is False

    assert result.status is TaskStatus.FAILED
    assert result.files_changed == []
    assert result.patch is None


def test_no_gate_does_not_capture_in_the_gate_seam(git_repo):
    (git_repo / "agent.txt").write_text("x\n", encoding="utf-8")
    worktree = Worktree(git_repo, None, "codex", True)
    result = FakeAgent("codex").execute("go", git_repo, 1)

    assert _gate_accepts(result, worktree, None) is True

    assert result.files_changed == []
    assert result.patch is None


def _uncaptureable(git_repo: Path) -> None:
    """``git add -A`` refuses an initialized repository with no commit."""
    (git_repo / "scratch").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=git_repo / "scratch", check=True)
    (git_repo / "scratch" / "file").write_text("x\n", encoding="utf-8")


def test_a_capture_failure_stops_the_run_only_for_the_winner(git_repo):
    _uncaptureable(git_repo)
    worktree = Worktree(git_repo, None, "codex", True)
    result = FakeAgent("codex").execute("go", git_repo, 1)

    assert _gate_accepts(result, worktree, FakeGate(passes=True)) is True

    with pytest.raises(PatchCaptureError):
        _collect_files_changed(result, worktree)


def test_a_rejected_candidate_keeps_no_capture_failure(git_repo):
    _uncaptureable(git_repo)
    worktree = Worktree(git_repo, None, "codex", True)
    result = FakeAgent("codex").execute("go", git_repo, 1)

    assert _gate_accepts(result, worktree, FakeGate(passes=False)) is False

    assert result.status is TaskStatus.FAILED
    assert "quality gate failed" in result.error
    assert result.capture_error is None


def test_judge_still_chooses_when_another_candidate_cannot_be_captured(git_repo, tmp_path):
    config = _run_config(git_repo, tmp_path, "compete", [])
    config.selection.mode = "judge"
    config.selection.judge_agent = "judge"
    from syncmill.worktree import WorktreePool

    pool = WorktreePool(git_repo, "judge-capture")
    worktrees = [pool.acquire("claude"), pool.acquire("codex")]
    (worktrees[0].path / "good.txt").write_text("good\n", encoding="utf-8")
    _uncaptureable(worktrees[1].path)
    results = [
        FakeAgent("claude").execute("go", worktrees[0].path, 1),
        FakeAgent("codex").execute("go", worktrees[1].path, 1),
    ]
    judge = FakeAgent("judge", output="WINNER: claude")
    try:
        winner = _select_winner(
            worktrees, results, FakeGate(), mode="judge", agents=[judge], pool=pool, config=config
        )
        assert winner is not None and winner[0].agent_id == "claude"
        _collect_files_changed(winner[0], winner[1])
        assert winner[0].files_changed == ["good.txt"]
    finally:
        pool.cleanup_all()


def test_selection_falls_through_a_rejected_candidate_it_could_not_capture(git_repo, tmp_path):
    _uncaptureable(git_repo)
    other = tmp_path / "other"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "other", str(other)],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    (other / "good.txt").write_text("good\n", encoding="utf-8")
    worktrees = [Worktree(git_repo, None, "claude", True), Worktree(other, "other", "codex", True)]
    results = [
        FakeAgent("claude").execute("go", git_repo, 1),
        FakeAgent("codex").execute("go", other, 1),
    ]

    winner = _select_winner(
        worktrees, results, FakeGate(by_agent={"claude": False, "codex": True})
    )

    assert winner is not None
    assert winner[0].agent_id == "codex"
    assert winner[0].files_changed == ["good.txt"]


def test_judge_sees_the_captured_patch_not_what_the_gate_wrote(git_repo, tmp_path):
    config = _run_config(git_repo, tmp_path, "compete", [])
    config.selection.mode = "judge"
    config.selection.judge_agent = "judge"

    class _WritingGate(FakeGate):
        def evaluate(self, worktree):
            (worktree.path / "candidate.txt").write_text("rewritten by the gate\n", encoding="utf-8")
            (worktree.path / "gate-debris.txt").write_text("debris\n", encoding="utf-8")
            return super().evaluate(worktree)

    from syncmill.worktree import WorktreePool

    pool = WorktreePool(git_repo, "judge-run")
    worktrees = [pool.acquire("claude"), pool.acquire("codex")]
    for worktree in worktrees:
        (worktree.path / "candidate.txt").write_text(
            f"from {worktree.agent_id}\n", encoding="utf-8"
        )
    results = [
        FakeAgent("claude").execute("go", worktrees[0].path, 1),
        FakeAgent("codex").execute("go", worktrees[1].path, 1),
    ]
    judge = FakeAgent("judge", output="WINNER: codex")
    try:
        winner = _select_winner(
            worktrees, results, _WritingGate(), mode="judge", agents=[judge], pool=pool, config=config
        )
    finally:
        pool.cleanup_all()

    assert winner is not None and winner[0].agent_id == "codex"
    prompt = judge.calls[0]["prompt"]
    assert "+from claude" in prompt and "+from codex" in prompt
    assert "rewritten by the gate" not in prompt
    assert "gate-debris.txt" not in prompt


# ── rooms: the worktree outlives the candidate ───────────────────────────────


class _RoundAgent(FakeAgent):
    """Writes one file per round, so each round's patch is predictable."""

    def __init__(self, agent_id: str):
        super().__init__(agent_id, output="done")
        self.rounds = 0

    async def aexecute(self, prompt: str, workdir: Path, timeout: int):
        self.rounds += 1
        (workdir / f"round{self.rounds}.txt").write_text("agent\n", encoding="utf-8")
        return self.execute(prompt, workdir, timeout)


def _room_config(tmp_path: Path, gate: list[str]) -> SyncmillConfig:
    return SyncmillConfig(
        project={"id": "gate-keeps"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True, "max_members": 3, "max_parallel": 3},
        gate={"commands": gate},
    )


def _files(coordinator: RoomCoordinator, room_id: str) -> list[list[str]]:
    rows = coordinator.store.list_candidates(room_id)
    rows.sort(key=lambda row: row["round_number"])
    return [
        coordinator.store.load_candidate_manifest(row["id"])["files_changed"] for row in rows
    ]


def test_room_second_round_patch_excludes_what_the_gate_wrote(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _room_config(tmp_path, [_gate_writes()]), git_repo, agents=[_RoundAgent("claude")]
    )
    try:
        room = coordinator.create("go", ["claude"])
        coordinator.run_round(room["id"])
        coordinator.run_round(room["id"], "again")
        assert _files(coordinator, room["id"]) == [
            ["round1.txt"],
            ["round1.txt", "round2.txt"],
        ]
        assert any(e["kind"] == "workspace.rebuilt" for e in coordinator.store.events(room["id"]))
    finally:
        coordinator.close()


def test_room_apply_leaves_no_gate_file_in_the_source(git_repo, tmp_path):
    config = _room_config(tmp_path, [_gate_writes()])
    coordinator = RoomCoordinator(config, git_repo, agents=[_RoundAgent("claude")])
    try:
        room = coordinator.create("go", ["claude"])
        coordinator.run_round(room["id"])
        coordinator.run_round(room["id"], "again")
        latest = [
            row
            for row in coordinator.store.list_candidates(room["id"])
            if row["round_number"] == 2
        ][0]
        coordinator.select(room["id"], latest["id"])
        coordinator.apply(room["id"])
    finally:
        coordinator.close()
    assert (git_repo / "round2.txt").exists()
    assert not (git_repo / "gate-debris.txt").exists()


def test_room_keeps_ignored_files_when_the_gate_left_the_tree_alone(git_repo, tmp_path):
    (git_repo / ".gitignore").write_text("deps/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "ignore deps"], cwd=git_repo, check=True, capture_output=True
    )
    code = "import os; os.makedirs('deps', exist_ok=True); open('deps/lib', 'w').write('dep')"
    gate = [subprocess.list2cmdline([sys.executable, "-c", code])]
    coordinator = RoomCoordinator(
        _room_config(tmp_path, gate), git_repo, agents=[_RoundAgent("claude")]
    )
    try:
        room = coordinator.create("go", ["claude"])
        coordinator.run_round(room["id"])
        workspace = coordinator.store.list_workspaces(room["id"])[0]
        assert (Path(workspace["path"]) / "deps" / "lib").exists()
        assert not any(
            e["kind"] == "workspace.rebuilt" for e in coordinator.store.events(room["id"])
        )
    finally:
        coordinator.close()


def test_room_rebuild_keeps_the_worktree_path_across_rounds(git_repo, tmp_path):
    # A provider session is bound to this path, so it must survive the rebuild.
    coordinator = RoomCoordinator(
        _room_config(tmp_path, [_gate_writes()]), git_repo, agents=[_RoundAgent("claude")]
    )
    try:
        room = coordinator.create("go", ["claude"])
        coordinator.run_round(room["id"])
        first = coordinator.store.list_workspaces(room["id"])[0]
        path = Path(first["path"])
        assert (path / "round1.txt").exists()
        assert not (path / "gate-debris.txt").exists()

        coordinator.run_round(room["id"], "again")
        second = coordinator.store.list_workspaces(room["id"])[0]
        assert second["path"] == first["path"]
        assert second["branch"] == first["branch"]
        assert (path / "round2.txt").exists()
    finally:
        coordinator.close()


def test_room_failed_rebuild_freezes_the_room_instead_of_reusing_the_tree(
    git_repo, tmp_path, monkeypatch
):
    import syncmill.rooms as rooms_module

    def refuse(*args, **kwargs):
        raise WorkspaceError("simulated rebuild failure")

    monkeypatch.setattr(rooms_module, "rebuild_worktree", refuse)
    coordinator = RoomCoordinator(
        _room_config(tmp_path, [_gate_writes()]), git_repo, agents=[_RoundAgent("claude")]
    )
    try:
        room = coordinator.create("go", ["claude"])
        with pytest.raises(WorkspaceError, match="simulated"):
            coordinator.run_round(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"
        with pytest.raises(RoomError):
            coordinator.run_round(room["id"], "again")
        # The candidate was saved before the rebuild, so the agent's work survives.
        assert _files(coordinator, room["id"]) == [["round1.txt"]]
    finally:
        coordinator.close()


def test_room_keeps_a_checkout_its_patch_cannot_rebuild(git_repo, tmp_path):
    # A commit inside an embedded repository is only a gitlink in the patch, so
    # the retained checkout holds the only copy of that work.
    class _EmbeddedRepoAgent(FakeAgent):
        async def aexecute(self, prompt: str, workdir: Path, timeout: int):
            nested = workdir / "component"
            nested.mkdir()
            (nested / "important.py").write_text("irreplaceable\n", encoding="utf-8")
            for args in (
                ["init", "-q"],
                ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "work"],
            ):
                subprocess.run(["git", *args], cwd=nested, check=True, capture_output=True)
            return self.execute(prompt, workdir, timeout)

    coordinator = RoomCoordinator(
        _room_config(tmp_path, [_gate_writes()]), git_repo, agents=[_EmbeddedRepoAgent("claude")]
    )
    try:
        room = coordinator.create("go", ["claude"])
        with pytest.raises(WorkspaceError):
            coordinator.run_round(room["id"])

        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"
        workspace = coordinator.store.list_workspaces(room["id"])[0]
        path = Path(workspace["path"])
        assert (path / "component" / "important.py").read_text() == "irreplaceable\n"
        assert (path / "component" / ".git").is_dir()
        assert not path.with_name(f"{path.name}-rebuild").exists()
    finally:
        coordinator.close()


# ── rebuild_worktree ─────────────────────────────────────────────────────────


def _linked_worktree(git_repo: Path, tmp_path: Path) -> tuple[Worktree, str]:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    path = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked-branch", str(path), base],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    return Worktree(
        path, "linked-branch", "member", True, base_commit=base,
        git_dir=trusted_admin_dir(git_repo, path),
    ), base


def test_rebuild_restores_the_patch_and_drops_everything_else(git_repo, tmp_path):
    worktree, base = _linked_worktree(git_repo, tmp_path)
    (worktree.path / "kept.txt").write_text("kept\n", encoding="utf-8")
    (worktree.path / "binary.dat").write_bytes(bytes(range(256)))
    patch = capture_changes(worktree.path, base, git_dir=worktree.git_dir).patch

    (worktree.path / "debris.txt").write_text("debris\n", encoding="utf-8")
    (worktree.path / "nested").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree.path / "nested", check=True)

    rebuild_worktree(git_repo, worktree, patch.encode("utf-8", "surrogateescape"))

    assert capture_changes(worktree.path, base, git_dir=worktree.git_dir).patch == patch
    assert not (worktree.path / "debris.txt").exists()
    assert not (worktree.path / "nested").exists()


def test_rebuild_handles_an_empty_patch(git_repo, tmp_path):
    worktree, base = _linked_worktree(git_repo, tmp_path)
    (worktree.path / "debris.txt").write_text("debris\n", encoding="utf-8")

    rebuild_worktree(git_repo, worktree, b"")

    assert capture_changes(worktree.path, base, git_dir=worktree.git_dir).patch == ""
    assert not (worktree.path / "debris.txt").exists()


def test_rebuild_leaves_the_checkout_and_no_scratch_when_the_patch_will_not_apply(
    git_repo, tmp_path
):
    worktree, base = _linked_worktree(git_repo, tmp_path)
    (worktree.path / "work.txt").write_text("work\n", encoding="utf-8")

    with pytest.raises(WorkspaceError):
        rebuild_worktree(git_repo, worktree, b"this is not a patch\n")

    assert (worktree.path / "work.txt").read_text() == "work\n"
    assert not worktree.path.with_name(f"{worktree.path.name}-rebuild").exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "linked-branch*"],
        cwd=git_repo, check=True, capture_output=True, text=True,
    ).stdout.split()
    assert "linked-branch-rebuild" not in branches
    assert "linked-branch" in branches


def test_rebuild_refuses_a_patch_that_does_not_reproduce_itself(git_repo, tmp_path):
    worktree, base = _linked_worktree(git_repo, tmp_path)
    (worktree.path / "work.txt").write_text("work\n", encoding="utf-8")
    patch = capture_changes(worktree.path, base, git_dir=worktree.git_dir).patch
    # Applies cleanly, but capture would not produce these bytes.
    altered = ("# a leading comment git apply ignores\n" + patch).encode()
    (worktree.path / "not-in-the-patch.txt").write_text("kept\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="does not reproduce"):
        rebuild_worktree(git_repo, worktree, altered)

    assert (worktree.path / "not-in-the-patch.txt").exists()
