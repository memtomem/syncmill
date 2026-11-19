"""Produce the frozen Mem0 and LangGraph stores under this directory (#271).

Run once per release. This script runs under any Python 3; everything that
touches a store is the release's own code. Its CLI writes the rows, and its
interpreter (`<release-root>/.venv`, synced with the `mem0` and `langgraph`
extras) adds the rows no CLI writes and then reads every store back. The
current source is never used. The fake agents, scenario texts and tokens come
from the memtomem generator one directory up (`../freeze.py`, imported by path
and not changed). The output directory must not exist; frozen fixtures are
never regenerated, and a run that stops early leaves no output directory.

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev --extra mem0 --extra langgraph)
    python tests/fixtures/persisted/memory/backends/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --fastembed-cache /path/to/cache \\
        --out tests/fixtures/persisted/memory/backends/v0.3.0

Both backends embed with FastEmbed. The cache must already hold the dense model
and `Qdrant/bm25`; generation runs offline (`HF_HUB_OFFLINE=1`) and records the
revision and sha256 of every model file it used (`models.json`).

For each backend (`SYNCMILL_MEMORY__BACKEND=mem0`, then `langgraph`) two stores
come out:

- `<backend>/store`: every scenario one after another, as in the memtomem
  family (`run` under each strategy, a failed run, Unicode with CRLF, `###`
  headings, a long prompt, a finished `board run`, `room apply`), then rows
  written by the backend's SDK that syncmill never wrote (`foreign`), and last a
  `board run -s pipeline` whose CLI is SIGKILLed while its second stage works.
- `<backend>/bulk`: a finished `board run`, then 104 more rows in its scratch
  namespace and 105 in `shared`, added through the release's own backend `add`,
  so both namespaces pass the backends' 100-row pages. It is a store of its own
  because a later board run's GC would delete that scratch namespace.

Generation fails unless every scenario left exactly the rows its definition
names and every earlier row is unchanged (`_require`), so a release that writes
something else cannot silently narrow the coverage. The stores are read with the
standard library only: the Qdrant local points through an unpickler that maps
exactly two qdrant-client classes to inert stubs and refuses any other, the
LangGraph values as JSON. Every row syncmill wrote into Mem0 must carry the
`bm25` sparse vector (Mem0 searches hybrid; a release whose BM25 encoder failed
to load would freeze dense-only points). Only after every store is copied does
the release's interpreter read the stores it wrote, in place, and record what it
makes of them (`release-read.json`): the expected reading the tests hold HEAD to,
on the fields the release returned. `index.json` holds every stored row, the
scenario definitions and a sha256 of every file.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import pickle
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location(
    "frozen_memory_freeze", Path(__file__).resolve().parent.parent / "freeze.py"
)
m1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m1)

ANCHOR = m1.ANCHOR
BACKENDS = ("mem0", "langgraph")
LANGGRAPH_PREFIX = "syncmill.memory.items"
NAMESPACES_READ = ["shared", "agent-runtime:codex", "agent-runtime:kimi-code"]
BULK_SHARED = 105
BULK_SCRATCH = 105  # the board run's own scratch row plus 104 added
ABSENT_TAG = "frozen-absent-kind"
MODELS = (
    "models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
    "models--Qdrant--bm25",
)
# Rows the backends' SDKs hold that syncmill never wrote (scenario `foreign`).
# LangGraph keys are fixed; Mem0 assigns its own ids.
FOREIGN_TEXT = {
    "unowned": f"foreign row without a syncmill namespace {ANCHOR} tokforeign",
    "untagged": f"foreign row in shared without syncmill tags {ANCHOR} tokforeign",
}
FOREIGN_KEYS = {
    "unowned": "00000000-0000-4000-8000-00000000f0a1",
    "untagged": "00000000-0000-4000-8000-00000000f0a2",
}

# What each release's writer does (`memory.share_result`, `share_board_result`,
# the rooms). v0.2.0 is generated only to compare with v0.3.0.
PROFILES = {
    "0.1.2": {"heading": "## ", "split": False, "plan_row": False, "room_memory": False,
              "gc_on_claim": True},
    "0.1.4": {"heading": "## ", "split": True, "plan_row": True, "room_memory": False,
              "gc_on_claim": True},
    "0.2.0": {"heading": "**", "split": True, "plan_row": True, "room_memory": True,
              "gc_on_claim": False},
    "0.3.0": {"heading": "**", "split": True, "plan_row": True, "room_memory": True,
              "gc_on_claim": False},
}


def _title(profile: dict, title: str) -> str:
    return f"## {title}" if profile["heading"] == "## " else f"**{title}**"


def task_body(profile: dict, prompt: str, agent: str, output: str, files: bool) -> str:
    """The body `share_result` stores: v0.2.0 and later open it with a bold
    label, v0.1.x with a `## ` heading."""
    body = m1.task_body(prompt, agent, output, files)
    return _title(profile, "Task Result") + body[len("**Task Result**"):]


def board_body(profile: dict, item_id: str, task: str, output: str) -> str:
    body = m1.board_body(item_id, task, output)
    return _title(profile, "Board Item Result") + body[len("**Board Item Result**"):]


def result(profile: dict, kind: str, agent: str, body: str) -> list[dict]:
    """A shared result: from v0.1.4 the private row and its `shared` copy (kind,
    agent and task tags; the copy names its source), on v0.1.2 one row written
    straight into `shared`, tagged `task-result` and the agent only."""
    if profile["split"]:
        return m1.pair(kind, agent, body)
    return [{"namespace": "shared", "tags": sorted(["task-result", agent]), "body": body}]


def scratch_tags(profile: dict, kind: str) -> list[str]:
    return sorted([kind, "codex", "task:*"]) if profile["split"] else ["codex", "task-result"]


# --- standard-library readers (also used by the default-suite tests) ---------

_QDRANT_CLASSES = {
    ("qdrant_client.http.models.models", "PointStruct"),
    ("qdrant_client.http.models.models", "SparseVector"),
}


class _Inert:
    """Holds a pickled qdrant-client model's state and does nothing else."""

    def __setstate__(self, state):
        self.__dict__["_state"] = state


_STUBS = {name: type(name, (_Inert,), {}) for _, name in _QDRANT_CLASSES}


class _QdrantUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) in _QDRANT_CLASSES:
            return _STUBS[name]
        raise pickle.UnpicklingError(f"refused {module}.{name}")


def _fields(obj) -> dict:
    state = obj.__dict__["_state"]
    return state["__dict__"] if isinstance(state, dict) and "__dict__" in state else state


def _copy_query(db: Path, sql: str) -> list[tuple]:
    """One query on a throwaway copy of an SQLite file and its journal/WAL, so a
    store a release wrote is never opened in place."""
    with tempfile.TemporaryDirectory() as tmp:
        for part in db.parent.iterdir():
            if part.name == db.name or part.name.startswith(db.name + "-"):
                shutil.copy(part, Path(tmp) / part.name)
        conn = sqlite3.connect(Path(tmp) / db.name)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


def qdrant_points(data_dir: Path) -> dict[str, dict]:
    """Mem0's Qdrant local points: id -> {payload, vectors}."""
    db = data_dir / "qdrant" / "collection" / "syncmill" / "storage.sqlite"
    if not db.exists():
        return {}
    points = {}
    for _, blob in _copy_query(db, "SELECT id, point FROM points"):
        fields = _fields(_QdrantUnpickler(io.BytesIO(blob)).load())
        vectors = fields["vector"]
        sparse = _fields(vectors["bm25"]) if vectors.get("bm25") is not None else None
        dense = vectors.get("", [])
        points[str(fields["id"])] = {
            "payload": fields["payload"],
            "dense_dims": len(dense),
            "bm25_terms": len(sparse["indices"]) if sparse is not None else None,
            # The vectors themselves, so a reader that re-embeds a stored row
            # is a changed row.
            "vectors_sha256": hashlib.sha256(json.dumps(
                [list(dense), sparse and [list(sparse["indices"]), list(sparse["values"])]]
            ).encode()).hexdigest(),
        }
    return points


def langgraph_items(db: Path) -> dict[str, dict]:
    """LangGraph store items under syncmill's prefix: key -> {value, created_at,
    updated_at, vectors}."""
    if not db.exists():
        return {}
    vectors: dict[str, list] = {}
    for key, field, embedding in _copy_query(
            db, f"SELECT key, field_name, embedding FROM store_vectors "
                f"WHERE prefix = '{LANGGRAPH_PREFIX}' ORDER BY key, field_name"):
        vectors.setdefault(key, []).append((field, hashlib.sha256(embedding or b"").hexdigest()))
    return {
        # `vectors`: each embedded field and the sha256 of its stored bytes.
        key: {"value": json.loads(value), "created_at": created, "updated_at": updated,
              "vectors": vectors.get(key, [])}
        for key, value, created, updated in _copy_query(
            db, f"SELECT key, value, created_at, updated_at FROM store "
                f"WHERE prefix = '{LANGGRAPH_PREFIX}'")
    }


def stored_rows(backend: str, path: Path) -> dict[str, dict]:
    """Every stored row as syncmill's writer shaped it: id -> {namespace, tags,
    content, raw}. A field the row does not have is None."""
    rows = {}
    if backend == "mem0":
        for point_id, point in qdrant_points(path).items():
            payload = point["payload"]
            rows[point_id] = {
                "namespace": payload.get("user_id"),
                "tags": payload.get("syncmill_tags"),
                "content": payload.get("data"),
                "raw": point,
            }
    else:
        for key, item in langgraph_items(path).items():
            value = item["value"]
            rows[key] = {
                "namespace": value.get("namespace"),
                "tags": value.get("tags"),
                "content": value.get("content"),
                "raw": item,
            }
    return rows


# --- scenario definitions --------------------------------------------------

def _normal_tags(tags) -> list[str] | None:
    return None if tags is None else m1._normal_tags(json.dumps(tags))


def _require(name: str, new: dict[str, dict], expected: list[dict]) -> None:
    def key(row: dict) -> tuple:
        return (row["namespace"] or "", tuple(row["tags"] or ()), row["body"] or "")

    got = sorted(
        ({"namespace": r["namespace"], "tags": _normal_tags(r["tags"]), "body": r["content"]}
         for r in new.values()),
        key=key,
    )
    want = sorted(({k: r[k] for k in ("namespace", "tags", "body")} for r in expected), key=key)
    if got != want:
        sys.exit(
            f"{name}: the release left other rows than the scenario names\n"
            f"--- left\n{json.dumps(got, ensure_ascii=False, indent=1)}\n"
            f"--- named\n{json.dumps(want, ensure_ascii=False, indent=1)}"
        )


# --- release-side scripts (run by the RELEASED interpreter) ------------------

_RELEASE_PRELUDE = r'''
import json, sys
from syncmill.config import MemoryConfig
from syncmill.memory import run_async
from syncmill.memory_backend import build_backend
backend_name, path = sys.argv[1], sys.argv[2]
if backend_name == "mem0":
    cfg = MemoryConfig(backend="mem0", mem0={"data_dir": path})
else:
    cfg = MemoryConfig(backend="langgraph", langgraph={"db_path": path})
backend = build_backend(cfg)
'''

# argv: backend, path, namespace, first index, count, tag prefix, token
RELEASE_BULK = _RELEASE_PRELUDE + r'''
ns, first, count, prefix, token = sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6], sys.argv[7]
try:
    for i in range(first, first + count):
        run_async(backend.add(
            f"**Bulk Row**\n\n{token} {prefix}{i:03d} {ns}",
            tags=["bulk", f"{prefix}{i:03d}"], namespace=ns,
        ))
finally:
    run_async(backend.close())
print("ok")
'''

# argv: backend, path, JSON {name: text}, JSON {name: key}
RELEASE_FOREIGN = _RELEASE_PRELUDE + r'''
texts, keys = json.loads(sys.argv[3]), json.loads(sys.argv[4])
out = {}
try:
    if backend_name == "mem0":
        # Through Mem0's own API, the way a non-syncmill caller would write.
        for name, kwargs in (("unowned", {"agent_id": "foreign-agent"}),
                             ("untagged", {"user_id": "shared"})):
            response = run_async(backend._store.add(
                [{"role": "user", "content": texts[name]}], metadata={}, infer=False, **kwargs))
            out[name] = response["results"][0]["id"]
    else:
        store = run_async(backend._ensure_store())
        ns = ("syncmill", "memory", "items")
        run_async(store.aput(ns, keys["unowned"], {"content": texts["unowned"], "tags": []},
                             index=["content"]))
        run_async(store.aput(ns, keys["untagged"],
                             {"namespace": "shared", "content": texts["untagged"]},
                             index=["content"]))
        out = dict(keys)
finally:
    run_async(backend.close())
print(json.dumps(out))
'''

# argv: backend, path, JSON spec {ids, namespaces, tokens, kinds, absent}
RELEASE_READ = _RELEASE_PRELUDE + r'''
spec = json.loads(sys.argv[3])


def plain(row):
    return None if row is None else {k: row[k] for k in sorted(row) if k != "score"}


def hits(rows):
    return [{"id": r["id"], "namespace": r.get("namespace"), "score": r.get("score")}
            for r in rows]


read = {"get": {}, "count": {}, "search": {}, "search_all": {}, "search_by_kind": {},
        "search_absent": {}, "search_multi": None}
try:
    for row_id in spec["ids"]:
        read["get"][row_id] = plain(run_async(backend.get(row_id)))
    for ns in spec["namespaces"]:
        read["count"][ns] = run_async(backend.count_namespace(ns))
        read["search_all"][ns] = hits(run_async(backend.search(
            spec["anchor"], namespace=ns, top_k=1000)))
        read["search_absent"][ns] = hits(run_async(backend.search(
            spec["anchor"], namespace=ns, top_k=1000, tag_filter=spec["absent"])))
        read["search_by_kind"][ns] = {
            kind: hits(run_async(backend.search(
                spec["anchor"], namespace=ns, top_k=1000, tag_filter=kind)))
            for kind in spec["kinds"]
        }
        read["search"][ns] = {
            token: hits(run_async(backend.search(token, namespace=ns, top_k=3)))
            for token in spec["tokens"]
        }
    # Each row searched by its own content, in its own namespace: the one query
    # whose expected first hit is known for a dense-only backend too.
    read["search_self"] = {}
    for row_id in spec["self_ids"]:
        row = read["get"][row_id]
        read["search_self"][row_id] = hits(run_async(backend.search(
            row["content"], namespace=row["namespace"], top_k=3)))
    if hasattr(backend, "search_multi"):
        read["search_multi"] = {
            token: {
                "two": hits(run_async(backend.search_multi(
                    token, namespaces=["shared", "agent-runtime:codex"], top_k=3))),
                "repeated": hits(run_async(backend.search_multi(
                    token, namespaces=["shared", "shared"], top_k=3))),
            }
            for token in spec["tokens"]
        }
finally:
    run_async(backend.close())
print(json.dumps(read, ensure_ascii=False, sort_keys=True))
'''


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def model_manifest(cache: Path) -> dict:
    manifest = {}
    for model in MODELS:
        snapshots = sorted((cache / model / "snapshots").iterdir()) if (
            cache / model / "snapshots").is_dir() else []
        if len(snapshots) != 1:
            sys.exit(f"the FastEmbed cache must hold exactly one snapshot of {model}: {snapshots}")
        manifest[model] = {
            "revision": snapshots[0].name,
            # A list of {name, sha256}: a file name and its digest on one line
            # ("tokenizer.json": "<hex>") reads to secret scanners as a token.
            "files": [{"name": p.name, "sha256": _sha256(p)}
                      for p in sorted(snapshots[0].iterdir())],
        }
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--fastembed-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    release = args.release_root.resolve()
    cache = args.fastembed_cache.resolve()
    out = args.out.absolute().parent.resolve() / args.out.name

    def refuse_existing() -> None:
        if out.exists() or out.is_symlink():
            sys.exit(f"{out} exists; frozen fixtures are never regenerated")

    refuse_existing()
    python = release / ".venv" / "bin" / "python"
    syncmill = release / ".venv" / "bin" / "syncmill"

    def metadata(package: str) -> str:
        return subprocess.run(
            [str(python), "-c",
             f"import importlib.metadata as m; print(m.version('{package}'))"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    version = metadata("syncmill")
    if version not in PROFILES:
        sys.exit(f"no scenario definitions for syncmill {version} yet")
    sdks = {p: metadata(p) for p in (
        "mem0ai", "qdrant-client", "langgraph-checkpoint-sqlite", "fastembed")}
    models = model_manifest(cache)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp).resolve() / "frozen"
        _generate(Path(tmp).resolve(), stage, python, syncmill, version, sdks, models, cache)
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(tempfile.mkdtemp(dir=out.parent, prefix=f".{out.name}.partial-"))
        try:
            shutil.copytree(stage, partial / "frozen", symlinks=True)
            refuse_existing()
            (partial / "frozen").rename(out)
        finally:
            shutil.rmtree(partial)
    print(f"frozen {version} into {out}")


def _generate(box: Path, out: Path, python: Path, syncmill: Path, version: str,
              sdks: dict, models: dict, cache: Path) -> None:
    repo, home, state, fakebin, started = (
        box / n for n in ("repo", "home", "state", "fakebin", "started")
    )
    for folder in (repo, home, state, fakebin, started):
        folder.mkdir()
    for binary in ("codex", "kimi"):
        (fakebin / binary).write_text(f"#!{sys.executable}\n" + m1.FAKE_AGENT, encoding="utf-8")
        (fakebin / binary).chmod(0o755)
    (fakebin / "python").symlink_to(sys.executable)
    common_env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
        "TMPDIR": str(box),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "FASTEMBED_CACHE_PATH": str(cache),
        "HF_HUB_OFFLINE": "1",
        "MEM0_TELEMETRY": "false",
        "FAKE_STARTED": str(started),
        "FAKE_ANCHOR": ANCHOR,
        "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex", "kimi-code"]),
        "SYNCMILL_CLAUDE__ENABLED": "false",
        "SYNCMILL_COUNCIL__ROUNDS": "2",
        "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
        "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
        "SYNCMILL_TUI__STATE_DIR": str(state / "tui"),
        # memtomem is not the backend here; its paths are pinned anyway so no
        # step can reach a store outside the box.
        "SYNCMILL_MEMORY__DB_PATH": str(state / "unused-memtomem.db"),
        "SYNCMILL_MEMORY__MEMORY_DIR": str(state / "unused-memtomem"),
        **m1.GIT_ENV,
    }

    def git(*argv: str) -> None:
        subprocess.run(["git", *argv], cwd=repo, env=common_env, check=True,
                       capture_output=True)

    git("init", "-q", "-b", "main")
    for name, text in m1.TREE.items():
        (repo / name).write_text(text, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "frozen tree")

    def restore_tree() -> None:
        git("checkout", "--", ".")

    out.mkdir(parents=True)
    index: dict = {
        "release": version,
        "sdks": sdks,
        "anchor": ANCHOR,
        "generating_root": str(box),
        "stores": {},
        "scenarios": {},
        "rows": {},
        # Rows a later step of the release deleted (its board GC), by store.
        "vanished": {},
    }
    profile = PROFILES[version]

    def tb(prompt: str, agent: str, output: str, files: bool) -> str:
        return task_body(profile, prompt, agent, output, files)

    def res(kind: str, agent: str, body: str) -> list[dict]:
        return result(profile, kind, agent, body)
    counter = [0]

    class Store:
        def __init__(self, backend: str, kind: str):
            self.backend, self.kind = backend, kind
            self.name = f"{backend}/{kind}"
            self.root = state / backend / kind
            self.root.mkdir(parents=True)
            self.path = self.root / ("mem0" if backend == "mem0" else "langgraph.db")
            self.env = {
                **common_env,
                "SYNCMILL_PROJECT__ID": m1.PROJECT_ID,
                "SYNCMILL_RUNS__ROOT": str(state / f"runs-{backend}-{kind}"),
                "SYNCMILL_MEMORY__BACKEND": backend,
                "SYNCMILL_MEMORY__MEM0__DATA_DIR": str(self.root / "mem0"),
                "SYNCMILL_MEMORY__LANGGRAPH__DB_PATH": str(self.root / "langgraph.db"),
                "SYNCMILL_BOARD__ENABLED": "true",
                "SYNCMILL_BOARD__DB_PATH": str(self.root / "board.db"),
                "SYNCMILL_ROOMS__ENABLED": "true",
            }
            self.known: dict[str, dict] = {}
            self.ids: dict[str, list[str]] = {}

        def sm(self, scenario: str, *argv: str, plan: dict | None = None,
               ok: bool = True) -> str:
            calls = state / f"calls-{counter[0]}"
            counter[0] += 1
            calls.mkdir()
            done = subprocess.run(
                [str(syncmill), *argv], cwd=repo,
                env={**self.env, "FAKE_PLAN": json.dumps(plan or {}), "FAKE_CALLS": str(calls)},
                capture_output=True, text=True,
            )
            if ok and done.returncode != 0:
                sys.exit(f"{self.name} {scenario}: {argv}: {done.stdout}{done.stderr}")
            if not ok and done.returncode == 0:
                sys.exit(f"{self.name} {scenario}: {argv} was expected to fail: {done.stdout}")
            return done.stdout

        def py(self, scenario: str, script: str, *argv: str) -> str:
            done = subprocess.run(
                [str(python), "-c", script, self.backend, str(self.path), *argv],
                cwd=box, env=self.env, capture_output=True, text=True,
            )
            if done.returncode != 0:
                sys.exit(f"{self.name} {scenario}: release script: {done.stdout}{done.stderr}")
            return done.stdout.strip().splitlines()[-1]

        def new_rows(self, scenario: str, *, may_vanish=lambda row_id, row: False) -> dict:
            """The rows added since the last call. Every earlier row must still
            be stored exactly as it was (payload, timestamps and vectors);
            only rows ``may_vanish`` names may be gone."""
            rows = stored_rows(self.backend, self.path)
            changed = {
                row_id: row for row_id, row in self.known.items()
                if rows.get(row_id) != row and not (
                    row_id not in rows and may_vanish(row_id, row))
            }
            if changed:
                sys.exit(f"{self.name} {scenario}: an earlier row was changed or removed: "
                         f"{json.dumps(sorted(changed), indent=1)}")
            for row_id in sorted(set(self.known) - set(rows)):
                gone = self.known[row_id]
                index["vanished"].setdefault(self.name, {})[row_id] = {
                    "namespace": gone["namespace"], "tags": gone["tags"],
                    "content": gone["content"], "during": scenario}
            new = {row_id: row for row_id, row in rows.items() if row_id not in self.known}
            self.known = rows
            return new

        def check_bm25(self, scenario: str, new: dict) -> None:
            if self.backend != "mem0":
                return
            missing = [i for i, r in new.items()
                       if r["tags"] is not None and not r["raw"]["bm25_terms"]]
            if missing:
                sys.exit(f"{self.name} {scenario}: syncmill rows without a bm25 vector: {missing}")

        def record(self, scenario: str, expected: list[dict], new: dict | None = None,
                   **facts) -> None:
            new = self.new_rows(scenario) if new is None else new
            _require(f"{self.name} {scenario}", new, expected)
            self.check_bm25(scenario, new)
            index["scenarios"][f"{self.name}:{scenario}"] = {
                "store": self.name, "rows": expected, "ids": sorted(new), **facts}

        def freeze(self) -> None:
            target = out / self.name
            shutil.copytree(self.root, target, symlinks=True)
            index["stores"][self.name] = {
                "backend": self.backend,
                "path": str((target / self.path.relative_to(self.root)).relative_to(out)),
                "board": f"{self.name}/board.db",
                "root": str(self.root),
            }

    def board_scenario(store: Store, scenario: str, token: str) -> tuple[str, str, dict]:
        task_text = f"{ANCHOR} {token} fix add in calc.py"
        plan = {"codex": {"token": token, "edit": ["1"]}}
        line = store.sm(scenario, "board", "add", task_text, "--goal", "frozen")
        item_id = line.split("added item ")[1].split()[0]
        store.sm(scenario, "board", "run", "--id", item_id, plan=plan)
        new = store.new_rows(scenario)
        runs = sorted({r["namespace"] for r in new.values()
                       if (r["namespace"] or "").startswith("board-run:")})
        if len(runs) != 1:
            sys.exit(f"{store.name} {scenario}: expected one scratch namespace, found {runs}")
        reply = m1.default_reply("codex", 1, token)
        scratch = [r for r in new.values() if r["namespace"] == runs[0]]
        shared = [r for r in new.values() if r["namespace"] == "shared"]
        expected_shared = {"namespace": "shared", "tags": [f"board-result:{item_id}"],
                           "body": board_body(profile, item_id, task_text, reply)}
        if (
            len(new) != 2 or len(scratch) != 1 or len(shared) != 1
            or _normal_tags(scratch[0]["tags"]) != scratch_tags(profile, "task-result")
            or not scratch[0]["content"].startswith(
                _title(profile, "Task Result") + "\n\n**Prompt:** ")
            or not scratch[0]["content"].endswith(
                f"**Agent:** codex\n\n**Output:**\n{reply}\n\n**Files changed:** calc.py")
            or task_text not in scratch[0]["content"]
            or {"namespace": "shared", "tags": shared[0]["tags"],
                "body": shared[0]["content"]} != expected_shared
        ):
            sys.exit(f"{store.name} {scenario}: unexpected rows "
                     f"{json.dumps(list(new.values()), ensure_ascii=False, default=str)}")
        expected = [
            # The scratch row's prompt is the rendered board item, the
            # release's own text; only its frame is checked above.
            {"namespace": runs[0], "tags": scratch_tags(profile, "task-result"),
             "body": scratch[0]["content"], "body_is_release_text": True},
            expected_shared,
        ]
        restore_tree()
        return item_id, runs[0].split(":", 1)[1], {"new": new, "expected": expected,
                                                   "task": task_text}

    for backend in BACKENDS:
        main_store = Store(backend, "store")

        prompt = f"{ANCHOR} tokroute fix add in calc.py"
        main_store.sm("route", "run", prompt, "-a", "codex",
                      plan={"codex": {"token": "tokroute", "edit": ["1"]}})
        main_store.record("route", res("task-result", "codex", tb(
            prompt, "codex", m1.default_reply("codex", 1, "tokroute"), True)),
            prompt=prompt, token="tokroute")
        restore_tree()

        prompt = f"{ANCHOR} tokpipeline fix add in calc.py"
        main_store.sm("pipeline", "run", prompt, "-s", "pipeline", plan={
            "codex": {"token": "tokpipeline", "edit": ["1"]},
            "kimi": {"token": "tokpipeline", "edit": ["1"]}})
        main_store.record(
            "pipeline",
            res("pipeline-stage", "codex", tb(
                prompt, "codex", m1.default_reply("codex", 1, "tokpipeline"), False))
            + res("task-result", "kimi-code", tb(
                prompt, "kimi-code", m1.default_reply("kimi", 1, "tokpipeline"), True)),
            prompt=prompt, token="tokpipeline")
        restore_tree()

        prompt = f"{ANCHOR} tokcompete fix add in calc.py"
        main_store.sm("compete", "run", prompt, "-s", "compete", plan={
            "codex": {"token": "tokcompete", "edit": ["1"]},
            "kimi": {"token": "tokcompete", "edit": ["1"]}})
        main_store.record("compete", res("task-result", "codex", tb(
            prompt, "codex", m1.default_reply("codex", 1, "tokcompete"), True)),
            prompt=prompt, token="tokcompete")
        restore_tree()

        prompt = f"{ANCHOR} tokcouncil fix add in calc.py"
        main_store.sm("council", "run", prompt, "-s", "council", plan={
            "codex": {"token": "tokcouncil", "edit": ["4"]},
            "kimi": {"token": "tokcouncil", "edit": ["4"]}})
        expected: list[dict] = []
        for agent, binary in (("codex", "codex"), ("kimi-code", "kimi")):
            expected += res("council-proposal", agent, tb(
                prompt, agent, m1.default_reply(binary, 1, "tokcouncil"), False))
            for call in (2, 3):
                expected += res("council-critique", agent, tb(
                    prompt, agent, m1.default_reply(binary, call, "tokcouncil"), False))
        expected += res("task-result", "codex", tb(
            prompt, "codex", m1.default_reply("codex", 4, "tokcouncil"), True))
        main_store.record("council", expected, prompt=prompt, token="tokcouncil")
        restore_tree()

        prompt = f"{ANCHOR} tokdecompose fix add in calc.py"
        main_store.sm("decompose", "run", prompt, "-s", "decompose", plan={
            "codex": {"token": "tokdecompose", "edit": ["3"],
                      "replies": {"1": json.dumps(m1.SUBTASKS)}},
            "kimi": {"token": "tokdecompose", "edit": ["2"]}})
        main_store.record(
            "decompose",
            # v0.1.2 shares no plan row.
            (res("decompose-plan", "codex", tb(
                prompt, "codex", json.dumps(m1.SUBTASKS, ensure_ascii=False), False))
             if profile["plan_row"] else [])
            + res("decompose-subtask", "codex", tb(
                prompt, "codex", m1.default_reply("codex", 2, "tokdecompose"), False))
            + res("decompose-subtask", "kimi-code", tb(
                prompt, "kimi-code", m1.default_reply("kimi", 1, "tokdecompose"), False))
            + res("task-result", "codex", tb(
                prompt, "codex", m1.default_reply("codex", 3, "tokdecompose"), True)),
            prompt=prompt, token="tokdecompose", subtasks=m1.SUBTASKS)
        restore_tree()

        # The reply is cut at 1000 characters after its outer whitespace is
        # dropped. Unlike memtomem's day file, these stores keep the CRLF pair.
        prompt = f"{ANCHOR} tokunicode say something long"
        main_store.sm("unicode", "run", prompt, "-a", "codex",
                      plan={"codex": {"token": "tokunicode", "replies": {"1": m1.UNICODE_REPLY}}})
        main_store.record("unicode", res("task-result", "codex", tb(
            prompt, "codex", m1.UNICODE_REPLY.strip()[:1000], False)),
            prompt=prompt, token="tokunicode", reply=m1.UNICODE_REPLY)

        prompt = f"{ANCHOR} tokheadings review calc.py"
        main_store.sm("headings", "run", prompt, "-a", "codex",
                      plan={"codex": {"token": "tokheadings",
                                      "replies": {"1": m1.HEADINGS_REPLY}}})
        main_store.record("headings", res("task-result", "codex", tb(
            prompt, "codex", m1.HEADINGS_REPLY, False)),
            prompt=prompt, token="tokheadings", reply=m1.HEADINGS_REPLY)

        # No chunking: the whole long body is one row (memtomem cuts it in two).
        main_store.sm("longprompt", "run", m1.LONG_PROMPT, "-a", "codex",
                      plan={"codex": {"token": "toklong"}})
        main_store.record("longprompt", res("task-result", "codex", tb(
            m1.LONG_PROMPT, "codex", m1.default_reply("codex", 1, "toklong"), False)),
            prompt=m1.LONG_PROMPT, token="toklong")

        prompt = f"{ANCHOR} tokfailed fix add in calc.py"
        main_store.sm("failed", "run", prompt, "-a", "codex",
                      plan={"codex": {"token": "tokfailed", "fail": ["1"]}}, ok=False)
        main_store.record("failed", [], prompt=prompt, token="tokfailed")

        item_id, run_id, done = board_scenario(main_store, "board-done", "tokboard")
        main_store_done_run = run_id
        main_store.record("board-done", done["expected"], new=done["new"],
                          task=done["task"], token="tokboard", item_id=item_id, run_id=run_id)

        prompt = f"{ANCHOR} tokroom fix add in calc.py"
        plan = {"codex": {"token": "tokroom", "edit": ["1"]},
                "kimi": {"token": "tokroom", "edit": ["1"]}}
        room_id = main_store.sm("room-apply", "room", "create", prompt,
                                "-a", "codex", "-a", "kimi-code").split()[0]
        main_store.sm("room-apply", "room", "round", room_id, plan=plan)
        rooms_db = state / f"runs-{backend}-store" / m1.PROJECT_ID / "rooms" / "rooms.sqlite3"
        candidates = m1._query(
            rooms_db,
            "SELECT id, member_id FROM candidates WHERE room_id = ? AND eligible = 1 "
            "ORDER BY member_id", (room_id,))
        if not candidates or candidates[0][1] != "codex":
            sys.exit(f"{main_store.name} room-apply: no eligible codex candidate: {candidates}")
        main_store.sm("room-apply", "room", "select", room_id, candidates[0][0])
        main_store.sm("room-apply", "room", "apply", room_id)
        # Before v0.2.0 `room apply` shares nothing.
        main_store.record("room-apply", res("room-result", "codex", tb(
            prompt, "codex", m1.default_reply("codex", 1, "tokroom"), True))
            if profile["room_memory"] else [],
            prompt=prompt, token="tokroom", room_id=room_id)
        restore_tree()

        ids = json.loads(main_store.py("foreign", RELEASE_FOREIGN,
                                       json.dumps(FOREIGN_TEXT), json.dumps(FOREIGN_KEYS)))
        new = main_store.new_rows("foreign")
        foreign_expected = [
            # Mem0 names the writer of an unowned row agent_id, not user_id;
            # LangGraph's unowned item has no namespace key at all.
            {"namespace": None, "tags": [] if backend == "langgraph" else None,
             "body": FOREIGN_TEXT["unowned"]},
            {"namespace": "shared", "tags": None, "body": FOREIGN_TEXT["untagged"]},
        ]
        if sorted(new) != sorted(ids.values()):
            sys.exit(f"{main_store.name} foreign: ids {sorted(new)} != {ids}")
        main_store.record("foreign", foreign_expected, new=new, foreign_ids=ids)

        # ---- last: a board run SIGKILLed during its second stage ------------
        task_text = f"{ANCHOR} tokkilled fix add in calc.py"
        line = main_store.sm("board-killed", "board", "add", task_text, "--goal", "frozen")
        item_id = line.split("added item ")[1].split()[0]
        for marker in started.iterdir():
            marker.unlink()
        calls = state / f"calls-killed-{backend}"
        calls.mkdir()
        plan = {"codex": {"token": "tokkilled", "edit": ["1"]},
                "kimi": {"token": "tokkilled", "hang": ["1"]}}
        proc = subprocess.Popen(
            [str(syncmill), "board", "run", "--id", item_id, "-s", "pipeline"],
            cwd=repo, env={**main_store.env, "FAKE_PLAN": json.dumps(plan),
                           "FAKE_CALLS": str(calls)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )

        def kill_group() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        deadline = time.monotonic() + 180
        while not (started / "kimi").exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                kill_group()
                try:
                    output = proc.communicate(timeout=30)[0]
                except subprocess.TimeoutExpired:
                    output = "(no output: the pipe is still held open)"
                sys.exit(f"{main_store.name} board-killed: the second stage never started: "
                         f"{output}")
            time.sleep(0.05)
        agent_pid = int((started / "kimi").read_text())
        kill_group()
        proc.wait()
        try:
            os.kill(agent_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.stdout.close()
        # v0.1.x collects the finished board run's scratch namespace when the
        # next run claims; nothing else may vanish.
        done_ns = f"board-run:{main_store_done_run}"
        new = main_store.new_rows(
            "board-killed",
            may_vanish=lambda row_id, row: profile["gc_on_claim"] and row["namespace"] == done_ns)
        if profile["gc_on_claim"] and any(
                r["namespace"] == done_ns for r in main_store.known.values()):
            sys.exit(f"{main_store.name} board-killed: the release kept {done_ns}")
        rows = list(new.values())
        reply = m1.default_reply("codex", 1, "tokkilled")
        if (
            len(rows) != 1
            or not (rows[0]["namespace"] or "").startswith("board-run:")
            or _normal_tags(rows[0]["tags"]) != scratch_tags(profile, "pipeline-stage")
            # The prompt is the rendered board item, the release's own text:
            # its frame and the task in it are checked.
            or not rows[0]["content"].startswith(
                _title(profile, "Task Result") + "\n\n**Prompt:** ")
            or task_text not in rows[0]["content"]
            or not rows[0]["content"].endswith(f"**Agent:** codex\n\n**Output:**\n{reply}")
        ):
            sys.exit(f"{main_store.name} board-killed: unexpected rows "
                     f"{json.dumps(rows, ensure_ascii=False, default=str)}")
        (lease,) = m1._query(
            main_store.root / "board.db",
            "SELECT state, lease_run_id, claimed_at FROM work_items WHERE id = ?", (item_id,))
        if lease[0] != "running" or f"board-run:{lease[1]}" != rows[0]["namespace"]:
            sys.exit(f"{main_store.name} board-killed: no live lease for the scratch run: {lease}")
        main_store.record(
            "board-killed",
            [{"namespace": rows[0]["namespace"], "tags": _normal_tags(rows[0]["tags"]),
              "body": rows[0]["content"], "body_is_release_text": True}],
            new=new, task=task_text, token="tokkilled", item_id=item_id,
            run_id=lease[1], claimed_at=lease[2],
            # What the kill left beside the stores: journal or WAL files.
            leftovers=sorted(p.name for p in main_store.root.rglob("*")
                             if p.name.endswith(("-journal", "-wal", "-shm"))),
        )
        restore_tree()
        main_store.freeze()

        # ---- the bulk store -------------------------------------------------
        bulk = Store(backend, "bulk")
        item_id, run_id, done = board_scenario(bulk, "board-done", "tokbulkboard")
        bulk.record("board-done", done["expected"], new=done["new"], task=done["task"],
                    token="tokbulkboard", item_id=item_id, run_id=run_id)
        scratch_ns = f"board-run:{run_id}"
        bulk.py("bulk", RELEASE_BULK, scratch_ns, "1", str(BULK_SCRATCH - 1), "bulkscratch-",
                f"{ANCHOR} tokbulk")
        bulk.py("bulk", RELEASE_BULK, "shared", "1", str(BULK_SHARED), "bulkshared-",
                f"{ANCHOR} tokbulk")
        bulk.record(
            "bulk",
            [{"namespace": scratch_ns, "tags": ["bulk", f"bulkscratch-{i:03d}"],
              "body": f"**Bulk Row**\n\n{ANCHOR} tokbulk bulkscratch-{i:03d} {scratch_ns}"}
             for i in range(1, BULK_SCRATCH)]
            + [{"namespace": "shared", "tags": ["bulk", f"bulkshared-{i:03d}"],
                "body": f"**Bulk Row**\n\n{ANCHOR} tokbulk bulkshared-{i:03d} shared"}
               for i in range(1, BULK_SHARED + 1)],
            token="tokbulk", run_id=run_id, item_id=item_id)
        (lease,) = m1._query(bulk.root / "board.db",
                             "SELECT state, lease_run_id FROM work_items WHERE id = ?",
                             (item_id,))
        if lease[0] == "running":
            sys.exit(f"{bulk.name}: the finished board run still holds its lease: {lease}")
        bulk.freeze()

        for store in (main_store, bulk):
            index["rows"][store.name] = {
                row_id: {k: row[k] for k in ("namespace", "tags", "content")}
                for row_id, row in sorted(store.known.items())
            }

    # ---- what the release reads from the stores it wrote, in place ----------
    tokens = sorted({s["token"] for s in index["scenarios"].values()
                     if isinstance(s, dict) and "token" in s} | {"tokforeign"})
    read = {}
    for name, store in index["stores"].items():
        root = Path(store["root"])
        path = root / ("mem0" if store["backend"] == "mem0" else "langgraph.db")
        spec = {
            "ids": sorted(index["rows"][name]),
            "namespaces": sorted({r["namespace"] for r in index["rows"][name].values()
                                  if r["namespace"]} | set(NAMESPACES_READ)),
            "tokens": tokens, "kinds": m1.KINDS, "absent": ABSENT_TAG, "anchor": ANCHOR,
            # Every syncmill row of a scenario store; every tenth of a bulk one.
            "self_ids": [
                row_id for n, (row_id, row) in enumerate(sorted(index["rows"][name].items()))
                if row["namespace"] and row["tags"] is not None
                and (not name.endswith("/bulk") or n % 10 == 0)
            ],
        }
        env = {**common_env, "SYNCMILL_MEMORY__BACKEND": store["backend"]}
        done = subprocess.run(
            [str(python), "-c", RELEASE_READ, store["backend"], str(path), json.dumps(spec)],
            cwd=box, env=env, capture_output=True, text=True,
        )
        if done.returncode != 0:
            sys.exit(f"release read of {name}: {done.stdout}{done.stderr}")
        read[name] = {"spec": spec, **json.loads(done.stdout.strip().splitlines()[-1])}
    (out / "release-read.json").write_text(
        json.dumps(read, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / "models.json").write_text(
        json.dumps(models, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    index["files"] = {
        str(p.relative_to(out)): _sha256(p)
        for p in sorted(out.rglob("*")) if p.is_file() and not p.is_symlink()
    }
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
