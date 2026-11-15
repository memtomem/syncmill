"""memtomem stores written by released syncmill: the stored bytes (#271).

The stores under ``tests/fixtures/persisted/memory/<release>/`` were written by
that release's own CLI (``freeze.py`` beside them) and are never regenerated.
This module needs no memtomem engine: it reads the databases with ``sqlite3``
and the day files as text, and holds them to the scenario definitions and to
the names HEAD derives for them. What HEAD's readers make of these stores is
``test_frozen_memory_real.py``, which runs against real memtomem.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncmill.memory_backend import _day_file, _is_day_file, _namespace_day_file_suffixes

from frozen_memory_support import RELEASES, Frozen, normal_tags

#: Tables memtomem keys by source path. A release-written store leaves the ones
#: its memtomem version has empty; schema 1 (memtomem 0.3) has none of them.
SOURCE_TABLES = (
    "source_index_receipts",
    "held_sources",
    "pending_source_checks",
    "source_visibility_generations",
)
#: The kind tags the release read searched by (``freeze.py`` ``KINDS``).
KINDS = {
    "task-result", "room-result", "pipeline-stage", "council-proposal",
    "council-critique", "decompose-plan", "decompose-subtask",
}
#: What ``_namespace_day_file_suffixes`` reads from a memtomem config.
STORE_CONFIG = SimpleNamespace(namespace=SimpleNamespace(default_namespace="default"))

pytestmark = pytest.mark.parametrize("release", RELEASES)


@pytest.fixture(autouse=True, scope="module")
def fixtures_stay_as_frozen():
    yield
    for release in RELEASES:
        Frozen(release).verify_pins()


def test_the_fixture_files_are_the_frozen_bytes(release):
    Frozen(release).verify_pins()


def test_each_store_holds_exactly_the_rows_its_scenarios_name(release, tmp_path):
    frozen = Frozen(release)
    for name in frozen.stores:
        store = frozen.copy(name, tmp_path)
        rows = store.rows()
        assert sorted(row.key for row in rows.values()) == frozen.expected(name), name
        assert rows, name

    # A run whose agent failed shared nothing: its token is in no row.
    store = frozen.copy("store", tmp_path / "again")
    failed = frozen.scenario("failed")
    assert failed["rows"] == []
    assert not [r for r in store.rows().values() if failed["token"] in r.content]


def test_a_shared_copy_names_the_private_row_it_was_copied_from(release, tmp_path):
    frozen = Frozen(release)
    copies = 0
    for name in frozen.stores:
        rows = frozen.copy(name, tmp_path).rows()
        for row in rows.values():
            sources = [t.split("=", 1)[1] for t in row.tags if t.startswith("shared-from=")]
            if not sources:
                continue
            copies += 1
            assert row.namespace == "shared" and len(sources) == 1
            private = rows[sources[0]]
            assert private.namespace.startswith("agent-runtime:")
            assert private.content == row.content
            assert set(row.tags) == {*private.tags, f"shared-from={private.id}"}
    assert copies


def test_head_derives_each_day_file_name_and_its_namespace(release, tmp_path):
    frozen = Frozen(release)
    for name in frozen.stores:
        store = frozen.copy(name, tmp_path)
        rows = store.rows()
        namespaces = sorted({row.namespace for row in rows.values()})
        files = {p.name for p in store.day_files()}
        assert files == {_day_file(store.mem, ns, frozen.date).name for ns in namespaces}
        for ns in namespaces:
            owned = {
                f for f in files
                if any(
                    _is_day_file(f, suffix)
                    for suffix in _namespace_day_file_suffixes(STORE_CONFIG, ns)
                )
            }
            assert owned == {_day_file(store.mem, ns, frozen.date).name}, ns
        for row in rows.values():
            assert Path(row.source_file).name == _day_file(
                store.mem, row.namespace, frozen.date
            ).name


def test_a_day_file_holds_the_entries_the_scenarios_wrote(release, tmp_path):
    frozen = Frozen(release)
    for name in frozen.stores:
        store = frozen.copy(name, tmp_path)
        entries: dict[str, int] = {}
        for scenario in frozen.index["scenarios"].values():
            if scenario["store"] != name:
                continue
            # A private entry too long for one chunk is several rows; each of
            # its chunks is copied into `shared` as an entry of its own.
            per_entry = scenario.get("private_chunks_per_entry", 1)
            for row in scenario["rows"]:
                private = row["namespace"].startswith("agent-runtime:")
                share = 1 / per_entry if private else 1
                entries[row["namespace"]] = entries.get(row["namespace"], 0) + share
        for path in store.day_files():
            text = path.read_text(encoding="utf-8")
            ns = next(
                ns for ns in entries if _day_file(store.mem, ns, frozen.date).name == path.name
            )
            assert len(re.findall(r"(?m)^> created: ", text)) == entries[ns], path.name
            assert len(re.findall(r"(?m)^## Entry ", text)) == entries[ns], path.name
            assert frozen.index["generating_root"] not in text


def test_the_release_read_is_tied_to_the_stored_rows(release, tmp_path):
    frozen = Frozen(release)
    for name in frozen.stores:
        rows = frozen.copy(name, tmp_path).rows()
        read = frozen.read[name]
        assert set(read["get"]) == set(rows)
        for chunk_id, row in rows.items():
            got = read["get"][chunk_id]
            assert got["id"] == chunk_id
            assert got["namespace"] == row.namespace
            assert got["content"] == row.content
            assert tuple(sorted(got["tags"])) == row.tags
            assert got["source"] == row.source_file
        by_namespace: dict[str, list[str]] = {}
        for row in rows.values():
            by_namespace.setdefault(row.namespace, []).append(row.id)
        assert read["count"] == {ns: len(ids) for ns, ids in by_namespace.items()}
        # Every stored body carries the common token, so the release's own
        # search for it returns the whole namespace, and only that namespace.
        assert all(frozen.anchor in row.content for row in rows.values())
        assert read["search"] == {ns: sorted(ids) for ns, ids in by_namespace.items()}
        # Every release's backend search takes a tag filter, so the release
        # searched every namespace by every kind.
        assert set(read["search_by_kind"]) == set(by_namespace)
        for ns, by_kind in read["search_by_kind"].items():
            assert set(by_kind) == KINDS, ns
            for kind, ids in by_kind.items():
                assert ids == sorted(
                    r.id for r in rows.values() if r.namespace == ns and kind in r.tags
                ), (ns, kind)
    kinds = {
        tag
        for row in frozen.copy("store", tmp_path / "kinds").rows().values()
        for tag in normal_tags(list(row.tags))
    }
    assert KINDS <= kinds


def test_only_the_source_path_binds_a_store_to_where_it_was_written(release, tmp_path):
    frozen = Frozen(release)
    root = frozen.index["generating_root"]
    for name in frozen.stores:
        store = frozen.copy(name, tmp_path)
        conn = sqlite3.connect(store.db)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )]
            holding = set()
            for table in tables:
                try:
                    columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                    for column in columns:
                        hit = conn.execute(
                            f'SELECT 1 FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ? '
                            "LIMIT 1", (f"%{root}%",),
                        ).fetchone()
                        if hit:
                            holding.add((table, column))
                except sqlite3.OperationalError:
                    continue  # a virtual table whose module is not loaded here
            assert holding == {
                ("chunks", "source_file"),
                ("chunks_fts", "source_file"),
                ("chunks_fts_content", "c1"),
            }, name
            # The stored path is the day file under the generating directory.
            for (path,) in conn.execute("SELECT source_file FROM chunks"):
                assert str(Path(path).parent) == store.generating_mem
            assert conn.execute(
                "SELECT COUNT(*) FROM chunks c JOIN chunks_fts f ON f.rowid = c.rowid "
                "WHERE f.source_file != c.source_file"
            ).fetchone() == (0,)
            present = [t for t in SOURCE_TABLES if t in tables]
            memtomem = tuple(int(part) for part in frozen.index["memtomem"].split(".")[:2])
            expected = list(SOURCE_TABLES) if memtomem >= (0, 6) else []
            assert present == expected, name
            for table in present:
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,), table
            assert conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE project_root IS NOT NULL"
            ).fetchone() == (0,)
        finally:
            conn.close()


def test_the_store_was_written_with_no_embedding_provider(release, tmp_path):
    """A search finds a released row only through a word (the tests rely on
    the scenario tokens), because the store has no vectors at all."""
    frozen = Frozen(release)
    for name in frozen.stores:
        conn = sqlite3.connect(frozen.copy(name, tmp_path).db)
        try:
            meta = dict(conn.execute("SELECT key, value FROM _memtomem_meta"))
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
        assert (meta["embedding_provider"], meta["embedding_dimension"]) == ("none", "0"), name
        assert not [t for t in tables if t.startswith("chunks_vec")], name


def test_the_killed_runs_row_is_only_in_the_wal(release, tmp_path):
    """The SIGKILLed board run is the last scenario, and its stage row never
    reached the main database file: a reader that ignores the WAL misses it."""
    frozen = Frozen(release)
    killed = frozen.scenario("board-killed")
    store = frozen.copy("store", tmp_path / "whole")
    wal = store.db.with_name(store.db.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    # The main file's bytes are taken before anything opens the copy: closing
    # an ordinary connection checkpoints the WAL into it.
    alone = tmp_path / "main-file-only"
    alone.mkdir()
    (alone / store.db.name).write_bytes(store.db.read_bytes())
    (row,) = frozen.scenario_rows(store, "board-killed")
    assert row.namespace == f"board-run:{killed['run_id']}"
    conn = sqlite3.connect(f"file:{alone / store.db.name}?mode=ro&immutable=1", uri=True)
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM chunks")}
    finally:
        conn.close()
    assert row.id not in ids
    assert ids == set(store.rows()) - {row.id}
