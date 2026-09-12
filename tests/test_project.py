from __future__ import annotations

import json
import os
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
    assert any("supplied by Git" in record.message for record in caplog.records)


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

    from syncmill.project import _MARKER_SUPPLY_TRACKED, _marker_supply, project_id

    marker_dir = git_repo / ".syncmill"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "Project.json").write_text('{"id": "shipped"}\n', encoding="utf-8")
    _git(git_repo, "add", ".syncmill/Project.json")
    _git(git_repo, "commit", "-q", "-m", "ship a case-variant marker")

    if _filesystem_is_case_insensitive(git_repo):
        # Only here does the lowercase read reach the tracked file — and only
        # here, therefore, is the variant the marker at all.
        assert _marker_supply(git_repo) == _MARKER_SUPPLY_TRACKED
        assert project_id(git_repo) == _path_hash_id(git_repo)
    else:
        # A case-sensitive filesystem holds no `.syncmill/project.json`: the
        # tracked variant is an unrelated file and must not be folded onto it.
        assert _marker_supply(git_repo) is None
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

    real_run = sp.run  # captured BEFORE patching: `sp` is the patched module

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "ls-files"]:
            return sp.CompletedProcess(argv, 0, stdout=listing, stderr=b"")
        return real_run(argv, **kwargs)

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


@pytest.mark.parametrize("payload", ["[]", '"just a string"', "null", "12"])
def test_marker_holding_non_object_json_is_ignored(git_repo, payload):
    """Valid JSON that is not an object has no `.get`, and AttributeError is
    neither OSError nor ValueError — the whole file is repository-controllable,
    so that traceback would be a checkout turning every command into a crash."""
    from syncmill.project import project_id

    marker = git_repo / ".syncmill" / "project.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(payload, encoding="utf-8")

    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_init_refuses_a_symlinked_marker_path(git_repo, tmp_path):
    """Reading through a symlink merely distrusts the content; WRITING acts on
    whatever it leads to — here a file outside the repository entirely."""
    outside = tmp_path / "outside.json"
    outside.write_text('{"id": "someone-elses"}\n', encoding="utf-8")
    marker_dir = git_repo / ".syncmill"
    marker_dir.mkdir()
    (marker_dir / "project.json").symlink_to(outside)

    with pytest.raises(ProjectError, match="reached through a symlink"):
        _init(git_repo)
    assert outside.read_text(encoding="utf-8") == '{"id": "someone-elses"}\n'


def test_init_refuses_a_submodule_marker_directory(git_repo, tmp_path):
    """Writing into a populated submodule takes an unrelated repository's
    working tree dirty."""
    from conftest import _git

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

    with pytest.raises(ProjectError, match="Git submodule"):
        _init(git_repo)
    assert (
        (git_repo / ".syncmill" / "project.json").read_text(encoding="utf-8")
        == '{"id": "shipped"}\n'
    )


def test_init_still_rewrites_an_ordinary_tracked_marker(git_repo):
    """A tracked marker is a normal local file — rewriting it with a freshly
    minted id is the upgrade path, not something to refuse."""
    from conftest import _git

    _write_marker(git_repo, "shipped-by-the-repo")
    _git(git_repo, "add", "-f", ".syncmill/project.json")
    _git(git_repo, "commit", "-q", "-m", "an older project committed its marker")

    _, pid = _init(git_repo)
    assert pid != "shipped-by-the-repo"
    assert json.loads(
        (git_repo / ".syncmill" / "project.json").read_text(encoding="utf-8")
    )["id"] == pid


def test_init_writes_the_canonical_rules_even_when_covered_elsewhere(git_repo):
    """`.git/info/exclude`, `core.excludesFile`, and global excludes are all
    MACHINE-LOCAL. A project whose paths are ignored only through those would
    commit an empty `.syncmill/.gitignore` and leave the next clone exposed."""
    exclude = git_repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".syncmill/\n", encoding="utf-8")

    _init(git_repo)
    lines = (git_repo / ".syncmill" / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines()
    assert "memory/" in lines
    assert "project.json" in lines


def test_init_refuses_to_write_when_the_index_cannot_be_read(git_repo, monkeypatch):
    """An unanswered gitlink question is refused with a confirmed one: reading
    falls back to the path hash, but a write has no fallback — the
    cross-repository write would simply happen."""
    import subprocess as sp

    _write_marker(git_repo, "local-machine-id")
    real_run = sp.run  # captured BEFORE patching: `sp` is the patched module

    def fake_run(argv, **kwargs):
        if argv[:2] == ["git", "ls-files"]:
            return sp.CompletedProcess(argv, 128, stdout=b"", stderr=b"broken index")
        return real_run(argv, **kwargs)

    monkeypatch.setattr("syncmill.project.subprocess.run", fake_run)
    with pytest.raises(ProjectError, match="status could not be read"):
        _init(git_repo)


def test_marker_removed_from_the_index_but_still_in_head_is_ignored(git_repo):
    """`git rm --cached` takes the file out of the INDEX while leaving the
    repository-supplied content in the worktree, so an index-only question
    would call it machine-local."""
    from conftest import _git

    from syncmill.project import project_id

    _write_marker(git_repo, "shipped-by-the-repo")
    _git(git_repo, "add", "-f", ".syncmill/project.json")
    _git(git_repo, "commit", "-q", "-m", "ship a marker")
    _git(git_repo, "rm", "--cached", "-q", ".syncmill/project.json")

    assert (git_repo / ".syncmill" / "project.json").is_file()
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_a_case_variant_path_on_a_case_sensitive_filesystem_is_not_the_marker(
    git_repo,
):
    """`:(icase)` also returns genuinely DIFFERENT paths on a case-sensitive
    filesystem, where folding them onto the marker would discard a legitimate
    local id (or, for a submodule, block `init` over an unrelated directory)."""
    from conftest import _git

    from syncmill.project import project_id

    if _filesystem_is_case_insensitive(git_repo):
        pytest.skip("needs a case-sensitive filesystem to be a distinct path")

    _write_marker(git_repo, "local-machine-id")
    variant = git_repo / ".Syncmill"
    variant.mkdir()
    (variant / "project.json").write_text('{"id": "unrelated"}\n', encoding="utf-8")
    _git(git_repo, "add", "-f", ".Syncmill/project.json")
    _git(git_repo, "commit", "-q", "-m", "an unrelated case-variant directory")

    assert project_id(git_repo) == "local-machine-id"


@pytest.mark.parametrize("reserved", ["CON", "nul", "COM1", "con.txt", "LPT9"])
def test_windows_reserved_marker_ids_are_ignored(git_repo, reserved):
    """The id becomes a path component, and Windows refuses these in any casing
    and with any extension — so it would break the artifact/session/room
    constructors rather than degrade the way every other rejection does."""
    from syncmill.project import project_id

    _write_marker(git_repo, reserved)
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_isolated_sessions_strip_pathspec_overrides():
    """The isolated-session launch set is an explicit union, so a new family of
    Git variables has to be added there too — it does not ride
    `is_git_routing_var`."""
    from syncmill.isolated_session import _ISOLATED_LAUNCH_ROUTING_ENV

    assert "GIT_LITERAL_PATHSPECS" in _ISOLATED_LAUNCH_ROUTING_ENV
    assert "GIT_ICASE_PATHSPECS" in _ISOLATED_LAUNCH_ROUTING_ENV


def test_submodule_marker_removed_from_the_index_is_still_ignored(git_repo, tmp_path):
    """Git does not traverse a gitlink, so for a committed `.syncmill` submodule
    staged away with `git rm --cached`, `HEAD:.syncmill/project.json` resolves
    to nothing while the populated worktree and its shipped marker remain."""
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
    _git(git_repo, "rm", "--cached", "-q", ".syncmill")

    assert (git_repo / ".syncmill" / "project.json").is_file()
    assert project_id(git_repo) == _path_hash_id(git_repo)
    with pytest.raises(ProjectError, match="Git submodule"):
        _init(git_repo)


def test_init_allowed_after_a_submodule_is_fully_removed(git_repo, tmp_path):
    """`git rm .syncmill` stages the submodule's removal and deletes its
    worktree in one step; only HEAD still remembers the gitlink. There is no
    nested repository left for `init` to write into, so refusing here would
    force an intermediate commit into an ordinary submodule-to-directory
    conversion."""
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
    _git(git_repo, "rm", "-q", ".syncmill")
    assert not (git_repo / ".syncmill").exists()

    _init(git_repo)
    assert (git_repo / ".syncmill" / "project.json").is_file()
    # The READ path stays conservative while HEAD still records the gitlink —
    # the freshly minted marker is indistinguishable from a shipped one until
    # the removal is committed, so identity rides the path hash meanwhile.
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_failed_force_init_leaves_a_tracked_marker_untouched(git_repo):
    """The marker is written last: a forced re-init whose config merge refuses
    (here a newer config_version) must exit without having overwritten a
    TRACKED marker — the command failed, so repository content stays put."""
    from conftest import _git

    marker = _write_marker(git_repo, "committed-id")
    _git(git_repo, "add", ".syncmill/project.json")
    _git(git_repo, "commit", "-q", "-m", "track the marker")
    (git_repo / "syncmill.toml").write_text(
        "config_version = 99\n", encoding="utf-8"
    )
    before = marker.read_text(encoding="utf-8")

    with pytest.raises(ProjectError, match="newer than this syncmill"):
        _init(git_repo, force=True)
    assert marker.read_text(encoding="utf-8") == before
    # The (committable) ignore file is another tracked artifact a refused
    # init must not have touched — validation now precedes every mutation.
    assert not (git_repo / ".syncmill" / ".gitignore").exists()


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs file write bits"
)
def test_failed_exclude_append_leaves_the_config_unwritten(git_repo):
    """An unwritable `.git/info/exclude` that still needs a rule must refuse
    BEFORE `syncmill.toml` commits — the config is the artifact that flips a
    retry into 'already initialized', so it lands last."""
    exclude = git_repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".syncmill/memory/\n", encoding="utf-8")
    os.chmod(exclude, 0o400)
    try:
        with pytest.raises(ProjectError, match="protect project memory"):
            _init(git_repo)
    finally:
        os.chmod(exclude, 0o600)
    assert not (git_repo / "syncmill.toml").exists()


def test_init_refuses_a_directory_at_the_marker_path(git_repo):
    """A directory sitting exactly at `.syncmill/project.json` must refuse
    up front: the final atomic swap can only replace a file, and discovering
    that at the end of init used to leave syncmill.toml and the ignore file
    already written — with a retry rejected as already initialized."""
    (git_repo / ".syncmill" / "project.json").mkdir(parents=True)

    with pytest.raises(ProjectError, match="is a directory"):
        _init(git_repo)
    assert not (git_repo / "syncmill.toml").exists()
    assert not (git_repo / ".syncmill" / ".gitignore").exists()


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs directory write bits"
)
def test_failed_marker_staging_leaves_the_config_untouched(git_repo):
    """The marker is STAGED before anything commits: an unwritable marker
    directory refuses init before `syncmill.toml` changes, instead of after —
    where a retry without --force would claim 'already initialized' while the
    marker still holds the old id."""
    config_path, _ = _init(git_repo)
    before = config_path.read_text(encoding="utf-8")
    marker_dir = git_repo / ".syncmill"
    os.chmod(marker_dir, 0o500)
    try:
        with pytest.raises(ProjectError, match="cannot stage"):
            _init(git_repo, force=True)
    finally:
        os.chmod(marker_dir, 0o700)
    assert config_path.read_text(encoding="utf-8") == before


def test_init_refuses_an_untracked_embedded_checkout(git_repo):
    """No gitlink anywhere names `.syncmill` — an untracked embedded checkout,
    or a former submodule kept on disk after its removal was committed — yet
    the directory is still another repository's working tree, and `init`
    would rewrite its tracked `project.json` and append to its ignore file."""
    from conftest import _git

    nested = git_repo / ".syncmill"
    nested.mkdir()
    _git(nested, "init", "-q", ".")
    (nested / "project.json").write_text('{"id": "theirs"}\n', encoding="utf-8")

    with pytest.raises(ProjectError, match="Git submodule"):
        _init(git_repo)


def test_init_allowed_after_a_tracked_marker_file_blob_is_removed(git_repo):
    """HEAD can track `.syncmill` as a regular FILE; staging its removal is
    the ordinary conversion of that path into the project directory. The HEAD
    walk must not descend `ls-tree` into the blob — the failed descent used
    to read as unreadable repository state and refuse `init` until an
    intermediate commit."""
    from conftest import _git

    (git_repo / ".syncmill").write_text("not a directory\n", encoding="utf-8")
    _git(git_repo, "add", ".syncmill")
    _git(git_repo, "commit", "-q", "-m", "track .syncmill as a file")
    _git(git_repo, "rm", "-q", ".syncmill")

    _init(git_repo)
    assert (git_repo / ".syncmill" / "project.json").is_file()


def test_materialized_symlink_marker_is_still_repository_supplied(git_repo):
    """Under `core.symlinks=false` (the common Windows checkout) Git writes a
    120000 entry's link payload to disk as a REGULAR file — so a repository
    can ship the marker itself as a symlink whose payload is the JSON. No
    symlink guard fires on the real file, and the round-14 exclusion of
    symlink entries must not skip it: the content arrived with the clone, in
    both the index and HEAD halves."""
    from conftest import _git

    from syncmill.project import project_id

    payload = git_repo / "payload"
    payload.write_text('{"id": "shipped-via-link"}', encoding="utf-8")
    blob = _git(git_repo, "hash-object", "-w", "payload").stdout.strip()
    payload.unlink()
    _git(
        git_repo, "update-index", "--add", "--cacheinfo",
        f"120000,{blob},.syncmill/project.json",
    )
    _git(git_repo, "commit", "-q", "-m", "ship the marker as a link payload")
    _git(git_repo, "config", "core.symlinks", "false")
    _git(git_repo, "checkout", "--", ".syncmill")
    marker = git_repo / ".syncmill" / "project.json"
    assert marker.is_file() and not marker.is_symlink()
    assert "shipped-via-link" in marker.read_text(encoding="utf-8")

    assert project_id(git_repo) == _path_hash_id(git_repo)

    # The HEAD half must answer the same once the index entry is staged away.
    _git(git_repo, "rm", "--cached", "-q", ".syncmill/project.json")
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_gitlink_in_head_wins_over_a_tracked_answer_from_the_index(
    git_repo, tmp_path, monkeypatch
):
    """A plain "tracked" from the index must not end the question while HEAD
    still records `.syncmill` as a gitlink: only the gitlink answer stops `init`
    writing into a nested repository.

    The index half is forced here because Git will not stage a path inside a
    LIVE submodule, so the two sources cannot actually disagree that way while
    the nested repository is still there — the precedence is what is being
    pinned, not a reachable Git state."""
    from conftest import _git

    from syncmill.project import _MARKER_SUPPLY_GITLINK, _marker_supply

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
    monkeypatch.setattr(
        "syncmill.project._index_supply", lambda root: "tracked"
    )

    assert _marker_supply(git_repo) == _MARKER_SUPPLY_GITLINK
    with pytest.raises(ProjectError, match="Git submodule"):
        _init(git_repo)


@pytest.mark.parametrize("hostile", ["cache.", "id..", "CON", "com1.txt"])
def test_windows_unsafe_marker_ids_are_ignored(git_repo, hostile):
    """Win32 strips a trailing period from a path component, so `cache.` would
    silently address `cache` — another project's state — instead of failing."""
    from syncmill.project import project_id

    _write_marker(git_repo, hostile)
    assert project_id(git_repo) == _path_hash_id(git_repo)


def test_path_identity_is_asked_of_the_filesystem_not_the_name(tmp_path):
    """"These two names open the same file" is not a question `casefold` can
    answer for someone else's filesystem — NTFS resolves names through an
    uppercase table that is not Unicode case folding. So every non-exact
    candidate goes to the filesystem, whatever its spelling."""
    from syncmill.project import _names_marker_path

    (tmp_path / "real.json").write_text("{}", encoding="utf-8")
    (tmp_path / "alias.json").symlink_to(tmp_path / "real.json")

    # Spellings with nothing in common, yet one file.
    assert _names_marker_path(tmp_path, "alias.json", "real.json") is True
    # And an unrelated name is not an alias just because it was listed.
    assert _names_marker_path(tmp_path, "other.json", "real.json") is False


def test_head_enumeration_catches_an_alias_git_case_matching_would_miss(
    git_repo, monkeypatch
):
    """The HEAD half lists without a pathspec, so it does not inherit Git's
    narrower `:(icase)` notion of a match — the filesystem decides."""
    from conftest import _git

    from syncmill.project import _MARKER_SUPPLY_TRACKED, _head_supply

    _write_marker(git_repo, "shipped-by-the-repo")
    _git(git_repo, "add", "-f", ".syncmill/project.json")
    _git(git_repo, "commit", "-q", "-m", "ship a marker")
    # Force the index half to see nothing, leaving HEAD as the only source.
    monkeypatch.setattr("syncmill.project._index_supply", lambda root: None)

    assert _head_supply(git_repo) == _MARKER_SUPPLY_TRACKED


def test_init_refuses_a_symlinked_local_ignore_file(git_repo, tmp_path):
    """The ignore file beside the marker is written too, so a checkout can
    leave the marker path ordinary and track `.syncmill/.gitignore` as a
    symlink — `init` would then append its rules to a file of the checkout's
    choosing, outside the repository."""
    outside = tmp_path / "victim.txt"
    outside.write_text("important\n", encoding="utf-8")
    marker_dir = git_repo / ".syncmill"
    marker_dir.mkdir()
    (marker_dir / ".gitignore").symlink_to(outside)

    with pytest.raises(ProjectError, match="reached through a symlink"):
        _init(git_repo)
    assert outside.read_text(encoding="utf-8") == "important\n"


def test_index_half_finds_a_marker_under_an_aliasing_directory_name(git_repo):
    """Git's `:(icase)` is narrower than the filesystem's notion of an alias —
    on NTFS `.syncmıll` opens as the canonical path while the pathspec skips
    it. A symlink stands in for that aliasing here, since the dotless-ı case
    needs an NTFS uppercase table; the mechanism under test is the same one:
    the on-disk names that actually resolve to the marker directory are asked
    of the filesystem and passed to Git literally."""
    from conftest import _git

    from syncmill.project import _MARKER_SUPPLY_TRACKED, _index_supply

    payload = git_repo / "payload"
    payload.mkdir()
    (payload / "project.json").write_text('{"id": "shipped"}\n', encoding="utf-8")
    (git_repo / ".syncmill").symlink_to("payload", target_is_directory=True)
    _git(git_repo, "add", "-f", "payload/project.json")
    _git(git_repo, "commit", "-q", "-m", "ship a marker under another name")

    # `:(icase).syncmill` never names `payload/project.json`; the alias does.
    assert _index_supply(git_repo) == _MARKER_SUPPLY_TRACKED


def test_alias_pathspecs_are_empty_in_the_ordinary_case(git_repo):
    """The scan must not manufacture pathspecs for a normal project."""
    from syncmill.project import _alias_pathspecs

    _init(git_repo)
    assert _alias_pathspecs(git_repo, ".syncmill") == []


def test_a_tracked_symlink_to_the_marker_does_not_discard_a_local_id(git_repo):
    """A repository must not be able to break an ordinary project by tracking
    a symlink that happens to resolve to the marker. What Git supplies there is
    the LINK; the content is this machine's own file."""
    from conftest import _git

    from syncmill.project import project_id

    _write_marker(git_repo, "local-machine-id")
    # Inside the marker directory, so the listing actually reaches it.
    (git_repo / ".syncmill" / "alias.json").symlink_to(
        git_repo / ".syncmill" / "project.json"
    )
    _git(git_repo, "add", "-f", ".syncmill/alias.json")
    _git(git_repo, "commit", "-q", "-m", "track a symlink at the marker")

    assert project_id(git_repo) == "local-machine-id"


def test_a_tracked_symlink_to_the_marker_directory_does_not_block_init(git_repo):
    """The directory case would send `ls-tree` descending into a blob, answer
    "unknown", and block `init --force` outright."""
    from conftest import _git

    from syncmill.project import _MARKER_SUPPLY_UNKNOWN, _marker_supply

    _init(git_repo)
    (git_repo / "alias").symlink_to(git_repo / ".syncmill", target_is_directory=True)
    _git(git_repo, "add", "-f", "alias")
    _git(git_repo, "commit", "-q", "-m", "track a symlink at the marker directory")

    assert _marker_supply(git_repo) != _MARKER_SUPPLY_UNKNOWN
    _init(git_repo, force=True)  # must not raise


def test_failed_config_commit_leaves_the_ignore_appends_behind(
    git_repo, monkeypatch, isolated_home
):
    """Characterization of init's accepted non-atomic window (#99).

    Everything init can refuse on is checked before it mutates anything, and the
    config commits last — but three files cannot be swapped atomically, so a
    failure of the config's own atomic write leaves the ignore appends already
    on disk. This pins that window: the two artifacts a retry depends on
    (`syncmill.toml` and the marker) are still absent, so a plain `syncmill init`
    retries cleanly rather than refusing as 'already initialized' over a
    half-built tree — which is why the order is what it is. The leftover ignore
    rules are re-appended idempotently by that retry.

    Scope, so the claim is not read wider than it is: this drives a failure
    BEFORE the config is published. A failure of the marker `os.replace` that
    follows a successful config write leaves `syncmill.toml` on disk, and the
    refusal keys on that file, so such a retry does need --force. That window is
    one replace of an already-staged sibling, and it is not what this test
    covers.

    Reopen #99 only if a real report shows the leftovers matter; closing the
    window fully needs an undo journal for appends this test would then invert.
    """
    from syncmill import project as project_module
    from syncmill import teamfile

    def boom(path, text):
        raise teamfile.TeamFileError(
            f"cannot write {path}: [Errno 28] No space left on device"
        )

    # A scoped context, not `monkeypatch.undo()`: undo() also reverses the
    # autouse fixtures' patches, including HOME isolation, so the retry below
    # would reach the real `~/.syncmill`.
    with monkeypatch.context() as patch:
        patch.setattr(teamfile, "_atomic_write", boom)
        with pytest.raises(ProjectError, match="No space left on device"):
            _init(git_repo)

    # Fail-closed halves: a retry is not blocked.
    assert not (git_repo / "syncmill.toml").exists()
    assert not (git_repo / ".syncmill" / "project.json").exists()
    assert not list((git_repo / ".syncmill").glob("*.tmp"))

    # The accepted residue: the tracked ignore file is already written, and the
    # machine-local exclude already appended.
    local_ignore = git_repo / ".syncmill" / ".gitignore"
    assert local_ignore.exists()
    rules = local_ignore.read_text(encoding="utf-8").split()
    assert "memory/" in rules and "project.json" in rules
    exclude = (git_repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert project_module._MARKER_RELATIVE in exclude

    # And the retry the ordering protects actually succeeds, converging the
    # appends rather than duplicating them. The HOME check is the guard on the
    # comment above: `_init` takes a config lock under `~/.syncmill/locks`, so a
    # test that let the isolation lapse would write into the developer's home.
    assert Path(os.environ["HOME"]) == isolated_home
    _init(git_repo)
    assert (git_repo / "syncmill.toml").exists()
    assert (git_repo / ".syncmill" / "project.json").exists()
    assert local_ignore.read_text(encoding="utf-8").split().count("project.json") == 1


def test_a_failed_marker_swap_after_the_config_lands_does_block_a_retry(
    git_repo, monkeypatch, isolated_home
):
    """The second, narrower window — the one the sibling test does NOT cover.

    The sibling test drives a failure before the config is published, where the
    tracked-last ordering leaves a retry unblocked. This pins what happens on
    the other side of that boundary: `_write_init_artifacts` succeeds, the
    marker `os.replace` that follows it fails, and `syncmill.toml` is on disk
    with no marker beside it. The refusal at the top of `initialize_project`
    keys on that file, so a plain retry is turned away and `--force` is the way
    through.

    Recorded rather than fixed. It is one replace of an already-staged sibling,
    and closing it would need an undo journal for the ignore appends — the same
    reason #99 was accepted. The point is that the claim in the comment above
    `initialize_project`'s try block stays true to what the code does.
    """
    from syncmill import project as project_module

    real_replace = os.replace
    marker_name = Path(project_module._MARKER_RELATIVE).name

    def failing_marker_swap(src, dst, *args, **kwargs):
        if Path(dst).name == marker_name:
            raise OSError("simulated marker replace failure")
        return real_replace(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(project_module.os, "replace", failing_marker_swap)
        with pytest.raises(OSError, match="simulated marker replace failure"):
            _init(git_repo)

    # The config landed; the marker did not.
    assert (git_repo / "syncmill.toml").exists()
    assert not (git_repo / project_module._MARKER_RELATIVE).exists()

    # So the retry the sibling test relies on is refused here.
    assert Path(os.environ["HOME"]) == isolated_home
    with pytest.raises(ProjectError, match="already initialized"):
        _init(git_repo)

    # --force is the documented way through, and it converges.
    _init(git_repo, force=True)
    assert (git_repo / project_module._MARKER_RELATIVE).exists()
