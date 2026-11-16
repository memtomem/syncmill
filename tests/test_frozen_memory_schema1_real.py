"""Schema-1 memtomem stores written by released syncmill, read by HEAD (#271).

Opt-in like every real-memtomem test (``SYNCMILL_TEST_MEMTOMEM_REAL=1``; CI's
``memory-backend-real`` job runs it). HEAD's memtomem migrates a v0.1.x store
from schema 1 on open; everything here works on a copy of a store the release's
own CLI wrote (``schema1/freeze.py``). ``test_frozen_memory_schema1.py`` holds
the stored bytes to what generation proved, without an engine.

The two layouts differ (``index.json`` ``layout``): v0.1.4 ("split") wrote a day
file per namespace and copied each result from a private row into ``shared``;
v0.1.2 ("mixed") wrote every namespace into one ``<date>.md`` and each result
straight into ``shared``. Where HEAD treats them differently, a test asserts each
outcome as measured; none is skipped for a layout.

Several outcomes here are findings, frozen as they are (#271 changes no reader):
neither release indexed a kind tag, so HEAD's later-phase recall cannot see a
released row; HEAD's memtomem refuses to re-chunk a released v0.1.4 ``shared``
day file, which also refuses a HEAD write to it on the release's own date; and
HEAD cannot purge a v0.1.2 scratch namespace, whose rows live in the shared
``<date>.md``, so its board GC records a failed purge and degrades.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from pathlib import Path
from uuid import UUID

import pytest
from click.testing import CliRunner

from syncmill.board import Board
from syncmill.cli import cli
from syncmill.gateway import build_gateway
from syncmill.memory import run_async
from syncmill.memory_backend import UnprovablePurgeError
from syncmill.models import Task
from syncmill.strategies.compete import CompeteStrategy
from syncmill.strategies.council import CouncilStrategy
from syncmill.strategies.decompose import DecomposeStrategy
from syncmill.strategies.pipeline import PipelineStrategy
from syncmill.strategies.route import RouteStrategy

from conftest import FakeAgent
from frozen_memory_schema1_support import SCHEMA1_RELEASES, Schema1Frozen, full_rows
from frozen_memory_support import StoreCopy
from test_frozen_memory_real import (  # the Memory-1a engine helpers, unchanged
    ALL,
    LATER_PHASE_FILTERS,
    LEASE,
    _Clock,
    _Engine,
    _Scripted,
    _call,
    _recording,
    _snapshot,
    _supervisor,
    rebind,
)
# The Memory-1a fixtures, registered in this module by importing them: the
# autouse memtomem isolation, and the worktree stand-in the orchestration tests
# request by name (`usefixtures`; its value is not used).
from test_frozen_memory_real import engine_environment, pool  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SYNCMILL_TEST_MEMTOMEM_REAL") != "1",
        reason="set SYNCMILL_TEST_MEMTOMEM_REAL=1 to run the real memtomem tests",
    ),
    pytest.mark.parametrize("release", SCHEMA1_RELEASES),
]

#: What HEAD's memtomem says when it would have to drop a chunk a
#: `chunk_links` row names in order to re-chunk a released file.
LINKED = "Reindex would remove linked chunk identities"


@pytest.fixture(autouse=True)
def a_later_day(monkeypatch):
    """HEAD writes into the day file of the current UTC date. Run on the date
    the release wrote, that is the released file, which HEAD refuses to
    re-chunk (see the release-day test, the one place that date is used), so
    every other write is pinned to a later day."""
    monkeypatch.setattr("syncmill.memory_backend._utc_date_str", lambda: "2099-01-01")


@pytest.fixture(autouse=True, scope="module")
def fixtures_stay_as_frozen():
    yield
    for release in SCHEMA1_RELEASES:
        Schema1Frozen(release).verify_pins()


def _spans(store: StoreCopy) -> list[dict]:
    return full_rows(store.db)


def _schema(store: StoreCopy) -> str:
    conn = sqlite3.connect(store.db)
    try:
        return dict(conn.execute("SELECT key, value FROM _memtomem_meta"))["schema_version"]
    finally:
        conn.close()


def _split(frozen: Schema1Frozen) -> bool:
    return frozen.index["layout"] == "split"


def _by_role(frozen: Schema1Frozen, store: StoreCopy, scenario: str, namespace: str) -> list:
    """The stored rows holding a scenario's own token in one namespace."""
    token = frozen.scenario(scenario)["token"]
    return [r for r in store.rows().values() if r.namespace == namespace and token in r.content]


# ── open ─────────────────────────────────────────────────────────────────────


def test_head_migrates_a_released_store_and_leaves_its_rows_alone(release, tmp_path):
    """HEAD opens the copy first, WAL and all: any other connection closed on
    it beforehand would checkpoint the WAL into the main file and remove it.
    The schema version and the rows to compare with therefore come from a
    second copy (and from what generation recorded)."""
    frozen = Schema1Frozen(release)
    reference = frozen.copy(tmp_path / "reference")
    assert _schema(reference) == "1"
    before = _spans(reference)
    assert before == frozen.index["rows"]
    store = frozen.copy(tmp_path / "opened")  # moved, not rebound, not opened
    wal = store.db.with_name(store.db.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    with _Engine(store) as engine:
        engine.healthy()
    assert _schema(store) != "1"
    assert _spans(store) == before
    migrated = _schema(store)
    with _Engine(store):
        pass
    assert (_schema(store), _spans(store)) == (migrated, before)


def test_get_and_search_read_each_released_row_as_the_release_did(release, tmp_path):
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    rows = store.rows()
    read = frozen.read["store"]
    with _Engine(store) as engine:
        backend = engine.backend
        for chunk_id, row in rows.items():
            got = run_async(backend.get(chunk_id))
            released = read["get"][chunk_id]
            assert (got["id"], got["namespace"], got["content"]) == (
                chunk_id, released["namespace"], released["content"])
            assert sorted(got["tags"]) == sorted(released["tags"]) == list(row.tags)
            assert Path(got["source"]).name == Path(released["source"]).name
            assert Path(got["source"]).parent == store.mem
        for ns, ids in read["search"].items():
            found = run_async(backend.search(frozen.anchor, namespace=ns, top_k=ALL))
            assert sorted(r["id"] for r in found) == ids, ns
            assert {r["namespace"] for r in found} == {ns}
            assert run_async(backend.count_namespace(ns)) == read["count"][ns] == len(ids)
        found = run_async(backend.search_multi(
            frozen.anchor, namespaces=sorted(read["search"]), top_k=ALL))
        assert sorted(r["id"] for r in found) == sorted(rows)
        status = run_async(backend.status())
        assert (status["total_chunks"], status["total_sources"]) == (
            len(rows), len({Path(r.source_file).name for r in rows.values()}))
        engine.healthy()


def test_no_kind_filter_finds_a_released_row(release, tmp_path):
    """v0.1.x indexed no kind tag (its `shared` copies carry only
    `shared-from=`), so every filter HEAD's strategies send comes back empty
    next to a non-empty unfiltered search."""
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    with _Engine(store) as engine:
        for ns in sorted(frozen.read["store"]["search"]):
            assert run_async(engine.backend.search(frozen.anchor, namespace=ns, top_k=ALL))
            for wanted in [*LATER_PHASE_FILTERS, "task-result"]:
                assert run_async(engine.backend.search(
                    frozen.anchor, namespace=ns, top_k=ALL, tag_filter=wanted)) == [], wanted
        engine.healthy()


# ── HEAD writing beside released rows ────────────────────────────────────────


def test_sharing_a_released_result_row_names_it(release, tmp_path):
    """The route result's row (v0.1.4's private row, v0.1.2's `shared` one,
    the release wrote no other) copied into `shared` with HEAD's provenance."""
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    before = _snapshot(store)
    source_ns = "agent-runtime:codex" if _split(frozen) else "shared"
    private = _by_role(frozen, store, "route", source_ns)[0]
    with _Engine(store) as engine:
        new_id = run_async(engine.backend.share_chunk(private.id, target_namespace="shared"))
        link = run_async(run_async(engine.backend._storage()).get_chunk_link(
            UUID(new_id), "shared"))
        assert link is not None and str(link.source_id) == private.id
    after = store.rows()
    assert {i: v for i, v in _snapshot(store).items() if i in before} == before
    assert set(after) == set(before) | {new_id}
    assert after[new_id].namespace == "shared" and after[new_id].content == private.content
    assert after[new_id].tags == (f"shared-from={private.id}",)


def test_a_fresh_share_on_a_later_day_lands_beside_the_released_rows(release, tmp_path):
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    before = _snapshot(store)
    released_files = {p.name: p.read_bytes() for p in store.day_files()}
    body = f"**Task Result**\n\nwritten by HEAD beside {frozen.anchor}"
    with _Engine(store) as engine:
        write = run_async(engine.backend.add_and_share(
            body, tags=["task-result", "codex"], namespace="agent-runtime:codex",
            target_namespace="shared",
        ))
    assert len(write.source_ids) == len(write.shared_ids) == 1
    after = store.rows()
    assert {i: v for i, v in _snapshot(store).items() if i in before} == before
    assert set(after) == set(before) | {*write.source_ids, *write.shared_ids}
    assert set(released_files) <= {p.name for p in store.day_files()}
    for path in store.day_files():
        if path.name in released_files:
            assert path.read_bytes() == released_files[path.name]
        else:
            assert path.name.startswith("2099-01-01.")


def test_a_fresh_share_on_the_release_day(release, tmp_path, monkeypatch):
    """v0.1.4, finding: on the date the release wrote, HEAD appends to the
    released day files. The private one re-chunks, but re-chunking the
    released `shared` file would replace chunks that `chunk_links` rows name,
    which HEAD's memtomem refuses: the call raises after both appends, the new
    private row is indexed, and the copy's text sits in the file unindexed.
    v0.1.2: HEAD's per-namespace files are new files beside the released
    `<date>.md`, which stays byte for byte."""
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    before = _snapshot(store)
    monkeypatch.setattr("syncmill.memory_backend._utc_date_str", lambda: frozen.date)
    body = f"**Task Result**\n\nwritten by HEAD on the release day {frozen.anchor}"
    if not _split(frozen):
        released = (store.mem / f"{frozen.date}.md").read_bytes()
        with _Engine(store) as engine:
            write = run_async(engine.backend.add_and_share(
                body, tags=["task-result", "codex"], namespace="agent-runtime:codex",
                target_namespace="shared",
            ))
            engine.healthy()
        assert (store.mem / f"{frozen.date}.md").read_bytes() == released
        assert {p.name for p in store.day_files()} == {
            f"{frozen.date}.md", f"{frozen.date}.agent-runtime.codex.md",
            f"{frozen.date}.shared.md"}
        assert set(store.rows()) == set(before) | {*write.source_ids, *write.shared_ids}
        return
    shared_file = store.mem / f"{frozen.date}.shared.md"
    released_shared = shared_file.read_bytes()
    with _Engine(store) as engine:
        with pytest.raises(ValueError, match=LINKED):
            run_async(engine.backend.add_and_share(
                body, tags=["task-result", "codex"], namespace="agent-runtime:codex",
                target_namespace="shared",
            ))
    after = store.rows()
    assert {i: v for i, v in _snapshot(store).items() if i in before} == before
    (added,) = [after[i] for i in set(after) - set(before)]
    assert added.namespace == "agent-runtime:codex" and body.split("\n\n", 1)[1] in added.content
    grown = shared_file.read_bytes()
    assert grown.startswith(released_shared) and body.encode() in grown[len(released_shared):]


def test_reindexing_the_released_day_files(release, tmp_path):
    """v0.1.4: the private and scratch files re-derive to the released rows;
    the `shared` file is refused (finding: HEAD's chunker splits it
    differently and would drop linked copies). v0.1.2: the one `<date>.md`
    skips every chunk; one row's end line moves up by one (measured), nothing
    else changes."""
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    before = _spans(store)
    per_file = Counter(r["source"] for r in before)
    with _Engine(store) as engine:
        components = run_async(engine.backend._store._ensure_init())
        for path in store.day_files():
            if _split(frozen) and path.name == f"{frozen.date}.shared.md":
                with pytest.raises(ValueError, match=LINKED):
                    run_async(components.index_engine.index_file(path, already_scanned=True))
                continue
            stats = run_async(components.index_engine.index_file(path, already_scanned=True))
            assert (stats.total_files, stats.indexed_chunks, stats.deleted_chunks) == (1, 0, 0)
            assert stats.skipped_chunks == per_file[path.name] > 0
            assert tuple(stats.errors) == ()
    after = _spans(store)
    if _split(frozen):
        assert after == before
        return
    moved = [(b["end_line"], a["end_line"]) for b, a in zip(before, after) if a != b]
    assert len(moved) == 1 and moved[0][1] == moved[0][0] - 1
    assert [{**r, "end_line": 0} for r in after] == [{**r, "end_line": 0} for r in before]


def test_purging_the_released_scratch_namespace(release, tmp_path):
    """v0.1.4: removes exactly that namespace and its day file. v0.1.2: its
    rows live in the shared `<date>.md`, which HEAD cannot lock as theirs, so
    the purge is refused before anything is deleted (finding)."""
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    before = store.rows()
    scratch_ns = f"board-run:{frozen.scenario('board-killed')['run_id']}"
    doomed = {i for i, r in before.items() if r.namespace == scratch_ns}
    (doomed_file,) = {Path(before[i].source_file) for i in doomed}
    files = {p.name for p in store.day_files()}
    if not _split(frozen):
        assert doomed_file.name == f"{frozen.date}.md"
        with _Engine(store) as engine:
            with pytest.raises(UnprovablePurgeError, match="not a day file"):
                run_async(engine.backend.purge_namespace(scratch_ns))
        assert store.rows() == before and {p.name for p in store.day_files()} == files
        return
    with _Engine(store) as engine:
        assert run_async(engine.backend.purge_namespace(scratch_ns)) == len(doomed) > 0
    after = store.rows()
    assert after == {i: r for i, r in before.items() if i not in doomed}
    assert {p.name for p in store.day_files()} == files - {doomed_file.name}


# ── SyncmillMemory and the read surfaces ─────────────────────────────────────


def test_recall_returns_released_shared_bodies_but_no_later_phase_does(release, tmp_path):
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    rows = list(store.rows().values())
    shared = {r.content for r in rows if r.namespace == "shared"}
    scratch = {r.content for r in rows if r.namespace.startswith("board-run:")} - shared
    assert scratch
    with _Engine(store) as engine:
        memory = engine.memory
        everything = run_async(memory.search_context(frozen.anchor, top_k=ALL, max_bytes=10**7))
        assert all(body in everything for body in shared)
        assert not any(body in everything for body in scratch)
        for wanted in LATER_PHASE_FILTERS:
            text = run_async(memory.search_context(
                frozen.anchor, top_k=ALL, max_bytes=10**7, tag_filter=wanted))
            assert not any(body in text for body in shared), wanted
        default = run_async(memory.search_context(frozen.anchor))
        assert 1 <= sum(1 for body in shared if body in default) <= 5
        engine.healthy()


def test_shared_reads_never_disclose_a_private_or_scratch_row(release, tmp_path):
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    rows = store.rows()
    shared = {i: r for i, r in rows.items() if r.namespace == "shared"}
    hidden = [r for r in rows.values() if r.namespace != "shared"]
    run_id = frozen.scenario("board-killed")["run_id"]
    scratch = [r for r in hidden if r.namespace == f"board-run:{run_id}"]
    assert scratch
    with _Engine(store) as engine:
        memory = engine.memory
        found = run_async(memory.search_shared(frozen.anchor, top_k=ALL))
        assert sorted(r["id"] for r in found) == sorted(shared)
        for chunk_id, row in shared.items():
            read = run_async(memory.get_shared(chunk_id))
            assert read.status == "found" and read.chunk["content"] == row.content
        for row in hidden:
            assert run_async(engine.backend.get(row.id))["content"] == row.content
            assert run_async(memory.get_shared(row.id)).status == "not_found"
        # A copy whose source a later re-chunk replaced still reads as itself.
        dangling = set(frozen.index["findings"]["dangling_shared_from"])
        orphans = [r for r in shared.values() if any(t.split("=", 1)[1] in dangling
                                                     for t in r.tags)]
        assert bool(orphans) == _split(frozen)
        assert all(run_async(memory.get_shared(r.id)).status == "found" for r in orphans)
        assert run_async(memory.shared_stats()) == {
            "namespace": "shared", "chunk_count": len(shared)}
        view = memory.scoped(run_id)
        text = run_async(view.search_context(frozen.anchor, top_k=ALL, max_bytes=10**7))
        assert all(row.content in text for row in scratch)
        assert next(iter(shared.values())).content in text
        assert not view.degraded and view.last_error is None
        engine.healthy()


def test_the_gateway_and_status_serve_a_released_store(release, tmp_path, monkeypatch):
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    rows = store.rows()
    token = frozen.scenario("unicode")["token"]
    # Merged chunks can carry a token into several rows; any pair will do. The
    # row the gateway must not show is v0.1.4's private result, or a v0.1.2
    # scratch row (that release wrote no private row).
    shared = next(r for r in rows.values() if r.namespace == "shared" and token in r.content)
    private = (_by_role(frozen, store, "unicode", "agent-runtime:codex")[0] if _split(frozen)
               else next(r for r in rows.values() if r.namespace.startswith("board-run:")))
    with _Engine(store) as engine:
        gateway = build_gateway(engine.memory)
        out = _call(gateway, "shared_search", {"query": token})
        assert shared.id in out and private.id not in out
        assert shared.content in _call(gateway, "shared_read", {"chunk_id": shared.id})
        assert private.content not in _call(gateway, "shared_read", {"chunk_id": private.id})
        in_shared = sum(1 for r in rows.values() if r.namespace == "shared")
        assert _call(gateway, "shared_stats", {}) == f"shared: {in_shared} chunks"
        engine.healthy()
    before = _snapshot(store)
    monkeypatch.setenv("SYNCMILL_MEMORY__DB_PATH", str(store.db))
    monkeypatch.setenv("SYNCMILL_MEMORY__MEMORY_DIR", str(store.mem))
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "memtomem: connected" in result.output
    assert _snapshot(store) == before


# ── HEAD orchestration over a released store ─────────────────────────────────


@pytest.mark.parametrize(
    "strategy, config, filters",
    [
        (RouteStrategy, {}, [None]),
        (CompeteStrategy, {}, [None]),
        (PipelineStrategy, {}, [None, "pipeline-stage"]),
        (CouncilStrategy, {"council": {"rounds": 2}},
         [None, "council-proposal", "council-proposal,council-critique",
          "council-proposal,council-critique"]),
        (DecomposeStrategy, {},
         [None, "decompose-plan,decompose-subtask", "decompose-subtask"]),
    ],
)
@pytest.mark.usefixtures("pool")
def test_a_head_strategy_sees_released_rows_only_in_its_first_phase(
    release, tmp_path, strategy, config, filters
):
    """The first phase's recall hands every agent released bodies; each later
    phase's kind filter finds none of them (finding), only this run's rows."""
    frozen = Schema1Frozen(release)
    store = rebind(frozen.copy(tmp_path))
    released = {r.content for r in store.rows().values() if r.namespace == "shared"}
    task = f"{frozen.anchor} fix add in calc.py"
    events: list = []
    plan = '["first half of the work", "second half of the work"]'
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
    for n, at in enumerate(recalls):
        _, wanted, text = events[at]
        until = recalls[n + 1] if n + 1 < len(recalls) else len(events)
        phase = [event[1] for event in events[at + 1:until] if event[0] == "call"]
        assert phase, wanted
        returned = {body for body in released if body in text}
        if wanted is None:
            assert returned, text
            for body in returned:
                assert all(body in prompt for prompt in phase)
        else:
            # Neither the recall nor anything the phase's agents are handed
            # carries a released body.
            assert not returned, wanted
            assert not any(body in prompt for body in released for prompt in phase), wanted


@pytest.mark.usefixtures("pool")
def test_head_reclaims_a_released_lease_and_collects_its_scratch_namespace(release, tmp_path):
    """HEAD reclaims the killed run's lease and completes the item on both. The
    next sweep, one lease timeout later, purges the scratch namespace on
    v0.1.4; on v0.1.2 the purge is refused, memory degrades, and the ledger
    schedules a retry instead of recording a purge (finding)."""
    frozen = Schema1Frozen(release)
    killed = frozen.scenario("board-killed")
    store = rebind(frozen.copy(tmp_path))
    before = store.rows()
    scratch_ns = f"board-run:{killed['run_id']}"
    doomed = {i for i, r in before.items() if r.namespace == scratch_ns}
    (scratch_file,) = {Path(before[i].source_file) for i in doomed}
    assert doomed and scratch_file.is_file()
    clock = _Clock(killed["claimed_at"] + LEASE)
    board = Board(str(store.board), now=clock, lease_timeout_seconds=LEASE)
    agent = FakeAgent("codex", output="finished after the crash")
    try:
        with _Engine(store) as engine:
            sup = _supervisor(engine.memory, RouteStrategy(), [agent], board=board)
            result = sup.run_item()
            assert result is not None and result.status.value == "completed"
            assert f"Task: {killed['task']}" in agent.calls[0]["prompt"]
            assert run_async(engine.backend.count_namespace(scratch_ns)) == len(doomed)
            engine.healthy()
            clock.now += LEASE
            sup._gc_board_run_namespaces(board)
            if _split(frozen):
                assert run_async(engine.backend.count_namespace(scratch_ns)) == 0
                engine.healthy()
            else:
                assert run_async(engine.backend.count_namespace(scratch_ns)) == len(doomed)
                assert engine.memory.degraded
                assert engine.memory.last_error.startswith("UnprovablePurgeError: ")
                assert "not a day file" in engine.memory.last_error
        ledger = sqlite3.connect(store.board)
        try:
            ledger_row = ledger.execute(
                "SELECT purged_at, failed_attempts, next_attempt_at FROM board_run_namespaces "
                "WHERE run_id = ?", (killed["run_id"],),
            ).fetchone()
        finally:
            ledger.close()
        if _split(frozen):
            assert ledger_row == (clock.now, None, None)
        else:
            assert ledger_row[:2] == (None, 1) and ledger_row[2] > clock.now
    finally:
        board.close()
    after = store.rows()
    if _split(frozen):
        assert not set(after) & doomed and not scratch_file.exists()
    else:
        assert doomed <= set(after) and scratch_file.exists()
    durable = {i: r for i, r in before.items() if not r.namespace.startswith("board-run:")}
    assert {i: r for i, r in after.items() if i in durable} == durable
    assert [
        (r.namespace, r.tags) for i, r in after.items()
        if i not in before and not r.namespace.startswith("board-run:")
    ] == [("shared", (f"board-result:{killed['item_id']}",))]
