from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from click.testing import CliRunner

from syncmill.agents import KimiCodeAgent, provider_session_scope
from syncmill.cli import cli
from syncmill.worktree import WorktreePool, capture_repository_snapshot


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("base\n")
    (path / ".gitignore").write_text("ignored.txt\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def test_snapshot_captures_dirty_and_untracked_without_touching_source(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("dirty\n")
    (repo / "new.txt").write_text("untracked\n")
    (repo / "ignored.txt").write_text("secret\n")
    before_status = _git(repo, "status", "--porcelain")
    before_index = _git(repo, "write-tree")

    snapshot = capture_repository_snapshot(repo, "run-1", tmp_path / "workspaces")
    pool = WorktreePool(repo, "task-1", snapshot=snapshot)
    try:
        worktree = pool.acquire("codex")
        assert (worktree.path / "tracked.txt").read_text() == "dirty\n"
        assert (worktree.path / "new.txt").read_text() == "untracked\n"
        assert not (worktree.path / "ignored.txt").exists()
        assert _git(repo, "status", "--porcelain") == before_status
        assert _git(repo, "write-tree") == before_index
    finally:
        pool.cleanup_all()
    assert not snapshot.run_root.exists()


def test_run_continue_and_explicit_apply_end_to_end(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "resumed = len(sys.argv) > 2 and sys.argv[1:3] == ['exec', 'resume']\n"
        "pathlib.Path('winner.txt').write_text('second\\n' if resumed else 'first\\n')\n"
        "sid = sys.argv[3] if resumed else '11111111-1111-4111-8111-111111111111'\n"
        "print(json.dumps({'type':'thread.started','thread_id':sid}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))\n"
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SYNCMILL_AGENT_PRIORITY": '["codex"]',
        "SYNCMILL_WORKSPACE__ROOT": str(tmp_path / "workspaces"),
        "SYNCMILL_RUNS__ROOT": str(tmp_path / "runs"),
        "SYNCMILL_MEMORY__DB_PATH": str(tmp_path / "memory" / "memory.db"),
        "SYNCMILL_MEMORY__MEMORY_DIR": str(tmp_path / "memory"),
    }
    runner = CliRunner()
    first = runner.invoke(
        cli, ["run", "--repo", str(repo), "--agent", "codex", "make it"], env=env
    )
    assert first.exit_code == 0, first.output
    assert not (repo / "winner.txt").exists()
    first_run = next(
        line.split(": ", 1)[1]
        for line in first.output.splitlines()
        if line.startswith("[syncmill] run:")
    )

    continued = runner.invoke(
        cli,
        ["continue", first_run, "improve it", "--repo", str(repo)],
        env=env,
    )
    assert continued.exit_code == 0, continued.output
    second_run = next(
        line.split(": ", 1)[1]
        for line in continued.output.splitlines()
        if line.startswith("[syncmill] run:")
    )
    project_runs = next((tmp_path / "runs").glob("*/runs"))
    first_manifest = json.loads(
        (project_runs / first_run / "manifest.json").read_text()
    )
    second_manifest = json.loads(
        (project_runs / second_run / "manifest.json").read_text()
    )
    assert first_manifest["state"] == "superseded"
    assert second_manifest["parent_run_id"] == first_run
    assert second_manifest["provider_session_id"] == first_manifest["provider_session_id"]
    assert not (repo / "winner.txt").exists()

    applied = runner.invoke(
        cli, ["apply", second_run, "--repo", str(repo)], env=env
    )
    assert applied.exit_code == 0, applied.output
    assert (repo / "winner.txt").read_text() == "second\n"
    final_manifest = json.loads(
        (project_runs / second_run / "manifest.json").read_text()
    )
    assert final_manifest["state"] == "applied"
    assert list((project_runs / second_run / "applications").glob("*.json"))


def test_kimi_resume_hint_is_used_on_next_turn(tmp_path: Path, monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"role":"assistant","content":"done"}\n'
                '{"role":"meta","type":"session.resume_hint",'
                '"content":"To resume: kimi -r kimi-session-1"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("syncmill.agents.subprocess.run", fake_run)
    agent = KimiCodeAgent()
    with provider_session_scope():
        first = agent.execute("first", tmp_path, 10)
        second = agent.execute("second", tmp_path, 10)
    assert first.provider_session_id == "kimi-session-1"
    assert second.provider_session_id == "kimi-session-1"
    assert "--session" not in commands[0]
    assert commands[1][-2:] == ["--session", "kimi-session-1"]
