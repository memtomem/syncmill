from __future__ import annotations

import json
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


# ── project_id must not trust an id the repository shipped ───────────────────


def _write_marker(repo: Path, pid: str) -> Path:
    import json

    marker = repo / ".syncmill" / "project.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"id": pid}) + "\n", encoding="utf-8")
    return marker


def _path_hash_id(root: Path) -> str:
    import hashlib

    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]


def test_untracked_marker_id_is_trusted(git_repo):
    from syncmill.project import project_id

    _write_marker(git_repo, "local-machine-id")
    assert project_id(git_repo) == "local-machine-id"


def test_tracked_marker_id_is_ignored(git_repo):
    """A tracked marker arrived with the clone, not from this machine's init —
    honoring it would bind this machine's state roots to an id chosen by
    whoever committed it."""
    from conftest import _git

    from syncmill.project import project_id

    _write_marker(git_repo, "shipped-by-the-repo")
    _git(git_repo, "add", ".syncmill/project.json")
    _git(git_repo, "commit", "-q", "-m", "ship a marker")

    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_staged_marker_id_is_ignored(git_repo):
    """`git add` alone makes the marker tracked; the distrust must not wait
    for a commit."""
    from conftest import _git

    from syncmill.project import project_id

    _write_marker(git_repo, "staged-only")
    _git(git_repo, "add", ".syncmill/project.json")

    assert project_id(git_repo) == _path_hash_id(git_repo)


@pytest.mark.parametrize(
    "hostile",
    [
        "../escape",
        "nested/id",
        "..",
        ".hidden",
        "a" * 65,
        "id with spaces",
    ],
)
def test_unsafe_marker_id_is_ignored(git_repo, hostile):
    """The id becomes a directory component under the state roots, so anything
    that is not a single conservative path token falls back to the path hash."""
    from syncmill.project import project_id

    _write_marker(git_repo, hostile)
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_configured_id_still_beats_the_marker(git_repo):
    from syncmill.project import project_id

    _write_marker(git_repo, "from-the-marker")
    assert project_id(git_repo, "from-config") == "from-config"


def test_rejected_marker_warns(git_repo, caplog):
    """A silent identity flip would read as vanished runs; the reader gets told
    why the marker was passed over."""
    import logging

    from conftest import _git

    from syncmill.project import project_id

    _write_marker(git_repo, "shipped-by-the-repo")
    _git(git_repo, "add", ".syncmill/project.json")
    with caplog.at_level(logging.WARNING, logger="syncmill.project"):
        project_id(git_repo)
    assert any("tracked by Git" in record.message for record in caplog.records)


def test_init_mints_a_new_id_over_a_tracked_marker(git_repo):
    """Re-initializing must not launder a shipped id into the config, where the
    configured value IS trusted."""
    from conftest import _git

    _write_marker(git_repo, "shipped-by-the-repo")
    _git(git_repo, "add", ".syncmill/project.json")
    _git(git_repo, "commit", "-q", "-m", "ship a marker")

    _, pid = _init(git_repo)
    assert pid != "shipped-by-the-repo"


def test_init_preserves_an_untracked_marker_id(git_repo):
    _write_marker(git_repo, "minted-here-earlier")
    _, pid = _init(git_repo)
    assert pid == "minted-here-earlier"


def test_init_git_ignores_the_marker(git_repo):
    """A fresh project must never accidentally commit the marker — becoming
    tracked is exactly what stops it being trusted."""
    import subprocess

    _init(git_repo)
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--", ".syncmill/project.json"],
        cwd=str(git_repo),
        capture_output=True,
    )
    assert ignored.returncode == 0
    exclude = (git_repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".syncmill/project.json" in exclude.splitlines()


def test_init_adds_the_marker_rule_to_an_existing_ignore(git_repo):
    """An upgrade path: a pre-existing `.syncmill/.gitignore` holding only the
    memory rule gains the marker rule without duplicating either."""
    local_ignore = git_repo / ".syncmill" / ".gitignore"
    local_ignore.parent.mkdir(parents=True, exist_ok=True)
    local_ignore.write_text("memory/\n", encoding="utf-8")

    _init(git_repo)
    lines = local_ignore.read_text(encoding="utf-8").splitlines()
    assert lines.count("memory/") == 1
    assert lines.count("project.json") == 1


def _filesystem_is_case_insensitive(root: Path) -> bool:
    probe = root / "SyncmillCaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (root / "syncmillcaseprobe").exists()
    finally:
        probe.unlink()


def test_case_variant_tracked_marker_is_ignored(git_repo):
    """A repository can commit `.syncmill/Project.json`. On a case-insensitive
    filesystem (the macOS and Windows defaults) the read opens that tracked
    file through the lowercase path, so an exact-case trackedness query would
    report "untracked" and trust an id the repository shipped."""
    from conftest import _git

    from syncmill.project import _marker_is_tracked, project_id

    marker_dir = git_repo / ".syncmill"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "Project.json").write_text('{"id": "shipped"}\n', encoding="utf-8")
    _git(git_repo, "add", ".syncmill/Project.json")
    _git(git_repo, "commit", "-q", "-m", "ship a case-variant marker")

    assert _marker_is_tracked(git_repo) is True
    if _filesystem_is_case_insensitive(git_repo):
        # Only here does the lowercase read actually reach the tracked file.
        assert project_id(git_repo) == _path_hash_id(git_repo)


def test_marker_id_with_a_trailing_newline_is_ignored(git_repo):
    """`$` also matches just before a trailing newline, so a whole-string match
    is what keeps a newline out of a directory name."""
    from syncmill.project import project_id

    marker = git_repo / ".syncmill" / "project.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"id": "trailing-newline\\n"}\n', encoding="utf-8")

    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_marker_is_distrusted_when_git_cannot_answer(git_repo, monkeypatch):
    """An unanswered question distrusts the marker: the path-hash fallback is
    always safe, a shipped id never is."""
    from syncmill.project import project_id

    _write_marker(git_repo, "local-machine-id")

    def boom(*args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr("syncmill.project.subprocess.run", boom)
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_marker_is_distrusted_outside_a_git_repository(tmp_path):
    from syncmill.project import project_id

    _write_marker(tmp_path, "local-machine-id")
    assert project_id(tmp_path) == _path_hash_id(tmp_path)


def test_marker_reached_through_a_symlink_is_ignored(git_repo):
    """A trackedness query is lexical. If a checkout commits `.syncmill` as a
    symlink to a checked-in directory, the index holds the link and the payload
    under two names, the query for `.syncmill/project.json` finds neither — and
    the read still lands on repository-controlled content."""
    from conftest import _git

    from syncmill.project import project_id

    payload = git_repo / "payload"
    payload.mkdir()
    (payload / "project.json").write_text('{"id": "shipped"}\n', encoding="utf-8")
    (git_repo / ".syncmill").symlink_to("payload", target_is_directory=True)
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "ship a symlinked marker directory")

    assert (git_repo / ".syncmill" / "project.json").read_text().strip().endswith("}")
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_marker_file_that_is_a_symlink_is_ignored(git_repo):
    from syncmill.project import project_id

    payload = git_repo / "payload.json"
    payload.write_text('{"id": "shipped"}\n', encoding="utf-8")
    marker_dir = git_repo / ".syncmill"
    marker_dir.mkdir()
    (marker_dir / "project.json").symlink_to(payload)

    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_literal_pathspec_env_cannot_hide_a_tracked_marker(git_repo, monkeypatch):
    """`GIT_LITERAL_PATHSPECS` turns the `:(icase)` pathspec into a filename
    nothing matches, so a tracked marker would read as untracked. The variable
    is stripped by `isolated_git_env` along with the other routing controls."""
    from conftest import _git

    from syncmill.project import project_id

    _write_marker(git_repo, "shipped-by-the-repo")
    _git(git_repo, "add", ".syncmill/project.json")
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")

    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_icase_pathspec_env_is_stripped_too():
    """The whole pathspec-semantics family is stripped, not just the one that
    happened to defeat this call: each redefines what a written pathspec means,
    and `check-ignore`'s state-root probes ask the same kind of question."""
    from syncmill.git_env import is_git_routing_var

    for key in (
        "GIT_LITERAL_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
    ):
        assert is_git_routing_var(key) is True


def test_marker_from_a_tracked_submodule_is_ignored(git_repo, tmp_path):
    """A populated `.syncmill` submodule is a single gitlink entry in the
    superproject index, so nothing names `.syncmill/project.json` — yet the
    file arrives with the clone all the same, and the directory is not a
    symlink either."""
    from conftest import _git

    from syncmill.project import project_id

    source = tmp_path / "marker-src"
    source.mkdir()
    _git(source, "init", "-q", ".")
    _git(source, "config", "user.email", "t@t.test")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "commit.gpgsign", "false")
    (source / "project.json").write_text('{"id": "shipped"}\n', encoding="utf-8")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "ship a marker")

    _git(
        git_repo, "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", str(source), ".syncmill",
    )
    _git(git_repo, "commit", "-q", "-m", "add the marker submodule")

    assert (git_repo / ".syncmill" / "project.json").is_file()
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_a_tracked_sibling_does_not_make_the_marker_tracked(git_repo):
    """`init` writes a `.syncmill/.gitignore` the project is MEANT to commit.
    Trackedness is a question about the marker's own path, so a committed
    sibling must not cost the project its id."""
    from conftest import _git

    from syncmill.project import project_id

    _init(git_repo)
    _git(git_repo, "add", "-f", ".syncmill/.gitignore")
    _git(git_repo, "commit", "-q", "-m", "commit the ignore file")

    marker_id = json.loads(
        (git_repo / ".syncmill" / "project.json").read_text(encoding="utf-8")
    )["id"]
    assert project_id(git_repo) == marker_id


def test_init_ignores_the_marker_despite_a_negation(git_repo):
    """A `.gitignore` can hold the rule and then negate it. A membership test
    sees its own rule present and adds nothing while Git leaves the marker
    exposed — and `.git/info/exclude` cannot rescue it, since a per-directory
    `.gitignore` outranks it."""
    from syncmill.project import _path_is_ignored

    marker_dir = git_repo / ".syncmill"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / ".gitignore").write_text(
        "project.json\n!project.json\n", encoding="utf-8"
    )

    _init(git_repo)
    assert _path_is_ignored(git_repo, ".syncmill/project.json") is True


def test_init_ignores_the_marker_despite_a_blanket_negation(git_repo):
    from syncmill.project import _path_is_ignored

    marker_dir = git_repo / ".syncmill"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / ".gitignore").write_text("project.json\n!*\n", encoding="utf-8")

    _init(git_repo)
    assert _path_is_ignored(git_repo, ".syncmill/project.json") is True


def test_init_does_not_duplicate_an_effective_ignore_rule(git_repo):
    """Re-initializing an already-protected project appends nothing."""
    _init(git_repo)
    local_ignore = git_repo / ".syncmill" / ".gitignore"
    first = local_ignore.read_text(encoding="utf-8")

    _init(git_repo, force=True)
    assert local_ignore.read_text(encoding="utf-8") == first


def test_trackedness_survives_an_undecodable_sibling_name(git_repo, monkeypatch):
    """The listing covers the whole marker directory, so a tracked sibling whose
    name is not valid UTF-8 (legal on POSIX) reaches the parser as raw bytes.
    Decoding it strictly would raise UnicodeDecodeError straight out of
    `project_id`, which is required to degrade rather than fail."""
    import subprocess as sp

    from syncmill.project import project_id

    _write_marker(git_repo, "local-machine-id")
    listing = (
        b"100644 aa 0\t.syncmill/bad\xff.txt\x00"
        b"100644 bb 0\t.syncmill/project.json\x00"
    )

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "ls-files"]:
            return sp.CompletedProcess(argv, 0, stdout=listing, stderr=b"")
        return sp.run(argv, **kwargs)

    monkeypatch.setattr("syncmill.project.subprocess.run", fake_run)
    # The marker's own path is in the listing, so it is tracked and rejected —
    # the point is that the undecodable sibling did not crash the read.
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_init_does_not_duplicate_the_rule_for_a_tracked_marker(git_repo):
    """The upgrade case: a project that already committed its marker. Plain
    `check-ignore` suppresses tracked paths, so without `--no-index` the answer
    is "not ignored" however good the rules are, and every `init --force`
    appends one more identical line."""
    from conftest import _git

    _init(git_repo)
    local_ignore = git_repo / ".syncmill" / ".gitignore"
    _git(git_repo, "add", "-f", ".syncmill/project.json", ".syncmill/.gitignore")
    _git(git_repo, "commit", "-q", "-m", "an older project committed its marker")

    _init(git_repo, force=True)
    _init(git_repo, force=True)
    lines = local_ignore.read_text(encoding="utf-8").splitlines()
    assert lines.count("project.json") == 1


def test_init_creates_the_local_ignore_file_even_when_already_covered(git_repo):
    """`init` documents this file as one of its artifacts and the guides tell
    the reader to commit it, so a rule covered by a parent `.gitignore` must
    not leave a fresh project without one."""
    (git_repo / ".gitignore").write_text(".syncmill/\n", encoding="utf-8")

    _init(git_repo)
    assert (git_repo / ".syncmill" / ".gitignore").is_file()
