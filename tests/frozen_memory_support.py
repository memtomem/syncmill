"""Shared access to the frozen memtomem stores (#271).

``tests/fixtures/persisted/memory/<release>/`` holds stores a released syncmill
wrote through its own CLI (see ``freeze.py`` beside them). Nothing here opens a
fixture file for writing: a test works on a copy under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "persisted" / "memory"
RELEASES = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir() and p.name.startswith("v"))

_TASK_TAG = re.compile(r"task:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHARED_FROM_TAG = re.compile(
    r"shared-from=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def normal_tags(tags: list[str]) -> tuple[str, ...]:
    """Tags with the two generated ids replaced by ``*``, the way the scenario
    definitions name them (a task id and a private row id exist only once the
    release has run)."""
    out = []
    for tag in tags:
        if _TASK_TAG.fullmatch(tag):
            tag = "task:*"
        elif _SHARED_FROM_TAG.fullmatch(tag):
            tag = "shared-from=*"
        out.append(tag)
    return tuple(sorted(out))


@dataclass(frozen=True)
class Row:
    id: str
    namespace: str
    tags: tuple[str, ...]  # as stored, sorted
    content: str
    source_file: str  # the stored absolute path

    @property
    def key(self) -> tuple[str, tuple[str, ...], str]:
        return (self.namespace, normal_tags(list(self.tags)), self.content)


@dataclass(frozen=True)
class StoreCopy:
    """A copy of one frozen store under ``tmp_path``, moved and nothing else."""

    name: str
    root: Path
    db: Path
    mem: Path
    board: Path | None
    generating_mem: str  # the day-file directory the release wrote into

    def rows(self) -> dict[str, Row]:
        """The chunk rows, through an ordinary connection so that a WAL the
        release left is applied the way any SQLite reader applies it."""
        conn = sqlite3.connect(self.db)
        try:
            return {
                r[0]: Row(r[0], r[1], tuple(sorted(json.loads(r[2]))), r[3], r[4])
                for r in conn.execute(
                    "SELECT id, namespace, tags, content, source_file FROM chunks ORDER BY rowid"
                )
            }
        finally:
            conn.close()

    def day_files(self) -> list[Path]:
        return sorted(p for p in self.mem.iterdir() if p.suffix == ".md" and p.name[0] != ".")


class Frozen:
    def __init__(self, release: str):
        self.release = release
        self.root = FIXTURES / release
        self.index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.read = json.loads((self.root / "release-read.json").read_text(encoding="utf-8"))
        self.anchor: str = self.index["anchor"]
        self.date: str = self.index["date"]

    @property
    def stores(self) -> list[str]:
        return sorted(self.index["stores"])

    def scenario(self, name: str) -> dict:
        return self.index["scenarios"][name]

    def verify_pins(self) -> None:
        """Every fixture file is the byte sequence ``freeze.py`` recorded, and
        there is no file it did not record."""
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

    def copy(self, store: str, dest: Path) -> StoreCopy:
        """Copy one store whole, database sidecars and lock files included."""
        spec = self.index["stores"][store]
        target = dest.resolve() / store
        shutil.copytree(self.root / store, target)
        return StoreCopy(
            name=store,
            root=target,
            db=target.parent / spec["db"],
            mem=target.parent / spec["mem"],
            board=target.parent / spec["board"] if spec["board"] else None,
            generating_mem=str(Path(spec["root"]) / Path(spec["mem"]).relative_to(store)),
        )

    def expected(self, store: str) -> list[tuple[str, tuple[str, ...], str]]:
        """The rows the scenario definitions name for one store."""
        return sorted(
            (row["namespace"], tuple(row["tags"]), row["body"])
            for scenario in self.index["scenarios"].values()
            if scenario["store"] == store
            for row in scenario["rows"]
        )

    def scenario_rows(self, store: StoreCopy, name: str) -> list[Row]:
        """The stored rows of one scenario, found by what its definition names."""
        wanted = {
            (row["namespace"], tuple(row["tags"]), row["body"])
            for row in self.scenario(name)["rows"]
        }
        return [row for row in store.rows().values() if row.key in wanted]
