"""Offline trial behind ``syncmill start --trial`` (#171).

A five-minute walkthrough of the whole loop — change, verify, compare, diff,
explicit apply — that needs no login, no real provider and no source checkout.
It drives the real ``syncmill`` CLI as child processes, exactly as a user would,
inside a throwaway folder:

    <tmp>/syncmill-trial.XXXX/
        .syncmill-trial      ownership marker; cleanup refuses without it
        demo-repo/           a tiny Git repository whose test fails
        home/                HOME for every child (all ``~`` state lands here)
        state/               explicit syncmill state paths
        fakebin/             fake ``codex``/``kimi`` launchers, ``python``, ``git``
        fakes/               the Python source each fake runs
        tmp/                 TMPDIR for every child

The children get an environment built from an allowlist, never the user's:
ambient ``SYNCMILL_*``/``MEMTOMEM_*``/``GIT_*`` settings, the user's Git config
and hooks, and any real agent binary on PATH cannot take part. Adapted from the
notebook sandbox (``docs/notebooks/_sandbox.py``), trimmed to this one scenario.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "syncmill-trial."
MARKER = ".syncmill-trial"
_ROOT_NAME = re.compile(r"^syncmill-trial\.[A-Za-z0-9_]+$")

#: The trial's one verification command. Standard library only, so nothing
#: needs installing; ``python`` is a shim for the interpreter running syncmill.
GATE_COMMAND = "python -m unittest"
TASK = "Fix add() in calc.py so the tests in test_calc.py pass."

#: Agent id -> binary syncmill calls for it.
FAKE_AGENTS = {"codex": "codex", "kimi-code": "kimi"}

_BUGGY = "def add(a, b):\n    return a + b + 1\n"
_TEST = (
    "import unittest\n\nfrom calc import add\n\n\n"
    "class AddTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 2), 4)\n"
    "        self.assertEqual(add(1, 2), 3)\n\n\n"
    'if __name__ == "__main__":\n'
    "    unittest.main()\n"
)
#: What each fake writes: codex fixes the bug; kimi-code takes a shortcut that
#: satisfies the first assertion and fails the second — the case verification
#: exists for.
SCENARIO = {
    "codex": {
        "files": {"calc.py": "def add(a, b):\n    return a + b\n"},
        "message": "Removed the stray + 1 from add().",
    },
    "kimi-code": {
        "files": {"calc.py": "def add(a, b):\n    return 4\n"},
        "message": "Made add(2, 2) return 4.",
    },
}

# Ambient variables worth keeping: locale only. Everything else is rebuilt.
_KEPT_AMBIENT = ("LANG", "LC_ALL", "LC_CTYPE")
_STEP_TIMEOUT_SECONDS = 180

_FAKE_AGENT_SOURCE = '''\
"""Fake `{binary}` CLI for the syncmill trial (agent id: {agent_id}).

It speaks just enough of the real CLI's output format for syncmill to parse:
it writes the files the trial scenario names into its working directory (the
isolated worktree syncmill gives it) and prints JSON events on stdout.
"""
import json, os, sys
from pathlib import Path

AGENT_ID = {agent_id!r}
BINARY = {binary!r}
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

step = {{}}
scenario = os.environ.get("FAKE_SCENARIO")
if scenario and os.path.isfile(scenario):
    with open(scenario, encoding="utf-8") as handle:
        step = json.load(handle).get(AGENT_ID) or {{}}
for name, text in step.get("files", {{}}).items():
    Path(name).parent.mkdir(parents=True, exist_ok=True)
    Path(name).write_text(text, encoding="utf-8")
message = step.get("message", AGENT_ID + " (fake) made no change.")

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


class TrialError(RuntimeError):
    """A trial step failed, or the sandbox refused an unsafe operation."""


def fake_agent_source(agent_id: str) -> str:
    """The Python source of the fake CLI that impersonates ``agent_id``."""
    return _FAKE_AGENT_SOURCE.format(agent_id=agent_id, binary=FAKE_AGENTS[agent_id])


def _launcher(path: Path, argv: list[str]) -> None:
    """A ``/bin/sh`` launcher, not a ``#!`` line: a shebang cannot quote, so an
    interpreter path containing a space would break it."""
    path.write_text(
        "#!/bin/sh\nexec " + " ".join(shlex.quote(a) for a in argv) + ' "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


@dataclass
class TrialSandbox:
    root: Path
    base: Path
    # Proof of ownership: set only by create() and written into the marker. A
    # sandbox built by hand around another directory has no token, so cleanup()
    # refuses it whatever that directory contains.
    _token: str | None = field(default=None, init=False, repr=False)

    @classmethod
    def create(cls, base: str | os.PathLike[str] | None = None) -> "TrialSandbox":
        base_path = Path(base if base is not None else tempfile.gettempdir())
        root = Path(tempfile.mkdtemp(prefix=PREFIX, dir=base_path))
        box = cls(root=root, base=base_path)
        box._token = secrets.token_hex(16)
        (root / MARKER).write_text(box._token + "\n", encoding="utf-8")
        for folder in (box.repo, box.home, box.state, box.fakebin, box.fakes, box.tmp):
            folder.mkdir()
        box._install_tools()
        box.scenario_path.write_text(json.dumps(SCENARIO, indent=2), encoding="utf-8")
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
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def scenario_path(self) -> Path:
        return self.state / "scenario.json"

    def _install_tools(self) -> None:
        for agent_id, binary in FAKE_AGENTS.items():
            source = self.fakes / f"{binary}.py"
            source.write_text(fake_agent_source(agent_id), encoding="utf-8")
            _launcher(self.fakebin / binary, [sys.executable, str(source)])
        # The gate runs with this sandbox's PATH: give it the interpreter that
        # is running syncmill, under both common names.
        for name in ("python", "python3"):
            _launcher(self.fakebin / name, [sys.executable])
        git = shutil.which("git")
        if git is None:
            raise TrialError("git was not found on PATH; the trial needs it")
        (self.fakebin / "git").symlink_to(git)

    def env(self) -> dict[str, str]:
        """The environment every child of this sandbox receives."""
        env = {k: os.environ[k] for k in _KEPT_AMBIENT if k in os.environ}
        s = self.state
        env.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "PATH": os.pathsep.join([str(self.fakebin), "/usr/bin", "/bin"]),
            "TMPDIR": str(self.tmp),
            "GIT_CONFIG_NOSYSTEM": "1",
            # The gate imports calc.py; keep it from leaving __pycache__ behind
            # as an untracked file in the demo repository.
            "PYTHONDONTWRITEBYTECODE": "1",
            "FAKE_SCENARIO": str(self.scenario_path),
            "SYNCMILL_REPO_ROOT": str(self.repo),
            "SYNCMILL_AGENT_PRIORITY": json.dumps(list(FAKE_AGENTS)),
            "SYNCMILL_CLAUDE__ENABLED": "false",
            "SYNCMILL_ROOMS__ENABLED": "true",
            "SYNCMILL_GATE__COMMANDS": json.dumps([GATE_COMMAND]),
            "SYNCMILL_WORKSPACE__ROOT": str(s / "workspaces"),
            "SYNCMILL_RUNS__ROOT": str(s / "runs"),
            "SYNCMILL_TUI__STATE_DIR": str(s / "tui"),
            "SYNCMILL_MEMORY__DB_PATH": str(s / "memory" / "memtomem.db"),
            "SYNCMILL_MEMORY__MEMORY_DIR": str(s / "memory" / "files"),
            "SYNCMILL_BOARD__DB_PATH": str(s / "board.db"),
            "SYNCMILL_BREAKER__STATE_PATH": str(s / "breaker.json"),
            "SYNCMILL_INTEGRATION__REPORT__RUNS_DIR": str(s / "reports"),
        })
        return env

    def _run(self, argv: list[str], what: str) -> subprocess.CompletedProcess[str]:
        try:
            done = subprocess.run(
                argv, cwd=self.repo, env=self.env(), capture_output=True,
                text=True, timeout=_STEP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise TrialError(f"{what} did not finish in {_STEP_TIMEOUT_SECONDS}s") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip().splitlines()[-5:]
            raise TrialError(f"{what} failed (exit {done.returncode}):\n" + "\n".join(detail))
        return done

    def syncmill(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``syncmill <args>`` in the demo repository, as a user would."""
        argv = [sys.executable, "-c", "from syncmill.cli import cli; cli(prog_name='syncmill')"]
        return self._run([*argv, *args], f"syncmill {args[0]}")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run([str(self.fakebin / "git"), *args], f"git {args[0]}")

    def gate(self) -> subprocess.CompletedProcess[str]:
        """Run the verification command in the demo repository (may fail)."""
        return subprocess.run(
            shlex.split(GATE_COMMAND), cwd=self.repo, env=self.env(),
            capture_output=True, text=True, timeout=_STEP_TIMEOUT_SECONDS,
        )

    def init_repo(self) -> None:
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Syncmill Trial")
        self.git("config", "user.email", "trial@example.invalid")
        (self.repo / "calc.py").write_text(_BUGGY, encoding="utf-8")
        (self.repo / "test_calc.py").write_text(_TEST, encoding="utf-8")
        self.git("add", "calc.py", "test_calc.py")
        self.git("commit", "-qm", "add() has a bug")
        self.syncmill("init")
        self.git("add", "syncmill.toml", ".syncmill/.gitignore")
        self.git("commit", "-qm", "initialize syncmill")

    def cleanup(self) -> None:
        """Delete the sandbox, but only if it is provably the one we created."""
        root = self.root
        if self._token is None:
            raise TrialError(f"refusing to remove a folder this trial did not create: {root}")
        if root.is_symlink():
            raise TrialError(f"refusing to remove a symlink: {root}")
        if os.path.realpath(root.parent) != os.path.realpath(self.base):
            raise TrialError(f"refusing to remove outside {self.base}: {root}")
        if not _ROOT_NAME.match(root.name):
            raise TrialError(f"refusing to remove an unexpected name: {root}")
        marker = root / MARKER
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != self._token:
            raise TrialError(f"refusing to remove without our {MARKER}: {root}")
        shutil.rmtree(root)


@dataclass(frozen=True)
class Candidate:
    label: str
    candidate_id: str
    agent_id: str
    verdict: str
    eligible: bool
    files: tuple[str, ...]


def _candidates(compare: dict) -> tuple[str, list[Candidate]]:
    """The room id and the latest round's candidates from ``room compare --json``."""
    try:
        room_id = compare["room"]["room_id"]
        rows = compare["rounds"][-1]["candidates"]
        return room_id, [
            Candidate(
                label=chr(ord("A") + i),
                candidate_id=row["candidate_id"],
                agent_id=row["member_id"],
                verdict=(row.get("gate") or {}).get("verdict") or "not run",
                eligible=bool(row["eligible"]),
                files=tuple(row.get("files_changed") or ()),
            )
            for i, row in enumerate(rows)
        ]
    except (KeyError, IndexError, TypeError) as exc:
        raise TrialError(f"could not read the comparison: {exc!r}") from exc


@dataclass(frozen=True)
class TrialOutcome:
    applied: bool
    kept: Path | None


def run_trial(
    *,
    say: Callable[[str], None],
    confirm: Callable[[str], bool],
    keep: bool = False,
    base: str | os.PathLike[str] | None = None,
) -> TrialOutcome:
    """Walk through change → verify → compare → diff → explicit apply.

    ``say`` prints a block of text; ``confirm`` asks a yes/no question (the CLI
    passes ``click.confirm``, or an auto-yes when there is no terminal). The
    folder is removed afterwards unless ``keep``; on a failure it is kept so
    the error can be inspected, and its path is in the raised message.
    """
    box = TrialSandbox.create(base)
    try:
        say(
            "This walkthrough runs two fake agents on a tiny demo repository in a "
            "temporary folder.\nNo login, no real provider, and nothing outside "
            f"that folder: {box.root}\n"
        )
        box.init_repo()
        say(
            "1/5  The demo repository has a bug: add(2, 2) returns 5.\n"
            f"     Its test is the verification command: {GATE_COMMAND}\n"
        )
        room = box.syncmill(
            "room", "create", TASK, *(arg for a in FAKE_AGENTS for arg in ("-a", a))
        )
        room_id = room.stdout.split()[0]
        say(
            "2/5  Two agents (fake codex and fake kimi-code) each try a fix in their "
            "own isolated copy.\n     Your repository is never edited until you apply.\n"
        )
        box.syncmill("room", "round", room_id)
        _, candidates = _candidates(json.loads(box.syncmill("room", "compare", room_id, "--json").stdout))
        lines = [f"3/5  Each candidate was verified with: {GATE_COMMAND}"]
        for cand in candidates:
            status = "verification passed" if cand.verdict == "passed" else f"verification {cand.verdict.upper()}"
            lines.append(
                f"       {cand.label}  {cand.agent_id:<10} {status:<22} {', '.join(cand.files) or '-'}"
            )
        eligible = [c for c in candidates if c.eligible]
        if not eligible:
            raise TrialError("no candidate passed verification; the trial scenario is broken")
        chosen = eligible[0]
        rejected = [c.label for c in candidates if not c.eligible]
        if rejected:
            lines.append(
                f"     Only {chosen.label} can be applied: {', '.join(rejected)} changed the "
                "code but failed the test."
            )
        say("\n".join(lines) + "\n")
        diff = box.syncmill("room", "diff", chosen.candidate_id).stdout.rstrip()
        say(f"4/5  {chosen.label}'s change ({chosen.agent_id}):\n\n{diff}\n")
        if not confirm(f"Apply {chosen.label} to the demo repository?"):
            say("Nothing was applied. The demo repository still has the bug.\n")
            return TrialOutcome(applied=False, kept=_finish(box, keep, say))
        box.syncmill("room", "select", room_id, chosen.candidate_id)
        box.syncmill("room", "apply", room_id, "--verify")
        verified = box.gate()
        say(
            f"5/5  Applied {chosen.label} and verified it again before changing the "
            "demo repository.\n"
            + (
                f"     `{GATE_COMMAND}` now passes there.\n"
                if verified.returncode == 0
                else f"     `{GATE_COMMAND}` still fails there (unexpected).\n"
            )
        )
        return TrialOutcome(applied=True, kept=_finish(box, keep, say))
    except BaseException as exc:
        # Keep the folder: it is the only record of what went wrong.
        if isinstance(exc, TrialError):
            raise TrialError(f"{exc}\nThe trial folder was kept for inspection: {box.root}") from exc
        raise


def _finish(box: TrialSandbox, keep: bool, say: Callable[[str], None]) -> Path | None:
    say(
        "Next, on your own project:\n"
        "  syncmill init       # set up syncmill in your repository\n"
        "  syncmill doctor     # check agents, logins and the repository\n"
        "  syncmill start      # then choose \"Work on my project\"\n"
    )
    if keep:
        say(f"The trial folder was kept: {box.root}")
        return box.root
    box.cleanup()
    say("The trial folder was removed (use --keep to keep it).")
    return None
