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

Part 2 freezes run dirs with a real preflight block into its own reports root,
`<release>/preflight/`, leaving part 1's bytes alone. Run it with an interpreter
that has `jsonschema` (the fake toolgraph validates what it writes):

    .venv/bin/python tests/fixtures/persisted/envelope/freeze.py --part preflight \\
        --release-root /tmp/wt-v0.3.0 --out tests/fixtures/persisted/envelope/v0.3.0/preflight

`preflight_scenarios` lists each scenario with the block and preflight files
measured on the release CLIs; the run stops if a release writes anything else.
`index.json` there also names the golden blob ids, the scenarios whose producer
reply is synthetic, and the scenarios reached through an injected write failure.
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
    parser.add_argument("--part", choices=("runs", "preflight"), default="runs")
    args = parser.parse_args()
    if args.part == "preflight":
        freeze_preflight(args.release_root.resolve(), args.out.resolve())
        return
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


# ---------------------------------------------------------------------------
# Part 2 (`--part preflight`): run dirs whose envelope carries a real preflight
# block. The release's own `syncmill run` calls this fake `toolgraph`, which
# answers with the RELEASE tree's vendored golden (tests/fixtures/toolgraph) and
# changes only what the consumer binds to the request:
#   - `toolgraph preflight <agent> <tools...> --profile P --run-id R --out O`:
#     `run_id`. The request's agent, profile and (when the agent is found)
#     candidates must equal the golden's, or it exits 3. Two scenarios are
#     synthetic and say so in index.json: an identity listed in
#     FAKE_TG_SYNTHETIC_AGENTS gets the golden with `agent` replaced (no
#     successful golden names a second identity), and FAKE_TG_GEN replaces an
#     identity's `graph_generation`.
#   - `toolgraph control-preflight <plan> --profile P --out O`: `run_id`,
#     `plan_digest` (sha256 of the plan bytes) and one evaluation per plan
#     principal with `eligible` = its `candidate_tools`; a principal outside
#     FAKE_TG_PRINCIPALS exits 3. The `finding` behaviour adds one synthetic
#     finding, which is not a claim that toolgraph would find it in that plan.
# Every document is validated against the release's vendored producer schema
# before it is written. FAKE_TG is one behaviour, or a JSON map by identity
# (control uses the key "*"): allow, drift, unknown, unresolved, fail, hang,
# finding.
FAKE_TOOLGRAPH = '''\
import hashlib, json, os, sys, time
from pathlib import Path
import jsonschema

kind, rest = sys.argv[1], sys.argv[2:]
opts, positional, i = {}, [], 0
while i < len(rest):
    if rest[i].startswith("--"):
        opts[rest[i]] = rest[i + 1]
        i += 2
    else:
        positional.append(rest[i])
        i += 1
golden = Path(os.environ["FAKE_TG_GOLDEN"])
with open(os.environ["FAKE_TG_LOG"], "a") as log:
    log.write(json.dumps({"kind": kind, "argv": sys.argv[1:]}) + "\\n")
plan = os.environ.get("FAKE_TG", "allow")
key = positional[0] if kind == "preflight" else "*"
if plan.startswith("{"):
    plan = json.loads(plan)[key]
if plan == "fail":
    sys.stderr.write("toolgraph (fake): graph store unavailable\\n")
    sys.exit(2)
if plan == "hang":
    time.sleep(60)
    sys.exit(1)

def refuse(why):
    sys.stderr.write("fake toolgraph: " + why + "\\n")
    sys.exit(3)

if kind == "preflight":
    agent, tools = positional[0], positional[1:]
    name = {"allow": "preflight-allow", "drift": "preflight-drift",
            "unknown": "preflight-unknown-tool",
            "unresolved": "preflight-unresolved-identity"}[plan]
    doc = json.loads((golden / (name + ".json")).read_text())
    if agent != doc["agent"]:
        if agent not in json.loads(os.environ.get("FAKE_TG_SYNTHETIC_AGENTS", "[]")):
            refuse("agent " + agent + " is not the golden's " + doc["agent"])
        doc["agent"] = agent
    if opts["--profile"] != doc["profile"]:
        refuse("profile differs from the golden")
    covered = [*doc["eligible"], *(row["candidate"] for row in doc["rejected"])]
    if doc["agent_found"] and sorted(covered) != sorted(tools):
        refuse("candidates differ from the golden")
    doc["run_id"] = opts["--run-id"]
    generations = json.loads(os.environ.get("FAKE_TG_GEN", "{}"))
    if agent in generations:
        doc["graph_generation"] = generations[agent]
    schema = "preflight.schema.json"
else:
    raw = Path(positional[0]).read_bytes()
    control = json.loads(raw)
    doc = json.loads((golden / "control-preflight-v1.json").read_text())
    if opts["--profile"] != doc["profile"]:
        refuse("profile differs from the golden")
    allowed = json.loads(os.environ["FAKE_TG_PRINCIPALS"])
    if any(p["id"] not in allowed for p in control["principals"]):
        refuse("unexpected principal")
    doc["run_id"] = control["run_id"]
    doc["plan_digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    doc["evaluations"] = [
        {"principal": p["id"], "agent_found": True,
         "eligible": list(p["candidate_tools"]), "rejected": []}
        for p in control["principals"]
    ]
    if plan == "finding":
        doc["findings"] = [{"code": "UNREACHABLE_NODE", "node": "verify"}]
        doc["decision"] = "advisory_warn"
    elif plan != "allow":
        refuse("no control behaviour " + plan)
    schema = "control-preflight.schema.json"
contract = json.loads((golden / schema).read_text())
jsonschema.validators.validator_for(contract)(contract).validate(doc)
Path(opts["--out"]).write_text(json.dumps(doc, indent=2) + "\\n")
print(json.dumps(doc))
'''

# Persistence failures are released shapes only a failing write reaches: the
# release's own writer runs with `syncmill.preflight._atomic_write` raising for
# one destination name and delegating every other write. Nothing else is patched.
FAILING_WRITE_LAUNCHER = """\
import os, sys
import syncmill.preflight as preflight
original = preflight._atomic_write
def failing(raw, dest):
    if dest.name == os.environ["FAIL_WRITE"]:
        raise OSError("injected write failure: " + dest.name)
    return original(raw, dest)
preflight._atomic_write = failing
from syncmill.cli import cli
sys.argv = ["syncmill", *sys.argv[1:]]
cli()
"""
INJECTED_EXCEPTION = "OSError('injected write failure: <destination>')"

ANY = object()  # present; its value is checked by the tests, not here
ALLOW_TOOLS = ["memtomem::mem_search", "syncmill::shared_read"]
DRIFT_TOOLS = ["legacy::old_tool"]
UNKNOWN_TOOLS = ["legacy::missing"]
CONTROL_TOOLS = ["workspace::read", "workspace::write"]
GRAPH_STATE = {"generation": 42, "instance_id": "test-instance"}
GOLDENS = (
    "preflight-allow.json", "preflight-drift.json", "preflight-unknown-tool.json",
    "preflight-unresolved-identity.json", "preflight.schema.json",
    "control-preflight-v1.json", "control-preflight.schema.json",
)


def _block(status: str, mode: str, **fields: object) -> dict:
    return {"status": status, "mode": mode, "profile": "review", **fields}


def _advisory_ok(**fields: object) -> dict:
    return _block("ok", "advisory", graph_generation=42, artifact="preflight.json",
                  digest=ANY, **fields)


def _strict(status: str, decision: str, evaluations: int, blocks: int, warnings: int,
            **fields: object) -> dict:
    return _block(status, "strict", enforcement_decision=decision,
                  evaluation_count=evaluations, block_count=blocks,
                  warning_count=warnings, **fields)


def _control(mode: str, producer: str, warnings: int, *, persisted: bool = True,
             **fields: object) -> dict:
    block = _block("ok" if persisted else "error", mode, graph_generation=42,
                   graph_state=GRAPH_STATE, warning_count=warnings, evaluation_count=1,
                   producer_decision=producer, **fields)
    if persisted:
        block.update(artifact="control-preflight.json", digest=ANY,
                     control_plan={"artifact": "control-plan.json", "digest": ANY})
    return block


def preflight_scenarios(version: tuple[int, ...]) -> list[dict]:
    """Each scenario with its measured block and preflight files (#271 part 2)."""
    strict = {"SYNCMILL_INTEGRATION__TOOLGRAPH__MODE": "strict"}
    as_kimi = {"SYNCMILL_INTEGRATION__TOOLGRAPH__AGENT_MAP": json.dumps({"codex": "kimi-code"})}
    both = {"SYNCMILL_INTEGRATION__TOOLGRAPH__AGENT_MAP":
            json.dumps({"codex": "codex", "kimi-code": "kimi"}),
            "FAKE_TG_SYNTHETIC_AGENTS": json.dumps(["kimi"])}
    timeout = {"SYNCMILL_INTEGRATION__TOOLGRAPH__TIMEOUT_SECONDS": "1"}
    warn = {"SYNCMILL_INTEGRATION__TOOLGRAPH__UNAVAILABLE_POLICY": "warn"}
    control = {"SYNCMILL_INTEGRATION__TOOLGRAPH__CONTROL_PLAN_ENABLED": "true",
               "FAKE_TG_PRINCIPALS": json.dumps(["codex"])}

    def tools(names: list[str]) -> dict:
        return {"SYNCMILL_INTEGRATION__TOOLGRAPH__CANDIDATE_TOOLS": json.dumps(names)}

    strict_files = {"preflight.json", "preflight/eval-000.json"}
    released_before_cleanup = version < (0, 2, 0)
    rows = [
        ("advisory-allow", "allow", {}, _advisory_ok(warning_count=0), {"preflight.json"}),
        ("advisory-drift", "drift", tools(DRIFT_TOOLS), _advisory_ok(warning_count=1),
         {"preflight.json"}),
        ("advisory-unresolved", "unresolved", as_kimi,
         _block("unresolved_identity", "advisory", graph_generation=42, warning_count=0,
                artifact="preflight.json", digest=ANY), {"preflight.json"}),
        ("advisory-unmapped", "allow", {"SYNCMILL_INTEGRATION__TOOLGRAPH__AGENT_MAP": "{}"},
         _block("unresolved_identity", "advisory"), set()),
        ("advisory-error", "fail", {}, _block("error", "advisory"), set()),
        ("advisory-timeout", "hang", timeout, _block("timeout", "advisory"), set()),
        ("strict-allow", "allow", strict,
         _strict("ok", "allow", 1, 0, 0, graph_generation=42, artifact="preflight.json",
                 digest=ANY), strict_files),
        ("strict-warn", "drift", {**strict, **tools(DRIFT_TOOLS)},
         _strict("ok", "allow", 1, 0, 1, graph_generation=42, artifact="preflight.json",
                 digest=ANY), strict_files),
        ("strict-block", "unknown", {**strict, **tools(UNKNOWN_TOOLS)},
         _strict("ok", "block", 1, 1, 0, graph_generation=42, artifact="preflight.json",
                 digest=ANY), strict_files),
        ("strict-timeout", "hang", {**strict, **timeout},
         _strict("timeout", "block", 1, 1, 0, artifact="preflight.json", digest=ANY),
         {"preflight.json"}),
        ("strict-unresolved", "unresolved", {**strict, **as_kimi},
         _strict("unresolved_identity", "block", 1, 1, 0, graph_generation=42,
                 artifact="preflight.json", digest=ANY), strict_files),
        ("strict-unavailable", "fail", strict,
         _strict("error", "block", 1, 1, 0, artifact="preflight.json", digest=ANY),
         {"preflight.json"}),
        ("strict-unavailable-warn", "fail", {**strict, **warn},
         _strict("error", "allow", 1, 0, 1, artifact="preflight.json", digest=ANY),
         {"preflight.json"}),
        ("strict-multi-allow", json.dumps({"codex": "allow", "kimi": "allow"}),
         {**strict, **both},
         _strict("ok", "allow", 2, 0, 0, graph_generation=42, artifact="preflight.json",
                 digest=ANY), {*strict_files, "preflight/eval-001.json"}),
        ("strict-multi-mixed", json.dumps({"codex": "allow", "kimi": "fail"}),
         {**strict, **both},
         _strict("error", "block", 2, 1, 0, graph_generation=42, artifact="preflight.json",
                 digest=ANY), strict_files),
        ("strict-gen-mismatch", json.dumps({"codex": "allow", "kimi": "allow"}),
         {**strict, **both, "FAKE_TG_GEN": json.dumps({"kimi": 43})},
         _strict("error", "block", 2, 1, 0, artifact="preflight.json", digest=ANY),
         {*strict_files, "preflight/eval-001.json"}),
        ("strict-unmapped", "allow",
         {**strict, "SYNCMILL_INTEGRATION__TOOLGRAPH__AGENT_MAP":
          json.dumps({"kimi-code": "kimi"})},
         _strict("unresolved_identity", "block", 1, 1, 0, artifact="preflight.json",
                 digest=ANY), {"preflight.json"}),
        ("strict-persist-eval", "allow", {**strict, "FAIL_WRITE": "eval-000.json"},
         _strict("error", "block", 1, 1, 0, graph_generation=42)
         if released_before_cleanup else
         _strict("error", "block", 1, 1, 0, graph_generation=42, artifact="preflight.json",
                 digest=ANY),
         set() if released_before_cleanup else {"preflight.json"}),
        ("strict-persist-aggregate", "allow", {**strict, "FAIL_WRITE": "preflight.json"},
         _strict("error", "block", 1, 1, 0, graph_generation=42),
         {"preflight/eval-000.json"} if released_before_cleanup else set()),
    ]
    if version >= (0, 2, 0):
        c = {**control, **tools(CONTROL_TOOLS)}
        control_files = {"control-plan.json", "control-preflight.json"}
        rows += [
            ("control-allow", "allow", c, _control("advisory", "advisory_allow", 0),
             control_files),
            ("control-strict-allow", "allow", {**c, **strict},
             _control("strict", "advisory_allow", 0, enforcement_decision="allow",
                      block_count=0), control_files),
            ("control-finding", "finding", c, _control("advisory", "advisory_warn", 2),
             control_files),
            ("control-strict-finding", "finding", {**c, **strict},
             _control("strict", "advisory_warn", 1, enforcement_decision="block",
                      block_count=1), control_files),
            ("control-error", "fail", c, _block("error", "advisory"), set()),
            ("control-strict-error", "fail", {**c, **strict},
             _strict("error", "block", 0, 1, 0), set()),
            ("control-strict-error-warn", "fail", {**c, **strict, **warn},
             _strict("error", "allow", 0, 0, 1), set()),
            ("control-strict-timeout", "hang", {**c, **strict, **timeout},
             _strict("timeout", "block", 0, 1, 0), set()),
            ("control-persist", "allow", {**c, "FAIL_WRITE": "control-preflight.json"},
             _control("advisory", "advisory_allow", 0, persisted=False), set()),
            ("control-strict-persist", "allow",
             {**c, **strict, "FAIL_WRITE": "control-preflight.json"},
             _control("strict", "advisory_allow", 0, persisted=False,
                      enforcement_decision="block", block_count=1), set()),
        ]
    multi = {"strict-multi-allow", "strict-multi-mixed", "strict-gen-mismatch"}
    return [
        {"name": name, "plan": plan, "env": env, "block": block, "files": files,
         "argv": ("--team", "pair") if name in multi else ("-a", "codex")}
        for name, plan, env, block, files in rows
    ]


SYNTHETIC = {
    "strict-multi-allow": "identity kimi answered with the allow golden, agent replaced",
    "strict-multi-mixed": "identity kimi answered with the allow golden, agent replaced",
    "strict-gen-mismatch": "identity kimi answered with the allow golden, agent replaced "
                           "and graph_generation 43",
    "control-finding": "one UNREACHABLE_NODE finding added to the control golden",
    "control-strict-finding": "one UNREACHABLE_NODE finding added to the control golden",
}


def _preflight_files(run_dir: Path) -> set[str]:
    owned = {"envelope.json", "result.json", ".syncmill-run-dir"}
    return {
        str(p.relative_to(run_dir)) for p in run_dir.rglob("*")
        if p.is_file() and str(p.relative_to(run_dir)) not in owned
    }


def _matches(got: object, want: object) -> bool:
    if want is ANY:
        return True
    if isinstance(want, dict):
        return (isinstance(got, dict) and set(got) == set(want)
                and all(_matches(got[key], value) for key, value in want.items()))
    return got == want


def _check_block(scenario: str, got: object, want: dict) -> None:
    if not _matches(got, want):
        sys.exit(f"{scenario}: preflight block {got!r} does not match {want!r}")


def freeze_preflight(release: Path, out: Path) -> None:
    if out.exists():
        sys.exit(f"{out} exists; frozen fixtures are never regenerated")
    python = release / ".venv" / "bin" / "python"
    syncmill = release / ".venv" / "bin" / "syncmill"
    version_text = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('syncmill'))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    version = tuple(int(part) for part in version_text.split("."))
    goldens = release / "tests" / "fixtures" / "toolgraph"
    golden_blobs = {}
    for name in GOLDENS:
        if (goldens / name).exists():
            golden_blobs[name] = subprocess.run(
                ["git", "-C", str(release), "rev-parse",
                 f"HEAD:tests/fixtures/toolgraph/{name}"],
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
        (fakebin / "toolgraph").write_text(f"#!{sys.executable}\n" + FAKE_TOOLGRAPH,
                                           encoding="utf-8")
        (fakebin / "toolgraph").chmod(0o755)
        reports = state / "reports"
        calls = box / "toolgraph-calls.jsonl"
        base_env = {
            "HOME": str(home),
            "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
            "TMPDIR": str(box),
            "GIT_CONFIG_NOSYSTEM": "1",
            "FAKE_STARTED": str(started),
            "FAKE_TG_GOLDEN": str(goldens),
            "FAKE_TG_LOG": str(calls),
            "SYNCMILL_PROJECT__ID": "frozen-envelope",
            "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex", "kimi-code"]),
            "SYNCMILL_CLAUDE__ENABLED": "false",
            "SYNCMILL_TEAMS": json.dumps(TEAMS),
            "SYNCMILL_RUNS__ROOT": str(state / "runs"),
            "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
            "SYNCMILL_MEMORY__DB_PATH": str(state / "memory" / "memtomem.db"),
            "SYNCMILL_MEMORY__MEMORY_DIR": str(state / "memory" / "files"),
            "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
            "SYNCMILL_INTEGRATION__REPORT__ENABLED": "true",
            "SYNCMILL_INTEGRATION__REPORT__RUNS_DIR": str(reports),
            "SYNCMILL_INTEGRATION__REPORT__KEEP_LAST": "1000",
            "SYNCMILL_INTEGRATION__TOOLGRAPH__ENABLED": "true",
            "SYNCMILL_INTEGRATION__TOOLGRAPH__AGENT_MAP": json.dumps({"codex": "codex"}),
            "SYNCMILL_INTEGRATION__TOOLGRAPH__CANDIDATE_TOOLS": json.dumps(ALLOW_TOOLS),
            **GIT_ENV,
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
            "release": version_text,
            "runs_dir": str(reports),
            "goldens": golden_blobs,
            "scenarios": {},
        }

        def dirs() -> set[str]:
            return {p.name for p in reports.iterdir() if p.is_dir()} if reports.exists() else set()

        def new_dir(before: set[str], what: str) -> str:
            new = sorted(dirs() - before)
            if len(new) != 1:
                sys.exit(f"{what}: expected one new run dir, got {new}")
            return new[0]

        def toolgraph_calls() -> int:
            return len(calls.read_text().splitlines()) if calls.exists() else 0

        def freeze(scenario: dict, run_id: str, result: dict) -> None:
            run_dir = reports / run_id
            doc = json.loads((run_dir / "envelope.json").read_text())
            _check_block(scenario["name"], doc.get("preflight"), scenario["block"])
            for key, want in result.items():
                if (doc.get("result") or {}).get(key) != want:
                    sys.exit(f"{scenario['name']}: result.{key} is "
                             f"{(doc.get('result') or {}).get(key)!r}, expected {want!r}")
            files = _preflight_files(run_dir)
            if files != scenario["files"]:
                sys.exit(f"{scenario['name']}: preflight files {sorted(files)}, "
                         f"expected {sorted(scenario['files'])}")
            shutil.copytree(run_dir, out / "runs" / run_id)
            entry: dict = {"run_id": run_id}
            if scenario["name"] in SYNTHETIC:
                entry["synthetic"] = SYNTHETIC[scenario["name"]]
            if "FAIL_WRITE" in scenario["env"]:
                entry["injected_write_failure"] = {
                    "destination": scenario["env"]["FAIL_WRITE"],
                    "exception": INJECTED_EXCEPTION,
                }
            index["scenarios"][scenario["name"]] = entry
            git("checkout", "--", ".")

        blocked = {"status": "failed", "agent_id": "preflight", "exit_code": 1}
        for scenario in preflight_scenarios(version):
            env = {**base_env, **scenario["env"], "FAKE_TG": scenario["plan"],
                   "FAKE_PLAN": "{}"}
            argv = ["run", "fix add", *scenario["argv"]]
            command = (
                [str(python), "-c", FAILING_WRITE_LAUNCHER, *argv]
                if "FAIL_WRITE" in scenario["env"] else [str(syncmill), *argv]
            )
            calls.unlink(missing_ok=True)
            before = dirs()
            done = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
            run_id = new_dir(before, f"{scenario['name']}: {done.stderr}")
            if scenario["name"] in ("advisory-unmapped", "strict-unmapped") and toolgraph_calls():
                sys.exit(f"{scenario['name']}: toolgraph was called")
            decision = scenario["block"].get("enforcement_decision")
            expect_blocked = decision == "block"
            if done.returncode != (1 if expect_blocked else 0):
                sys.exit(f"{scenario['name']}: exit {done.returncode}: {done.stderr}")
            freeze(scenario, run_id,
                   blocked if expect_blocked else {"status": "completed", "agent_id": "codex"})

        # SIGKILL while the agent works, after a strict-allow preflight finished:
        # the skeleton envelope (preflight `disabled`) beside the preflight evidence.
        strict_allow = next(s for s in preflight_scenarios(version) if s["name"] == "strict-allow")
        marker = started / "codex"
        marker.unlink(missing_ok=True)
        before = dirs()
        proc = subprocess.Popen(
            [str(syncmill), "run", "fix add", "-a", "codex"], cwd=repo,
            env={**base_env, **strict_allow["env"], "FAKE_TG": "allow",
                 "FAKE_PLAN": json.dumps({"codex": "hang"})},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        agent_pid = None
        try:
            deadline = time.monotonic() + 60
            while not (marker.exists() and any(
                (reports / d / "envelope.json").exists() for d in dirs() - before
            )):
                if time.monotonic() > deadline or proc.poll() is not None:
                    sys.exit("killed-after-preflight: the fake agent never started")
                time.sleep(0.1)
            agent_pid = int(marker.read_text())
            proc.send_signal(signal.SIGKILL)
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
        freeze({"name": "killed-after-preflight", "env": {}, "block": {"status": "disabled"},
                "files": {"preflight.json", "preflight/eval-000.json"}},
               new_dir(before, "killed-after-preflight"),
               {"status": "failed", "agent_id": None, "exit_code": None})

        # Newest-first must differ from directory-name order (see part 1).
        def orders() -> tuple[list[str], list[str]]:
            listed = sorted(dirs())
            newest = sorted(
                listed,
                key=lambda d: json.loads((reports / d / "envelope.json").read_text())["created_at"],
                reverse=True,
            )
            return listed, newest

        listed, newest = orders()
        extra = 0
        while newest == listed or newest == listed[::-1]:
            extra += 1
            if extra > 5:
                sys.exit("could not make created_at order differ from name order")
            scenario = {**preflight_scenarios(version)[0], "name": f"advisory-allow-{extra}"}
            before = dirs()
            done = subprocess.run([str(syncmill), "run", "fix add", "-a", "codex"], cwd=repo,
                                  env={**base_env, "FAKE_TG": "allow", "FAKE_PLAN": "{}"},
                                  capture_output=True, text=True)
            freeze(scenario, new_dir(before, f"{scenario['name']}: {done.stderr}"),
                   {"status": "completed", "agent_id": "codex"})
            listed, newest = orders()

        shown = subprocess.run([str(syncmill), "report", "list"], cwd=repo, env=base_env,
                               capture_output=True, text=True)
        if shown.returncode != 0:
            sys.exit(f"report list: {shown.stderr}")
        index["report_list"] = shown.stdout
        for entry in index["scenarios"].values():
            done = subprocess.run([str(syncmill), "report", "show", entry["run_id"]], cwd=repo,
                                  env=base_env, capture_output=True, text=True)
            entry["report_show"] = {
                "returncode": done.returncode, "stdout": done.stdout, "stderr": done.stderr,
            }
        (out / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
