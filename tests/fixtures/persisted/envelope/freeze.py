"""Produce the frozen run-report directories under this directory (#271).

Run once per release, with THAT release's interpreter and source, never the
current one: the point of the fixtures is that the current writer did not make
them. The output directory must not exist; nothing here is ever regenerated.

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev --extra phoenix)
    python tests/fixtures/persisted/envelope/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --out tests/fixtures/persisted/envelope/v0.3.0

The `phoenix` extra matters: without it a release records the stream as
`unavailable` instead of the export error the `phoenix-error` scenario needs.

Every run directory comes from the released `syncmill run` (or `board run`)
with report recording on and fake `codex` / `kimi` binaries on PATH. The run
directory under the reports root is copied byte for byte as soon as its step
finishes, into `runs/<run-id>/`. `index.json` maps each scenario to its run id
and holds what the release's own CLI printed for `report list` (once, over the
whole reports root) and `report show <id>` (per scenario, with its return code
and stderr). `run-envelope.schema.json` is the release's own schema, with its
git blob id in `index.json`. No value in these files is bound to the
generating machine except `report show`'s error for a run with no envelope,
which names the reports root (recorded in `index.json` as `runs_dir`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TREE = {"calc.py": "def add(a, b):\n    return a - b\n"}
GOAL = "frozen-goal"
TEAMS = {"pair": {"agents": ["codex", "kimi-code"], "strategy": "compete"}}

# FAKE_PLAN maps the binary to one behaviour: fix (codex a + b, kimi b + a),
# nochange, fail (exits 1), hang (records its pid, then sleeps).
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
    os.replace(marker.with_suffix(".tmp"), marker)  # never seen half-written
    time.sleep(120); sys.exit(1)
if plan == "fail":
    sys.stderr.write(BINARY + " (fake): refused\\n"); sys.exit(1)
if plan == "fix":
    body = "a + b" if BINARY == "codex" else "b + a"
    Path("calc.py").write_text("def add(a, b):\\n    return " + body + "\\n", encoding="utf-8")
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
    has_phoenix = subprocess.run(
        [str(python), "-c", "import phoenix.otel"], capture_output=True,
    ).returncode == 0
    if not has_phoenix:
        sys.exit("the release venv lacks the phoenix extra: uv sync --frozen --no-dev --extra phoenix")
    schema_blob = subprocess.run(
        ["git", "-C", str(release), "rev-parse", "HEAD:contracts/run-envelope.schema.json"],
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
        reports = state / "reports"
        # A port bound but never listening: a connection is refused at once.
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed.bind(("127.0.0.1", 0))
        phoenix_endpoint = f"http://127.0.0.1:{closed.getsockname()[1]}"
        (state / "breaker-dir").mkdir()
        (state / "trace-blocker").write_text("a regular file where a directory is expected\n")
        base_env = {
            "HOME": str(home),
            "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
            "TMPDIR": str(box),
            "GIT_CONFIG_NOSYSTEM": "1",
            "FAKE_STARTED": str(started),
            "SYNCMILL_PROJECT__ID": "frozen-envelope",
            "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex", "kimi-code"]),
            "SYNCMILL_CLAUDE__ENABLED": "false",
            "SYNCMILL_TEAMS": json.dumps(TEAMS),
            "SYNCMILL_RUNS__ROOT": str(state / "runs"),
            "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
            "SYNCMILL_MEMORY__DB_PATH": str(state / "memory" / "memtomem.db"),
            "SYNCMILL_MEMORY__MEMORY_DIR": str(state / "memory" / "files"),
            "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
            "SYNCMILL_BOARD__DB_PATH": str(state / "board.db"),
            "SYNCMILL_INTEGRATION__REPORT__ENABLED": "true",
            "SYNCMILL_INTEGRATION__REPORT__RUNS_DIR": str(reports),
            # The release's own retention must not prune a scenario before it is read.
            "SYNCMILL_INTEGRATION__REPORT__KEEP_LAST": "1000",
            **GIT_ENV,
        }
        trace_on = {"SYNCMILL_INTEGRATION__TRACE__ENABLED": "true"}
        phoenix = {
            **trace_on,
            "SYNCMILL_INTEGRATION__TRACE__EXPORTER": "otlp-http",
            "SYNCMILL_INTEGRATION__TRACE__ENDPOINT": phoenix_endpoint,
        }
        breaker_dir = {
            "SYNCMILL_BREAKER__ENABLED": "true",
            "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker-dir"),
        }

        def git(*argv: str) -> None:
            subprocess.run(["git", *argv], cwd=repo, env=base_env, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        for name, text in TREE.items():
            (repo / name).write_text(text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "frozen tree")

        (out / "runs").mkdir(parents=True)
        index: dict = {
            "release": version,
            "schema_blob": schema_blob,
            "runs_dir": str(reports),
            "scenarios": {},
        }

        def dirs() -> set[str]:
            return {p.name for p in reports.iterdir() if p.is_dir()} if reports.exists() else set()

        def new_dir(before: set[str], what: str) -> str:
            new = sorted(dirs() - before)
            if len(new) != 1:
                sys.exit(f"{what}: expected one new run dir, got {new}")
            return new[0]

        def sm(argv: list[str], env: dict, plan: dict) -> subprocess.CompletedProcess:
            return subprocess.run(
                [str(syncmill), *argv], cwd=repo, capture_output=True, text=True,
                env={**base_env, **env, "FAKE_PLAN": json.dumps(plan)},
            )

        def freeze(scenario: str, run_id: str, expect: dict) -> None:
            # Copied at once, before any later step can touch the reports root.
            run_dir = reports / run_id
            envelope = run_dir / "envelope.json"
            doc = json.loads(envelope.read_text()) if envelope.exists() else None
            for path, want in expect.items():
                got: object = doc
                for key in path.split("."):
                    got = got.get(key) if isinstance(got, dict) else None
                if got != want:
                    sys.exit(f"{scenario}: {path} is {got!r}, expected {want!r}")
            shutil.copytree(run_dir, out / "runs" / run_id)
            index["scenarios"][scenario] = {"run_id": run_id}
            git("checkout", "--", ".")

        def run(scenario: str, expect: dict, *, plan: dict | None = None,
                env: dict | None = None, argv: tuple[str, ...] = ("-a", "codex")) -> None:
            before = dirs()
            done = sm(["run", "fix add", *argv], env or {}, plan or {})
            freeze(scenario, new_dir(before, f"{scenario}: {done.stderr}"), expect)

        def interrupted(scenario: str, sig: int, expect: dict) -> None:
            marker = started / "codex"
            marker.unlink(missing_ok=True)
            before = dirs()
            proc = subprocess.Popen(
                [str(syncmill), "run", "fix add", "-a", "codex"], cwd=repo,
                env={**base_env, "FAKE_PLAN": json.dumps({"codex": "hang"})},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            agent_pid = None
            try:
                deadline = time.monotonic() + 60
                # The agent is working and the skeleton envelope is on disk.
                while not (marker.exists() and any(
                    (reports / d / "envelope.json").exists() for d in dirs() - before
                )):
                    if time.monotonic() > deadline or proc.poll() is not None:
                        sys.exit(f"{scenario}: the fake agent never started")
                    time.sleep(0.1)
                agent_pid = int(marker.read_text())
                proc.send_signal(sig)
                proc.wait(timeout=60)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if agent_pid is not None:
                    try:
                        os.kill(agent_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            freeze(scenario, new_dir(before, scenario), expect)

        completed = {"result.status": "completed", "result.agent_id": "codex"}
        route_failed = {"result.status": "failed", "result.agent_id": "none",
                        "result.exit_code": -1, "result.gate_passed": None}

        run("completed", {**completed, "result.files_changed_count": 1})
        run("nochange", {**completed, "result.files_changed_count": 0}, plan={"codex": "nochange"})
        run("failed", route_failed, plan={"codex": "fail"})
        run("gate-pass", {**completed, "result.gate_passed": True},
            env={"SYNCMILL_GATE__COMMANDS": '["true"]'})
        run("gate-fail", {"result.status": "failed", "result.agent_id": "codex",
                          "result.gate_passed": False},
            env={"SYNCMILL_GATE__COMMANDS": '["false"]'}, argv=("-a", "codex", "-s", "pipeline"))
        run("compete-team", {"team": "pair", "strategy": "compete",
                             "agents": ["codex", "kimi-code"], "result.status": "completed"},
            argv=("--team", "pair"))
        run("traced", {**completed, "trace.status": "ok"}, env=trace_on)
        run("phoenix-error", {**completed, "trace.status": "ok", "trace.stream.status": "error",
                              "degraded": ["phoenix"]}, env=phoenix)
        run("trace-error", {**completed, "trace.status": "error", "degraded": ["trace"]},
            env={**trace_on, "SYNCMILL_INTEGRATION__TRACE__DIR": str(state / "trace-blocker")})
        run("breaker-degraded", {**completed, "degraded": ["breaker"]}, env=breaker_dir)
        run("multi-degraded", {**completed, "degraded": ["breaker", "phoenix"]},
            env={**breaker_dir, **phoenix})
        run("trace-only", {}, env={**trace_on, "SYNCMILL_INTEGRATION__REPORT__ENABLED": "false"})

        board_env = {"SYNCMILL_BOARD__ENABLED": "true"}
        added = sm(["board", "add", "fix add", "--goal", GOAL], board_env, {})
        if added.returncode != 0:
            sys.exit(f"board add: {added.stderr}")
        item_id = added.stdout.split("added item ")[1].split()[0]
        before = dirs()
        done = sm(["board", "run", "--id", item_id, "--agent", "codex"], board_env, {})
        freeze("board", new_dir(before, f"board: {done.stderr}"),
               {"board": {"item_id": item_id, "goal": GOAL}, **completed})

        skeleton = {"result.status": "failed", "result.agent_id": None, "result.exit_code": None}
        interrupted("killed", signal.SIGKILL, skeleton)
        interrupted("interrupted", signal.SIGINT, {"result.status": "failed",
                                                   "result.agent_id": "none",
                                                   "result.exit_code": 1})

        # A listing that merely returned directories in name order must not pass:
        # the newest-first order has to differ from the ascending directory names.
        listed = [
            d for d in sorted(dirs()) if (reports / d / "envelope.json").exists()
        ]
        newest = sorted(
            listed,
            key=lambda d: json.loads((reports / d / "envelope.json").read_text())["created_at"],
            reverse=True,
        )
        extra = 0
        while newest == listed or newest == listed[::-1]:
            extra += 1
            if extra > 5:
                sys.exit("could not make created_at order differ from name order")
            run(f"completed-{extra}", {**completed})
            listed = [d for d in sorted(dirs()) if (reports / d / "envelope.json").exists()]
            newest = sorted(
                listed,
                key=lambda d: json.loads((reports / d / "envelope.json").read_text())["created_at"],
                reverse=True,
            )

        closed.close()
        shown = sm(["report", "list"], {}, {})
        if shown.returncode != 0:
            sys.exit(f"report list: {shown.stderr}")
        index["report_list"] = shown.stdout
        for entry in index["scenarios"].values():
            done = sm(["report", "show", entry["run_id"]], {}, {})
            entry["report_show"] = {
                "returncode": done.returncode, "stdout": done.stdout, "stderr": done.stderr,
            }

        shutil.copyfile(release / "contracts" / "run-envelope.schema.json",
                        out / "run-envelope.schema.json")
        (out / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
