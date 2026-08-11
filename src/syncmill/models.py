"""Data models for tasks and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolOutcome(StrEnum):
    """Body-free terminal outcome for one observed MCP tool call."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolCallRecord:
    """The minimum safe projection of an observed MCP call."""

    sequence: int
    tool_key: str
    outcome: ToolOutcome
    # Explicit producer-authored retry causality. Built-in parsers leave this unset.
    retry_of_sequence: int | None = None


@dataclass(frozen=True)
class ToolTelemetry:
    """Provider capture summary attached in-memory to a ``TaskResult`` only."""

    source: str
    complete: bool
    calls: tuple[ToolCallRecord, ...] = ()
    malformed_count: int = 0
    unqualified_count: int = 0


@dataclass(frozen=True)
class TokenUsage:
    """Best-effort provider token/cost telemetry parsed from a CLI's output.

    Normalized across providers so display is uniform: ``total_input_tokens`` is
    the FULL prompt size including cached tokens (Claude reports cache tokens
    beside ``input_tokens``, so they are summed in; codex's ``input_tokens``
    already includes ``cached_input_tokens``). Every field is optional — a
    provider that emits no usage (kimi) or malformed usage yields ``None`` at
    the ``TaskResult.usage`` level, never a partial guess.
    """

    total_input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    total_cost_usd: float | None = None

    @classmethod
    def from_mapping(cls, raw: object) -> "TokenUsage | None":
        """Validated construction from an untrusted usage dict (a manifest's
        ``usage`` block, provider telemetry). Each field is dropped
        individually when malformed — negative, boolean, non-int (non-finite
        or negative for cost), or ``cache_read_tokens`` exceeding
        ``total_input_tokens`` — so bad telemetry renders as omitted, never
        as a misleading number. ``None`` when ``raw`` is not a dict."""
        import math

        if not isinstance(raw, dict):
            return None

        def _count(value: object) -> int | None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        counts = {
            key: _count(raw.get(key))
            for key in (
                "total_input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
            )
        }
        if (
            counts["cache_read_tokens"] is not None
            and counts["total_input_tokens"] is not None
            and counts["cache_read_tokens"] > counts["total_input_tokens"]
        ):
            counts["cache_read_tokens"] = None
        cost = raw.get("total_cost_usd")
        try:
            if (
                isinstance(cost, bool)
                or not isinstance(cost, (int, float))
                or not math.isfinite(cost)  # OverflowError on an absurd int
                or cost < 0
            ):
                cost = None
            else:
                cost = float(cost)
        except OverflowError:
            cost = None
        return cls(**counts, total_cost_usd=cost)

    @property
    def cache_hit_ratio(self) -> float | None:
        """Fraction of the input the provider served from its prompt cache
        (None when either side of the ratio is unknown or the input is empty)."""
        if self.cache_read_tokens is None or not self.total_input_tokens:
            return None
        return self.cache_read_tokens / self.total_input_tokens

    def format_compact(self) -> str | None:
        """Short human string for tables — e.g. ``cache 85% (34.2k/40.1k)`` —
        or ``None`` when there is nothing meaningful to show."""

        def _k(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        ratio = self.cache_hit_ratio
        if ratio is not None:
            assert self.cache_read_tokens is not None  # implied by ratio
            assert self.total_input_tokens is not None
            return (
                f"cache {ratio:.0%} "
                f"({_k(self.cache_read_tokens)}/{_k(self.total_input_tokens)})"
            )
        if self.total_input_tokens:
            return f"in {_k(self.total_input_tokens)}"
        return None


@dataclass
class Task:
    """A unit of work to be executed by an agent."""

    prompt: str
    id: UUID = field(default_factory=uuid4)
    status: TaskStatus = TaskStatus.PENDING
    tags: list[str] = field(default_factory=list)


@dataclass
class GateCheck:
    """Result of one quality-gate command run against a worktree."""

    command: str
    passed: bool
    exit_code: int
    output: str = ""
    duration_seconds: float = 0.0


@dataclass
class GateReport:
    """Outcome of running the quality gate against an agent's worktree.

    ``passed`` is the accept/reject signal (every check passed); ``score`` is the
    fraction that passed, the ranking signal CompeteStrategy's ``_select_winner``
    can order candidates by (currently pass/priority; ``score`` is the seam for a
    finer-grained future ranking).
    """

    passed: bool
    score: float
    checks: list[GateCheck] = field(default_factory=list)

    @classmethod
    def from_checks(cls, checks: list[GateCheck]) -> GateReport:
        """Aggregate per-command checks. No checks → trivially passed (score 1.0),
        so an empty gate never blocks (defensive; ``build_gate`` returns ``None``
        for the disabled case instead of an empty gate)."""
        if not checks:
            return cls(passed=True, score=1.0, checks=[])
        n_pass = sum(1 for c in checks if c.passed)
        return cls(
            passed=n_pass == len(checks),
            score=round(n_pass / len(checks), 2),
            checks=list(checks),
        )

    def summary(self) -> str:
        """One-line human summary, naming the failing commands — used in gate
        failure errors and CLI output."""
        n_pass = sum(1 for c in self.checks if c.passed)
        head = f"{n_pass}/{len(self.checks)} checks passed"
        failed = [c.command for c in self.checks if not c.passed]
        if failed:
            head += "; failed: " + ", ".join(failed)
        return head


@dataclass
class TaskResult:
    """Outcome of an agent executing a task.

    ``agent_id`` is normally a real agent id, but Supervisor-level failures use
    sentinel values that are NOT agents: ``"none"`` (no agents available),
    ``"artifact"`` (mandatory run-artifact persistence failed), ``"routing"``
    (model-routing validation failed — all-unmapped explicit tier or corrupt
    inherited metadata), and ``"preflight"`` (strict preflight blocked the
    run). Consumers keying on ``agent_id`` must not treat these as agents.
    """

    task_id: UUID
    agent_id: str
    status: TaskStatus
    output: str
    exit_code: int
    duration_seconds: float
    files_changed: list[str] = field(default_factory=list)
    error: str | None = None
    # Quality-gate verdict, attached by the strategy when a gate ran against this
    # result's worktree (None when no gate is configured). Like files_changed,
    # it's an annotation set post-hoc by the strategy, not produced by the agent.
    gate: GateReport | None = None
    # Full applyable diff of the winning worktree vs HEAD (untracked files
    # included), collected alongside files_changed BEFORE the Supervisor tears the
    # worktree down — the recovery path `run --emit-patch` exports. Like
    # files_changed/gate, an annotation set post-hoc by the strategy; None for a
    # failed result, a non-git workspace, or a diff-collection failure ("" means
    # a clean worktree: the agent changed nothing).
    patch: str | None = None
    # Deliberately omitted from result.json/the envelope; strategies project it
    # into body-free OTLP TOOL spans instead. Kept last to preserve TaskResult's
    # established positional constructor contract.
    tool_telemetry: ToolTelemetry | None = None
    # Durable orchestration metadata. Kept optional and last for compatibility
    # with integrations constructing TaskResult positionally.
    run_id: str | None = None
    artifact_path: str | None = None
    conversation_id: str | None = None
    provider_session_id: str | None = None
    # Best-effort provider token/cache telemetry (None when the CLI emitted none
    # or it was malformed). Appended last to preserve the positional contract.
    usage: TokenUsage | None = None
