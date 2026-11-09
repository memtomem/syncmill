"""Produce the frozen rooms documents under this directory (#271).

Run once per release, with THAT release's interpreter and source, never the
current one. The output directory must not exist; frozen fixtures are never
regenerated.

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev)
    python tests/fixtures/persisted/rooms/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --out tests/fixtures/persisted/rooms/v0.3.0

Each scenario is one project with its own room store, because a project holds
one active room at a time (`recovered` continues `applying`'s project). The
released CLI makes every scenario: `room create`, `room round`, `room select`,
`room apply`, `room close` and `room recover`, with a fake `codex` and a fake
`kimi` whose behaviour each scenario sets. Two states need a process death
that no CLI exposes:
- `running`: the released `room round`, SIGKILLed while its agents work.
- `applying`: the coordinator, built the way the released CLI builds it, is
  killed at the room-bound run artifact's `applied` flip.

The project's state directory (`<runs.root>/<project>/`: the room database,
candidate artifacts, member logs and any run artifact) is copied byte for byte
as soon as its step finishes, including the WAL a killed process leaves.
Room workspaces (git clones under `workspace.root`) are not copied.
`bindings.json` records the values bound to the generating machine so the
tests can rebind them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TREE = {"calc.py": "def add(a, b):\n    return a - b\n"}
GATE = ["python -c \"from calc import add; assert add(2, 3) == 5\""]

# FAKE_PLAN maps the binary to one behaviour: fix (codex a + b, kimi b + a),
# wrong (fails the gate), none (changes nothing), fail (exits 1), hang.
# A prompt carrying TESTS also adds a test file.
FAKE_AGENT = '''\
import json, os, sys, time
from pathlib import Path
BINARY = os.path.basename(sys.argv[0])
args = sys.argv[1:]
if args[:2] == ["login", "status"] or args[:1] in (["--version"], ["--help"]):
    print(BINARY + " (fake) - logged in"); sys.exit(0)
plan = json.loads(os.environ.get("FAKE_PLAN") or "{}").get(BINARY, "fix")
if plan == "hang":
    marker = Path(os.environ["FAKE_STARTED"]) / BINARY
    marker.with_suffix(".tmp").write_text(str(os.getpid()))
    os.replace(marker.with_suffix(".tmp"), marker)
    time.sleep(120); sys.exit(1)
if plan == "fail":
    sys.stderr.write(BINARY + " (fake): refused\\n"); sys.exit(1)
body = {"fix": "a + b" if BINARY == "codex" else "b + a", "wrong": "a * b"}.get(plan)
if body:
    Path("calc.py").write_text("def add(a, b):\\n    return " + body + "\\n", encoding="utf-8")
if body and "TESTS" in " ".join(args):
    Path("test_calc.py").write_text(
        "from calc import add\\n\\n\\ndef test_add():\\n    assert add(2, 3) == 5\\n",
        encoding="utf-8",
    )
message = BINARY + " (fake) " + plan + "."
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

# Runs inside the released interpreter: room apply, killed at the run
# artifact's applied flip (receipt written, patch in the source).
CRASH_ROOM_APPLY = '''\
import os, sys
from pathlib import Path
from syncmill import cli
from syncmill.artifacts import ArtifactStore
repo, room_id = sys.argv[1:3]
original = ArtifactStore._compare_and_swap_manifest
def die(self, run_dir, expected, replacement):
    if replacement.get("state") == "applied":
        os._exit(0)
    return original(self, run_dir, expected, replacement)
ArtifactStore._compare_and_swap_manifest = die
_, _, coordinator = cli._room_coordinator(Path(repo))
coordinator.apply(room_id)
sys.exit("room apply returned; the crash hook never fired")
'''

GIT_ENV = {
    "GIT_AUTHOR_NAME": "frozen", "GIT_AUTHOR_EMAIL": "frozen@example.invalid",
    "GIT_COMMITTER_NAME": "frozen", "GIT_COMMITTER_EMAIL": "frozen@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


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

    with tempfile.TemporaryDirectory() as tmp:
        box = Path(tmp).resolve()
        repo, home, state, fakebin, started = (
            box / n for n in ("repo", "home", "state", "fakebin", "started")
        )
        for folder in (repo, home, state, fakebin, started):
            folder.mkdir()
        for binary in ("codex", "kimi"):
            (fakebin / binary).write_text(f"#!{sys.executable}\n" + FAKE_AGENT, encoding="utf-8")
            (fakebin / binary).chmod(0o755)
        (fakebin / "python").symlink_to(sys.executable)
        base_env = {
            "HOME": str(home),
            "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
            "TMPDIR": str(box),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_STARTED": str(started),
            "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex", "kimi-code"]),
            "SYNCMILL_CLAUDE__ENABLED": "false",
            "SYNCMILL_ROOMS__ENABLED": "true",
            "SYNCMILL_RUNS__ROOT": str(state / "runs"),
            "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
            "SYNCMILL_MEMORY__DB_PATH": str(state / "memory" / "memtomem.db"),
            "SYNCMILL_MEMORY__MEMORY_DIR": str(state / "memory" / "files"),
            "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
            "SYNCMILL_TUI__STATE_DIR": str(state / "tui"),
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
        bindings: dict = {
            "release": version,
            "box_root": str(box),
            "tree": TREE,
            "scenarios": {},
        }

        class Scenario:
            def __init__(self, project: str, *, gate: bool = True,
                         plan: dict | None = None, allowlist: list | None = None):
                self.project = f"frozen-rooms-{project}"
                self.env = {**base_env, "SYNCMILL_PROJECT__ID": self.project,
                            "FAKE_PLAN": json.dumps(plan or {})}
                if gate:
                    self.env["SYNCMILL_GATE__COMMANDS"] = json.dumps(GATE)
                if allowlist is not None:
                    self.env["SYNCMILL_GATE__ENV_ALLOWLIST"] = json.dumps(allowlist)
                self.room_id = ""

            def room(self, *argv: str, ok: bool = True) -> str:
                done = subprocess.run(
                    [str(syncmill), "room", *argv], cwd=repo, env=self.env,
                    capture_output=True, text=True,
                )
                if ok and done.returncode != 0:
                    sys.exit(f"{self.project}: room {argv}: {done.stdout}{done.stderr}")
                return done.stdout

            def create(self) -> str:
                self.room_id = self.room(
                    "create", "fix add", "-a", "codex", "-a", "kimi-code"
                ).split()[0]
                return self.room_id

            @property
            def db(self) -> Path:
                return state / "runs" / self.project / "rooms" / "rooms.sqlite3"

            def eligible(self, number: int | None = None) -> list[str]:
                # Immutable: a read-only connection to a WAL database would
                # create -shm/-wal files of its own in the release's store.
                uri = f"file:{self.db}?mode=ro&immutable=1"
                with sqlite3.connect(uri, uri=True) as conn:
                    rows = [row[0] for row in conn.execute(
                        "SELECT c.id FROM candidates c JOIN rounds r ON r.id = c.round_id "
                        "WHERE c.room_id = ? AND c.eligible = 1 AND (? IS NULL OR r.number = ?) "
                        "ORDER BY r.number, c.member_id",
                        (self.room_id, number, number),
                    )]
                conn.close()
                if not rows:
                    sys.exit(f"{self.project}: no eligible candidate")
                return rows

        def freeze(name: str, sc: Scenario, *, crashed: bool = False) -> None:
            # A process that exits normally checkpoints its WAL. One killed
            # mid-step leaves committed rows in rooms.sqlite3-wal, which is the
            # shape a crash leaves behind, so it is copied with the rest.
            source = state / "runs" / sc.project
            wal = sorted(p.name for p in source.rglob("*-wal"))
            if bool(wal) != crashed:
                sys.exit(f"{name}: expected {'a' if crashed else 'no'} WAL, found {wal}")
            shutil.copytree(source, out / name)
            # Bindings are read from a scratch copy with a normal connection,
            # so rows still in a crash's WAL are seen and the frozen files are
            # never opened.
            scratch = box / "bindings-read"
            shutil.rmtree(scratch, ignore_errors=True)
            shutil.copytree(source / "rooms", scratch)
            with sqlite3.connect(scratch / "rooms.sqlite3") as conn:
                fingerprints = sorted({
                    value for (value,) in conn.execute(
                        "SELECT source_fingerprint FROM rooms "
                        "UNION SELECT source_fingerprint FROM candidates"
                    ) if value
                })
            conn.close()
            bindings["scenarios"][name] = {
                "project_id": sc.project,
                "room_id": sc.room_id,
                "source_fingerprints": fingerprints,
            }

        # ── the lifecycle ────────────────────────────────────────────────
        sc = Scenario("draft")
        sc.create()
        freeze("draft", sc)

        # Round 1, round 2 continuing each member's own work, round 3
        # branching from round 2's first eligible candidate.
        sc = Scenario("awaiting")
        sc.create()
        sc.room("round", sc.room_id)
        sc.room("round", sc.room_id, "now add TESTS", "--lineage", "continue-own")
        sc.room("round", sc.room_id, "tidy it", "--lineage", "branch-from",
                "--base-candidate", sc.eligible(2)[0])
        freeze("awaiting", sc)

        # No gate: provenance records gate "none" and candidates carry no gate.
        # kimi changes nothing, so its candidate has an empty patch.
        sc = Scenario("ungated", gate=False, plan={"kimi": "none"})
        sc.create()
        sc.room("round", sc.room_id)
        freeze("ungated", sc)

        # codex fails the gate, kimi's attempt fails: no eligible candidate.
        sc = Scenario("rejected", plan={"codex": "wrong", "kimi": "fail"})
        sc.create()
        sc.room("round", sc.room_id)
        freeze("rejected", sc)

        # Selected with a gate env allowlist (v0.3.0 records it; older
        # releases ignore the setting).
        sc = Scenario("selected", allowlist=["FAKE_PLAN"])
        sc.create()
        sc.room("round", sc.room_id)
        sc.room("select", sc.room_id, sc.eligible()[0])
        freeze("selected", sc)

        sc = Scenario("closed")
        sc.create()
        sc.room("round", sc.room_id)
        sc.room("close", sc.room_id)
        freeze("closed", sc)

        # A source change after round 1 makes round 2 find the room stale.
        sc = Scenario("stale")
        sc.create()
        sc.room("round", sc.room_id)
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # edited\n")
        sc.room("round", sc.room_id, ok=False)
        git("checkout", "--", ".")
        freeze("stale", sc)

        # The released round, SIGKILLed while both agents work.
        sc = Scenario("running", plan={"codex": "hang", "kimi": "hang"})
        sc.create()
        proc = subprocess.Popen(
            [str(syncmill), "room", "round", sc.room_id], cwd=repo, env=sc.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        agent_pids: list[int] = []
        try:
            deadline = time.monotonic() + 60
            markers = [started / "codex", started / "kimi"]
            while not all(marker.exists() for marker in markers):
                if time.monotonic() > deadline or proc.poll() is not None:
                    sys.exit("running: the fake agents never started")
                time.sleep(0.1)
            agent_pids = [int(marker.read_text()) for marker in markers]
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for pid in agent_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        freeze("running", sc, crashed=True)

        sc = Scenario("applied")
        sc.create()
        sc.room("round", sc.room_id)
        sc.room("select", sc.room_id, sc.eligible()[0])
        sc.room("apply", sc.room_id)
        freeze("applied", sc)
        git("checkout", "--", ".")

        # Killed at the artifact's applied flip; then the released `room
        # recover` writes its own recovery record (`recovered`).
        sc = Scenario("applying")
        sc.create()
        sc.room("round", sc.room_id)
        sc.room("select", sc.room_id, sc.eligible()[0])
        subprocess.run(
            [str(python), "-c", CRASH_ROOM_APPLY, str(repo), sc.room_id],
            cwd=repo, env=sc.env, check=True,
        )
        freeze("applying", sc, crashed=True)
        sc.room("recover")
        freeze("recovered", sc)
        git("checkout", "--", ".")

        (out / "bindings.json").write_text(json.dumps(bindings, indent=2) + "\n")


if __name__ == "__main__":
    main()
