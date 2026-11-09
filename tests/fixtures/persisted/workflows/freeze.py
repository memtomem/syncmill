"""Produce the frozen workflow documents under this directory (#271).

Run once per release, with THAT release's interpreter and source, never the
current one. The output directory must not exist; frozen fixtures are never
regenerated.

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev)
    python tests/fixtures/persisted/workflows/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --out tests/fixtures/persisted/workflows/v0.3.0

Each scenario is one project with its own workflow database. The released
CLI makes every scenario (`workflow plan`, `run`, `cancel`, `status`, and
v0.3.0's `create` and `revise`) against a fake `codex` (the planner and the
`draft` node) and a fake `kimi` (the final `fix` node). Workflow agents run
with an environment allowlist, so the fakes take their behaviour from a file
under HOME, which the allowlist keeps.

`workflow run` launches a durable job whose worker owns the workflow lease.
Three states need that worker to die:
- `running`: SIGKILLed while the final node's agent works (`draft` is done).
- `unapproved-completed`: exits inside `approve_workflow_run`, after the
  fenced completion commit and before the approval is written.
- `unapproved-running`: exits right after the final artifact is published,
  before the completion commit.
The two exits are hooks loaded into the worker's own interpreter through a
`.pth` file this script puts in the release venv for the run and removes
afterwards; they act only while HOME holds a `crash-at` file.

The project's state directory (`<runs.root>/<project>/`: the workflow
database, workflow directories and any workflow-origin run artifact) is
copied byte for byte once every writer has stopped, including any WAL a
killed process leaves. Workspaces and TUI job records are not copied.
`bindings.json` records the values bound to the generating machine.
"""

from __future__ import annotations

import argparse
import fcntl
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
GATE = ["python3 -c \"from calc import add; assert add(2, 3) == 5\""]
SPEC = {
    "schema_version": 1,
    "goal": "fix add",
    "final_node": "fix",
    "nodes": [
        {"id": "draft", "agent": "codex", "prompt": "draft the fix"},
        {"id": "fix", "agent": "kimi-code", "prompt": "apply the fix", "needs": ["draft"]},
    ],
}
REVISED = {**SPEC, "goal": "fix add, revised"}

# HOME/fake-control.json maps the binary to one behaviour: fix (writes a + b),
# wrong (a * b, fails the gate), none (changes nothing), fail (exits 1), hang.
# The planner call (codex, prompt naming final_node) answers with the spec.
FAKE_AGENT = '''\
import json, os, sys, time
from pathlib import Path
BINARY = os.path.basename(sys.argv[0])
args = sys.argv[1:]
if args[:2] == ["login", "status"] or args[:1] in (["--version"], ["--help"]):
    print(BINARY + " (fake) - logged in"); sys.exit(0)
home = Path(os.environ["HOME"])
control = json.loads((home / "fake-control.json").read_text())
if BINARY == "codex" and "final_node" in " ".join(args):
    message = json.dumps(control["spec"])
else:
    plan = control.get(BINARY, "fix")
    if plan == "hang":
        marker = home / "started" / BINARY
        marker.parent.mkdir(exist_ok=True)
        marker.with_suffix(".tmp").write_text(str(os.getpid()))
        os.replace(marker.with_suffix(".tmp"), marker)
        time.sleep(300); sys.exit(1)
    if plan == "fail":
        sys.stderr.write(BINARY + " (fake): refused\\n"); sys.exit(1)
    body = {"fix": "a + b", "wrong": "a * b"}.get(plan)
    if body:
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

# Loaded at interpreter start by the .pth below, in every process of the
# release venv. HOME/fake-host pins the host name the release records in a
# lease owner (so no real machine name is frozen); HOME/crash-at names a
# crash point. Without those files it does nothing.
CRASH_HOOK = '''\
import os
from pathlib import Path


def _install():
    home = Path(os.environ.get("HOME", "/nonexistent"))
    host = home / "fake-host"
    if host.is_file():
        import socket
        name = host.read_text().strip()
        socket.gethostname = lambda: name
    flag = home / "crash-at"
    if not flag.is_file():
        return
    point = flag.read_text().strip()
    from syncmill.artifacts import ArtifactStore

    if point == "approve":
        def approve_workflow_run(self, *args, **kwargs):
            os._exit(0)
        ArtifactStore.approve_workflow_run = approve_workflow_run
    elif point == "publish":
        original = ArtifactStore.save

        def save(self, *args, **kwargs):
            saved = original(self, *args, **kwargs)
            if str(kwargs.get("run_id", "")).endswith("-final"):
                os._exit(0)
            return saved
        ArtifactStore.save = save


_install()
'''
HOOK_MODULE = "_syncmill_freeze_hook"
FROZEN_HOST = "frozen-host"

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


def _lock_free(path: Path) -> bool:
    """Whether no process holds an flock on ``path`` (absent counts as free)."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return True
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    site = _site_packages(python)
    hook_files = [site / f"{HOOK_MODULE}.py", site / f"zz{HOOK_MODULE}.pth"]
    if any(path.exists() for path in hook_files):
        sys.exit(f"{site} already holds the freeze hook")

    # The job manager's control socket lives in XDG_RUNTIME_DIR; a socket
    # path may not exceed ~104 bytes, so it gets a short directory of its own.
    runtime = Path(tempfile.mkdtemp(prefix="smwf.", dir="/tmp"))
    try:
        hook_files[0].write_text(CRASH_HOOK, encoding="utf-8")
        hook_files[1].write_text(f"import {HOOK_MODULE}\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            _generate(Path(tmp).resolve(), runtime, syncmill, version, out)
    finally:
        for path in hook_files:
            path.unlink(missing_ok=True)
        shutil.rmtree(runtime, ignore_errors=True)


def _generate(box: Path, runtime: Path, syncmill: Path, version: str, out: Path) -> None:
    repo, home, state, fakebin = (box / n for n in ("repo", "home", "state", "fakebin"))
    for folder in (repo, home, state, fakebin):
        folder.mkdir()
    (home / "fake-host").write_text(FROZEN_HOST)
    for binary in ("codex", "kimi"):
        (fakebin / binary).write_text(f"#!{sys.executable}\n" + FAKE_AGENT, encoding="utf-8")
        (fakebin / binary).chmod(0o755)
    (fakebin / "python3").symlink_to(sys.executable)
    base_env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
        "TMPDIR": str(box),
        "XDG_RUNTIME_DIR": str(runtime),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex", "kimi-code"]),
        "SYNCMILL_CLAUDE__ENABLED": "false",
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
        "hostname": FROZEN_HOST,
        "tree": TREE,
        "spec": SPEC,
        "scenarios": {},
    }

    class Scenario:
        def __init__(self, name: str, *, gate: bool = True, **control: str):
            self.name = name
            self.project = f"frozen-workflows-{name}"
            self.env = {**base_env, "SYNCMILL_PROJECT__ID": self.project}
            if gate:
                self.env["SYNCMILL_GATE__COMMANDS"] = json.dumps(GATE)
            self.control = {"spec": SPEC, **control}
            self.workflow_ids: list[str] = []

        @property
        def project_dir(self) -> Path:
            return state / "runs" / self.project

        def prepare(self, crash_at: str | None = None) -> None:
            (home / "fake-control.json").write_text(json.dumps(self.control))
            flag = home / "crash-at"
            flag.unlink(missing_ok=True)
            if crash_at:
                flag.write_text(crash_at)
            shutil.rmtree(home / "started", ignore_errors=True)

        def workflow(self, *argv: str, ok: bool = True) -> str:
            done = subprocess.run(
                [str(syncmill), "workflow", *argv], cwd=repo, env=self.env,
                capture_output=True, text=True,
            )
            if ok and done.returncode != 0:
                sys.exit(f"{self.name}: workflow {argv}: {done.stdout}{done.stderr}")
            return done.stdout + done.stderr

        def newest_workflow(self) -> str:
            listing = self.workflow("list")
            ids = [line.split()[0] for line in listing.splitlines()
                   if line.startswith("wf-")]
            new = [wf for wf in ids if wf not in self.workflow_ids]
            if len(new) != 1:
                sys.exit(f"{self.name}: expected one new workflow, found {new}")
            self.workflow_ids.append(new[0])
            return new[0]

        def plan(self) -> str:
            self.prepare()
            self.workflow("plan", "fix add", "--planner", "codex")
            return self.newest_workflow()

    def settle(sc: Scenario, timeout: float = 60) -> None:
        """Wait until no process holds the workflow or job locks."""
        locks = [*sc.project_dir.glob("workflows/.locks/*.lock"),
                 *(state / "tui").glob("*/locks/*.lock")]
        deadline = time.monotonic() + timeout
        while not all(_lock_free(lock) for lock in locks):
            if time.monotonic() > deadline:
                sys.exit(f"{sc.name}: a writer still holds a lock")
            time.sleep(0.2)

    def freeze(sc: Scenario) -> None:
        settle(sc)
        source = sc.project_dir
        target = out / sc.name
        shutil.copytree(source, target)
        # Nothing that a digest covers may carry this machine's paths; only
        # the named binding locations may.
        for path in target.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".patch", ".txt", ".log"}:
                if str(box).encode() in path.read_bytes() and path.name != "manifest.json" \
                        and "applications" not in path.parts:
                    sys.exit(f"{sc.name}: {path} carries the generating path")
        # Bindings from a scratch copy, so a crash's WAL is seen and the
        # frozen files are never opened.
        scratch = box / "bindings-read"
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.copytree(source / "workflows", scratch)
        with sqlite3.connect(scratch / "workflows.db") as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM workflows ORDER BY created_at, workflow_id"
            )]
        conn.close()
        bindings["scenarios"][sc.name] = {
            "project_id": sc.project,
            "workflow_ids": [row["workflow_id"] for row in rows],
            "states": {row["workflow_id"]: row["state"] for row in rows},
            "source_fingerprints": sorted({row["source_fingerprint"] for row in rows}),
            "lease_owners": sorted({row["lease_owner"] for row in rows if row.get("lease_owner")}),
            "final_run_ids": sorted({row["final_run_id"] for row in rows if row["final_run_id"]}),
            "run_artifacts": sorted(p.name for p in (source / "runs").glob("*/")
                                    if p.is_dir()) if (source / "runs").is_dir() else [],
            "wal": sorted(str(p.relative_to(source)) for p in source.rglob("*-wal")),
        }

    def job_state(workflow_id: str) -> dict:
        for spec in (state / "tui").glob("*/jobs/*/spec.json"):
            if workflow_id in spec.read_text():
                state_file = spec.with_name("state.json")
                if state_file.is_file():
                    return json.loads(state_file.read_text())
        return {}

    def run_until(sc: Scenario, workflow_id: str, marker: str) -> None:
        """Start a detached run, wait for an agent's start marker, then kill
        the job's worker group and the agent's own group."""
        sc.workflow("run", workflow_id, "--detach")
        started = home / "started" / marker
        deadline = time.monotonic() + 120
        while not started.exists():
            if time.monotonic() > deadline:
                sys.exit(f"{sc.name}: the {marker} agent never started")
            time.sleep(0.1)
        job = job_state(workflow_id)
        if not job.get("pgid"):
            sys.exit(f"{sc.name}: no job record for {workflow_id}")
        os.killpg(int(job["pgid"]), signal.SIGKILL)
        agent = int(started.read_text())
        try:
            os.killpg(os.getpgid(agent), signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 60
        while _alive(int(job["pid"])) or _alive(agent):
            if time.monotonic() > deadline:
                sys.exit(f"{sc.name}: the worker or its agent survived SIGKILL")
            time.sleep(0.1)

    # ── the lifecycle ────────────────────────────────────────────────────
    sc = Scenario("planned")
    sc.plan()
    freeze(sc)

    sc = Scenario("completed")
    workflow_id = sc.plan()
    sc.workflow("run", workflow_id)
    freeze(sc)

    # No gate, and neither node changes anything: an empty final patch.
    sc = Scenario("no-changes", gate=False, codex="none", kimi="none")
    workflow_id = sc.plan()
    sc.workflow("run", workflow_id)
    freeze(sc)

    # The final node's change fails the gate: a failing report, no artifact.
    sc = Scenario("gate-failed", kimi="wrong")
    workflow_id = sc.plan()
    sc.workflow("run", workflow_id, ok=False)
    freeze(sc)

    # The final node's only attempt fails.
    sc = Scenario("failed", kimi="fail")
    workflow_id = sc.plan()
    sc.workflow("run", workflow_id, ok=False)
    freeze(sc)

    sc = Scenario("cancelled")
    workflow_id = sc.plan()
    sc.workflow("cancel", workflow_id)
    freeze(sc)

    # The worker dies while the final node's agent works; then the
    # release's own `status` recovers the dead lease.
    sc = Scenario("running", kimi="hang")
    workflow_id = sc.plan()
    sc.prepare()
    run_until(sc, workflow_id, "kimi")
    freeze(sc)
    sc.name = "recovery-required"
    sc.workflow("status", workflow_id)
    freeze(sc)

    sc = Scenario("unapproved-completed")
    workflow_id = sc.plan()
    sc.prepare(crash_at="approve")
    sc.workflow("run", workflow_id, ok=False)
    (home / "crash-at").unlink()
    freeze(sc)

    sc = Scenario("unapproved-running")
    workflow_id = sc.plan()
    sc.prepare(crash_at="publish")
    sc.workflow("run", workflow_id, ok=False)
    (home / "crash-at").unlink()
    freeze(sc)

    if version != "0.2.0":
        # A plan from a document, then a revision of it: one lineage, two
        # plan versions, no planner call.
        sc = Scenario("created")
        document = box / "spec.json"
        document.write_text(json.dumps(SPEC))
        sc.prepare()
        sc.workflow("create", "--file", str(document))
        parent = sc.newest_workflow()
        freeze(sc)
        sc.name = "revised"
        document.write_text(json.dumps(REVISED))
        sc.workflow("revise", parent, "--file", str(document))
        sc.newest_workflow()
        freeze(sc)

    (out / "bindings.json").write_text(json.dumps(bindings, indent=2) + "\n")


if __name__ == "__main__":
    main()
