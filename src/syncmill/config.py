"""Configuration system for syncmill."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class AgentConfig(BaseModel):
    """Per-agent configuration.

    Env overrides flow through the parent ``SyncmillConfig`` prefix, e.g.
    ``SYNCMILL_CLAUDE__TIMEOUT_SECONDS``.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    timeout_seconds: int = 300
    max_turns: int = 10


class ProjectConfig(BaseModel):
    """Stable project identity and project-local memory boundary."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    memory_dir: str = ".syncmill/memory"


class WorkspaceConfig(BaseModel):
    """Private workspace used to snapshot the user's complete Git state.

    ``stale_seconds`` (``SYNCMILL_WORKSPACE__STALE_SECONDS``) is the age
    backstop in ``sweep_orphaned_workspaces``, NOT its ordinary criterion: a
    crashed run is collected as soon as its recorded pid is gone, and the
    backstop only covers the case a pid cannot decide — the owner is gone but
    its pid has been reused, which would otherwise protect the leak forever.
    Because it OVERRIDES a live pid, lowering it is the setting that can delete
    a running run's workspace, so the floor is an hour. Raise it on a machine
    whose runs legitimately go quiet for days; lower it (toward the floor) when
    a short-lived CI worker recycles pids fast and leaked workspaces cost disk.
    """

    model_config = ConfigDict(extra="ignore")

    root: str = "~/.syncmill/workspaces"
    retain_on_failure: bool = True
    stale_seconds: int = Field(default=7 * 24 * 3600, ge=3600)


class RunsConfig(BaseModel):
    """Durable run artifacts. These are correctness state, not telemetry."""

    model_config = ConfigDict(extra="ignore")

    root: str = "~/.syncmill/projects"


class WorkflowConfig(BaseModel):
    """Bounded native workflow planning and execution.

    A workflow is declarative data, never an executable provider-generated
    script. The limits stay finite at both the friendly default and validation
    layers so a malformed plan cannot turn into unbounded fanout.
    """

    model_config = ConfigDict(extra="forbid")

    planner_agent: Literal["claude", "codex"] = "claude"
    max_parallel: int = Field(default=4, ge=1, le=16)
    max_agent_calls: int = Field(default=32, ge=2, le=256)
    max_depth: int = Field(default=6, ge=1, le=12)
    deadline_seconds: int = Field(default=60 * 60, ge=60, le=4 * 60 * 60)
    max_attempts_per_node: int = Field(default=2, ge=1, le=3)
    retention_days: int = Field(default=30, ge=1, le=3650)
    keep_workflows: int = Field(default=50, ge=0, le=10000)
    max_output_bytes: int = Field(
        default=1024 * 1024, ge=4096, le=16 * 1024 * 1024
    )
    # The durable copy of a node's diff. max_output_bytes bounds what an agent
    # SAYS; this bounds what it leaves behind, which is a separate way for one
    # node to fill the state root.
    max_patch_bytes: int = Field(
        default=32 * 1024 * 1024, ge=4096, le=512 * 1024 * 1024
    )


class RoomsConfig(BaseModel):
    """Durable, human-gated multi-agent comparison rooms.

    Rooms are deliberately opt-in while the state-machine surface settles.  The
    first shipping slice supports one active room per project and at most the
    three built-in providers.  The upper bounds already match the persistent
    room design so expanding the roster later is a configuration change rather
    than an on-disk schema rewrite.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    max_members: int = Field(default=3, ge=1, le=8)
    max_parallel: int = Field(default=3, ge=1, le=4)
    max_rounds: int = Field(default=20, ge=1, le=100)
    retention_days: int = Field(default=30, ge=1)
    keep_rooms: int = Field(default=50, ge=1)
    output_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    lease_timeout_seconds: int = Field(default=900, ge=30)

    @model_validator(mode="after")
    def _parallel_not_above_members(self) -> "RoomsConfig":
        if self.max_parallel > self.max_members:
            raise ValueError("rooms.max_parallel cannot exceed rooms.max_members")
        return self


class ConversationConfig(BaseModel):
    """Provider-native conversation continuation."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True


class SelectionConfig(BaseModel):
    """Winner selection policy for concurrent strategies."""

    model_config = ConfigDict(extra="ignore")

    mode: Literal["verified", "priority", "judge"] = "verified"
    judge_agent: str | None = None


class Mem0Config(BaseModel):
    """Settings for the optional mem0 reference backend.

    ``config`` is passed through to mem0 after syncmill adds its local-first
    defaults and validates the Qdrant lifecycle requirements. Like ``TeamConfig``,
    this nested settings object is populated only by its parent so ambient
    unprefixed ``CONFIG``/``DATA_DIR`` variables cannot reconfigure it.
    """

    model_config = ConfigDict(extra="ignore")

    data_dir: str = "~/.syncmill/mem0"
    telemetry: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


class LangGraphConfig(BaseModel):
    """Settings for the optional local LangGraph BaseStore adapter."""

    model_config = ConfigDict(extra="ignore")

    db_path: str = "~/.syncmill/langgraph.db"


class MemoryConfig(BaseModel):
    """Long-term memory settings (memtomem by default)."""

    model_config = ConfigDict(extra="ignore")

    backend: Literal["memtomem", "mem0", "langgraph"] = "memtomem"
    db_path: str = "~/.memtomem/memtomem.db"
    # None preserves memtomem's ambient/default memory directory. Isolated
    # runtimes should set this together with db_path so Markdown writes and the
    # SQLite index share the same explicit boundary.
    memory_dir: str | None = None
    mem0: Mem0Config = Field(default_factory=Mem0Config)
    langgraph: LangGraphConfig = Field(default_factory=LangGraphConfig)


class BreakerConfig(BaseModel):
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

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    failure_threshold: int = Field(default=3, ge=1)
    cooldown_seconds: int = Field(default=300, ge=0)
    state_path: str = "~/.syncmill/breaker.json"


class GateConfig(BaseModel):
    """Quality-gate settings — commands run against an agent's worktree before
    its result is accepted (#10).

    Each entry in ``commands`` is a single command (parsed with ``shlex.split``,
    run without a shell), so chain checks by adding more entries rather than with
    ``&&``. An empty ``commands`` (the default) **disables** gating, so default
    behavior is unchanged. Override via env, e.g.::

        SYNCMILL_GATE__COMMANDS='["pytest -q", "ruff check ."]'
        SYNCMILL_GATE__TIMEOUT_SECONDS=120
    """

    model_config = ConfigDict(extra="ignore")

    commands: list[str] = Field(default_factory=list)
    timeout_seconds: int = 300


class CapabilityConfig(BaseModel):
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

    model_config = ConfigDict(extra="ignore")

    agents: dict[str, list[str]] = Field(default_factory=dict)


class TeamConfig(BaseModel):
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

    model_config = ConfigDict(extra="ignore")

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


class CouncilConfig(BaseModel):
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

    model_config = ConfigDict(extra="ignore")

    rounds: int = Field(
        default=1,
        ge=1,
        description="Number of critique rounds between propose and synthesize "
        "(>=1). Total phases per council = 1 propose + rounds critique + 1 synthesize.",
    )


class DecomposeConfig(BaseModel):
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

    model_config = ConfigDict(extra="ignore")

    max_subtasks: int = Field(
        default=8,
        ge=1,
        description="Maximum number of subtasks the planner may produce (>=1); the "
        "breakdown is truncated to this many.",
    )


class BoardConfig(BaseModel):
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

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    db_path: str = "~/.syncmill/board.db"
    lease_timeout_seconds: int = Field(default=1800, ge=1)
    stall_seconds: int = Field(default=3600, ge=1)


class TuiConfig(BaseModel):
    """Persistent state for the optional full-screen TUI command center."""

    model_config = ConfigDict(extra="ignore")

    state_dir: str = "~/.syncmill/tui"
    keep_jobs: int = Field(default=20, ge=1)
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)


class MonitorConfig(BaseModel):
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

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    session_prefix: str = "syncmill"


class ReportConfig(BaseModel):
    """Run-report (envelope) settings — the ecosystem join artifact (S3).

    DISABLED by default (``enabled=False``): ``build_reporter`` returns ``None``
    and the run writes no report, so out of the box every run is byte-identical
    to before (mirrors ``BreakerConfig``/``BoardConfig``). ``runs_dir`` may still
    be touched while disabled if tracing is on — the tracer's default output dir
    is that same tree, so the Supervisor sweeps it under ``keep_last`` rather
    than letting trace-only run dirs accumulate forever. When
    enabled, every run writes ``{runs_dir}/{run_id}/envelope.json`` (plus a
    ``result.json`` copy) per ``contracts/run-envelope.schema.json`` —
    best-effort/degrade like memory: a report failure warns and the run,
    result output, and cleanup continue unchanged. Enable + tune via env::

        SYNCMILL_INTEGRATION__REPORT__ENABLED=true
        SYNCMILL_INTEGRATION__REPORT__RUNS_DIR=~/.syncmill/runs
        SYNCMILL_INTEGRATION__REPORT__KEEP_LAST=50

    ``keep_last`` bounds the owned run directories retained at run start (the
    current run always survives and counts toward the total, so ``0`` would be
    meaningless — hence ``ge=1``). Foreign directories are untouched and do not
    count. It governs the trace-only sweep too, so one setting bounds owned
    ``runs_dir`` entries whichever producer is filling them.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    runs_dir: str = "~/.syncmill/runs"
    keep_last: int = Field(default=50, ge=1)


def normalize_phoenix_endpoint(value: str) -> str:
    """Validate and normalize a Phoenix OTLP/HTTP traces endpoint."""
    if not value:
        return value
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "trace endpoint must be an absolute http(s) URL without "
            "credentials, query, or fragment"
        )
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1/traces"):
        path += "/v1/traces"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class TraceConfig(BaseModel):
    """Trace-exporter settings — OTLP-JSON spans for tracegraph (S3′).

    DISABLED by default (``enabled=False``): ``build_tracer`` returns ``None``
    and the run is byte-identical to before (the ``build_gate``/``build_breaker``
    pattern). When enabled, each run buffers span records and atomically writes
    ``{dir}/{run_id}/trace.otlp.json`` — an OTLP protobuf-JSON document
    tracegraph ingests as-is (``ingest-otlp``). Best-effort like memory: an
    exporter failure warns and the run, result, and cleanup continue. Enable via::

        SYNCMILL_INTEGRATION__TRACE__ENABLED=true

    ``exporter="file"`` preserves the durable OTLP-JSON artifact only.
    ``exporter="otlp-http"`` keeps that artifact and additionally streams the
    same body-free spans to Phoenix over OTLP/HTTP protobuf. ``endpoint`` and
    ``project_name`` are optional SyncMill overrides; empty values defer to the
    official ``PHOENIX_COLLECTOR_ENDPOINT`` / ``PHOENIX_PROJECT_NAME`` env vars,
    then local/syncmill defaults. Authentication is intentionally not modeled
    here: the exporter reads ``PHOENIX_API_KEY`` without persisting the secret in
    project configuration. ``dir`` empty means "the report ``runs_dir``" so the
    trace lands beside the run envelope — and is retention-swept under
    ``report.keep_last`` even when reporting is off. A non-empty ``dir`` is
    user-owned and NEVER swept: syncmill won't delete inside a directory the
    operator chose, which may hold more than its run dirs.
    ``include_output`` remains warn+ignored
    — spans are body-free by contract (digests/counts only).
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    exporter: str = "file"
    dir: str = ""
    include_output: bool = False
    endpoint: str = ""
    project_name: str = ""
    batch_delay_seconds: float = Field(default=0.5, gt=0)
    timeout_seconds: int = Field(default=5, ge=1)
    tool_calls_enabled: bool = False
    tool_name_map: dict[str, dict[str, str]] = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def _safe_otlp_http_endpoint(cls, value: str) -> str:
        return normalize_phoenix_endpoint(value)

    @field_validator("project_name")
    @classmethod
    def _nonblank_project_name(cls, value: str) -> str:
        if value and not value.strip():
            raise ValueError("trace project_name must be empty or non-blank")
        return value

    @field_validator("tool_name_map")
    @classmethod
    def _qualified_tool_name_map(
        cls, value: dict[str, dict[str, str]]
    ) -> dict[str, dict[str, str]]:
        bad: list[str] = []
        for provider, mapping in value.items():
            if not provider or not isinstance(mapping, dict):
                bad.append(repr(provider))
                continue
            for raw_name, tool_key in mapping.items():
                if (
                    not raw_name
                    or not tool_key
                    or tool_key.count("::") != 1
                    or any(not part for part in tool_key.split("::"))
                    or any(ch.isspace() for ch in tool_key)
                ):
                    bad.append(f"{provider}:{raw_name!r}->{tool_key!r}")
        if bad:
            raise ValueError(
                "trace tool_name_map values must be qualified 'server::tool' keys: "
                + ", ".join(bad)
            )
        return value


class ToolgraphConfig(BaseModel):
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

    ``mode="advisory"`` preserves the S5 lead-only fail-open behavior;
    ``mode="strict"`` enables P3.1 all-identity enforcement. Strict requires a
    non-empty qualified candidate surface and agent map. Unknown identity/tool
    and unavailability block by default while drift warns; the three policy
    fields accept explicit ``block|warn`` overrides. ``agent_map``
    maps a syncmill agent id to a toolgraph ``Agent.id``, keyed by the **real**
    agent id (``kimi-code`` verbatim — the ``[capabilities.agents]`` rule);
    ``command`` is the invocation seam tests override. ``candidate_tools`` are
    server-qualified ``server::tool`` keys (the ambient MCP surface — memtomem,
    the ``serve`` gateway — an operator wants policy-checked).
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    mode: str = "advisory"
    profile: str = "review"
    unknown_policy: str = "block"
    drift_policy: str = "warn"
    unavailable_policy: str = "block"
    timeout_seconds: int = Field(default=5, ge=1)
    command: list[str] = Field(default_factory=lambda: ["toolgraph", "preflight"])
    candidate_tools: list[str] = Field(default_factory=list)
    agent_map: dict[str, str] = Field(default_factory=dict)

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in {"advisory", "strict"}:
            raise ValueError("toolgraph mode must be 'advisory' or 'strict'")
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

    @field_validator("unknown_policy", "drift_policy", "unavailable_policy")
    @classmethod
    def _known_enforcement_policy(cls, value: str) -> str:
        if value not in {"block", "warn"}:
            raise ValueError("toolgraph enforcement policies must be 'block' or 'warn'")
        return value

    @field_validator("candidate_tools")
    @classmethod
    def _shaped_candidate_tools(cls, value: list[str]) -> list[str]:
        # Mode-INDEPENDENT shape check: candidate_tools are spliced verbatim
        # into the toolgraph argv in advisory mode too, so a token like
        # "--out" or one with whitespace/control chars would be re-parsed as
        # an option and redirect toolgraph's own output/profile flags (argv
        # position confusion — no shell involved). Strict-only rules
        # (non-empty, unique, agent_map present) stay in the model validator.
        bad = [
            tool
            for tool in value
            if not tool
            or tool.startswith("-")
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in tool)
            or tool.count("::") != 1
            or any(not part for part in tool.split("::"))
        ]
        if bad:
            raise ValueError(
                "toolgraph candidate_tools must use qualified 'server::tool' keys "
                "(no leading '-', no whitespace/control chars): "
                + ", ".join(repr(tool) for tool in bad)
            )
        return value

    @model_validator(mode="after")
    def _validate_strict_surface(self) -> "ToolgraphConfig":
        if self.mode != "strict" or not self.enabled:
            return self
        if not self.candidate_tools:
            raise ValueError("toolgraph strict mode requires candidate_tools")
        if len(set(self.candidate_tools)) != len(self.candidate_tools):
            raise ValueError("toolgraph strict candidate_tools must be unique")
        if not self.agent_map:
            raise ValueError("toolgraph strict mode requires at least one agent_map entry")
        return self


def valid_model_selector(value: object) -> bool:
    """The ONE selector-shape rule (config validation AND manifest inheritance):
    a single 1-256-char argv token — no whitespace, no ASCII control chars.
    ``\\Z`` (not ``$``, which tolerates one trailing newline) keeps the regex
    self-sufficient; the control-char clause covers what ``\\S`` can't (e.g.
    DEL, and non-space controls inside the token)."""
    return (
        isinstance(value, str)
        and re.match(r"^\S{1,256}\Z", value) is not None
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    )


class ModelRoutingConfig(BaseModel):
    """Difficulty-based provider model routing (``[model_routing]``).

    DISABLED by default (``mode="off"``): no classifier runs, no decision is
    built, no model flag is passed — behavior is byte-identical to before.
    ``mode="observe"`` classifies each top-level run and records the
    recommendation in the run plan/manifest WITHOUT applying any model flag;
    an explicit ``syncmill run --difficulty low|medium|high`` applies the
    configured selector even under ``mode="off"``. ``mode="enforce"`` is
    reserved for PR 2 (post-calibration) and rejected here.

    ``models`` maps tier → provider → selector::

        [model_routing.models.low]
        claude = "claude-haiku-4-5-20251001"
        kimi-code = "kimi-k2"

    Provider keys are the **real** agent ids (``kimi-code`` verbatim — the
    ``[capabilities.agents]`` key rule). Unlike the lenient per-agent tables
    this model is ``extra="forbid"``: a typo inside a cost-control table must
    be a config error, not a silently-ignored key (§2.2 invariant 5). Selector
    strings are validated for shape only (one non-whitespace argv token, no
    control characters) — syncmill never checks a provider catalog, so a
    syntactically valid but unavailable selector remains an ordinary provider
    execution failure. Env forms::

        SYNCMILL_MODEL_ROUTING__MODE=observe
        SYNCMILL_MODEL_ROUTING__MODELS='{"low":{"codex":"<selector>"}}'
    """

    model_config = ConfigDict(extra="forbid")

    mode: str = "off"
    classifier: str = "prompt-complexity-v1"
    models: dict[str, dict[str, str]] = Field(default_factory=dict)

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value == "enforce":
            raise ValueError(
                "model_routing mode 'enforce' is reserved for a later release "
                "(calibration-gated); use 'off' or 'observe'"
            )
        if value not in {"off", "observe"}:
            raise ValueError("model_routing mode must be 'off' or 'observe'")
        return value

    @field_validator("classifier")
    @classmethod
    def _known_classifier(cls, value: str) -> str:
        if value != "prompt-complexity-v1":
            raise ValueError(
                f"unknown model_routing classifier {value!r} "
                "(supported: 'prompt-complexity-v1')"
            )
        return value

    @field_validator("models")
    @classmethod
    def _valid_models(
        cls, value: dict[str, dict[str, str]]
    ) -> dict[str, dict[str, str]]:
        allowed_tiers = {"low", "medium", "high"}
        allowed_providers = {"claude", "codex", "kimi-code"}
        for tier, table in value.items():
            if tier not in allowed_tiers:
                raise ValueError(
                    f"model_routing.models has unknown tier {tier!r} "
                    f"(allowed: {sorted(allowed_tiers)})"
                )
            for provider, selector in table.items():
                if provider not in allowed_providers:
                    raise ValueError(
                        f"model_routing.models.{tier} has unknown provider "
                        f"{provider!r} (allowed: {sorted(allowed_providers)})"
                    )
                if not valid_model_selector(selector):
                    raise ValueError(
                        f"model_routing.models.{tier}.{provider} selector "
                        f"{selector!r} must be 1-256 non-whitespace characters "
                        "with no control characters"
                    )
        return value


class IntegrationConfig(BaseModel):
    """Ecosystem-integration settings — the ``[integration.*]`` table family.

    ``report`` (S3), ``trace`` (S3′), and ``toolgraph`` (S5) are the members;
    each is disabled by default, so out of the box the integration surface is
    inert.
    """

    model_config = ConfigDict(extra="ignore")

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
    config_version: int = Field(default=2, ge=1)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    runs: RunsConfig = Field(default_factory=RunsConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    rooms: RoomsConfig = Field(default_factory=RoomsConfig)
    conversations: ConversationConfig = Field(default_factory=ConversationConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
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
    tui: TuiConfig = Field(default_factory=TuiConfig)
    integration: IntegrationConfig = Field(default_factory=IntegrationConfig)
    model_routing: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
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
    def _validate_strict_reporting(self) -> "SyncmillConfig":
        if (
            self.integration.toolgraph.enabled
            and self.integration.toolgraph.mode == "strict"
            and not self.integration.report.enabled
        ):
            raise ValueError("toolgraph strict mode requires integration.report.enabled=true")
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
