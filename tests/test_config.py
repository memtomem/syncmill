"""SyncmillConfig env overrides + agent_config()."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from syncmill.config import AgentConfig, SyncmillConfig


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
