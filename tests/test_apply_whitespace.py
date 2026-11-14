"""Every patch syncmill applies lands byte for byte, whatever ``apply.whitespace`` says.

``git apply`` reads the user's ``apply.whitespace``: ``fix`` rewrites the
whitespace on the lines it adds (or, in reverse, on the lines it restores) and
still exits 0, and ``error`` refuses the patch. Each test below sets one of them
in the isolated ``~/.gitconfig`` — the file the source repo, the verify clone
and the control clone all read — and checks the bytes that land.
"""

from __future__ import annotations

import ast
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from syncmill import artifacts as artifacts_module
from syncmill.artifacts import ArtifactError, ArtifactStore
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.worktree import (
    Worktree,
    capture_changes,
    git_apply_argv,
    rebuild_worktree,
    repository_fingerprint,
    trusted_admin_dir,
)

TAB = chr(9)

# Three whitespace errors git knows by name: trailing space, space before a tab
# in the indent, and a new blank line at EOF.
WS_CONTENT = "kept\n" + "trailing " + "\n" + " " + TAB + "indent\n" + "\n"

ADD_PATCH = (
    "diff --git a/ws.txt b/ws.txt\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/ws.txt\n"
    "@@ -0,0 +1,4 @@\n"
    + "".join("+" + line + "\n" for line in WS_CONTENT.split("\n")[:-1])
)

# Removes a tracked line that carries trailing whitespace, so reversing it
# (a rollback) restores a whitespace error.
TRACKED_BEFORE = "one\n" + "two " + "\n" + "three\n"
REMOVE_PATCH = (
    "diff --git a/tracked.txt b/tracked.txt\n"
    "--- a/tracked.txt\n"
    "+++ b/tracked.txt\n"
    "@@ -1,3 +1,2 @@\n"
    " one\n"
    "-two \n"
    " three\n"
)

MODES = pytest.mark.parametrize("mode", ["fix", "error"])


@pytest.fixture(autouse=True)
def only_the_test_gitconfig(monkeypatch, isolated_home):
    # The autouse HOME swap hides ~/.gitconfig, but Git also reads
    # $XDG_CONFIG_HOME/git/config and the system file; a developer's
    # core.autocrlf there would change the bytes these tests compare.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _set_user_apply_whitespace(home: Path, mode: str) -> None:
    (home / ".gitconfig").write_text(
        f"[apply]\n{TAB}whitespace = {mode}\n", encoding="utf-8"
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text(TRACKED_BEFORE, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def _pending(tmp_path: Path, repo: Path, patch: str) -> ArtifactStore:
    store = ArtifactStore(tmp_path / "artifacts", "project-1")
    task = Task(prompt="whitespace")
    fingerprint = repository_fingerprint(repo)
    head = _git(repo, "rev-parse", "HEAD")
    store.begin(
        run_id="run-1", task=task, repo_root=repo, snapshot_commit=head,
        source_fingerprint=fingerprint, strategy="route", agents=["codex"], plan={},
    )
    store.save(
        run_id="run-1",
        task=task,
        result=TaskResult(
            task_id=task.id, agent_id="codex", status=TaskStatus.COMPLETED,
            output="done", exit_code=0, duration_seconds=0.1,
            files_changed=["ws.txt"], patch=patch,
        ),
        repo_root=repo, snapshot_commit=head, source_fingerprint=fingerprint,
        strategy="route", plan={},
    )
    return store


def test_the_apply_argv_pins_whitespace_handling():
    assert git_apply_argv() == ["git", "apply", "--binary", "--whitespace=nowarn", "-"]
    assert git_apply_argv("--reverse", "--check") == [
        "git", "apply", "--reverse", "--check", "--binary", "--whitespace=nowarn", "-",
    ]


@MODES
def test_apply_lands_the_reviewed_bytes(tmp_path, isolated_home, mode):
    repo = _repo(tmp_path / "repo")
    store = _pending(tmp_path, repo, ADD_PATCH)
    _set_user_apply_whitespace(isolated_home, mode)

    store.apply("run-1", repo, repository_fingerprint(repo))

    assert (repo / "ws.txt").read_bytes() == WS_CONTENT.encode()
    assert store.load("run-1").manifest["state"] == "applied"


@MODES
def test_rollback_restores_the_source_bytes(monkeypatch, tmp_path, isolated_home, mode):
    repo = _repo(tmp_path / "repo")
    store = _pending(tmp_path, repo, REMOVE_PATCH)
    _set_user_apply_whitespace(isolated_home, mode)
    real_write = artifacts_module.ArtifactIO.write

    def fail_receipt(self, path: Path, data: bytes) -> None:
        if path.parent.name == "applications":
            raise OSError("injected receipt failure")
        real_write(self, path, data)

    monkeypatch.setattr(artifacts_module.ArtifactIO, "write", fail_receipt)
    # A rollback that does not restore the source exactly raises
    # ArtifactRecoveryRequired ("manual recovery is required") instead.
    with pytest.raises(ArtifactError, match="source patch was rolled back"):
        store.apply("run-1", repo, repository_fingerprint(repo))

    assert (repo / "tracked.txt").read_bytes() == TRACKED_BEFORE.encode()
    assert store.load("run-1").manifest["state"] == "pending"


@MODES
def test_verify_gates_the_bytes_that_will_be_applied(tmp_path, isolated_home, mode):
    repo = _repo(tmp_path / "repo")
    store = _pending(tmp_path, repo, ADD_PATCH)
    _set_user_apply_whitespace(isolated_home, mode)
    check = tmp_path / "check_ws.py"
    check.write_text(
        "import sys\n"
        f"sys.exit(0 if open('ws.txt', 'rb').read() == {WS_CONTENT.encode()!r} else 1)\n",
        encoding="utf-8",
    )

    store.apply(
        "run-1", repo, repository_fingerprint(repo),
        verify_commands=[shlex.join([sys.executable, str(check)])],
        verify_workspace_root=tmp_path / "workspaces",
    )

    assert (repo / "ws.txt").read_bytes() == WS_CONTENT.encode()


@MODES
def test_reverse_check_reads_an_applied_patch_as_reverse_applicable(
    tmp_path, isolated_home, mode
):
    repo = _repo(tmp_path / "repo")
    subprocess.run(
        ["git", "apply", "-"], cwd=repo, input=REMOVE_PATCH.encode(), check=True
    )
    _set_user_apply_whitespace(isolated_home, mode)

    verdict = ArtifactStore._reverse_check(repo, REMOVE_PATCH.encode())

    assert verdict == "patch is reverse-applicable"


@MODES
def test_rebuilt_checkout_holds_the_patch_bytes(git_repo, tmp_path, isolated_home, mode):
    base = _git(git_repo, "rev-parse", "HEAD")
    path = tmp_path / "linked"
    _git(git_repo, "worktree", "add", "-q", "-b", "linked", str(path), base)
    worktree = Worktree(
        path, "linked", "member", True, base_commit=base,
        git_dir=trusted_admin_dir(git_repo, path),
    )
    (path / "ws.txt").write_bytes(WS_CONTENT.encode())
    patch = capture_changes(path, base, git_dir=worktree.git_dir).patch
    _set_user_apply_whitespace(isolated_home, mode)

    rebuild_worktree(git_repo, worktree, patch.encode("utf-8", "surrogateescape"))

    assert (worktree.path / "ws.txt").read_bytes() == WS_CONTENT.encode()


def test_every_git_apply_in_the_package_goes_through_the_helper():
    # A literal ["git", "apply", ...] would bypass the pinned --whitespace.
    package = Path(artifacts_module.__file__).parent
    offenders = []
    for source in sorted(package.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        helper_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "git_apply_argv":
                helper_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
                continue
            head = node.elts[:2]
            if (
                all(isinstance(e, ast.Constant) for e in head)
                and [e.value for e in head] == ["git", "apply"]
                and node.lineno not in helper_lines
            ):
                offenders.append(f"{source.relative_to(package)}:{node.lineno}")
    assert offenders == []
