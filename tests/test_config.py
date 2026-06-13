"""SyncmillConfig env overrides + agent_config()."""

from __future__ import annotations

from syncmill.config import AgentConfig, SyncmillConfig


def test_defaults():
    c = SyncmillConfig()
    assert c.agent_priority == ["claude", "gemini", "codex"]
    assert c.default_strategy == "route"
    assert c.repo_root == "."
    assert c.memory.db_path == "~/.memtomem/memtomem.db"
    assert c.claude.enabled is True
    assert c.claude.timeout_seconds == 300
    assert c.claude.max_turns == 10


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
    assert c.agent_config("gemini") is c.gemini
    assert c.agent_config("codex") is c.codex


def test_agent_config_unknown_returns_default():
    c = SyncmillConfig()
    cfg = c.agent_config("nope")
    assert isinstance(cfg, AgentConfig)
    assert cfg.enabled is True
