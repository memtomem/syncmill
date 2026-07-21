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
