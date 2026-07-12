"""memory.py — search_context logic, best-effort writes, status, degradation,
and the run_async loop. No real memtomem is constructed."""

from __future__ import annotations

import sys

import pytest

import syncmill.memory as mem
from syncmill.config import MemoryConfig
from syncmill.memory import BOARD_RUN_PREFIX, SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus

from conftest import FakeBackend, make_memory


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
    store = FakeBackend(
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
    store = FakeBackend(
        search_results={
            "agent-runtime:claude": [{"id": "x", "content": "nope", "score": 1.0, "source": "a"}],
            "shared": [{"id": "y", "content": "yes", "score": 0.1, "source": "b"}],
        }
    )
    out = run_async(make_memory(store).search_context("q"))
    assert out == "[b] yes"


def test_search_context_empty():
    assert run_async(make_memory(FakeBackend()).search_context("q", "claude")) == ""


def test_search_context_source_fallback_and_truncation():
    store = FakeBackend(
        search_results={"shared": [{"id": "1", "content": "x" * 400, "score": 1.0}]}
    )
    out = run_async(make_memory(store).search_context("q"))
    assert out.startswith("[unknown] ")
    assert len(out) == len("[unknown] ") + 300


def test_search_context_caps_at_five():
    store = FakeBackend(
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
    m = make_memory(FakeBackend(raise_on={"search"}))
    assert run_async(m.search_context("q")) == ""
    assert m.degraded is True
    assert m.last_error


def test_register_agent_degrades_on_init_error():
    m = make_memory(FakeBackend(raise_on={"ensure_ready"}))
    run_async(m.register_agent("claude"))  # must not raise
    assert m.degraded is True


def test_start_session_degrades_returns_none():
    m = make_memory(FakeBackend(raise_on={"start_session"}))
    assert run_async(m.start_task_session("claude", Task(prompt="p"))) is None
    assert m.degraded is True


def test_construction_failure_degrades_through_real_init(monkeypatch):
    # memtomem entirely broken at construction must NOT crash; the real __init__
    # path (not __new__) is exercised here.
    def boom(*a, **k):
        raise RuntimeError("memtomem unavailable at construction")

    monkeypatch.setattr("memtomem.integrations.langgraph.MemtomemStore", boom)
    m = SyncmillMemory(MemoryConfig())
    assert m._backend is None
    assert m.degraded is True and m.last_error
    # every method stays safe with a None store
    assert run_async(m.search_context("q", "claude")) == ""
    assert run_async(m.start_task_session("claude", Task(prompt="p"))) is None
    run_async(m.register_agent("claude"))
    run_async(m.end_task_session(None, _result()))
    run_async(m.share_result(Task(prompt="p"), _result()))
    assert run_async(m.status())["connected"] is False
    run_async(m.close())


def test_construction_failure_uses_backend_independent_namespace_fallbacks(monkeypatch):
    import memtomem.constants as constants

    monkeypatch.setattr(constants, "AGENT_NAMESPACE_PREFIX", "custom-agent:")
    monkeypatch.setattr(constants, "SHARED_NAMESPACE", "custom-shared")
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("construction failed")),
    )

    m = SyncmillMemory(MemoryConfig())
    assert m._backend is None
    assert m._agent_prefix == "agent-runtime:"
    assert m._shared_ns == "shared"


def test_unimportable_memtomem_uses_hardcoded_namespace_fallbacks(monkeypatch):
    monkeypatch.setitem(sys.modules, "memtomem.constants", None)
    m = SyncmillMemory(MemoryConfig())
    assert m._backend is None and m.degraded is True
    assert m._agent_prefix == "agent-runtime:"
    assert m._shared_ns == "shared"


def test_successful_constructor_uses_backend_namespace_properties(monkeypatch):
    backend = FakeBackend()
    backend._agent_prefix = "backend-agent:"
    backend._shared_ns = "backend-shared"
    monkeypatch.setattr(
        type(backend),
        "agent_namespace_prefix",
        property(lambda self: self._agent_prefix),
    )
    monkeypatch.setattr(
        type(backend), "shared_namespace", property(lambda self: self._shared_ns)
    )
    monkeypatch.setattr("syncmill.memory_backend.build_backend", lambda config: backend)

    m = SyncmillMemory(MemoryConfig())
    assert m._backend is backend
    assert m._agent_prefix == "backend-agent:"
    assert m._shared_ns == "backend-shared"


def test_init_derives_namespaces_from_memtomem_constants():
    # The real __init__ (not the __new__ fixture) must derive its namespace
    # prefixes from memtomem.constants so they can never drift from the library.
    from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE

    m = SyncmillMemory(MemoryConfig())  # store is lazy — no DB / network touch
    assert m._agent_prefix == AGENT_NAMESPACE_PREFIX
    assert m._shared_ns == SHARED_NAMESPACE


# ── best-effort writes ───────────────────────────────────────────────────


def test_register_agent_sets_missing_namespaces():
    store = FakeBackend()
    run_async(make_memory(store).register_agent("claude"))
    assert set(store.set_calls) == {"agent-runtime:claude", "shared"}


def test_register_agent_idempotent():
    store = FakeBackend(existing_ns={"agent-runtime:claude", "shared"})
    run_async(make_memory(store).register_agent("claude"))
    assert store.set_calls == []


def test_start_task_session_logs_and_truncates_prompt():
    store = FakeBackend()
    sid = run_async(make_memory(store).start_task_session("claude", Task(prompt="P" * 500)))
    assert sid == "sid-1"
    assert store.sessions[0] == {"agent_id": "claude", "namespace": "agent-runtime:claude"}
    et, content = store.events[0]
    assert et == "task_start"
    assert content == "Prompt: " + "P" * 200


def test_end_task_session_logs_error_then_end():
    store = FakeBackend()
    run_async(make_memory(store).end_task_session("sid", _result(error="kaboom")))
    kinds = [e[0] for e in store.events]
    assert "task_error" in kinds and "task_end" in kinds
    assert store.ended and "claude: completed" in store.ended[0]


def test_end_task_session_no_error_skips_task_error():
    store = FakeBackend()
    run_async(make_memory(store).end_task_session("sid", _result()))
    assert [e[0] for e in store.events] == ["task_end"]


def test_share_result_writes_to_shared_with_tags():
    store = FakeBackend()
    run_async(make_memory(store).share_result(Task(prompt="do"), _result(files=["a.py"])))
    rec = store.added[0]
    assert rec["namespace"] == "shared"
    assert rec["tags"] == ["task-result", "claude"]
    assert "**Files changed:** a.py" in rec["content"]


# ── board (Phase 1): share_board_result + scoped() namespace isolation ─────


def test_share_board_result_writes_to_global_shared_with_composite_tag():
    store = FakeBackend()
    ok = run_async(
        make_memory(store).share_board_result("item-1", "the task", "the output")
    )
    assert ok is True
    rec = store.added[0]
    assert rec["namespace"] == "shared"  # the ONE board-derived GLOBAL write
    # the cross-link is the SINGLE composite tag — EXACTLY one, never a broad
    # "board-result" alongside it (which tag_filter would OR-match across all items)
    assert rec["tags"] == ["board-result:item-1"]
    assert "**Task:** the task" in rec["content"]
    assert "the output" in rec["content"]


def test_share_board_result_degrades_returns_false():
    store = FakeBackend(raise_on={"add"})
    m = make_memory(store)
    assert run_async(m.share_board_result("item-1", "t", "o")) is False
    assert m.degraded is True


def test_scoped_memory_collapses_write_and_session_namespaces():
    # the leak guard at the production level: a board-scoped view's WRITES (share +
    # session prompt-log + namespace registration) land ONLY in board-run:{run_id} —
    # never global shared nor agent-runtime:{agent}. (Reads widen — see the D4 test.)
    store = FakeBackend(search_results={"board-run:R1": []})
    parent = make_memory(store)
    view = parent.scoped("R1")
    assert view._backend is parent._backend
    assert view.degraded is False and parent.degraded is False
    # share_result lands in the per-run scope, NOT global shared
    run_async(view.share_result(Task(prompt="enriched board prompt"), _result()))
    assert store.added[-1]["namespace"] == "board-run:R1"
    # the episodic session (which logs the prompt) is scoped too — not agent-runtime
    run_async(view.start_task_session("claude", Task(prompt="enriched")))
    assert store.sessions[-1]["namespace"] == "board-run:R1"
    # Scratch is implicit-create: registration must not leave metadata-only residue.
    run_async(view.register_agent("claude"))
    assert store.set_calls == []
    view._degrade("test", RuntimeError("view-only"))
    assert view.degraded is True and parent.degraded is False


def test_scoped_memory_search_union_reads_global_shared():
    # D4: a board-scoped view's READS widen to UNION the per-run scope with the REAL
    # global `shared`, so a board run is enriched by cross-run knowledge — while its
    # WRITES stay scoped (the leak guard above is untouched).
    store = FakeBackend(search_results={"board-run:R1": [], "shared": []})
    view = make_memory(store).scoped("R1")
    run_async(view.search_context("q", agent_id="claude"))
    assert {s["namespace"] for s in store.searched} == {"board-run:R1", "shared"}


def test_unscoped_memory_search_does_not_double_read_shared():
    # a plain (unscoped) run is UNCHANGED by D4: its `_shared_ns` IS global `shared`,
    # so it reads `shared` once (+ the agent scope), never a redundant union read.
    store = FakeBackend(search_results={"agent-runtime:claude": [], "shared": []})
    run_async(make_memory(store).search_context("q", agent_id="claude"))
    namespaces = [s["namespace"] for s in store.searched]
    assert namespaces.count("shared") == 1
    assert set(namespaces) == {"agent-runtime:claude", "shared"}


def test_scoped_does_not_mutate_parent_namespaces():
    store = FakeBackend()
    parent = make_memory(store)
    parent.scoped("R1")
    # the parent is untouched: its writes still go to global shared / agent-runtime
    run_async(parent.share_result(Task(prompt="p"), _result()))
    assert store.added[-1]["namespace"] == "shared"
    run_async(parent.start_task_session("claude", Task(prompt="p")))
    assert store.sessions[-1]["namespace"] == "agent-runtime:claude"


def test_scoped_namespace_uses_prefix_constant():
    view = make_memory(FakeBackend()).scoped("R1")
    assert view._shared_ns == f"{BOARD_RUN_PREFIX}R1"
    assert view._scope == f"{BOARD_RUN_PREFIX}R1"


def test_scoped_rejects_backend_invalid_full_namespace():
    backend = FakeBackend()
    backend.validate_namespace = lambda ns: False
    with pytest.raises(ValueError, match="invalid board-run memory namespace"):
        make_memory(backend).scoped("bad id")


def test_scoped_validation_error_degrades_but_allows_advisory_view():
    backend = FakeBackend()

    def boom(ns):
        raise RuntimeError("validator broke")

    backend.validate_namespace = boom
    parent = make_memory(backend)
    view = parent.scoped("R1")
    assert view._scope == "board-run:R1"
    assert parent.degraded is True


# ── board-run namespace GC ───────────────────────────────────────────────


def test_delete_board_runs_reconstructs_prefix_only():
    # the by-construction prefix guard: even a hostile bare "shared" run_id can
    # only ever target "board-run:shared" — never the real shared namespace.
    store = FakeBackend(
        existing_ns={"shared", "board-run:R1"},
        ns_counts={"board-run:R1": 2, "board-run:shared": 1},
    )
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["R1", "shared"])) == {"R1", "shared"}
    assert store.deleted == ["board-run:R1", "board-run:shared"]
    assert "shared" in store.meta


def test_delete_board_runs_continues_past_one_failure():
    # each delete is independently transactional — one bad namespace must not
    # strand the rest of the garbage.
    store = FakeBackend(
        existing_ns={"board-run:bad", "board-run:good"},
        ns_counts={"board-run:bad": 1, "board-run:good": 1},
        delete_raise_ns={"board-run:bad"},
    )
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["bad", "good"])) == {"good"}
    assert store.deleted == ["board-run:good"]
    assert m.degraded is True


def test_delete_board_runs_reachable_but_every_delete_fails_returns_zero():
    store = FakeBackend(
        ns_counts={"board-run:a": 1, "board-run:b": 1},
        delete_raise_ns={"board-run:a", "board-run:b"},
    )
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["a", "b"])) == set()
    assert m.degraded is True


def test_delete_board_runs_zero_chunk_call_is_acknowledged():
    # memtomem#1705: delete_by_namespace returns 0 for a chunk-less namespace
    # WITHOUT removing its metadata row — so the purged count must not claim
    # it (it's still there and will be a candidate again next sweep). Not an
    # error either: the memory stays healthy.
    store = FakeBackend(existing_ns={"board-run:meta-only"})  # no ns_counts → 0 chunks
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["meta-only"])) == {"meta-only"}
    assert m.degraded is False


def test_delete_board_runs_unreachable_store_returns_none():
    m = make_memory(FakeBackend(raise_on={"ensure_ready"}))
    assert run_async(m.delete_board_runs(["R1"])) is None
    assert m.degraded is True


def test_delete_board_runs_skips_empty_run_id():
    store = FakeBackend()
    assert run_async(make_memory(store).delete_board_runs(["", None])) == set()
    assert store.deleted == []


# ── status ───────────────────────────────────────────────────────────────


def test_status_connected():
    info = run_async(make_memory(FakeBackend()).status())
    assert info == {"connected": True, "db_path": "/tmp/fake.db"}


def test_status_disconnected():
    m = make_memory(FakeBackend(raise_on={"ensure_ready"}))
    info = run_async(m.status())
    assert info == {"connected": False, "error": "boom in ensure_ready"}
    assert m.degraded is False  # status is a probe, not a degrading operation


# ── shared-read gateway methods (#15) ──────────────────────────────────────


def test_search_shared_scopes_to_shared_only():
    store = FakeBackend(
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
    store = FakeBackend(
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
    assert run_async(make_memory(FakeBackend()).search_shared("q")) == []


def test_search_shared_degrades_returns_none():
    m = make_memory(FakeBackend(raise_on={"search"}))
    assert run_async(m.search_shared("q")) is None
    assert m.degraded is True


def test_get_shared_returns_shared_chunk():
    cid = "11111111-1111-1111-1111-111111111111"
    store = FakeBackend(get_results={cid: {"id": cid, "content": "c",
                                         "namespace": "shared", "source": "a.md", "tags": []}})
    out = run_async(make_memory(store).get_shared(cid))
    assert out["id"] == cid


def test_get_shared_refuses_nonshared_chunk_isolation():
    cid = "22222222-2222-2222-2222-222222222222"
    store = FakeBackend(get_results={cid: {"id": cid, "content": "PRIVATE",
                                         "namespace": "agent-runtime:claude"}})
    assert run_async(make_memory(store).get_shared(cid)) is None  # isolation guard


def test_get_shared_missing_id_returns_none():
    cid = "33333333-3333-3333-3333-333333333333"
    assert run_async(make_memory(FakeBackend()).get_shared(cid)) is None


def test_get_shared_malformed_id_not_found_without_degrading():
    m = make_memory(FakeBackend())
    assert run_async(m.get_shared("not-a-uuid")) is None
    assert m.degraded is False  # a client typo is not a backend failure


def test_get_shared_degrades_on_store_error():
    cid = "44444444-4444-4444-4444-444444444444"
    m = make_memory(FakeBackend(raise_on={"get"}))
    assert run_async(m.get_shared(cid)) is None
    assert m.degraded is True


def test_shared_stats_reports_shared_count():
    store = FakeBackend(existing_ns={"shared"},
                      ns_counts={"shared": 7, "agent-runtime:claude": 3})
    out = run_async(make_memory(store).shared_stats())
    assert out == {"namespace": "shared", "chunk_count": 7}


def test_shared_stats_zero_when_shared_absent():
    assert run_async(make_memory(FakeBackend()).shared_stats()) == {
        "namespace": "shared",
        "chunk_count": 0,
    }


def test_shared_stats_degrades_returns_none():
    m = make_memory(FakeBackend(raise_on={"count_namespace"}))
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
