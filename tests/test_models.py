"""Task / TaskResult / TaskStatus dataclasses."""

from __future__ import annotations

from uuid import UUID

from syncmill.models import GateCheck, GateReport, Task, TaskResult, TaskStatus


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
    assert r.gate is None  # no gate verdict until a strategy attaches one


def test_taskresult_files_changed_not_shared():
    r1 = TaskResult(UUID(int=0), "claude", TaskStatus.COMPLETED, "", 0, 0.0)
    r2 = TaskResult(UUID(int=1), "codex", TaskStatus.COMPLETED, "", 0, 0.0)
    r1.files_changed.append("a.py")
    assert r2.files_changed == []


# ── GateCheck / GateReport ────────────────────────────────────────────────


def test_gatereport_from_checks_passed_and_score():
    r = GateReport.from_checks([GateCheck("a", True, 0), GateCheck("b", True, 0)])
    assert r.passed is True and r.score == 1.0


def test_gatereport_from_checks_fails_if_any_check_fails():
    r = GateReport.from_checks([GateCheck("a", True, 0), GateCheck("b", False, 1)])
    assert r.passed is False and r.score == 0.5


def test_gatereport_empty_is_trivially_passed():
    r = GateReport.from_checks([])
    assert r.passed is True and r.score == 1.0 and r.checks == []


def test_gatereport_summary_lists_failures():
    r = GateReport.from_checks([GateCheck("ok", True, 0), GateCheck("nope", False, 2)])
    summary = r.summary()
    assert "1/2 checks passed" in summary
    assert "failed: nope" in summary  # the failing command, under the "failed:" label
    assert "ok" not in summary  # passing commands aren't listed
