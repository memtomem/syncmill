"""Schema-1 memtomem stores written by released syncmill: the stored bytes (#271).

The stores under ``tests/fixtures/persisted/memory/schema1/<release>/`` were
written by v0.1.x's own CLI through memtomem 0.3.x (``schema1/freeze.py``) and
are never regenerated. No memtomem engine here: the databases are read with
``sqlite3`` and the day files as bytes, and both are held to what generation
recorded and proved (the entries each step appended, the chunks the release's
own chunker derived from them). What HEAD's readers make of these stores is
``test_frozen_memory_schema1_real.py``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncmill.memory_backend import _day_file, _is_day_file, _namespace_day_file_suffixes

from frozen_memory_schema1_support import (
    SCHEMA1_RELEASES,
    Schema1Frozen,
    full_rows,
    load_freeze,
)
from frozen_memory_support import RELEASES

STORE_CONFIG = SimpleNamespace(namespace=SimpleNamespace(default_namespace="default"))
SOURCE_TABLES = (
    "source_index_receipts",
    "held_sources",
    "pending_source_checks",
    "source_visibility_generations",
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

freeze = load_freeze()

pytestmark = pytest.mark.parametrize("release", SCHEMA1_RELEASES)


@pytest.fixture(autouse=True, scope="module")
def fixtures_stay_as_frozen():
    yield
    for release in SCHEMA1_RELEASES:
        Schema1Frozen(release).verify_pins()


def test_the_memtomem_06_stores_are_exactly_the_memory_1a_releases(release):
    """The schema-1 fixtures sit one directory down, so the Memory-1a tests
    keep running over exactly their own releases."""
    assert RELEASES == ["v0.2.0", "v0.3.0"]
    assert release not in RELEASES


def test_the_fixture_files_are_the_frozen_bytes(release):
    frozen = Schema1Frozen(release)
    frozen.verify_pins()
    assert frozen.index["release"] == release.removeprefix("v")
    assert frozen.index["memtomem"].startswith("0.3.")


def test_the_stored_rows_are_the_rows_generation_recorded(release, tmp_path):
    frozen = Schema1Frozen(release)
    assert full_rows(frozen.copy(tmp_path).db) == frozen.index["rows"]


def _boundaries(frozen: Schema1Frozen) -> dict[str, list[dict]]:
    return {name: entries for name, entries in frozen.index["entries"].items()}


def test_each_day_file_is_exactly_its_recorded_entries(release, tmp_path):
    """Generation proved every step only appended; the recorded byte offsets
    cut each frozen file into exactly the entries it records, in write order,
    and the files the release's board GC deleted are gone."""
    frozen = Schema1Frozen(release)
    store = frozen.copy(tmp_path)
    on_disk = {p.name for p in store.day_files()}
    assert on_disk == set(frozen.index["entries"]) - frozen.purged
    # v0.1.4's board GC deleted the finished board run's scratch file; v0.1.2's
    # scratch text is in the one `<date>.md`, which nothing deletes.
    assert bool(frozen.purged) == (frozen.index["layout"] == "split")
    for name, entries in _boundaries(frozen).items():
        if name in frozen.purged:
            continue
        data = (store.mem / name).read_bytes()
        steps = [e["step"] for e in entries]
        assert steps == sorted(steps), name
        start = 0
        for entry in entries:
            assert entry["end"] > start, name
            (parsed,) = freeze.parse_entries(data[start:entry["end"]].decode("utf-8"))
            assert (parsed["title"], parsed["tags"], parsed["body"]) == (
                entry["title"], entry["tags"], entry["body"]
            ), (name, entry["step"])
            start = entry["end"]
        assert start == len(data), name
        # Read whole, the parser finds the same boundaries on its own: a copy
        # ending in an absorbed section's quote, followed by the next titled
        # entry, is still one entry.
        assert [(e["title"], e["tags"], e["body"])
                for e in freeze.parse_entries(data.decode("utf-8"))] == [
            (e["title"], e["tags"], e["body"]) for e in entries], name
        assert frozen.index["generating_root"].encode() not in data


def _results(frozen: Schema1Frozen) -> list[dict]:
    """The entries a result share wrote: v0.1.4's private ones, v0.1.2's
    direct `shared` ones."""
    role = {"split": "private", "mixed": "direct"}[frozen.index["layout"]]
    assert not frozen.entries({"split": "direct", "mixed": "private"}[frozen.index["layout"]])
    return frozen.entries(role)


def test_the_result_entries_are_the_scenarios_results(release):
    """Each scenario's results are the wrappers of its own prompt, in its own
    steps: v0.1.4 in the agent's own file with the kind and task tags, v0.1.2
    in the one `<date>.md`, tagged `task-result` and the agent only."""
    frozen = Schema1Frozen(release)
    split = frozen.index["layout"] == "split"
    for name, facts in frozen.index["scenarios"].items():
        if "prompt" not in facts:
            continue
        mine = [e for e in _results(frozen) if e["step"] in frozen.scenario_steps(name)]
        for entry in mine:
            assert entry["body"].startswith(f"## Task Result\n\n**Prompt:** {facts['prompt']}\n\n")
            (agent,) = [t for t in entry["tags"] if t in ("codex", "kimi-code")]
            assert f"**Agent:** {agent}\n\n**Output:**\n" in entry["body"]
            if split:
                assert entry["namespace"] == f"agent-runtime:{agent}"
                assert entry["file"] == _day_file(Path("."), entry["namespace"], frozen.date).name
            else:
                assert entry["namespace"] == "shared" and entry["file"] == f"{frozen.date}.md"
                assert sorted(entry["tags"]) == sorted(["task-result", agent])
        assert bool(mine) == (name not in ("failed", "room-apply")), name


def test_the_shared_copies_are_exactly_the_chunks_the_private_writes_produced(release):
    frozen = Schema1Frozen(release)
    copies = frozen.entries("copy")
    # v0.1.2 wrote results to `shared` directly: no private row, no copy.
    assert bool(copies) == (frozen.index["layout"] == "split")
    for index, step in enumerate(frozen.steps):
        produced = [
            chunk for e in frozen.entries("private") if e["step"] == index
            for chunk in e["produced"]
        ]
        mine = [c["body"] for c in copies if c["step"] == index]
        freeze.check_copies(step["scenario"], mine, produced)
    for copy in copies:
        assert copy["namespace"] == "shared" and copy["title"].startswith("Entry ")
        (tag,) = copy["tags"]
        assert tag.startswith("shared-from=") and _UUID.fullmatch(tag.split("=", 1)[1])
    # A body long enough for two chunks produced two (and, on v0.1.4, was
    # copied as two entries).
    (long_entry,) = [
        e for e in _results(frozen) if e["step"] in frozen.scenario_steps("longprompt")
    ]
    assert len(long_entry["produced"]) >= 2


def test_every_stored_row_is_a_chunk_some_write_produced(release):
    """The recorded derivation is what the stored rows are made of: no row
    content the release's chunker never produced for that file."""
    frozen = Schema1Frozen(release)
    produced: dict[str, Counter] = {}
    for entry in frozen.entries():
        produced.setdefault(entry["file"], Counter()).update(entry["produced"] or [])
    for row in frozen.index["rows"]:
        assert produced[row["source"]][row["content"]] > 0, (row["source"], row["id"])
        file_ns = freeze._day_file_namespace(row["source"], frozen.date)
        if file_ns is None:  # v0.1.2: some write's namespace
            assert row["namespace"] in {e["namespace"] for e in frozen.entries()}
        elif file_ns != "shared":
            assert row["namespace"] == file_ns


def test_the_copy_check_refuses_a_duplicated_head_for_a_missing_tail(release):
    """Driven with the long prompt's two produced chunks (on v0.1.4 also its
    two real copies)."""
    frozen = Schema1Frozen(release)
    (step,) = frozen.scenario_steps("longprompt")
    produced = [c for e in _results(frozen) if e["step"] == step for c in e["produced"]]
    copies = [c["body"] for c in frozen.entries("copy") if c["step"] == step] or list(produced)
    assert len(copies) >= 2
    freeze.check_copies("longprompt", copies, produced)
    forged = [copies[0]] * len(copies)
    assert forged != copies
    with pytest.raises(freeze.GenerationError, match="not the chunks"):
        freeze.check_copies("longprompt", forged, produced)
    # The same chunks, one of them copied twice: only the counts differ.
    with pytest.raises(freeze.GenerationError, match="not the chunks"):
        freeze.check_copies("longprompt", copies + copies[:1], produced)


def test_the_append_check_refuses_a_rewritten_or_vanished_file(release):
    before = {"a.md": b"\n> created: x\n\n## A\n", "b.md": b"one"}
    gained = freeze.check_append_only(before, {"a.md": before["a.md"] + b"more", "b.md": b"one"})
    assert gained == {"a.md": b"more"}
    with pytest.raises(freeze.GenerationError, match="rewritten"):
        freeze.check_append_only(before, {"a.md": b"\n> created: y\n\n## A\nmore", "b.md": b"one"})
    with pytest.raises(freeze.GenerationError, match="disappeared"):
        freeze.check_append_only(before, {"a.md": before["a.md"]})
    assert freeze.check_append_only(before, {"a.md": before["a.md"]},
                                    may_vanish=lambda n: n == "b.md") == {}


def test_an_inner_quote_of_a_merged_copy_is_not_an_entry(release):
    """A merged chunk keeps the quote lines of the sections it absorbed, so a
    copy of it holds `> created:` lines; the parser reads one entry."""
    frozen = Schema1Frozen(release)
    merged = [r["content"] for r in frozen.index["rows"] if "\n> created: " in r["content"]]
    assert merged
    block = (f"\n## Entry {frozen.date}T00:00:00+00:00\n\n> created: {frozen.date}T00:00:00+00:00"
             f"\n> tags: [\"shared-from=00000000-0000-0000-0000-000000000000\"]\n\n"
             f"{merged[0]}\n")
    (entry,) = freeze.parse_entries(block)
    assert entry["body"] == merged[0]


def test_the_findings_follow_from_the_stored_rows(release, tmp_path):
    frozen = Schema1Frozen(release)
    rows = frozen.index["rows"]
    ids = {r["id"] for r in rows}
    named = {t.split("=", 1)[1] for r in rows for t in r["tags"] if t.startswith("shared-from=")}
    findings = frozen.index["findings"]
    split = frozen.index["layout"] == "split"
    # Copies whose source row a later re-chunk replaced name no row at all.
    assert findings["dangling_shared_from"] == sorted(named - ids)
    assert bool(findings["dangling_shared_from"]) == split
    unindexed = [e for e in frozen.entries() if not e["indexed"]]
    assert [{"file": e["file"], "end": e["end"], "role": e["role"]} for e in unindexed] == (
        findings["unindexed_entries"]
    )
    # An unindexed entry is on disk and inside no stored row's line span.
    store = frozen.copy(tmp_path)
    for entry in unindexed:
        data = (store.mem / entry["file"]).read_bytes()
        first = data[:entry["end"] - len(entry["body"].encode()) - 1].count(b"\n") + 1
        last = data[:entry["end"]].count(b"\n")
        for row in rows:
            if row["source"] == entry["file"]:
                assert row["end_line"] < first or row["start_line"] > last, row["id"]
    copied_from = {c["tags"][0].split("=", 1)[1] for c in frozen.entries("copy")}
    assert set(findings["unverified_shared_from"]) <= copied_from
    # A row is mislabeled when its text comes from an entry of another
    # namespace: on v0.1.2 a later write stamps its namespace on text a purge
    # or a merge handed it, so scratch text sits in `shared` and shared text in
    # a scratch namespace.
    mislabeled = []
    for row in rows:
        spans = freeze.entry_lines(store.mem / row["source"], [
            e for e in frozen.entries() if e["file"] == row["source"]])
        owners = {ns for (first, last), ns in spans
                  if not (row["end_line"] < first or row["start_line"] > last)}
        if owners != {row["namespace"]}:
            mislabeled.append({"id": row["id"], "namespace": row["namespace"],
                               "entry_namespaces": sorted(owners)})
    assert mislabeled == findings["mislabeled_rows"]
    assert bool(mislabeled) == (not split)
    if not split:
        assert any(m["namespace"] == "shared" for m in mislabeled)
        assert any(m["namespace"].startswith("board-run:") and "shared" in m["entry_namespaces"]
                   for m in mislabeled)


def test_head_derives_the_released_day_file_names(release, tmp_path):
    """v0.1.4 already named its day files the way HEAD does, so HEAD's purge
    and GC find a released namespace's file by name. v0.1.2's one `<date>.md`
    belongs to no namespace: HEAD never deletes it."""
    frozen = Schema1Frozen(release)
    store = frozen.copy(tmp_path)
    for path in store.day_files():
        ns = freeze._day_file_namespace(path.name, frozen.date)
        if ns is None:
            assert frozen.index["layout"] == "mixed" and path.name == f"{frozen.date}.md"
            assert not [
                other for other in {r["namespace"] for r in frozen.index["rows"]}
                if any(_is_day_file(path.name, s)
                       for s in _namespace_day_file_suffixes(STORE_CONFIG, other))
            ]
            continue
        assert path.name == _day_file(store.mem, ns, frozen.date).name
        owners = [
            other for other in {r["namespace"] for r in frozen.index["rows"]}
            if any(_is_day_file(path.name, s)
                   for s in _namespace_day_file_suffixes(STORE_CONFIG, other))
        ]
        assert owners == ([ns] if ns in {r["namespace"] for r in frozen.index["rows"]} else [])


def test_the_store_is_schema_1_with_no_embedding_provider(release, tmp_path):
    frozen = Schema1Frozen(release)
    conn = sqlite3.connect(frozen.copy(tmp_path).db)
    try:
        meta = dict(conn.execute("SELECT key, value FROM _memtomem_meta"))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert meta["schema_version"] == "1"
    assert (meta["embedding_provider"], meta["embedding_dimension"]) == ("none", "0")
    assert not [t for t in tables if t.startswith("chunks_vec")]
    assert not set(SOURCE_TABLES) & tables


def test_only_the_source_path_binds_the_store_to_where_it_was_written(release, tmp_path):
    frozen = Schema1Frozen(release)
    root = frozen.index["generating_root"]
    store = frozen.copy(tmp_path)
    conn = sqlite3.connect(store.db)
    try:
        holding = set()
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            try:
                for column in [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]:
                    if conn.execute(
                        f'SELECT 1 FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ? LIMIT 1',
                        (f"%{root}%",),
                    ).fetchone():
                        holding.add((table, column))
            except sqlite3.OperationalError:
                continue
        assert holding == {
            ("chunks", "source_file"), ("chunks_fts", "source_file"), ("chunks_fts_content", "c1"),
        }
        for (path,) in conn.execute("SELECT source_file FROM chunks"):
            assert str(Path(path).parent) == store.generating_mem
    finally:
        conn.close()


def test_the_killed_runs_row_is_only_in_the_wal(release, tmp_path):
    frozen = Schema1Frozen(release)
    killed = frozen.scenario("board-killed")
    store = frozen.copy(tmp_path / "whole")
    wal = store.db.with_name(store.db.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    alone = tmp_path / "main-file-only"
    alone.mkdir()
    (alone / store.db.name).write_bytes(store.db.read_bytes())
    killed_rows = {r["id"] for r in frozen.index["rows"]
                   if r["namespace"] == f"board-run:{killed['run_id']}"}
    assert killed_rows
    conn = sqlite3.connect(f"file:{alone / store.db.name}?mode=ro&immutable=1", uri=True)
    try:
        main = sorted(r[0] for r in conn.execute("SELECT id FROM chunks"))
    finally:
        conn.close()
    # The whole killed step is in the WAL: its GC of the finished board run's
    # scratch namespace and its own scratch write. The main file alone is the
    # store as it stood before the run started.
    assert main == killed["rows_before"]
    assert not killed_rows & set(main)
    (step,) = [s for s in frozen.steps if s["label"] == "run (killed)"]
    assert step["purged_namespaces"] == [f"board-run:{frozen.scenario('board-done')['run_id']}"]


def test_the_release_read_is_tied_to_the_stored_rows(release):
    frozen = Schema1Frozen(release)
    read = frozen.read["store"]
    rows = {r["id"]: r for r in frozen.index["rows"]}
    assert set(read["get"]) == set(rows)
    for chunk_id, row in rows.items():
        got = read["get"][chunk_id]
        assert (got["namespace"], got["content"], sorted(got["tags"])) == (
            row["namespace"], row["content"], row["tags"])
    by_ns = Counter(r["namespace"] for r in rows.values())
    assert read["count"] == dict(by_ns)
    assert read["search"] == {
        ns: sorted(i for i, r in rows.items() if r["namespace"] == ns) for ns in by_ns
    }
    # No kind tag was ever indexed, so every kind search the release ran
    # came back empty although the unfiltered one did not.
    assert read["search_by_kind"] and all(
        ids == [] for by_kind in read["search_by_kind"].values() for ids in by_kind.values()
    )
    assert json.dumps(read).count(frozen.anchor) >= len(rows)
