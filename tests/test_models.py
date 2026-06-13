"""Task / TaskResult / TaskStatus dataclasses."""

from __future__ import annotations

from uuid import UUID

from syncmill.models import Task, TaskResult, TaskStatus


def test_task_defaults_unique_id():
    a, b = Task(prompt="x"), Task(prompt="y")
    assert isinstance(a.id, UUID)
    assert a.id != b.id
    assert a.status is TaskStatus.PENDING
    assert a.tags == []


def test_task_tags_not_shared_between_instances():
    a, b = Task(prompt="x"), Task(prompt="y")
    a.tags.append("t")
    assert b.tags == []


def test_task_status_is_str_enum():
    assert TaskStatus.COMPLETED == "completed"
    assert f"{TaskStatus.FAILED.value}" == "failed"


def test_taskresult_defaults():
    r = TaskResult(
        task_id=UUID(int=0),
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="done",
        exit_code=0,
        duration_seconds=1.5,
    )
    assert r.files_changed == []
    assert r.error is None


def test_taskresult_files_changed_not_shared():
    r1 = TaskResult(UUID(int=0), "claude", TaskStatus.COMPLETED, "", 0, 0.0)
    r2 = TaskResult(UUID(int=1), "codex", TaskStatus.COMPLETED, "", 0, 0.0)
    r1.files_changed.append("a.py")
    assert r2.files_changed == []
