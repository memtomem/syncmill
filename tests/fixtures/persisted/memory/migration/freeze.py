"""Produce the frozen `memory migration` artifacts under this directory (#271).

Run once per release. This script runs under any Python 3; everything that
touches a store or an artifact is the release's own code: its CLI writes the
source rows and runs every `memory migration` command, and its interpreter
(`<release-root>/.venv`, synced with the `mem0` and `langgraph` extras) adds the
rows no CLI writes and makes the out-of-band changes two states need. The
current source is never used. The fake agents and texts come from the memtomem
generator (`../freeze.py`), the standard-library store readers from the Mem0 and
LangGraph generator (`../backends/freeze.py`); both are imported by path and not
changed. The output directory must not exist; frozen fixtures are never
regenerated, and a run that stops early leaves no output directory.

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev --extra mem0 --extra langgraph)
    python tests/fixtures/persisted/memory/migration/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --fastembed-cache /path/to/cache \\
        --out tests/fixtures/persisted/memory/migration/v0.3.0

The FastEmbed cache must hold the files `../backends/v*/models.json` names;
generation runs offline.

Three source stores (memtomem, Mem0, LangGraph), each holding a route `run`, a
finished `board run` (its `board-run:` row, which no plan may select) and three
rows in each state namespace `frozen-mig-<state>`. One destination store per
direction (`DIRECTIONS`). Each state's artifact selects only its own namespace,
so the states share a destination; `verified` uses the default selection
(`shared` and `agent-runtime:*`), which no state namespace overlaps. The states:

- planned: `plan` only.
- crashed-first / crashed: `apply` under a launcher that `os._exit(91)`s in
  place of the `state.json` write that would record the first / second target.
- partial: `apply` with the destination adapter's `put` raising on the second
  record.
- verified: `plan` + `apply` (default selection).
- verification_failed: `apply`, the second record's target deleted through the
  release's own maintenance adapter, then `verify`.
- rolled_back: `apply`, `rollback --yes`.
- rollback_partial: `apply`, the second record's target rewritten in place
  through the release's own SDK, then `rollback --yes`.

Generation fails unless every step left exactly what its state names: exit code,
status, ledger, errors, receipt, and the destination rows of that namespace
(read with the standard library), while every other destination row and every
source row is unchanged. All artifacts live under one migrations root (`mig/`).
`index.json` records each artifact's steps and the rows they left;
`bindings.json` the paths every store and the artifacts were bound to.

v0.1.1 wrote schema 1 artifacts, which carry no destination binding. For it
(`--out .../legacy/v0.1.1`) only two artifacts are frozen, from a LangGraph
source into Mem0 (no memtomem: v0.1.1 locked memtomem 0.2.4): one planned, one
applied (its ledger filled). No store is kept: HEAD refuses both artifacts
before it opens any.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
_HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m1 = _load("frozen_memory_freeze", _HERE.parent / "freeze.py")
be = _load("frozen_memory_backends_freeze", _HERE.parent / "backends" / "freeze.py")

ANCHOR = m1.ANCHOR
STATES = ("planned", "crashed-first", "crashed", "partial", "verified",
          "verification_failed", "rolled_back", "rollback_partial")
RESUME = ("planned", "verified", "crashed-first", "crashed", "partial")
PAIR = ("planned", "verified")
# Every state for each destination adapter; the resume states for both other
# sources into memtomem; every direction at least planned and verified.
DIRECTIONS = {
    ("mem0", "memtomem"): STATES,
    ("memtomem", "mem0"): STATES,
    ("memtomem", "langgraph"): STATES,
    ("langgraph", "memtomem"): RESUME,
    ("mem0", "langgraph"): PAIR,
    ("langgraph", "mem0"): PAIR,
}
DEFAULT_STATE = "verified"
CRASH_EXIT = 91
PUT_ERROR = "frozen: destination write refused"
CHANGED_SUFFIX = "\n\nfrozen: changed after migration"
LANGGRAPH_NS = ("syncmill", "memory", "items")


def state_namespace(state: str) -> str:
    return f"frozen-mig-{state}"


def state_rows(state: str, version: str) -> list[dict]:
    """The three rows each state namespace holds in every source. The first
    carries non-ASCII text and a CRLF pair, the second a Markdown list. Each is
    about 160 words and 1,300 characters, so that memtomem 0.3.x keeps it one
    chunk (measured: it merged a 100-word row into its neighbour, and cut a
    250-word one at about 2,000 characters); the states need one record per
    row. Each opens the way that release's own bodies do
    (`share_result`): a `## ` heading on v0.1.x, whose memtomem writes an entry
    without a title of its own (a body without a heading runs into the next
    entry), a bold label from v0.2.0."""
    title = "## Migration Row" if version.startswith("0.1.") else "**Migration Row**"
    unicode_text = m1.UNICODE_REPLY.strip()[:120]

    def filler(i: int) -> str:
        return " ".join(f"{ANCHOR} tokmig-{state}-{i} sentence {k} keeps this row whole."
                        for k in range(20))

    texts = [
        f"{title}\n\n{ANCHOR} tokmig-{state}-1 {unicode_text}\n\n{filler(1)}",
        f"{title}\n\n{ANCHOR} tokmig-{state}-2\n\n- a list item\n- another\n\n{filler(2)}",
        f"{title}\n\n{ANCHOR} tokmig-{state}-3\n\n{filler(3)}",
    ]
    return [{"namespace": state_namespace(state), "tags": ["migration-row", f"tokmig-{state}-{i}"],
             "content": text} for i, text in enumerate(texts, 1)]


def namespaces_of(source: str) -> list[str]:
    states = sorted({s for (src, _), ss in DIRECTIONS.items() if src == source for s in ss})
    return [s for s in states if s != DEFAULT_STATE]


# --- standard-library readers (also used by the default-suite tests) --------

def memtomem_rows(db: Path) -> dict[str, dict]:
    """Every chunk of a memtomem store: id -> its stored cells."""
    if not db.exists():
        return {}
    # `updated_at` too: a reader that rewrites a row in place with the same
    # content changes nothing else a test could see.
    cols = ("namespace", "tags", "content", "source_file", "created_at", "updated_at")
    return {
        r[0]: {**dict(zip(cols, r[1:])), "tags": json.loads(r[2])}
        for r in m1._query(db, f"SELECT id, {', '.join(cols)} FROM chunks ORDER BY rowid")
    }


def rows(backend: str, path: Path) -> dict[str, dict]:
    """id -> {namespace, tags, content, created_at, raw}; a field the row does
    not have is None."""
    if backend == "memtomem":
        return {i: {**r, "raw": r} for i, r in memtomem_rows(path / "mm.db").items()}
    if backend == "mem0":
        return {
            i: {"namespace": p["payload"].get("user_id"), "tags": p["payload"].get("syncmill_tags"),
                "content": p["payload"].get("data"), "created_at": p["payload"].get("created_at"),
                "raw": p}
            for i, p in be.qdrant_points(path).items()
        }
    return {
        k: {"namespace": it["value"].get("namespace"), "tags": it["value"].get("tags"),
            "content": it["value"].get("content"), "created_at": it["created_at"], "raw": it}
        for k, it in be.langgraph_items(path).items()
    }


def store_files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def artifact(root: Path, migration_id: str) -> dict:
    path = root / migration_id
    doc = {name: json.loads((path / f"{name}.json").read_text(encoding="utf-8"))
           for name in ("manifest", "snapshot", "state", "receipt", "rollback")
           if (path / f"{name}.json").exists()}
    doc["files"] = store_files(path)
    return doc


# --- release-side scripts (run by the RELEASED interpreter) -----------------

# argv: JSON [{namespace, tags, content}]; the backend and its store come from
# the environment, as for the CLI.
RELEASE_ADD = r'''
import json, sys
from syncmill.config import SyncmillConfig
from syncmill.memory import run_async
from syncmill.memory_backend import build_backend
backend = build_backend(SyncmillConfig().memory)
try:
    for row in json.loads(sys.argv[1]):
        run_async(backend.add(row["content"], tags=row["tags"], namespace=row["namespace"]))
finally:
    run_async(backend.close())
print("ok")
'''

# argv: mode (delete|rewrite), destination backend, target id, suffix
RELEASE_TAMPER = r'''
import sys
from uuid import UUID
from syncmill import memory_migration as mm
from syncmill.config import SyncmillConfig
from syncmill.memory import run_async
mode, target, target_id, suffix = sys.argv[1:5]
dst = mm._maintenance(SyncmillConfig(), target)
try:
    if mode == "delete":
        if not run_async(dst.delete(target_id)):
            sys.exit("delete reported nothing deleted")
    elif target == "memtomem":
        comp = run_async(dst.backend._store._ensure_init())
        chunk = run_async(comp.storage.get_chunk(UUID(target_id)))
        chunk.content = chunk.content + suffix
        chunk.embedding = run_async(comp.embedder.embed_texts([chunk.content]))[0]
        run_async(comp.storage.upsert_chunks([chunk]))
    elif target == "mem0":
        current = run_async(dst.backend._store.get(target_id))
        run_async(dst.backend._store.update(target_id, data=current["memory"] + suffix))
    else:
        store = run_async(dst.backend._ensure_store())
        ns = ("syncmill", "memory", "items")
        value = dict(run_async(store.aget(ns, target_id)).value)
        value["content"] = value["content"] + suffix
        run_async(store.aput(ns, target_id, value, index=["content"]))
finally:
    run_async(dst.close())
print("ok")
'''

# argv: mode (crash|fail-put), n, destination backend, then the CLI's argv.
# crash: os._exit in place of the state.json write recording the n-th target.
# fail-put: the destination adapter's n-th put raises.
RELEASE_LAUNCH = r'''
import os, sys
from syncmill import memory_migration as mm
mode, n, target = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if mode == "crash":
    real = mm._atomic_json
    def crash(path, value, **kwargs):
        if path.name == "state.json" and len(value.get("targets", {})) == n:
            os._exit(%(exit)d)
        return real(path, value, **kwargs)
    mm._atomic_json = crash
else:
    cls = {"memtomem": mm._MemtomemMaintenance, "mem0": mm._Mem0Maintenance,
           "langgraph": mm._LangGraphMaintenance}[target]
    real_put, calls = cls.put, [0]
    async def put(self, record, migration_id):
        calls[0] += 1
        if calls[0] == n:
            raise RuntimeError(%(error)r)
        return await real_put(self, record, migration_id)
    cls.put = put
from syncmill.cli import cli
sys.argv = ["syncmill", *sys.argv[4:]]
cli()
''' % {"exit": CRASH_EXIT, "error": PUT_ERROR}


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
    version = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('syncmill'))"],
        check=True, capture_output=True, text=True).stdout.strip()
    models = be.model_manifest(cache)
    frozen_models = json.loads(
        (_HERE.parent / "backends" / "v0.3.0" / "models.json").read_text(encoding="utf-8"))
    if models != frozen_models:
        sys.exit("the FastEmbed cache is not the one ../backends/v0.3.0/models.json names")
    generate = _generate_v1 if version == "0.1.1" else _generate
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp).resolve() / "frozen"
        generate(Path(tmp).resolve(), stage, python, release / ".venv" / "bin" / "syncmill",
                 version, cache)
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
              cache: Path) -> None:
    repo, home, state, fakebin, started, mig = (
        box / n for n in ("repo", "home", "state", "fakebin", "started", "mig"))
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
        "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
        "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
        "SYNCMILL_TUI__STATE_DIR": str(state / "tui"),
        "SYNCMILL_PROJECT__ID": m1.PROJECT_ID,
        "SYNCMILL_MIGRATIONS_DIR": str(mig),
        **m1.GIT_ENV,
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=common_env, check=True)
    for name, text in m1.TREE.items():
        (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=common_env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "frozen tree"], cwd=repo, env=common_env,
                   check=True)

    def store_env(backend: str, root: Path) -> dict:
        if backend == "memtomem":
            return {"SYNCMILL_MEMORY__DB_PATH": str(root / "mm.db"),
                    "SYNCMILL_MEMORY__MEMORY_DIR": str(root / "mem")}
        if backend == "mem0":
            return {"SYNCMILL_MEMORY__MEM0__DATA_DIR": str(root)}
        return {"SYNCMILL_MEMORY__LANGGRAPH__DB_PATH": str(root)}

    unused = {
        "SYNCMILL_MEMORY__DB_PATH": str(state / "unused" / "mm.db"),
        "SYNCMILL_MEMORY__MEMORY_DIR": str(state / "unused" / "mem"),
        "SYNCMILL_MEMORY__MEM0__DATA_DIR": str(state / "unused" / "mem0"),
        "SYNCMILL_MEMORY__LANGGRAPH__DB_PATH": str(state / "unused" / "langgraph.db"),
    }

    def run(env: dict, argv: list[str], *, script: str | None = None,
            expect: int | str = 0) -> subprocess.CompletedProcess:
        if "FAKE_CALLS" in env:
            Path(env["FAKE_CALLS"]).mkdir()
        command = [str(syncmill), *argv] if script is None else [str(python), "-c", script, *argv]
        done = subprocess.run(command, cwd=repo, env={**common_env, **env},
                              capture_output=True, text=True)
        ok = done.returncode == expect if isinstance(expect, int) else done.returncode != 0
        if not ok:
            sys.exit(f"{argv}: exit {done.returncode} (wanted {expect}):\n"
                     f"{done.stdout}{done.stderr}")
        return done

    out.mkdir(parents=True)
    index: dict = {"release": version, "anchor": ANCHOR, "sources": {}, "destinations": {},
                   "artifacts": {}}
    bindings: dict = {"generating_root": str(box), "cwd": str(repo), "migrations_root": str(mig),
                      "stores": {}}

    # ---- sources -------------------------------------------------------------
    sources: dict[str, Path] = {}
    for backend in ("memtomem", "mem0", "langgraph"):
        root = box / "sources" / backend
        root.mkdir(parents=True)
        path = root if backend != "langgraph" else root / "langgraph.db"
        if backend == "mem0":
            path = root / "mem0"
        sources[backend] = path
        env = {**unused, **store_env(backend, path), "SYNCMILL_MEMORY__BACKEND": backend,
               "SYNCMILL_RUNS__ROOT": str(state / f"runs-{backend}"),
               "SYNCMILL_BOARD__ENABLED": "true",
               "SYNCMILL_BOARD__DB_PATH": str(state / f"board-{backend}.db")}
        prompt = f"{ANCHOR} tokmigroute fix add in calc.py"
        run({**env, "FAKE_PLAN": json.dumps({"codex": {"token": "tokmigroute", "edit": ["1"]}}),
             "FAKE_CALLS": str(state / f"calls-{backend}-route")},
            ["run", prompt, "-a", "codex"])
        subprocess.run(["git", "checkout", "--", "."], cwd=repo, env=common_env, check=True)
        line = run(env, ["board", "add", f"{ANCHOR} tokmigboard fix add in calc.py",
                         "--goal", "frozen"]).stdout
        item_id = line.split("added item ")[1].split()[0]
        run({**env, "FAKE_PLAN": json.dumps({"codex": {"token": "tokmigboard", "edit": ["1"]}}),
             "FAKE_CALLS": str(state / f"calls-{backend}-board")},
            ["board", "run", "--id", item_id])
        subprocess.run(["git", "checkout", "--", "."], cwd=repo, env=common_env, check=True)
        before = rows(backend, path)
        added = [r for s in namespaces_of(backend) for r in state_rows(s, version)]
        run(env, [json.dumps(added)], script=RELEASE_ADD)
        stored = rows(backend, path)
        new = {i: r for i, r in stored.items() if i not in before}
        changed = {i: sorted(k for k in r if k != "raw" and (stored.get(i) or {}).get(k) != r[k])
                   if i in stored else "gone"
                   for i, r in before.items() if stored.get(i) != r}
        # v0.1.2 keeps every namespace in one `<date>.md` and re-chunks it on
        # each write, so a short earlier row can merge into a new one (#318):
        # recorded, not refused. Every other source must keep its rows.
        rewritten = changed if (backend, version) == ("memtomem", "0.1.2") else None
        if changed and rewritten is None:
            sys.exit(f"source {backend}: earlier rows changed while state rows were added: "
                     f"{changed}")
        for s in namespaces_of(backend):
            got = sorted(r["content"] for r in new.values() if r["namespace"] == state_namespace(s))
            if len(got) < 3:
                sys.exit(f"source {backend}: {state_namespace(s)} holds {len(got)} rows: "
                         f"{[c[:80] for c in got]}")
        has_scratch = any((r["namespace"] or "").startswith("board-run:")
                          for r in stored.values())
        # On v0.1.2's shared day file the scratch row's text is re-chunked into
        # a later write's row under that write's namespace (#318); no
        # `board-run:` chunk is left to exclude.
        if not has_scratch and rewritten is None:
            sys.exit(f"source {backend}: the finished board run left no scratch row")
        index["sources"][backend] = {
            "path": str(path.relative_to(box)),
            "rows": {i: {k: r[k] for k in ("namespace", "tags", "content", "created_at")}
                     for i, r in sorted(stored.items())},
            "files": store_files(path if path.is_dir() else path.parent),
            "rewritten_by_state_rows": rewritten or {},
            "scratch_row": has_scratch,
        }
        bindings["stores"][f"sources/{backend}"] = str(path)

    def source_state(backend: str) -> tuple:
        path = sources[backend]
        logical = {i: {k: r[k] for k in ("namespace", "tags", "content", "created_at")}
                   for i, r in rows(backend, path).items()}
        if backend == "memtomem":
            # The release's own memtomem may rewrite its database on open; the
            # rows and the day files must not change.
            files = {k: v for k, v in store_files(path / "mem").items()}
        else:
            files = store_files(path if path.is_dir() else path.parent)
        return logical, files

    source_before = {b: source_state(b) for b in sources}

    # ---- one destination per direction ----------------------------------------
    for (source, target), states in DIRECTIONS.items():
        direction = f"{source}-to-{target}"
        droot = box / "destinations" / direction
        droot.mkdir(parents=True)
        dpath = {"memtomem": droot, "mem0": droot / "mem0",
                 "langgraph": droot / "langgraph.db"}[target]
        env = {**unused, **store_env(source, sources[source]), **store_env(target, dpath)}
        known: dict[str, dict] = {}
        index["destinations"][direction] = {"path": str(dpath.relative_to(box)),
                                            "source": source, "target": target}
        bindings["stores"][f"destinations/{direction}"] = str(dpath)

        def dest_rows() -> dict[str, dict]:
            return rows(target, dpath)

        for state_name in states:
            namespaces = [] if state_name == DEFAULT_STATE else [state_namespace(state_name)]
            label = f"{direction}/{state_name}"
            plan_argv = ["memory", "migration", "plan", "--from", source, "--to", target]
            for ns in namespaces:
                plan_argv += ["--namespace", ns]
            planned = run(env, plan_argv).stdout
            migration_id = planned.split("migration planned: ")[1].split()[0]
            steps = [{"argv": plan_argv, "exit": 0}]
            doc = artifact(mig, migration_id)
            records = doc["snapshot"]["records"]
            if len(records) < (1 if state_name == DEFAULT_STATE else 3):
                sys.exit(f"{label}: the plan selected {len(records)} records")

            def step(argv, *, launch=None, expect=0, record=True):
                if launch is None:
                    done = run(env, argv, expect=expect)
                else:
                    done = run(env, [*launch, *argv], script=RELEASE_LAUNCH, expect=expect)
                if record:
                    steps.append({"argv": argv, "launch": launch, "exit": done.returncode,
                                  "stdout": done.stdout.splitlines(),
                                  "stderr": [ln for ln in done.stderr.splitlines()
                                             if ln.startswith("Error")]})
                return done

            apply_argv = ["memory", "migration", "apply", migration_id]
            rollback_argv = ["memory", "migration", "rollback", migration_id, "--yes"]
            verify_argv = ["memory", "migration", "verify", migration_id]
            lineages = [r["lineage_key"] for r in records]
            tampered = None
            if state_name == "crashed-first":
                step(apply_argv, launch=["crash", "1", target], expect=CRASH_EXIT)
            elif state_name == "crashed":
                step(apply_argv, launch=["crash", "2", target], expect=CRASH_EXIT)
            elif state_name == "partial":
                step(apply_argv, launch=["fail-put", "2", target], expect="nonzero")
            elif state_name in ("verified", "verification_failed", "rolled_back",
                                "rollback_partial"):
                step(apply_argv)
                targets = json.loads((mig / migration_id / "state.json").read_text())["targets"]
                if state_name == "verification_failed":
                    tampered = targets[lineages[1]]
                    run(env, ["delete", target, tampered, ""], script=RELEASE_TAMPER)
                    steps.append({"tamper": "delete", "target_id": tampered})
                    step(verify_argv, expect="nonzero")
                elif state_name == "rolled_back":
                    step(rollback_argv)
                elif state_name == "rollback_partial":
                    tampered = targets[lineages[1]]
                    run(env, ["rewrite", target, tampered, CHANGED_SUFFIX], script=RELEASE_TAMPER)
                    steps.append({"tamper": "rewrite", "target_id": tampered,
                                  "suffix": CHANGED_SUFFIX})
                    step(rollback_argv, expect="nonzero")

            doc = artifact(mig, migration_id)
            st = doc["state"]
            now = dest_rows()
            # Every row written before this step is unchanged, except the one
            # this state's own tamper touched.
            for row_id, row in known.items():
                if now.get(row_id) != row:
                    sys.exit(f"{label}: an earlier destination row changed: {row_id}")
            mine = {i: r for i, r in now.items() if i not in known}
            if tampered is not None and state_name == "rollback_partial":
                mine_after = mine
            else:
                mine_after = mine
            ns_rows = {i: r for i, r in now.items()
                       if (r["namespace"] in namespaces if namespaces else
                           r["namespace"] in {x["namespace"] for x in records})}
            if set(ns_rows) != set(mine_after):
                sys.exit(f"{label}: rows outside this artifact's namespaces were written")
            want = _expected(state_name, records, st, doc, tampered, mine)
            if want is not None:
                sys.exit(f"{label}: {want}\nstate={json.dumps(st)}\nrows="
                         f"{json.dumps(sorted(r['content'] for r in mine.values()))}")
            for b, before in source_before.items():
                if source_state(b) != before:
                    sys.exit(f"{label}: the source store {b} changed")
            known = now
            index["artifacts"][label] = {
                "migration_id": migration_id, "direction": direction, "source": source,
                "target": target, "state": state_name, "namespaces": namespaces,
                "steps": steps, "tampered": tampered,
                "rows": {i: {k: r[k] for k in ("namespace", "tags", "content", "created_at")}
                         for i, r in sorted(mine.items())},
                "files": doc["files"],
            }
        index["destinations"][direction]["rows"] = {
            i: {k: r[k] for k in ("namespace", "tags", "content", "created_at")}
            for i, r in sorted(known.items())}
        index["destinations"][direction]["raw"] = {
            i: (r["raw"] if target == "memtomem" else
                {k: v for k, v in r["raw"].items() if k != "value"})
            for i, r in sorted(known.items())}

    # Each artifact is as its own step left it.
    for label, entry in index["artifacts"].items():
        if store_files(mig / entry["migration_id"]) != entry["files"]:
            sys.exit(f"{label}: a later step changed this artifact")

    for name in ("sources", "destinations"):
        shutil.copytree(box / name, out / name, symlinks=True)
    shutil.copytree(mig, out / "mig", symlinks=True)
    (out / "bindings.json").write_text(
        json.dumps(bindings, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    index["files"] = {str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sorted(out.rglob("*")) if p.is_file() and not p.is_symlink()}
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _generate_v1(box: Path, out: Path, python: Path, syncmill: Path, version: str,
                 cache: Path) -> None:
    home, mig, repo = box / "home", box / "mig", box / "repo"
    for folder in (home, repo):
        folder.mkdir()
    source, target = box / "langgraph.db", box / "mem0"
    env = {
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": "/usr/bin:/bin", "TMPDIR": str(box), "PYTHONDONTWRITEBYTECODE": "1",
        "FASTEMBED_CACHE_PATH": str(cache), "HF_HUB_OFFLINE": "1", "MEM0_TELEMETRY": "false",
        "SYNCMILL_MIGRATIONS_DIR": str(mig),
        "SYNCMILL_MEMORY__LANGGRAPH__DB_PATH": str(source),
        "SYNCMILL_MEMORY__MEM0__DATA_DIR": str(target),
        "SYNCMILL_MEMORY__DB_PATH": str(box / "unused" / "mm.db"),
        "SYNCMILL_MEMORY__MEMORY_DIR": str(box / "unused" / "mem"),
    }

    def run(argv, *, script=None, backend=None):
        command = [str(syncmill), *argv] if script is None else [str(python), "-c", script, *argv]
        extra = {"SYNCMILL_MEMORY__BACKEND": backend} if backend else {}
        done = subprocess.run(command, cwd=repo, env={**env, **extra}, capture_output=True,
                              text=True)
        if done.returncode != 0:
            sys.exit(f"{argv}: exit {done.returncode}:\n{done.stdout}{done.stderr}")
        return done.stdout

    states = ("planned", "applied")
    run([json.dumps([r for s in states for r in state_rows(s, version)])], script=RELEASE_ADD,
        backend="langgraph")
    index: dict = {"release": version, "artifacts": {}}
    for state_name in states:
        argv = ["memory", "migration", "plan", "--from", "langgraph", "--to", "mem0",
                "--namespace", state_namespace(state_name)]
        migration_id = run(argv).split("migration planned: ")[1].split()[0]
        steps = [argv]
        if state_name == "applied":
            apply_argv = ["memory", "migration", "apply", migration_id]
            run(apply_argv)
            steps.append(apply_argv)
        doc = artifact(mig, migration_id)
        if doc["manifest"]["schema_version"] != 1 or "target_storage_digest" in doc["manifest"]:
            sys.exit(f"{state_name}: not a schema 1 artifact: {doc['manifest']}")
        want = ({"status": "planned", "targets": {}, "errors": []} if state_name == "planned"
                else None)
        if want is not None and doc["state"] != want:
            sys.exit(f"{state_name}: {doc['state']}")
        if state_name == "applied" and (doc["state"]["status"] != "verified"
                                        or len(doc["state"]["targets"]) != 3):
            sys.exit(f"{state_name}: {doc['state']}")
        index["artifacts"][state_name] = {"migration_id": migration_id, "state": state_name,
                                          "steps": steps, "files": doc["files"]}
    for state_name, entry in index["artifacts"].items():
        if store_files(mig / entry["migration_id"]) != entry["files"]:
            sys.exit(f"{state_name}: a later step changed this artifact")
    shutil.copytree(mig, out / "mig", symlinks=True)
    index["files"] = {str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sorted(out.rglob("*")) if p.is_file() and not p.is_symlink()}
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _expected(state: str, records: list[dict], st: dict, doc: dict, tampered: str | None,
              mine: dict) -> str | None:
    """What is wrong with a state's end, or None."""
    n = len(records)
    contents = sorted(r["content"] for r in mine.values())
    want_status = {"planned": "planned", "crashed-first": "applying", "crashed": "applying",
                   "partial": "partial", "verified": "verified",
                   "verification_failed": "verification_failed", "rolled_back": "rolled_back",
                   "rollback_partial": "rollback_partial"}[state]
    if st["status"] != want_status:
        return f"status {st['status']} != {want_status}"
    ledger = st["targets"]
    first = records[0] if records else None
    second = records[1] if len(records) > 1 else None
    if state == "planned":
        ok = not ledger and not mine and not st["errors"]
    elif state == "crashed-first":
        ok = not ledger and contents == [first["content"]]
    elif state == "crashed":
        ok = (list(ledger) == [first["lineage_key"]]
              and contents == sorted([first["content"], second["content"]]))
    elif state == "partial":
        ok = (list(ledger) == [first["lineage_key"]] and contents == [first["content"]]
              and len(st["errors"]) == 1 and st["errors"][0]["lineage"] == second["lineage_key"]
              and PUT_ERROR in st["errors"][0]["error"])
    elif state == "verified":
        ok = (len(ledger) == n and contents == sorted(r["content"] for r in records)
              and doc["receipt"]["status"] == "verified" and doc["receipt"]["verified_count"] == n)
    elif state == "verification_failed":
        ok = (len(ledger) == n and tampered not in mine
              and contents == sorted(r["content"] for r in records if r is not second)
              and doc["receipt"]["failure_lineages"] == [second["lineage_key"]])
    elif state == "rolled_back":
        ok = (not ledger and not mine and doc["rollback"]["deleted"] == n
              and doc["rollback"]["status"] == "rolled_back")
    else:
        ok = (list(ledger) == [second["lineage_key"]]
              and contents == [second["content"] + CHANGED_SUFFIX]
              and doc["rollback"]["refused"] == [second["lineage_key"]]
              and doc["rollback"]["deleted"] == n - 1)
    return None if ok else f"{state}: unexpected end"


if __name__ == "__main__":
    main()
