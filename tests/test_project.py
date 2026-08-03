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


def test_init_excludes_the_config_write_lock(git_repo):
    """The lock must sit beside the config (every writer has to derive the same
    path without probing), so init keeps it out of the way: excluded means it
    never shows in `git status`, never gets committed by accident, and — since
    snapshots capture untracked files but skip ignored ones — never rides along
    into every agent worktree."""
    import subprocess

    from syncmill import teamfile

    config_path, _ = _init(git_repo)
    lock = git_repo / teamfile.lock_name_for(config_path.name)
    with teamfile.config_write_lock(config_path):
        pass
    assert lock.is_file()  # it really is created there

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True
    ).stdout
    assert lock.name not in status
    ignored = subprocess.run(
        ["git", "check-ignore", str(lock)], cwd=git_repo, capture_output=True, text=True
    )
    assert ignored.returncode == 0


def test_init_is_idempotent_about_its_exclude_rules(git_repo):
    """Re-running init must not append a second copy of either rule."""
    _init(git_repo)
    _init(git_repo, force=True)
    exclude = (git_repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    lines = exclude.splitlines()
    assert lines.count(".syncmill/memory/") == 1
    assert lines.count("/.syncmill.toml.lock") == 1


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


def test_force_init_refuses_a_non_table_project_key(git_repo):
    """A key holding something else is REFUSED, never replaced — overwriting it
    silently destroys whatever the user had there, which is the whole point."""
    config_path, _ = _init(git_repo)
    config_path.write_text("project = \"not a table\"\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="not a table"):
        _init(git_repo, force=True)
    assert config_path.read_text(encoding="utf-8") == "project = \"not a table\"\n"


def test_force_init_refuses_a_non_table_gate_key(git_repo):
    config_path, _ = _init(git_repo)
    config_path.write_text("gate = 5\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="not a table"):
        _init(git_repo, force=True)
    assert config_path.read_text(encoding="utf-8") == "gate = 5\n"


def test_force_init_accepts_inline_tables(git_repo):
    """An inline table is still a table — merging into it is correct."""
    config_path, _ = _init(git_repo)
    config_path.write_text(
        "gate = { commands = [\"pytest -q\"] }\n", encoding="utf-8"
    )
    _init(git_repo, force=True)
    text = config_path.read_text(encoding="utf-8")
    assert 'commands = ["pytest -q"]' in text


def test_force_init_refuses_a_newer_config_version(git_repo):
    """Stamping the file as version 2 would relabel a schema we cannot read."""
    config_path, _ = _init(git_repo)
    config_path.write_text("config_version = 3\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="newer than this syncmill"):
        _init(git_repo, force=True)
    assert config_path.read_text(encoding="utf-8") == "config_version = 3\n"


def test_force_init_does_not_clobber_an_existing_backup(git_repo):
    """The first repair's backup is the user's only copy of what it replaced."""
    config_path, _ = _init(git_repo)
    config_path.write_text("first = = broken\n", encoding="utf-8")
    _init(git_repo, force=True)
    config_path.write_text("second = = broken\n", encoding="utf-8")
    _init(git_repo, force=True)

    first = config_path.with_suffix(config_path.suffix + ".bak")
    second = config_path.with_suffix(config_path.suffix + ".bak.1")
    assert first.read_text(encoding="utf-8") == "first = = broken\n"
    assert second.read_text(encoding="utf-8") == "second = = broken\n"


def test_force_init_does_not_replace_an_unreadable_config(git_repo):
    """A file we could not READ may be perfectly good — that is an error, not a
    licence to replace it."""
    import os

    config_path, _ = _init(git_repo)
    original = config_path.read_text(encoding="utf-8")
    os.chmod(config_path, 0o000)
    try:
        with pytest.raises(ProjectError, match="cannot read"):
            _init(git_repo, force=True)
    finally:
        os.chmod(config_path, 0o644)
    assert config_path.read_text(encoding="utf-8") == original
    assert not config_path.with_suffix(config_path.suffix + ".bak").exists()


def test_init_survives_a_non_table_project_key_in_the_id_probe(git_repo):
    """`existing.get("project", {}).get("id")` raised AttributeError — neither a
    ValueError nor a TypeError — on a scalar `project`, crashing init outright."""
    config_path, _ = _init(git_repo)
    config_path.write_text("project = \"not a table\"\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="not a table"):  # the merge refusal, not AttributeError
        _init(git_repo, force=True)


def test_force_init_refuses_a_coerced_config_version(git_repo):
    """The config model coerces `3.0` and `"3"` to 3, so an isinstance(int) guard
    let those bypass and be relabelled as version 2."""
    config_path, _ = _init(git_repo)
    for raw in ('config_version = 3.0\n', 'config_version = "3"\n', 'config_version = true\n'):
        config_path.write_text(raw, encoding="utf-8")
        with pytest.raises(ProjectError, match="config_version"):
            _init(git_repo, force=True)
        assert config_path.read_text(encoding="utf-8") == raw


def test_force_init_refuses_an_invalid_low_config_version(git_repo):
    """`SyncmillConfig` requires >= 1, so preserving 0 or a negative would leave
    `init --force` reporting success over a config no command can load."""
    config_path, _ = _init(git_repo)
    for raw in ("config_version = 0\n", "config_version = -1\n"):
        config_path.write_text(raw, encoding="utf-8")
        with pytest.raises(ProjectError, match="not a valid version"):
            _init(git_repo, force=True)
        assert config_path.read_text(encoding="utf-8") == raw


def test_force_init_does_not_move_aside_on_an_unexpected_parser_failure(
    git_repo, monkeypatch
):
    """A blanket `except Exception` treated any parser bug as proof the user's
    config was malformed, and replaced a file that was fine."""

    config_path, _ = _init(git_repo)
    original = config_path.read_text(encoding="utf-8")

    def boom(text):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr("syncmill.project.tomlkit.parse", boom)
    with pytest.raises(RuntimeError, match="parser exploded"):
        _init(git_repo, force=True)
    assert config_path.read_text(encoding="utf-8") == original
    assert not config_path.with_suffix(config_path.suffix + ".bak").exists()


def test_force_init_reserves_its_backup_name_atomically(git_repo, monkeypatch):
    """Two concurrent repairs must not both choose `.bak`.

    Testing `exists()` and then calling `os.replace` is check-then-use: the second
    process's rename silently overwrites the first's backup, destroying the only
    preserved copy. The name is reserved with O_CREAT|O_EXCL instead."""
    from syncmill.project import _reserve_backup_path

    config_path, _ = _init(git_repo)
    first = _reserve_backup_path(config_path)
    second = _reserve_backup_path(config_path)
    assert first != second  # the reservation held; no reuse
    assert first.name.endswith(".bak")
    assert second.name.endswith(".bak.1")


def test_force_init_removes_the_backup_placeholder_when_the_move_fails(
    git_repo, monkeypatch
):
    """The reserved `.bak` must not survive a failed move: an EMPTY backup beside
    a config that is still in place reads as "your config was backed up and lost"."""
    config_path, _ = _init(git_repo)
    config_path.write_text("broken = = toml\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("cross-device link")

    monkeypatch.setattr("syncmill.project.os.replace", boom)
    with pytest.raises(ProjectError, match="could not be moved aside"):
        _init(git_repo, force=True)
    assert config_path.read_text(encoding="utf-8") == "broken = = toml\n"
    assert not config_path.with_suffix(config_path.suffix + ".bak").exists()
