"""memory.py — search_context logic, best-effort writes, status, degradation,
and the run_async loop. No real memtomem is constructed."""

from __future__ import annotations

import pytest

import syncmill.memory as mem
from syncmill.config import MemoryConfig
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus

from conftest import FakeStore, make_memory


def _result(error=None, files=None):
    r = TaskResult(
        task_id=Task(prompt="p").id,
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="the output",
        exit_code=0,
        duration_seconds=1.2,
        error=error,
    )
    if files:
        r.files_changed = files
    return r


# ── search_context ───────────────────────────────────────────────────────


def test_search_context_merges_dedups_and_sorts():
    store = FakeStore(
        search_results={
            "agent-runtime:claude": [
                {"id": "1", "content": "AGENT COPY", "score": 0.9, "source": "a.md"}
            ],
            "shared": [
                {"id": "1", "content": "SHARED COPY", "score": 0.99, "source": "s.md"},
                {"id": "2", "content": "beta", "score": 0.5, "source": "b.md"},
            ],
        }
    )
    out = run_async(make_memory(store).search_context("q", agent_id="claude"))
    lines = out.split("\n---\n")
    # dedup keeps the agent-namespace copy (id=1), drops the higher-scored shared dup
    assert "AGENT COPY" in out and "SHARED COPY" not in out
    # sorted by score desc: 0.9 (alpha) before 0.5 (beta)
    assert lines[0] == "[a.md] AGENT COPY"
    assert lines[1] == "[b.md] beta"


def test_search_context_without_agent_only_shared():
    store = FakeStore(
        search_results={
            "agent-runtime:claude": [{"id": "x", "content": "nope", "score": 1.0, "source": "a"}],
            "shared": [{"id": "y", "content": "yes", "score": 0.1, "source": "b"}],
        }
    )
    out = run_async(make_memory(store).search_context("q"))
    assert out == "[b] yes"


def test_search_context_empty():
    assert run_async(make_memory(FakeStore()).search_context("q", "claude")) == ""


def test_search_context_source_fallback_and_truncation():
    store = FakeStore(
        search_results={"shared": [{"id": "1", "content": "x" * 400, "score": 1.0}]}
    )
    out = run_async(make_memory(store).search_context("q"))
    assert out.startswith("[unknown] ")
    assert len(out) == len("[unknown] ") + 300


def test_search_context_caps_at_five():
    store = FakeStore(
        search_results={
            "shared": [
                {"id": str(i), "content": f"c{i}", "score": 1.0 - i / 100, "source": "s"}
                for i in range(8)
            ]
        }
    )
    out = run_async(make_memory(store).search_context("q"))
    assert len(out.split("\n---\n")) == 5


# ── degradation: a dead store never raises ───────────────────────────────


def test_search_context_degrades_on_error():
    m = make_memory(FakeStore(raise_on={"search"}))
    assert run_async(m.search_context("q")) == ""
    assert m.degraded is True
    assert m.last_error


def test_register_agent_degrades_on_init_error():
    m = make_memory(FakeStore(raise_on={"_ensure_init"}))
    run_async(m.register_agent("claude"))  # must not raise
    assert m.degraded is True


def test_start_session_degrades_returns_none():
    m = make_memory(FakeStore(raise_on={"start_session"}))
    assert run_async(m.start_task_session("claude", Task(prompt="p"))) is None
    assert m.degraded is True


def test_construction_failure_degrades_through_real_init(monkeypatch):
    # memtomem entirely broken at construction must NOT crash; the real __init__
    # path (not __new__) is exercised here.
    def boom(*a, **k):
        raise RuntimeError("memtomem unavailable at construction")

    monkeypatch.setattr("memtomem.integrations.langgraph.MemtomemStore", boom)
    m = SyncmillMemory(MemoryConfig())
    assert m._store is None
    assert m.degraded is True and m.last_error
    # every method stays safe with a None store
    assert run_async(m.search_context("q", "claude")) == ""
    assert run_async(m.start_task_session("claude", Task(prompt="p"))) is None
    run_async(m.register_agent("claude"))
    run_async(m.end_task_session(None, _result()))
    run_async(m.share_result(Task(prompt="p"), _result()))
    assert run_async(m.status())["connected"] is False
    run_async(m.close())


def test_init_derives_namespaces_from_memtomem_constants():
    # The real __init__ (not the __new__ fixture) must derive its namespace
    # prefixes from memtomem.constants so they can never drift from the library.
    from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE

    m = SyncmillMemory(MemoryConfig())  # store is lazy — no DB / network touch
    assert m._agent_prefix == AGENT_NAMESPACE_PREFIX
    assert m._shared_ns == SHARED_NAMESPACE


# ── best-effort writes ───────────────────────────────────────────────────


def test_register_agent_sets_missing_namespaces():
    store = FakeStore()
    run_async(make_memory(store).register_agent("claude"))
    assert set(store._components.storage.set_calls) == {"agent-runtime:claude", "shared"}


def test_register_agent_idempotent():
    store = FakeStore(existing_ns={"agent-runtime:claude", "shared"})
    run_async(make_memory(store).register_agent("claude"))
    assert store._components.storage.set_calls == []


def test_start_task_session_logs_and_truncates_prompt():
    store = FakeStore()
    sid = run_async(make_memory(store).start_task_session("claude", Task(prompt="P" * 500)))
    assert sid == "sid-1"
    assert store.sessions[0] == {"agent_id": "claude", "namespace": "agent-runtime:claude"}
    et, content = store.events[0]
    assert et == "task_start"
    assert content == "Prompt: " + "P" * 200


def test_end_task_session_logs_error_then_end():
    store = FakeStore()
    run_async(make_memory(store).end_task_session("sid", _result(error="kaboom")))
    kinds = [e[0] for e in store.events]
    assert "task_error" in kinds and "task_end" in kinds
    assert store.ended and "claude: completed" in store.ended[0]


def test_end_task_session_no_error_skips_task_error():
    store = FakeStore()
    run_async(make_memory(store).end_task_session("sid", _result()))
    assert [e[0] for e in store.events] == ["task_end"]


def test_share_result_writes_to_shared_with_tags():
    store = FakeStore()
    run_async(make_memory(store).share_result(Task(prompt="do"), _result(files=["a.py"])))
    rec = store.added[0]
    assert rec["namespace"] == "shared"
    assert rec["tags"] == ["task-result", "claude"]
    assert "**Files changed:** a.py" in rec["content"]


# ── status ───────────────────────────────────────────────────────────────


def test_status_connected():
    info = run_async(make_memory(FakeStore()).status())
    assert info["connected"] is True
    assert info["db_path"] == "/tmp/fake.db"


def test_status_disconnected():
    info = run_async(make_memory(FakeStore(raise_on={"_ensure_init"})).status())
    assert info["connected"] is False
    assert "error" in info


# ── shared-read gateway methods (#15) ──────────────────────────────────────


def test_search_shared_scopes_to_shared_only():
    store = FakeStore(
        search_results={
            "shared": [
                {"id": "s1", "content": "SHARED", "score": 0.9,
                 "source": "a.md", "namespace": "shared"}
            ],
            "agent-runtime:claude": [
                {"id": "a1", "content": "PRIVATE", "score": 0.99,
                 "source": "b.md", "namespace": "agent-runtime:claude"}
            ],
        }
    )
    hits = run_async(make_memory(store).search_shared("q", top_k=4))
    assert [h["id"] for h in hits] == ["s1"]  # the private namespace is never searched
    # the store was queried with namespace="shared" and the given top_k
    assert store.searched == [{"query": "q", "namespace": "shared", "top_k": 4}]


def test_search_shared_filters_nonshared_rows_defense_in_depth():
    # Even if the store mislabels a row, the method drops anything not in shared.
    store = FakeStore(
        search_results={
            "shared": [
                {"id": "s1", "content": "ok", "score": 0.5,
                 "source": "a.md", "namespace": "shared"},
                {"id": "x1", "content": "leak", "score": 0.6,
                 "source": "b.md", "namespace": "agent-runtime:claude"},
            ]
        }
    )
    hits = run_async(make_memory(store).search_shared("q"))
    assert [h["id"] for h in hits] == ["s1"]


def test_search_shared_empty_is_list_not_none():
    # Healthy-empty is [], distinct from unavailable (None).
    assert run_async(make_memory(FakeStore()).search_shared("q")) == []


def test_search_shared_degrades_returns_none():
    m = make_memory(FakeStore(raise_on={"search"}))
    assert run_async(m.search_shared("q")) is None
    assert m.degraded is True


def test_get_shared_returns_shared_chunk():
    cid = "11111111-1111-1111-1111-111111111111"
    store = FakeStore(get_results={cid: {"id": cid, "content": "c",
                                         "namespace": "shared", "source": "a.md", "tags": []}})
    out = run_async(make_memory(store).get_shared(cid))
    assert out["id"] == cid


def test_get_shared_refuses_nonshared_chunk_isolation():
    cid = "22222222-2222-2222-2222-222222222222"
    store = FakeStore(get_results={cid: {"id": cid, "content": "PRIVATE",
                                         "namespace": "agent-runtime:claude"}})
    assert run_async(make_memory(store).get_shared(cid)) is None  # isolation guard


def test_get_shared_missing_id_returns_none():
    cid = "33333333-3333-3333-3333-333333333333"
    assert run_async(make_memory(FakeStore()).get_shared(cid)) is None


def test_get_shared_malformed_id_not_found_without_degrading():
    m = make_memory(FakeStore())
    assert run_async(m.get_shared("not-a-uuid")) is None
    assert m.degraded is False  # a client typo is not a backend failure


def test_get_shared_degrades_on_store_error():
    cid = "44444444-4444-4444-4444-444444444444"
    m = make_memory(FakeStore(raise_on={"get"}))
    assert run_async(m.get_shared(cid)) is None
    assert m.degraded is True


def test_shared_stats_reports_shared_count():
    store = FakeStore(existing_ns={"shared"},
                      ns_counts={"shared": 7, "agent-runtime:claude": 3})
    out = run_async(make_memory(store).shared_stats())
    assert out == {"namespace": "shared", "chunk_count": 7}


def test_shared_stats_zero_when_shared_absent():
    assert run_async(make_memory(FakeStore()).shared_stats()) == {
        "namespace": "shared",
        "chunk_count": 0,
    }


def test_shared_stats_degrades_returns_none():
    m = make_memory(FakeStore(raise_on={"list_namespace_meta"}))
    assert run_async(m.shared_stats()) is None
    assert m.degraded is True


# ── run_async / loop lifecycle ───────────────────────────────────────────


async def _echo(v):
    return v


def test_run_async_returns_value():
    assert run_async(_echo(7)) == 7


def test_run_async_reuses_loop():
    run_async(_echo(1))
    loop1 = mem._loop
    run_async(_echo(2))
    assert mem._loop is loop1


def test_run_async_recreates_closed_loop():
    run_async(_echo(1))
    mem._loop.close()
    assert run_async(_echo(2)) == 2
    assert not mem._loop.is_closed()


def test_run_async_propagates_exceptions():
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        run_async(boom())
