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


@dataclass
class Task:
    """A unit of work to be executed by an agent."""

    prompt: str
    id: UUID = field(default_factory=uuid4)
    status: TaskStatus = TaskStatus.PENDING
    tags: list[str] = field(default_factory=list)


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
