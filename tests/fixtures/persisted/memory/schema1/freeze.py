"""Produce the frozen schema-1 memtomem stores under this directory (#271).

v0.1.2 and v0.1.4 locked memtomem 0.3.x, whose stores are `_memtomem_meta` schema
1 and which HEAD migrates on open. Run once per release, like ``../freeze.py``,
whose fake agents, scenario texts and release read this script reuses (imported by
path; that file is not changed):

    git worktree add --detach /tmp/wt-v0.1.4 v0.1.4
    (cd /tmp/wt-v0.1.4 && uv sync --frozen --no-dev)
    python tests/fixtures/persisted/memory/schema1/freeze.py \\
        --release-root /tmp/wt-v0.1.4 --out tests/fixtures/persisted/memory/schema1/v0.1.4

The same scenarios run in the same order as for v0.2.0 and later, one store per
release. What differs is what can be required of them. memtomem 0.3.x appends an
entry to a day file and then re-derives the chunks of the WHOLE file, merging
short neighbours, so a later write replaces the rows an earlier one left (new ids,
merged content) and a row-per-scenario delta does not exist. What a release
writes deterministically is the day-file entry, so generation requires, per CLI
step:

- every day file that existed before the step is a byte prefix of itself after it
  (append-only);
- the entries the step appended are exactly those the scenario definition names
  (file, namespace, quoted tags, body). A `shared` copy's body is the content of
  a chunk the release derived, so the definition names it by its source and the
  release's own chunker supplies it (below).

Then the RELEASE's interpreter replays the history from the frozen bytes
(``ORACLE``): for each day file it derives the chunks at every entry boundary with
the release's own memtomem chunker, and generation requires that

- the derivation at the last indexed entry equals the stored rows exactly (start
  and end line, content, tags, namespace), where an unchanged chunk keeps its
  namespace and a new one takes the namespace of the write that produced it;
- the `shared` copies a step appended are exactly the chunks that step's private
  writes produced (as a multiset: the release shares every new chunk id), each
  copy's `shared-from` id naming, where a snapshot saw it, a row with that
  content in the private namespace.

A write the release itself reports as degraded (its CLI prints the notice and
exits 0) is recorded as not indexed, and the derivation proves the stored rows
leave it out. The chunk rows are recorded, not predicted: memtomem merges two
`shared` copies whose `## Entry <second>` headings coincide, so which of two real
shapes the frozen bytes hold depends on the clock, and both satisfy every check.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.dont_write_bytecode = True
_SPEC = importlib.util.spec_from_file_location(
    "frozen_memory_freeze", Path(__file__).resolve().parent.parent / "freeze.py"
)
base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base)

ANCHOR = base.ANCHOR
PROJECT_ID = base.PROJECT_ID
#: Each release's day-file layout: v0.1.4 one file per namespace (results are
#: written private-first and copied into `shared`), v0.1.2 ONE `<date>.md` for
#: every namespace (results are written to `shared` directly).
LAYOUTS = {"0.1.2": "mixed", "0.1.4": "split"}
RELEASES = tuple(LAYOUTS)

# One entry block as memtomem 0.3.x `format_entry_block` writes it: the
# created/tags quote, a blank line, the stripped content and a newline, under a
# `## <title>` heading unless the content itself starts with `## `. So a block
# with no title must open a `## ` body that is not itself a titled block: a merged
# chunk copied into `shared` keeps the quote lines of the sections it absorbed
# (also a trailing one, parsed before the next section's heading) but not their
# headings, and those inner quotes must not read as blocks.
_BLOCK = re.compile(
    r"\n(?:## (?P<title>[^\n]*)\n\n> created: (?P<created>[^\n]*)"
    r"(?:\n> tags: (?P<tags>[^\n]*))?\n\n"
    r"|> created: (?P<bare_created>[^\n]*)(?:\n> tags: (?P<bare_tags>[^\n]*))?\n\n"
    r"(?=## )(?!## [^\n]*\n\n> created: ))"
)
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_DEGRADED_BOARD = "the board-result summary may not have reached shared"

# Runs inside the RELEASED interpreter. argv: a JSON file {name: {"path": ...,
# "events": [{"end": byte offset, "namespace": ns, "indexed": bool} | {"purge":
# ns}]}}. Replays each file's history with the release's own chunker and prints,
# per file, the chunks at the end (with the namespace each carries: an unchanged
# chunk keeps its own, a new one takes the namespace of the write that produced
# it, and a purge drops a namespace's chunks, whose text then counts as new at the
# next write) and, per write, the chunks it produced.
ORACLE = r'''
import inspect, json, sys
from collections import Counter
from pathlib import Path
from memtomem.config import IndexingConfig
from memtomem.chunking.markdown import MarkdownChunker
from memtomem.indexing.engine import _merge_short_chunks
cfg = IndexingConfig()
chunker = MarkdownChunker(cfg)
params = inspect.signature(_merge_short_chunks).parameters
def derive(path, data):
    text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    args = [cfg.min_chunk_tokens, cfg.max_chunk_tokens]
    if len(params) > 3:
        args.append(cfg.target_chunk_tokens)
    return [(c.metadata.start_line, c.metadata.end_line, c.content, sorted(c.metadata.tags))
            for c in _merge_short_chunks(chunker.chunk_file(path, text), *args)]
out = {}
for name, spec in json.loads(Path(sys.argv[1]).read_text()).items():
    path = Path(spec["path"]); data = path.read_bytes()
    current = []   # [start, end, content, tags, namespace]
    produced = []
    for entry in spec["events"]:
        if "purge" in entry:
            current = [c for c in current if c[4] != entry["purge"]]
            continue
        if not entry["indexed"]:
            produced.append(None)
            continue
        derived = derive(path, data[:entry["end"]])
        pool = Counter(c[2] for c in current)
        owner = {}
        for c in current:
            owner.setdefault(c[2], []).append(c[4])
        new_current, fresh = [], []
        for start, end, content, tags in derived:
            if pool[content] > 0:
                pool[content] -= 1
                ns = owner[content].pop(0)
            else:
                ns = entry["namespace"]
                fresh.append(content)
            new_current.append([start, end, content, tags, ns])
        current = new_current
        produced.append(fresh)
    out[name] = {"final": current, "produced": produced}
print(json.dumps(out, ensure_ascii=False, sort_keys=True))
'''


# ---- pure checks (the default suite drives them with bad input) -----------------

class GenerationError(Exception):
    pass


def check_append_only(before: dict[str, bytes], after: dict[str, bytes],
                      may_vanish=lambda name: False) -> dict[str, bytes]:
    """The text each file gained. Every file that existed must still start with
    exactly its earlier bytes; only a file ``may_vanish`` admits may disappear."""
    gained = {}
    for name, old in before.items():
        new = after.get(name)
        if new is None:
            if may_vanish(name):
                continue
            raise GenerationError(f"{name} disappeared")
        if not new.startswith(old):
            raise GenerationError(f"{name} was rewritten, not appended to")
        if len(new) > len(old):
            gained[name] = new[len(old):]
    for name, new in after.items():
        if name not in before:
            gained[name] = new
    return gained


def parse_entries(text: str) -> list[dict]:
    """Split appended text into memtomem 0.3.x entry blocks. The text must be
    blocks and nothing else; each body is the stripped content its block holds."""
    starts = list(_BLOCK.finditer(text))
    if not starts or starts[0].start() != 0:
        raise GenerationError(f"appended text does not start with an entry: {text[:80]!r}")
    entries = []
    for match, nxt in zip(starts, starts[1:] + [None]):
        end = nxt.start() if nxt else len(text)
        body = text[match.end():end]
        if not body.endswith("\n"):
            raise GenerationError(f"an entry does not end with a newline: {body[-80:]!r}")
        tags = match["tags"] if match["title"] is not None else match["bare_tags"]
        entries.append({
            "title": match["title"],
            "created": match["created"] if match["title"] is not None else match["bare_created"],
            "tags": json.loads(tags) if tags else [],
            "body": body[:-1],
            "length": end - match.start(),
        })
    return entries


def check_copies(step: str, copies: list[str], produced: list[str]) -> None:
    """The `shared` copies one step appended must be exactly the chunks its
    private writes produced: the release shares every new chunk id, once."""
    if Counter(copies) != Counter(produced):
        missing = Counter(produced) - Counter(copies)
        extra = Counter(copies) - Counter(produced)
        raise GenerationError(
            f"{step}: the shared copies are not the chunks the private writes produced "
            f"(missing {len(missing)}, extra {len(extra)})"
        )


# ---- scenario definitions ---------------------------------------------------------

def private_body(prompt: str, agent: str, output: str, files: bool) -> str:
    """The content v0.1.x `share_result` writes, as `format_entry_block` strips it."""
    body = (
        f"## Task Result\n\n**Prompt:** {prompt}\n\n"
        f"**Agent:** {agent}\n\n**Output:**\n{output[:1000]}"
    )
    return (body + ("\n\n**Files changed:** calc.py" if files else "")).strip()


def board_body(item_id: str, task: str, output: str) -> str:
    return (
        f"## Board Item Result\n\n**Item:** {item_id}\n\n"
        f"**Task:** {task}\n\n**Output:**\n{output[:1000]}"
    )


def private(kind: str, agent: str, body: str) -> dict:
    return {"role": "private", "namespace": f"agent-runtime:{agent}",
            "tags": sorted([kind, agent, "task:*"]), "body": body}


def _normal_tags(tags: list[str]) -> list[str]:
    out = []
    for tag in tags:
        tag = re.sub(rf"^task:{_UUID}$", "task:*", tag)
        tag = re.sub(rf"^shared-from={_UUID}$", "shared-from=*", tag)
        out.append(tag)
    return sorted(out)


def _day_file_namespace(name: str, date: str) -> str | None:
    """v0.1.4's own file naming: `<date>.<namespace with ':' as '.'>.md`."""
    stem = name[len(date) + 1:-3] if name.startswith(date + ".") and name.endswith(".md") else None
    if not stem:
        return None
    head, _, rest = stem.partition(".")
    return f"{head}:{rest}" if rest else head


# ---- generation -------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    release = args.release_root.resolve()
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
    if version not in RELEASES:
        sys.exit(f"no schema-1 scenario definitions for syncmill {version} yet")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp).resolve() / "frozen"
        try:
            _generate(Path(tmp).resolve(), stage, python, syncmill, version,
                      metadata("memtomem"))
        except GenerationError as exc:
            sys.exit(str(exc))
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
        (fakebin / binary).write_text(f"#!{sys.executable}\n" + base.FAKE_AGENT, encoding="utf-8")
        (fakebin / binary).chmod(0o755)
    (fakebin / "python").symlink_to(sys.executable)
    root = state / "store"
    root.mkdir()
    db, mem = root / "mm.db", root / "mem"
    env = {
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
        "SYNCMILL_PROJECT__ID": PROJECT_ID,
        "SYNCMILL_RUNS__ROOT": str(state / "runs"),
        "SYNCMILL_MEMORY__DB_PATH": str(db),
        "SYNCMILL_MEMORY__MEMORY_DIR": str(mem),
        "SYNCMILL_BOARD__ENABLED": "true",
        "SYNCMILL_BOARD__DB_PATH": str(root / "board.db"),
        "SYNCMILL_ROOMS__ENABLED": "true",
        **base.GIT_ENV,
    }

    def git(*argv: str) -> None:
        subprocess.run(["git", *argv], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    for name, text in base.TREE.items():
        (repo / name).write_text(text, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "frozen tree")

    def day_files() -> dict[str, bytes]:
        if not mem.exists():
            return {}
        return {p.name: p.read_bytes() for p in sorted(mem.iterdir())
                if p.is_file() and not p.name.startswith(".") and p.name.endswith(".md")}

    def db_rows() -> dict[str, tuple]:
        if not db.exists():
            return {}
        return {r[0]: tuple(r[1:]) for r in base._query(
            db, "SELECT id, namespace, content FROM chunks ORDER BY rowid")}

    steps: list[dict] = []
    files_seen: dict[str, bytes] = {}
    counter = [0]

    def observe(scenario: str, label: str, output: str) -> dict:
        nonlocal files_seen
        now = day_files()
        rows = db_rows()
        live = {r[0] for r in rows.values()}
        # The release's board GC deletes a finished run's scratch day file
        # together with its rows; nothing else may vanish.
        gained = check_append_only(
            files_seen, now,
            may_vanish=lambda n: ".board-run." in n
            and _day_file_namespace(n, n[:10]) not in live)
        purged = sorted(set(files_seen) - set(now))
        # The board GC deletes a scratch namespace's rows; on v0.1.2 its text
        # stays in the shared `<date>.md`.
        previous = {r[0] for r in (steps[-1]["rows"] if steps else {}).values()}
        purged_ns = sorted({ns for ns in previous if ns.startswith("board-run:")} - live)
        appended = {}
        for name, data in gained.items():
            offset = len(files_seen.get(name, b""))
            text = data.decode("utf-8")
            entries = parse_entries(text)
            # `end` is the byte offset just after the entry in the whole file.
            cursor = offset
            for entry in entries:
                entry_text = text[:entry.pop("length")]
                text = text[len(entry_text):]
                cursor += len(entry_text.encode("utf-8"))
                entry["end"] = cursor
            appended[name] = entries
        files_seen = now
        step = {"scenario": scenario, "label": label, "appended": appended,
                "purged": purged, "purged_ns": purged_ns,
                "degraded": [line for line in output.splitlines() if "degraded" in line],
                "rows": rows}
        steps.append(step)
        return step

    def sm(scenario: str, label: str, *argv: str, plan: dict | None = None,
           ok: bool = True) -> str:
        calls = state / f"calls-{counter[0]}"
        counter[0] += 1
        calls.mkdir()
        done = subprocess.run(
            [str(syncmill), *argv], cwd=repo,
            env={**env, "FAKE_PLAN": json.dumps(plan or {}), "FAKE_CALLS": str(calls)},
            capture_output=True, text=True,
        )
        if ok and done.returncode != 0:
            raise GenerationError(f"{scenario}: {argv}: {done.stdout}{done.stderr}")
        if not ok and done.returncode == 0:
            raise GenerationError(f"{scenario}: {argv} was expected to fail: {done.stdout}")
        observe(scenario, label, done.stdout)
        return done.stdout

    def restore_tree() -> None:
        git("checkout", "--", ".")

    layout = LAYOUTS[version]

    def result(kind: str, agent: str, body: str) -> dict:
        """A shared result as this release writes it: v0.1.4 private-first with
        the kind and task tags, v0.1.2 straight into `shared` tagged only
        `task-result` and the agent."""
        if layout == "split":
            return private(kind, agent, body)
        return {"role": "direct", "namespace": "shared",
                "tags": sorted(["task-result", agent]), "body": body}

    def scratch_tags(kind: str) -> list[str]:
        return sorted([kind, "codex", "task:*"]) if layout == "split" else ["codex", "task-result"]

    def new_scratch_namespace(scenario: str) -> str:
        """The board-run namespace the last step's rows gained."""
        before = {r[0] for r in steps[-2]["rows"].values()} if len(steps) > 1 else set()
        gained = sorted({r[0] for r in steps[-1]["rows"].values()
                         if r[0].startswith("board-run:")} - before)
        if len(gained) != 1:
            raise GenerationError(f"{scenario}: expected one new scratch namespace, found {gained}")
        return gained[0]

    expected: dict[str, list[dict]] = {}   # step index -> named entries
    facts: dict[str, dict] = {}

    def expect(entries: list[dict], **scenario_facts) -> None:
        expected[len(steps) - 1] = entries
        facts.setdefault(steps[-1]["scenario"], {}).update(scenario_facts)

    reply = base.default_reply

    prompt = f"{ANCHOR} tokroute fix add in calc.py"
    sm("route", "run", "run", prompt, "-a", "codex",
       plan={"codex": {"token": "tokroute", "edit": ["1"]}})
    expect([result("task-result", "codex",
                    private_body(prompt, "codex", reply("codex", 1, "tokroute"), True))],
           prompt=prompt, token="tokroute")

    prompt = f"{ANCHOR} tokpipeline fix add in calc.py"
    sm("pipeline", "run", "run", prompt, "-s", "pipeline",
       plan={"codex": {"token": "tokpipeline", "edit": ["1"]},
             "kimi": {"token": "tokpipeline", "edit": ["1"]}})
    expect([result("pipeline-stage", "codex",
                    private_body(prompt, "codex", reply("codex", 1, "tokpipeline"), False)),
            result("task-result", "kimi-code",
                    private_body(prompt, "kimi-code", reply("kimi", 1, "tokpipeline"), True))],
           prompt=prompt, token="tokpipeline")

    prompt = f"{ANCHOR} tokcompete fix add in calc.py"
    sm("compete", "run", "run", prompt, "-s", "compete",
       plan={"codex": {"token": "tokcompete", "edit": ["1"]},
             "kimi": {"token": "tokcompete", "edit": ["1"]}})
    expect([result("task-result", "codex",
                    private_body(prompt, "codex", reply("codex", 1, "tokcompete"), True))],
           prompt=prompt, token="tokcompete")

    prompt = f"{ANCHOR} tokcouncil fix add in calc.py"
    sm("council", "run", "run", prompt, "-s", "council",
       plan={"codex": {"token": "tokcouncil", "edit": ["4"]},
             "kimi": {"token": "tokcouncil", "edit": ["4"]}})
    council = []
    for agent, binary in (("codex", "codex"), ("kimi-code", "kimi")):
        council.append(result("council-proposal", agent, private_body(
            prompt, agent, reply(binary, 1, "tokcouncil"), False)))
        for call in (2, 3):
            council.append(result("council-critique", agent, private_body(
                prompt, agent, reply(binary, call, "tokcouncil"), False)))
    council.append(result("task-result", "codex", private_body(
        prompt, "codex", reply("codex", 4, "tokcouncil"), True)))
    expect(council, prompt=prompt, token="tokcouncil")

    prompt = f"{ANCHOR} tokdecompose fix add in calc.py"
    sm("decompose", "run", "run", prompt, "-s", "decompose",
       plan={"codex": {"token": "tokdecompose", "edit": ["3"],
                       "replies": {"1": json.dumps(base.SUBTASKS)}},
             "kimi": {"token": "tokdecompose", "edit": ["2"]}})
    # v0.1.2 shares no plan row.
    plan_row = [result("decompose-plan", "codex", private_body(
        prompt, "codex", json.dumps(base.SUBTASKS, ensure_ascii=False), False))]
    expect([*(plan_row if layout == "split" else []),
            result("decompose-subtask", "codex", private_body(
                prompt, "codex", reply("codex", 2, "tokdecompose"), False)),
            result("decompose-subtask", "kimi-code", private_body(
                prompt, "kimi-code", reply("kimi", 1, "tokdecompose"), False)),
            result("task-result", "codex", private_body(
                prompt, "codex", reply("codex", 3, "tokdecompose"), True))],
           prompt=prompt, token="tokdecompose", subtasks=base.SUBTASKS)

    # The reply's outer whitespace is dropped before the 1000-character cut; the
    # day file keeps the CRLF pair (the row, read in text mode, has LF).
    prompt = f"{ANCHOR} tokunicode say something long"
    sm("unicode", "run", "run", prompt, "-a", "codex",
       plan={"codex": {"token": "tokunicode", "replies": {"1": base.UNICODE_REPLY}}})
    expect([result("task-result", "codex", private_body(
                prompt, "codex", base.UNICODE_REPLY.strip()[:1000], False))],
           prompt=prompt, token="tokunicode", reply=base.UNICODE_REPLY)

    prompt = f"{ANCHOR} tokheadings review calc.py"
    sm("headings", "run", "run", prompt, "-a", "codex",
       plan={"codex": {"token": "tokheadings", "replies": {"1": base.HEADINGS_REPLY}}})
    expect([result("task-result", "codex", private_body(
                prompt, "codex", base.HEADINGS_REPLY, False))],
           prompt=prompt, token="tokheadings", reply=base.HEADINGS_REPLY)

    sm("longprompt", "run", "run", base.LONG_PROMPT, "-a", "codex",
       plan={"codex": {"token": "toklong"}})
    expect([result("task-result", "codex", private_body(
                base.LONG_PROMPT, "codex", reply("codex", 1, "toklong"), False))],
           prompt=base.LONG_PROMPT, token="toklong")

    prompt = f"{ANCHOR} tokfailed fix add in calc.py"
    sm("failed", "run", "run", prompt, "-a", "codex",
       plan={"codex": {"token": "tokfailed", "fail": ["1"]}}, ok=False)
    expect([], prompt=prompt, token="tokfailed")

    task_text = f"{ANCHOR} tokboard fix add in calc.py"
    line = sm("board-done", "add", "board", "add", task_text, "--goal", "frozen")
    expect([])
    item_id = line.split("added item ")[1].split()[0]
    sm("board-done", "run", "board", "run", "--id", item_id,
       plan={"codex": {"token": "tokboard", "edit": ["1"]}})
    run_ns = new_scratch_namespace("board-done")
    # The scratch row's prompt is the rendered board item, the release's own text;
    # only its frame is named.
    expect([{"role": "scratch", "namespace": run_ns,
             "tags": scratch_tags("task-result"), "body": None,
             "frame": ["## Task Result\n\n**Prompt:** ", task_text,
                       f"**Agent:** codex\n\n**Output:**\n{reply('codex', 1, 'tokboard')}"
                       "\n\n**Files changed:** calc.py"]},
            {"role": "board-result", "namespace": "shared",
             "tags": [f"board-result:{item_id}"],
             "body": board_body(item_id, task_text, reply("codex", 1, "tokboard"))}],
           task=task_text, token="tokboard", item_id=item_id, run_id=run_ns.split(":", 1)[1])
    restore_tree()

    prompt = f"{ANCHOR} tokroom fix add in calc.py"
    plan = {"codex": {"token": "tokroom", "edit": ["1"]},
            "kimi": {"token": "tokroom", "edit": ["1"]}}
    room_id = sm("room-apply", "create", "room", "create", prompt,
                 "-a", "codex", "-a", "kimi-code").split()[0]
    expect([])
    sm("room-apply", "round", "room", "round", room_id, plan=plan)
    expect([])
    rooms_db = state / "runs" / PROJECT_ID / "rooms" / "rooms.sqlite3"
    candidates = base._query(
        rooms_db,
        "SELECT id, member_id FROM candidates WHERE room_id = ? AND eligible = 1 "
        "ORDER BY member_id", (room_id,),
    )
    if not candidates or candidates[0][1] != "codex":
        raise GenerationError(f"room-apply: no eligible codex candidate: {candidates}")
    sm("room-apply", "select", "room", "select", room_id, candidates[0][0])
    expect([])
    # Before v0.2.0 `room apply` shares nothing.
    sm("room-apply", "apply", "room", "apply", room_id)
    expect([], prompt=prompt, token="tokroom", room_id=room_id)
    restore_tree()

    # ---- last: a board run SIGKILLed during its second stage ------------------------
    task_text = f"{ANCHOR} tokkilled fix add in calc.py"
    line = sm("board-killed", "add", "board", "add", task_text, "--goal", "frozen")
    expect([])
    item_id = line.split("added item ")[1].split()[0]
    for marker in started.iterdir():
        marker.unlink()
    calls = state / "calls-killed"
    calls.mkdir()
    plan = {"codex": {"token": "tokkilled", "edit": ["1"]},
            "kimi": {"token": "tokkilled", "hang": ["1"]}}
    proc = subprocess.Popen(
        [str(syncmill), "board", "run", "--id", item_id, "-s", "pipeline"],
        cwd=repo, env={**env, "FAKE_PLAN": json.dumps(plan), "FAKE_CALLS": str(calls)},
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
            kill_group()
            try:
                output = proc.communicate(timeout=30)[0]
            except subprocess.TimeoutExpired:
                output = "(no output: the pipe is still held open)"
            raise GenerationError(f"board-killed: the second stage never started: {output}")
        time.sleep(0.05)
    agent_pid = int((started / "kimi").read_text())
    kill_group()
    proc.wait()
    try:
        os.kill(agent_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.stdout.close()
    before_kill = sorted(steps[-1]["rows"])
    observe("board-killed", "run (killed)", "")
    run_ns = new_scratch_namespace("board-killed")
    # v0.1.2's pipeline shares every stage as a `task-result`.
    expect([{"role": "scratch", "namespace": run_ns,
             "tags": scratch_tags("pipeline-stage"), "body": None,
             "frame": ["## Task Result\n\n**Prompt:** ", task_text,
                       f"**Agent:** codex\n\n**Output:**\n{reply('codex', 1, 'tokkilled')}"]}],
           task=task_text, token="tokkilled", item_id=item_id)
    (lease,) = base._query(
        root / "board.db",
        "SELECT state, lease_run_id, claimed_at FROM work_items WHERE id = ?", (item_id,),
    )
    if lease[0] != "running" or f"board-run:{lease[1]}" != run_ns:
        raise GenerationError(f"board-killed: no live lease for the scratch run: {lease}")
    facts["board-killed"].update(run_id=lease[1], claimed_at=lease[2],
                                 rows_before=before_kill)
    # The kill left the whole step in the WAL only: the main file alone is the
    # store exactly as it stood before the killed run started.
    in_main = sorted(r[0] for r in base._query(db, "SELECT id FROM chunks", wal=False))
    if in_main != before_kill:
        raise GenerationError("board-killed: the main file is not the store before the kill")
    restore_tree()

    # ---- the store, copied as the kill left it ---------------------------------------
    out.mkdir(parents=True)
    shutil.copytree(root, out / "store")
    if not (out / "store" / "mm.db-wal").is_file():
        raise GenerationError("the killed store has no WAL to freeze")
    frozen_mem = out / "store" / "mem"
    date = sorted({n[:10] for n in files_seen})
    if len(date) != 1:
        raise GenerationError(f"the day files span more than one UTC date ({date}); generate again")
    date = date[0]

    # ---- entries against the definitions ---------------------------------------------
    history: dict[str, list[dict]] = {}
    for index, step in enumerate(steps):
        named = list(expected.get(index, []))
        copies = []
        for name, entries in step["appended"].items():
            # v0.1.4 names the namespace in the file name; v0.1.2's one file
            # holds every namespace, so an entry's is the definition's.
            file_ns = _day_file_namespace(name, date)
            if (file_ns is None) != (layout == "mixed"):
                raise GenerationError(f"{name}: not a {layout} day file")
            for entry in entries:
                entry["namespace"] = file_ns
                entry["step"] = index
                entry["indexed"] = True
                history.setdefault(name, []).append(entry)
                if file_ns == "shared" and entry["title"] == f"Entry {entry['created']}" \
                        and _normal_tags(entry["tags"]) == ["shared-from=*"]:
                    entry["role"] = "copy"
                    copies.append(entry)
                    continue
                match = next((n for n in named if file_ns in (None, n["namespace"])
                              and n["tags"] == _normal_tags(entry["tags"])
                              and (n["body"] == entry["body"] if n["body"] is not None
                                   else _framed(entry["body"], n["frame"]))), None)
                if match is None:
                    raise GenerationError(
                        f"{step['scenario']} ({step['label']}): an entry no definition names: "
                        f"{name} {json.dumps(entry, ensure_ascii=False)[:600]}")
                named.remove(match)
                entry["role"] = match["role"]
                entry["namespace"] = match["namespace"]
                if entry["title"] is not None:
                    raise GenerationError(f"{step['scenario']}: a heading-led entry got a title")
        if named:
            raise GenerationError(
                f"{step['scenario']} ({step['label']}): named entries never written: "
                f"{json.dumps(named, ensure_ascii=False)[:600]}")
        if any(_DEGRADED_BOARD in line for line in step["degraded"]):
            for entry in (e for es in step["appended"].values() for e in es):
                if entry["role"] == "board-result":
                    entry["indexed"] = False
        unexplained = [line for line in step["degraded"] if _DEGRADED_BOARD not in line]
        if unexplained:
            raise GenerationError(f"{step['scenario']}: memory degraded: {unexplained}")
        step["copies"] = copies

    # ---- the release replays the history ---------------------------------------------
    gone = {n for s in steps for n in s["purged"]}
    spec = {}
    for name, entries in history.items():
        if name in gone:
            continue
        events = []
        for index, step in enumerate(steps):
            # A run's GC comes before its writes.
            events += [{"purge": ns} for ns in step["purged_ns"]]
            events += [{"end": e["end"], "namespace": e["namespace"], "indexed": e["indexed"]}
                       for e in entries if e["step"] == index]
        spec[name] = {"path": str(frozen_mem / name), "events": events}
    spec_path = box / "oracle.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    done = subprocess.run([str(python), "-c", ORACLE, str(spec_path)], env=env,
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise GenerationError(f"oracle: {done.stdout}{done.stderr}")
    oracle = json.loads(done.stdout.strip().splitlines()[-1])
    for name, entries in history.items():
        for entry, produced in zip(entries, oracle.get(name, {}).get(
                "produced", [None] * len(entries))):
            entry["produced"] = produced

    stored = base._query(
        out / "store" / "mm.db",
        "SELECT id, namespace, tags, content, start_line, end_line, source_file "
        "FROM chunks ORDER BY rowid")
    rows = [{"id": r[0], "namespace": r[1], "tags": sorted(json.loads(r[2])),
             "content": r[3], "start_line": r[4], "end_line": r[5],
             "source": Path(r[6]).name} for r in stored]
    for name in set(history) - gone:
        got = sorted((r["start_line"], r["end_line"], r["content"], r["tags"], r["namespace"])
                     for r in rows if r["source"] == name)
        want = sorted(tuple(c) for c in oracle[name]["final"])
        if [list(g) for g in got] != [list(w) for w in want]:
            raise GenerationError(
                f"{name}: the stored rows are not what the release derives from the file "
                f"({len(got)} stored, {len(want)} derived)")
    if {r["source"] for r in rows} - (set(history) - gone):
        raise GenerationError("a stored row names a file no step wrote")

    # Copies: per step, exactly the chunks that step's private writes produced, and
    # each provenance id, where the step's snapshot saw it, a private row with that
    # content.
    unverified = []
    for index, step in enumerate(steps):
        produced = [c for es in step["appended"].values() for e in es
                    if e["role"] == "private" for c in (e["produced"] or [])]
        check_copies(f"{step['scenario']} ({step['label']})",
                     [c["body"] for c in step["copies"]], produced)
        for copy in step["copies"]:
            (source_id,) = [t.split("=", 1)[1] for t in copy["tags"]]
            seen = step["rows"].get(source_id)
            if seen is None:
                unverified.append(source_id)
            elif not seen[0].startswith("agent-runtime:") or seen[1] != copy["body"]:
                raise GenerationError(f"{step['scenario']}: shared-from={source_id} names "
                                      "another row's content")

    # ---- findings, derived from the stored rows --------------------------------------
    ids = {r["id"] for r in rows}
    dangling = sorted({t.split("=", 1)[1] for r in rows for t in r["tags"]
                       if t.startswith("shared-from=") and t.split("=", 1)[1] not in ids})
    unindexed = [{"file": name, "end": e["end"], "role": e["role"]}
                 for name, es in history.items() for e in es if not e["indexed"]]
    # A row whose text comes from an entry of another namespace (v0.1.2: a later
    # write's namespace stamped on text a purge or a merge handed it).
    mislabeled = []
    for row in rows:
        spans = entry_lines(frozen_mem / row["source"], history[row["source"]])
        owners = {ns for (first, last), ns in spans
                  if not (row["end_line"] < first or row["start_line"] > last)}
        if owners != {row["namespace"]}:
            mislabeled.append({"id": row["id"], "namespace": row["namespace"],
                               "entry_namespaces": sorted(owners)})

    # ---- the release's own read, in place --------------------------------------------
    # On the store the release wrote, never on the frozen copy: opening it applies
    # and removes the WAL the kill left.
    done = subprocess.run(
        [str(python), "-c", base.RELEASE_READ, str(db), str(mem), ANCHOR,
         json.dumps(base.KINDS)],
        env=env, capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise GenerationError(f"release read: {done.stdout}{done.stderr}")
    (out / "release-read.json").write_text(
        json.dumps({"store": json.loads(done.stdout.strip().splitlines()[-1])},
                   ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    index = {
        "release": version,
        "memtomem": memtomem,
        "anchor": ANCHOR,
        "generating_root": str(box),
        "store": {"db": "store/mm.db", "mem": "store/mem", "board": "store/board.db",
                  "root": str(root)},
        "date": date,
        "scenarios": facts,
        "steps": [{"scenario": s["scenario"], "label": s["label"], "degraded": s["degraded"],
                   "purged": s["purged"], "purged_namespaces": s["purged_ns"]} for s in steps],
        "entries": {name: [{k: e[k] for k in ("step", "end", "role", "namespace", "title",
                                               "tags", "body", "indexed", "produced")}
                           for e in es] for name, es in sorted(history.items())},
        "rows": rows,
        "findings": {"dangling_shared_from": dangling, "unindexed_entries": unindexed,
                     "unverified_shared_from": sorted(set(unverified)),
                     "mislabeled_rows": mislabeled},
        "layout": layout,
    }
    index["files"] = {str(p.relative_to(out)): base._sha256(p)
                      for p in sorted(out.rglob("*")) if p.is_file()}
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def entry_lines(path: Path, entries: list[dict]) -> list[tuple[tuple[int, int], str]]:
    """Each recorded entry's 1-based line span in its file, with its namespace:
    from the line after the block's leading newline to its last line."""
    data = path.read_bytes()
    out, start = [], 0
    for entry in entries:
        first = data[:start].count(b"\n") + 2
        last = data[:entry["end"]].count(b"\n")
        out.append(((first, last), entry["namespace"]))
        start = entry["end"]
    return out


def _framed(body: str, frame: list[str]) -> bool:
    head, middle, tail = frame
    return body.startswith(head) and middle in body and body.endswith(tail)


if __name__ == "__main__":
    main()
