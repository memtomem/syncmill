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


@dataclass(frozen=True)
class ToolTelemetry:
    """Provider capture summary attached in-memory to a ``TaskResult`` only."""

    source: str
    complete: bool
    calls: tuple[ToolCallRecord, ...] = ()
    malformed_count: int = 0
    unqualified_count: int = 0


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
    """Outcome of an agent executing a task."""

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
