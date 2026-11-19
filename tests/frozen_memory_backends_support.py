"""Shared access to the frozen Mem0 and LangGraph stores (#271).

``tests/fixtures/persisted/memory/backends/<release>/`` holds the stores a
released syncmill wrote through its optional backends (see ``freeze.py``
beside them). They live apart from the memtomem stores, so
``frozen_memory_support.RELEASES`` (the ``v*`` directories one level up) never
picks them up. The standard-library readers are ``freeze.py``'s own, imported
by path: the generator required every stored row through them, and the tests
read the frozen files through the same code. Nothing here opens a fixture file
for writing: a test works on a copy under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from frozen_memory_support import FIXTURES, normal_tags

BACKENDS_DIR = FIXTURES / "backends"
BACKEND_RELEASES = sorted(
    p.name for p in BACKENDS_DIR.iterdir() if p.is_dir() and p.name.startswith("v")
)
#: Order between two search hits is asserted only when their scores differ by
#: more than this. The frozen stores' smallest measured margin between a first
#: and a second hit is 2.3e-4 (LangGraph bulk rows that differ by one number).
SCORE_TOLERANCE = 1e-4


def load_freeze():
    """``backends/freeze.py`` as a module (its stdlib readers and constants)."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("backends_freeze", BACKENDS_DIR / "freeze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FREEZE = load_freeze()


@dataclass(frozen=True)
class StoreCopy:
    """A copy of one frozen store under ``tmp_path``, moved and nothing else."""

    name: str  # "<backend>/<kind>"
    backend: str
    root: Path
    path: Path  # Mem0 data dir or LangGraph database
    board: Path

    def rows(self) -> dict[str, dict]:
        return FREEZE.stored_rows(self.backend, self.path)

    def memory_config(self) -> dict:
        if self.backend == "mem0":
            return {"backend": "mem0", "mem0": {"data_dir": str(self.path)}}
        return {"backend": "langgraph", "langgraph": {"db_path": str(self.path)}}


class BackendsFrozen:
    def __init__(self, release: str):
        self.release = release
        self.root = BACKENDS_DIR / release
        self.index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.read = json.loads((self.root / "release-read.json").read_text(encoding="utf-8"))
        self.models = json.loads((self.root / "models.json").read_text(encoding="utf-8"))
        self.anchor: str = self.index["anchor"]

    def stores(self, backend: str | None = None) -> list[str]:
        return sorted(
            name for name, spec in self.index["stores"].items()
            if backend is None or spec["backend"] == backend
        )

    def scenario(self, store: str, name: str) -> dict:
        return self.index["scenarios"][f"{store}:{name}"]

    def scenarios(self, store: str) -> dict[str, dict]:
        prefix = store + ":"
        return {
            key[len(prefix):]: value for key, value in self.index["scenarios"].items()
            if key.startswith(prefix) and isinstance(value, dict) and "rows" in value
        }

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
        spec = self.index["stores"][store]
        target = dest.resolve() / store
        shutil.copytree(self.root / store, target, symlinks=True)
        return StoreCopy(
            name=store,
            backend=spec["backend"],
            root=target,
            path=dest.resolve() / spec["path"],
            board=dest.resolve() / spec["board"],
        )

    def scenario_ids(self, store: str, name: str, namespace: str | None = None) -> list[str]:
        rows = self.index["rows"][store]
        return sorted(
            i for i in self.scenario(store, name)["ids"]
            if namespace is None or rows[i]["namespace"] == namespace
        )


#: Stands for "no tag list at all" (a foreign row) in a sortable key.
NO_TAGS = ("<no tag list>",)


def row_key(row: dict) -> tuple:
    """A stored row the way the scenario definitions name one."""
    tags = row["tags"]
    return (
        row["namespace"] or "",
        NO_TAGS if tags is None else normal_tags(tags),
        row["content"],
    )


def scenario_key(row: dict) -> tuple:
    """A scenario definition's row (tags already normalized) as ``row_key``."""
    tags = row["tags"]
    return (row["namespace"] or "", NO_TAGS if tags is None else tuple(tags), row["body"])


def ordered_prefix(hits: list[dict]) -> list[str]:
    """The leading ids of ``hits`` whose place is a contract: each is ahead of
    the next hit by more than ``SCORE_TOLERANCE``. The last hit is never
    included (a row the cut at ``top_k`` left out may tie with it)."""
    ids = []
    for before, after in zip(hits, hits[1:]):
        if before["score"] - after["score"] <= SCORE_TOLERANCE:
            break
        ids.append(before["id"])
    return ids
