"""Shared access to the frozen schema-1 memtomem stores (#271).

``tests/fixtures/persisted/memory/schema1/<release>/`` holds the stores v0.1.x
wrote through memtomem 0.3.x (see ``schema1/freeze.py`` beside them). They live
apart from the Memory-1a stores so that ``frozen_memory_support.RELEASES`` (the
``v*`` directories one level up) never picks them up. Nothing here opens a
fixture file for writing: a test works on a copy under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from frozen_memory_support import FIXTURES, StoreCopy

SCHEMA1 = FIXTURES / "schema1"
SCHEMA1_RELEASES = sorted(
    p.name for p in SCHEMA1.iterdir() if p.is_dir() and p.name.startswith("v")
)


def load_freeze():
    """``schema1/freeze.py`` as a module: its pure checks are tested directly,
    and its entry parser is how the tests read a day file."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("schema1_freeze", SCHEMA1 / "freeze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Schema1Frozen:
    def __init__(self, release: str):
        self.release = release
        self.root = SCHEMA1 / release
        self.index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.read = json.loads((self.root / "release-read.json").read_text(encoding="utf-8"))
        self.anchor: str = self.index["anchor"]
        self.date: str = self.index["date"]

    def scenario(self, name: str) -> dict:
        return self.index["scenarios"][name]

    @property
    def steps(self) -> list[dict]:
        return self.index["steps"]

    @property
    def purged(self) -> set[str]:
        return {name for step in self.steps for name in step["purged"]}

    def entries(self, role: str | None = None) -> list[dict]:
        """Every recorded entry, with its file name, in file then write order."""
        return [
            {**entry, "file": name}
            for name, entries in sorted(self.index["entries"].items())
            for entry in entries
            if role is None or entry["role"] == role
        ]

    def scenario_steps(self, scenario: str) -> set[int]:
        return {i for i, step in enumerate(self.steps) if step["scenario"] == scenario}

    def verify_pins(self) -> None:
        pinned = self.index["files"]
        present = {
            str(p.relative_to(self.root))
            for p in self.root.rglob("*")
            if p.is_file() and p.name != "index.json"
        }
        assert present == set(pinned), sorted(present ^ set(pinned))
        for relative, digest in pinned.items():
            actual = hashlib.sha256((self.root / relative).read_bytes()).hexdigest()
            assert actual == digest, relative

    def copy(self, dest: Path) -> StoreCopy:
        spec = self.index["store"]
        target = dest.resolve() / "store"
        shutil.copytree(self.root / "store", target)
        return StoreCopy(
            name="store",
            root=target,
            db=target.parent / spec["db"],
            mem=target.parent / spec["mem"],
            board=target.parent / spec["board"],
            generating_mem=str(Path(spec["root"]) / "mem"),
        )


def full_rows(db: Path) -> list[dict]:
    """The chunk rows with their line spans, the WAL applied."""
    conn = sqlite3.connect(db)
    try:
        return [
            {"id": r[0], "namespace": r[1], "tags": sorted(json.loads(r[2])), "content": r[3],
             "start_line": r[4], "end_line": r[5], "source": Path(r[6]).name}
            for r in conn.execute(
                "SELECT id, namespace, tags, content, start_line, end_line, source_file "
                "FROM chunks ORDER BY rowid"
            )
        ]
    finally:
        conn.close()
