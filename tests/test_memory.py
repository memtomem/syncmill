"""memory.py — search_context logic, best-effort writes, status, degradation,
and the run_async loop. No real memtomem is constructed."""

from __future__ import annotations

import asyncio
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


def test_search_context_is_one_unified_merged_search():
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
    # ONE merged-ranking backend call over both namespaces (mem_agent_search's
    # include_shared semantics), not two per-namespace calls.
    assert store.searched == [
        {"query": "q", "namespaces": ["agent-runtime:claude", "shared"], "top_k": 5}
    ]
    # dedup by id keeps the highest-scored copy — one relevance pool, no
    # per-leg precedence.
    assert "SHARED COPY" in out and "AGENT COPY" not in out
    # sorted by score desc: 0.99 before 0.5
    assert lines[0] == "[s.md] SHARED COPY"
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
    assert store.searched[0]["namespaces"] == ["shared"]


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


def test_register_agent_registers_metadata_and_ensures_shared():
    store = FakeBackend()
    run_async(make_memory(store).register_agent("claude"))
    # mem_agent_register semantics: the agent namespace gets an unconditional
    # description/color upsert (with the built-in defaults) …
    assert store.registered == [
        {
            "ns": "agent-runtime:claude",
            "description": "Agent: claude",
            "color": "#D97757",
        }
    ]
    # … while `shared` keeps its create-if-missing registration.
    assert store.set_calls == ["shared"]


def test_register_agent_explicit_description_and_color_win():
    store = FakeBackend()
    run_async(
        make_memory(store).register_agent(
            "claude", description="lead reviewer", color="#123456"
        )
    )
    assert store.registered[0]["description"] == "lead reviewer"
    assert store.registered[0]["color"] == "#123456"


def test_register_agent_unknown_id_leaves_color_unset():
    store = FakeBackend()
    run_async(make_memory(store).register_agent("mystery"))
    assert store.registered[0]["color"] is None


def test_register_agent_empty_string_clears_not_defaults():
    # memtomem semantics: None = leave unchanged, "" = clear the field. An
    # explicit "" must pass through verbatim, never be swapped for a default.
    store = FakeBackend()
    run_async(make_memory(store).register_agent("claude", description="", color=""))
    assert store.registered[0]["description"] == ""
    assert store.registered[0]["color"] == ""


def test_register_agent_idempotent_shared_but_upserts_agent_meta():
    store = FakeBackend(existing_ns={"agent-runtime:claude", "shared"})
    run_async(make_memory(store).register_agent("claude"))
    # shared already exists → no set_namespace_meta create; the agent namespace
    # is still re-registered (unconditional upsert, the mem_agent_register rule).
    assert store.set_calls == []
    assert [r["ns"] for r in store.registered] == ["agent-runtime:claude"]


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


def test_share_result_two_step_provenance_share():
    store = FakeBackend()
    task = Task(prompt="do")
    run_async(make_memory(store).share_result(task, _result(files=["a.py"])))
    # Step 1: the result lands in the producing agent's private namespace …
    original = store.added[0]
    assert original["namespace"] == "agent-runtime:claude"
    assert original["tags"] == ["task-result", "claude", f"task:{task.id}"]
    assert "**Files changed:** a.py" in original["content"]
    # … Step 2: it is COPIED to shared with mem_agent_share provenance.
    assert store.share_calls == [
        {"chunk_id": "fake-chunk-1", "target_namespace": "shared"}
    ]
    copy = store.added[1]
    assert copy["namespace"] == "shared"
    assert copy["content"] == original["content"]
    assert copy["tags"] == original["tags"] + ["shared-from=fake-chunk-1"]


def test_shared_payloads_never_open_with_a_markdown_heading():
    """The writer skips the unique per-entry heading it is given whenever the
    payload supplies its own `## ` one, and in that branch emits the tags
    blockquote BEFORE it. A heading-led payload therefore gave every result the
    SAME heading — merging consecutive results into one chunk, re-minting the
    ids `share_result` had already returned, and stranding the tags in the
    previous entry's section so `tag_filter` could not find the survivor.
    """
    store = FakeBackend()
    run_async(make_memory(store).share_result(Task(prompt="do"), _result()))
    run_async(
        make_memory(store).share_board_result("item-1", "task text", "output")
    )
    assert store.added, "nothing was written"
    for write in store.added:
        first = write["content"].lstrip().splitlines()[0]
        assert not first.startswith("## "), first
        # still self-labelling, just not as a section heading
        assert first.startswith("**")


def test_share_result_kind_tag_rides_both_writes():
    store = FakeBackend()
    task = Task(prompt="do")
    run_async(
        make_memory(store).share_result(task, _result(), kind="decompose-plan")
    )
    assert store.added[0]["tags"][0] == "decompose-plan"
    assert store.added[1]["tags"][0] == "decompose-plan"


def test_share_result_multi_chunk_shares_every_chunk():
    # a long result can split into several chunks — EVERY chunk must be
    # promoted to shared, not just the first (else collaborators get only the
    # prompt half of the content).
    store = FakeBackend(add_chunk_count=2)
    run_async(make_memory(store).share_result(Task(prompt="do"), _result()))
    assert [c["chunk_id"] for c in store.share_calls] == [
        "fake-chunk-1",
        "fake-chunk-2",
    ]
    shared_copies = [a for a in store.added if a["namespace"] == "shared"]
    assert len(shared_copies) == 2


def test_share_result_falls_back_to_direct_add_when_no_chunk_id():
    store = FakeBackend(add_ids_empty=True)
    m = make_memory(store)
    run_async(m.share_result(Task(prompt="do"), _result()))
    # add_returning_ids yielded no id → no share attempt, direct shared write.
    # A benign dedup edge, not a backend failure — memory stays healthy.
    assert store.share_calls == []
    assert store.added[-1]["namespace"] == "shared"
    assert m.degraded is False


def test_share_result_partial_share_degrades_without_duplicating():
    # Some chunks made it to `shared`, some didn't. Falling through to the direct
    # write would put a SECOND full copy beside the partial one — inflating
    # shared_stats and feeding near-duplicate rows to every reader — so the
    # incomplete provenance is surfaced instead.
    store = FakeBackend(add_chunk_count=2)
    calls = {"n": 0}
    real_share = store.share_chunk

    async def flaky_share(chunk_id, *, target_namespace):
        calls["n"] += 1
        if calls["n"] == 2:
            return None  # e.g. the source lookup came back empty
        return await real_share(chunk_id, target_namespace=target_namespace)

    store.share_chunk = flaky_share
    m = make_memory(store)
    run_async(m.share_result(Task(prompt="do"), _result()))
    shared_copies = [a for a in store.added if a["namespace"] == "shared"]
    assert len(shared_copies) == 1  # the one that succeeded, no extra full copy
    assert all("shared-from=" in "".join(a["tags"]) for a in shared_copies)
    assert m.degraded is True and "1 of 2" in m.last_error


def test_share_result_falls_back_when_share_chunk_raises_and_degrades():
    store = FakeBackend(raise_on={"share_chunk"})
    m = make_memory(store)
    run_async(m.share_result(Task(prompt="do"), _result()))
    # The provenance step broke mid-way; the share still lands directly — but
    # the lost original/link contract is SURFACED, not silently absorbed.
    assert store.added[-1]["namespace"] == "shared"
    assert "shared-from=" not in "".join(store.added[-1]["tags"])
    assert m.degraded is True and m.last_error


def test_share_result_blocked_content_skips_fallback():
    # a redaction-guard block would re-block in the direct fallback (same guard,
    # same content) — degrade once, write nothing, no duplicate audit noise.
    store = FakeBackend(block_writes=True)
    m = make_memory(store)
    run_async(m.share_result(Task(prompt="do"), _result()))
    assert store.added == []
    assert m.degraded is True


def test_share_result_two_step_and_fallback_both_dead_degrades():
    # the full degradation contract: provenance path AND direct write both
    # broken → degraded, no raise out of SyncmillMemory.
    store = FakeBackend(raise_on={"share_chunk", "add"})
    m = make_memory(store)
    run_async(m.share_result(Task(prompt="do"), _result()))  # must not raise
    assert m.degraded is True and m.last_error


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
    # share_result lands in the per-run scope, NOT global shared — as a SINGLE
    # direct write (never the provenance two-step: both namespaces collapse to
    # the scope, so a copy would be pure duplication)
    run_async(view.share_result(Task(prompt="enriched board prompt"), _result()))
    assert store.added[-1]["namespace"] == "board-run:R1"
    assert len(store.added) == 1 and store.share_calls == []
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
    # one unified call whose namespace union covers the scope + global shared
    assert store.searched == [
        {"query": "q", "namespaces": ["board-run:R1", "shared"], "top_k": 5}
    ]


def test_doubly_scoped_view_keeps_the_true_global_shared():
    # scoped() on an already-scoped parent must carry the parent's captured
    # global `shared` — the parent's `_shared_ns` IS its scratch, and a
    # grandchild union-reading another run's scratch as "shared" would leak
    # board-internal content across runs. Unreachable today (only the
    # Supervisor calls scoped, on the unscoped memory), guarded anyway.
    store = FakeBackend(search_results={"board-run:R2": [], "shared": []})
    grandchild = make_memory(store).scoped("R1").scoped("R2")
    assert grandchild._global_shared_ns == "shared"
    run_async(grandchild.search_context("q", agent_id="claude"))
    assert store.searched == [
        {"query": "q", "namespaces": ["board-run:R2", "shared"], "top_k": 5}
    ]


def test_scoped_view_refuses_the_gateway_shared_readers():
    # search_shared/get_shared/shared_stats are shared-only BY CONSTRUCTION: on
    # a scoped view `_shared_ns` is the board-run scratch, so they must report
    # unavailable (None) rather than serve scratch as shared — a scoped view
    # handed to build_gateway would otherwise pass every namespace check.
    store = FakeBackend(search_results={"shared": []})
    view = make_memory(store).scoped("R1")
    assert run_async(view.search_shared("q")) is None
    assert (
        run_async(view.get_shared("123e4567-e89b-42d3-a456-426614174000")) is None
    )
    assert run_async(view.shared_stats()) is None
    assert view.degraded is False  # a refusal, not a backend failure


def test_unscoped_memory_search_does_not_double_read_shared():
    # a plain (unscoped) run is UNCHANGED by D4: its `_shared_ns` IS global `shared`,
    # so it reads `shared` once (+ the agent scope), never a redundant union read.
    store = FakeBackend(search_results={"agent-runtime:claude": [], "shared": []})
    run_async(make_memory(store).search_context("q", agent_id="claude"))
    namespaces = store.searched[0]["namespaces"]
    assert namespaces.count("shared") == 1
    assert namespaces == ["agent-runtime:claude", "shared"]


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


def test_delete_board_runs_keeps_the_rows_when_sources_survive():
    # Sources go FIRST, so a failure there leaves the rows in place. That
    # ordering is what keeps a surviving file harmless until a retry: while its
    # rows exist a reindex recognizes the text and leaves its namespace alone,
    # whereas rows-first would strand the markdown with nothing claiming it and
    # the next pass would stamp board-internal text with whatever namespace it
    # resolves — into `shared`. Either way the run is not acknowledged, so it
    # stays collectable rather than being tombstoned.
    store = FakeBackend(
        existing_ns={"board-run:R1"}, ns_counts={"board-run:R1": 1}
    )

    async def _boom(ns):
        raise OSError("permission denied")

    store.delete_namespace_sources = _boom
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["R1"])) == set()
    assert store.deleted == []  # the rows outlive the file they protect
    assert m.degraded is True


def test_delete_board_runs_does_not_acknowledge_a_run_written_mid_purge():
    # Nothing locks a writer out between the source drop and the row delete, and
    # collecting a still-live run is a narrowed-not-closed race. A write landing
    # in that window creates a file AND its row; the row delete then takes the
    # row, stranding the markdown — the exact leak the ordering exists to stop.
    # The second sweep both removes it and disqualifies the acknowledgement, so
    # the namespace stays collectable instead of being tombstoned under a live
    # producer. Simulating the loser deterministically: the racing write lands
    # once, so the first sweep finds nothing and the second finds the new file.
    sweeps: list[str] = []
    store = FakeBackend(
        existing_ns={"board-run:R1"}, ns_counts={"board-run:R1": 1}
    )

    async def _sources(ns):
        sweeps.append(ns)
        return 0 if len(sweeps) == 1 else 1  # a writer got in behind us

    store.delete_namespace_sources = _sources
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["R1"])) == set()
    assert len(sweeps) == 2
    assert store.deleted == ["board-run:R1"]


def test_delete_board_runs_prefers_the_atomic_purge_when_the_backend_has_one():
    # A backend that can hold its writers out does BOTH halves itself, so the
    # caller must not also run the unlocked two-call fallback beside it.
    calls: list[str] = []
    store = FakeBackend(
        existing_ns={"board-run:R1"}, ns_counts={"board-run:R1": 1}
    )

    async def _purge(ns):
        calls.append(f"purge:{ns}")
        return 1

    async def _sources(ns):
        calls.append("sources")
        return 0

    store.purge_namespace = _purge
    store.delete_namespace_sources = _sources
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["R1"])) == {"R1"}
    assert calls == ["purge:board-run:R1"]
    assert store.deleted == []  # the purge owns the row delete too


def test_delete_board_runs_leaves_an_unprovable_purge_collectable():
    # `purge_namespace` raises when it cannot prove the namespace is gone (a
    # writer it cannot exclude landed a file). Acknowledging that would
    # tombstone a namespace with a live producer, retiring it from collection
    # while its text is still re-indexable.
    store = FakeBackend(
        existing_ns={"board-run:R1"}, ns_counts={"board-run:R1": 1}
    )

    async def _purge(ns):
        raise RuntimeError("not provably complete")

    store.purge_namespace = _purge
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["R1"])) == set()
    assert m.degraded is True


def test_delete_board_runs_drops_sources_before_rows():
    # The ordering is the guarantee, so pin the sequence itself rather than
    # only its failure mode.
    order: list[str] = []
    store = FakeBackend(
        existing_ns={"board-run:R1"}, ns_counts={"board-run:R1": 1}
    )
    real_delete = store.delete_namespace

    async def _sources(ns):
        order.append("sources")
        return 0  # a quiet namespace: nothing left for the verify sweep

    async def _rows(ns):
        order.append("rows")
        return await real_delete(ns)

    store.delete_namespace_sources = _sources
    store.delete_namespace = _rows
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["R1"])) == {"R1"}
    # The trailing sweep is the mid-purge-write check; the guarantee under test
    # is that a source drop precedes the row delete.
    assert order == ["sources", "rows", "sources"]


def test_delete_board_runs_reachable_but_every_delete_fails_returns_zero():
    store = FakeBackend(
        ns_counts={"board-run:a": 1, "board-run:b": 1},
        delete_raise_ns={"board-run:a", "board-run:b"},
    )
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["a", "b"])) == set()
    assert m.degraded is True


def test_delete_board_runs_zero_chunk_call_is_acknowledged():
    # A registered-but-never-written scratch namespace deletes 0 CHUNKS and is
    # still fully purged (memtomem drops the metadata row regardless), so the
    # ledger must acknowledge it rather than leaving it a candidate forever.
    # Not an error either: the memory stays healthy.
    store = FakeBackend(existing_ns={"board-run:meta-only"})  # no ns_counts → 0 chunks
    m = make_memory(store)
    assert run_async(m.delete_board_runs(["meta-only"])) == {"meta-only"}
    assert m.degraded is False
    assert "board-run:meta-only" not in store.meta  # nothing left to re-collect


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


def test_close_event_loop_shuts_down_async_generators():
    # Closing the loop without draining async generators leaves backend
    # resources (e.g. aiosqlite handles) finalized against a dead loop — the
    # exact error class the shared loop exists to avoid. The finally block of
    # a suspended asyncgen must run before the loop closes.
    finalized = {}

    async def agen():
        try:
            yield 1
            yield 2
        finally:
            finalized["done"] = True

    gen = agen()

    async def first():
        return await gen.__anext__()

    assert run_async(first()) == 1
    mem.close_event_loop()
    assert finalized.get("done") is True


def test_close_event_loop_cancels_pending_tasks():
    # A SIGINT escapes run_until_complete WITHOUT cancelling the in-flight
    # future, stranding fan-out tasks (with live agent subprocesses) pending on
    # the reused module loop. close_event_loop must cancel + drain them — each
    # cancellation runs the task's cleanup (aexecute's kill/reap tail) — rather
    # than destroy them un-awaited at loop.close().
    state = {"cancelled": False}

    async def hang():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def strand():
        # Schedule and return without awaiting — the Ctrl-C-mid-fan-out shape.
        asyncio.ensure_future(hang())

    run_async(strand())
    mem.close_event_loop()
    assert state["cancelled"] is True


def test_run_memory_bounded_times_out_degrades_and_returns_default():
    # A HUNG backend never raises, so SyncmillMemory's own except-nets never
    # fire — the bound is what keeps "memory never blocks the task" true.
    m = make_memory(FakeBackend())

    async def hang():
        await asyncio.sleep(3600)

    out = mem.run_memory_bounded(
        hang(), default="fallback", memory=m, label="search_context", timeout=0.05
    )
    assert out == "fallback"
    assert m.degraded is True  # surfaced like any other best-effort failure


def test_run_memory_bounded_passes_results_through():
    async def quick():
        return "value"

    assert mem.run_memory_bounded(quick(), default=None) == "value"


def test_degraded_clears_on_a_later_fully_successful_operation():
    # Latest-outcome semantics (the breaker's rule): a transient failure during
    # a long-lived `--loop` process must not report the memory surface degraded
    # forever after the backend recovered.
    backend = FakeBackend(raise_on={"search"})
    m = make_memory(backend)
    assert run_async(m.search_context("q")) == ""
    assert m.degraded is True
    backend._raise_on = set()  # the backend recovered
    run_async(m.search_context("q"))
    assert m.degraded is False and m.last_error is None


def test_partial_share_stays_degraded_after_its_own_call():
    # A PARTIAL share is an unmet contract for THIS call — the fallback write
    # after a provenance failure must not clear the degrade it just recorded.
    backend = FakeBackend(raise_on={"share_chunk"})
    m = make_memory(backend)
    run_async(m.share_result(Task(prompt="p"), _result()))
    assert m.degraded is True


def test_run_async_cancels_its_future_on_interrupt():
    # A KeyboardInterrupt escapes run_until_complete with the in-flight future
    # still pending on the REUSED loop; a later run_async (Supervisor.close, a
    # room finalizer) would resume the stranded fan-out mid-teardown. run_async
    # must cancel + drain it before propagating.
    state = {"cancelled": False}

    async def hang():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    run_async(asyncio.sleep(0))  # materialize the module loop
    loop = mem._loop

    def boom():
        raise KeyboardInterrupt

    loop.call_later(0.01, boom)
    with pytest.raises(KeyboardInterrupt):
        run_async(hang())
    assert state["cancelled"] is True
    # And the loop is healthy for the next caller (the finalizer path).
    assert run_async(asyncio.sleep(0, result="ok")) == "ok"


def test_run_async_coroutine_exception_passes_straight_through():
    async def broken():
        raise ValueError("from the coroutine")

    with pytest.raises(ValueError, match="from the coroutine"):
        run_async(broken())


# ── review 2026-09-10 regressions ────────────────────────────────────────


def test_empty_sweep_does_not_clear_a_prior_degrade():
    # all([]) is True, so an empty sweep called _mark_healthy() having done no
    # backend work at all — silently retracting the operator's degrade notice.
    mem = make_memory(FakeBackend())
    mem._degrade("share_result", RuntimeError("provenance lost"))
    assert mem.degraded

    assert run_async(mem.delete_board_runs([])) == set()
    assert mem.degraded, "an empty sweep is not a successful backend operation"


def test_share_board_result_refuses_on_a_scoped_view():
    # The only shared-facing method without the scope guard: on a board-scoped
    # view `_shared_ns` IS the board-run scratch, so the one cross-link meant to
    # outlive the run would land in the namespace the next GC pass deletes —
    # and report success doing it.
    backend = FakeBackend()
    mem = make_memory(backend)
    scoped = mem.scoped("run-1")

    assert run_async(scoped.share_board_result("item-1", "task", "output")) is False
    assert backend.added == []


def test_search_context_window_defaults_and_widens():
    # The default window (5) is SMALLER than decompose.max_subtasks (8), so a
    # default decompose asked for fewer rows than it had subtask results and
    # could not surface three of its own pieces — in the one strategy whose job
    # is integrating every piece. A caller that knows how many artifacts it just
    # published can now ask for that many.
    store = FakeBackend()
    mem = make_memory(store)
    run_async(mem.search_context("q"))
    assert store.searched[-1]["top_k"] == 5
    run_async(mem.search_context("q", top_k=8))
    assert store.searched[-1]["top_k"] == 8


def test_search_context_window_is_clamped_both_ways():
    # max_subtasks is free-form config: an unbounded request would issue a huge
    # search whose rows the prompt cap discards anyway.
    store = FakeBackend()
    mem = make_memory(store)
    run_async(mem.search_context("q", top_k=10_000))
    assert store.searched[-1]["top_k"] == 50
    run_async(mem.search_context("q", top_k=0))
    assert store.searched[-1]["top_k"] == 1


def test_search_context_renders_the_whole_widened_window():
    # The row slice must follow the requested window, not the default constant,
    # or a widened search would fetch more rows and then throw them away.
    rows = [
        {"id": str(i), "content": f"row{i}", "score": 1.0 - i / 100, "source": f"s{i}"}
        for i in range(8)
    ]
    mem = make_memory(FakeBackend(search_results={"shared": rows}))
    assert len(run_async(mem.search_context("q", top_k=8)).split("\n---\n")) == 8
    assert len(run_async(mem.search_context("q")).split("\n---\n")) == 5


def test_atomic_gc_failure_then_success_clears_degraded():
    backend = FakeBackend()
    memory = make_memory(backend)
    fail = True

    async def purge(ns):
        if fail:
            raise RuntimeError("partial purge")
        return 0

    backend.purge_namespace = purge
    assert run_async(memory.delete_board_runs(["r1"])) == set()
    assert memory.degraded
    fail = False
    assert run_async(memory.delete_board_runs(iter(["r1"]))) == {"r1"}
    assert not memory.degraded


def test_gc_budget_cancels_cooperative_purge_and_preserves_completed_ids(monkeypatch):
    import syncmill.memory as module

    backend = FakeBackend()
    memory = make_memory(backend)
    calls = []
    released = []

    async def purge(ns):
        calls.append(ns)
        if ns.endswith(":first"):
            return 0
        try:
            await asyncio.sleep(60)
        finally:
            released.append(ns)

    backend.purge_namespace = purge
    monkeypatch.setattr(module, "_BOARD_GC_BUDGET_SECONDS", 0.03)
    result = run_async(memory.purge_board_runs(["first", "slow", "untried"]))
    assert result.successful == {"first"}
    assert result.failed == {"slow"}
    assert result.unattempted == {"untried"}
    assert calls == ["board-run:first", "board-run:slow"]
    assert released == ["board-run:slow"]
    assert memory.degraded
    # Re-entering the reused loop cannot resume an abandoned purge.
    run_async(asyncio.sleep(0))
    assert calls == ["board-run:first", "board-run:slow"]
