"""SyncmillConfig env overrides + agent_config()."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syncmill.config import (
    AgentConfig,
    SyncmillConfig,
    apply_team,
    resolve_goal_team,
)


def test_defaults():
    c = SyncmillConfig()
    assert c.agent_priority == ["claude", "codex", "kimi-code"]
    assert c.default_strategy == "route"
    assert c.repo_root == "."
    assert c.memory.db_path == "~/.memtomem/memtomem.db"
    assert c.claude.enabled is True
    assert c.claude.timeout_seconds == 300
    assert c.claude.max_turns == 10


def test_gate_defaults_disabled():
    # no commands by default → gating off, so default behavior is unchanged
    c = SyncmillConfig()
    assert c.gate.commands == []
    assert c.gate.timeout_seconds == 300


def test_gate_env_override(monkeypatch):
    monkeypatch.setenv("SYNCMILL_GATE__COMMANDS", '["pytest -q", "ruff check ."]')
    monkeypatch.setenv("SYNCMILL_GATE__TIMEOUT_SECONDS", "120")
    c = SyncmillConfig()
    assert c.gate.commands == ["pytest -q", "ruff check ."]
    assert c.gate.timeout_seconds == 120


def test_breaker_defaults_disabled():
    # disabled by default → behavior unchanged out of the box
    c = SyncmillConfig()
    assert c.breaker.enabled is False
    assert c.breaker.failure_threshold == 3
    assert c.breaker.cooldown_seconds == 300
    assert c.breaker.state_path == "~/.syncmill/breaker.json"


def test_breaker_env_override(monkeypatch):
    monkeypatch.setenv("SYNCMILL_BREAKER__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BREAKER__FAILURE_THRESHOLD", "5")
    monkeypatch.setenv("SYNCMILL_BREAKER__COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("SYNCMILL_BREAKER__STATE_PATH", "/tmp/x.json")
    c = SyncmillConfig()
    assert c.breaker.enabled is True
    assert c.breaker.failure_threshold == 5
    assert c.breaker.cooldown_seconds == 60
    assert c.breaker.state_path == "/tmp/x.json"


def test_breaker_rejects_nonpositive_threshold(monkeypatch):
    monkeypatch.setenv("SYNCMILL_BREAKER__FAILURE_THRESHOLD", "0")
    with pytest.raises(ValidationError):
        SyncmillConfig()


def test_breaker_rejects_negative_cooldown(monkeypatch):
    monkeypatch.setenv("SYNCMILL_BREAKER__COOLDOWN_SECONDS", "-1")
    with pytest.raises(ValidationError):
        SyncmillConfig()


def test_council_defaults():
    c = SyncmillConfig()
    assert c.council.rounds == 1


def test_council_env_override(monkeypatch):
    monkeypatch.setenv("SYNCMILL_COUNCIL__ROUNDS", "3")
    c = SyncmillConfig()
    assert c.council.rounds == 3


def test_council_rejects_nonpositive_rounds(monkeypatch):
    monkeypatch.setenv("SYNCMILL_COUNCIL__ROUNDS", "0")
    with pytest.raises(ValidationError):
        SyncmillConfig()


def test_decompose_defaults():
    c = SyncmillConfig()
    assert c.decompose.max_subtasks == 8


def test_decompose_env_override(monkeypatch):
    monkeypatch.setenv("SYNCMILL_DECOMPOSE__MAX_SUBTASKS", "6")
    c = SyncmillConfig()
    assert c.decompose.max_subtasks == 6


def test_decompose_rejects_nonpositive_max_subtasks(monkeypatch):
    monkeypatch.setenv("SYNCMILL_DECOMPOSE__MAX_SUBTASKS", "0")
    with pytest.raises(ValidationError):
        SyncmillConfig()


def test_capabilities_defaults_disabled():
    # empty map by default → matcher disabled, ordering unchanged out of the box
    c = SyncmillConfig()
    assert c.capabilities.agents == {}


def test_capabilities_env_override(monkeypatch):
    # JSON object string, decoded like SYNCMILL_GATE__COMMANDS; a hyphenated agent
    # id is a verbatim key (no underscore mapping).
    monkeypatch.setenv(
        "SYNCMILL_CAPABILITIES__AGENTS",
        '{"codex": ["python", "refactor"], "kimi-code": ["rust"]}',
    )
    c = SyncmillConfig()
    assert c.capabilities.agents == {
        "codex": ["python", "refactor"],
        "kimi-code": ["rust"],
    }


def test_env_override_scalar(monkeypatch):
    monkeypatch.setenv("SYNCMILL_REPO_ROOT", "/srv/work")
    monkeypatch.setenv("SYNCMILL_DEFAULT_STRATEGY", "route")
    c = SyncmillConfig()
    assert c.repo_root == "/srv/work"


def test_env_override_priority_list(monkeypatch):
    monkeypatch.setenv("SYNCMILL_AGENT_PRIORITY", '["codex","claude"]')
    c = SyncmillConfig()
    assert c.agent_priority == ["codex", "claude"]


def test_env_override_nested_delimiter(monkeypatch):
    monkeypatch.setenv("SYNCMILL_CLAUDE__ENABLED", "false")
    monkeypatch.setenv("SYNCMILL_CLAUDE__TIMEOUT_SECONDS", "42")
    c = SyncmillConfig()
    assert c.claude.enabled is False
    assert c.claude.timeout_seconds == 42


def test_agent_config_returns_subconfig():
    c = SyncmillConfig()
    assert c.agent_config("claude") is c.claude
    assert c.agent_config("codex") is c.codex


def test_agent_config_hyphenated_id_maps_to_underscore_field():
    # `kimi-code` isn't a valid Python identifier; its config field is `kimi_code`.
    c = SyncmillConfig()
    assert c.agent_config("kimi-code") is c.kimi_code


def test_env_override_hyphenated_agent_nested(monkeypatch):
    # the env var uses the underscore field name: SYNCMILL_KIMI_CODE__...
    monkeypatch.setenv("SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS", "123")
    monkeypatch.setenv("SYNCMILL_KIMI_CODE__ENABLED", "false")
    c = SyncmillConfig()
    assert c.kimi_code.timeout_seconds == 123
    assert c.kimi_code.enabled is False


def test_agent_config_unknown_returns_default():
    c = SyncmillConfig()
    cfg = c.agent_config("nope")
    assert isinstance(cfg, AgentConfig)
    assert cfg.enabled is True


# ── syncmill.toml (#11) ────────────────────────────────────────────────────


def _use_toml(tmp_path, monkeypatch, body: str):
    """Write a syncmill.toml and point the config's toml source straight at it
    (absolute path), overriding the autouse ``isolate_toml_discovery`` default.

    This exercises ``TomlConfigSettingsSource`` with no cwd dependence — the real
    cwd-relative *discovery* of the shipped ``toml_file="syncmill.toml"`` is
    covered separately by ``test_toml_discovered_relative_to_cwd``."""
    p = tmp_path / "syncmill.toml"
    p.write_text(body)
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", str(p))
    return p


def test_toml_absent_uses_defaults():
    # no file present (autouse isolate_toml_discovery points at an absent path):
    # must not crash and yields plain defaults
    c = SyncmillConfig()
    assert c.default_strategy == "route"
    assert c.repo_root == "."
    assert c.agent_priority == ["claude", "codex", "kimi-code"]


def test_toml_discovered_relative_to_cwd(tmp_path, monkeypatch):
    # the shipped default is cwd-relative discovery of "syncmill.toml"; prove that
    # real path works (the other tests point toml_file at an absolute file).
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")
    (tmp_path / "syncmill.toml").write_text('default_strategy = "compete"\n')
    monkeypatch.chdir(tmp_path)
    assert SyncmillConfig().default_strategy == "compete"


def test_toml_loaded(tmp_path, monkeypatch):
    _use_toml(
        tmp_path,
        monkeypatch,
        """
agent_priority = ["codex", "claude"]
default_strategy = "compete"
repo_root = "/srv/project"

[claude]
enabled = false
timeout_seconds = 600

[gate]
commands = ["pytest -q", "ruff check ."]

[breaker]
enabled = true
failure_threshold = 5
cooldown_seconds = 60

[council]
rounds = 2

[capabilities.agents]
codex = ["python", "refactor"]
kimi-code = ["rust"]
""",
    )
    c = SyncmillConfig()
    assert c.agent_priority == ["codex", "claude"]
    assert c.default_strategy == "compete"
    assert c.repo_root == "/srv/project"
    assert c.claude.enabled is False
    assert c.claude.timeout_seconds == 600
    assert c.gate.commands == ["pytest -q", "ruff check ."]
    assert c.council.rounds == 2
    # breaker is the only sub-config with field validators — confirm it loads
    assert c.breaker.enabled is True
    assert c.breaker.failure_threshold == 5
    assert c.breaker.cooldown_seconds == 60
    # capabilities are keyed by the REAL agent id — hyphen kept verbatim
    assert c.capabilities.agents == {
        "codex": ["python", "refactor"],
        "kimi-code": ["rust"],
    }


def test_toml_breaker_invalid_rejected(tmp_path, monkeypatch):
    # a constrained field (failure_threshold ge=1) is validated through the toml
    # source too, not just the env layer
    _use_toml(tmp_path, monkeypatch, "[breaker]\nfailure_threshold = 0\n")
    with pytest.raises(ValidationError):
        SyncmillConfig()


def test_env_overrides_toml(tmp_path, monkeypatch):
    # the documented invariant: env > file. And nested tables deep-merge across
    # sources, so overriding ONE nested field via env leaves the rest from the
    # file intact (claude.enabled survives while timeout comes from env).
    _use_toml(
        tmp_path,
        monkeypatch,
        """
default_strategy = "compete"

[claude]
enabled = false
timeout_seconds = 600
""",
    )
    monkeypatch.setenv("SYNCMILL_DEFAULT_STRATEGY", "route")
    monkeypatch.setenv("SYNCMILL_CLAUDE__TIMEOUT_SECONDS", "42")
    c = SyncmillConfig()
    assert c.default_strategy == "route"  # env wins over file
    assert c.claude.timeout_seconds == 42  # env wins over file
    assert c.claude.enabled is False  # untouched file value deep-merges through


def test_env_capabilities_deep_merge_with_toml(tmp_path, monkeypatch):
    # the capability MAP also deep-merges across sources: a partial env override
    # adds/overrides keys rather than replacing the whole toml map. Pin this so a
    # future pydantic-settings change to merge-vs-replace can't silently reroute
    # capability matching.
    _use_toml(
        tmp_path,
        monkeypatch,
        """
[capabilities.agents]
codex = ["python"]
kimi-code = ["rust"]
""",
    )
    monkeypatch.setenv(
        "SYNCMILL_CAPABILITIES__AGENTS", '{"codex": ["go"], "claude": ["docs"]}'
    )
    c = SyncmillConfig()
    assert c.capabilities.agents == {
        "codex": ["go"],  # env overrides this key
        "kimi-code": ["rust"],  # file-only key survives the merge
        "claude": ["docs"],  # env-only key added
    }


def test_init_kwarg_overrides_toml(tmp_path, monkeypatch):
    # explicit constructor kwargs (programmatic use / tests) sit above both env
    # and the file. The CLI does NOT use this path — it mutates the built config
    # post-construction (see test_cli.py) — but the precedence still holds.
    _use_toml(tmp_path, monkeypatch, 'default_strategy = "compete"\n')
    c = SyncmillConfig(default_strategy="pipeline")
    assert c.default_strategy == "pipeline"


def test_toml_unknown_toplevel_key_rejected(tmp_path, monkeypatch):
    # a typo'd top-level key fails loudly rather than being silently ignored
    _use_toml(tmp_path, monkeypatch, 'defualt_strategy = "compete"\n')
    with pytest.raises(ValidationError):
        SyncmillConfig()


def test_toml_kimi_code_table_uses_underscore_field(tmp_path, monkeypatch):
    # per-agent table is keyed by the FIELD name (underscore), mirroring the
    # SYNCMILL_KIMI_CODE__ env prefix — not the hyphenated agent id.
    _use_toml(
        tmp_path,
        monkeypatch,
        """
[kimi_code]
timeout_seconds = 123
enabled = false
""",
    )
    c = SyncmillConfig()
    assert c.kimi_code.timeout_seconds == 123
    assert c.kimi_code.enabled is False


# ── teams (Phase 4) ────────────────────────────────────────────────────────


def test_teams_defaults_empty():
    # no teams by default → nothing selectable, behavior unchanged out of the box
    c = SyncmillConfig()
    assert c.teams == {}


def test_teams_toml_loaded(tmp_path, monkeypatch):
    # [teams.<name>] table name is the VERBATIM team name (hyphens kept, like
    # [capabilities.agents] keys); capabilities inside are keyed by the real
    # agent id too.
    _use_toml(
        tmp_path,
        monkeypatch,
        """
[teams.python-team]
agents = ["codex", "claude"]
strategy = "compete"
goals = ["backend", "api"]

[teams.python-team.capabilities]
codex = ["python"]
kimi-code = ["rust"]
""",
    )
    c = SyncmillConfig()
    team = c.teams["python-team"]
    assert team.agents == ["codex", "claude"]  # order preserved
    assert team.strategy == "compete"
    assert team.goals == ["backend", "api"]
    assert team.capabilities == {"codex": ["python"], "kimi-code": ["rust"]}


def test_teams_optional_fields_default(tmp_path, monkeypatch):
    _use_toml(tmp_path, monkeypatch, '[teams.t]\nagents = ["claude"]\n')
    team = SyncmillConfig().teams["t"]
    assert team.capabilities == {}
    assert team.strategy is None
    assert team.goals == []


def test_teams_env_override(monkeypatch):
    # dict root field → JSON object env form (like SYNCMILL_CAPABILITIES__AGENTS)
    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        '{"rust-team": {"agents": ["kimi-code"], "goals": ["porting"]}}',
    )
    c = SyncmillConfig()
    assert c.teams["rust-team"].agents == ["kimi-code"]
    assert c.teams["rust-team"].goals == ["porting"]


def test_teams_env_deep_merge_with_toml(tmp_path, monkeypatch):
    # env teams deep-merge with toml teams: an env-only team is added while the
    # file-only team survives (the capabilities-map merge precedent).
    _use_toml(tmp_path, monkeypatch, '[teams.python-team]\nagents = ["codex"]\n')
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"rust-team": {"agents": ["kimi-code"]}}')
    c = SyncmillConfig()
    assert set(c.teams) == {"python-team", "rust-team"}


def test_teams_unknown_subkey_ignored(tmp_path, monkeypatch):
    # sub-tables stay lenient (extra="ignore"), like every other nested table
    _use_toml(
        tmp_path, monkeypatch, '[teams.t]\nagents = ["claude"]\nnot_a_field = 1\n'
    )
    assert SyncmillConfig().teams["t"].agents == ["claude"]


@pytest.mark.parametrize(
    "teams_json",
    [
        '{"t": {"agents": []}}',  # empty agents
        '{"t": {}}',  # missing agents
        '{"t": {"agents": ["claude", "claude"]}}',  # duplicate agent
        '{"t": {"agents": [""]}}',  # empty agent string
        '{"t": {"agents": ["claude"], "goals": [""]}}',  # empty goal string
        '{"t": {"agents": ["claude"], "goals": ["g", "g"]}}',  # duplicate goal
        '{"": {"agents": ["claude"]}}',  # empty team name
    ],
)
def test_teams_structurally_invalid_rejected(monkeypatch, teams_json):
    monkeypatch.setenv("SYNCMILL_TEAMS", teams_json)
    with pytest.raises(ValidationError):
        SyncmillConfig()


def test_teams_duplicate_goal_across_teams_rejected(monkeypatch):
    # a goal owned by two teams would make --goal team selection depend on dict
    # insertion order — rejected hard, naming both teams and the goal
    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        '{"a-team": {"agents": ["claude"], "goals": ["auth"]},'
        ' "b-team": {"agents": ["codex"], "goals": ["auth"]}}',
    )
    with pytest.raises(ValidationError) as exc_info:
        SyncmillConfig()
    msg = str(exc_info.value)
    assert "a-team" in msg and "b-team" in msg and "auth" in msg


def test_apply_team_overlays_all_three_fields(monkeypatch):
    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        '{"t": {"agents": ["kimi-code", "codex"], "strategy": "compete",'
        ' "capabilities": {"kimi-code": ["rust"]}}}',
    )
    c = SyncmillConfig()
    team = apply_team(c, "t")
    assert c.agent_priority == ["kimi-code", "codex"]
    assert c.capabilities.agents == {"kimi-code": ["rust"]}
    assert c.default_strategy == "compete"
    assert team is c.teams["t"]


def test_apply_team_unset_fields_keep_globals(monkeypatch):
    # a profile without strategy/capabilities keeps the resolved global values
    monkeypatch.setenv("SYNCMILL_DEFAULT_STRATEGY", "pipeline")
    monkeypatch.setenv(
        "SYNCMILL_CAPABILITIES__AGENTS", '{"codex": ["python"]}'
    )
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"t": {"agents": ["codex"]}}')
    c = SyncmillConfig()
    apply_team(c, "t")
    assert c.agent_priority == ["codex"]
    assert c.default_strategy == "pipeline"  # kept
    assert c.capabilities.agents == {"codex": ["python"]}  # kept


def test_apply_team_unknown_name_raises_keyerror():
    with pytest.raises(KeyError):
        apply_team(SyncmillConfig(), "nope")


def test_resolve_goal_team(monkeypatch):
    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        '{"t": {"agents": ["claude"], "goals": ["auth", "billing"]}}',
    )
    c = SyncmillConfig()
    assert resolve_goal_team(c, "auth") == "t"
    assert resolve_goal_team(c, "billing") == "t"
    assert resolve_goal_team(c, "other") is None


def test_resolve_goal_team_no_teams():
    assert resolve_goal_team(SyncmillConfig(), "auth") is None


# ── default_team (P3 TEAM pane) ────────────────────────────────────────────


def test_default_team_defaults_none():
    assert SyncmillConfig().default_team is None


def test_default_team_toml_loaded(tmp_path, monkeypatch):
    _use_toml(
        tmp_path,
        monkeypatch,
        'default_team = "python-team"\n\n[teams.python-team]\nagents = ["codex"]\n',
    )
    assert SyncmillConfig().default_team == "python-team"


def test_default_team_env_beats_toml(tmp_path, monkeypatch):
    _use_toml(tmp_path, monkeypatch, 'default_team = "file-team"\n')
    monkeypatch.setenv("SYNCMILL_DEFAULT_TEAM", "env-team")
    assert SyncmillConfig().default_team == "env-team"


def test_default_team_dangling_does_not_fail_construction():
    # deliberately NOT validated against `teams` at construction — a dangling
    # value must not brick every command including its own fix
    # (`team use --clear`); the apply sites raise the clean error instead.
    c = SyncmillConfig(default_team="no-such-team")
    assert c.default_team == "no-such-team"
    assert c.teams == {}


# ── monitor (#16) ──────────────────────────────────────────────────────────


def test_monitor_defaults_disabled():
    c = SyncmillConfig()
    assert c.monitor.enabled is False
    assert c.monitor.session_prefix == "syncmill"


def test_monitor_env_override(monkeypatch):
    monkeypatch.setenv("SYNCMILL_MONITOR__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_MONITOR__SESSION_PREFIX", "mill")
    c = SyncmillConfig()
    assert c.monitor.enabled is True
    assert c.monitor.session_prefix == "mill"


# ── [integration.report] (S3 envelope writer) ─────────────────────────────────


def test_report_defaults_disabled():
    # disabled by default → no filesystem touch, behavior unchanged out of the box
    c = SyncmillConfig()
    assert c.integration.report.enabled is False
    assert c.integration.report.runs_dir == "~/.syncmill/runs"
    assert c.integration.report.keep_last == 50
    assert c.applied_team is None


def test_report_env_override(monkeypatch):
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", "/tmp/runs")
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__KEEP_LAST", "5")
    c = SyncmillConfig()
    assert c.integration.report.enabled is True
    assert c.integration.report.runs_dir == "/tmp/runs"
    assert c.integration.report.keep_last == 5


def test_report_toml_table(tmp_path, monkeypatch):
    _use_toml(
        tmp_path,
        monkeypatch,
        "[integration.report]\nenabled = true\nkeep_last = 7\n",
    )
    c = SyncmillConfig()
    assert c.integration.report.enabled is True
    assert c.integration.report.keep_last == 7
    assert c.integration.report.runs_dir == "~/.syncmill/runs"  # deep-merge


def test_report_env_beats_toml(tmp_path, monkeypatch):
    _use_toml(tmp_path, monkeypatch, "[integration.report]\nkeep_last = 7\n")
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__KEEP_LAST", "3")
    assert SyncmillConfig().integration.report.keep_last == 3


def test_report_rejects_nonpositive_keep_last(monkeypatch):
    # keep_last counts the current run, which always survives — 0 is meaningless.
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__KEEP_LAST", "0")
    with pytest.raises(ValidationError):
        SyncmillConfig()


def test_apply_team_stamps_applied_team():
    c = SyncmillConfig(
        teams={"python-team": {"agents": ["codex", "claude"]}},
    )
    assert c.applied_team is None
    apply_team(c, "python-team")
    assert c.applied_team == "python-team"


def test_applied_team_env_and_toml_are_scrubbed(tmp_path, monkeypatch):
    # codex review (S3): applied_team is runtime-only — a stray env var or toml
    # key must never forge a team name into the run-envelope's team field.
    monkeypatch.setenv("SYNCMILL_APPLIED_TEAM", "forged")
    assert SyncmillConfig().applied_team is None
    monkeypatch.delenv("SYNCMILL_APPLIED_TEAM")
    _use_toml(tmp_path, monkeypatch, 'applied_team = "forged"\n')
    assert SyncmillConfig().applied_team is None
