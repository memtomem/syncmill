"""memtomem stores written by released syncmill, read by HEAD (#271).

Opt-in like every real-memtomem test (``SYNCMILL_TEST_MEMTOMEM_REAL=1``; CI's
real-memtomem job runs it): HEAD's ``MemtomemBackend`` and everything above it
(``SyncmillMemory``, the gateway, ``status``, the strategies, a room round, the
board's scratch collection) work on a copy of a store the release's own CLI
wrote. ``test_frozen_memory.py`` holds the stored bytes to the scenario
definitions without an engine; this module holds HEAD's readers to those
definitions and to what the release itself read (``release-read.json``).

A store is bound to the directory it was written in by one column,
``chunks.source_file`` (and its FTS mirror). A copy that is only moved reads
correctly, and that is how the open tests use it. Anything that resolves a
source file (purge, reindex, a write) needs the copy rebound first; ``rebind``
does that and proves it touched nothing else.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
from click.testing import CliRunner

import syncmill.supervisor as S
from syncmill.board import Board
from syncmill.cli import cli
from syncmill.config import MemoryConfig, SyncmillConfig
from syncmill.gateway import build_gateway
from syncmill.memory import run_async
from syncmill.memory_backend import MemtomemBackend
from syncmill.models import Task, TaskResult
from syncmill.rooms import RoomCoordinator
from syncmill.strategies.compete import CompeteStrategy
from syncmill.strategies.council import CouncilStrategy
from syncmill.strategies.decompose import DecomposeStrategy
from syncmill.strategies.pipeline import PipelineStrategy
from syncmill.strategies.route import RouteStrategy
from syncmill.supervisor import Supervisor
from syncmill.worktree import CapturedChanges

from conftest import FakeAgent, FakeRegistry, FakeWorktreePool, make_memory
from frozen_memory_support import RELEASES, Frozen, Row, StoreCopy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SYNCMILL_TEST_MEMTOMEM_REAL") != "1",
        reason="set SYNCMILL_TEST_MEMTOMEM_REAL=1 to run the real memtomem tests",
    ),
    pytest.mark.parametrize("release", RELEASES),
]

#: HEAD's default board lease.
LEASE = 1800
#: Larger than any frozen store: a search with it is not cut by ``top_k``.
ALL = 200
#: The filters HEAD's strategies send for a later phase, written out. The
#: values are compared with what a HEAD run actually sent, and each is shown to
#: select exactly the released rows of those kinds.
LATER_PHASE_FILTERS = {
    "pipeline-stage": {"pipeline-stage"},
    "council-proposal": {"council-proposal"},
    "council-proposal,council-critique": {"council-proposal", "council-critique"},
    "decompose-plan,decompose-subtask": {"decompose-plan", "decompose-subtask"},
    "decompose-subtask": {"decompose-subtask"},
}


@pytest.fixture(autouse=True)
def engine_environment(monkeypatch, tmp_path):
    """Neither interpreter state nor the developer's memtomem may leak in: no
    ambient memtomem settings, a home of its own, and a private runtime
    directory (memtomem refuses a schema migration when it cannot read the
    server registry there, and it only accepts a directory nobody else can
    enter)."""
    for key in list(os.environ):
        if key.startswith("MEMTOMEM_"):
            monkeypatch.delenv(key)
    home = (tmp_path / "home").resolve()
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    runtime = (tmp_path / "memtomem-runtime").resolve()
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("_MEMTOMEM_TEST_RUNTIME_DIR", str(runtime))


@pytest.fixture(autouse=True, scope="module")
def fixtures_stay_as_frozen():
    yield
    for release in RELEASES:
        Frozen(release).verify_pins()


def _snapshot(store: StoreCopy) -> dict[str, tuple]:
    return {i: (r.namespace, r.tags, r.content) for i, r in store.rows().items()}


def _cells(db: Path) -> dict[str, list[tuple]]:
    """Every cell of every ordinary table, with the one location-bound column
    blanked. FTS shadow tables are left out: they mirror ``chunks``."""
    conn = sqlite3.connect(db)
    try:
        cells = {}
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall():
            if table.startswith("chunks_fts") or table.startswith("chunks_vec"):
                continue
            columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            picked = ", ".join(
                "''" if (table, c) == ("chunks", "source_file") else f'"{c}"' for c in columns
            )
            cells[table] = conn.execute(f'SELECT {picked} FROM "{table}" ORDER BY rowid').fetchall()
        return cells
    finally:
        conn.close()


def rebind(store: StoreCopy) -> StoreCopy:
    """Point a moved copy at its own directory.

    The same two statements memtomem itself uses when a source file moves
    (``update_chunks_scope_for_source``): the path in ``chunks`` and, because
    the FTS table has no trigger, its mirror there. The new path is spelled the
    way memtomem spells one (``norm_path``).
    """
    from memtomem.storage.sqlite_helpers import norm_path

    # Directory prefixes with their separator, compared exactly (a LIKE
    # pattern would read the `_` in a temporary path as a wildcard).
    old = store.generating_mem.rstrip("/") + "/"
    new = norm_path(store.mem).rstrip("/") + "/"
    before = _cells(store.db)
    conn = sqlite3.connect(store.db)
    try:
        moved = conn.execute(
            "UPDATE chunks SET source_file = ? || substr(source_file, ?) "
            "WHERE substr(source_file, 1, ?) = ?",
            (new, len(old) + 1, len(old), old),
        ).rowcount
        conn.execute(
            "UPDATE chunks_fts SET source_file = "
            "(SELECT source_file FROM chunks WHERE chunks.rowid = chunks_fts.rowid)"
        )
        conn.commit()
        assert moved == conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE substr(source_file, 1, ?) = ?", (len(old), old)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN chunks_fts f ON f.rowid = c.rowid "
            "WHERE f.source_file != c.source_file"
        ).fetchone() == (0,)
    finally:
        conn.close()
    assert _cells(store.db) == before
    for row in store.rows().values():
        assert Path(row.source_file).parent == store.mem
    return store


def _backend(store: StoreCopy) -> MemtomemBackend:
    return MemtomemBackend(MemoryConfig(db_path=str(store.db), memory_dir=str(store.mem)))


class _Engine:
    """HEAD's backend on one store copy, closed before anyone looks at the
    database file again."""

    def __init__(self, store: StoreCopy):
        self.store = store
        self.backend = _backend(store)
        self.memory = make_memory(self.backend)

    def __enter__(self) -> "_Engine":
        run_async(self.backend.ensure_ready())
        return self

    def __exit__(self, *exc) -> None:
        run_async(self.backend.close())

    def healthy(self) -> None:
        assert not self.memory.degraded and self.memory.last_error is None


def _ids(rows) -> list[str]:
    return sorted(row["id"] for row in rows)


def _private(frozen: Frozen, store: StoreCopy, scenario: str) -> Row:
    (row,) = [
        r for r in frozen.scenario_rows(store, scenario)
        if r.namespace.startswith("agent-runtime:")
    ]
    return row


def _shared(frozen: Frozen, store: StoreCopy, scenario: str) -> Row:
    (row,) = [r for r in frozen.scenario_rows(store, scenario) if r.namespace == "shared"]
    return row


# ── open ─────────────────────────────────────────────────────────────────────


def test_head_opens_every_released_store_and_leaves_its_rows_alone(release, tmp_path):
    frozen = Frozen(release)
    for name in frozen.stores:
        # The reference rows come from a second copy: the copy HEAD opens must
        # not have been opened by anything else first, least of all the store
        # a SIGKILL left with its rows still in the WAL.
        before = _snapshot(frozen.copy(name, tmp_path / "reference"))
        assert before, name
        released = frozen.read[name]
        store = frozen.copy(name, tmp_path / "opened")
        for _ in range(2):  # the second open finds a store HEAD already opened
            with _Engine(store) as engine:
                counts = {
                    ns: run_async(engine.backend.count_namespace(ns))
                    for ns in {v[0] for v in before.values()}
                }
                # Moved, not rebound: the rows read as stored, the killed
                # run's row out of the WAL included.
                for chunk_id, (ns, tags, content) in before.items():
                    got = run_async(engine.backend.get(chunk_id))
                    assert (got["namespace"], sorted(got["tags"]), got["content"]) == (
                        ns, list(tags), content
                    ), chunk_id
                    assert got["source"] == released["get"][chunk_id]["source"]
                found = {
                    ns: _ids(run_async(engine.backend.search(
                        frozen.anchor, namespace=ns, top_k=ALL
                    )))
                    for ns in counts
                }
            assert counts == {
                ns: sum(1 for v in before.values() if v[0] == ns) for ns in counts
            }, name
            assert found == released["search"], name
            assert _snapshot(store) == before, name


# ── adapter reads ────────────────────────────────────────────────────────────


def test_get_reads_each_released_row_as_the_release_did(release, tmp_path):
    frozen = Frozen(release)
    for name in frozen.stores:
        store = rebind(frozen.copy(name, tmp_path))
        rows = store.rows()
        with _Engine(store) as engine:
            for chunk_id, row in rows.items():
                got = run_async(engine.backend.get(chunk_id))
                released = frozen.read[name]["get"][chunk_id]
                assert got["id"] == released["id"] == chunk_id
                assert got["namespace"] == released["namespace"] == row.namespace
                assert got["content"] == released["content"] == row.content
                assert sorted(got["tags"]) == sorted(released["tags"]) == list(row.tags)
                # The release read its own path; HEAD reads the copy's.
                assert Path(got["source"]).name == Path(released["source"]).name
                assert Path(got["source"]).parent == store.mem


def test_search_stays_inside_the_namespace_and_the_kind_asked_for(release, tmp_path):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    rows = store.rows()
    namespaces = sorted({r.namespace for r in rows.values()})
    kinds_seen = set()
    with _Engine(store) as engine:
        search = engine.backend.search
        for ns in namespaces:
            in_ns = [r for r in rows.values() if r.namespace == ns]
            found = run_async(search(frozen.anchor, namespace=ns, top_k=ALL))
            assert _ids(found) == sorted(r.id for r in in_ns) == frozen.read["store"]["search"][ns]
            assert {r["namespace"] for r in found} == {ns}
            for wanted, kinds in LATER_PHASE_FILTERS.items():
                expected = sorted(r.id for r in in_ns if kinds & set(r.tags))
                found = run_async(
                    search(frozen.anchor, namespace=ns, top_k=ALL, tag_filter=wanted)
                )
                assert _ids(found) == expected, (ns, wanted)
                if expected:
                    kinds_seen.add(wanted)
            assert run_async(
                search(frozen.anchor, namespace=ns, top_k=ALL, tag_filter="no-such-kind")
            ) == []
        # One scenario's own token finds that scenario's rows and no other.
        for scenario in ("route", "unicode", "headings", "room-apply"):
            token = frozen.scenario(scenario)["token"]
            found = run_async(search(token, namespace="shared", top_k=ALL))
            assert _ids(found) == [_shared(frozen, store, scenario).id], scenario
    assert kinds_seen == set(LATER_PHASE_FILTERS)  # every filter met a released row


def test_search_multi_count_and_status_cover_the_released_rows(release, tmp_path):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    rows = store.rows()
    codex, shared = (
        sorted(r.id for r in rows.values() if r.namespace == ns)
        for ns in ("agent-runtime:codex", "shared")
    )
    assert codex and shared
    with _Engine(store) as engine:
        backend = engine.backend
        both = run_async(backend.search_multi(
            frozen.anchor, namespaces=["agent-runtime:codex", "shared"], top_k=ALL
        ))
        assert _ids(both) == sorted(codex + shared)
        only = run_async(backend.search_multi(frozen.anchor, namespaces=["shared"], top_k=ALL))
        assert _ids(only) == shared
        assert len(run_async(backend.search_multi(
            frozen.anchor, namespaces=["agent-runtime:codex", "shared"], top_k=3
        ))) == 3
        for ns in {r.namespace for r in rows.values()}:
            count = run_async(backend.count_namespace(ns))
            assert count == sum(1 for r in rows.values() if r.namespace == ns) > 0
            assert count == frozen.read["store"]["count"][ns]
        status = run_async(backend.status())
        assert status["db_path"] == str(store.db)
        assert status["total_chunks"] == len(rows)
        assert status["embedding_provider"] == "none"


# ── adapter writes beside released rows ──────────────────────────────────────


def test_sharing_a_released_row_copies_its_tags_and_names_it(release, tmp_path):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    before = _snapshot(store)
    private = _private(frozen, store, "route")
    (scratch,) = [
        r for r in frozen.scenario_rows(store, "board-done")
        if r.namespace.startswith("board-run:")
    ]
    already_shared = _shared(frozen, store, "route")
    made = {}
    with _Engine(store) as engine:
        storage = run_async(engine.backend._storage())
        for source in (private, scratch, already_shared):
            new_id = run_async(engine.backend.share_chunk(source.id, target_namespace="shared"))
            assert new_id and new_id not in before
            link = run_async(storage.get_chunk_link(UUID(new_id), "shared"))
            assert link is not None and str(link.source_id) == source.id
            made[new_id] = source
    after = store.rows()
    assert {i: v for i, v in _snapshot(store).items() if i in before} == before
    assert set(after) == set(before) | set(made)
    for new_id, source in made.items():
        copy = after[new_id]
        assert copy.namespace == "shared" and copy.content == source.content
        kept = [t for t in source.tags if not t.startswith("shared-from=")]
        assert sorted(copy.tags) == sorted([*kept, f"shared-from={source.id}"])


@pytest.mark.parametrize("same_day", [False, True])
def test_a_fresh_share_lands_beside_the_released_rows(release, tmp_path, monkeypatch, same_day):
    """On a later day HEAD starts new day files and leaves the released ones
    byte for byte; on the release's own day it appends to them."""
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    before = _snapshot(store)
    released_files = {p.name: p.read_bytes() for p in store.day_files()}
    day = frozen.date if same_day else "2099-01-01"
    monkeypatch.setattr("syncmill.memory_backend._utc_date_str", lambda: day)
    body = f"**Task Result**\n\nwritten by HEAD beside {frozen.anchor}"
    with _Engine(store) as engine:
        write = run_async(engine.backend.add_and_share(
            body, tags=["task-result", "codex"], namespace="agent-runtime:codex",
            target_namespace="shared",
        ))
        assert len(write.source_ids) == len(write.shared_ids) == 1
        found = run_async(engine.backend.search(frozen.anchor, namespace="shared", top_k=ALL))
        released_shared = {i for i, v in before.items() if v[0] == "shared"}
        assert _ids(found) == sorted(released_shared | {write.shared_ids[0]})
    after = store.rows()
    assert {i: v for i, v in _snapshot(store).items() if i in before} == before
    assert set(after) == set(before) | {write.source_ids[0], write.shared_ids[0]}
    assert after[write.shared_ids[0]].content == body
    written = {f"{day}.agent-runtime.codex.md", f"{day}.shared.md"}
    assert {Path(after[i].source_file).name for i in set(after) - set(before)} == written
    for path in store.day_files():
        if path.name not in released_files:
            assert not same_day and path.name in written
        elif same_day and path.name in written:
            grown = path.read_bytes()
            assert grown.startswith(released_files[path.name]) and len(grown) > len(
                released_files[path.name]
            )
        else:
            assert path.read_bytes() == released_files[path.name]


def test_purging_a_released_scratch_namespace_removes_only_it(release, tmp_path):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    before = store.rows()
    scratch_ns = f"board-run:{frozen.scenario('board-done')['run_id']}"
    doomed = {i for i, r in before.items() if r.namespace == scratch_ns}
    (doomed_file,) = {Path(before[i].source_file) for i in doomed}
    assert doomed and doomed_file.is_file()
    files = {p.name for p in store.day_files()}
    with _Engine(store) as engine:
        assert run_async(engine.backend.purge_namespace(scratch_ns)) == len(doomed)
        assert run_async(engine.backend.count_namespace(scratch_ns)) == 0
    after = store.rows()
    assert set(after) == set(before) - doomed
    assert {i: r for i, r in after.items()} == {i: r for i, r in before.items() if i not in doomed}
    assert {p.name for p in store.day_files()} == files - {doomed_file.name}


def test_reindexing_the_released_day_files_changes_no_row(release, tmp_path):
    frozen = Frozen(release)
    for name in frozen.stores:
        store = rebind(frozen.copy(name, tmp_path))
        before = _snapshot(store)
        per_file: dict[str, int] = {}
        for row in store.rows().values():
            per_file[Path(row.source_file).name] = per_file.get(Path(row.source_file).name, 0) + 1
        with _Engine(store) as engine:
            components = run_async(engine.backend._store._ensure_init())
            for path in store.day_files():
                stats = run_async(components.index_engine.index_file(path, already_scanned=True))
                assert (stats.total_files, stats.indexed_chunks, stats.deleted_chunks) == (1, 0, 0)
                assert stats.skipped_chunks == per_file[path.name] > 0
                assert tuple(stats.errors) == ()
        assert _snapshot(store) == before, name


# ── SyncmillMemory ───────────────────────────────────────────────────────────


def _bodies(rows: list[Row]) -> set[str]:
    return {r.content for r in rows}


def test_search_context_returns_the_released_bodies_a_phase_is_entitled_to(release, tmp_path):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    rows = list(store.rows().values())
    shared = [r for r in rows if r.namespace == "shared"]
    elsewhere = _bodies([r for r in rows if r.namespace != "shared"]) - _bodies(shared)
    assert elsewhere  # the board run's scratch body exists nowhere in `shared`
    with _Engine(store) as engine:
        memory = engine.memory

        def context(**kwargs) -> str:
            text = run_async(memory.search_context(frozen.anchor, max_bytes=10**7, **kwargs))
            engine.healthy()
            return text

        everything = context(top_k=ALL)
        assert all(body in everything for body in _bodies(shared))
        assert not any(body in everything for body in elsewhere)
        for wanted, kinds in LATER_PHASE_FILTERS.items():
            entitled = _bodies([r for r in shared if kinds & set(r.tags)])
            others = _bodies(shared) - entitled
            text = context(top_k=ALL, tag_filter=wanted)
            assert entitled and all(body in text for body in entitled), wanted
            assert not any(body in text for body in others), wanted
        # The default limits return a handful of released rows, never none.
        default = run_async(memory.search_context(frozen.anchor))
        engine.healthy()
        assert 1 <= sum(1 for body in _bodies(shared) if body in default) <= 5
        # With an agent the private namespace is searched too: the route
        # result is then found twice, as the private row and as its copy.
        route = _shared(frozen, store, "route").content
        token = frozen.scenario("route")["token"]
        assert run_async(memory.search_context(token, top_k=ALL)).count(route) == 1
        assert run_async(
            memory.search_context(token, agent_id="codex", top_k=ALL)
        ).count(route) == 2
        engine.healthy()


def test_shared_reads_never_disclose_a_private_or_scratch_row(release, tmp_path):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    rows = store.rows()
    shared = {i: r for i, r in rows.items() if r.namespace == "shared"}
    private = _private(frozen, store, "route")
    run_id = frozen.scenario("board-done")["run_id"]
    (scratch,) = [r for r in rows.values() if r.namespace == f"board-run:{run_id}"]
    with _Engine(store) as engine:
        memory = engine.memory
        found = run_async(memory.search_shared(frozen.anchor, top_k=ALL))
        assert _ids(found) == sorted(shared)
        assert {row["namespace"] for row in found} == {"shared"}
        for chunk_id, row in shared.items():
            read = run_async(memory.get_shared(chunk_id))
            assert read.status == "found" and read.chunk["content"] == row.content
        for hidden in (private, scratch):
            assert run_async(engine.backend.get(hidden.id))["content"] == hidden.content
            read = run_async(memory.get_shared(hidden.id))
            assert (read.status, read.chunk) == ("not_found", None)
        assert run_async(memory.shared_stats()) == {
            "namespace": "shared", "chunk_count": len(shared)
        }
        engine.healthy()

        # A board-scoped view reads its own scratch namespace and `shared`.
        view = memory.scoped(run_id)
        text = run_async(view.search_context(frozen.anchor, top_k=ALL, max_bytes=10**7))
        assert scratch.content in text
        assert _shared(frozen, store, "route").content in text
        assert not view.degraded and view.last_error is None


# ── production surfaces ──────────────────────────────────────────────────────


def _call(mcp, name: str, args: dict) -> str:
    return run_async(mcp.call_tool(name, args)).structured_content["result"]


def test_the_gateway_serves_the_released_shared_rows(release, tmp_path):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    rows = store.rows()
    shared = _shared(frozen, store, "unicode")
    private = _private(frozen, store, "unicode")
    with _Engine(store) as engine:
        gateway = build_gateway(engine.memory)
        token = frozen.scenario("unicode")["token"]
        out = _call(gateway, "shared_search", {"query": token})
        assert shared.id in out and private.id not in out
        assert shared.content[:200] in out
        assert shared.content in _call(gateway, "shared_read", {"chunk_id": shared.id})
        hidden = _call(gateway, "shared_read", {"chunk_id": private.id})
        assert private.content not in hidden and shared.content not in hidden
        in_shared = sum(1 for r in rows.values() if r.namespace == "shared")
        assert _call(gateway, "shared_stats", {}) == f"shared: {in_shared} chunks"
        engine.healthy()


def test_status_connects_to_a_released_store(release, tmp_path, monkeypatch):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    before = _snapshot(store)
    monkeypatch.setenv("SYNCMILL_MEMORY__DB_PATH", str(store.db))
    monkeypatch.setenv("SYNCMILL_MEMORY__MEMORY_DIR", str(store.mem))
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "memtomem: connected" in result.output
    assert f"db_path: {store.db}" in result.output
    assert _snapshot(store) == before


class _Scripted(FakeAgent):
    """A fake agent whose replies are scripted per call (the last repeats).
    Each call is also appended to ``events``, when given, as ``("call",
    prompt)``."""

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
    """Append each recall to ``events`` as ``("recall", tag filter, context
    returned)``."""
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
            [
                None,
                "council-proposal",
                "council-proposal,council-critique",
                "council-proposal,council-critique",
            ],
        ),
        (
            DecomposeStrategy,
            {},
            [None, "decompose-plan,decompose-subtask", "decompose-subtask"],
        ),
    ],
)
def test_a_head_strategy_asks_a_released_store_for_the_kinds_it_holds(
    release, tmp_path, pool, strategy, config, filters
):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    released = [r for r in store.rows().values() if r.namespace == "shared"]
    # Every word of the task is in each released strategy result (their
    # prompts were "<anchor> tok<scenario> fix add in calc.py"). memtomem
    # falls back from matching every word to matching any word only when the
    # first finds nothing, so a word the released rows lack would leave a
    # later phase with this run's own rows alone, which repeat the task.
    task = f"{frozen.anchor} fix add in calc.py"
    assert sum(1 for r in released if all(w in r.content for w in task.split())) >= 10
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
    for n, at in enumerate(recalls):
        _, wanted, text = events[at]
        # A phase recalls once, then runs its agents: its prompts are the
        # calls before the next recall.
        until = recalls[n + 1] if n + 1 < len(recalls) else len(events)
        phase = [event[1] for event in events[at + 1:until] if event[0] == "call"]
        assert phase, wanted
        kinds = LATER_PHASE_FILTERS[wanted] if wanted else None
        entitled = _bodies([r for r in released if kinds is None or kinds & set(r.tags)])
        others = _bodies(released) - entitled
        returned = {body for body in entitled if body in text}
        # Each recall finds released rows, only of the kinds it asks for, and
        # every agent of that phase is handed them.
        assert returned, (wanted, text)
        assert not any(body in text for body in others), wanted
        for body in returned:
            assert all(body in prompt for prompt in phase), (wanted, body[:80])


def test_a_head_run_hands_its_agent_the_released_result(release, tmp_path, pool):
    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path))
    released = _shared(frozen, store, "route")
    agent = FakeAgent("codex", output="a follow-up")
    with _Engine(store) as engine:
        _supervisor(engine.memory, RouteStrategy(), [agent]).run(
            Task(prompt=frozen.scenario("route")["token"])
        )
        engine.healthy()
    (call,) = agent.calls
    assert call["prompt"].startswith("Context from previous work:\n")
    assert released.content in call["prompt"]


def test_a_head_room_round_hands_its_member_the_released_result(release, tmp_path, git_repo):
    from test_rooms import WritingAgent

    frozen = Frozen(release)
    store = rebind(frozen.copy("store", tmp_path / "store"))
    released = _shared(frozen, store, "room-apply")
    config = SyncmillConfig(
        project={"id": "frozen-memory-room"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True},
        memory={"db_path": str(store.db), "memory_dir": str(store.mem)},
    )
    agents = [WritingAgent("codex")]
    with _Engine(store) as engine:
        coordinator = RoomCoordinator(config, git_repo, agents=agents, memory=engine.memory)
        try:
            room = coordinator.create("a new room task", ["codex"])
            coordinator.run_round(room["id"], frozen.scenario("room-apply")["token"])
        finally:
            coordinator.close()
        engine.healthy()
    prompt = agents[0].calls[-1]["prompt"]
    assert prompt.startswith("Context from previous work:\n")
    assert released.content in prompt


def test_head_finds_a_released_store_at_the_project_default_location(
    release, tmp_path, git_repo, monkeypatch
):
    """An initialized project that leaves ``[memory]`` at its defaults keeps
    its store under ``<repo>/.syncmill/memory``, database and day files side
    by side. The released bytes are put there, and a HEAD room round, given no
    memory and no memory paths, finds them through ``configure_project_memory``.
    """
    import syncmill.rooms as rooms
    from syncmill.memory import SyncmillMemory
    from test_rooms import WritingAgent

    frozen = Frozen(release)
    staged = frozen.copy("store", tmp_path / "staged")
    home = git_repo / ".syncmill" / "memory"
    home.mkdir(parents=True)
    for path in staged.day_files():
        (home / path.name).write_bytes(path.read_bytes())
    for part in staged.db.parent.glob(staged.db.name + "*"):
        (home / part.name.replace(staged.db.name, "memtomem.db", 1)).write_bytes(part.read_bytes())
    store = rebind(StoreCopy(
        name="project", root=home.resolve(), db=home.resolve() / "memtomem.db",
        mem=home.resolve(), board=None, generating_mem=staged.generating_mem,
    ))
    released = _shared(frozen, store, "room-apply")
    config = SyncmillConfig(
        project={"id": "frozen-memory-project"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True},
    )
    assert config.memory.memory_dir is None
    # Undo the suite-wide stand-in: this round builds its own real memory.
    monkeypatch.setattr(rooms, "SyncmillMemory", SyncmillMemory)
    agents = [WritingAgent("codex")]
    coordinator = RoomCoordinator(config, git_repo, agents=agents)
    try:
        assert config.memory.db_path == str(store.db)
        assert config.memory.memory_dir == str(store.mem)
        room = coordinator.create("a new room task", ["codex"])
        coordinator.run_round(room["id"], frozen.scenario("room-apply")["token"])
    finally:
        coordinator.close()
    prompt = agents[0].calls[-1]["prompt"]
    assert prompt.startswith("Context from previous work:\n")
    assert released.content in prompt


class _Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_head_reclaims_a_released_lease_and_purges_its_scratch_namespace(
    release, tmp_path, pool
):
    frozen = Frozen(release)
    killed = frozen.scenario("board-killed")
    store = rebind(frozen.copy("store", tmp_path))
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
            # First sweep: the released lease has just run out, so the item is
            # reclaimed and run again. Its old scratch namespace is inside its
            # grace period and stays.
            result = sup.run_item()
            assert result is not None and result.status.value == "completed"
            assert f"Task: {killed['task']}" in agent.calls[0]["prompt"]
            assert run_async(engine.backend.count_namespace(scratch_ns)) == len(doomed)
            # Second sweep, one more lease timeout later: now it is collected.
            clock.now += LEASE
            sup._gc_board_run_namespaces(board)
            assert run_async(engine.backend.count_namespace(scratch_ns)) == 0
            engine.healthy()
        ledger = sqlite3.connect(store.board)
        try:
            purged_at = ledger.execute(
                "SELECT purged_at FROM board_run_namespaces WHERE run_id = ?",
                (killed["run_id"],),
            ).fetchone()
        finally:
            ledger.close()
        assert purged_at == (clock.now,)
    finally:
        board.close()
    after = store.rows()
    assert not set(after) & doomed
    assert not scratch_file.exists()
    # Nothing the release stored outside a scratch namespace was touched, and
    # the reclaimed run added one row there: its board-result.
    durable = {i: r for i, r in before.items() if not r.namespace.startswith("board-run:")}
    assert {i: r for i, r in after.items() if i in durable} == durable
    assert [
        (r.namespace, r.tags) for i, r in after.items()
        if i not in before and not r.namespace.startswith("board-run:")
    ] == [("shared", (f"board-result:{killed['item_id']}",))]
