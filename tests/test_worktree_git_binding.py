"""Git run on an agent's worktree never reads the worktree's own `.git` (#269).

A linked worktree names its admin directory in its `.git` file, and an agent
confined to the worktree can rewrite that file to point at a directory it
planted, whose config names programs Git runs: ``core.fsmonitor``, a clean
filter, ``diff.external``. Every helper that runs Git on a worktree after an
agent's turn binds Git to the admin directory found from the control side.

Each attack test also runs the same plant with ordinary discovery and asserts
the marker fires, so a passing test proves the binding, not a harmless plant.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from syncmill.git_env import isolated_git_env
from syncmill.isolated_session import _strict_checkpoint_patch
from syncmill.rooms import RoomCoordinator, RoomError
from syncmill.strategies.base import _git_diff_excerpt
import syncmill.worktree as worktree_module
from syncmill.worktree import (
    WorkspaceError,
    Worktree,
    _checkout_from_patch,
    capture_changes,
    trusted_admin_dir,
)
from test_rooms import WritingAgent, _config


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _control_with_worktree(tmp_path: Path, git_repo: Path, name: str = "member-1"):
    """A control clone and one linked worktree, the snapshot layout."""
    control = tmp_path / "run" / "control"
    control.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--no-checkout", str(git_repo), str(control))
    base = _git(control, "rev-parse", "HEAD")
    worktree = Worktree(
        tmp_path / "run" / "worktrees" / name, f"syncmill/{name}", name, True,
        base_commit=base,
    )
    _checkout_from_patch(control, worktree, None)
    return control, worktree, base


def _marker_script(directory: Path, name: str, marker: Path, *, passthrough: bool) -> Path:
    script = directory / f"{name}.sh"
    body = "cat" if passthrough else "exit 0"
    script.write_text(f'#!/bin/sh\necho {name} >> "{marker}"\n{body}\n')
    script.chmod(0o755)
    return script


def _plant(worktree: Path, scripts: Path, marker: Path, *, external: Path | None = None) -> None:
    """What a confined agent can do: rewrite its `.git` to a gitdir it planted
    inside the worktree, whose common dir carries config that runs programs.
    ``external`` points the planted commondir at another repository instead."""
    real = Path(
        (worktree / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
    )
    if not real.is_absolute():
        real = (worktree / real).resolve()
    real_common = (real / (real / "commondir").read_text().strip()).resolve()
    evil = worktree / ".evil"
    shutil.copytree(real, evil)
    if external is None:
        common = worktree / ".evilcommon"
        shutil.copytree(real_common, common, ignore=shutil.ignore_patterns("worktrees"))
    else:
        common = external
    (evil / "commondir").write_text(f"{common}\n", encoding="utf-8")
    fsmonitor = _marker_script(scripts, "fsmonitor", marker, passthrough=False)
    clean = _marker_script(scripts, "filter", marker, passthrough=True)
    external_diff = _marker_script(scripts, "extdiff", marker, passthrough=False)
    with open(common / "config", "a", encoding="utf-8") as config:
        config.write(
            f"[core]\n\tfsmonitor = {fsmonitor}\n"
            f'[filter "evil"]\n\tclean = {clean}\n'
            f"[diff]\n\texternal = {external_diff}\n"
        )
    (worktree / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {evil}\n", encoding="utf-8")
    (worktree / "work.txt").write_text("agent work\n", encoding="utf-8")


def _fired(marker: Path) -> list[str]:
    return marker.read_text().splitlines() if marker.exists() else []


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    directory = tmp_path / "scripts"
    directory.mkdir()
    return directory


# ── trusted_admin_dir ───────────────────────────────────────────────────


def test_admin_dir_is_the_control_side_entry_for_the_worktree(tmp_path, git_repo):
    control, worktree, _ = _control_with_worktree(tmp_path, git_repo)
    found = trusted_admin_dir(control, worktree.path)
    assert found.parent == Path(os.path.realpath(control / ".git" / "worktrees"))
    assert (found / "gitdir").read_text().strip() == str(
        Path(os.path.realpath(worktree.path)) / ".git"
    )
    assert worktree.git_dir == found


def test_admin_dir_ignores_what_the_worktree_marker_says(tmp_path, git_repo):
    control, first, _ = _control_with_worktree(tmp_path, git_repo, "member-1")
    second = Worktree(
        tmp_path / "run" / "worktrees" / "member-2", "syncmill/member-2", "member-2", True,
        base_commit=first.base_commit,
    )
    _checkout_from_patch(control, second, None)
    expected = trusted_admin_dir(control, first.path)
    # The agent in member-1 aliases its marker to member-2's.
    (first.path / ".git").unlink()
    (first.path / ".git").symlink_to(second.path / ".git")
    assert trusted_admin_dir(control, first.path) == expected
    (first.path / ".git").unlink()
    (first.path / ".git").write_text(f"gitdir: {second.git_dir}\n")
    assert trusted_admin_dir(control, first.path) == expected


def test_admin_dir_is_unchanged_when_the_marker_is_replaced_mid_scan(
    tmp_path, git_repo, monkeypatch
):
    """The agent may swap its `.git` while the control side is being read; the
    scan never consults it, so the swap cannot change the answer."""
    control, first, _ = _control_with_worktree(tmp_path, git_repo, "member-1")
    second = Worktree(
        tmp_path / "run" / "worktrees" / "member-2", "syncmill/member-2", "member-2", True,
        base_commit=first.base_commit,
    )
    _checkout_from_patch(control, second, None)
    expected = first.git_dir
    real_scandir = os.scandir
    swapped = []

    def scandir_then_swap(path):
        entries = list(real_scandir(path))
        (first.path / ".git").unlink()
        (first.path / ".git").symlink_to(second.path / ".git")
        swapped.append(True)
        return iter(entries)

    monkeypatch.setattr(worktree_module.os, "scandir", scandir_then_swap)
    assert trusted_admin_dir(control, first.path) == expected
    assert swapped == [True]


def test_admin_dir_accepts_a_relative_backlink(tmp_path, git_repo):
    control, worktree, _ = _control_with_worktree(tmp_path, git_repo)
    admin = trusted_admin_dir(control, worktree.path)
    absolute = (admin / "gitdir").read_text().strip()
    (admin / "gitdir").write_text(os.path.relpath(absolute, admin) + "\n")
    assert trusted_admin_dir(control, worktree.path) == admin


def test_admin_dir_refuses_no_match_and_two_matches(tmp_path, git_repo):
    control, worktree, _ = _control_with_worktree(tmp_path, git_repo)
    with pytest.raises(WorkspaceError, match="0 admin directories"):
        trusted_admin_dir(control, worktree.path.with_name("elsewhere"))
    admin = trusted_admin_dir(control, worktree.path)
    shutil.copytree(admin, admin.with_name(admin.name + "-copy"))
    with pytest.raises(WorkspaceError, match="2 admin directories"):
        trusted_admin_dir(control, worktree.path)


def test_admin_dir_skips_a_symlinked_entry(tmp_path, git_repo):
    control, worktree, _ = _control_with_worktree(tmp_path, git_repo)
    admin = trusted_admin_dir(control, worktree.path)
    (admin.parent / "alias").symlink_to(admin)
    assert trusted_admin_dir(control, worktree.path) == admin


# ── capture and excerpt ─────────────────────────────────────────────────


@pytest.mark.parametrize("external_common", [False, True])
def test_capture_never_runs_programs_from_a_planted_gitdir(
    tmp_path, git_repo, scripts, external_common
):
    control, worktree, base = _control_with_worktree(tmp_path, git_repo)
    external = None
    if external_common:
        external = tmp_path / "external"
        _git(tmp_path, "init", "-q", str(external))
        external = external / ".git"
    marker = tmp_path / "marker"
    _plant(worktree.path, scripts, marker, external=external)

    changes = capture_changes(worktree.path, base, git_dir=worktree.git_dir)

    assert _fired(marker) == []
    assert "work.txt" in changes.files
    assert "+agent work" in changes.patch
    # Bite: ordinary discovery follows the planted `.git` and runs the programs.
    # (With the external common dir, capture's first step already fails there,
    # so the plant is shown live through `git status`.)
    with contextlib.suppress(WorkspaceError):
        capture_changes(worktree.path, base, git_dir=None)
    subprocess.run(
        ["git", "status"], cwd=worktree.path, env=isolated_git_env(),
        capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert "fsmonitor" in _fired(marker)


def test_excerpt_never_runs_programs_from_a_planted_gitdir(tmp_path, git_repo, scripts):
    control, worktree, base = _control_with_worktree(tmp_path, git_repo)
    marker = tmp_path / "marker"
    _plant(worktree.path, scripts, marker)

    excerpt = _git_diff_excerpt(worktree.path, base, git_dir=worktree.git_dir)

    assert _fired(marker) == []
    assert excerpt is not None and "work.txt" in excerpt
    _git_diff_excerpt(worktree.path, base, git_dir=None)
    assert {"fsmonitor", "filter", "extdiff"} <= set(_fired(marker))


def test_isolated_checkpoint_never_runs_programs_from_a_planted_gitdir(
    tmp_path, git_repo, scripts
):
    control, worktree, base = _control_with_worktree(tmp_path, git_repo)
    marker = tmp_path / "marker"
    _plant(worktree.path, scripts, marker)

    files, patch = _strict_checkpoint_patch(worktree.path, base, control)

    assert _fired(marker) == []
    assert "work.txt" in files and "+agent work" in patch


def test_a_global_filter_still_runs_through_a_bound_capture(
    tmp_path, git_repo, isolated_home
):
    """The user's own global config stays trusted: a clean filter it defines
    (the LFS shape) is applied when the worktree's attributes ask for it."""
    (isolated_home / ".gitconfig").write_text(
        '[filter "upper"]\n\tclean = tr a-z A-Z\n\tsmudge = cat\n', encoding="utf-8"
    )
    control, worktree, base = _control_with_worktree(tmp_path, git_repo)
    (worktree.path / ".gitattributes").write_text("*.txt filter=upper\n")
    (worktree.path / "note.txt").write_text("shout\n")

    patch = capture_changes(worktree.path, base, git_dir=worktree.git_dir).patch

    assert "+SHOUT" in patch


def test_a_bound_capture_is_byte_identical_to_ordinary_discovery(tmp_path, git_repo):
    control, worktree, base = _control_with_worktree(tmp_path, git_repo)
    (worktree.path / "README.md").write_text("# changed\n")
    (worktree.path / "new.bin").write_bytes(bytes(range(256)))
    script = worktree.path / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    bound = capture_changes(worktree.path, base, git_dir=worktree.git_dir)
    discovered = capture_changes(worktree.path, base, git_dir=None)
    assert bound == discovered
    assert bound.patch


# ── rooms ───────────────────────────────────────────────────────────────


class PlantingAgent(WritingAgent):
    """Writes its work, then plants a hostile gitdir in round ``plant_round``."""

    def __init__(self, agent_id: str, scripts: Path, marker: Path, plant_round: int):
        super().__init__(agent_id)
        self.scripts, self.marker, self.plant_round = scripts, marker, plant_round
        self.turns = 0

    async def aexecute(self, prompt, workdir, timeout):
        self.turns += 1
        result = await super().aexecute(prompt, workdir, timeout)
        if self.turns == self.plant_round:
            _plant(Path(workdir), self.scripts, self.marker)
        return result


@pytest.mark.parametrize("gated", [False, True], ids=["no-gate", "gate"])
def test_a_room_capture_ignores_a_gitdir_planted_during_the_turn(
    tmp_path, git_repo, scripts, gated
):
    """With a gate, the member's tree is captured again after the gate ran
    (``_keep_candidate_tree``); that recapture must be bound too."""
    marker = tmp_path / "marker"
    agent = PlantingAgent("claude", scripts, marker, plant_round=1)
    config = _config(tmp_path)
    if gated:
        config.gate.commands = ["true"]
    coordinator = RoomCoordinator(config, git_repo, agents=[agent])
    try:
        room = coordinator.create("plant", ["claude"])
        try:
            coordinator.run_round(room["id"])
        finally:
            # Checked even if the round raises: firing is the failure that matters.
            assert _fired(marker) == []
        [candidate] = coordinator.store.list_candidates(room["id"])
        assert candidate["eligible"] == 1
        if gated:
            manifest = coordinator.store.candidates.load(candidate["artifact_path"])
            assert manifest["gate"]["passed"] is True
        assert _fired(marker) == []
        # The next round finds the tampered marker and fails closed.
        with pytest.raises(RoomError, match="Git binding is invalid"):
            coordinator.run_round(room["id"])
        assert _fired(marker) == []
    finally:
        coordinator.close()


def test_a_retained_room_accepts_git_relative_worktree_markers(
    tmp_path, git_repo, isolated_home
):
    (isolated_home / ".gitconfig").write_text(
        "[worktree]\n\tuseRelativePaths = true\n", encoding="utf-8"
    )
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("relative", ["claude"])
        coordinator.run_round(room["id"])
        [workspace] = coordinator.store.list_workspaces(room["id"])
        marker = (Path(workspace["path"]) / ".git").read_text().strip()
        if Path(marker.removeprefix("gitdir: ")).is_absolute():
            pytest.skip("this git does not write relative worktree markers")
        second = coordinator.run_round(room["id"], "again")
        assert second["state"] == "awaiting_decision"
    finally:
        coordinator.close()
