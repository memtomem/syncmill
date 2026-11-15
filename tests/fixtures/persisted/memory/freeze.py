"""Produce the frozen memtomem stores under this directory (#271).

Run once per release. This script itself runs under any Python 3; everything
that touches the store is the release's own code: its CLI writes the rows and
its interpreter (`<release-root>/.venv`) reads them back, never the current
source. The output directory must not exist; frozen fixtures are never
regenerated, and a run that stops early leaves no output directory.

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev)
    python tests/fixtures/persisted/memory/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --out tests/fixtures/persisted/memory/v0.3.0

The released CLI writes every row through its default backend, memtomem at the
version the release locked: `run` under each strategy, `board run`, and
`room create` / `round` / `select` / `apply`, with fake `codex` and `kimi`
binaries on PATH. One store comes out, with every scenario in it one after
another. The last is a `board run -s pipeline` whose CLI is SIGKILLed while its
second stage works, so the store is copied as that kill left it (its newest
row only in the WAL, which is checked; a live lease on the board) and never
reopened.

Each fake reply carries a common token and a token of its own scenario. The
stores are written with no embedding provider, so a search finds a row only
through a word, and the tests address the store, or one scenario, by those.

Generation fails unless every scenario left exactly the rows its definition
names (`_require`), so a release that writes something else cannot silently
narrow the coverage. The store is copied byte for byte as soon as the last
scenario ends. Only then does the release's own interpreter read the store it
wrote, in place, and record what it makes of every row: that is the expected
interpretation the tests hold HEAD's readers to. `index.json` holds the
scenario definitions and a sha256 of every file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ANCHOR = "frozenmemoryanchor"
TREE = {"calc.py": "def add(a, b):\n    return a - b\n"}
PROJECT_ID = "frozen-memory"

# FAKE_PLAN = {binary: {"token", "edit": [call...], "fail": [...], "hang": [...],
# "replies": {call: text}}}. Calls are numbered per binary and per scenario. A
# reply not listed is "<binary> (fake) call <n>: <anchor> <token>".
FAKE_AGENT = r'''
import json, os, sys, time
from pathlib import Path
BINARY = os.path.basename(sys.argv[0])
args = sys.argv[1:]
if args[:2] == ["login", "status"] or args[:1] in (["--version"], ["--help"]):
    print(BINARY + " (fake) - logged in"); sys.exit(0)
plan = json.loads(os.environ.get("FAKE_PLAN") or "{}").get(BINARY, {})
calls = Path(os.environ["FAKE_CALLS"])
n = 1
while True:
    try:
        os.close(os.open(calls / (BINARY + "-" + str(n)), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        break
    except FileExistsError:
        n += 1
step = str(n)
if step in plan.get("hang", []):
    marker = Path(os.environ["FAKE_STARTED"]) / BINARY
    marker.with_suffix(".tmp").write_text(str(os.getpid()))
    os.replace(marker.with_suffix(".tmp"), marker)
    time.sleep(120); sys.exit(1)
if step in plan.get("fail", []):
    sys.stderr.write(BINARY + " (fake): refused\n"); sys.exit(1)
if step in plan.get("edit", []):
    body = "a + b" if BINARY == "codex" else "b + a"
    Path("calc.py").write_text("def add(a, b):\n    return " + body + "\n", encoding="utf-8")
message = plan.get("replies", {}).get(step)
if message is None:
    message = "%s (fake) call %d: %s %s" % (BINARY, n, os.environ["FAKE_ANCHOR"], plan.get("token", ""))
if BINARY == "codex":
    events = [
        {"type": "thread.started", "thread_id": "fake-codex-thread"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
else:
    events = [
        {"role": "assistant", "content": message},
        {"role": "meta", "type": "session.resume_hint", "session_id": "fake-kimi-session",
         "content": "To resume this session: kimi -r fake-kimi-session"},
    ]
for event in events:
    print(json.dumps(event))
'''

# Runs inside the RELEASED interpreter on the store it wrote, in place: what
# the release itself reads from every row. argv: db, memory dir, anchor, and a
# JSON list of the kind tags to search by.
RELEASE_READ = r'''
import inspect, json, sqlite3, sys
from syncmill.config import MemoryConfig
from syncmill.memory import run_async
from syncmill.memory_backend import MemtomemBackend
db, mem, anchor, kinds = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
ids = [r[0] for r in conn.execute("SELECT id FROM chunks ORDER BY rowid")]
namespaces = sorted({r[0] for r in conn.execute("SELECT namespace FROM chunks")})
conn.close()
backend = MemtomemBackend(MemoryConfig(db_path=db, memory_dir=mem))
has_filter = "tag_filter" in inspect.signature(backend.search).parameters


def plain(row):
    return None if row is None else {k: row[k] for k in sorted(row) if k not in ("score", "rank")}


read = {"get": {}, "count": {}, "search": {}, "search_by_kind": {}}
try:
    for chunk_id in ids:
        read["get"][chunk_id] = plain(run_async(backend.get(chunk_id)))
    for ns in namespaces:
        read["count"][ns] = run_async(backend.count_namespace(ns))
        rows = run_async(backend.search(anchor, namespace=ns, top_k=200))
        read["search"][ns] = sorted(r["id"] for r in rows)
        if has_filter:
            read["search_by_kind"][ns] = {
                kind: sorted(
                    r["id"] for r in run_async(
                        backend.search(anchor, namespace=ns, top_k=200, tag_filter=kind)
                    )
                )
                for kind in kinds
            }
finally:
    run_async(backend.close())
print(json.dumps(read, ensure_ascii=False, sort_keys=True))
'''

GIT_ENV = {
    "GIT_AUTHOR_NAME": "frozen", "GIT_AUTHOR_EMAIL": "frozen@example.invalid",
    "GIT_COMMITTER_NAME": "frozen", "GIT_COMMITTER_EMAIL": "frozen@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}

KINDS = [
    "task-result", "room-result", "pipeline-stage", "council-proposal",
    "council-critique", "decompose-plan", "decompose-subtask",
]

UNICODE_REPLY = (
    "  " + ANCHOR + " tokunicode 한글 본문 と日本語 🙂\r\nsecond line after CRLF\r\n"
    + "가나다라마바사 " * 170 + "tail-after-the-cut  "
)
HEADINGS_REPLY = (
    ANCHOR + " tokheadings summary line\n\n"
    "### Findings\n\n" + ANCHOR + " tokheadings findings. "
    + " ".join(["The add function subtracted its second argument."] * 8) + "\n\n"
    "### Next steps\n\n" + ANCHOR + " tokheadings steps. "
    + " ".join(["Add a test for negative numbers and for zero."] * 8)
)
LONG_PROMPT = (
    ANCHOR + " toklong fix add in calc.py.\n\n"
    + "\n\n".join(f"requirement-{i:03d} keeps the sum of two integers exact" for i in range(60))
)
LONG_SPLIT = "requirement-037 "
SUBTASKS = [
    f"alpha part of the work {ANCHOR} tokdecompose",
    f"beta part of the work {ANCHOR} tokdecompose",
]


def default_reply(binary: str, call: int, token: str) -> str:
    return f"{binary} (fake) call {call}: {ANCHOR} {token}"


def task_body(prompt: str, agent: str, output: str, files: bool) -> str:
    """The body v0.2.0 and later give a shared result (memory.share_result)."""
    body = (
        f"**Task Result**\n\n**Prompt:** {prompt}\n\n"
        f"**Agent:** {agent}\n\n**Output:**\n{output[:1000]}"
    )
    return body + ("\n\n**Files changed:** calc.py" if files else "")


def board_body(item_id: str, task: str, output: str) -> str:
    return (
        f"**Board Item Result**\n\n**Item:** {item_id}\n\n"
        f"**Task:** {task}\n\n**Output:**\n{output[:1000]}"
    )


def pair(kind: str, agent: str, body: str) -> list[dict]:
    """A result shared from an agent's private namespace: the private row and
    its copy in `shared`, which carries one more tag naming the private row."""
    tags = sorted([kind, agent, "task:*"])
    return [
        {"namespace": f"agent-runtime:{agent}", "tags": tags, "body": body},
        {"namespace": "shared", "tags": sorted([*tags, "shared-from=*"]), "body": body},
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal_tags(raw: str) -> list[str]:
    out = []
    for tag in json.loads(raw):
        if re.fullmatch(r"task:[0-9a-f-]{36}", tag):
            tag = "task:*"
        elif re.fullmatch(r"shared-from=[0-9a-f-]{36}", tag):
            tag = "shared-from=*"
        out.append(tag)
    return sorted(out)


def _query(db: Path, sql: str, params: tuple = (), *, wal: bool = True) -> list[tuple]:
    """Run one query on a throwaway copy of a database (its WAL with it, unless
    ``wal`` is false), so a database a release wrote is never opened by this
    script and a reader always sees what the release committed."""
    with tempfile.TemporaryDirectory() as tmp:
        parts = db.parent.glob(db.name + "*") if wal else [db]
        for part in parts:
            shutil.copy(part, Path(tmp) / part.name)
        conn = sqlite3.connect(Path(tmp) / db.name)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


def _raw_rows(db: Path) -> dict[str, tuple]:
    """The chunk rows of a store as stored: id -> (namespace, tags JSON,
    content, source_file)."""
    return {
        r[0]: tuple(r[1:])
        for r in _query(
            db, "SELECT id, namespace, tags, content, source_file FROM chunks ORDER BY rowid"
        )
    }


def _normal(raw: tuple) -> dict:
    """A stored row the way the scenario definitions name one."""
    return {"namespace": raw[0], "tags": _normal_tags(raw[1]), "content": raw[2],
            "source": Path(raw[3]).name}


def _require(name: str, new: list[dict], expected: list[dict]) -> None:
    def key(row: dict) -> tuple:
        return (row["namespace"], tuple(row["tags"]), row["body"])

    got = sorted(
        ({"namespace": r["namespace"], "tags": r["tags"], "body": r["content"]} for r in new),
        key=key,
    )
    want = sorted(expected, key=key)
    if got != want:
        sys.exit(
            f"{name}: the release left other rows than the scenario names\n"
            f"--- left\n{json.dumps(got, ensure_ascii=False, indent=1)}\n"
            f"--- named\n{json.dumps(want, ensure_ascii=False, indent=1)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    release = args.release_root.resolve()
    # The parent is resolved, not the target: a symlink there must be refused,
    # not followed.
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
    if version not in ("0.2.0", "0.3.0"):
        sys.exit(f"no scenario definitions for syncmill {version} yet")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp).resolve() / "frozen"
        _generate(Path(tmp).resolve(), stage, python, syncmill, version, metadata("memtomem"))
        # Publish whole: a hidden sibling first (fixture discovery skips it),
        # then one rename into place, the target checked again just before.
        # The sibling is removed on any failure, an interrupt included.
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(tempfile.mkdtemp(dir=out.parent, prefix=f".{out.name}.partial-"))
        try:
            shutil.copytree(stage, partial / "frozen")
            refuse_existing()
            (partial / "frozen").rename(out)
        finally:
            shutil.rmtree(partial)
    print(f"frozen {version} into {out}")


def _generate(box: Path, out: Path, python: Path, syncmill: Path,
              version: str, memtomem: str) -> None:
    repo, home, state, fakebin, started = (
        box / n for n in ("repo", "home", "state", "fakebin", "started")
    )
    for folder in (repo, home, state, fakebin, started):
        folder.mkdir()
    for binary in ("codex", "kimi"):
        (fakebin / binary).write_text(f"#!{sys.executable}\n" + FAKE_AGENT, encoding="utf-8")
        (fakebin / binary).chmod(0o755)
    (fakebin / "python").symlink_to(sys.executable)
    common_env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
        "TMPDIR": str(box),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "FAKE_STARTED": str(started),
        "FAKE_ANCHOR": ANCHOR,
        "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex", "kimi-code"]),
        "SYNCMILL_CLAUDE__ENABLED": "false",
        "SYNCMILL_COUNCIL__ROUNDS": "2",
        "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
        "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
        "SYNCMILL_TUI__STATE_DIR": str(state / "tui"),
        **GIT_ENV,
    }

    def init_repo(root: Path) -> None:
        def git(*argv: str) -> None:
            subprocess.run(["git", *argv], cwd=root, env=common_env, check=True,
                           capture_output=True)
        git("init", "-q", "-b", "main")
        for name, text in TREE.items():
            (root / name).write_text(text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "frozen tree")

    init_repo(repo)
    out.mkdir(parents=True)
    index: dict = {
        "release": version,
        "memtomem": memtomem,
        "anchor": ANCHOR,
        "generating_root": str(box),
        "stores": {},
        "scenarios": {},
    }
    counter = [0]

    class Store:
        def __init__(self, name: str, root: Path, db: Path, mem: Path, env: dict):
            self.name, self.root, self.db, self.mem = name, root, db, mem
            self.env = {**common_env, **env}
            self.known: dict[str, tuple] = {}

        def sm(self, scenario: str, *argv: str, plan: dict | None = None,
               ok: bool = True, cwd: Path = repo) -> str:
            calls = state / f"calls-{counter[0]}"
            counter[0] += 1
            calls.mkdir()
            done = subprocess.run(
                [str(syncmill), *argv], cwd=cwd,
                env={**self.env, "FAKE_PLAN": json.dumps(plan or {}),
                     "FAKE_CALLS": str(calls)},
                capture_output=True, text=True,
            )
            if ok and done.returncode != 0:
                sys.exit(f"{scenario}: {argv}: {done.stdout}{done.stderr}")
            if not ok and done.returncode == 0:
                sys.exit(f"{scenario}: {argv} was expected to fail: {done.stdout}")
            return done.stdout

        def new_rows(self) -> list[dict]:
            """The rows added since the last call. Every earlier row must still
            be there exactly as stored (ids in its tags and its full source
            path included): a scenario only adds."""
            rows = _raw_rows(self.db) if self.db.exists() else {}
            changed = {
                chunk_id: (row, rows.get(chunk_id))
                for chunk_id, row in self.known.items() if rows.get(chunk_id) != row
            }
            if changed:
                sys.exit(f"an earlier row was changed or removed: "
                         f"{json.dumps(changed, ensure_ascii=False, indent=1)}")
            new = [_normal(row) for chunk_id, row in rows.items() if chunk_id not in self.known]
            self.known = rows
            return new

        def record(self, scenario: str, expected: list[dict], **facts) -> None:
            _require(scenario, self.new_rows(), expected)
            index["scenarios"][scenario] = {"store": self.name, "rows": expected, **facts}

        def freeze(self, *, board: bool) -> None:
            target = out / self.name
            shutil.copytree(self.root, target)
            index["stores"][self.name] = {
                "db": str((target / self.db.relative_to(self.root)).relative_to(out)),
                "mem": str((target / self.mem.relative_to(self.root)).relative_to(out)),
                "board": f"{self.name}/board.db" if board else None,
                "root": str(self.root),
            }

    def explicit_store(name: str) -> Store:
        root = state / name
        root.mkdir()
        return Store(name, root, root / "mm.db", root / "mem", {
            "SYNCMILL_PROJECT__ID": PROJECT_ID,
            "SYNCMILL_RUNS__ROOT": str(state / f"runs-{name}"),
            "SYNCMILL_MEMORY__DB_PATH": str(root / "mm.db"),
            "SYNCMILL_MEMORY__MEMORY_DIR": str(root / "mem"),
            "SYNCMILL_BOARD__ENABLED": "true",
            "SYNCMILL_BOARD__DB_PATH": str(root / "board.db"),
            "SYNCMILL_ROOMS__ENABLED": "true",
        })

    def restore_tree() -> None:
        subprocess.run(["git", "checkout", "--", "."], cwd=repo, env=common_env,
                       check=True, capture_output=True)

    main_store = explicit_store("store")

    prompt = f"{ANCHOR} tokroute fix add in calc.py"
    plan = {"codex": {"token": "tokroute", "edit": ["1"]}}
    main_store.sm("route", "run", prompt, "-a", "codex", plan=plan)
    main_store.record(
        "route",
        pair("task-result", "codex",
             task_body(prompt, "codex", default_reply("codex", 1, "tokroute"), True)),
        prompt=prompt, token="tokroute",
    )

    prompt = f"{ANCHOR} tokpipeline fix add in calc.py"
    plan = {"codex": {"token": "tokpipeline", "edit": ["1"]},
            "kimi": {"token": "tokpipeline", "edit": ["1"]}}
    main_store.sm("pipeline", "run", prompt, "-s", "pipeline", plan=plan)
    main_store.record(
        "pipeline",
        # A stage before the last is shared before its changes are collected.
        pair("pipeline-stage", "codex",
             task_body(prompt, "codex", default_reply("codex", 1, "tokpipeline"), False))
        + pair("task-result", "kimi-code",
               task_body(prompt, "kimi-code", default_reply("kimi", 1, "tokpipeline"), True)),
        prompt=prompt, token="tokpipeline",
    )

    prompt = f"{ANCHOR} tokcompete fix add in calc.py"
    plan = {"codex": {"token": "tokcompete", "edit": ["1"]},
            "kimi": {"token": "tokcompete", "edit": ["1"]}}
    main_store.sm("compete", "run", prompt, "-s", "compete", plan=plan)
    main_store.record(
        "compete",
        pair("task-result", "codex",
             task_body(prompt, "codex", default_reply("codex", 1, "tokcompete"), True)),
        prompt=prompt, token="tokcompete",
    )

    # Two critique rounds (SYNCMILL_COUNCIL__ROUNDS): calls 1 propose, 2 and 3
    # critique, 4 synthesize. Only the synthesis edits the tree.
    prompt = f"{ANCHOR} tokcouncil fix add in calc.py"
    plan = {"codex": {"token": "tokcouncil", "edit": ["4"]},
            "kimi": {"token": "tokcouncil", "edit": ["4"]}}
    main_store.sm("council", "run", prompt, "-s", "council", plan=plan)
    expected: list[dict] = []
    for agent, binary in (("codex", "codex"), ("kimi-code", "kimi")):
        expected += pair("council-proposal", agent, task_body(
            prompt, agent, default_reply(binary, 1, "tokcouncil"), False))
        for call in (2, 3):
            expected += pair("council-critique", agent, task_body(
                prompt, agent, default_reply(binary, call, "tokcouncil"), False))
    expected += pair("task-result", "codex", task_body(
        prompt, "codex", default_reply("codex", 4, "tokcouncil"), True))
    main_store.record("council", expected, prompt=prompt, token="tokcouncil")

    # codex call 1 plans (a JSON array, which is all the plan row keeps), then
    # codex takes the first subtask and kimi the second, then both synthesize.
    prompt = f"{ANCHOR} tokdecompose fix add in calc.py"
    plan = {"codex": {"token": "tokdecompose", "edit": ["3"],
                      "replies": {"1": json.dumps(SUBTASKS)}},
            "kimi": {"token": "tokdecompose", "edit": ["2"]}}
    main_store.sm("decompose", "run", prompt, "-s", "decompose", plan=plan)
    main_store.record(
        "decompose",
        pair("decompose-plan", "codex", task_body(
            prompt, "codex", json.dumps(SUBTASKS, ensure_ascii=False), False))
        + pair("decompose-subtask", "codex", task_body(
            prompt, "codex", default_reply("codex", 2, "tokdecompose"), False))
        + pair("decompose-subtask", "kimi-code", task_body(
            prompt, "kimi-code", default_reply("kimi", 1, "tokdecompose"), False))
        + pair("task-result", "codex", task_body(
            prompt, "codex", default_reply("codex", 3, "tokdecompose"), True)),
        prompt=prompt, token="tokdecompose", subtasks=SUBTASKS,
    )

    # The agent's reply is cut at 1000 characters after its outer whitespace
    # is dropped; memtomem reads the day file in text mode, so the CRLF pair
    # inside the reply reaches the row as LF.
    prompt = f"{ANCHOR} tokunicode say something long"
    plan = {"codex": {"token": "tokunicode", "replies": {"1": UNICODE_REPLY}}}
    main_store.sm("unicode", "run", prompt, "-a", "codex", plan=plan)
    stored_reply = UNICODE_REPLY.strip()[:1000].replace("\r\n", "\n")
    main_store.record(
        "unicode",
        pair("task-result", "codex", task_body(prompt, "codex", stored_reply, False)),
        prompt=prompt, token="tokunicode", reply=UNICODE_REPLY,
    )

    prompt = f"{ANCHOR} tokheadings review calc.py"
    plan = {"codex": {"token": "tokheadings", "replies": {"1": HEADINGS_REPLY}}}
    main_store.sm("headings", "run", prompt, "-a", "codex", plan=plan)
    # memtomem keeps this body in one chunk: the `###` lines stay inside it.
    main_store.record(
        "headings",
        pair("task-result", "codex", task_body(prompt, "codex", HEADINGS_REPLY, False)),
        prompt=prompt, token="tokheadings", reply=HEADINGS_REPLY,
    )

    plan = {"codex": {"token": "toklong"}}
    main_store.sm("longprompt", "run", LONG_PROMPT, "-a", "codex", plan=plan)
    # One private entry, two chunks: memtomem cuts this body at the blank line
    # before LONG_SPLIT. Each chunk gets its own copy in `shared`, and each
    # copy is an entry of its own there.
    body = task_body(LONG_PROMPT, "codex", default_reply("codex", 1, "toklong"), False)
    head, tail = body.split("\n\n" + LONG_SPLIT, 1)
    main_store.record(
        "longprompt",
        pair("task-result", "codex", head) + pair("task-result", "codex", LONG_SPLIT + tail),
        prompt=LONG_PROMPT, token="toklong", private_chunks_per_entry=2,
    )

    prompt = f"{ANCHOR} tokfailed fix add in calc.py"
    plan = {"codex": {"token": "tokfailed", "fail": ["1"]}}
    main_store.sm("failed", "run", prompt, "-a", "codex", plan=plan, ok=False)
    main_store.record("failed", [], prompt=prompt, token="tokfailed")

    task_text = f"{ANCHOR} tokboard fix add in calc.py"
    plan = {"codex": {"token": "tokboard", "edit": ["1"]}}
    line = main_store.sm("board-done", "board", "add", task_text, "--goal", "frozen")
    item_id = line.split("added item ")[1].split()[0]
    main_store.sm("board-done", "board", "run", "--id", item_id, plan=plan)
    scratch = [r for r in main_store.new_rows()]
    runs = sorted({r["namespace"] for r in scratch if r["namespace"].startswith("board-run:")})
    if len(runs) != 1:
        sys.exit(f"board-done: expected one scratch namespace, found {runs}")
    reply = default_reply("codex", 1, "tokboard")
    board_expected = [
        {"namespace": runs[0], "tags": sorted(["task-result", "codex", "task:*"]), "body": None},
        {"namespace": "shared", "tags": [f"board-result:{item_id}"],
         "body": board_body(item_id, task_text, reply)},
    ]
    # The scratch row's prompt is the rendered board item, the release's own
    # text; only its frame is named here.
    found = sorted(scratch, key=lambda r: r["namespace"] != runs[0])
    if (
        len(found) != 2
        or found[0]["tags"] != board_expected[0]["tags"]
        or not found[0]["content"].startswith("**Task Result**\n\n**Prompt:** ")
        or not found[0]["content"].endswith(
            f"**Agent:** codex\n\n**Output:**\n{reply}\n\n**Files changed:** calc.py")
        or task_text not in found[0]["content"]
        or {"namespace": found[1]["namespace"], "tags": found[1]["tags"],
            "body": found[1]["content"]} != board_expected[1]
    ):
        sys.exit(f"board-done: unexpected rows {json.dumps(found, ensure_ascii=False, indent=1)}")
    board_expected[0]["body"] = found[0]["content"]
    board_expected[0]["body_is_release_text"] = True
    index["scenarios"]["board-done"] = {
        "store": "store", "rows": board_expected, "task": task_text, "token": "tokboard",
        "item_id": item_id, "run_id": runs[0].split(":", 1)[1],
    }
    restore_tree()

    prompt = f"{ANCHOR} tokroom fix add in calc.py"
    plan = {"codex": {"token": "tokroom", "edit": ["1"]},
            "kimi": {"token": "tokroom", "edit": ["1"]}}
    room_id = main_store.sm("room-apply", "room", "create", prompt,
                            "-a", "codex", "-a", "kimi-code").split()[0]
    main_store.sm("room-apply", "room", "round", room_id, plan=plan)
    rooms_db = state / "runs-store" / PROJECT_ID / "rooms" / "rooms.sqlite3"
    candidates = _query(
        rooms_db,
        "SELECT id, member_id FROM candidates WHERE room_id = ? AND eligible = 1 "
        "ORDER BY member_id", (room_id,),
    )
    if not candidates or candidates[0][1] != "codex":
        sys.exit(f"room-apply: no eligible codex candidate: {candidates}")
    main_store.sm("room-apply", "room", "select", room_id, candidates[0][0])
    main_store.sm("room-apply", "room", "apply", room_id)
    main_store.record(
        "room-apply",
        pair("room-result", "codex",
             task_body(prompt, "codex", default_reply("codex", 1, "tokroom"), True)),
        prompt=prompt, token="tokroom", room_id=room_id,
    )
    restore_tree()

    # ---- last: a board run SIGKILLed during its second stage ----------------
    killed = main_store
    task_text = f"{ANCHOR} tokkilled fix add in calc.py"
    line = killed.sm("board-killed", "board", "add", task_text, "--goal", "frozen")
    item_id = line.split("added item ")[1].split()[0]
    for marker in started.iterdir():
        marker.unlink()
    calls = state / "calls-killed"
    calls.mkdir()
    plan = {"codex": {"token": "tokkilled", "edit": ["1"]},
            "kimi": {"token": "tokkilled", "hang": ["1"]}}
    proc = subprocess.Popen(
        [str(syncmill), "board", "run", "--id", item_id, "-s", "pipeline"],
        cwd=repo, env={**killed.env, "FAKE_PLAN": json.dumps(plan), "FAKE_CALLS": str(calls)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    def kill_group() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 120
    while not (started / "kimi").exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            # The whole group, or an agent still holding the output pipe
            # would keep the read below from ever returning.
            kill_group()
            try:
                output = proc.communicate(timeout=30)[0]
            except subprocess.TimeoutExpired:
                output = "(no output: the pipe is still held open)"
            sys.exit(f"board-killed: the second stage never started: {output}")
        time.sleep(0.05)
    agent_pid = int((started / "kimi").read_text())
    kill_group()
    proc.wait()
    try:
        os.kill(agent_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.stdout.close()
    scratch = killed.new_rows()
    runs = sorted({r["namespace"] for r in scratch}) or [""]
    reply = default_reply("codex", 1, "tokkilled")
    if (
        len(scratch) != 1
        or not runs[0].startswith("board-run:")
        or scratch[0]["tags"] != sorted(["pipeline-stage", "codex", "task:*"])
        or not scratch[0]["content"].endswith(f"**Agent:** codex\n\n**Output:**\n{reply}")
    ):
        sys.exit(f"board-killed: unexpected rows {json.dumps(scratch, ensure_ascii=False, indent=1)}")
    # The kill left the stage's row in the WAL only: the main file alone does
    # not have it.
    in_main = _query(killed.db, "SELECT COUNT(*) FROM chunks WHERE namespace = ?",
                     (runs[0],), wal=False)
    if in_main != [(0,)]:
        sys.exit(f"board-killed: the scratch row is already in the main file: {in_main}")
    (lease,) = _query(
        killed.root / "board.db",
        "SELECT state, lease_run_id, claimed_at FROM work_items WHERE id = ?", (item_id,),
    )
    if lease[0] != "running" or f"board-run:{lease[1]}" != runs[0]:
        sys.exit(f"board-killed: the board holds no live lease for the scratch run: {lease}")
    index["scenarios"]["board-killed"] = {
        "store": "store", "task": task_text, "token": "tokkilled", "item_id": item_id,
        "run_id": lease[1], "claimed_at": lease[2],
        "rows": [{"namespace": runs[0], "tags": scratch[0]["tags"],
                  "body": scratch[0]["content"], "body_is_release_text": True}],
    }
    restore_tree()
    killed.freeze(board=True)

    # ---- what the release reads from the store it wrote, in place -----------
    read = {}
    for store in (main_store,):
        done = subprocess.run(
            [str(python), "-c", RELEASE_READ, str(store.db), str(store.mem), ANCHOR,
             json.dumps(KINDS)],
            env=store.env, capture_output=True, text=True,
        )
        if done.returncode != 0:
            sys.exit(f"release read of {store.name}: {done.stdout}{done.stderr}")
        read[store.name] = json.loads(done.stdout.strip().splitlines()[-1])
    (out / "release-read.json").write_text(
        json.dumps(read, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )

    dates = sorted({
        p.name[:10] for p in out.rglob("*.md") if re.match(r"\d{4}-\d{2}-\d{2}\.", p.name)
    })
    if len(dates) != 1:
        sys.exit(f"the day files span more than one UTC date ({dates}); generate again")
    index["date"] = dates[0]
    index["files"] = {
        str(p.relative_to(out)): _sha256(p) for p in sorted(out.rglob("*")) if p.is_file()
    }
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
