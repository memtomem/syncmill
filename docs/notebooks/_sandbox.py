"""Disposable sandbox for the syncmill hands-on notebooks.

Every notebook drives the real ``syncmill`` CLI through ``subprocess`` — the
same surface a user types into a shell — but inside a throwaway directory:

    <root>/                      tempfile.mkdtemp(prefix="syncmill-notebook.")
        .syncmill-notebook       ownership marker; cleanup() refuses without it
        demo-repo/               a tiny Git repository the agents work on
        home/                    HOME for fake mode (all ``~`` state lands here)
        state/                   explicit syncmill state paths (runs, memory, ...)
        fakebin/                 ``codex`` / ``kimi`` launchers + a ``git`` link
        fakes/                   the Python source each launcher runs
        scratch/                 a non-repository folder for raw agent demos

Fake mode (the default) needs no login and costs nothing. It builds the child
environment from an allowlist instead of inheriting yours, so ambient
``SYNCMILL_*`` / ``MEMTOMEM_*`` / ``GIT_*`` settings, your global Git config and
hooks, and any real agent binary on your PATH can never take part.

Real mode (``real=True``) is for the one optional cell that calls an actual
provider CLI. Provider logins live in your real home directory, so that mode
keeps your HOME and environment; only the syncmill state paths stay pinned
inside the sandbox.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import secrets
from dataclasses import dataclass, field
from pathlib import Path

MARKER = ".syncmill-notebook"
PREFIX = "syncmill-notebook."
_ROOT_NAME = re.compile(r"^syncmill-notebook\.[A-Za-z0-9_]+$")
_RUN_LINE = re.compile(r"^\[syncmill\] run: ([0-9a-f-]{36})\s*$", re.MULTILINE)

#: Text syncmill puts in a phase's instruction, used to label logged prompts.
#: tests/test_notebooks.py pins each marker against the real prompt builders.
PHASE_MARKERS = (
    ("council: propose", "Draft and implement your best solution"),
    ("council: critique", "Critique those ideas"),
    ("council: synthesize", "Synthesize them into a single best FINAL solution"),
    ("decompose: plan", "You are the planner"),
    ("decompose: subtask", "YOUR subtask to implement now:"),
    ("decompose: synthesize", "Integrate that work"),
)

#: The agent ids the fakes impersonate, mapped to the binary syncmill calls.
FAKE_AGENTS = {"codex": "codex", "kimi-code": "kimi"}

# Ambient variables worth keeping in fake mode: locale only. Everything else
# is rebuilt from scratch below.
_KEPT_AMBIENT = ("LANG", "LC_ALL", "LC_CTYPE")

# Variables real mode must still drop: the ones that would point syncmill or
# Git somewhere other than the sandbox.
_REAL_MODE_DROP_PREFIXES = ("SYNCMILL_", "GIT_")

_FAKE_AGENT_SOURCE = '''\
"""Fake `{binary}` CLI for the syncmill notebooks (agent id: {agent_id}).

It speaks just enough of the real CLI's output format for syncmill to parse it:
it writes one file into its working directory (the isolated worktree syncmill
gives it) and prints one JSON event per line on stdout.
"""
import json, os, sys, time
from pathlib import Path

AGENT_ID = {agent_id!r}
BINARY = {binary!r}
TARGET = "NOTES-" + BINARY + ".md"
args = sys.argv[1:]

if args[:2] == ["login", "status"] or args[:1] in (["--version"], ["--help"]):
    print(BINARY + " (fake) - logged in")
    sys.exit(0)

if BINARY == "codex":
    # codex exec [-m SEL] [-c K=V ...] [resume SESSION] PROMPT --json
    prompt = args[-2] if len(args) >= 2 and args[-1] == "--json" else ""
else:
    # kimi [-m SEL] -p PROMPT --output-format stream-json [--session ID]
    prompt = args[args.index("-p") + 1] if "-p" in args else ""

log_dir = os.environ.get("FAKE_PROMPT_LOG_DIR")
if log_dir:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    name = "%s-%d.txt" % (AGENT_ID, time.time_ns())
    Path(log_dir, name).write_text(prompt, encoding="utf-8")

if os.environ.get("FAKE_" + BINARY.upper() + "_FAIL") == "1":
    print(BINARY + " (fake): simulated failure", file=sys.stderr)
    sys.exit(1)

# A scenario file (written by Sandbox.set_scenario) can say exactly which
# files this agent writes: one step, or a list of steps where the first whose
# "when" text appears in the prompt wins. Otherwise, append to the notes file.
step = None
scenario = os.environ.get("FAKE_SCENARIO")
if scenario and os.path.isfile(scenario):
    with open(scenario, encoding="utf-8") as handle:
        entry = json.load(handle).get(AGENT_ID)
    if isinstance(entry, list):
        step = next((rule for rule in entry if rule.get("when", "") in prompt), None)
    else:
        step = entry
if step is not None:
    for name, text in step.get("files", {{}}).items():
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        Path(name).write_text(text, encoding="utf-8")
    message = step.get("message", AGENT_ID + " (fake) followed the scenario.")
else:
    with open(TARGET, "a", encoding="utf-8") as handle:
        handle.write("- " + AGENT_ID + " wrote this line.\\n")
    message = AGENT_ID + " (fake) appended one line to " + TARGET + "."

if BINARY == "codex":
    events = [
        {{"type": "thread.started", "thread_id": "fake-codex-thread"}},
        {{"type": "item.completed",
          "item": {{"type": "agent_message", "text": message}}}},
        {{"type": "turn.completed",
          "usage": {{"input_tokens": len(prompt), "output_tokens": len(message)}}}},
    ]
else:
    events = [
        {{"role": "assistant", "content": message}},
        {{"role": "meta", "type": "session.resume_hint", "session_id": "fake-kimi-session",
          "content": "To resume this session: kimi -r fake-kimi-session"}},
    ]
for event in events:
    print(json.dumps(event, ensure_ascii=False))
'''


def fake_agent_source(agent_id: str) -> str:
    """Return the Python source of the fake CLI that impersonates ``agent_id``."""
    return _FAKE_AGENT_SOURCE.format(agent_id=agent_id, binary=FAKE_AGENTS[agent_id])


def write_fake_agent(
    launchers: Path, sources: Path, agent_id: str, python: str | None = None
) -> Path:
    """Install the fake for ``agent_id``: its source plus a ``/bin/sh`` launcher.

    The launcher, not a ``#!`` line, names the interpreter: a shebang cannot
    quote, so an interpreter path containing a space would break it.
    """
    binary = FAKE_AGENTS[agent_id]
    source = sources / f"{binary}.py"
    source.write_text(fake_agent_source(agent_id), encoding="utf-8")
    launcher = launchers / binary
    launcher.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(python or sys.executable)} {shlex.quote(str(source))} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def syncmill_executable() -> Path:
    """The ``syncmill`` entry point installed next to this Python."""
    path = Path(sys.executable).parent / "syncmill"
    if not path.exists():
        raise RuntimeError(
            f"syncmill is not installed next to {sys.executable}; start Jupyter "
            "with `uv run --with jupyterlab jupyter lab` from the syncmill source."
        )
    return path


class SandboxError(RuntimeError):
    """Raised when the sandbox refuses an unsafe operation."""


class Result(subprocess.CompletedProcess):
    """A finished command whose notebook echo is one short line, not its argv.

    The argv starts with an absolute interpreter path inside the user's home
    directory; the default ``CompletedProcess`` repr would print it under every
    cell that ends in a call.
    """

    label = ""

    @classmethod
    def wrap(cls, done: subprocess.CompletedProcess, label: str) -> "Result":
        result = cls(done.args, done.returncode, done.stdout, done.stderr)
        result.label = label
        return result

    def __repr__(self) -> str:
        return f"<{self.label}: exit {self.returncode}>"


@dataclass
class Sandbox:
    root: Path
    base: Path
    # Proof of ownership: set only by create(), and written into the marker.
    # A Sandbox built by hand around some other directory has no token, so
    # cleanup() refuses it no matter what that directory contains.
    _token: str | None = field(default=None, init=False, repr=False)

    # ── construction ────────────────────────────────────────────────────

    @classmethod
    def create(cls, base: str | os.PathLike[str] = "/tmp") -> "Sandbox":
        base_path = Path(base)
        root = Path(tempfile.mkdtemp(prefix=PREFIX, dir=base_path))
        box = cls(root=root, base=base_path)
        box._token = secrets.token_hex(16)
        (root / MARKER).write_text(box._token + "\n")
        for folder in (box.repo, box.home, box.state, box.fakebin, box.fakes, box.scratch):
            folder.mkdir()
        box._install_fakes()
        box._init_repo()
        return box

    @property
    def repo(self) -> Path:
        return self.root / "demo-repo"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def fakebin(self) -> Path:
        return self.root / "fakebin"

    @property
    def fakes(self) -> Path:
        return self.root / "fakes"

    @property
    def scratch(self) -> Path:
        return self.root / "scratch"

    @property
    def prompt_log(self) -> Path:
        return self.state / "prompts"

    def _install_fakes(self) -> None:
        for agent_id in FAKE_AGENTS:
            write_fake_agent(self.fakebin, self.fakes, agent_id)
        # Gate commands run with this sandbox's PATH, so give them a `python3`
        # that is the notebook's own interpreter (quoted, like the fakes).
        python3 = self.fakebin / "python3"
        python3.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        python3.chmod(0o755)
        git = shutil.which("git")
        if git is None:
            raise SandboxError("git was not found on PATH")
        (self.fakebin / "git").symlink_to(git)

    def _init_repo(self) -> None:
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Syncmill Notebook")
        self.git("config", "user.email", "notebook@example.invalid")
        (self.repo / "README.md").write_text("# Notebook demo\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "initial notebook fixture")

    # ── environments ────────────────────────────────────────────────────

    def _state_pins(self) -> dict[str, str]:
        s = self.state
        return {
            "SYNCMILL_REPO_ROOT": str(self.repo),
            "SYNCMILL_WORKSPACE__ROOT": str(s / "workspaces"),
            "SYNCMILL_RUNS__ROOT": str(s / "runs"),
            "SYNCMILL_TUI__STATE_DIR": str(s / "tui"),
            "SYNCMILL_MEMORY__DB_PATH": str(s / "memory" / "memtomem.db"),
            "SYNCMILL_MEMORY__MEMORY_DIR": str(s / "memory" / "files"),
            "SYNCMILL_BOARD__DB_PATH": str(s / "board.db"),
            "SYNCMILL_BREAKER__STATE_PATH": str(s / "breaker.json"),
            "SYNCMILL_INTEGRATION__REPORT__RUNS_DIR": str(s / "reports"),
            "SYNCMILL_ROOMS__ENABLED": "false",
        }

    def env(self, *, real: bool = False) -> dict[str, str]:
        """The environment every child process of this sandbox receives."""
        if real:
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(_REAL_MODE_DROP_PREFIXES)
            }
        else:
            env = {k: os.environ[k] for k in _KEPT_AMBIENT if k in os.environ}
            home = self.home
            env.update(
                {
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_CACHE_HOME": str(home / ".cache"),
                    "XDG_DATA_HOME": str(home / ".local" / "share"),
                    "PATH": os.pathsep.join([str(self.fakebin), "/usr/bin", "/bin"]),
                    "TMPDIR": str(self.root / "tmp"),
                    "SYNCMILL_CLAUDE__ENABLED": "false",
                    "FAKE_PROMPT_LOG_DIR": str(self.prompt_log),
                    "FAKE_SCENARIO": str(self.scenario_path),
                }
            )
            (self.root / "tmp").mkdir(exist_ok=True)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env.update(self._state_pins())
        return env

    # ── running things ──────────────────────────────────────────────────

    def sm(
        self,
        *args: str,
        real: bool = False,
        show: bool = True,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
        timeout: float = 300,
    ) -> Result:
        """Run ``syncmill <args>`` against the demo repository.

        A non-zero exit raises ``SandboxError`` (after the output is shown)
        unless ``check=False``, so a notebook never carries on past a failure.
        """
        env = self.env(real=real)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [str(syncmill_executable()), *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if show:
            _show(result)
        if check and result.returncode != 0:
            raise SandboxError(f"syncmill {' '.join(args[:1])} exited {result.returncode}")
        return Result.wrap(result, f"syncmill {args[0]}" if args else "syncmill")

    def git(self, *args: str, show: bool = False) -> Result:
        """Run ``git <args>`` in the demo repository with the sandbox env."""
        result = subprocess.run(
            [str(self.fakebin / "git"), *args],
            cwd=self.repo,
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
        )
        if show:
            _show(result)
        if result.returncode != 0:
            raise SandboxError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return Result.wrap(result, f"git {args[0]}" if args else "git")

    def cmd(self, *args: str, show: bool = True) -> Result:
        """Run any other command (e.g. ``python3 check.py``) in the demo repository."""
        result = subprocess.run(
            list(args), cwd=self.repo, env=self.env(), capture_output=True, text=True
        )
        if show:
            _show(result)
        return Result.wrap(result, args[0] if args else "cmd")

    def run_fake(self, binary: str, *args: str, show: bool = True) -> Result:
        """Call a fake agent CLI directly, in ``scratch/`` (never the repo)."""
        env = self.env()
        env.pop("FAKE_PROMPT_LOG_DIR")  # only prompts syncmill sends are logged
        result = subprocess.run(
            [str(self.fakebin / binary), *args],
            cwd=self.scratch,
            env=env,
            capture_output=True,
            text=True,
        )
        if show:
            _show(result)
        return Result.wrap(result, binary)

    # ── reading results ─────────────────────────────────────────────────

    @staticmethod
    def run_id(result: subprocess.CompletedProcess) -> str:
        """The run id from a ``syncmill run`` result's ``[syncmill] run:`` line."""
        found = _RUN_LINE.findall(result.stdout)
        if len(found) != 1:
            raise SandboxError(f"expected one run id line, found {len(found)}")
        return found[0]

    @property
    def scenario_path(self) -> Path:
        return self.state / "fake-scenario.json"

    def set_scenario(self, scenario: dict | None) -> None:
        """Tell the fakes what to write: ``{agent_id: {"files": {...}, "message": ...}}``.

        The value may also be a list of such steps, each with a ``"when"`` text;
        the first step whose ``when`` appears in the prompt is used, which lets
        one agent behave differently in each phase of a strategy. An agent with
        no matching step (or ``None`` for all) appends one line to its notes file.
        """
        if scenario is None:
            self.scenario_path.unlink(missing_ok=True)
        else:
            self.scenario_path.write_text(
                json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def init_project(self) -> Result:
        """``syncmill init`` and commit the config, as notebook 01 does step by step."""
        result = self.sm("init", show=False)
        self.git("add", "syncmill.toml", ".syncmill/.gitignore")
        self.git("commit", "-qm", "initialize syncmill")
        return result

    def calls(self, since: int = 0) -> list[dict[str, str]]:
        """The agent calls syncmill made, oldest first, each labelled by phase.

        ``since`` is a count from an earlier ``len(box.prompts())``. The phase is
        recognised from the instruction syncmill wraps around the task (see
        PHASE_MARKERS); ``task`` is what follows the last ``Task: ``.
        """
        rows = []
        for path in self.prompts()[since:]:
            text = path.read_text(encoding="utf-8")
            phase = next((name for name, marker in PHASE_MARKERS if marker in text), "task")
            rows.append({
                "agent": path.name.rsplit("-", 1)[0],
                "phase": phase,
                "context": "yes" if text.startswith("Context from previous work:") else "no",
                "task": text.rpartition("Task: ")[2],
            })
        return rows

    def prompts(self) -> list[Path]:
        """Prompt files the fake agents logged, oldest first."""
        if not self.prompt_log.is_dir():
            return []
        return sorted(self.prompt_log.iterdir(), key=lambda p: p.name.split("-")[-1])

    def tree(self) -> None:
        """Print the sandbox layout two levels deep."""
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if len(rel.parts) <= 2 and ".git" not in rel.parts:
                print(("  " * (len(rel.parts) - 1)) + rel.name + ("/" if path.is_dir() else ""))

    # ── cleanup ─────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Delete the sandbox, but only if it is provably the one we created."""
        root = self.root
        if self._token is None:
            raise SandboxError(f"refusing to remove a sandbox this object did not create: {root}")
        if root.is_symlink():
            raise SandboxError(f"refusing to remove a symlink: {root}")
        if os.path.realpath(root.parent) != os.path.realpath(self.base):
            raise SandboxError(f"refusing to remove outside {self.base}: {root}")
        if not _ROOT_NAME.match(root.name):
            raise SandboxError(f"refusing to remove an unexpected name: {root}")
        marker = root / MARKER
        if not marker.is_file() or marker.read_text().strip() != self._token:
            raise SandboxError(f"refusing to remove without our {MARKER}: {root}")
        shutil.rmtree(root)


def _show(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
    if result.returncode != 0:
        print(f"(exit code {result.returncode})")


def jsonl(text: str) -> list[dict]:
    """Parse JSON-per-line output, skipping lines that are not JSON objects."""
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
