from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from syncmill.cli import cli


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "tmp-test-guide.ko.md"


def _fake_provider(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "auth status"|"login status") exit 0 ;;\n'
        "esac\n"
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_tmp_guide_links_and_safety_contract():
    text = GUIDE.read_text(encoding="utf-8")

    assert "`/tmp`에서 Syncmill 안전하게 시험하기" in text
    assert "mktemp -d /tmp/syncmill-guide.XXXXXX" in text
    for variable in (
        "SYNCMILL_REPO_ROOT",
        "SYNCMILL_WORKSPACE__ROOT",
        "SYNCMILL_RUNS__ROOT",
        "SYNCMILL_TUI__STATE_DIR",
        "SYNCMILL_MEMORY__DB_PATH",
        "SYNCMILL_MEMORY__MEMORY_DIR",
        "UV_CACHE_DIR",
    ):
        assert variable in text
    for agent in ("--agent claude", "--agent codex", "--agent kimi-code"):
        assert agent in text
    assert "MEMTOMEM_FASTEMBED_CACHE" in text
    assert "Room 생성 자체는 호출이" in text
    assert 'Refusing to remove unexpected path' in text

    for relative in (
        "README.md",
        "docs/user-guide.ko.md",
        "docs/tui-guide.ko.md",
    ):
        assert "tmp-test-guide.ko.md" in (ROOT / relative).read_text(encoding="utf-8")


def test_user_guides_describe_current_multiagent_memory_semantics():
    for relative in ("README.md", "docs/user-guide.ko.md", "docs/user-guide.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "agent-runtime:<id>" in text
        assert "shared-from=<chunk-id>" in text
        assert "decompose-plan" in text
        assert "council-critique" in text


def test_tmp_guide_init_doctor_and_three_dry_runs_are_hermetic(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for binary in ("claude", "codex", "kimi"):
        _fake_provider(fake_bin / binary)

    state = tmp_path / "state"
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SYNCMILL_WORKSPACE__ROOT", str(state / "workspaces"))
    monkeypatch.setenv("SYNCMILL_RUNS__ROOT", str(state / "runs"))
    monkeypatch.setenv("SYNCMILL_TUI__STATE_DIR", str(state / "tui"))
    monkeypatch.setenv(
        "SYNCMILL_MEMORY__DB_PATH", str(state / "memory" / "memtomem.db")
    )
    monkeypatch.setenv(
        "SYNCMILL_MEMORY__MEMORY_DIR", str(state / "memory" / "files")
    )
    runner = CliRunner()

    initialized = runner.invoke(cli, ["init"])
    assert initialized.exit_code == 0, initialized.output
    assert (git_repo / "syncmill.toml").is_file()

    checked = runner.invoke(cli, ["doctor"])
    assert checked.exit_code == 0, checked.output
    assert "Agent claude authentication: ready" in checked.output
    assert "Agent codex authentication: ready" in checked.output
    assert "Ready." in checked.output
    assert str(state / "runs") in checked.output

    for agent in ("claude", "codex", "kimi-code"):
        planned = runner.invoke(
            cli,
            [
                "run",
                "--dry-run",
                "Create one provider smoke file",
                "--agent",
                agent,
            ],
        )
        assert planned.exit_code == 0, planned.output
        assert f'"{agent}"' in planned.output
        assert '"maximum_agent_calls": 1' in planned.output
        assert "No agent was started and no source file was changed." in planned.output

    assert not list(git_repo.glob("provider-*.txt"))
