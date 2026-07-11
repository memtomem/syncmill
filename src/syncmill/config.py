"""Configuration system for syncmill."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field, field_validator, model_validator
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


class TeamConfig(BaseSettings):
    """A named team profile — an ordered agent subset + capability/strategy
    bundle, selectable per run (Phase 4 of the agent-team platform).

    Declared as ``[teams.<name>]`` tables in ``syncmill.toml``; the table name is
    the team name and, like ``[capabilities.agents]`` keys, is a **verbatim dict
    key** (hyphens allowed — no identifier restriction). The env path is the JSON
    object form (dict fields don't ride the ``__`` nested delimiter)::

        SYNCMILL_TEAMS='{"python-team": {"agents": ["codex", "claude"],
                          "strategy": "compete", "goals": ["backend"]}}'

    env teams deep-merge with toml teams like every other nested table.

    Selecting a team (``run --team`` / ``board run --team`` / a ``board run
    --goal`` whose goal a team declares) overlays the profile onto the resolved
    config **before** the ``Supervisor`` is constructed: ``agents`` replaces
    ``agent_priority`` (subset + order), a non-empty ``capabilities`` replaces
    ``capabilities.agents``, and a set ``strategy`` replaces ``default_strategy``
    (an explicit ``--strategy`` flag still wins). The registry, matcher, and
    strategy all derive from those fields in ``Supervisor.__init__``, so the
    whole stack follows with no strategy/agent-layer changes.
    """

    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Init-only: a team is populated by the PARENT ``SyncmillConfig`` (the
        ``SYNCMILL_TEAMS`` JSON env or the ``[teams.<name>]`` toml table), which
        passes the assembled dict in via init. Without this, ``TeamConfig``
        being a ``BaseSettings`` subclass would ALSO read UNPREFIXED ambient env
        — ``GOALS``/``AGENTS``/``STRATEGY``/``CAPABILITIES`` — so a stray shell
        ``GOALS='["auth"]'`` would silently reconfigure every team's goals (a
        codex review Major; the same latent footgun applies to the other nested
        settings models, but teams' field names are common English words, so it
        actually bites). Dropping every source but ``init_settings`` makes a team
        purely what its parent handed it — no independent env footprint."""
        return (init_settings,)

    agents: list[str] = Field(
        min_length=1,
        description="Ordered agent subset — becomes this team's agent_priority "
        "(required, at least one agent).",
    )
    capabilities: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Team-scoped capability map (same shape/keys as "
        "[capabilities.agents]); empty keeps the global declaration.",
    )
    strategy: str | None = Field(
        default=None,
        description="Team default strategy; unset keeps the resolved "
        "default_strategy. An explicit --strategy flag beats it.",
    )
    goals: list[str] = Field(
        default_factory=list,
        description="Board goals this team owns — `board run --goal <g>` "
        "auto-selects the team declaring g. A goal may belong to at most one team.",
    )

    @field_validator("agents", "goals")
    @classmethod
    def _no_empty_or_duplicate_entries(cls, value: list[str]) -> list[str]:
        """Reject empty-string and duplicate entries.

        An empty goal ``""`` is the un-goaled board default — a team declaring it
        would silently own every goal-less item, so it is rejected outright.
        """
        if any(not entry for entry in value):
            raise ValueError("entries must be non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("entries must be unique")
        return value


def check_team_goals(teams: Mapping[str, TeamConfig]) -> None:
    """Reject a structurally broken ``teams`` map (raises ``ValueError``).

    An empty team name is meaningless (nothing to select by), and a goal
    declared by two teams would make ``board run --goal`` team selection depend
    on dict insertion order — both are config typos, rejected hard (the
    ``_make_strategy`` hard-fail house style, never first-wins). Shared by the
    ``SyncmillConfig`` model validator and ``teamfile.validate_team_save`` (the
    pre-write dup-goal check for ``team save``) — both pass fully-constructed
    ``TeamConfig`` values, so the rule can't drift between the two.
    """
    goal_owner: dict[str, str] = {}
    for name, team in teams.items():
        if not name:
            raise ValueError("team name must be a non-empty string")
        for goal in team.goals:
            other = goal_owner.get(goal)
            if other is not None:
                raise ValueError(
                    f"goal {goal!r} is declared by two teams: "
                    f"{other!r} and {name!r} — a goal may belong to at "
                    f"most one team"
                )
            goal_owner[goal] = name


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


class ReportConfig(BaseSettings):
    """Run-report (envelope) settings — the ecosystem join artifact (S3).

    DISABLED by default (``enabled=False``): ``build_reporter`` returns ``None``
    and the run touches no filesystem, so out of the box every run is
    byte-identical to before (mirrors ``BreakerConfig``/``BoardConfig``). When
    enabled, every run writes ``{runs_dir}/{run_id}/envelope.json`` (plus a
    ``result.json`` copy) per ``contracts/run-envelope.schema.json`` —
    best-effort/degrade like memory: a report failure warns and the run,
    result output, and cleanup continue unchanged. Enable + tune via env::

        SYNCMILL_INTEGRATION__REPORT__ENABLED=true
        SYNCMILL_INTEGRATION__REPORT__RUNS_DIR=~/.syncmill/runs
        SYNCMILL_INTEGRATION__REPORT__KEEP_LAST=50

    ``keep_last`` bounds the retention sweep at run start: the oldest run
    directories beyond this many are removed (the current run always survives
    and counts toward the total, so ``0`` would be meaningless — hence ``ge=1``).
    """

    model_config = SettingsConfigDict(extra="ignore")

    enabled: bool = False
    runs_dir: str = "~/.syncmill/runs"
    keep_last: int = Field(default=50, ge=1)


class TraceConfig(BaseSettings):
    """Trace-exporter settings — OTLP-JSON spans for tracegraph (S3′).

    DISABLED by default (``enabled=False``): ``build_tracer`` returns ``None``
    and the run is byte-identical to before (the ``build_gate``/``build_breaker``
    pattern). When enabled, each run buffers span records and atomically writes
    ``{dir}/{run_id}/trace.otlp.json`` — an OTLP protobuf-JSON document
    tracegraph ingests as-is (``ingest-otlp``). Best-effort like memory: an
    exporter failure warns and the run, result, and cleanup continue. Enable via::

        SYNCMILL_INTEGRATION__TRACE__ENABLED=true

    ``exporter`` is ``"file"`` in v1 — ``"otlp-http"`` is a reserved option
    (tracegraph has no HTTP receiver yet); selecting it warns and falls back to
    the file exporter. ``dir`` empty means "the report ``runs_dir``" so the
    trace lands beside the run envelope. ``include_output`` is accepted but
    warn+ignored in v1 — spans are body-free by contract (digests/counts only).
    """

    model_config = SettingsConfigDict(extra="ignore")

    enabled: bool = False
    exporter: str = "file"
    dir: str = ""
    include_output: bool = False


class ToolgraphConfig(BaseSettings):
    """toolgraph advisory-preflight settings (S5).

    DISABLED by default (``enabled=False``): ``run_preflight`` returns ``None``
    and no subprocess runs, so behavior is byte-identical to before. When
    enabled, the Supervisor runs ``toolgraph preflight`` once (for the lead
    matched agent) BEFORE dispatching the strategy — advisory only, so any
    failure (binary absent, timeout, non-zero exit, bad JSON, unknown major)
    is a yellow warning and the run CONTINUES; the outcome is recorded in the
    envelope ``preflight`` block. Enable + tune via env::

        SYNCMILL_INTEGRATION__TOOLGRAPH__ENABLED=true
        SYNCMILL_INTEGRATION__TOOLGRAPH__PROFILE=review
        SYNCMILL_INTEGRATION__TOOLGRAPH__AGENT_MAP='{"codex": "codex", "kimi-code": "kimi"}'

    ``mode`` is ``"advisory"`` only in v1 — ``"strict"`` (enforcement) is
    reserved for P3.1 and rejected with a clear error rather than silently
    downgraded (the ``_make_strategy`` hard-fail house style). ``agent_map``
    maps a syncmill agent id to a toolgraph ``Agent.id``, keyed by the **real**
    agent id (``kimi-code`` verbatim — the ``[capabilities.agents]`` rule);
    ``command`` is the invocation seam tests override. ``candidate_tools`` are
    server-qualified ``server::tool`` keys (the ambient MCP surface — memtomem,
    the ``serve`` gateway — an operator wants policy-checked).
    """

    model_config = SettingsConfigDict(extra="ignore")

    enabled: bool = False
    mode: str = "advisory"
    profile: str = "review"
    timeout_seconds: int = Field(default=5, ge=1)
    command: list[str] = Field(default_factory=lambda: ["toolgraph", "preflight"])
    candidate_tools: list[str] = Field(default_factory=list)
    agent_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("mode")
    @classmethod
    def _reject_strict(cls, value: str) -> str:
        if value != "advisory":
            raise ValueError(
                f"toolgraph mode {value!r} is not supported in v1 — only "
                "'advisory' is available ('strict' enforcement is reserved for "
                "a later stage)"
            )
        return value

    @field_validator("profile")
    @classmethod
    def _known_profile(cls, value: str) -> str:
        allowed = {"strict", "review", "explore"}
        if value not in allowed:
            raise ValueError(
                f"toolgraph profile {value!r} must be one of {sorted(allowed)}"
            )
        return value


class IntegrationConfig(BaseSettings):
    """Ecosystem-integration settings — the ``[integration.*]`` table family.

    ``report`` (S3), ``trace`` (S3′), and ``toolgraph`` (S5) are the members;
    each is disabled by default, so out of the box the integration surface is
    inert.
    """

    model_config = SettingsConfigDict(extra="ignore")

    report: ReportConfig = Field(default_factory=ReportConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    toolgraph: ToolgraphConfig = Field(default_factory=ToolgraphConfig)


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
    verbatim — like the ``SYNCMILL_CAPABILITIES__AGENTS`` JSON) and
    ``[teams.<name>]`` is keyed by the verbatim team name (like the
    ``SYNCMILL_TEAMS`` JSON).
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
    integration: IntegrationConfig = Field(default_factory=IntegrationConfig)
    teams: dict[str, TeamConfig] = Field(default_factory=dict)
    applied_team: str | None = Field(
        default=None,
        description="INTERNAL/runtime-only: the team profile actually applied to "
        "this process (stamped by `apply_team`, read by the run-report "
        "envelope's `team` field). The `_scrub_applied_team` validator forces "
        "it to None at construction, so a stray SYNCMILL_APPLIED_TEAM env var "
        "or toml key can never forge it — only `apply_team` sets it.",
    )
    default_team: str | None = Field(
        default=None,
        description="Team profile auto-applied by `run`/`board run` when no "
        "explicit --team is passed (precedence: --team > goal→team mapping > "
        "default_team). Persisted by `team use` as a top-level syncmill.toml "
        "key; rides env as SYNCMILL_DEFAULT_TEAM. Deliberately NOT validated "
        "against `teams` at construction — a dangling value must not brick "
        "every command including its own fix (`team use --clear`); the apply "
        "sites raise a clean error instead. An empty string is treated as "
        'unset at apply sites (the `-r ""` house rule).',
    )
    repo_root: str = "."
    default_strategy: str = "route"

    @model_validator(mode="after")
    def _validate_teams(self) -> "SyncmillConfig":
        """Reject a structurally broken ``teams`` map at construction.

        Delegates to the shared :func:`check_team_goals` — the same rule
        ``teamfile.validate_team_save`` enforces before writing a profile.
        """
        check_team_goals(self.teams)
        return self

    @model_validator(mode="after")
    def _scrub_applied_team(self) -> "SyncmillConfig":
        """Force ``applied_team`` to ``None`` at construction — it is RUNTIME
        state only ever stamped by :func:`apply_team` (codex review, S3): a
        declared field necessarily rides the env/toml sources, so without this
        scrub a stray ``SYNCMILL_APPLIED_TEAM`` or toml key could forge a team
        name (arbitrary text) into every run-envelope's ``team`` field."""
        self.applied_team = None
        return self

    def agent_config(self, agent_id: str) -> AgentConfig:
        """Get config for a specific agent by id.

        Hyphenated ids (e.g. ``kimi-code``) map to their underscore field name
        (``kimi_code``) — Python identifiers can't contain hyphens, but agent
        ids can, so the env var is ``SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS``.
        """
        return getattr(self, agent_id.replace("-", "_"), AgentConfig())


def apply_team(config: SyncmillConfig, team_name: str) -> TeamConfig:
    """Overlay the named team profile onto the resolved config IN PLACE.

    ``agent_priority`` ← ``team.agents`` (subset + order); ``capabilities.agents``
    ← ``team.capabilities`` only when non-empty (an unset map keeps the global
    declaration); ``default_strategy`` ← ``team.strategy`` only when set. This is
    post-construction mutation — the same mechanism as the ``--repo``/``--strategy``
    CLI flags — so it trivially overrides every config source; apply it BEFORE
    constructing the ``Supervisor``, which derives the registry, matcher, and
    strategy from these fields in ``__init__``.

    Raises ``KeyError`` on an unknown team name (the CLI wraps it with a friendly
    listing). Returns the applied profile.
    """
    team = config.teams[team_name]
    config.agent_priority = list(team.agents)
    if team.capabilities:
        config.capabilities.agents = dict(team.capabilities)
    if team.strategy is not None:
        config.default_strategy = team.strategy
    # Record WHICH profile was applied — the run-report envelope's `team` field
    # reads it. Stamped here (not threaded through Supervisor.run) so all three
    # apply paths — direct --team/default-team, the monitored inner's forwarded
    # --team, and `board run` — are covered by construction.
    config.applied_team = team_name
    return team


def resolve_goal_team(config: SyncmillConfig, goal: str) -> str | None:
    """Name of the team whose profile declares ``goal``, or ``None``.

    Deterministic by construction: ``_validate_teams`` rejects a goal declared
    by two teams, so at most one team can match.
    """
    for name, team in config.teams.items():
        if goal in team.goals:
            return name
    return None
