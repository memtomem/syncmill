"""Configuration system for syncmill."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


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


class CouncilConfig(BaseSettings):
    """CouncilStrategy settings — multi-round deliberation (#13).

    The council runs three kinds of phase: a concurrent **propose** phase (every
    agent drafts a solution in its own worktree), ``rounds`` concurrent
    **critique** phases (each member refines its solution after reading the
    council's proposals + prior critiques from ``shared``), and a concurrent
    **synthesize** phase (every member produces a final solution from the whole
    deliberation, and the winner is picked by the same priority/gate rule as
    Compete). ``rounds`` counts only the critique phases, so the total phases per
    council = propose(1) + critique(``rounds``) + synthesize(1): the default
    ``rounds=1`` is a genuine multi-round council (propose → critique → synthesize).

    There is nothing to disable — ``council`` is only consulted when the council
    strategy is selected (``default_strategy="council"`` / ``--strategy council``),
    so the defaults are inert for the other strategies. Override via env, e.g.::

        SYNCMILL_COUNCIL__ROUNDS=2
    """

    model_config = SettingsConfigDict(extra="ignore")

    rounds: int = Field(
        default=1,
        ge=1,
        description="Number of critique rounds between propose and synthesize "
        "(>=1). Total phases per council = 1 propose + rounds critique + 1 synthesize.",
    )


class DecomposeConfig(BaseSettings):
    """DecomposeStrategy settings — task decomposition (#14).

    ``max_subtasks`` caps how many subtasks the planner's breakdown may contain:
    the planner is asked to stay within it and ``_parse_subtasks`` truncates any
    overflow (logging what it drops — no silent cap). It bounds the fan-out cost,
    since N subtasks become N concurrent subtask runs plus one synthesis run per
    runnable agent.

    There is nothing to disable — ``decompose`` is only consulted when the decompose
    strategy is selected (``default_strategy="decompose"`` / ``--strategy
    decompose``), so the defaults are inert for the other strategies. Override via
    env, e.g.::

        SYNCMILL_DECOMPOSE__MAX_SUBTASKS=6
    """

    model_config = SettingsConfigDict(extra="ignore")

    max_subtasks: int = Field(
        default=8,
        ge=1,
        description="Maximum number of subtasks the planner may produce (>=1); the "
        "breakdown is truncated to this many.",
    )


class BoardConfig(BaseSettings):
    """Persistent work-board settings (Phase 1).

    DISABLED by default (``enabled=False``): ``build_board`` returns ``None`` and
    the board CLI errors with a clear "enable it" message, so plain ``syncmill
    run`` and every existing command are unchanged out of the box (mirrors
    ``BreakerConfig``).

    Unlike memory/breaker, the store is **LOAD-BEARING** when enabled (the source
    of work + system of record), so it does NOT degrade silently — a corrupt db
    raises ``BoardError``. The Supervisor builds it LAZILY (not in ``__init__``),
    so even an enabled-but-corrupt board never breaks a plain ``run`` that never
    touches it. Enable + tune via env::

        SYNCMILL_BOARD__ENABLED=true
        SYNCMILL_BOARD__DB_PATH=~/.syncmill/board.db
        SYNCMILL_BOARD__LEASE_TIMEOUT_SECONDS=1800

    ``lease_timeout_seconds`` bounds how long a claimed (``running``) item may sit
    before ``recover_stale_running`` reclaims it to ``planned`` — the SIGKILL /
    crashed-run recovery analogue of ``cleanup_orphaned_worktrees``.

    ``stall_seconds`` is the aging window for ``board stats`` (Phase 2): a blocked /
    human-required item, or an item whose handoff baton was never picked up, counts
    as a *stall* once it has sat untouched longer than this. Tunable via
    ``SYNCMILL_BOARD__STALL_SECONDS`` (it only affects the read-only stats report,
    never dispatch).
    """

    model_config = SettingsConfigDict(extra="ignore")

    enabled: bool = False
    db_path: str = "~/.syncmill/board.db"
    lease_timeout_seconds: int = Field(default=1800, ge=1)
    stall_seconds: int = Field(default=3600, ge=1)


class MonitorConfig(BaseSettings):
    """tmux live-monitoring settings — a live view of a concurrent run (#16).

    DISABLED by default (``enabled=False``): the CLI's ``--monitor`` flag flips it
    on for a single run (set post-construction, like ``--strategy``). When on AND
    the selected strategy is concurrent (compete/council/decompose) AND stdout is a
    TTY AND tmux is installed, the run is launched inside a tmux session with one
    live pane per agent; otherwise it falls through to a normal run with a notice.
    Out of the box nothing changes. Override via env, e.g.::

        SYNCMILL_MONITOR__ENABLED=true
        SYNCMILL_MONITOR__SESSION_PREFIX=syncmill

    Note: the internal outer→inner handshake uses the ``SYNCMILL_MONITOR_DIR`` env
    var + a sentinel file (see ``monitor.inner_dir``); that is launcher-set IPC, NOT
    user config, and deliberately sits outside this ``SYNCMILL_MONITOR__*`` table.
    """

    model_config = SettingsConfigDict(extra="ignore")

    enabled: bool = False
    session_prefix: str = "syncmill"


class SyncmillConfig(BaseSettings):
    """Root configuration, resolved from three layers (highest priority first):

    1. ``SYNCMILL_*`` environment variables (nested delimiter ``__``).
    2. A project ``syncmill.toml`` in the current working directory.
    3. Field defaults.

    Env always wins over the file and the file over defaults, so **every field
    stays env-overridable** (the long-standing invariant) while a checked-in
    ``syncmill.toml`` gives a project baseline. Explicit constructor kwargs sit
    above all three (the programmatic/test path). The CLI flags ``--repo`` /
    ``--strategy`` / ``--timeout`` don't use that init source — they mutate the
    built config post-construction (only when passed), which likewise overrides
    every source. Unknown *top-level* keys in the file are rejected (typo
    protection); per-agent tables (``extra="ignore"``) are lenient.

    TOML mirrors the env naming exactly: per-agent tables use the **field** name
    (``[kimi_code]``, underscored — like ``SYNCMILL_KIMI_CODE__…``), while
    ``[capabilities.agents]`` is keyed by the **real agent id** (``kimi-code``,
    verbatim — like the ``SYNCMILL_CAPABILITIES__AGENTS`` JSON).
    """

    model_config = SettingsConfigDict(
        env_prefix="SYNCMILL_",
        env_nested_delimiter="__",
        toml_file="syncmill.toml",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Slot ``syncmill.toml`` below env vars and above field defaults.

        The returned tuple is precedence order (first = highest): explicit init
        kwargs, then env, then the toml file, then defaults — keeping the
        env > file > defaults invariant without dropping any default source.
        Nested values deep-merge across sources, so an env override of a single
        nested field (e.g. ``SYNCMILL_CLAUDE__TIMEOUT_SECONDS``) leaves the rest
        of that table from the file intact.
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    agent_priority: list[str] = Field(
        default=["claude", "codex", "kimi-code"],
        description="Agent order — RouteStrategy fallback order, "
        "PipelineStrategy stage order, CompeteStrategy winner priority, "
        "CouncilStrategy member/synthesis-winner priority, and DecomposeStrategy "
        "planner + subtask round-robin + synthesis-winner priority. "
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
    council: CouncilConfig = Field(default_factory=CouncilConfig)
    decompose: DecomposeConfig = Field(default_factory=DecomposeConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    board: BoardConfig = Field(default_factory=BoardConfig)
    repo_root: str = "."
    default_strategy: str = "route"

    def agent_config(self, agent_id: str) -> AgentConfig:
        """Get config for a specific agent by id.

        Hyphenated ids (e.g. ``kimi-code``) map to their underscore field name
        (``kimi_code``) — Python identifiers can't contain hyphens, but agent
        ids can, so the env var is ``SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS``.
        """
        return getattr(self, agent_id.replace("-", "_"), AgentConfig())
