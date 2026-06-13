"""supervisor.py — RouteStrategy, Supervisor.run lifecycle, graceful memory
degradation, the unbound-result fix, and git change detection."""

from __future__ import annotations

from pathlib import Path

import pytest

import syncmill.supervisor as S
from syncmill.config import MemoryConfig, SyncmillConfig
from syncmill.memory import SyncmillMemory
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.supervisor import RouteStrategy, Supervisor, _aggregate_failure, _git_changed_files

from conftest import FakeAgent, FakeMemory, FakeRegistry, FakeStore, FakeWorktree, make_memory


# ── RouteStrategy ────────────────────────────────────────────────────────


def _run_route(agents, memory, *, config=None, task=None):
    return RouteStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        workdir=Path("/tmp"),
        config=config or SyncmillConfig(),
    )


def test_route_enriches_prompt_with_context():
    agent = FakeAgent("claude")
    _run_route([agent], FakeMemory(context="prior knowledge"))
    sent = agent.calls[0]["prompt"]
    assert sent == "Context from previous work:\nprior knowledge\n\n---\n\nTask: do it"


def test_route_no_context_passes_raw_prompt():
    agent = FakeAgent("claude")
    _run_route([agent], FakeMemory(context=""))
    assert agent.calls[0]["prompt"] == "do it"


def test_route_falls_back_to_second_agent():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="nope")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="win")
    r = _run_route([a, b], FakeMemory())
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_route_short_circuits_on_first_success():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex")
    _run_route([a, b], FakeMemory())
    assert b.calls == []


def test_route_all_fail_aggregates():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="auth")
    b = FakeAgent("codex", status=TaskStatus.FAILED, error=None, exit_code=3)
    r = _run_route([a, b], FakeMemory())
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "none"
    assert r.error.startswith("All agents failed:")
    assert "[claude] auth" in r.error
    assert "[codex] exit_code=3" in r.error


def test_route_passes_per_agent_timeout():
    cfg = SyncmillConfig()
    cfg.claude.timeout_seconds = 7
    agent = FakeAgent("claude")
    _run_route([agent], FakeMemory(), config=cfg)
    assert agent.calls[0]["timeout"] == 7


def test_aggregate_failure_sums_durations():
    errs = [
        TaskResult(Task(prompt="").id, "claude", TaskStatus.FAILED, "", 1, 2.0, error="a"),
        TaskResult(Task(prompt="").id, "codex", TaskStatus.FAILED, "", 1, 3.0, error="b"),
    ]
    r = _aggregate_failure(Task(prompt=""), errs)
    assert r.duration_seconds == 5.0


def test_route_survives_dead_memory():
    # real SyncmillMemory wrapping a fully-failing store must degrade, not raise
    mem = make_memory(FakeStore(raise_on={"search", "_ensure_init"}))
    agent = FakeAgent("claude", status=TaskStatus.COMPLETED)
    r = _run_route([agent], mem)
    assert r.status is TaskStatus.COMPLETED
    assert agent.calls[0]["prompt"] == "do it"  # no context injected
    assert mem.degraded is True


# ── Supervisor.run ───────────────────────────────────────────────────────


def _supervisor(registry, memory, strategy=None, config=None):
    sup = Supervisor.__new__(Supervisor)
    sup._config = config or SyncmillConfig()
    sup._registry = registry
    sup._memory = memory
    sup._strategy = strategy or RouteStrategy()
    return sup


@pytest.fixture
def patched_worktree(monkeypatch, tmp_path):
    """Stub worktree creation/cleanup; capture cleanup calls."""
    wt = FakeWorktree(tmp_path, is_git=True)
    created = {}
    cleaned = []
    monkeypatch.setattr(
        S, "create_worktree",
        lambda root, agent_id, task_id: created.update(
            root=root, agent_id=agent_id, task_id=task_id
        ) or wt,
    )
    monkeypatch.setattr(S, "cleanup_worktree", lambda root, w: cleaned.append(w))
    monkeypatch.setattr(S, "_git_changed_files", lambda p: {"changed.py"})
    return {"wt": wt, "created": created, "cleaned": cleaned}


def test_run_happy_path(patched_worktree):
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem)
    task = Task(prompt="go")
    r = sup.run(task)
    assert r.status is TaskStatus.COMPLETED
    assert "register:claude" in mem.events
    assert any(e.startswith("start:") for e in mem.events)
    assert any(e.startswith("end:") for e in mem.events)
    assert len(mem.shared) == 1
    assert patched_worktree["cleaned"] == [patched_worktree["wt"]]
    assert patched_worktree["created"]["agent_id"] == "claude"
    assert patched_worktree["created"]["task_id"] == str(task.id)


def test_run_collects_files_changed_when_git_and_completed(patched_worktree):
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"))
    assert r.files_changed == ["changed.py"]


def test_run_no_files_changed_when_not_git(monkeypatch, tmp_path):
    wt = FakeWorktree(tmp_path, is_git=False)
    monkeypatch.setattr(S, "create_worktree", lambda *a: wt)
    monkeypatch.setattr(S, "cleanup_worktree", lambda *a: None)
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"))
    assert r.files_changed == []


def test_run_failed_skips_share_but_cleans_up(patched_worktree):
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", status=TaskStatus.FAILED, error="x")]), mem
    )
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.FAILED
    assert mem.shared == []
    assert any(e.startswith("end:") for e in mem.events)
    assert patched_worktree["cleaned"]


def test_run_unknown_forced_agent(patched_worktree):
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"), agent_id="ghost")
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "ghost"
    assert "not found" in r.error
    assert patched_worktree["created"] == {}  # no worktree created


def test_run_no_agents_available(patched_worktree):
    sup = _supervisor(FakeRegistry([]), FakeMemory())
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.FAILED
    assert r.error == "No agents available"


def test_run_exclude_drops_agent_from_fallback(patched_worktree):
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", output="from-claude"),
                      FakeAgent("gemini", output="from-gemini")]),
        mem,
    )
    r = sup.run(Task(prompt="go"), exclude={"claude"})
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "gemini"  # claude (first priority) was excluded
    assert r.output == "from-gemini"
    assert patched_worktree["created"]["agent_id"] == "gemini"  # workspace keyed off the survivor
    # excluded agents are never registered in memory either
    assert "register:claude" not in mem.events
    assert "register:gemini" in mem.events


def test_run_all_agents_excluded(patched_worktree):
    # two excluded agents → exercises the sorted() multi-element error rendering
    sup = _supervisor(FakeRegistry([FakeAgent("gemini"), FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"), exclude={"gemini", "claude"})
    assert r.status is TaskStatus.FAILED
    assert r.error == "No agents available (excluded: claude, gemini)"  # sorted
    assert patched_worktree["created"] == {}  # no worktree created


def test_run_forced_agent_also_excluded_errors(patched_worktree):
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"), agent_id="claude", exclude={"claude"})
    assert r.status is TaskStatus.FAILED
    assert "both forced" in r.error
    assert patched_worktree["created"] == {}  # rejected before any workspace


def test_run_forced_agent_with_unrelated_exclude(patched_worktree):
    # forcing gemini while excluding a *different* agent (claude) is valid, not a conflict
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude"), FakeAgent("gemini", output="g"),
                      FakeAgent("codex")]),
        mem,
    )
    r = sup.run(Task(prompt="go"), agent_id="gemini", exclude={"claude"})
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "gemini"  # the guard only fires when the forced agent IS excluded
    assert "register:gemini" in mem.events and "register:claude" not in mem.events


def test_run_survives_dead_memory(patched_worktree):
    # the whole point: a broken memtomem must not crash the task
    mem = make_memory(
        FakeStore(raise_on={"_ensure_init", "start_session", "search", "add",
                            "log_event", "end_session"})
    )
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="done")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "done"
    assert mem.degraded is True


def test_run_survives_memtomem_construction_failure(monkeypatch, patched_worktree):
    # memtomem broken at construction time must still let the task run
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no memtomem")),
    )
    mem = SyncmillMemory(MemoryConfig())
    assert mem._store is None and mem.degraded is True
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="done")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "done"


def test_run_strategy_exception_does_not_mask_with_unbound_result(patched_worktree):
    class Boom:
        def execute(self, **kw):
            raise ValueError("strategy blew up")

    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), strategy=Boom())
    with pytest.raises(ValueError, match="strategy blew up"):
        sup.run(Task(prompt="go"))
    # finally still cleaned the worktree, and did NOT raise UnboundLocalError
    assert patched_worktree["cleaned"]


def test_close_closes_memory():
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([]), mem)
    sup.close()
    assert "close" in mem.events


# ── _git_changed_files (real git) ────────────────────────────────────────


def test_git_changed_detects_modified_and_untracked(git_repo):
    (git_repo / "README.md").write_text("# changed\n")
    (git_repo / "new.txt").write_text("hi\n")
    changed = _git_changed_files(git_repo)
    assert "README.md" in changed
    assert "new.txt" in changed


def test_git_changed_clean_repo_empty(git_repo):
    assert _git_changed_files(git_repo) == set()


def test_git_changed_respects_gitignore(git_repo):
    (git_repo / ".gitignore").write_text("ignored.log\n")
    (git_repo / "ignored.log").write_text("secret\n")
    changed = _git_changed_files(git_repo)
    assert "ignored.log" not in changed


def test_git_changed_non_git_dir_empty(tmp_path):
    assert _git_changed_files(tmp_path) == set()
