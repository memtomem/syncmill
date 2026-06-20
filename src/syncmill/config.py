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


class BreakerConfig(BaseSettings):
    """Circuit-breaker settings — reactive back-off from repeatedly-failing
    agents (#8).

    DISABLED by default (``enabled=False``): ``build_breaker`` returns ``None``
    and strategies skip breaker logic entirely, so out-of-the-box behavior is
    unchanged. Enable + tune via env::

        SYNCMILL_BREAKER__ENABLED=true
        SYNCMILL_BREAKER__FAILURE_THRESHOLD=3
        SYNCMILL_BREAKER__COOLDOWN_SECONDS=300
        SYNCMILL_BREAKER__STATE_PATH=~/.syncmill/breaker.json

    State persists to ``state_path`` (JSON) so consecutive failures accumulate
    *across* separate ``syncmill run`` invocations — the only way "fails N times
    in a row" is observable, since each run executes an agent at most once.
    """

    model_config = SettingsConfigDict(extra="ignore")

    enabled: bool = False
    failure_threshold: int = Field(default=3, ge=1)
    cooldown_seconds: int = Field(default=300, ge=0)
    state_path: str = "~/.syncmill/breaker.json"


class GateConfig(BaseSettings):
    """Quality-gate settings — commands run against an agent's worktree before
    its result is accepted (#10).

    Each entry in ``commands`` is a single command (parsed with ``shlex.split``,
    run without a shell), so chain checks by adding more entries rather than with
    ``&&``. An empty ``commands`` (the default) **disables** gating, so default
    behavior is unchanged. Override via env, e.g.::

        SYNCMILL_GATE__COMMANDS='["pytest -q", "ruff check ."]'
        SYNCMILL_GATE__TIMEOUT_SECONDS=120
    """

    model_config = SettingsConfigDict(extra="ignore")

    commands: list[str] = Field(default_factory=list)
    timeout_seconds: int = 300


class CapabilityConfig(BaseSettings):
    """Declarative per-agent capability descriptors — capability matching (#9).

    DISABLED by default: ``agents`` is an empty map, so ``build_matcher`` returns
    ``None`` and agent ordering stays at raw priority — behavior is unchanged out
    of the box (mirrors ``GateConfig``/``BreakerConfig``).

    ``agents`` maps an agent id to the capability tokens it declares (a language,
    a task type, …). A task carrying matching tokens in its ``Task.tags`` routes a
    matching agent *ahead of* raw ``agent_priority``; nothing matching falls back
    to priority order. Purely DECLARATIVE — config only, never a live probe of the
    binary (respects "no ``health_check``"). The map is keyed by the **real agent
    id**, so a hyphenated id like ``kimi-code`` is a verbatim key (unlike the
    per-agent ``AgentConfig`` fields, a dict key has no identifier restriction).
    Override via env (a JSON object), e.g.::

        SYNCMILL_CAPABILITIES__AGENTS='{"codex": ["python", "refactor"], "kimi-code": ["rust"]}'
    """

    model_config = SettingsConfigDict(extra="ignore")

    agents: dict[str, list[str]] = Field(default_factory=dict)


class SyncmillConfig(BaseSettings):
    """Root configuration — all fields overridable via SYNCMILL_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="SYNCMILL_",
        env_nested_delimiter="__",
    )

    agent_priority: list[str] = Field(
        default=["claude", "codex", "kimi-code"],
        description="Agent order — RouteStrategy fallback order, "
        "PipelineStrategy stage order, and CompeteStrategy winner priority. "
        "Capability matching (#9) re-orders agents OVER this baseline per task; "
        "with no capability hint (or no match) the order is exactly this priority.",
    )
    claude: AgentConfig = Field(default_factory=AgentConfig)
    codex: AgentConfig = Field(default_factory=AgentConfig)
    kimi_code: AgentConfig = Field(default_factory=AgentConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    gate: GateConfig = Field(default_factory=GateConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    capabilities: CapabilityConfig = Field(default_factory=CapabilityConfig)
    repo_root: str = "."
    default_strategy: str = "route"

    def agent_config(self, agent_id: str) -> AgentConfig:
        """Get config for a specific agent by id.

        Hyphenated ids (e.g. ``kimi-code``) map to their underscore field name
        (``kimi_code``) — Python identifiers can't contain hyphens, but agent
        ids can, so the env var is ``SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS``.
        """
        return getattr(self, agent_id.replace("-", "_"), AgentConfig())
