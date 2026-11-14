"""User-repository git calls in project.py and cli.py are bounded.

They run through ``worktree._run_git``, which kills git's whole process group
on timeout and reports rc 124, so a wedged git (a hung filter, fsmonitor or
credential helper) cannot hang ``init``, ``doctor``, ``start`` or a project
lookup forever.
"""

from __future__ import annotations

import ast
import os
import stat
import time
from pathlib import Path

import click
import pytest

import syncmill.worktree as worktree
from syncmill import cli, project
from syncmill.project import ProjectError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")

_SRC = Path(__file__).resolve().parents[1] / "src" / "syncmill"


def _git_argv_calls(path: Path) -> list[int]:
    """Lines where ``subprocess.<anything>`` is called with a git argv."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assigned: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assigned[target.id] = node.value
    hits = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.args
        ):
            continue
        argv = node.args[0]
        if isinstance(argv, ast.Name):
            argv = assigned.get(argv.id, argv)
        if (
            isinstance(argv, ast.List)
            and argv.elts
            and isinstance(argv.elts[0], ast.Constant)
            and argv.elts[0].value == "git"
        ):
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize("module", ["project.py", "cli.py"])
def test_no_unbounded_git_subprocess(module):
    assert _git_argv_calls(_SRC / module) == []


def test_the_guard_sees_a_git_subprocess(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import subprocess\n"
        "subprocess.run(['git', 'status'])\n"
        "argv = ['git', 'ls-files']\n"
        "subprocess.run(argv)\n"
        "subprocess.run(['codex', 'login'])\n"
    )
    assert _git_argv_calls(sample) == [2, 4]


@pytest.fixture
def wedged_git(tmp_path, monkeypatch):
    """A ``git`` on PATH that starts a child and both hang; the child records
    its pid so the test can prove the whole group died."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "child.pid"
    script = bin_dir / "git"
    script.write_text(
        "#!/bin/sh\n"
        f"sh -c 'echo $$ > \"{pid_file}\"; exec sleep 300' &\n"
        "exec sleep 300\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(worktree, "_GIT_TIMEOUT_SECONDS", 1.0)
    return pid_file


def _assert_child_died(pid_file: Path) -> None:
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pid_file.read_text().strip())
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"git's child {pid} outlived the timeout")


def test_git_root_reports_a_timeout_not_a_missing_repository(tmp_path, wedged_git):
    start = time.monotonic()
    with pytest.raises(ProjectError, match="git timed out inspecting"):
        project.git_root(tmp_path)
    assert time.monotonic() - start < 30
    _assert_child_died(wedged_git)


def test_dirty_count_fails_on_a_timeout(tmp_path, wedged_git):
    with pytest.raises(click.ClickException, match="timed out"):
        cli._dirty_count(tmp_path)
    _assert_child_died(wedged_git)


def test_status_dirty_fails_on_a_timeout(tmp_path, wedged_git):
    with pytest.raises(Exception, match="git status failed"):
        cli.subprocess_status_dirty(tmp_path)
    _assert_child_died(wedged_git)


def test_project_id_falls_back_when_git_times_out(git_repo, wedged_git):
    """The marker question goes unanswered, so the marker is distrusted and
    the path hash is used: the existing fallback, reached in bounded time."""
    import hashlib
    import json

    marker = git_repo / ".syncmill" / "project.json"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text(json.dumps({"id": "local-machine-id"}) + "\n")
    with pytest.MonkeyPatch.context() as healthy:
        healthy.setattr(worktree, "_GIT_TIMEOUT_SECONDS", 600.0)
        healthy.setenv("PATH", os.environ["PATH"].split(os.pathsep, 1)[1])
        assert project.project_id(git_repo) == "local-machine-id"  # trusted when git answers
    start = time.monotonic()
    path_hash = hashlib.sha256(str(git_repo.resolve()).encode()).hexdigest()[:24]
    assert project.project_id(git_repo) == path_hash
    assert time.monotonic() - start < 30
