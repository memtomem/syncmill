"""Mem0 and LangGraph stores written by released syncmill: the stored bytes (#271).

The stores under ``tests/fixtures/persisted/memory/backends/<release>/`` were
written by that release's own CLI and backend code (``freeze.py`` beside them)
and are never regenerated. This module needs neither SDK: it reads the Qdrant
local points with a restricted unpickler and the LangGraph values as JSON, and
holds them to the scenario definitions. ``LangGraphBackend._row`` maps a stored
item without its SDK, so it is checked here too; Mem0's ``_row`` maps what the
Mem0 SDK returns, not what Qdrant stores, so it is left to the real tests
(``test_frozen_memory_backends_real.py``).
"""

from __future__ import annotations

import io
import pickle
import sqlite3
from types import SimpleNamespace

import pytest

from syncmill.memory_backend import LangGraphBackend

from frozen_memory_backends_support import (
    BACKEND_RELEASES,
    FREEZE,
    BackendsFrozen,
    row_key,
    scenario_key,
)
from frozen_memory_support import normal_tags

pytestmark = pytest.mark.parametrize("release", BACKEND_RELEASES)

_ALL_KINDS = {
    "task-result", "room-result", "pipeline-stage", "council-proposal",
    "council-critique", "decompose-plan", "decompose-subtask",
}
#: The kind tags each release's writer put on a stored row: v0.1.2 tagged every
#: result `task-result`; v0.1.x rooms shared nothing.
KINDS_WRITTEN = {
    "v0.1.2": {"task-result"},
    "v0.1.4": _ALL_KINDS - {"room-result"},
    "v0.3.0": _ALL_KINDS,
}
#: Releases whose board GC collected a finished run's scratch namespace when the
#: next run claimed (the killed run, in the scenario store).
GC_ON_CLAIM = {"v0.1.2", "v0.1.4"}


@pytest.fixture(autouse=True, scope="module")
def fixtures_stay_as_frozen():
    yield
    for release in BACKEND_RELEASES:
        BackendsFrozen(release).verify_pins()


def test_the_fixture_files_are_the_frozen_bytes(release):
    BackendsFrozen(release).verify_pins()


def test_each_store_holds_exactly_the_rows_its_scenarios_name(release, tmp_path):
    frozen = BackendsFrozen(release)
    assert frozen.stores() == ["langgraph/bulk", "langgraph/store", "mem0/bulk", "mem0/store"]
    for name in frozen.stores():
        rows = frozen.copy(name, tmp_path).rows()
        # Rows a later step of the release deleted (v0.1.x's board GC took the
        # finished run's scratch namespace when the next run claimed).
        vanished = frozen.index["vanished"].get(name, {})
        assert not set(vanished) & set(rows), name
        # Each scenario's recorded ids are its rows, and every row belongs to one.
        owned = [i for s in frozen.scenarios(name).values() for i in s["ids"]]
        assert len(set(owned)) == len(owned), name
        assert sorted(owned) == sorted([*rows, *vanished]), name
        for scenario, spec in frozen.scenarios(name).items():
            mine = sorted(row_key(rows[i] if i in rows else vanished[i]) for i in spec["ids"])
            assert mine == sorted(scenario_key(r) for r in spec["rows"]), (name, scenario)
        # index.json's row table is the stored rows, ids included.
        assert {
            i: {k: r[k] for k in ("namespace", "tags", "content")} for i, r in rows.items()
        } == frozen.index["rows"][name], name
    # Only the release's board GC deleted rows, and only finished scratch runs.
    for name, gone in frozen.index["vanished"].items():
        assert frozen.release in GC_ON_CLAIM, name
        done = frozen.scenario(name, "board-done")
        assert set(gone) == {i for i in done["ids"] if i not in frozen.index["rows"][name]}, name
        assert {r["namespace"] for r in gone.values()} == {f"board-run:{done['run_id']}"}
        assert {r["during"] for r in gone.values()} == {"board-killed"}


def test_the_scenarios_cover_every_shape_the_readers_are_held_to(release, tmp_path):
    """The coverage the real tests rely on, stated on the stored rows."""
    frozen = BackendsFrozen(release)
    for backend in ("mem0", "langgraph"):
        store = f"{backend}/store"
        rows = frozen.index["rows"][store]
        kinds = {t for r in rows.values() for t in (r["tags"] or [])} & set(FREEZE.m1.KINDS)
        assert kinds == KINDS_WRITTEN[release], store
        assert any("\r\n" in (r["content"] or "") for r in rows.values()), store
        assert any("### Findings" in (r["content"] or "") for r in rows.values()), store
        scratch = sum(1 for r in rows.values() if (r["namespace"] or "").startswith("board-run:"))
        assert scratch == (1 if release in GC_ON_CLAIM else 2), store
        bulk = frozen.index["rows"][f"{backend}/bulk"]
        per_ns: dict[str, int] = {}
        for r in bulk.values():
            per_ns[r["namespace"]] = per_ns.get(r["namespace"], 0) + 1
        run_id = frozen.scenario(f"{backend}/bulk", "board-done")["run_id"]
        # Both namespaces pass one 100-row page.
        assert per_ns == {"shared": FREEZE.BULK_SHARED + 1, f"board-run:{run_id}": FREEZE.BULK_SCRATCH}


def test_the_foreign_rows_are_the_shapes_syncmill_never_writes(release, tmp_path):
    frozen = BackendsFrozen(release)
    for backend in ("mem0", "langgraph"):
        store = f"{backend}/store"
        rows = frozen.copy(store, tmp_path).rows()
        ids = frozen.scenario(store, "foreign")["foreign_ids"]
        unowned, untagged = rows[ids["unowned"]], rows[ids["untagged"]]
        assert unowned["namespace"] is None and unowned["content"] == FREEZE.FOREIGN_TEXT["unowned"]
        assert untagged["namespace"] == "shared" and untagged["tags"] is None
        if backend == "mem0":
            payload = unowned["raw"]["payload"]
            assert payload["agent_id"] == "foreign-agent" and "user_id" not in payload
            assert "syncmill_tags" not in untagged["raw"]["payload"]
        else:
            assert "namespace" not in unowned["raw"]["value"]
            assert "tags" not in untagged["raw"]["value"]
        # Every other row is syncmill's own.
        others = [r for i, r in rows.items() if i not in ids.values()]
        assert all(r["namespace"] and isinstance(r["tags"], list) for r in others), store


def test_every_syncmill_mem0_point_carries_its_bm25_vector(release, tmp_path):
    """Mem0 2.0 searches hybrid. A point written while the BM25 encoder could
    not load has no sparse vector, and keyword search would never see it."""
    frozen = BackendsFrozen(release)
    for store in frozen.stores("mem0"):
        points = FREEZE.qdrant_points(frozen.copy(store, tmp_path).path)
        foreign = set(frozen.scenario("mem0/store", "foreign")["foreign_ids"].values())
        assert points
        for point_id, point in points.items():
            assert point["dense_dims"] == 384, point_id
            if point_id not in foreign:
                assert point["bm25_terms"] and point["bm25_terms"] > 0, point_id
                # No NLP lemmatizer in a default install: the text is kept as is.
                payload = point["payload"]
                assert payload["text_lemmatized"] == payload["data"], point_id


def test_langgraph_items_are_indexed_and_mapped_by_head(release, tmp_path):
    """``LangGraphBackend._row`` reads the stored value as it is: every
    syncmill item maps to its namespace, tags and content, and the item with
    no namespace maps to nothing."""
    frozen = BackendsFrozen(release)
    for store in frozen.stores("langgraph"):
        copy = frozen.copy(store, tmp_path)
        items = FREEZE.langgraph_items(copy.path)
        rows = frozen.index["rows"][store]
        assert sorted(items) == sorted(rows)
        for key, item in items.items():
            mapped = LangGraphBackend._row(SimpleNamespace(key=key, value=item["value"]))
            expected = rows[key]
            if expected["namespace"] is None:
                assert mapped is None, key
                continue
            assert mapped["id"] == key
            assert (mapped["namespace"], mapped["content"]) == (
                expected["namespace"], expected["content"])
            assert mapped["tags"] == (expected["tags"] or []), key
            assert mapped["source"] == "langgraph"
            # One content embedding per item (index=["content"]).
            assert [field for field, _ in item["vectors"]] == ["content"], key


def test_no_store_holds_the_directory_it_was_written_in(release):
    """The stores are not bound to their location: only ``index.json`` (which
    records it) names the generating directory."""
    frozen = BackendsFrozen(release)
    roots = {frozen.index["generating_root"].encode()} | {
        spec["root"].encode() for spec in frozen.index["stores"].values()
    }
    for relative in frozen.index["files"]:
        data = (frozen.root / relative).read_bytes()
        for root in roots:
            assert root not in data, relative


def test_the_killed_board_run_left_its_lease_and_its_scratch_row(release, tmp_path):
    frozen = BackendsFrozen(release)
    for backend in ("mem0", "langgraph"):
        store = f"{backend}/store"
        killed = frozen.scenario(store, "board-killed")
        copy = frozen.copy(store, tmp_path)
        (row_id,) = killed["ids"]
        row = copy.rows()[row_id]
        assert row["namespace"] == f"board-run:{killed['run_id']}"
        # v0.1.2 tagged it `task-result` and the agent; later releases name the
        # stage's kind and the task.
        assert normal_tags(row["tags"]) == (
            ("codex", "task-result") if release == "v0.1.2"
            else ("codex", "pipeline-stage", "task:*"))
        conn = sqlite3.connect(copy.board)
        try:
            state, lease, claimed = conn.execute(
                "SELECT state, lease_run_id, claimed_at FROM work_items WHERE id = ?",
                (killed["item_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert (state, lease, claimed) == ("running", killed["run_id"], killed["claimed_at"])
        # The stores themselves were left with no journal; the board kept its WAL.
        assert killed["leftovers"] == ["board.db-shm", "board.db-wal"]


def test_the_restricted_unpickler_refuses_anything_but_the_two_point_classes(release):
    class Evil:
        def __reduce__(self):
            return (print, ("unpickled",))

    with pytest.raises(pickle.UnpicklingError, match="refused builtins.print"):
        FREEZE._QdrantUnpickler(io.BytesIO(pickle.dumps(Evil()))).load()
