"""Produce the frozen artifact documents under this directory (#271).

Run once per release, with THAT release's interpreter and source, never the
current one: the point of the fixtures is that the current writer did not make
them. Re-running against a newer release adds a directory; it never rewrites an
existing one (the output directory must not exist).

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev)
    python tests/fixtures/persisted/artifacts/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --out tests/fixtures/persisted/artifacts/v0.3.0

Each scenario comes from the released CLI against a fake `codex` binary:
`run`, `apply`, `discard`, `continue`, `conversations close`, and `run` stopped
by SIGKILL (`running`) or SIGINT (`recovery_required`) while its agent works.
The two `applying_*` scenarios need a process death at one exact manifest
write, which no CLI exposes: they call the released `ArtifactStore.apply` in
the released interpreter and exit the process there, either right after the
`applying` record (no receipt, source untouched) or right before the `applied`
flip (receipt written, patch in the source). Each run directory is copied byte
for byte as soon as its step finishes. `bindings.json` records the values bound
to the generating machine (repo root, source fingerprint) so the tests can
rebind them to their own repository; nothing in the copied files is edited.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import signal
import tempfile
import time
from pathlib import Path

PROJECT_ID = "frozen-artifacts"
TREE = {"calc.py": "def add(a, b):\n    return a - b\n"}
FIXED = {"calc.py": "def add(a, b):\n    return a + b\n"}

FAKE_CODEX = '''\
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["login", "status"] or args[:1] in (["--version"], ["--help"]):
    print("codex (fake) - logged in"); sys.exit(0)
mode = Path(os.environ["FAKE_MODE_FILE"]).read_text().strip()
if mode == "hang":
    marker = Path(os.environ["FAKE_STARTED"])
    marker.with_suffix(".tmp").write_text(str(os.getpid()))
    os.replace(marker.with_suffix(".tmp"), marker)  # never seen half-written
    import time; time.sleep(60); sys.exit(1)
if mode == "fail":
    sys.stderr.write("fake codex: refused\\n"); sys.exit(1)
if mode == "fix":
    for name, text in json.loads(os.environ["FAKE_FIXED"]).items():
        Path(name).write_text(text, encoding="utf-8")
message = {"fix": "Fixed add.", "nochange": "Nothing to change."}[mode]
for event in (
    {"type": "thread.started", "thread_id": "fake-codex-thread"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
):
    print(json.dumps(event))
'''

# Runs inside the released interpreter: apply, then die at one manifest write.
CRASH_APPLY = '''\
import os, sys
from pathlib import Path
from syncmill.artifacts import ArtifactStore
from syncmill.worktree import repository_fingerprint
runs_root, project, run_id, repo, die_at = sys.argv[1:6]
original = ArtifactStore._compare_and_swap_manifest
def die(self, run_dir, expected, replacement):
    state = replacement.get("state")
    if die_at == "before-applied" and state == "applied":
        os._exit(0)  # receipt written, patch in the source, flip never happens
    original(self, run_dir, expected, replacement)
    if die_at == "after-applying" and state == "applying":
        os._exit(0)  # applying recorded, source not yet touched, no receipt
ArtifactStore._compare_and_swap_manifest = die
store = ArtifactStore(Path(runs_root), project)
store.apply(run_id, Path(repo), repository_fingerprint(Path(repo)))
sys.exit("apply returned; the crash hook never fired")
'''

GIT_ENV = {
    "GIT_AUTHOR_NAME": "frozen", "GIT_AUTHOR_EMAIL": "frozen@example.invalid",
    "GIT_COMMITTER_NAME": "frozen", "GIT_COMMITTER_EMAIL": "frozen@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _runs(store: Path) -> set[str]:
    return {p.name for p in store.iterdir() if p.is_dir()} if store.exists() else set()


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
        repo, home, state, fakebin = (box / n for n in ("repo", "home", "state", "fakebin"))
        for folder in (repo, home, state, fakebin):
            folder.mkdir()
        (fakebin / "codex").write_text(
            f"#!{sys.executable}\n" + FAKE_CODEX, encoding="utf-8"
        )
        (fakebin / "codex").chmod(0o755)
        mode_file = box / "mode"
        started = box / "agent-started"
        env = {
            "HOME": str(home),
            "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
            "TMPDIR": str(box),
            "GIT_CONFIG_NOSYSTEM": "1",
            "FAKE_MODE_FILE": str(mode_file),
            "FAKE_STARTED": str(started),
            "FAKE_FIXED": json.dumps(FIXED),
            "SYNCMILL_PROJECT__ID": PROJECT_ID,
            "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex"]),
            "SYNCMILL_CLAUDE__ENABLED": "false",
            "SYNCMILL_RUNS__ROOT": str(state / "runs"),
            "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
            "SYNCMILL_MEMORY__DB_PATH": str(state / "memory" / "memtomem.db"),
            "SYNCMILL_MEMORY__MEMORY_DIR": str(state / "memory" / "files"),
            "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
            **GIT_ENV,
        }

        def git(*argv: str) -> None:
            subprocess.run(["git", *argv], cwd=repo, env=env, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        for name, text in TREE.items():
            (repo / name).write_text(text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "frozen tree")

        store = state / "runs" / PROJECT_ID / "runs"
        out.mkdir(parents=True)
        bindings: dict = {
            "release": version,
            "project_id": PROJECT_ID,
            "repo_root": str(repo),
            "tree": TREE,
            "runs": {},
        }

        def freeze(scenario: str, run_id: str) -> None:
            # Copied at once: a later step (an apply's reconcile pass, a
            # continue's supersede) must not reach a document already frozen.
            manifest = json.loads((store / run_id / "manifest.json").read_text())
            bindings["runs"][scenario] = {
                "run_id": run_id,
                "source_fingerprint": manifest["source_fingerprint"],
            }
            shutil.copytree(store / run_id, out / scenario)

        def syncmill_cli(*argv: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [str(syncmill), *argv], cwd=repo, env=env, capture_output=True, text=True,
            )

        def new_run(before: set[str], what: str) -> str:
            new = sorted(_runs(store) - before)
            if len(new) != 1:
                sys.exit(f"{what}: expected one new run, got {new}")
            return new[0]

        def run(mode: str) -> str:
            mode_file.write_text(mode)
            before = _runs(store)
            done = syncmill_cli("run", "fix add", "-a", "codex")
            return new_run(before, f"run {mode}: {done.stderr}")

        def interrupted(sig: int) -> str:
            # The released `run`, stopped while its agent is working.
            mode_file.write_text("hang")
            started.unlink(missing_ok=True)
            before = _runs(store)
            proc = subprocess.Popen(
                [str(syncmill), "run", "fix add", "-a", "codex"], cwd=repo, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            agent_pid = None
            try:
                deadline = time.monotonic() + 60
                while not started.exists():
                    if time.monotonic() > deadline or proc.poll() is not None:
                        sys.exit("the fake agent never started")
                    time.sleep(0.1)
                agent_pid = int(started.read_text())
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
            return new_run(before, f"interrupted run ({sig})")

        def crash_apply(run_id: str, die_at: str) -> None:
            subprocess.run(
                [str(python), "-c", CRASH_APPLY, str(state / "runs"), PROJECT_ID,
                 run_id, str(repo), die_at],
                cwd=repo, env=env, check=True,
            )

        def must(done: subprocess.CompletedProcess) -> None:
            if done.returncode != 0:
                sys.exit(f"{done.args}: {done.stderr}")

        for scenario, mode in (("pending", "fix"), ("failed", "fail"), ("no_changes", "nochange")):
            freeze(scenario, run(mode))

        run_id = run("fix")
        must(syncmill_cli("conversations", "close", run_id))
        freeze("closed", run_id)

        run_id = run("fix")
        must(syncmill_cli("discard", run_id))
        freeze("discarded", run_id)

        run_id = run("fix")
        before = _runs(store)
        must(syncmill_cli("continue", run_id, "and test it"))
        freeze("superseded", run_id)
        freeze("continued", new_run(before, "continue"))

        freeze("running", interrupted(signal.SIGKILL))
        freeze("recovery_required", interrupted(signal.SIGINT))

        run_id = run("fix")
        crash_apply(run_id, "after-applying")
        freeze("applying_unreceipted", run_id)

        run_id = run("fix")
        crash_apply(run_id, "before-applied")
        freeze("applying_receipted", run_id)
        git("checkout", "--", ".")

        run_id = run("fix")
        must(syncmill_cli("apply", run_id))
        freeze("applied", run_id)

        (out / "bindings.json").write_text(json.dumps(bindings, indent=2) + "\n")


if __name__ == "__main__":
    main()
