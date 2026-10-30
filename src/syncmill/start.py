"""Pure helpers behind ``syncmill start`` on a real project (#171).

Nothing here asks a question, runs a command or writes a file: the CLI owns the
conversation and reuses the existing ``run``/``diff``/``apply`` commands. This
module finds verification-command candidates by reading well-known files, and
renders the confirmation screen shown before any agent starts (also what
``start --dry-run`` prints).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

#: Read at most this much of a marker file; discovery never needs more.
_MAX_READ_BYTES = 1024 * 1024
_MAKE_TEST_TARGET = re.compile(r"^test\s*:", re.MULTILINE)
#: What `npm init` writes when there are no tests: not a verification command.
_NPM_PLACEHOLDER = "no test specified"


@dataclass(frozen=True)
class GateSuggestion:
    command: str
    source: str  # the file that suggested it, relative to the repository


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        with path.open("rb") as handle:
            return handle.read(_MAX_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def discover_gate_commands(repo: Path) -> list[GateSuggestion]:
    """Verification-command candidates from a fixed table of well-known files.

    Read-only: files are read, never executed, so discovery cannot run a
    repository script. The order is the table's, and a command appears once.
    """
    found: list[GateSuggestion] = []

    def add(command: str, source: str) -> None:
        if all(s.command != command for s in found):
            found.append(GateSuggestion(command, source))

    pyproject = _read(repo / "pyproject.toml")
    if pyproject is not None and "pytest" in pyproject:
        add("python -m pytest -q", "pyproject.toml")
    package = _read(repo / "package.json")
    if package is not None:
        try:
            scripts = json.loads(package).get("scripts") or {}
        except (ValueError, AttributeError):
            scripts = {}
        test = scripts.get("test") if isinstance(scripts, dict) else None
        if isinstance(test, str) and test.strip() and _NPM_PLACEHOLDER not in test:
            add("npm test", "package.json")
    for name in ("GNUmakefile", "makefile", "Makefile"):
        text = _read(repo / name)
        if text is not None and _MAKE_TEST_TARGET.search(text):
            add("make test", name)
            break
    if _read(repo / "Cargo.toml") is not None:
        add("cargo test", "Cargo.toml")
    if _read(repo / "go.mod") is not None:
        add("go test ./...", "go.mod")
    return found


@dataclass(frozen=True)
class StartPlan:
    """Everything the confirmation screen states, decided before any agent runs."""

    mode: str  # "single" or "compare"
    agents: tuple[str, ...]
    timeouts: tuple[int, ...]  # seconds, parallel to ``agents``
    max_calls: int
    gate_commands: tuple[str, ...]
    gate_scope: str  # "config", "this run" or "none"
    repository: str
    dirty: int  # uncommitted paths included in the starting point
    memory: str


def _duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def render_start_plan(plan: StartPlan) -> str:
    """The screen shown before running (and by ``start --dry-run``)."""
    if plan.mode == "single":
        mode = "single agent"
    else:
        mode = f"compare {len(plan.agents)} candidates (Room), then pick one to apply"
    agents = ", ".join(
        f"{aid} (timeout {_duration(t)})" for aid, t in zip(plan.agents, plan.timeouts)
    )
    if plan.gate_commands:
        scope = {
            "config": "from your [gate] settings",
            "this run": "this run only, not saved",
        }.get(plan.gate_scope, plan.gate_scope)
        check = f"{'; '.join(plan.gate_commands)}  ({scope})"
    else:
        check = "none — the result will be unverified"
    repository = plan.repository
    if plan.dirty:
        repository += (
            f"\n                 includes {plan.dirty} uncommitted change(s); editing "
            "those files before applying can make the apply refuse"
        )
    calls = "call" if plan.max_calls == 1 else "calls"
    return "\n".join([
        "Before running:",
        f"  Mode           {mode}",
        f"  Agents         {agents}",
        f"  Agent calls    at most {plan.max_calls} {calls}",
        f"  Verification   {check}",
        f"  Repository     {repository}",
        f"  Memory         {plan.memory}",
        "  Cost           unknown (billed by each provider's own plan)",
    ])
