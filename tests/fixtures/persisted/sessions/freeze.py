"""Produce the frozen isolated-session documents under this directory (#271).

Run once per release, with THAT release's interpreter and source, never the
current one. The output directory must not exist; frozen fixtures are never
regenerated.

    git worktree add --detach /tmp/wt-v0.3.0 v0.3.0
    (cd /tmp/wt-v0.3.0 && uv sync --frozen --no-dev)
    python tests/fixtures/persisted/sessions/freeze.py \\
        --release-root /tmp/wt-v0.3.0 --out tests/fixtures/persisted/sessions/v0.3.0

The release's own `syncmill init` initializes one repository; its marker
(`.syncmill/project.json`) stays untracked and ignored, and `syncmill.toml` is
committed, so every session snapshots the same tree. Each session scenario is
its own project (`SYNCMILL_PROJECT__ID`), made with the release's
`session open codex --no-attach`, `session checkpoint` and `session close`.
`session open` launches a fake interactive `codex` in a real tmux. A `tmux` on
the fake PATH execs the real one with a private socket and no config, so the
user's tmux server is never touched; that private server is killed at the end.

Three states need a process to stop at one exact point. A `.pth` hook loaded
into the release venv for the run only (active only while HOME holds a
`crash-at` file) provides them:
- `creating`: exit on entry to the launch, after the `creating` document;
- `closing`: exit right after the `closing` document is written;
- `recovery-required`: the worktree cleanup reports failure.

Frozen for `project`: `syncmill.toml` and the marker directory's two files,
stored as `marker.json` and `marker.gitignore` (the latter, from v0.2.0, names
`project.json`, so Git would ignore the marker under its own name).

Frozen per session scenario: `<runs.root>/<project>/sessions/*.json` and the
project's `runs/` (checkpoint artifacts). Workspaces are not frozen: they are
Git repositories holding absolute paths. `bindings.json` records the values
bound to the generating machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TREE = {"calc.py": "def add(a, b):\n    return a - b\n"}
FIXED = "def add(a, b):\n    return a + b\n"

# The interactive agent `session open codex` launches in tmux: it repairs
# calc.py in its workspace, says so under HOME/started, then waits to be killed.
FAKE_CODEX = '''\
import os, sys, time
from pathlib import Path
from uuid import uuid4
if sys.argv[1:2] in (["--version"], ["--help"]) or sys.argv[1:3] == ["login", "status"]:
    print("codex (fake) - logged in"); sys.exit(0)
Path("calc.py").write_text(FIXED, encoding="utf-8")
started = Path(os.environ["HOME"]) / "started"
started.mkdir(exist_ok=True)
marker = started / uuid4().hex
marker.with_suffix(".tmp").write_text(os.getcwd(), encoding="utf-8")
os.replace(marker.with_suffix(".tmp"), marker)
time.sleep(3600)
'''.replace("FIXED", repr(FIXED))

CRASH_HOOK = '''\
import os
from pathlib import Path


def _install():
    flag = Path(os.environ.get("HOME", "/nonexistent")) / "crash-at"
    if not flag.is_file():
        return
    point = flag.read_text().strip()
    import syncmill.isolated_session as isolated

    if point == "create":
        def _create_session(*args, **kwargs):
            os._exit(0)  # the creating document is written; tmux never starts
        isolated._create_session = _create_session
    elif point == "closing":
        original = isolated.IsolatedSessionStore._write

        def _write(self, value, *args, **kwargs):
            original(self, value, *args, **kwargs)
            if value.get("state") == "closing":
                os._exit(0)  # closing recorded; tmux and the worktree untouched
        isolated.IsolatedSessionStore._write = _write
    elif point == "cleanup":
        isolated.cleanup_worktree = lambda *args, **kwargs: False


_install()
'''
HOOK_MODULE = "_syncmill_freeze_sessions_hook"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "frozen", "GIT_AUTHOR_EMAIL": "frozen@example.invalid",
    "GIT_COMMITTER_NAME": "frozen", "GIT_COMMITTER_EMAIL": "frozen@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}

#: scenario -> (state, revision) the release must leave its session in.
EXPECTED = {
    "open": ("open", 1),
    "checkpointed": ("open", 2),
    "closed": ("closed", 4),
    "creating": ("creating", 0),
    "closing": ("closing", 2),
    "recovery-required": ("recovery_required", 3),
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
    real_tmux = shutil.which("tmux")
    if real_tmux is None:
        sys.exit("a real tmux is required: the release launches the agent in one")
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

    # A tmux socket path may not exceed ~104 bytes: a short directory of its own.
    sockets = Path(tempfile.mkdtemp(prefix="smss.", dir="/tmp"))
    socket_path = sockets / "s"
    stopped = False
    try:
        hook_files[0].write_text(CRASH_HOOK, encoding="utf-8")
        hook_files[1].write_text(f"import {HOOK_MODULE}\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                _generate(Path(tmp).resolve(), real_tmux, socket_path, syncmill, version, out)
            finally:
                # Before the box goes: the agents run inside it.
                stopped = _stop_private_server(real_tmux, socket_path)
    finally:
        for path in hook_files:
            path.unlink(missing_ok=True)
        if stopped:
            shutil.rmtree(sockets, ignore_errors=True)
        else:
            sys.stderr.write(
                f"the private tmux server may still run; stop it with "
                f"`tmux -S {socket_path} kill-server`\n"
            )


def _stop_private_server(real_tmux: str, socket_path: Path) -> bool:
    """Kill the private server and prove it is gone: a server that answers
    `list-sessions` afterwards still runs agents."""
    subprocess.run([real_tmux, "-S", str(socket_path), "kill-server"], capture_output=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        probe = subprocess.run([real_tmux, "-S", str(socket_path), "list-sessions"],
                               capture_output=True)
        if probe.returncode != 0:
            return True
        time.sleep(0.2)
    return False


def _generate(box: Path, real_tmux: str, socket_path: Path, syncmill: Path, version: str,
              out: Path) -> None:
    repo, home, state, fakebin = (box / n for n in ("repo", "home", "state", "fakebin"))
    for folder in (repo, home, state, fakebin):
        folder.mkdir()
    (fakebin / "codex").write_text(f"#!{sys.executable}\n" + FAKE_CODEX, encoding="utf-8")
    (fakebin / "codex").chmod(0o755)
    (fakebin / "tmux").write_text(
        f'#!/bin/sh\nexec {real_tmux} -S {socket_path} -f /dev/null "$@"\n', encoding="utf-8"
    )
    (fakebin / "tmux").chmod(0o755)
    (fakebin / "python3").symlink_to(sys.executable)
    base_env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(fakebin), "/usr/bin", "/bin"]),
        "TMPDIR": str(box),
        "TERM": "xterm-256color",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SYNCMILL_AGENT_PRIORITY": json.dumps(["codex"]),
        "SYNCMILL_RUNS__ROOT": str(state / "runs"),
        "SYNCMILL_WORKSPACE__ROOT": str(state / "workspaces"),
        "SYNCMILL_MEMORY__DB_PATH": str(state / "memory" / "memtomem.db"),
        "SYNCMILL_MEMORY__MEMORY_DIR": str(state / "memory" / "files"),
        "SYNCMILL_BREAKER__STATE_PATH": str(state / "breaker.json"),
        "SYNCMILL_TUI__STATE_DIR": str(state / "tui"),
        **GIT_ENV,
    }

    def git(*argv: str) -> str:
        return subprocess.run(["git", *argv], cwd=repo, env=base_env, check=True,
                              capture_output=True, text=True).stdout

    def cli(env: dict, *argv: str, ok: bool = True) -> str:
        done = subprocess.run([str(syncmill), *argv], cwd=repo, env=env,
                              capture_output=True, text=True)
        if ok and done.returncode != 0:
            sys.exit(f"{argv}: {done.stdout}{done.stderr}")
        return done.stdout + done.stderr

    git("init", "-q", "-b", "main")
    for name, text in TREE.items():
        (repo / name).write_text(text, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "frozen tree")
    cli(base_env, "init")
    marker = repo / ".syncmill" / "project.json"
    marker_mode = stat.S_IMODE(marker.stat().st_mode)
    if marker_mode != 0o600:
        sys.exit(f"init wrote the marker with mode {oct(marker_mode)}")
    # The marker stays untracked and ignored; the config is committed. Every
    # session then snapshots the same tree, and HEAD trusts the marker.
    (repo / ".git" / "info" / "exclude").write_text(".syncmill/\n", encoding="utf-8")
    git("add", "syncmill.toml")
    git("commit", "-q", "-m", "syncmill init")
    if git("status", "--porcelain"):
        sys.exit("the initialized repository is not clean")

    out.mkdir(parents=True)
    # Stored under names Git acts on neither in the fixture tree: from v0.2.0
    # `init` writes `.syncmill/.gitignore` naming `project.json` itself.
    project_dir = out / "project"
    project_dir.mkdir()
    for relative, stored in (("syncmill.toml", "syncmill.toml"),
                             (".syncmill/project.json", "marker.json"),
                             (".syncmill/.gitignore", "marker.gitignore")):
        shutil.copyfile(repo / relative, project_dir / stored)
    bindings: dict = {
        "release": version,
        "box_root": str(box),
        "tree": {name: (repo / name).read_text(encoding="utf-8")
                 for name in (*TREE, "syncmill.toml")},
        "marker": {"id": json.loads(marker.read_text())["id"], "mode": oct(marker_mode)},
        "scenarios": {},
    }

    def env_for(name: str) -> dict:
        return {**base_env, "SYNCMILL_PROJECT__ID": f"frozen-sessions-{name}"}

    def sessions_dir(name: str) -> Path:
        return state / "runs" / f"frozen-sessions-{name}" / "sessions"

    def open_session(name: str, *, crash_at: str | None = None) -> str:
        flag = home / "crash-at"
        flag.unlink(missing_ok=True)
        if crash_at:
            flag.write_text(crash_at)
        started = set((home / "started").glob("*")) if (home / "started").is_dir() else set()
        cli(env_for(name), "session", "open", "codex", "--no-attach", ok=crash_at is None)
        flag.unlink(missing_ok=True)
        [document] = sessions_dir(name).glob("*.json")
        session_id = document.stem
        if crash_at is None:
            deadline = time.monotonic() + 30
            while not (set((home / "started").glob("*")) - started
                       if (home / "started").is_dir() else set()):
                if time.monotonic() > deadline:
                    sys.exit(f"{name}: the fake codex never started")
                time.sleep(0.1)
        return session_id

    def crash(name: str, point: str, *argv: str) -> None:
        (home / "crash-at").write_text(point)
        cli(env_for(name), *argv, ok=False)
        (home / "crash-at").unlink()

    def freeze(name: str, session_id: str) -> None:
        source = state / "runs" / f"frozen-sessions-{name}"
        target = out / name
        (target / "sessions").mkdir(parents=True)
        document = sessions_dir(name) / f"{session_id}.json"
        shutil.copyfile(document, target / "sessions" / document.name)
        if (source / "runs").is_dir():
            shutil.copytree(source / "runs", target / "runs")
        value = json.loads(document.read_text())
        expected = EXPECTED[name]
        if (value["state"], value["revision"]) != expected:
            sys.exit(f"{name}: left {value['state']}/{value['revision']}, not {expected}")
        bindings["scenarios"][name] = {
            "project_id": f"frozen-sessions-{name}",
            "session_id": session_id,
            "agent_id": value["agent_id"],
            "state": value["state"],
            "revision": value["revision"],
            "source_fingerprint": value["source_fingerprint"],
            "checkpoints": list(value["checkpoints"]),
        }

    # ── the lifecycle ────────────────────────────────────────────────────
    freeze("open", open_session("open"))

    session_id = open_session("checkpointed")
    cli(env_for("checkpointed"), "session", "checkpoint", session_id)
    freeze("checkpointed", session_id)

    session_id = open_session("closed")
    cli(env_for("closed"), "session", "checkpoint", session_id)
    cli(env_for("closed"), "session", "close", session_id)
    freeze("closed", session_id)

    freeze("creating", open_session("creating", crash_at="create"))

    session_id = open_session("closing")
    crash("closing", "closing", "session", "close", session_id)
    freeze("closing", session_id)

    session_id = open_session("recovery-required")
    crash("recovery-required", "cleanup", "session", "close", session_id)
    freeze("recovery-required", session_id)

    (out / "bindings.json").write_text(json.dumps(bindings, indent=2) + "\n")


if __name__ == "__main__":
    main()
