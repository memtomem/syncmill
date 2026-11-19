"""`memory migration` artifacts written by released syncmill, applied, verified and
rolled back by HEAD (#271).

Opt-in with the Mem0/LangGraph real tests (``SYNCMILL_TEST_MEMORY_BACKENDS_REAL=1``;
CI's ``memory-backends-real`` job): it needs the ``mem0`` and ``langgraph``
extras and the FastEmbed cache ``models.json`` names. Each test copies a
release's whole box (sources, destinations, artifacts), rebinds it (memtomem's
``chunks.source_file`` and each manifest's ``target_storage_digest``; see
``MigrationCopy.rebind``), and runs HEAD's ``syncmill memory migration`` on one
released artifact. ``test_frozen_memory_migration.py`` checks the same files
without an SDK.

Every test also holds what the command must leave alone: the destination rows
of every other artifact, and the source rows (and, for Mem0 and LangGraph, the
source files' bytes).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from syncmill.cli import cli
from syncmill.config import MemoryConfig
from syncmill.memory import run_async
from syncmill.memory_backend import build_backend
from syncmill.memory_migration import _target_id

from frozen_memory_migration_support import (
    FREEZE,
    MIGRATION_RELEASES,
    MigrationFrozen,
    memory_settings,
    memtomem_created_at,
    release_memtomem,
    selected_by_default,
    tree_files,
)
from frozen_memory_support import RELEASES as MEMORY_RELEASES
from frozen_memory_schema1_support import SCHEMA1_RELEASES, Schema1Frozen
from frozen_memory_support import Frozen

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SYNCMILL_TEST_MEMORY_BACKENDS_REAL") != "1",
        reason="set SYNCMILL_TEST_MEMORY_BACKENDS_REAL=1 to run the real migration tests",
    ),
]

_FROZEN = {r: MigrationFrozen(r) for r in MIGRATION_RELEASES}


def _cases(*states: str) -> list:
    return [pytest.param(release, label, id=f"{release}:{label}")
            for release, frozen in _FROZEN.items() for label in frozen.labels
            if frozen.entry(label)["state"] in states]


@pytest.fixture(autouse=True)
def engine_environment(monkeypatch, tmp_path):
    """No ambient memtomem settings, a home of its own, a private memtomem
    runtime directory, and the exact FastEmbed files the stores were written
    with, read offline."""
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
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    cache = os.environ.get("FASTEMBED_CACHE_PATH")
    assert cache, "FASTEMBED_CACHE_PATH must name the cache models.json describes"
    models = json.loads(
        (Path(FREEZE.be.__file__).parent / "v0.3.0" / "models.json").read_text(encoding="utf-8"))
    assert FREEZE.be.model_manifest(Path(cache)) == models


@pytest.fixture(autouse=True, scope="module")
def fixtures_stay_as_frozen():
    yield
    for frozen in _FROZEN.values():
        frozen.verify_pins()


def _instants(backend: str, rows: dict[str, dict]) -> dict[str, dict]:
    """memtomem rows with their timestamps as UTC instants. HEAD's first open
    of a v0.1.x (schema 1) store rewrites every ``created_at``/``updated_at``
    into its own format, the same instant
    (``test_head_rewrites_released_memtomem_timestamps_as_the_same_instant``);
    comparisons across that open compare instants."""
    if backend != "memtomem":
        return rows
    out = {}
    for row_id, row in rows.items():
        cells = {**row["raw"]}
        for key in ("created_at", "updated_at"):
            if cells.get(key) is not None:
                cells[key] = memtomem_created_at(cells[key], "0.6")
        out[row_id] = {**{k: cells.get(k, row[k]) for k in row if k != "raw"}, "raw": cells}
    return out


class Box:
    """One rebound copy of a release's box, and HEAD's CLI over it."""

    def __init__(self, release: str, label: str, tmp_path: Path, *, rebind: bool = True):
        self.frozen = _FROZEN[release]
        self.label = label
        self.entry = self.frozen.entry(label)
        self.copy = self.frozen.copy(tmp_path)
        if rebind:
            self.copy.rebind()
        self.records = self.frozen.records(label)
        self.lineages = [r["lineage_key"] for r in self.records]
        self.namespaces = {r["namespace"] for r in self.records}
        self._others = self._other_rows()
        self._sources = self._source_state()
        self.release = release
        # Rows the release wrote; any other row in these namespaces is HEAD's.
        self._released_ids = set(self.rows())

    def run(self, *argv: str):
        result = CliRunner().invoke(
            cli, ["memory", "migration", *argv], env=self.copy.env(self.label),
            catch_exceptions=False)
        return result.exit_code, result.output

    @property
    def migration_id(self) -> str:
        return self.entry["migration_id"]

    def state(self) -> dict:
        return self.copy.state(self.label)

    def receipt(self) -> dict:
        return json.loads((self.copy.artifact_dir(self.label) / "receipt.json").read_text())

    def rollback_doc(self) -> dict:
        return json.loads((self.copy.artifact_dir(self.label) / "rollback.json").read_text())

    def rows(self) -> dict[str, dict]:
        """The destination rows in this artifact's namespaces."""
        return {i: r for i, r in self._dest_rows().items() if r["namespace"] in self.namespaces}

    def _dest_rows(self) -> dict[str, dict]:
        return _instants(self.entry["target"], self.copy.dest_rows(self.label))

    def contents(self) -> list[str]:
        return sorted(r["content"] for r in self.rows().values())

    def _other_rows(self) -> dict:
        return {i: r for i, r in self._dest_rows().items()
                if r["namespace"] not in self.namespaces}

    def _source_state(self) -> dict:
        state = {}
        for backend in ("memtomem", "mem0", "langgraph"):
            rows = {i: {k: r[k] for k in ("namespace", "tags", "content", "created_at")}
                    for i, r in _instants(backend, self.copy.source_rows(backend)).items()}
            path = self.copy.source_path(backend)
            files = tree_files(path / "mem") if backend == "memtomem" else tree_files(
                path if path.is_dir() else path.parent)
            state[backend] = (rows, files)
        return state

    def assert_nothing_else_changed(self) -> None:
        assert self._other_rows() == self._others
        assert self._source_state() == self._sources

    def runtime(self):
        """HEAD's runtime backend over the destination copy."""
        target = self.entry["target"]
        settings = memory_settings(target, self.copy.dest_path(self.entry["direction"]))
        return build_backend(MemoryConfig(backend=target, **settings))

    def assert_rows_are_the_records(self) -> None:
        rows = self.rows()
        ledger = self.state()["targets"]
        assert set(ledger) == set(self.lineages)
        assert set(ledger.values()) == set(rows)
        target = self.entry["target"]
        for record in self.records:
            target_id = ledger[record["lineage_key"]]
            row = rows[target_id]
            assert (row["namespace"], row["tags"], row["content"]) == (
                record["namespace"], record["tags"], record["content"])
            if target in ("memtomem", "langgraph"):
                assert target_id == _target_id(record["lineage_key"])
            if target == "memtomem":
                # The instant the writing memtomem stored: the release's 0.3.x
                # cut the fraction, HEAD keeps it.
                writer = (release_memtomem(self.release) if target_id in self._released_ids
                          else "0.6")
                assert row["created_at"] == memtomem_created_at(
                    memtomem_created_at(record["created_at"], writer), "0.6")
            elif target == "mem0":
                assert row["created_at"] == record["created_at"]


@pytest.mark.parametrize(("release", "label"), _cases("planned"))
def test_head_applies_and_rolls_back_a_released_plan(release, label, tmp_path):
    box = Box(release, label, tmp_path)
    assert box.rows() == {}
    code, out = box.run("apply", box.migration_id)
    assert code == 0, out
    assert f"migration applied + verified: {box.migration_id}" in out
    assert box.state()["status"] == "verified"
    box.assert_rows_are_the_records()
    box.assert_nothing_else_changed()
    code, out = box.run("rollback", box.migration_id, "--yes")
    assert code == 0, out
    assert box.rows() == {}
    assert box.rollback_doc()["deleted"] == len(box.records)
    box.assert_nothing_else_changed()


@pytest.mark.parametrize(("release", "label"), _cases("crashed"))
def test_head_resumes_a_crash_after_a_recorded_write_without_a_second_copy(
        release, label, tmp_path):
    box = Box(release, label, tmp_path)
    before = box.rows()
    (unrecorded,) = set(before) - set(box.state()["targets"].values())
    code, out = box.run("apply", box.migration_id)
    assert code == 0, out
    box.assert_rows_are_the_records()
    after = box.rows()
    assert len(after) == len(box.records)
    # The row the release wrote but never recorded is reused as it was.
    assert box.state()["targets"][box.lineages[1]] == unrecorded
    assert after[unrecorded] == before[unrecorded]
    box.assert_nothing_else_changed()


@pytest.mark.parametrize(("release", "label"), _cases("crashed-first"))
def test_a_crash_before_the_first_record_is_refused_and_its_row_survives_rollback(
        release, label, tmp_path):
    """#323, pinned as measured: the empty ledger makes apply check that the
    destination is empty, and rollback walks only the ledger."""
    box = Box(release, label, tmp_path)
    state_bytes = (box.copy.artifact_dir(label) / "state.json").read_bytes()
    before = box.rows()
    assert len(before) == 1
    code, out = box.run("apply", box.migration_id)
    assert code == 1 and "selected destination namespaces are no longer empty" in out
    assert (box.copy.artifact_dir(label) / "state.json").read_bytes() == state_bytes
    code, out = box.run("rollback", box.migration_id, "--yes")
    assert code == 0, out
    assert box.rollback_doc() == {"migration_id": box.migration_id, "status": "rolled_back",
                                  "deleted": 0, "refused": []}
    assert box.rows() == before
    box.assert_nothing_else_changed()


@pytest.mark.parametrize(("release", "label"), _cases("partial"))
def test_head_finishes_a_partial_apply(release, label, tmp_path):
    box = Box(release, label, tmp_path)
    released_errors = box.state()["errors"]
    before = box.rows()
    code, out = box.run("apply", box.migration_id)
    assert code == 0, out
    state = box.state()
    assert state["status"] == "verified"
    # HEAD keeps the released error history as it was.
    assert state["errors"] == released_errors and len(released_errors) == 1
    box.assert_rows_are_the_records()
    for row_id, row in before.items():
        assert box.rows()[row_id] == row
    box.assert_nothing_else_changed()


@pytest.mark.parametrize(("release", "label"), _cases("verified"))
def test_head_verifies_reads_and_rolls_back_a_verified_migration(release, label, tmp_path):
    box = Box(release, label, tmp_path)
    n = len(box.records)
    code, out = box.run("verify", box.migration_id)
    assert code == 0, out
    receipt = box.receipt()
    assert (receipt["status"], receipt["verified_count"], receipt["missing_target_count"]) == (
        "verified", n, 0)
    box.assert_rows_are_the_records()
    backend = box.runtime()
    try:
        for record in box.records:
            got = run_async(backend.get(box.state()["targets"][record["lineage_key"]]))
            assert (got["namespace"], got["content"], got["tags"]) == (
                record["namespace"], record["content"], record["tags"])
    finally:
        run_async(backend.close())
    code, out = box.run("rollback", box.migration_id, "--yes")
    assert code == 0, out
    assert box.rollback_doc()["deleted"] == n and box.rows() == {}
    code, out = box.run("rollback", box.migration_id, "--yes")
    assert code == 0, out
    assert box.rollback_doc() == {"migration_id": box.migration_id, "status": "rolled_back",
                                  "deleted": 0, "refused": []}
    code, out = box.run("verify", box.migration_id)
    assert code == 1 and "migration verification failed" in out
    assert (box.receipt()["missing_target_count"], box.receipt()["failure_lineages"]) == (n, [])
    box.assert_nothing_else_changed()


@pytest.mark.parametrize(("release", "label"), _cases("verification_failed"))
def test_a_released_verification_failure_stays_failed(release, label, tmp_path):
    box = Box(release, label, tmp_path)
    missing = box.state()["targets"][box.lineages[1]]
    before = box.rows()
    assert missing not in before
    code, out = box.run("verify", box.migration_id)
    assert code == 1 and "migration verification failed" in out
    assert box.receipt()["failure_lineages"] == [box.lineages[1]]
    ledger = box.state()["targets"]
    # apply skips a recorded target, so it does not write the missing row again.
    code, out = box.run("apply", box.migration_id)
    assert code == 1 and "migration verification failed" in out
    assert box.state()["targets"] == ledger
    assert box.rows() == before
    box.assert_nothing_else_changed()


@pytest.mark.parametrize(("release", "label"), _cases("rolled_back"))
def test_a_released_rollback_verifies_as_missing_and_rolls_back_nothing(
        release, label, tmp_path):
    box = Box(release, label, tmp_path)
    code, out = box.run("verify", box.migration_id)
    assert code == 1
    assert (box.receipt()["missing_target_count"], box.receipt()["failure_lineages"]) == (
        len(box.records), [])
    code, out = box.run("rollback", box.migration_id, "--yes")
    assert code == 0, out
    assert box.rollback_doc()["deleted"] == 0 and box.rows() == {}
    box.assert_nothing_else_changed()


@pytest.mark.parametrize(("release", "label"), _cases("rollback_partial"))
def test_a_released_partial_rollback_still_refuses_the_changed_row(release, label, tmp_path):
    box = Box(release, label, tmp_path)
    before = box.rows()
    code, out = box.run("rollback", box.migration_id, "--yes")
    assert code == 1 and "migration rollback refused changed target records" in out
    assert box.rollback_doc() == {"migration_id": box.migration_id,
                                  "status": "rollback_partial", "deleted": 0,
                                  "refused": [box.lineages[1]]}
    assert box.rows() == before
    code, out = box.run("verify", box.migration_id)
    assert code == 1
    assert (box.receipt()["failure_lineages"], box.receipt()["missing_target_count"]) == (
        [box.lineages[1]], len(box.records) - 1)
    box.assert_nothing_else_changed()


@pytest.mark.parametrize(("release", "label"), _cases("planned"))
def test_head_refuses_a_released_plan_bound_to_the_releases_own_destination(
        release, label, tmp_path):
    """Without the manifest rebound, the released binding names the box the
    release wrote in; HEAD refuses before writing anything."""
    box = Box(release, label, tmp_path, rebind=False)
    state_bytes = (box.copy.artifact_dir(label) / "state.json").read_bytes()
    code, out = box.run("apply", box.migration_id)
    assert code == 1
    assert "migration destination storage configuration does not match the plan" in out
    assert (box.copy.artifact_dir(label) / "state.json").read_bytes() == state_bytes
    assert box.rows() == {}


@pytest.mark.parametrize(("release", "label"), _cases("verified")[:1])
def test_head_makes_a_released_migrations_root_private(release, label, tmp_path):
    box = Box(release, label, tmp_path)
    os.chmod(box.copy.mig, 0o755)
    assert box.copy.mig.stat().st_mode & 0o777 == 0o755
    code, out = box.run("verify", box.migration_id)
    assert code == 0, out
    assert box.copy.mig.stat().st_mode & 0o777 == 0o700
    lock = box.copy.artifact_dir(label) / ".lock"
    assert lock.stat().st_mode & 0o777 == 0o600


# ---- `--from memtomem` over the stores Memory-1 froze ----------------------

def _memory_stores() -> list:
    return ([pytest.param("memtomem", r, id=f"memtomem-{r}") for r in MEMORY_RELEASES]
            + [pytest.param("schema1", r, id=f"schema1-{r}") for r in SCHEMA1_RELEASES])


@pytest.mark.parametrize(("family", "release"), _memory_stores())
def test_head_migrates_a_released_memtomem_store(family, release, tmp_path):
    """HEAD plans every row of a store Memory-1 froze that the default
    selection names (expected rows from the release's own reading and the
    stored timestamps, never from HEAD), and applies them into a fresh
    LangGraph store. A v0.1.x store is migrated to schema 3 by the first open;
    v0.1.2's rows keep the namespace v0.1.2 stamped on them (#318)."""
    frozen = Frozen(release) if family == "memtomem" else Schema1Frozen(release)
    store = frozen.copy(tmp_path / "copy") if family == "schema1" else frozen.copy(
        "store", tmp_path / "copy")
    read = frozen.read["store"]["get"]
    conn = sqlite3.connect(store.db)
    try:
        stamps = dict(conn.execute("SELECT id, created_at FROM chunks").fetchall())
    finally:
        conn.close()
    assert set(stamps) == set(read)
    wanted = sorted(
        # The snapshot holds `isoformat()` of the chunk's datetime.
        ((row["namespace"], row_id, row["content"], sorted(row["tags"]),
          datetime.fromisoformat(stamps[row_id]).isoformat())
         for row_id, row in read.items() if selected_by_default(row["namespace"])),
        key=lambda r: (r[0], r[1]))
    assert wanted and not any(r[0].startswith("board-run:") for r in wanted)
    migrations = tmp_path / "mig"
    env = {"SYNCMILL_MIGRATIONS_DIR": str(migrations),
           "SYNCMILL_MEMORY__DB_PATH": str(store.db),
           "SYNCMILL_MEMORY__MEMORY_DIR": str(store.mem),
           "SYNCMILL_MEMORY__LANGGRAPH__DB_PATH": str(tmp_path / "langgraph.db"),
           "SYNCMILL_MEMORY__MEM0__DATA_DIR": str(tmp_path / "unused-mem0")}
    runner = CliRunner()
    result = runner.invoke(cli, ["memory", "migration", "plan", "--from", "memtomem",
                                 "--to", "langgraph"], env=env, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    migration_id = result.output.split("migration planned: ")[1].split()[0]
    snapshot = json.loads((migrations / migration_id / "snapshot.json").read_text())
    got = [(r["namespace"], r["source_id"], r["content"], sorted(r["tags"]), r["created_at"])
           for r in snapshot["records"]]
    assert got == wanted
    result = runner.invoke(cli, ["memory", "migration", "apply", migration_id], env=env,
                           catch_exceptions=False)
    assert result.exit_code == 0, result.output
    rows = FREEZE.rows("langgraph", tmp_path / "langgraph.db")
    assert sorted((r["namespace"], r["content"], sorted(r["tags"])) for r in rows.values()) == \
        sorted((ns, c, tags) for ns, _, c, tags, _ in wanted)

@pytest.mark.parametrize("release", [r for r in MIGRATION_RELEASES if r.startswith("v0.1.")])
def test_head_rewrites_released_memtomem_timestamps_as_the_same_instant(release, tmp_path):
    """HEAD's memtomem migrates a v0.1.x store on open (schema 1): every
    timestamp is rewritten in 0.6's format, and is the same instant; nothing
    else in a row changes."""
    copy = _FROZEN[release].copy(tmp_path)
    copy.rebind()
    for path in [copy.source_path("memtomem")] + [
            copy.dest_path(d) for d, spec in _FROZEN[release].index["destinations"].items()
            if spec["target"] == "memtomem"]:
        before = FREEZE.rows("memtomem", path)
        backend = build_backend(MemoryConfig(backend="memtomem",
                                             **memory_settings("memtomem", path)))
        try:
            run_async(backend.count_namespace("shared"))
        finally:
            run_async(backend.close())
        after = FREEZE.rows("memtomem", path)
        assert set(after) == set(before) and before
        rewritten = 0
        for row_id, row in before.items():
            got = after[row_id]
            for key in ("namespace", "tags", "content", "source_file"):
                assert got[key] == row[key], (path, row_id, key)
            for key in ("created_at", "updated_at"):
                rewritten += got["raw"][key] != row["raw"][key]
                assert got["raw"][key] == memtomem_created_at(row["raw"][key], "0.6")
        assert rewritten
