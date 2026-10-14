"""Keep the hands-on notebooks under docs/notebooks honest.

The notebooks drive the real ``syncmill`` CLI against fake agent CLIs inside a
disposable sandbox (``docs/notebooks/_sandbox.py``). These tests pin the three
things that can rot without anyone opening a notebook:

* the fakes still speak the output format the real agent parsers read,
* the sandbox never touches anything outside its own root, even when the
  calling environment points every routing variable somewhere else,
* the committed notebooks are well-formed and their outputs carry no
  machine-specific paths.

Executing the notebook files themselves needs nbclient, which is not a
dependency, so that check is opt-in (``SYNCMILL_TEST_NOTEBOOKS_REAL=1``).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from syncmill import agents
from syncmill.agents import CodexAgent, KimiCodeAgent, model_routing_scope

ROOT = Path(__file__).resolve().parent.parent
NOTEBOOK_DIR = ROOT / "docs" / "notebooks"
NOTEBOOKS = sorted(NOTEBOOK_DIR.glob("*.ipynb"))

_spec = importlib.util.spec_from_file_location("_sandbox", NOTEBOOK_DIR / "_sandbox.py")
sandbox = importlib.util.module_from_spec(_spec)
sys.modules["_sandbox"] = sandbox
_spec.loader.exec_module(sandbox)


@pytest.fixture
def box(tmp_path):
    base = tmp_path / "boxes"
    base.mkdir()
    made = sandbox.Sandbox.create(base=base)
    yield made
    if made.root.exists():
        made.cleanup()


# ── fake agents speak the real formats ──────────────────────────────────


def test_fake_codex_output_parses_as_codex(box):
    agent = CodexAgent()
    result = box.run_fake("codex", *agent._build_command("write a note")[1:], show=False)

    assert result.returncode == 0
    assert agent._is_error_output(result.stdout) is None
    assert agent._parse_output(result.stdout) == (
        "codex (fake) appended one line to NOTES-codex.md."
    )
    assert agent._provider_session_from_output(result.stdout) == "fake-codex-thread"
    assert (box.scratch / "NOTES-codex.md").read_text() == "- codex wrote this line.\n"


def test_fake_kimi_output_parses_as_kimi(box):
    agent = KimiCodeAgent()
    result = box.run_fake("kimi", *agent._build_command("write a note")[1:], show=False)

    assert result.returncode == 0
    assert agent._is_error_output(result.stdout) is None
    assert agent._parse_output(result.stdout) == (
        "kimi-code (fake) appended one line to NOTES-kimi.md."
    )
    assert agent._provider_session_from_output(result.stdout) == "fake-kimi-session"
    assert (box.scratch / "NOTES-kimi.md").read_text() == "- kimi-code wrote this line.\n"


@pytest.mark.parametrize("binary", ["codex", "kimi"])
def test_fake_failure_switch_exits_nonzero_and_writes_nothing(box, binary, monkeypatch):
    args = ["exec", "x", "--json"] if binary == "codex" else ["-p", "x"]
    env = box.env()
    env.pop("FAKE_PROMPT_LOG_DIR")
    env[f"FAKE_{binary.upper()}_FAIL"] = "1"
    done = subprocess.run(
        [str(box.fakebin / binary), *args],
        cwd=box.scratch, env=env, capture_output=True, text=True,
    )

    assert done.returncode == 1
    assert list(box.scratch.iterdir()) == []


_PROMPT = "write a note\nwith a second line"


def _logged_prompt(box, binary: str, argv: list[str]) -> str:
    done = subprocess.run(
        [str(box.fakebin / binary), *argv[1:]],
        cwd=box.scratch, env=box.env(), capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stderr
    (logged,) = box.prompts()
    return logged.read_text()


@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
@pytest.mark.parametrize("selector", [None, "some-model"], ids=["default", "selector"])
@pytest.mark.parametrize("agent_cls,binary", [(CodexAgent, "codex"), (KimiCodeAgent, "kimi")])
def test_fake_reads_the_prompt_from_every_real_argv_shape(
    box, agent_cls, binary, resume, selector
):
    agent = agent_cls()
    token = agents._ACTIVE_PROVIDER_SESSION.set(
        ("session-1", True, False) if resume else (None, False, False)
    )
    try:
        with model_routing_scope({agent.agent_id: selector}):
            argv = agent._build_command(_PROMPT)
    finally:
        agents._ACTIVE_PROVIDER_SESSION.reset(token)
    assert ("session-1" in argv) == resume
    assert (selector in argv) == (selector is not None)

    assert _logged_prompt(box, binary, argv) == _PROMPT


def test_fake_runs_under_an_interpreter_path_with_a_space(box, tmp_path):
    spaced = tmp_path / "py with space" / "python"
    spaced.parent.mkdir()
    spaced.symlink_to(sys.executable)
    launchers, sources = tmp_path / "bin dir", tmp_path / "src dir"
    launchers.mkdir()
    sources.mkdir()
    launcher = sandbox.write_fake_agent(launchers, sources, "codex", python=str(spaced))

    done = subprocess.run(
        [str(launcher), "login", "status"], env=box.env(), capture_output=True, text=True
    )
    assert (done.returncode, done.stdout) == (0, "codex (fake) - logged in\n")


def test_scenario_tells_a_fake_exactly_what_to_write(box):
    box.set_scenario({"codex": {"files": {"pkg/mod.py": "x = 1\n"}, "message": "Set x."}})
    agent = CodexAgent()
    result = box.run_fake("codex", *agent._build_command("anything")[1:], show=False)

    assert agent._parse_output(result.stdout) == "Set x."
    assert (box.scratch / "pkg" / "mod.py").read_text() == "x = 1\n"
    assert not (box.scratch / "NOTES-codex.md").exists()

    box.set_scenario(None)
    assert not box.scenario_path.exists()


def test_gate_python3_is_the_notebook_interpreter(box):
    done = box.cmd("python3", "-c", "import sys; print(sys.version_info[:2])", show=False)
    assert done.stdout.strip() == str(tuple(sys.version_info[:2]))


def test_phase_markers_match_the_real_prompt_builders():
    # box.calls() labels each logged prompt by these markers; if syncmill
    # rewords a phase instruction, notebook 02's call tables would silently
    # degrade to "task", so pin every marker against the builder that emits it.
    from syncmill.strategies.council import (
        _council_critique, _council_propose, _council_synthesize,
    )
    from syncmill.strategies.decompose import (
        _decompose_plan_prompt, _decompose_subtask_prompt, _decompose_synthesize_prompt,
    )

    built = {
        "council: propose": _council_propose("t"),
        "council: critique": _council_critique("t"),
        "council: synthesize": _council_synthesize("t"),
        "decompose: plan": _decompose_plan_prompt("t", 8),
        "decompose: subtask": _decompose_subtask_prompt("t", "s", 1, 2),
        "decompose: synthesize": _decompose_synthesize_prompt("t"),
    }
    for phase, marker in sandbox.PHASE_MARKERS:
        assert marker in built[phase], phase
        others = [name for name, text in built.items() if name != phase and marker in text]
        assert others == [], (phase, others)


def test_phase_rules_pick_the_step_whose_text_appears(box):
    box.set_scenario({"codex": [
        {"when": "PLAN", "files": {}, "message": "planned"},
        {"files": {"a.py": "a\n"}, "message": "fallback step"},
    ]})
    agent = CodexAgent()
    planned = box.run_fake("codex", *agent._build_command("PLAN this")[1:], show=False)
    other = box.run_fake("codex", *agent._build_command("something else")[1:], show=False)

    assert agent._parse_output(planned.stdout) == "planned"
    assert agent._parse_output(other.stdout) == "fallback step"
    assert (box.scratch / "a.py").read_text() == "a\n"


def test_calls_labels_agent_phase_context_and_task(box):
    box.prompt_log.mkdir(parents=True)
    (box.prompt_log / "kimi-code-1.txt").write_text(
        "Context from previous work:\nx\n\nTask: Critique those ideas. go"
    )
    (box.prompt_log / "codex-2.txt").write_text("plain task")

    assert box.calls() == [
        {"agent": "kimi-code", "phase": "council: critique", "context": "yes",
         "task": "Critique those ideas. go"},
        {"agent": "codex", "phase": "task", "context": "no", "task": "plain task"},
    ]


def test_fake_codex_passes_the_doctor_login_probe(box):
    assert box.run_fake("codex", "login", "status", show=False).returncode == 0


# ── isolation from the calling environment ──────────────────────────────


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _hostile_victim(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """Point every routing variable at a separate repo that must stay untouched."""
    victim = tmp_path / "victim"
    sentinel = tmp_path / "SENTINEL"
    repo = victim / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    hook = f"#!/bin/sh\necho hook ran >> {sentinel}\n"
    for hooks in (victim / "hooks", victim / "template" / "hooks"):
        hooks.mkdir(parents=True)
        for name in ("pre-commit", "post-checkout", "post-commit"):
            (hooks / name).write_text(hook)
            (hooks / name).chmod(0o755)
    gitconfig = f"[core]\n\thooksPath = {victim / 'hooks'}\n"
    (victim / "home").mkdir()
    (victim / "home" / ".gitconfig").write_text(gitconfig)
    (victim / "xdg" / "git").mkdir(parents=True)
    (victim / "xdg" / "git" / "config").write_text(gitconfig)

    monkeypatch.setenv("HOME", str(victim / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(victim / "xdg"))
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(victim / "template"))
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    monkeypatch.setenv("GIT_INDEX_FILE", str(repo / ".git" / "index"))
    monkeypatch.setenv("SYNCMILL_REPO_ROOT", str(repo))
    monkeypatch.setenv("SYNCMILL_RUNS__ROOT", str(victim / "runs"))
    monkeypatch.setenv("SYNCMILL_CLAUDE__ENABLED", "true")
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(victim / "memtomem.db"))
    monkeypatch.setenv("MEMTOMEM_EMBEDDING__PROVIDER", "openai")
    return victim, sentinel


def test_fake_mode_flow_stays_inside_the_sandbox(tmp_path, monkeypatch):
    victim, sentinel = _hostile_victim(tmp_path, monkeypatch)
    before = _digest_tree(victim)
    base = tmp_path / "boxes"
    base.mkdir()

    box = sandbox.Sandbox.create(base=base)
    assert box.sm("init", show=False).returncode == 0
    box.git("add", "syncmill.toml", ".syncmill/.gitignore")
    box.git("commit", "-qm", "init")
    doctor = box.sm("doctor", show=False)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "Agent claude: missing" in doctor.stdout.splitlines()

    plan = box.sm("run", "--dry-run", "write a note", "--agent", "codex", show=False)
    plan_json = json.loads(plan.stdout[: plan.stdout.index("\n}\n") + 2])
    assert (plan_json["agents"], plan_json["maximum_agent_calls"]) == (["codex"], 1)

    run = box.sm("run", "write a note", "--agent", "codex", "--timeout", "60", show=False)
    run_id = box.run_id(run)
    assert not (box.repo / "NOTES-codex.md").exists()
    patch = box.sm("diff", run_id, show=False).stdout
    assert patch.splitlines()[-1] == "+- codex wrote this line."
    assert "+++ b/NOTES-codex.md" in patch.splitlines()
    assert box.sm("apply", run_id, show=False).returncode == 0
    assert (box.repo / "NOTES-codex.md").read_text() == "- codex wrote this line.\n"

    # The shared-memory handoff the notebook demonstrates: a second agent's
    # prompt carries the first agent's result.
    second = box.sm("run", "write another note", "--agent", "kimi-code", show=False)
    assert second.returncode == 0, second.stdout + second.stderr
    prompt = box.prompts()[-1].read_text()
    assert prompt.startswith("Context from previous work:")
    assert "codex (fake) appended one line to NOTES-codex.md." in prompt

    assert _digest_tree(victim) == before
    assert not sentinel.exists()
    assert (box.home / ".syncmill").is_dir()
    box.cleanup()


# ── cleanup only removes what it created ────────────────────────────────


def test_cleanup_removes_its_own_root(box):
    root = box.root
    box.cleanup()
    assert not root.exists()


def test_cleanup_refuses_a_sandbox_it_did_not_create(box, tmp_path):
    # Everything about this directory looks right except who made it.
    lookalike = box.base / "syncmill-notebook.lookalike"
    lookalike.mkdir()
    (lookalike / sandbox.MARKER).write_text("anything\n")
    with pytest.raises(sandbox.SandboxError, match="did not create"):
        sandbox.Sandbox(root=lookalike, base=box.base).cleanup()
    assert lookalike.exists()


def test_cleanup_refuses_a_marker_that_is_not_ours(box):
    (box.root / sandbox.MARKER).write_text("someone else\n")
    with pytest.raises(sandbox.SandboxError, match="without our"):
        box.cleanup()
    assert box.root.exists()
    (box.root / sandbox.MARKER).write_text(box._token + "\n")


def _retarget(box, root: Path) -> Path:
    """Point an owned sandbox at ``root`` (carrying a copy of its marker)."""
    original = box.root
    (root / sandbox.MARKER).write_text(box._token + "\n")
    box.root = root
    return original


def test_cleanup_refuses_a_root_outside_its_base(box, tmp_path):
    elsewhere = tmp_path / "elsewhere" / box.root.name
    elsewhere.mkdir(parents=True)
    original = _retarget(box, elsewhere)
    with pytest.raises(sandbox.SandboxError, match="outside"):
        box.cleanup()
    assert elsewhere.exists()
    box.root = original


def test_cleanup_refuses_a_traversal(box, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    original = _retarget(box, victim)
    box.root = box.base / original.name / ".." / ".." / "victim"
    with pytest.raises(sandbox.SandboxError):
        box.cleanup()
    assert victim.exists()
    box.root = original


def test_cleanup_refuses_a_symlink_root(box, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    original = _retarget(box, target)
    link = box.base / "syncmill-notebook.link"
    link.symlink_to(target)
    box.root = link
    with pytest.raises(sandbox.SandboxError, match="symlink"):
        box.cleanup()
    assert target.exists()
    box.root = original


def test_cleanup_refuses_an_unexpected_name(box):
    other = box.base / "something-else"
    other.mkdir()
    original = _retarget(box, other)
    with pytest.raises(sandbox.SandboxError, match="name"):
        box.cleanup()
    assert other.exists()
    box.root = original


# ── committed notebooks ─────────────────────────────────────────────────


def test_notebooks_exist():
    assert [p.name for p in NOTEBOOKS] == [
        "00-introduction.ipynb",
        "01-first-run.ipynb",
        "02-strategies.ipynb",
        "03-shared-memory.ipynb",
        "04-board-and-tui.ipynb",
    ]


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_is_well_formed_and_executed_cleanly(path):
    nb = json.loads(path.read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert code, "a hands-on notebook needs code cells"
    assert [c["execution_count"] for c in code] == list(range(1, len(code) + 1))
    errors = [o for c in code for o in c["outputs"] if o["output_type"] == "error"]
    assert errors == []


_PRIVATE_PATH = re.compile(r"/(?:Users|home)/[^/\s\"']+")


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_outputs_carry_no_home_paths(path):
    nb = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps([c.get("outputs", []) for c in nb["cells"]] + [nb["metadata"]])
    assert _PRIVATE_PATH.findall(text) == []


@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_NOTEBOOKS_REAL") != "1",
    reason="executes notebooks with nbclient; set SYNCMILL_TEST_NOTEBOOKS_REAL=1",
)
@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_executes_end_to_end(path, tmp_path):
    # Execute a copy (with its helper beside it, where the kernel's cwd will
    # be) so the committed outputs are never rewritten by a test run.
    work = tmp_path / "nb"
    work.mkdir()
    shutil.copy2(path, work / path.name)
    shutil.copy2(NOTEBOOK_DIR / "_sandbox.py", work / "_sandbox.py")
    done = subprocess.run(
        [
            "uv", "run", "--no-sync", "--project", str(ROOT),
            "--with", "nbclient", "--with", "ipykernel", "--with", "nbconvert",
            "jupyter", "execute", "--inplace", str(work / path.name),
        ],
        cwd=work, capture_output=True, text=True, timeout=600,
    )
    assert done.returncode == 0, done.stderr
    executed = json.loads((work / path.name).read_text(encoding="utf-8"))
    errors = [
        o for c in executed["cells"] if c["cell_type"] == "code"
        for o in c["outputs"] if o["output_type"] == "error"
    ]
    assert errors == []
