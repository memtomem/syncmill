"""Configuration system for syncmill."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    """Per-agent configuration.

    Env overrides flow through the parent ``SyncmillConfig`` prefix, e.g.
    ``SYNCMILL_CLAUDE__TIMEOUT_SECONDS``.
    """

    model_config = SettingsConfigDict(extra="ignore")

    enabled: bool = True
    timeout_seconds: int = 300
    max_turns: int = 10


class MemoryConfig(BaseSettings):
    """memtomem integration settings."""

    model_config = SettingsConfigDict(extra="ignore")

    db_path: str = "~/.memtomem/memtomem.db"


class SyncmillConfig(BaseSettings):
    """Root configuration — all fields overridable via SYNCMILL_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="SYNCMILL_",
        env_nested_delimiter="__",
    )

    agent_priority: list[str] = Field(
        default=["claude", "codex", "kimi-code"],
        description="Agent order — RouteStrategy fallback order, "
        "PipelineStrategy stage order, and CompeteStrategy winner priority",
    )
    claude: AgentConfig = Field(default_factory=AgentConfig)
    codex: AgentConfig = Field(default_factory=AgentConfig)
    kimi_code: AgentConfig = Field(default_factory=AgentConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    repo_root: str = "."
    default_strategy: str = "route"

    def agent_config(self, agent_id: str) -> AgentConfig:
        """Get config for a specific agent by id.

        Hyphenated ids (e.g. ``kimi-code``) map to their underscore field name
        (``kimi_code``) — Python identifiers can't contain hyphens, but agent
        ids can, so the env var is ``SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS``.
        """
        return getattr(self, agent_id.replace("-", "_"), AgentConfig())
