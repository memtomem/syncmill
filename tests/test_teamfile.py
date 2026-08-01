"""Tests for teamfile.py — the tomlkit syncmill.toml write path (P3 TEAM pane).

All hermetic: every path is a ``tmp_path`` file; the autouse
``isolate_toml_discovery`` fixture already points ``model_config["toml_file"]``
at an absent tmp path, so nothing here can ever touch a real repo-root
``syncmill.toml`` even when a test forgets to repoint.
"""

from __future__ import annotations

import pytest
import tomlkit
from pydantic import ValidationError

from syncmill import teamfile
from syncmill.config import SyncmillConfig, TeamConfig
from syncmill.teamfile import (
    TeamFileError,
    build_team,
    load_config_at,
    load_document,
    parse_capability_specs,
    render_team,
    save_team,
    set_default_team,
    toml_path,
    validate_team_save,
)

# ── helpers ────────────────────────────────────────────────────────────────

COMMENT_HEAVY = """# project baseline — hand-written comments must survive writes
default_strategy = "route"  # inline comment

[claude]
timeout_seconds = 300  # keep me

[teams.existing-team]
agents = ["claude"]  # sibling team, untouched
goals = ["frontend"]
"""


def _team(**kw) -> TeamConfig:
    kw.setdefault("agents", ["codex"])
    return TeamConfig(**kw)


# ── toml_path ──────────────────────────────────────────────────────────────


def test_toml_path_honors_model_config(tmp_path, monkeypatch):
    # read/write symmetry: the write target IS the discovery file — repointing
    # model_config (what the autouse isolation fixture does) moves both.
    p = tmp_path / "syncmill.toml"
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", str(p))
    assert toml_path() == p


# ── load_document ──────────────────────────────────────────────────────────


def test_load_document_missing_file_is_empty(tmp_path):
    doc = load_document(tmp_path / "absent.toml")
    assert dict(doc) == {}


def test_load_document_parse_error(tmp_path):
    p = tmp_path / "syncmill.toml"
    p.write_text("not [ valid toml ===")
    with pytest.raises(TeamFileError, match="cannot parse"):
        load_document(p)


def test_load_document_non_table_teams_rejected(tmp_path):
    p = tmp_path / "syncmill.toml"
    p.write_text('teams = "oops"\n')
    with pytest.raises(TeamFileError, match="not a table"):
        load_document(p)


# ── save_team ──────────────────────────────────────────────────────────────


def test_save_team_creates_missing_file(tmp_path):
    p = tmp_path / "syncmill.toml"
    save_team(p, "python-team", _team(agents=["codex", "claude"], strategy="compete"))
    text = p.read_text()
    assert "[teams.python-team]" in text  # verbatim hyphenated table name
    reparsed = tomlkit.parse(text)
    assert reparsed["teams"]["python-team"]["agents"] == ["codex", "claude"]
    assert reparsed["teams"]["python-team"]["strategy"] == "compete"


def test_save_team_omits_unset_optionals(tmp_path):
    # strategy None / empty goals / empty capabilities never emitted — the file
    # mirrors what `team show` reports as unset/inherited.
    p = tmp_path / "syncmill.toml"
    save_team(p, "t", _team())
    text = p.read_text()
    assert "strategy" not in text
    assert "goals" not in text
    assert "capabilities" not in text


def test_save_team_capabilities_subtable_verbatim_keys(tmp_path):
    p = tmp_path / "syncmill.toml"
    save_team(
        p,
        "rust-team",
        _team(agents=["kimi-code"], capabilities={"kimi-code": ["rust", "ffi"]}),
    )
    text = p.read_text()
    assert "[teams.rust-team.capabilities]" in text
    assert tomlkit.parse(text)["teams"]["rust-team"]["capabilities"]["kimi-code"] == [
        "rust",
        "ffi",
    ]


def test_save_team_preserves_comments_elsewhere(tmp_path):
    p = tmp_path / "syncmill.toml"
    p.write_text(COMMENT_HEAVY)
    save_team(p, "new-team", _team(goals=["backend"]))
    text = p.read_text()
    # every hand-written line elsewhere survives byte-for-byte
    for line in (
        "# project baseline — hand-written comments must survive writes",
        'default_strategy = "route"  # inline comment',
        "timeout_seconds = 300  # keep me",
        'agents = ["claude"]  # sibling team, untouched',
    ):
        assert line in text
    reparsed = tomlkit.parse(text)
    assert set(reparsed["teams"]) == {"existing-team", "new-team"}


def test_save_team_replaces_wholesale(tmp_path):
    # re-saving a team replaces its table WHOLESALE — stale goals are gone
    # (a partial merge could not express "clear goals").
    p = tmp_path / "syncmill.toml"
    p.write_text(COMMENT_HEAVY)
    save_team(p, "existing-team", _team(agents=["codex"]))
    reparsed = tomlkit.parse(p.read_text())
    entry = reparsed["teams"]["existing-team"]
    assert entry["agents"] == ["codex"]
    assert "goals" not in entry


def test_save_team_roundtrips_through_config(tmp_path, monkeypatch):
    # the saved file re-loads via SyncmillConfig into the IDENTICAL TeamConfig
    team = _team(
        agents=["codex", "kimi-code"],
        strategy="council",
        goals=["backend"],
        capabilities={"kimi-code": ["rust"]},
    )
    p = tmp_path / "syncmill.toml"
    save_team(p, "python-team", team)
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", str(p))
    assert SyncmillConfig().teams["python-team"] == team


def test_save_team_atomic_on_write_failure(tmp_path, monkeypatch):
    p = tmp_path / "syncmill.toml"
    p.write_text(COMMENT_HEAVY)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(teamfile.os, "replace", boom)
    with pytest.raises(TeamFileError, match="cannot write"):
        save_team(p, "t", _team())
    assert p.read_text() == COMMENT_HEAVY  # original intact
    assert list(tmp_path.glob("*.tmp")) == []  # temp cleaned up


# ── set_default_team ───────────────────────────────────────────────────────


def test_set_default_team_inserts_before_first_table(tmp_path):
    # REGRESSION: a top-level scalar appended AFTER [teams.x] would re-parse as
    # that table's member — the upsert must land in the document root.
    p = tmp_path / "syncmill.toml"
    p.write_text('[teams.x]\nagents = ["codex"]\n')
    set_default_team(p, "x")
    reparsed = tomlkit.parse(p.read_text())
    assert reparsed["default_team"] == "x"  # top-level, not teams.x.default_team
    assert "default_team" not in reparsed["teams"]["x"]


def test_set_default_team_updates_in_place(tmp_path):
    p = tmp_path / "syncmill.toml"
    p.write_text('# keep\ndefault_team = "old"\n\n[teams.x]\nagents = ["codex"]\n')
    set_default_team(p, "x")
    text = p.read_text()
    assert "# keep" in text
    assert tomlkit.parse(text)["default_team"] == "x"


def test_set_default_team_clear(tmp_path):
    p = tmp_path / "syncmill.toml"
    p.write_text('default_team = "x"\n\n[teams.x]\nagents = ["codex"]\n')
    set_default_team(p, None)
    reparsed = tomlkit.parse(p.read_text())
    assert "default_team" not in reparsed
    assert "x" in reparsed["teams"]  # rest untouched


def test_set_default_team_clear_absent_key_no_write(tmp_path):
    p = tmp_path / "syncmill.toml"
    body = '[teams.x]\nagents = ["codex"]\n'
    p.write_text(body)
    before = p.stat().st_mtime_ns
    set_default_team(p, None)
    assert p.read_text() == body
    assert p.stat().st_mtime_ns == before  # no rewrite at all


def test_set_default_team_clear_missing_file_no_create(tmp_path):
    p = tmp_path / "syncmill.toml"
    set_default_team(p, None)
    assert not p.exists()  # the fix path never creates a file


def test_set_default_team_creates_file_when_setting(tmp_path):
    p = tmp_path / "syncmill.toml"
    set_default_team(p, "x")
    assert tomlkit.parse(p.read_text())["default_team"] == "x"


# ── build_team / validate_team_save ────────────────────────────────────────


def test_build_team_valid():
    team = build_team(
        agents=["codex"], strategy="compete", goals=["g"], capabilities={"codex": ["py"]}
    )
    assert team == _team(
        agents=["codex"], strategy="compete", goals=["g"], capabilities={"codex": ["py"]}
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"agents": []},  # min_length=1
        {"agents": ["codex", "codex"]},  # duplicates
        {"agents": ["codex"], "goals": [""]},  # empty goal
        {"agents": [""]},  # empty agent entry
    ],
)
def test_build_team_invalid(kw):
    with pytest.raises(TeamFileError, match="invalid team profile"):
        build_team(**kw)


def test_validate_team_save_ok():
    config = SyncmillConfig(teams={"a": _team(goals=["auth"])})
    validate_team_save(config, "b", _team(goals=["other"]))


def test_validate_team_save_rejects_empty_name():
    with pytest.raises(TeamFileError, match="non-empty"):
        validate_team_save(SyncmillConfig(), "", _team())


def test_validate_team_save_rejects_unknown_agent():
    with pytest.raises(TeamFileError, match="unknown agent"):
        validate_team_save(SyncmillConfig(), "t", _team(agents=["codex", "nope"]))


def test_validate_team_save_rejects_dup_goal_resolved_view():
    # the existing team may come from ANY source (init kwargs stand in for env)
    config = SyncmillConfig(teams={"a": _team(goals=["auth"])})
    with pytest.raises(TeamFileError, match="declared by two teams"):
        validate_team_save(config, "b", _team(goals=["auth"]))


def test_validate_team_save_same_name_resave_ok():
    # re-saving the team that already owns the goal substitutes, not conflicts
    config = SyncmillConfig(teams={"a": _team(goals=["auth"])})
    validate_team_save(config, "a", _team(agents=["claude"], goals=["auth"]))


def test_validate_team_save_rejects_dup_goal_file_only_view(tmp_path):
    # codex design-review Major: env overrides the file team's goals, so the
    # RESOLVED view has no conflict — but the file standing alone would. The
    # save must be rejected, else removing the env later bricks _load_config.
    p = tmp_path / "syncmill.toml"
    save_team(p, "a", _team(goals=["auth"]))
    resolved = SyncmillConfig(teams={"a": _team(goals=["overridden"])})
    with pytest.raises(TeamFileError, match="standing alone"):
        validate_team_save(resolved, "b", _team(goals=["auth"]), path=p)


def test_validate_team_save_file_only_view_same_name_resave_ok(tmp_path):
    p = tmp_path / "syncmill.toml"
    save_team(p, "a", _team(goals=["auth"]))
    resolved = SyncmillConfig(teams={"a": _team(goals=["auth"])})
    validate_team_save(resolved, "a", _team(agents=["claude"], goals=["auth"]), path=p)


def test_validate_team_save_rejects_env_masked_invalid_file_team(tmp_path):
    # codex post-code review Major: an env override makes team `a` valid in the
    # RESOLVED view, but the file has `agents=[]` — the save must be rejected,
    # else removing SYNCMILL_TEAMS bricks _load_config on the file team.
    p = tmp_path / "syncmill.toml"
    p.write_text("[teams.a]\nagents = []\n")
    resolved = SyncmillConfig(teams={"a": _team(agents=["claude"])})  # env stand-in
    with pytest.raises(TeamFileError, match="invalid standing alone"):
        validate_team_save(resolved, "b", _team(), path=p)


def test_validate_team_save_rejects_non_table_file_team(tmp_path):
    p = tmp_path / "syncmill.toml"
    p.write_text('[teams]\nweird = "not a table"\n')
    resolved = SyncmillConfig(teams={"weird": _team()})  # env stand-in
    with pytest.raises(TeamFileError, match="not a table"):
        validate_team_save(resolved, "b", _team(), path=p)


def test_validate_team_save_ok_with_valid_other_file_team(tmp_path):
    # a structurally-valid other file team doesn't block the save
    p = tmp_path / "syncmill.toml"
    save_team(p, "a", _team(agents=["claude"], goals=["frontend"]))
    validate_team_save(SyncmillConfig(), "b", _team(goals=["backend"]), path=p)


def test_validate_team_save_ignores_bare_env_for_file_teams(tmp_path, monkeypatch):
    # Nested config models must not read UNPREFIXED ambient env (GOALS/AGENTS/...)
    # — a stray `GOALS` must NOT make a
    # goal-less file team appear to own the goal and falsely block a save.
    monkeypatch.setenv("GOALS", '["auth"]')
    p = tmp_path / "syncmill.toml"
    save_team(p, "a", _team(agents=["claude"]))  # no goals in the file
    # b legitimately owns `auth` — nothing else does once env is ignored
    validate_team_save(SyncmillConfig(), "b", _team(goals=["auth"]), path=p)


def test_team_config_ignores_bare_env(monkeypatch):
    # the root fix: a nested TeamConfig has no independent env footprint
    monkeypatch.setenv("GOALS", '["auth"]')
    monkeypatch.setenv("STRATEGY", "compete")
    team = TeamConfig(agents=["claude"])
    assert team.goals == []
    assert team.strategy is None


# ── load_config_at ─────────────────────────────────────────────────────────


def test_load_config_at_reads_exact_path(tmp_path):
    # ignores model_config["toml_file"] (isolated by the autouse fixture) and
    # the cwd — the dashboard reload seam pins the file it just wrote.
    p = tmp_path / "elsewhere.toml"
    p.write_text('default_team = "x"\n\n[teams.x]\nagents = ["codex"]\n')
    c = load_config_at(p)
    assert c.default_team == "x"
    assert c.teams["x"].agents == ["codex"]


def test_load_config_at_env_still_wins(tmp_path, monkeypatch):
    p = tmp_path / "elsewhere.toml"
    p.write_text('default_strategy = "compete"\n')
    monkeypatch.setenv("SYNCMILL_DEFAULT_STRATEGY", "council")
    assert load_config_at(p).default_strategy == "council"


def test_load_config_at_does_not_mutate_global_model_config(tmp_path):
    before = SyncmillConfig.model_config.get("toml_file")
    p = tmp_path / "elsewhere.toml"
    p.write_text("")
    load_config_at(p)
    assert SyncmillConfig.model_config.get("toml_file") == before


def test_load_config_at_invalid_raises_validation_error(tmp_path):
    p = tmp_path / "elsewhere.toml"
    p.write_text('[teams.a]\nagents = []\n')
    with pytest.raises(ValidationError):
        load_config_at(p)


# ── parse_capability_specs ─────────────────────────────────────────────────


def test_parse_capability_specs_ok():
    assert parse_capability_specs(["codex=python, cli", "kimi-code=rust"]) == {
        "codex": ["python", "cli"],
        "kimi-code": ["rust"],
    }


@pytest.mark.parametrize(
    "spec, match",
    [
        ("codexpython", "expected 'agent=tok1,tok2'"),  # missing =
        ("=python", "expected 'agent=tok1,tok2'"),  # empty agent
        ("codex=", "declares no tokens"),
        ("codex= , ,", "declares no tokens"),
    ],
)
def test_parse_capability_specs_invalid(spec, match):
    with pytest.raises(TeamFileError, match=match):
        parse_capability_specs([spec])


def test_parse_capability_specs_duplicate_agent():
    with pytest.raises(TeamFileError, match="duplicate"):
        parse_capability_specs(["codex=a", "codex=b"])


def test_parse_capability_specs_empty():
    assert parse_capability_specs([]) == {}


# ── render_team ────────────────────────────────────────────────────────────


def test_render_team_statuses_and_caps():
    config = SyncmillConfig()
    config.agent_config("codex").enabled = False
    team = _team(
        agents=["claude", "codex", "ghost"],
        capabilities={"claude": ["python", "cli"]},
        goals=["backend"],
        strategy="compete",
    )
    text = render_team("demo", team, config)
    assert "Team: demo" in text
    assert "  1. claude [enabled]  capabilities: python, cli" in text
    assert "  2. codex [disabled]" in text
    assert "  3. ghost [unknown agent]" in text
    assert "strategy: compete" in text
    assert "goals:    backend" in text


def test_render_team_inherits_default_strategy():
    config = SyncmillConfig()
    text = render_team("t", _team(), config)
    assert "strategy: (inherits default_strategy: route)" in text
    assert "goals:    —" in text


# ── the write preserves the destination's mode ──────────────────────────────


def test_save_team_preserves_the_file_mode(tmp_path):
    """`syncmill.toml` is a checked-in project file.

    `NamedTemporaryFile` creates at 0600 and `os.replace` carries that onto the
    destination, so a save silently narrowed a 0644 config — a spurious mode change
    in git, and a break for group-readable checkouts."""
    import os
    import stat

    p = tmp_path / "syncmill.toml"
    p.write_text(COMMENT_HEAVY)
    os.chmod(p, 0o644)
    save_team(p, "t", _team())
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_save_team_preserves_a_restrictive_file_mode(tmp_path):
    """Preserve means preserve — a deliberately private config stays private."""
    import os
    import stat

    p = tmp_path / "syncmill.toml"
    p.write_text(COMMENT_HEAVY)
    os.chmod(p, 0o600)
    save_team(p, "t", _team())
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_save_team_creates_a_new_file_with_the_umask_mode(tmp_path):
    """A file that does not exist yet gets what a plain `open()` would give it."""
    import os
    import stat

    p = tmp_path / "syncmill.toml"
    previous = os.umask(0o022)
    try:
        save_team(p, "t", _team())
    finally:
        os.umask(previous)
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_save_team_temp_is_never_more_permissive_than_the_destination(tmp_path, monkeypatch):
    """A private config must not pass through a world-readable temp.

    Creating the temp at 0644 and narrowing it after the write leaves the contents
    readable by every local user for the length of the write."""
    import os
    import stat

    p = tmp_path / "syncmill.toml"
    p.write_text(COMMENT_HEAVY)
    os.chmod(p, 0o600)
    seen: list[int] = []
    real_open = os.open

    def tracking(path, flags, mode=0o777, *args, **kwargs):
        fd = real_open(path, flags, mode, *args, **kwargs)
        if str(path).endswith(".tmp"):
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    monkeypatch.setattr(teamfile.os, "open", tracking)
    save_team(p, "t", _team())
    assert seen and all(m & 0o077 == 0 for m in seen)  # never group/other readable


def test_save_team_cleans_up_the_temp_on_a_non_oserror(tmp_path, monkeypatch):
    """Cleanup used to hang off `except OSError`, so a KeyboardInterrupt or an
    encoding failure mid-write stranded a partial temp beside the config."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    p = workdir / "syncmill.toml"
    p.write_text(COMMENT_HEAVY)

    def boom(*args, **kwargs):
        raise KeyboardInterrupt("ctrl-c mid-write")

    monkeypatch.setattr(teamfile.os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        save_team(p, "t", _team())
    assert p.read_text() == COMMENT_HEAVY
    assert [f.name for f in workdir.iterdir()] == ["syncmill.toml"]
