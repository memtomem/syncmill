"""Shared access to the frozen `memory migration` artifacts (#271).

``tests/fixtures/persisted/memory/migration/<release>/`` holds the source
stores, the destination stores and the migration artifacts a released
syncmill's own CLI wrote (see ``freeze.py`` beside them). The standard-library
readers are ``freeze.py``'s, imported by path: the generator required every
step's end through them, and the tests read the frozen files through the same
code. Nothing here opens a fixture file for writing: a test works on a copy
under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from frozen_memory_support import FIXTURES

MIGRATION_DIR = FIXTURES / "migration"
MIGRATION_RELEASES = sorted(
    p.name for p in MIGRATION_DIR.iterdir() if p.is_dir() and p.name.startswith("v")
)
#: v0.1.1's schema 1 artifacts (no destination binding), apart so that the
#: ``v*`` releases above all share one layout.
LEGACY_DIR = MIGRATION_DIR / "legacy"
LEGACY_RELEASES = sorted(
    p.name for p in LEGACY_DIR.iterdir() if p.is_dir() and p.name.startswith("v")
)


def load_freeze():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("migration_freeze", MIGRATION_DIR / "freeze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FREEZE = load_freeze()


def selected_by_default(namespace: str) -> bool:
    """The default selection, written out (not HEAD's ``_selected``)."""
    return namespace == "shared" or namespace.startswith("agent-runtime:")


def snapshot_created_at(source: str, stored: str | None) -> str | None:
    """A source row's stored timestamp as the snapshot records it: Mem0's
    payload string and memtomem's column verbatim; LangGraph's SQL timestamp
    with its date/time space replaced by ``T`` (no zone, seconds), the
    ``isoformat`` of the naive datetime LangGraph returns."""
    if stored is None:
        return None
    return stored.replace(" ", "T", 1) if source == "langgraph" else stored


def memtomem_created_at(record_created_at: str, writer: str) -> str:
    """A record's timestamp as the memtomem that wrote the migrated chunk
    stores ``created_at``. ``writer`` is ``"0.3"`` (the 0.3.x v0.1.2 and v0.1.4
    lock: ``isoformat(timespec="seconds")``, so a zoneless value stays zoneless
    and fractions are cut) or ``"0.6"`` (v0.2.0 on and HEAD: UTC with
    microseconds, a zoneless value read as UTC). Measured: LangGraph's
    ``13:21:09`` stays ``13:21:09`` under 0.3.x and becomes
    ``13:21:09.000000+00:00`` under 0.6."""
    value = datetime.fromisoformat(record_created_at)
    if writer == "0.3":
        return value.isoformat(timespec="seconds")
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(
        timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def release_memtomem(release: str) -> str:
    """Which memtomem a release locked: 0.3.x up to v0.1.4, 0.6 from v0.2.0."""
    return "0.3" if release.startswith("v0.1.") else "0.6"


def memory_settings(backend: str, path: Path | str) -> dict:
    path = str(path)
    if backend == "memtomem":
        return {"db_path": str(Path(path) / "mm.db"), "memory_dir": str(Path(path) / "mem")}
    if backend == "mem0":
        return {"mem0": {"data_dir": path}}
    return {"langgraph": {"db_path": path}}


def memory_env(backend: str, path: Path | str) -> dict:
    path = str(path)
    if backend == "memtomem":
        return {"SYNCMILL_MEMORY__DB_PATH": str(Path(path) / "mm.db"),
                "SYNCMILL_MEMORY__MEMORY_DIR": str(Path(path) / "mem")}
    if backend == "mem0":
        return {"SYNCMILL_MEMORY__MEM0__DATA_DIR": path}
    return {"SYNCMILL_MEMORY__LANGGRAPH__DB_PATH": path}


def tree_files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


class MigrationFrozen:
    def __init__(self, release: str):
        self.release = release
        self.root = MIGRATION_DIR / release
        self.index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.bindings = json.loads((self.root / "bindings.json").read_text(encoding="utf-8"))
        self.generating_root = self.bindings["generating_root"]

    @property
    def labels(self) -> list[str]:
        return sorted(self.index["artifacts"])

    def entry(self, label: str) -> dict:
        return self.index["artifacts"][label]

    def doc(self, label: str) -> dict:
        return FREEZE.artifact(self.root / "mig", self.entry(label)["migration_id"])

    def records(self, label: str) -> list[dict]:
        return self.doc(label)["snapshot"]["records"]

    def source_path(self, backend: str) -> Path:
        return self.root / self.index["sources"][backend]["path"]

    def dest_path(self, direction: str) -> Path:
        return self.root / self.index["destinations"][direction]["path"]

    def original(self, relative: str) -> str:
        """Where the release had a fixture path."""
        return str(Path(self.generating_root) / relative)

    def verify_pins(self) -> None:
        pinned = self.index["files"]
        present = {str(p.relative_to(self.root)) for p in self.root.rglob("*")
                   if p.is_file() and p.name != "index.json"}
        assert present == set(pinned), sorted(present ^ set(pinned))
        for relative, digest in pinned.items():
            assert hashlib.sha256((self.root / relative).read_bytes()).hexdigest() == digest, \
                relative

    def copy(self, dest: Path) -> MigrationCopy:
        """Copy the whole box (sources, destinations, artifacts) under ``dest``,
        laid out as the release had it, with the migrations root and every
        artifact directory 0700 again (git keeps no directory modes)."""
        target = dest.resolve() / "box"
        for name in ("sources", "destinations", "mig"):
            shutil.copytree(self.root / name, target / name)
        os.chmod(target / "mig", 0o700)
        for artifact_dir in (target / "mig").iterdir():
            os.chmod(artifact_dir, 0o700)
            for path in artifact_dir.rglob("*"):
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return MigrationCopy(self, target)


@dataclass
class MigrationCopy:
    frozen: MigrationFrozen
    root: Path

    @property
    def mig(self) -> Path:
        return self.root / "mig"

    def source_path(self, backend: str) -> Path:
        return self.root / self.frozen.index["sources"][backend]["path"]

    def dest_path(self, direction: str) -> Path:
        return self.root / self.frozen.index["destinations"][direction]["path"]

    def artifact_dir(self, label: str) -> Path:
        return self.mig / self.frozen.entry(label)["migration_id"]

    def state(self, label: str) -> dict:
        return json.loads((self.artifact_dir(label) / "state.json").read_text(encoding="utf-8"))

    def doc(self, label: str) -> dict:
        return FREEZE.artifact(self.mig, self.frozen.entry(label)["migration_id"])

    def dest_rows(self, label: str) -> dict[str, dict]:
        entry = self.frozen.entry(label)
        return FREEZE.rows(entry["target"], self.dest_path(entry["direction"]))

    def source_rows(self, backend: str) -> dict[str, dict]:
        return FREEZE.rows(backend, self.source_path(backend))

    def env(self, label: str) -> dict:
        """The settings a HEAD command for this artifact runs with: the copy's
        source and destination, everything else in unused paths."""
        entry = self.frozen.entry(label)
        unused = self.root / "unused"
        env = {"SYNCMILL_MIGRATIONS_DIR": str(self.mig),
               **memory_env("memtomem", unused / "memtomem"),
               **memory_env("mem0", unused / "mem0"),
               **memory_env("langgraph", unused / "langgraph.db")}
        env.update(memory_env(entry["source"], self.source_path(entry["source"])))
        env.update(memory_env(entry["target"], self.dest_path(entry["direction"])))
        return env

    def rebind_memtomem(self, store: Path) -> int:
        """Swap the generating box for the copy in a memtomem store's
        ``chunks.source_file`` (and its FTS mirror), the two statements memtomem
        itself uses when a source moves; return how many rows moved."""
        from memtomem.storage.sqlite_helpers import norm_path

        old = self.frozen.generating_root.rstrip("/") + "/"
        new = norm_path(self.root).rstrip("/") + "/"
        conn = sqlite3.connect(store / "mm.db")
        try:
            moved = conn.execute(
                "UPDATE chunks SET source_file = ? || substr(source_file, ?) "
                "WHERE substr(source_file, 1, ?) = ?",
                (new, len(old) + 1, len(old), old),
            ).rowcount
            conn.execute(
                "UPDATE chunks_fts SET source_file = "
                "(SELECT source_file FROM chunks WHERE chunks.rowid = chunks_fts.rowid)")
            conn.commit()
            left = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE substr(source_file, 1, ?) = ?",
                (len(old), old)).fetchone()[0]
        finally:
            conn.close()
        assert left == 0
        return moved

    def rebind_artifact(self, label: str) -> None:
        """Bind a copied artifact to the copy's destination: the released
        ``target_storage_digest`` must be what HEAD computes for the release's
        own destination path, and is then replaced, by exact bytes, with what
        HEAD computes for the copy."""
        from syncmill.config import SyncmillConfig
        from syncmill.memory_migration import _target_storage_digest

        entry = self.frozen.entry(label)
        target, direction = entry["target"], entry["direction"]
        relative = self.frozen.index["destinations"][direction]["path"]
        original = SyncmillConfig(memory=memory_settings(target, self.frozen.original(relative)))
        moved = SyncmillConfig(memory=memory_settings(target, self.dest_path(direction)))
        manifest = self.artifact_dir(label) / "manifest.json"
        data = manifest.read_bytes()
        released = json.loads(data)["target_storage_digest"]
        assert _target_storage_digest(original, target) == released, label
        new = _target_storage_digest(moved, target)
        assert data.count(released.encode()) == 1
        manifest.write_bytes(data.replace(released.encode(), new.encode()))

    def rebind(self) -> None:
        for backend in ("memtomem",):
            self.rebind_memtomem(self.source_path(backend))
        for direction, spec in self.frozen.index["destinations"].items():
            if spec["target"] == "memtomem":
                self.rebind_memtomem(self.dest_path(direction))
        for label in self.frozen.labels:
            self.rebind_artifact(label)
