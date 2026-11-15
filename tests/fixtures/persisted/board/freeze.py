"""Produce the frozen work-board databases under this directory (#271).

Run once per release, with THAT release's interpreter and source, never the
current one. The output directory must not exist; frozen fixtures are never
regenerated.

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev)
    python tests/fixtures/persisted/board/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --out tests/fixtures/persisted/board/v0.3.0

Each scenario is one board database. The released CLI makes every row:
`board add`, `board run` (with a fake `codex` whose behaviour each run sets),
`board update`, `board import-review-candidates`, `board decide` and, from
v0.2.0, `board compact`. Three states need a process death no CLI exposes:
- a run SIGKILLed while its agent works (a live lease, and the WAL it leaves);
- a run interrupted with SIGINT (the release's cancel path);
- a run whose orchestration raises, from a `.pth` hook this script puts in the
  release venv for the run and removes afterwards.

Generation fails unless every property the tests rely on is present (see
`_require`), so a release that writes something else cannot silently narrow
the coverage. The database is copied byte for byte as soon as its scenario
finishes. Only then does the release's own interpreter read a scratch copy and
record what it makes of every row (`Board.get`, `transitions`,
`active_decisions`, the decision rows and `stats` at two fixed clocks): that is
the expected interpretation the tests hold HEAD's readers to.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TREE = {"calc.py": "def add(a, b):\n    return a - b\n"}
GATE = ["python -c \"from calc import add; assert add(2, 3) == 5\""]
MISSING_DEP = "00000000-0000-4000-8000-000000000000"
STALL_SECONDS = 3600

# FAKE_PLAN = {"do": fix | wrong | none | fail | hang, "block": {...} | absent}.
# fix makes the gate pass, wrong and none make it fail, fail exits 1, hang waits
# to be killed. A "block" is appended to the reply as a syncmill-board block.
FAKE_AGENT = r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["login", "status"] or args[:1] in (["--version"], ["--help"]):
    print("codex (fake) - logged in"); sys.exit(0)
plan = json.loads(os.environ.get("FAKE_PLAN") or "{}")
do = plan.get("do", "fix")
if do == "hang":
    marker = Path(os.environ["FAKE_STARTED"]) / "codex"
    marker.with_suffix(".tmp").write_text(str(os.getpid()))
    os.replace(marker.with_suffix(".tmp"), marker)
    time.sleep(120); sys.exit(1)
if do == "fail":
    sys.stderr.write("codex (fake): refused\n"); sys.exit(1)
body = {"fix": "a + b", "wrong": "a * b"}.get(do)
if body:
    Path("calc.py").write_text("def add(a, b):\n    return " + body + "\n", encoding="utf-8")
message = "codex (fake) " + do + "."
if plan.get("block"):
    message += "\n```syncmill-board\n" + json.dumps(plan["block"]) + "\n```\n"
for event in [
    {"type": "thread.started", "thread_id": "fake-codex-thread"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]:
    print(json.dumps(event))
'''

# Loaded at interpreter start by the .pth below, in every process of the
# release venv. With HOME/crash-at present the orchestration raises inside
# run_item's guard (Supervisor.run), the way a bug in a strategy would.
HOOK_MODULE = "_syncmill_freeze_board_hook"
CRASH_HOOK = '''\
import os
from pathlib import Path


def _install():
    flag = Path(os.environ.get("HOME", "/nonexistent")) / "crash-at"
    if not flag.is_file():
        return
    from syncmill.supervisor import Supervisor

    def run(self, *args, **kwargs):
        raise RuntimeError("frozen: injected orchestration failure")
    Supervisor.run = run


_install()
'''

# Runs inside the RELEASED interpreter on a scratch copy: what the release
# itself reads from every row. argv: db, frozen_at, decision goals (JSON list),
# stall seconds, and the item whose aging boundary is probed ("" for none).
RELEASE_READ = '''\
import dataclasses, enum, json, math, sqlite3, sys
from syncmill.board import Board, _row_to_decision
db, frozen_at, goals = sys.argv[1], float(sys.argv[2]), json.loads(sys.argv[3])
stall, boundary_item = int(sys.argv[4]), sys.argv[5]


def plain(value):
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


board = Board(db, now=lambda: frozen_at, stall_seconds=stall)
items = board.list()
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
read = {
    "items": {it.id: plain(board.get(it.id)) for it in items},
    "order": [it.id for it in items],
    "transitions": {it.id: board.transitions(it.id) for it in items},
    "active_decisions": {
        "*": [plain(d) for d in board.active_decisions()],
        **{g: [plain(d) for d in board.active_decisions(goal=g)] for g in goals},
    },
    "decision_rows": [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY rowid")],
    # Every decision row through the release's own mapper, retired ones too:
    # no listing returns a superseded decision, so its superseded_by and
    # superseded_at reach a reader only through the mapper itself.
    "decisions_mapped": [
        plain(_row_to_decision(r))
        for r in conn.execute("SELECT * FROM decisions ORDER BY rowid")
    ],
    "aggregates": {
        table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('transition_rollups', 'transition_counters') ORDER BY name"
        ).fetchall()
    },
    "stats": {
        "fresh": plain(board.stats(now=frozen_at)),
        "aged": plain(board.stats(now=frozen_at + stall + 1)),
    },
}
if boundary_item:
    # The clocks around an item's aging boundary (stats ages an item once
    # updated_at <= now - stall): the last one that does not age it, the first
    # one that does, and the one after.
    updated = board.get(boundary_item).updated_at
    first = updated + stall
    while first - stall < updated:
        first = math.nextafter(first, math.inf)
    while math.nextafter(first, -math.inf) - stall >= updated:
        first = math.nextafter(first, -math.inf)
    times = {"before": math.nextafter(first, -math.inf), "at": first,
             "after": math.nextafter(first, math.inf)}
    read["boundary"] = {
        "item": boundary_item,
        "times": times,
        "stats": {k: plain(board.stats(now=t)) for k, t in times.items()},
    }
conn.close()
board.close()
print(json.dumps(read, ensure_ascii=False, sort_keys=True))
'''

GIT_ENV = {
    "GIT_AUTHOR_NAME": "frozen", "GIT_AUTHOR_EMAIL": "frozen@example.invalid",
    "GIT_COMMITTER_NAME": "frozen", "GIT_COMMITTER_EMAIL": "frozen@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _site_packages(python: Path) -> Path:
    return Path(subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    release = args.release_root.resolve()
    out = args.out.resolve()
    if out.exists():
        sys.exit(f"{out} exists; frozen fixtures are never regenerated")
    python = release / ".venv" / "bin" / "python"
    syncmill = release / ".venv" / "bin" / "syncmill"
    version = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('syncmill'))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    has_compact = version != "0.1.2"
    site = _site_packages(python)
    hook_files = [site / f"{HOOK_MODULE}.py", site / f"zz{HOOK_MODULE}.pth"]
    if any(p.exists() for p in hook_files):
        sys.exit(f"hook files already present in {site}; remove them first")
    try:
        hook_files[0].write_text(CRASH_HOOK, encoding="utf-8")
        hook_files[1].write_text(f"import {HOOK_MODULE}\n", encoding="utf-8")
        _generate(release, out, python, syncmill, version, has_compact)
    finally:
        for p in hook_files:
            p.unlink(missing_ok=True)


def _generate(release: Path, out: Path, python: Path, syncmill: Path,
              version: str, has_compact: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        box = Path(tmp).resolve()
        repo, home, state, fakebin, started = (
            box / n for n in ("repo", "home", "state", "fakebin", "started")
        )
        for folder in (repo, home, state, fakebin, started):
            folder.mkdir()
        (fakebin / "codex").write_text(f"#!{sys.executable}\n" + FAKE_AGENT, encoding="utf-8")
        (fakebin / "codex").chmod(0o755)
        (fakebin / "python").symlink_to(sys.executable)
        base_env = {
            "HOME": str(home),
            "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
            "TMPDIR": str(box),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_STARTED": str(started),
            "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex"]),
            "SYNCMILL_CLAUDE__ENABLED": "false",
            "SYNCMILL_BOARD__ENABLED": "true",
            "SYNCMILL_BOARD__STALL_SECONDS": str(STALL_SECONDS),
            "SYNCMILL_GATE__COMMANDS": json.dumps(GATE),
            "SYNCMILL_RUNS__ROOT": str(state / "runs"),
            "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
            "SYNCMILL_MEMORY__DB_PATH": str(state / "memory" / "memtomem.db"),
            "SYNCMILL_MEMORY__MEMORY_DIR": str(state / "memory" / "files"),
            "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
            "SYNCMILL_TUI__STATE_DIR": str(state / "tui"),
            "SYNCMILL_PROJECT__ID": "frozen-board",
            **GIT_ENV,
        }

        def git(*argv: str) -> None:
            subprocess.run(["git", *argv], cwd=repo, env=base_env, check=True,
                           capture_output=True)

        git("init", "-q", "-b", "main")
        for name, text in TREE.items():
            (repo / name).write_text(text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "frozen tree")

        out.mkdir(parents=True)
        report = release / "tests" / "fixtures" / "tracegraph" / "v1.json"
        bindings: dict = {
            "release": version,
            "box_root": str(box),
            "stall_seconds": STALL_SECONDS,
            "missing_dependency": MISSING_DEP,
            "tracegraph_report": "tests/fixtures/tracegraph/v1.json",
            "scenarios": {},
        }

        class Scenario:
            def __init__(self, name: str):
                self.name = name
                self.dir = state / name
                self.dir.mkdir()
                self.env = {**base_env, "SYNCMILL_BOARD__DB_PATH": str(self.dir / "board.db")}
                self.items: dict[str, str] = {}
                self.decisions: dict[str, str] = {}

            def sm(self, *argv: str, ok: bool = True, plan: dict | None = None,
                   env: dict | None = None) -> str:
                done = subprocess.run(
                    [str(syncmill), *argv], cwd=repo,
                    env={**self.env, "FAKE_PLAN": json.dumps(plan or {}), **(env or {})},
                    capture_output=True, text=True,
                )
                if ok and done.returncode != 0:
                    sys.exit(f"{self.name}: {argv}: {done.stdout}{done.stderr}")
                if not ok and done.returncode == 0:
                    sys.exit(f"{self.name}: {argv} was expected to fail: {done.stdout}")
                return done.stdout

            def add(self, role: str, task: str, *flags: str) -> str:
                line = self.sm("board", "add", task, *flags)
                self.items[role] = line.split("added item ")[1].split()[0]
                return self.items[role]

            def run(self, role: str, plan: dict, *flags: str, ok: bool = True,
                    env: dict | None = None) -> None:
                self.sm("board", "run", "--id", self.items[role], *flags,
                        ok=ok, plan=plan, env=env)

            def update(self, role: str, *flags: str, ok: bool = True) -> None:
                self.sm("board", "update", self.items[role], *flags, ok=ok)

            def decide(self, role: str, text: str, *flags: str) -> None:
                line = self.sm("board", "decide", text, *flags)
                self.decisions[role] = line.split("recorded decision ")[1].split()[0]

            def hang(self, role: str):
                """Start a run whose agent hangs; return it once the agent is up."""
                for p in started.iterdir():
                    p.unlink()
                proc = subprocess.Popen(
                    [str(syncmill), "board", "run", "--id", self.items[role]],
                    cwd=repo, env={**self.env, "FAKE_PLAN": json.dumps({"do": "hang"})},
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 60
                while not (started / "codex").exists():
                    if proc.poll() is not None or time.monotonic() > deadline:
                        sys.exit(f"{self.name}: hanging agent never started: "
                                 f"{proc.communicate()[0] if proc.poll() is not None else ''}")
                    time.sleep(0.1)
                return proc, int((started / "codex").read_text())

            def kill(self, role: str) -> None:
                proc, agent = self.hang(role)
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                _kill_quietly(agent)

            def interrupt(self, role: str) -> None:
                proc, agent = self.hang(role)
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    sys.exit(f"{self.name}: SIGINT did not end the run")
                _kill_quietly(agent)

            def crash(self, role: str) -> None:
                flag = home / "crash-at"
                flag.write_text("run", encoding="utf-8")
                try:
                    self.run(role, {"do": "fix"}, ok=False)
                finally:
                    flag.unlink()

            def reclaim(self, role: str) -> None:
                """Kill a claim, then let the release's next run reclaim and finish it."""
                self.kill(role)
                time.sleep(2)
                self.run(role, {"do": "fix"},
                         env={"SYNCMILL_BOARD__LEASE_TIMEOUT_SECONDS": "1"})

        def freeze(sc: Scenario, *, killed: bool, goals: list[str],
                   boundary: str = "") -> dict:
            wal = sorted(p.name for p in sc.dir.glob("*-wal"))
            if bool(wal) != killed:
                sys.exit(f"{sc.name}: expected {'a' if killed else 'no'} WAL, found {wal}")
            shutil.copytree(sc.dir, out / sc.name)
            read = release_read(sc, goals, sc.items.get(boundary, ""))
            # Rows no `board add` named (the imported review candidates) get a
            # role from their position in the release's own FIFO listing.
            unnamed = [i for i in read["order"] if i not in sc.items.values()]
            for n, item_id in enumerate(unnamed, 1):
                sc.items[f"imported-{n}"] = item_id
            entry = {
                "items": sc.items,
                "decisions": sc.decisions,
                "frozen_at": read.pop("frozen_at"),
                "files": sorted(p.name for p in (out / sc.name).iterdir()),
                "release_read": read,
            }
            bindings["scenarios"][sc.name] = entry
            return entry

        def release_read(sc: Scenario, goals: list[str], boundary_item: str = "") -> dict:
            # A scratch copy with its sidecars, read with a normal connection:
            # rows still in a killed process's WAL are seen, and neither the
            # release nor this script ever opens the frozen files.
            scratch = box / "read" / sc.name
            shutil.rmtree(scratch, ignore_errors=True)
            shutil.copytree(sc.dir, scratch)
            db = scratch / "board.db"
            _no_machine_values(db, box, home)
            with sqlite3.connect(db) as conn:
                frozen_at = conn.execute(
                    "SELECT MAX(m) FROM (SELECT MAX(updated_at) AS m FROM work_items "
                    "UNION ALL SELECT MAX(at) FROM transitions "
                    "UNION ALL SELECT MAX(created_at) FROM decisions)"
                ).fetchone()[0]
            conn.close()
            done = subprocess.run(
                [str(python), "-c", RELEASE_READ, str(db), repr(frozen_at),
                 json.dumps(goals), str(STALL_SECONDS), boundary_item],
                env={**base_env, "SYNCMILL_BOARD__DB_PATH": str(db)},
                capture_output=True, text=True,
            )
            if done.returncode != 0:
                sys.exit(f"{sc.name}: release read failed: {done.stderr}")
            read = json.loads(done.stdout)
            read["frozen_at"] = frozen_at
            return read

        # ── lifecycle: every row shape the released CLI writes ───────────
        sc = Scenario("lifecycle")
        sc.add("passed", "fix add")
        sc.run("passed", {"do": "fix"})
        sc.add("verified", "fix add and finish", "--goal", "backend")
        sc.run("verified", {"do": "fix", "block": {"state": "done"}})
        sc.add("handed-off", "덧셈을 고친다", "--goal", "backend")
        sc.run("handed-off", {"do": "fix", "block": {
            "state": "review", "handoff": "검토: 부호를 확인할 것",
            "outputs": ["notes/plan.md"]}})
        sc.add("route-gate-failed", "fix add badly")
        sc.run("route-gate-failed", {"do": "wrong"}, ok=False)
        sc.add("pipeline-gate-failed", "fix add badly in a pipeline")
        sc.run("pipeline-gate-failed", {"do": "wrong"}, "--strategy", "pipeline", ok=False)
        sc.add("agent-failed", "ask a refusing agent")
        sc.run("agent-failed", {"do": "fail"}, ok=False)
        sc.add("reclaimed", "fix add after a crash")
        sc.reclaim("reclaimed")
        sc.add("evidence-done", "verified by CI")
        sc.run("evidence-done", {"do": "fix"})
        sc.update("evidence-done", "--state", "done", "--evidence", "CI ✓ run 42 — grün")
        sc.add("signed-off", "verified by a human")
        sc.run("signed-off", {"do": "fix"})
        sc.update("signed-off", "--state", "done", "--sign-off")
        sc.add("bare-done-refused", "no verification yet")
        sc.update("bare-done-refused", "--state", "done", ok=False)
        sc.add("human-blocked", "waits on ops")
        sc.update("human-blocked", "--state", "blocked", "--note", "waiting on ops")
        sc.add("annotated", "carries a reference")
        sc.update("annotated", "--evidence", "https://example.invalid/log/7")
        sc.add("human-required", "needs a person", "--human-required")
        sc.add("human-cleared", "needed a person", "--human-required")
        sc.update("human-cleared", "--clear-human-required")
        sc.add("needs-done", "after the verified item", "--depends-on", sc.items["verified"])
        sc.add("needs-planned", "after a planned item", "--depends-on", sc.items["annotated"])
        sc.add("needs-missing", "after an item that does not exist", "--depends-on", MISSING_DEP)
        sc.add("needs-two", "after two items",
               "--depends-on", sc.items["verified"], "--depends-on", sc.items["annotated"])
        line = sc.sm("board", "import-review-candidates", str(report), "--goal", "review")
        if "imported" not in line:
            sys.exit(f"lifecycle: import printed {line!r}")
        sc.decide("global", "Use uv for every Python command.")
        sc.decide("scoped", "백엔드는 SQLite를 유지한다.", "--scope", "backend")
        sc.decide("superseded", "Tabs, not spaces.")
        sc.decide("replacement", "Spaces, not tabs.", "--supersedes", sc.decisions["superseded"])
        sc.decide("other-scope", "Ship the docs with the code.", "--scope", "docs")
        entry = freeze(sc, killed=False, goals=["backend", "docs", "review", "nothing"],
                       boundary="human-blocked")
        _require_lifecycle(entry)

        # ── running: a run killed while its agent works ──────────────────
        sc = Scenario("running")
        sc.add("waiting", "still planned")
        sc.add("killed", "killed mid-run", "--goal", "backend")
        sc.kill("killed")
        entry = freeze(sc, killed=True, goals=["backend"])
        _require(entry, "killed", state="running", lease=True)

        # ── crash-blocked: the release's cancel and crash paths ──────────
        sc = Scenario("crash-blocked")
        sc.add("interrupted", "interrupted by the operator")
        sc.interrupt("interrupted")
        sc.add("crashed", "orchestration raises")
        sc.crash("crashed")
        entry = freeze(sc, killed=False, goals=[], boundary="crashed")
        _require(entry, "interrupted", state="blocked", human=True,
                 note="orchestration cancelled by operator (SIGINT)")
        _require(entry, "crashed", state="blocked", human=True,
                 note="orchestration crashed: RuntimeError: frozen: injected orchestration failure")

        # ── compacted: aggregates folded out of a partly trimmed audit ────
        # `rolled` is reclaimed and re-claimed entirely before the cutoff, so
        # its two claims and its reclaim live only in the rollups. `mixed` is
        # claimed before the cutoff and reclaimed and re-claimed after it, so
        # each of its counts needs the live rows AND the rollups.
        if has_compact:
            sc = Scenario("compacted")
            sc.add("rolled", "fix add after a crash")
            sc.reclaim("rolled")
            sc.add("mixed", "fix add after a later crash")
            sc.kill("mixed")
            time.sleep(6)
            sc.run("mixed", {"do": "fix"},
                   env={"SYNCMILL_BOARD__LEASE_TIMEOUT_SECONDS": "1"})
            sc.add("passed", "fix add")
            sc.run("passed", {"do": "fix"})
            before = release_read(sc, [])
            history = before["transitions"][sc.items["mixed"]]
            first_claim = history[1]["at"]
            recovery = next(t["at"] for t in history if t["note"] == "stale-claim recovery")
            if recovery - first_claim < 4:
                sys.exit(f"compacted: no room for a cutoff: {history!r}")
            older_than = time.time() - (first_claim + recovery) / 2
            line = sc.sm("board", "compact", "--older-than", f"{older_than:.3f}")
            if not line.startswith("Compacted "):
                sys.exit(f"compacted: compact printed {line!r}")
            entry = freeze(sc, killed=False, goals=[])
            entry["before_compaction"] = before
            _require_compacted(entry, before)

        (out / "bindings.json").write_text(
            json.dumps(bindings, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _no_machine_values(db: Path, box: Path, home: Path) -> None:
    needles = {str(box), str(home), socket.gethostname(), os.path.expanduser("~")}
    with sqlite3.connect(db) as conn:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in conn.execute(f"SELECT * FROM {table}"):
                for cell in row:
                    if isinstance(cell, str) and any(n and n in cell for n in needles):
                        sys.exit(f"{db}: {table} holds a machine-bound value: {cell[:200]!r}")
    conn.close()


def _require(entry: dict, role: str, *, state: str, human: bool | None = None,
             lease: bool | None = None, note: str | None = None,
             evidence: list | None = None) -> None:
    item_id = entry["items"][role]
    item = entry["release_read"]["items"][item_id]
    history = entry["release_read"]["transitions"][item_id]
    problems = []
    if item["state"] != state:
        problems.append(f"state {item['state']!r}")
    if human is not None and item["human_required"] != human:
        problems.append(f"human_required {item['human_required']!r}")
    if lease is not None and bool(item["lease_run_id"]) != lease:
        problems.append(f"lease {item['lease_run_id']!r}")
    if note is not None and history[-1]["note"] != note:
        problems.append(f"last note {history[-1]['note']!r}")
    if evidence is not None:
        got = [(e["kind"], e["passed"]) for e in item["evidence"]]
        if got != evidence:
            problems.append(f"evidence {got!r}")
    if problems:
        sys.exit(f"{role}: required property missing: {', '.join(problems)}")


def _claims(history: list[dict]) -> int:
    return sum(1 for t in history if (t["from_state"], t["to_state"]) == ("planned", "running"))


def _require_lifecycle(entry: dict) -> None:
    _require(entry, "passed", state="review", evidence=[("gate", True)],
             note="agent completed; awaiting review")
    _require(entry, "verified", state="done", evidence=[("gate", True)],
             note="agent completed; verified")
    _require(entry, "handed-off", state="review", evidence=[("gate", True)])
    _require(entry, "route-gate-failed", state="blocked", human=True, evidence=[])
    _require(entry, "pipeline-gate-failed", state="blocked", human=True,
             evidence=[("gate", False)])
    _require(entry, "agent-failed", state="blocked", human=True, evidence=[])
    _require(entry, "reclaimed", state="review", evidence=[("gate", True)])
    _require(entry, "evidence-done", state="done", evidence=[("gate", True), ("manual", True)])
    _require(entry, "signed-off", state="done", evidence=[("gate", True), ("human", True)])
    _require(entry, "bare-done-refused", state="planned", evidence=[])
    _require(entry, "human-blocked", state="blocked", note="waiting on ops")
    _require(entry, "annotated", state="planned", evidence=[("manual", True)])
    _require(entry, "human-required", state="planned", human=True)
    _require(entry, "human-cleared", state="planned", human=False)
    read = entry["release_read"]
    handed = read["items"][entry["items"]["handed-off"]]
    if not handed["handoff"] or "notes/plan.md" not in handed["outputs"]:
        sys.exit(f"handed-off: handoff/outputs not recorded: {handed!r}")
    reclaimed = read["transitions"][entry["items"]["reclaimed"]]
    if _claims(reclaimed) != 2 or not any(t["note"] == "stale-claim recovery" for t in reclaimed):
        sys.exit(f"reclaimed: expected two claims and a stale recovery: {reclaimed!r}")
    imported = [read["items"][i] for r, i in entry["items"].items() if r.startswith("imported-")]
    if not imported or any(
        len(i["inputs"]) != 5 or i["state"] != "review" or not i["human_required"]
        or [(e["kind"], e["passed"]) for e in i["evidence"]] != [("tracegraph", False)]
        for i in imported
    ):
        sys.exit(f"lifecycle: review candidates not imported as expected: {imported!r}")
    active = {d["id"] for d in read["active_decisions"]["*"]}
    if entry["decisions"]["superseded"] in active or len(active) != 4:
        sys.exit(f"lifecycle: unexpected active decisions: {sorted(active)}")
    stats = read["stats"]
    if stats["fresh"]["redispatched"] != [entry["items"]["reclaimed"]] \
            or stats["fresh"]["reclaimed_stale"] != 1:
        sys.exit(f"lifecycle: unexpected stats: {stats['fresh']!r}")
    if stats["aged"]["blocked_aged"] == stats["fresh"]["blocked_aged"]:
        sys.exit("lifecycle: the aged clock changed no stall count")


def _require_compacted(entry: dict, before: dict) -> None:
    after = entry["release_read"]
    rolled, mixed = entry["items"]["rolled"], entry["items"]["mixed"]
    for label, read in (("before", before), ("after", after)):
        fresh = read["stats"]["fresh"]
        if sorted(fresh["redispatched"]) != sorted([rolled, mixed]) \
                or fresh["reclaimed_stale"] != 2:
            sys.exit(f"compacted: stats {label} compaction: {fresh!r}")
    if before["stats"] != after["stats"]:
        sys.exit("compacted: the release's own stats changed across compaction")
    rollups = {r["item_id"]: r["claim_count"]
               for r in after["aggregates"].get("transition_rollups", [])}
    counters = {r["name"]: r["value"]
                for r in after["aggregates"].get("transition_counters", [])}
    live = after["transitions"]
    if rollups.get(rolled) != 2 or live[rolled]:
        sys.exit(f"compacted: rolled is not wholly rolled up: {rollups!r} {live[rolled]!r}")
    if rollups.get(mixed) != 1 or _claims(live[mixed]) != 1:
        sys.exit(f"compacted: mixed does not split its claims: {rollups!r} {live[mixed]!r}")
    if counters.get("reclaimed_stale") != 1 or not any(
            t["note"] == "stale-claim recovery" for t in live[mixed]):
        sys.exit(f"compacted: reclaims do not split: {counters!r} {live[mixed]!r}")


if __name__ == "__main__":
    main()
