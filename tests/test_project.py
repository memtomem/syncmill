from __future__ import annotations

from pathlib import Path

import pytest

from syncmill.config import SyncmillConfig
from syncmill.project import ProjectError, resolve_project_state_paths


def _project_local_config() -> SyncmillConfig:
    return SyncmillConfig(
        repo_root=".",
        runs={"root": ".state"},
        workspace={"root": ".workspaces"},
    )


def test_project_local_state_roots_must_be_git_ignored(git_repo, tmp_path):
    with pytest.raises(ProjectError, match="runs.root.*must be Git-ignored"):
        resolve_project_state_paths(_project_local_config(), git_repo)


def test_project_local_state_roots_are_allowed_when_git_ignored(git_repo):
    exclude = git_repo / ".git" / "info" / "exclude"
    exclude.write_text(".state/\n.workspaces/\n", encoding="utf-8")
    config = resolve_project_state_paths(_project_local_config(), git_repo)

    assert config.runs.root == str((git_repo / ".state").resolve())
    assert config.workspace.root == str((git_repo / ".workspaces").resolve())


def test_state_root_cannot_use_git_metadata_directory(git_repo):
    config = SyncmillConfig(
        repo_root=".",
        runs={"root": ".git/syncmill"},
        workspace={"root": str(Path(git_repo).parent / "workspaces")},
    )
    with pytest.raises(ProjectError, match="outside the repository"):
        resolve_project_state_paths(config, git_repo)


# ── init --force must not discard a user's configuration ─────────────────────


def _init(repo: Path, *, force: bool = False):
    from syncmill.project import initialize_project

    return initialize_project(repo, force=force)


def test_force_init_preserves_user_configuration(git_repo):
    """`--force` is a repair verb, not a reset one.

    It used to write the template over whatever was there, keeping only
    `project.id` — so a re-init silently discarded gate commands, per-agent
    settings, the chosen strategy, routing, and team profiles."""
    config_path, pid = _init(git_repo)
    config_path.write_text(
        "config_version = 2\n"
        "repo_root = \".\"\n"
        "default_strategy = \"compete\"\n\n"
        "# keep me\n"
        "[project]\n"
        f"id = \"{pid}\"\n"
        "memory_dir = \".syncmill/memory\"\n\n"
        "[gate]\n"
        "commands = [\"pytest -q\"]\n\n"
        "[claude]\n"
        "timeout_seconds = 900\n\n"
        "[teams.python-team]\n"
        "agents = [\"codex\", \"claude\"]\n",
        encoding="utf-8",
    )

    again_path, again_pid = _init(git_repo, force=True)
    assert again_path == config_path
    assert again_pid == pid  # identity is still preserved

    text = config_path.read_text(encoding="utf-8")
    assert 'default_strategy = "compete"' in text  # not reset to route
    assert 'commands = ["pytest -q"]' in text
    assert "timeout_seconds = 900" in text
    assert "[teams.python-team]" in text
    assert "# keep me" in text  # comments survive the round-trip


def test_force_init_fills_in_missing_project_keys(git_repo):
    """Merging must still repair: the keys a project needs are added when absent."""
    config_path, _ = _init(git_repo)
    config_path.write_text("default_strategy = \"pipeline\"\n", encoding="utf-8")

    _, pid = _init(git_repo, force=True)
    text = config_path.read_text(encoding="utf-8")
    assert "config_version = 2" in text
    assert f'id = "{pid}"' in text
    assert 'memory_dir = ".syncmill/memory"' in text
    assert "commands = []" in text
    assert 'default_strategy = "pipeline"' in text  # the user's value is kept


def test_force_init_moves_an_unparseable_config_aside(git_repo):
    """An unmergeable file is preserved, not dropped."""
    config_path, _ = _init(git_repo)
    config_path.write_text("this is not = = toml\n", encoding="utf-8")

    _init(git_repo, force=True)
    backup = config_path.with_suffix(config_path.suffix + ".bak")
    assert backup.read_text(encoding="utf-8") == "this is not = = toml\n"
    assert "config_version = 2" in config_path.read_text(encoding="utf-8")


def test_force_init_preserves_the_config_file_mode(git_repo):
    """A checked-in 0644 config must not come back 0600 (git noise + group reads)."""
    import os
    import stat

    config_path, _ = _init(git_repo)
    os.chmod(config_path, 0o644)
    _init(git_repo, force=True)
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o644
