"""Mem0 and LangGraph stores written by released syncmill, read by HEAD (#271).

Opt-in (``SYNCMILL_TEST_MEMORY_BACKENDS_REAL=1``; CI's
``memory-backends-real`` job runs it): it needs the ``mem0`` and ``langgraph``
extras and a FastEmbed cache (``FASTEMBED_CACHE_PATH``) holding the exact model
files the stores were written with (``models.json``; checked first, read
offline). HEAD's ``Mem0Backend`` / ``LangGraphBackend`` and everything above
them (``SyncmillMemory``, the gateway, ``status``, the strategies, a room
round, the board's scratch collection) work on a copy of a store the release
wrote. ``test_frozen_memory_backends.py`` holds the stored bytes to the scenario
definitions without an SDK; this module holds HEAD's readers to those
definitions and to what the release itself read (``release-read.json``).

Neither store records where it was written, so a moved copy is the whole
rebinding.

Search oracles. These backends rank by embedding (Mem0 adds BM25), so a
scenario token is not a query whose first hit is known for LangGraph: its dense
model cannot tell ``tokroute`` from ``tokcompete``. Every row is therefore
searched by its own content, in its own namespace, and must come first; tag
filters are checked as exact sets with a ``top_k`` larger than the store; and a
ranking is compared with the release's only where scores differ by more than
``SCORE_TOLERANCE``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

import syncmill.supervisor as S
from syncmill.board import Board
from syncmill.cli import cli
from syncmill.config import MemoryConfig, SyncmillConfig
from syncmill.gateway import build_gateway
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult
from syncmill.rooms import RoomCoordinator
from syncmill.strategies.compete import CompeteStrategy
from syncmill.strategies.council import CouncilStrategy
from syncmill.strategies.decompose import DecomposeStrategy
from syncmill.strategies.pipeline import PipelineStrategy
from syncmill.strategies.route import RouteStrategy
from syncmill.supervisor import Supervisor
from syncmill.worktree import CapturedChanges

from conftest import FakeAgent, FakeRegistry, FakeWorktreePool
from frozen_memory_backends_support import (
    BACKEND_RELEASES,
    BackendsFrozen,
    StoreCopy,
    ordered_prefix,
)

BACKENDS = ("mem0", "langgraph")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SYNCMILL_TEST_MEMORY_BACKENDS_REAL") != "1",
        reason="set SYNCMILL_TEST_MEMORY_BACKENDS_REAL=1 to run the real Mem0/LangGraph tests",
    ),
    pytest.mark.parametrize("release", BACKEND_RELEASES),
    pytest.mark.parametrize("backend", BACKENDS),
]

#: HEAD's default board lease.
LEASE = 1800
#: Larger than any frozen store: a search with it is not cut by ``top_k``.
ALL = 1000
#: Mem0's keyword-search pool for the ``top_k=3`` searches these tests make
#: (``AsyncMemory.search``: ``max(limit * 4, 60)``).
MEM0_KEYWORD_POOL = 60
#: How ``search_context`` joins the rows it returns.
CONTEXT_SEPARATOR = "\n---\n"
LATER_PHASE_FILTERS = {
    "pipeline-stage": {"pipeline-stage"},
    "council-proposal": {"council-proposal"},
    "council-proposal,council-critique": {"council-proposal", "council-critique"},
    "decompose-plan,decompose-subtask": {"decompose-plan", "decompose-subtask"},
    "decompose-subtask": {"decompose-subtask"},
}


#: The FastEmbed model files every frozen store was embedded with (each
#: release's ``models.json``; ``fetch_models.py`` refuses releases that differ).
MODELS = BackendsFrozen(BACKEND_RELEASES[0]).models


@pytest.fixture(scope="module", autouse=True)
def fixtures_stay_as_frozen():
    assert os.environ.get("FASTEMBED_CACHE_PATH"), (
        "FASTEMBED_CACHE_PATH must point at the frozen FastEmbed models")
    for release in BACKEND_RELEASES:
        assert BackendsFrozen(release).models == MODELS, release
    yield
    for release in BACKEND_RELEASES:
        BackendsFrozen(release).verify_pins()


def assert_frozen_model(model_dir: str | Path) -> None:
    """A model FastEmbed LOADED is the frozen one: its directory is the
    recorded revision's snapshot and every recorded file there has the
    recorded bytes. Asked of the loaded model rather than derived from the
    cache layout, so a fallback to another directory cannot pass."""
    path = Path(model_dir)
    model = next((part for part in path.parts if part.startswith("models--")), None)
    assert model in MODELS, path
    spec = MODELS[model]
    assert path.parent.name == "snapshots" and path.name == spec["revision"], path
    for entry in spec["files"]:
        name, digest = entry["name"], entry["sha256"]
        assert hashlib.sha256((path / name).read_bytes()).hexdigest() == digest, (model, name)


@pytest.fixture(autouse=True)
def engine_environment(monkeypatch, tmp_path):
    """Offline, with a home of its own: Mem0 writes ``~/.mem0`` and both SDKs
    would otherwise reach the model hub."""
    home = (tmp_path / "home").resolve()
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("MEM0_TELEMETRY", "false")


class _Engine:
    """HEAD's memory over one store copy, closed before anyone looks at the
    files again."""

    def __init__(self, store: StoreCopy):
        self.store = store
        self.memory = SyncmillMemory(MemoryConfig(**store.memory_config()))
        assert self.memory._backend is not None, self.memory.last_error
        self.backend = self.memory._backend

    def __enter__(self) -> "_Engine":
        run_async(self.backend.ensure_ready())
        return self

    def loaded_models(self) -> list[str]:
        """The directories of the FastEmbed models this engine has loaded."""
        if self.store.backend == "mem0":
            dirs = [self.backend._store.embedding_model.dense_model.model._model_dir]
            bm25 = self.backend._store.vector_store._bm25_encoder
            if bm25 is not None:  # loaded by the first keyword search
                dirs.append(bm25.model._model_dir)
            return [str(d) for d in dirs]
        store = self.backend._store
        return [] if store is None else [str(store.embeddings.model.model._model_dir)]

    def __exit__(self, *exc) -> None:
        try:
            if exc[0] is None:
                loaded = self.loaded_models()
                assert loaded, "no FastEmbed model was loaded"
                for model_dir in loaded:
                    assert_frozen_model(model_dir)
        finally:
            run_async(self.memory.close())

    def healthy(self) -> None:
        assert not self.memory.degraded and self.memory.last_error is None


def _ids(rows) -> list[str]:
    return sorted(row["id"] for row in rows)


def _hits(rows) -> list[dict]:
    return [{"id": r["id"], "score": r["score"]} for r in rows]


def _rows(frozen: BackendsFrozen, store: str) -> dict[str, dict]:
    return frozen.index["rows"][store]


def _owned(rows: dict[str, dict]) -> dict[str, dict]:
    """The rows syncmill wrote (the foreign ones have no tag list or no
    namespace)."""
    return {i: r for i, r in rows.items() if r["namespace"] and isinstance(r["tags"], list)}


def _one(frozen: BackendsFrozen, store: str, scenario: str, namespace: str) -> str:
    (row_id,) = frozen.scenario_ids(store, scenario, namespace)
    return row_id


#: v0.1.2 wrote every result straight into `shared`, tagged `task-result` and
#: the agent only: no private row, no later-phase kind, no `search_multi`.
DIRECT_SHARE = {"v0.1.2"}


def _private(frozen: BackendsFrozen, store: str, scenario: str) -> str | None:
    """The scenario's private row, or None on a release that wrote none."""
    ids = frozen.scenario_ids(store, scenario, "agent-runtime:codex")
    assert len(ids) == (0 if frozen.release in DIRECT_SHARE else 1), ids
    return ids[0] if ids else None


def _killed_scratch(frozen: BackendsFrozen, store: str) -> tuple[str, str]:
    """The SIGKILLed board run's id and its one scratch row. It is the scratch
    row every release kept: v0.1.x collected the finished run's namespace when
    the killed run claimed."""
    killed = frozen.scenario(store, "board-killed")
    (row_id,) = killed["ids"]
    return killed["run_id"], row_id


# ── adapter reads ────────────────────────────────────────────────────────────


def test_get_and_count_read_every_released_row(release, backend, tmp_path):
    frozen = BackendsFrozen(release)
    for name in frozen.stores(backend):
        # The reference rows come from a second copy, never opened by an SDK.
        reference = frozen.copy(name, tmp_path / "reference").rows()
        store = frozen.copy(name, tmp_path / "opened")
        released = frozen.read[name]
        rows = _rows(frozen, name)
        foreign = (
            frozen.scenario(name, "foreign")["foreign_ids"] if name.endswith("/store") else {}
        )
        for _ in range(2):  # the second open finds a store HEAD already opened
            with _Engine(store) as engine:
                exact: list = []
                if backend == "mem0":
                    # Qdrant local counts exactly whatever it is asked; the
                    # argument is what a server would honour, so it is watched.
                    client = engine.backend._qdrant_store().client
                    real_count = client.count

                    def count(*args, **kwargs):
                        exact.append(kwargs.get("exact"))
                        return real_count(*args, **kwargs)

                    client.count = count
                for row_id, row in rows.items():
                    got = run_async(engine.backend.get(row_id))
                    if row_id == foreign.get("unowned"):
                        # No namespace: not a syncmill row, never returned.
                        assert got is None and released["get"][row_id] is None
                        continue
                    want = released["get"][row_id]
                    assert {k: got[k] for k in want} == want, row_id
                    # HEAD's tags come from the stored payload, not from the
                    # release's reading.
                    assert got["tags"] == (row["tags"] or []), row_id
                    assert got["namespace"] == row["namespace"] and got["content"] == row["content"]
                for ns, released_count in released["count"].items():
                    stored = sum(1 for r in rows.values() if r["namespace"] == ns)
                    got_count = run_async(engine.backend.count_namespace(ns))
                    assert got_count == released_count == stored, ns
                if backend == "mem0":
                    assert exact and set(exact) == {True}, exact
                engine.healthy()
            # Reading changes no stored row.
            assert store.rows() == reference, name


def test_each_row_is_found_first_by_its_own_content(release, backend, tmp_path):
    frozen = BackendsFrozen(release)
    for name in frozen.stores(backend):
        store = frozen.copy(name, tmp_path)
        rows = _rows(frozen, name)
        released = frozen.read[name]["search_self"]
        owned = set(_owned(rows))
        if name.endswith("/store"):
            assert set(released) == owned, name  # every row syncmill wrote
        else:
            assert set(released) <= owned and len(released) >= 20, name
        with _Engine(store) as engine:
            for row_id, release_hits in released.items():
                row = rows[row_id]
                found = run_async(engine.backend.search(
                    row["content"], namespace=row["namespace"], top_k=3))
                assert found and found[0]["id"] == row_id, (name, row_id, found)
                assert {r["namespace"] for r in found} == {row["namespace"]}
                assert len(found) == min(3, sum(
                    1 for r in _owned(rows).values() if r["namespace"] == row["namespace"]
                ) + (1 if row["namespace"] == "shared" and name.endswith("/store") else 0))
                # The release's ranking, where its scores make it one. Mem0
                # adds a row's BM25 score only when the keyword search ranks
                # it among its first max(4 * top_k, 60) hits; the bulk rows tie
                # there, so which of them get it depends on the store's order
                # of ties, and past the first hit their ranking differed
                # between macOS and Linux. Such a namespace pins the first hit
                # only.
                in_ns = sum(1 for r in rows.values() if r["namespace"] == row["namespace"])
                if backend == "mem0" and in_ns > MEM0_KEYWORD_POOL:
                    continue
                prefix = ordered_prefix(release_hits)
                assert [r["id"] for r in found][: len(prefix)] == prefix, (name, row_id)
            engine.healthy()


def test_mem0_finds_a_scenario_by_its_token_through_bm25(release, backend, tmp_path):
    """Mem0 searches hybrid. A scenario's own token is a word only BM25 can
    match, so its rows rank first, and the keyword half of the production search
    must have run and found them."""
    if backend != "mem0":
        pytest.skip("LangGraph searches by dense embedding only")
    frozen = BackendsFrozen(release)
    name = "mem0/store"
    store = frozen.copy(name, tmp_path)
    rows = _rows(frozen, name)
    released = frozen.read[name]["search"]
    with _Engine(store) as engine:
        vector_store = engine.backend._store.vector_store
        keyword_calls: list = []
        real = vector_store.keyword_search

        def keyword_search(query, top_k=5, filters=None):
            result = real(query, top_k=top_k, filters=filters)
            keyword_calls.append((query, result))
            return result

        vector_store.keyword_search = keyword_search
        checked = 0
        pairs = sum(
            1 for ns in ("shared", "agent-runtime:codex") for token in released[ns]
            if any(r["namespace"] == ns and token in r["content"].split()
                   for r in _owned(rows).values())
        )
        for ns in ("shared", "agent-runtime:codex"):
            for token, release_hits in released[ns].items():
                targets = {
                    i for i, r in _owned(rows).items()
                    if r["namespace"] == ns and token in r["content"].split()
                }
                keyword_calls.clear()
                found = run_async(engine.backend.search(token, namespace=ns, top_k=3))
                assert keyword_calls and all(q == token for q, _ in keyword_calls)
                if not targets:
                    continue
                checked += 1
                lead = [r["id"] for r in found][: min(3, len(targets))]
                assert set(lead) <= targets, (ns, token)
                assert any(result for _, result in keyword_calls), (ns, token)
                assert [h["id"] for h in release_hits][: len(lead)] == lead or (
                    set(h["id"] for h in release_hits[: len(lead)]) == set(lead)
                ), (ns, token)
        assert vector_store._bm25_encoder is not None
        assert len(engine.loaded_models()) == 2  # dense and BM25, checked on exit
        assert checked == pairs >= 9
        engine.healthy()


def test_tag_filters_select_exactly_the_released_rows_of_those_kinds(release, backend, tmp_path):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path)
    rows = _owned(_rows(frozen, name))
    released = frozen.read[name]
    kinds_seen = set()
    bounded_seen = False
    with _Engine(store) as engine:
        search = engine.backend.search
        for ns in sorted({r["namespace"] for r in rows.values()}):
            in_ns = {i for i, r in _rows(frozen, name).items() if r["namespace"] == ns}
            found = run_async(search(frozen.anchor, namespace=ns, top_k=ALL))
            assert _ids(found) == sorted(in_ns) == _ids(released["search_all"][ns]), ns
            assert {r["namespace"] for r in found} == {ns}
            for kind in released["search_by_kind"][ns]:
                expected = sorted(i for i in in_ns if kind in (rows.get(i, {}).get("tags") or []))
                got = run_async(search(frozen.anchor, namespace=ns, top_k=ALL, tag_filter=kind))
                assert _ids(got) == expected == _ids(released["search_by_kind"][ns][kind])
            for wanted, kinds in LATER_PHASE_FILTERS.items():
                expected = sorted(i for i in in_ns if kinds & set(rows.get(i, {}).get("tags") or []))
                got = run_async(search(frozen.anchor, namespace=ns, top_k=ALL, tag_filter=wanted))
                assert _ids(got) == expected, (ns, wanted)
                if expected:
                    kinds_seen.add(wanted)
                # Cut at top_k, a filter of two kinds included (one search per
                # kind is merged before the cut).
                if len(expected) > 3:
                    bounded = run_async(search(frozen.anchor, namespace=ns, top_k=3,
                                               tag_filter=wanted))
                    assert len(bounded) == 3 and set(_ids(bounded)) <= set(expected), (
                        ns, wanted)
                    bounded_seen = True
            assert run_async(search(
                frozen.anchor, namespace=ns, top_k=ALL, tag_filter=released["spec"]["absent"]
            )) == []
            # A bounded search returns exactly top_k rows when there are more.
            if len(in_ns) > 3:
                assert len(run_async(search(frozen.anchor, namespace=ns, top_k=3))) == 3
        engine.healthy()
    if release in DIRECT_SHARE:
        assert kinds_seen == set() and not bounded_seen  # no later-phase kind was written
    else:
        assert kinds_seen == set(LATER_PHASE_FILTERS)
        assert bounded_seen


def test_search_multi_merges_dedupes_and_cuts(release, backend, tmp_path):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path)
    released = frozen.read[name]
    # v0.1.2's adapters had no search_multi; HEAD's merge is held to the
    # release's own per-namespace searches either way.
    assert (released["search_multi"] is None) == (release in DIRECT_SHARE)
    with _Engine(store) as engine:
        for token in released["spec"]["tokens"]:
            multi = (released["search_multi"] or {}).get(token)
            # The release's own per-namespace searches, merged by the rule
            # written out here (dedupe by id keeping the best score, best
            # first, cut at top_k).
            merged: dict[str, float] = {}
            for ns in ("shared", "agent-runtime:codex"):
                for hit in released["search"][ns][token]:
                    merged[hit["id"]] = max(hit["score"], merged.get(hit["id"], hit["score"]))
            expected = sorted(merged, key=lambda i: -merged[i])[:3]
            two = run_async(engine.backend.search_multi(
                token, namespaces=["shared", "agent-runtime:codex"], top_k=3))
            assert sorted(r["id"] for r in two) == sorted(expected), token
            # Best first, where the release's scores make an order.
            prefix = ordered_prefix([{"id": i, "score": merged[i]} for i in expected])
            assert [r["id"] for r in two][: len(prefix)] == prefix, token
            if multi is not None:
                assert sorted(expected) == sorted(h["id"] for h in multi["two"]), token
            repeated = run_async(engine.backend.search_multi(
                token, namespaces=["shared", "shared"], top_k=3))
            ids = [r["id"] for r in repeated]
            assert len(ids) == len(set(ids)) == 3, token
            assert sorted(ids) == sorted(h["id"] for h in released["search"]["shared"][token])
        engine.healthy()


def test_status_reads_a_released_store(release, backend, tmp_path, monkeypatch):
    frozen = BackendsFrozen(release)
    store = frozen.copy(f"{backend}/store", tmp_path)
    before = store.rows()
    if backend == "mem0":
        monkeypatch.setenv("SYNCMILL_MEMORY__BACKEND", "mem0")
        monkeypatch.setenv("SYNCMILL_MEMORY__MEM0__DATA_DIR", str(store.path))
        expected = [f"data_dir: {store.path}", "vector_store: qdrant", "collection: syncmill"]
    else:
        monkeypatch.setenv("SYNCMILL_MEMORY__BACKEND", "langgraph")
        monkeypatch.setenv("SYNCMILL_MEMORY__LANGGRAPH__DB_PATH", str(store.path))
        expected = [f"db_path: {store.path}", "store: langgraph/AsyncSqliteStore"]
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert f"{backend}: connected" in result.output
    for line in expected:
        assert line in result.output, result.output
    assert store.rows() == before


# ── writes beside released rows ──────────────────────────────────────────────


def test_sharing_a_released_row_copies_its_tags_and_names_it(release, backend, tmp_path):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path)
    before = store.rows()
    sources = [
        source for source in (
            _private(frozen, name, "route"),
            _killed_scratch(frozen, name)[1],
            _one(frozen, name, "route", "shared"),
        ) if source is not None
    ]
    made = {}
    with _Engine(store) as engine:
        for source in sources:
            new_id = run_async(engine.backend.share_chunk(source, target_namespace="shared"))
            assert new_id and new_id not in before
            made[new_id] = source
        engine.healthy()
    after = store.rows()
    assert {i: r for i, r in after.items() if i in before} == before
    assert set(after) == set(before) | set(made)
    for new_id, source in made.items():
        copy, original = after[new_id], before[source]
        assert copy["namespace"] == "shared" and copy["content"] == original["content"]
        kept = [t for t in original["tags"] if not t.startswith("shared-from=")]
        assert sorted(copy["tags"]) == sorted([*kept, f"shared-from={source}"])


# ── SyncmillMemory and its consumers ─────────────────────────────────────────


def _bodies(rows: dict[str, dict], ids) -> set[str]:
    return {rows[i]["content"] for i in ids}


def test_search_context_returns_the_released_bodies_a_phase_is_entitled_to(
    release, backend, tmp_path
):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path)
    rows = _rows(frozen, name)
    owned = _owned(rows)
    shared = {i for i, r in owned.items() if r["namespace"] == "shared"}
    elsewhere = _bodies(rows, set(owned) - shared) - _bodies(rows, shared)
    assert elsewhere
    with _Engine(store) as engine:
        memory = engine.memory

        def context(**kwargs) -> str:
            text = run_async(memory.search_context(frozen.anchor, max_bytes=10**7, **kwargs))
            engine.healthy()
            return text

        everything = context(top_k=ALL)
        assert all(body in everything for body in _bodies(rows, shared))
        assert not any(body in everything for body in elsewhere)
        for wanted, kinds in LATER_PHASE_FILTERS.items():
            entitled = {i for i in shared if kinds & set(owned[i]["tags"])}
            text = context(top_k=ALL, tag_filter=wanted)
            # v0.1.2 wrote no later-phase kind: such a phase recalls nothing.
            assert bool(entitled) == (release not in DIRECT_SHARE), wanted
            assert all(b in text for b in _bodies(rows, entitled)), wanted
            others = _bodies(rows, shared - entitled) - _bodies(rows, entitled)
            assert not any(b in text for b in others), wanted
        # The default limits return a handful of released rows, never none.
        default = run_async(memory.search_context(frozen.anchor))
        assert 1 <= sum(1 for b in _bodies(rows, shared) if b in default) <= 5
        # With an agent the private namespace is searched too: the route result
        # is then found as the private row and as its copy (v0.1.2 wrote no
        # private row).
        route = rows[_one(frozen, name, "route", "shared")]["content"]
        assert run_async(memory.search_context(route, top_k=ALL, max_bytes=10**7)).count(route) == 1
        with_agent = 1 if _private(frozen, name, "route") is None else 2
        assert run_async(memory.search_context(
            route, agent_id="codex", top_k=ALL, max_bytes=10**7)).count(route) == with_agent
        engine.healthy()


def test_shared_reads_never_disclose_a_private_or_scratch_row(release, backend, tmp_path):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path)
    rows = _rows(frozen, name)
    shared = {i for i, r in rows.items() if r["namespace"] == "shared"}
    private = _private(frozen, name, "route")
    run_id, scratch = _killed_scratch(frozen, name)
    with _Engine(store) as engine:
        memory = engine.memory
        found = run_async(memory.search_shared(frozen.anchor, top_k=ALL))
        assert _ids(found) == sorted(shared)
        for row_id in shared:
            read = run_async(memory.get_shared(row_id))
            assert read.status == "found" and read.chunk["content"] == rows[row_id]["content"]
        for hidden in (h for h in (private, scratch) if h is not None):
            assert run_async(engine.backend.get(hidden))["content"] == rows[hidden]["content"]
            read = run_async(memory.get_shared(hidden))
            assert (read.status, read.chunk) == ("not_found", None)
        assert run_async(memory.get_shared("not-a-row-id")).status == "not_found"
        assert run_async(memory.shared_stats()) == {"namespace": "shared", "chunk_count": len(shared)}
        engine.healthy()

        # A board-scoped view reads its scratch namespace and `shared` for
        # context, and refuses every shared-reader method.
        view = memory.scoped(run_id)
        route = rows[_one(frozen, name, "route", "shared")]["content"]
        text = run_async(view.search_context(frozen.anchor, top_k=ALL, max_bytes=10**7))
        assert rows[scratch]["content"] in text and route in text
        assert run_async(view.search_shared(frozen.anchor, top_k=ALL)) is None
        assert run_async(view.get_shared(scratch)).status == "unavailable"
        assert run_async(view.get_shared(next(iter(shared)))).status == "unavailable"
        assert run_async(view.shared_stats()) is None
        gateway = build_gateway(view)
        assert _call(gateway, "shared_search", {"query": frozen.anchor}) == (
            "shared memory is currently unavailable")
        assert _call(gateway, "shared_read", {"chunk_id": scratch}) == (
            "shared memory is currently unavailable")
        assert _call(gateway, "shared_stats", {}) == "shared memory is currently unavailable"
        assert not view.degraded and view.last_error is None


def _call(mcp, name: str, args: dict) -> str:
    return run_async(mcp.call_tool(name, args)).structured_content["result"]


def test_the_gateway_serves_the_released_shared_rows(release, backend, tmp_path):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path)
    rows = _rows(frozen, name)
    shared = _one(frozen, name, "unicode", "shared")
    private = _private(frozen, name, "unicode") or _killed_scratch(frozen, name)[1]
    with _Engine(store) as engine:
        gateway = build_gateway(engine.memory)
        out = _call(gateway, "shared_search", {"query": rows[shared]["content"]})
        assert shared in out and private not in out
        assert rows[shared]["content"] in _call(gateway, "shared_read", {"chunk_id": shared})
        hidden = _call(gateway, "shared_read", {"chunk_id": private})
        assert hidden == f"Chunk {private} not found in shared memory."
        in_shared = sum(1 for r in rows.values() if r["namespace"] == "shared")
        assert _call(gateway, "shared_stats", {}) == f"shared: {in_shared} chunks"
        engine.healthy()


class _Scripted(FakeAgent):
    """A fake agent whose replies are scripted per call (the last repeats);
    each call is appended to ``events`` as ``("call", prompt)``."""

    def __init__(self, agent_id: str, replies: list[str], events: list | None = None):
        super().__init__(agent_id)
        self._replies = replies
        self._events = events

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        self._output = self._replies[min(len(self.calls), len(self._replies) - 1)]
        if self._events is not None:
            self._events.append(("call", prompt))
        return super().execute(prompt, workdir, timeout)


@pytest.fixture
def pool(monkeypatch):
    """No worktree and no git under a strategy: the store is what is real."""
    fake = FakeWorktreePool(is_git=True)
    monkeypatch.setattr(S, "WorktreePool", lambda repo_root, task_id: fake)
    monkeypatch.setattr(
        "syncmill.strategies.base.capture_changes",
        lambda p, base=None, *, git_dir=None: CapturedChanges(("changed.py",), ""),
    )
    return fake


def _supervisor(memory, strategy, agents: list, *, board=None, **config) -> Supervisor:
    sup = Supervisor.__new__(Supervisor)
    sup._config = SyncmillConfig(**config)
    sup._registry = FakeRegistry(agents)
    sup._memory = memory
    sup._breaker = None
    sup._matcher = None
    sup._board = board
    sup._strategy = strategy
    sup.last_report_path = None
    return sup


def _recording(memory, events: list) -> None:
    real = memory.search_context

    async def search_context(query, agent_id=None, **kwargs):
        text = await real(query, agent_id, **kwargs)
        events.append(("recall", kwargs.get("tag_filter"), text))
        return text

    memory.search_context = search_context


@pytest.mark.parametrize(
    "strategy, config, filters",
    [
        (RouteStrategy, {}, [None]),
        (CompeteStrategy, {}, [None]),
        (PipelineStrategy, {}, [None, "pipeline-stage"]),
        (
            CouncilStrategy,
            {"council": {"rounds": 2}},
            [None, "council-proposal", "council-proposal,council-critique",
             "council-proposal,council-critique"],
        ),
        (DecomposeStrategy, {}, [None, "decompose-plan,decompose-subtask", "decompose-subtask"]),
    ],
)
def test_a_head_strategy_asks_a_released_store_for_the_kinds_it_holds(
    release, backend, tmp_path, pool, strategy, config, filters
):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path)
    owned = _owned(_rows(frozen, name))
    released = {i: r for i, r in owned.items() if r["namespace"] == "shared"}
    task = f"{frozen.anchor} fix add in calc.py"
    events: list = []
    plan = json.dumps(["first half of the work", "second half of the work"])
    agents = [
        _Scripted("codex", [plan, "codex did it"], events),
        _Scripted("kimi-code", ["kimi did it"], events),
    ]
    with _Engine(store) as engine:
        _recording(engine.memory, events)
        result = _supervisor(engine.memory, strategy(), agents, **config).run(Task(prompt=task))
        engine.healthy()
    assert result.status.value == "completed", result.error
    recalls = [i for i, event in enumerate(events) if event[0] == "recall"]
    assert [events[i][1] for i in recalls] == filters
    seen: set = set()
    for n, at in enumerate(recalls):
        _, wanted, text = events[at]
        until = recalls[n + 1] if n + 1 < len(recalls) else len(events)
        phase = [event[1] for event in events[at + 1:until] if event[0] == "call"]
        assert phase, wanted
        kinds = LATER_PHASE_FILTERS[wanted] if wanted else None
        entitled = {r["content"] for r in released.values()
                    if kinds is None or kinds & set(r["tags"])}
        others = {r["content"] for r in released.values()} - entitled
        returned = {body for body in entitled if body in text}
        assert bool(entitled) == (wanted is None or release not in DIRECT_SHARE), wanted
        if wanted not in seen:
            # The first recall with a filter finds released rows of its kinds;
            # a kind the release never wrote (v0.1.2) is recalled as nothing.
            assert bool(returned) == bool(entitled), (n, wanted, text[:300])
        elif not returned:
            # Later, this run's own rows of those kinds may fill the top 5: a
            # relevance search, not a delivery guarantee.
            assert len(text.split(CONTEXT_SEPARATOR)) == 5, (n, wanted)
        seen.add(wanted)
        assert not any(body in text for body in others), wanted
        for body in returned:
            assert all(body in prompt for prompt in phase), (wanted, body[:80])


def test_a_head_run_hands_its_agent_the_released_result(release, backend, tmp_path, pool):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path)
    released = _rows(frozen, name)[_one(frozen, name, "route", "shared")]["content"]
    agent = FakeAgent("codex", output="a follow-up")
    with _Engine(store) as engine:
        # The released row's own text is the task: its first hit is known.
        _supervisor(engine.memory, RouteStrategy(), [agent]).run(Task(prompt=released))
        engine.healthy()
    (call,) = agent.calls
    assert call["prompt"].startswith("Context from previous work:\n")
    assert call["prompt"].count(released) == 2  # the recalled row, then the task


def test_a_head_room_round_hands_its_member_the_released_result(
    release, backend, tmp_path, git_repo
):
    from test_rooms import WritingAgent

    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    store = frozen.copy(name, tmp_path / "store")
    # The route result: every release shared it (v0.1.x rooms wrote no memory).
    released = _rows(frozen, name)[_one(frozen, name, "route", "shared")]["content"]
    config = SyncmillConfig(
        project={"id": f"frozen-{backend}-room"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True},
        memory=store.memory_config(),
    )
    agents = [WritingAgent("codex")]
    with _Engine(store) as engine:
        coordinator = RoomCoordinator(config, git_repo, agents=agents, memory=engine.memory)
        try:
            room = coordinator.create("a new room task", ["codex"])
            coordinator.run_round(room["id"], released)
        finally:
            coordinator.close()
        engine.healthy()
    prompt = agents[0].calls[-1]["prompt"]
    assert prompt.startswith("Context from previous work:\n")
    # The round prompt is the released text itself, so the recall must be an
    # earlier occurrence than the task's.
    assert released in prompt[: prompt.rindex(released)]


# ── the board's scratch collection ───────────────────────────────────────────


class _Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _ledger(board_db: Path, run_id: str) -> tuple:
    conn = sqlite3.connect(board_db)
    try:
        return conn.execute(
            "SELECT purged_at FROM board_run_namespaces WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()


#: Mem0's ``delete_all`` deletes concurrently over Qdrant local's one SQLite
#: connection and only logs the failures: the in-memory collection loses every
#: point, the file keeps some, and they come back on reopen.
MEM0_DELETE_LEAVES_ROWS = "#320: Mem0 delete_namespace leaves rows on disk"


def test_head_collects_a_released_scratch_namespace_past_one_page(
    release, backend, tmp_path, pool
):
    """The bulk store's finished board run left 105 scratch rows: deleting them
    takes more than one page of the backend's listing. What holds on both
    backends: the namespace reads empty in that process, the ledger tombstones
    it, and no row outside it is touched on disk."""
    store, run_id, before, doomed = _collect_bulk_scratch(release, backend, tmp_path)
    after = store.rows()
    assert set(after) <= set(before)
    kept = {i: r for i, r in before.items() if i not in doomed}
    assert {i: r for i, r in after.items() if i in kept} == kept


def test_a_collected_scratch_namespace_is_gone_from_disk(
    release, backend, tmp_path, pool, request
):
    if backend == "mem0":
        request.applymarker(pytest.mark.xfail(reason=MEM0_DELETE_LEAVES_ROWS, strict=False))
    store, run_id, before, doomed = _collect_bulk_scratch(release, backend, tmp_path)
    assert not set(store.rows()) & doomed
    with _Engine(store) as engine:  # a fresh process's view: the file
        assert run_async(engine.backend.count_namespace(f"board-run:{run_id}")) == 0


def _collect_bulk_scratch(release, backend, tmp_path):
    frozen = BackendsFrozen(release)
    name = f"{backend}/bulk"
    store = frozen.copy(name, tmp_path)
    run_id = frozen.scenario(name, "board-done")["run_id"]
    scratch_ns = f"board-run:{run_id}"
    before = store.rows()
    doomed = {i for i, r in before.items() if r["namespace"] == scratch_ns}
    assert len(doomed) == 105
    conn = sqlite3.connect(store.board)
    try:
        (claimed_at,) = conn.execute(
            "SELECT claimed_at FROM board_run_namespaces WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    # The run finished normally (no reclaim), so it is collectible at once.
    clock = _Clock(claimed_at + 1)
    board = Board(str(store.board), now=clock, lease_timeout_seconds=LEASE)
    try:
        with _Engine(store) as engine:
            sup = _supervisor(engine.memory, RouteStrategy(), [], board=board)
            sup._gc_board_run_namespaces(board)
            assert run_async(engine.backend.count_namespace(scratch_ns)) == 0
            engine.healthy()
        assert _ledger(store.board, run_id) == (clock.now,)
    finally:
        board.close()
    return store, run_id, before, doomed


def test_head_reclaims_a_released_lease_and_collects_its_scratch_namespace(
    release, backend, tmp_path, pool
):
    frozen = BackendsFrozen(release)
    name = f"{backend}/store"
    killed = frozen.scenario(name, "board-killed")
    store = frozen.copy(name, tmp_path)
    before = store.rows()
    scratch_ns = f"board-run:{killed['run_id']}"
    doomed = {i for i, r in before.items() if r["namespace"] == scratch_ns}
    assert doomed == set(killed["ids"])

    clock = _Clock(killed["claimed_at"] + LEASE)
    board = Board(str(store.board), now=clock, lease_timeout_seconds=LEASE)
    agent = FakeAgent("codex", output="finished after the crash")
    try:
        with _Engine(store) as engine:
            sup = _supervisor(engine.memory, RouteStrategy(), [agent], board=board)
            # First sweep: the lease has just run out, so the item is reclaimed
            # and run again; its old scratch namespace is inside its grace.
            result = sup.run_item()
            assert result is not None and result.status.value == "completed"
            assert f"Task: {killed['task']}" in agent.calls[0]["prompt"]
            assert run_async(engine.backend.count_namespace(scratch_ns)) == len(doomed)
            # One more lease timeout later it is collected.
            clock.now += LEASE
            sup._gc_board_run_namespaces(board)
            assert run_async(engine.backend.count_namespace(scratch_ns)) == 0
            engine.healthy()
        assert _ledger(store.board, killed["run_id"]) == (clock.now,)
    finally:
        board.close()
    after = store.rows()
    assert not set(after) & doomed
    durable = {i: r for i, r in before.items()
               if not (r["namespace"] or "").startswith("board-run:")}
    assert {i: r for i, r in after.items() if i in durable} == durable
    assert [
        (r["namespace"], r["tags"]) for i, r in after.items()
        if i not in before and not (r["namespace"] or "").startswith("board-run:")
    ] == [("shared", [f"board-result:{killed['item_id']}"])]
