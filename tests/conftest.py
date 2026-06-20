"""Shared hermetic test fixtures.

These tests never touch the user's real ~/.memtomem DB, the network, or the
real claude/codex/kimi binaries. Memory and agents are replaced with
in-memory duck-typed fakes that honor the same contracts as the real classes.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE

from syncmill.memory import SyncmillMemory
from syncmill.models import GateCheck, GateReport, Task, TaskResult, TaskStatus

# ── Fake agents / memory / registry ──────────────────────────────────────


class FakeAgent:
    """In-memory AgentRunner: records calls; returns a scripted TaskResult."""

    def __init__(
        self,
        agent_id: str,
        *,
        status: TaskStatus = TaskStatus.COMPLETED,
        output: str = "ok",
        error: str | None = None,
        exit_code: int = 0,
        duration: float = 0.01,
        raises: BaseException | None = None,
        adelay: float = 0.0,
    ):
        self._agent_id = agent_id
        self._status = status
        self._output = output
        self._error = error
        self._exit_code = exit_code
        self._duration = duration
        self._raises = raises
        # adelay: seconds aexecute() awaits before returning — lets concurrency
        # tests prove overlap (a slow agent shouldn't serialize the others).
        self._adelay = adelay
        self.calls: list[dict] = []

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        self.calls.append({"prompt": prompt, "workdir": workdir, "timeout": timeout})
        if self._raises is not None:
            raise self._raises
        import uuid

        return TaskResult(
            task_id=uuid.uuid4(),
            agent_id=self._agent_id,
            status=self._status,
            output=self._output,
            exit_code=self._exit_code,
            duration_seconds=self._duration,
            error=self._error,
        )

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        """Async twin of execute(): same scripted result, optional await delay.

        Delegates to execute() so call recording and the scripted TaskResult /
        ``raises`` behavior stay identical across both paths.
        """
        if self._adelay:
            await asyncio.sleep(self._adelay)
        return self.execute(prompt, workdir, timeout)


class FakeMemory:
    """Duck-typed stand-in for SyncmillMemory (async methods, via run_async).

    ``fail=True`` makes every operation raise — used to assert that a dead
    memtomem does NOT abort the task.
    """

    def __init__(self, *, context: str = "", fail: bool = False, echo_shared: bool = False):
        self._context = context
        self._fail = fail
        # echo_shared models a live shared namespace: search_context replays
        # whatever was share_result()'d so far. Lets pipeline tests prove the
        # stage→stage handoff without the real memtomem store.
        self._echo_shared = echo_shared
        self.degraded = False
        self.last_error: str | None = None
        self.shared: list[tuple] = []
        self.events: list[str] = []

    def _maybe_fail(self, where: str) -> None:
        if self._fail:
            raise RuntimeError(f"memtomem unavailable ({where})")

    async def register_agent(self, agent_id: str) -> None:
        self.events.append(f"register:{agent_id}")
        self._maybe_fail("register_agent")

    async def start_task_session(self, agent_id: str, task: Task) -> str | None:
        self._maybe_fail("start_task_session")
        self.events.append(f"start:{agent_id}")
        return "session-123"

    async def end_task_session(self, session_id: str | None, result: TaskResult) -> None:
        self.events.append(f"end:{session_id}")
        self._maybe_fail("end_task_session")

    async def share_result(self, task: Task, result: TaskResult) -> None:
        self._maybe_fail("share_result")
        self.shared.append((task, result))

    async def search_context(self, query: str, agent_id: str | None = None) -> str:
        self._maybe_fail("search_context")
        if self._echo_shared and self.shared:
            return "\n---\n".join(f"[{r.agent_id}] {r.output}" for _, r in self.shared)
        return self._context

    async def close(self) -> None:
        self.events.append("close")


class FakeRegistry:
    """Duck-typed AgentRegistry."""

    def __init__(self, agents: list):
        self._agents = list(agents)

    def available(self) -> list:
        return list(self._agents)

    def get(self, agent_id: str):
        for a in self._agents:
            if a.agent_id == agent_id:
                return a
        return None


class FakeStorage:
    """Stand-in for memtomem _components.storage (namespace meta only)."""

    def __init__(self, existing: set[str] | None = None):
        self.meta: set[str] = set(existing or ())
        self.set_calls: list[str] = []

    async def get_namespace_meta(self, ns: str):
        return {"ns": ns} if ns in self.meta else None

    async def set_namespace_meta(self, ns: str, description: str = ""):
        self.meta.add(ns)
        self.set_calls.append(ns)


class _Components:
    def __init__(self, storage):
        self.storage = storage


class FakeStore:
    """Stand-in for memtomem's MemtomemStore. Records calls; scripts search.

    ``search_results`` maps namespace -> list[dict]. ``raise_on`` is a set of
    method names that should raise (to drive degradation tests).
    """

    def __init__(self, *, search_results=None, existing_ns=None, raise_on=None):
        self._search_results = search_results or {}
        self._components = _Components(FakeStorage(existing_ns))
        self._config_overrides = {"storage": {"sqlite_path": "/tmp/fake.db"}}
        self._raise_on = set(raise_on or ())
        self.added: list[dict] = []
        self.events: list[tuple] = []
        self.sessions: list[dict] = []
        self.ended: list[str] = []
        self.closed = False

    def _guard(self, name: str):
        if name in self._raise_on:
            raise RuntimeError(f"boom in {name}")

    async def _ensure_init(self):
        self._guard("_ensure_init")

    async def start_session(self, agent_id: str = "default", namespace: str | None = None):
        self._guard("start_session")
        self.sessions.append({"agent_id": agent_id, "namespace": namespace})
        return "sid-1"

    async def log_event(self, event_type: str, content: str, chunk_ids=None):
        self._guard("log_event")
        self.events.append((event_type, content))

    async def end_session(self, summary: str | None = None):
        self._guard("end_session")
        self.ended.append(summary)
        return {"summary": summary}

    async def add(self, content: str, tags=None, namespace=None, **kw):
        self._guard("add")
        self.added.append({"content": content, "tags": tags, "namespace": namespace})
        return {"indexed_chunks": 1}

    async def search(self, query: str, namespace=None, top_k=10, **kw):
        self._guard("search")
        return list(self._search_results.get(namespace, []))

    async def close(self):
        self.closed = True


class FakeWorktree:
    def __init__(self, path: Path, *, is_git: bool = True, branch="syncmill/x/y"):
        self.path = path
        self.is_git = is_git
        self.branch = branch
        self.agent_id = "claude"
        self.is_temp = not is_git


class FakeWorktreePool:
    """Duck-typed WorktreePool: hands out distinct FakeWorktrees per acquire and
    records the acquisition order so tests can assert a strategy's topology
    (one-per-agent vs. single-shared) without touching real git.

    ``is_git`` flows into the handed-out worktrees so ``_collect_files_changed``
    either diffs (paired with a monkeypatched ``_git_changed_files``) or no-ops.
    """

    def __init__(self, *, is_git: bool = False):
        self._is_git = is_git
        self.acquired: list[str] = []  # agent_ids, in acquire order
        self.handed: list[FakeWorktree] = []
        self.cleaned = False
        self._n = 0

    def acquire(self, agent_id: str) -> FakeWorktree:
        self._n += 1
        wt = FakeWorktree(
            Path(f"/tmp/syncmill-wt/{agent_id}-{self._n}"),
            is_git=self._is_git,
            branch=f"syncmill/{agent_id}/{self._n}",
        )
        wt.agent_id = agent_id
        self.acquired.append(agent_id)
        self.handed.append(wt)
        return wt

    @property
    def worktrees(self) -> list[FakeWorktree]:
        return list(self.handed)

    def cleanup_all(self) -> int:
        # Simplification: always reports full success. The real pool can return
        # a partial count when a removal fails — that path is covered directly by
        # test_pool_cleanup_all_independent_on_failure against the real pool.
        self.cleaned = True
        return len(self.handed)


class FakeGate:
    """Duck-typed QualityGate: scripted verdict, no subprocess.

    The 'fake gate' the #10 acceptance criteria call for. ``passes`` is the
    default verdict; ``by_agent`` overrides it per agent id (keyed off
    ``worktree.agent_id``) so a test can make one competitor pass and another
    fail. Records the agent ids it evaluated, in order, so tests can assert which
    worktrees were gated (and that gating is lazy).
    """

    def __init__(self, *, passes: bool = True, by_agent: dict[str, bool] | None = None):
        self._passes = passes
        self._by_agent = by_agent or {}
        self.evaluated: list[str] = []

    def evaluate(self, worktree) -> GateReport:
        self.evaluated.append(worktree.agent_id)
        ok = self._by_agent.get(worktree.agent_id, self._passes)
        return GateReport(
            passed=ok,
            score=1.0 if ok else 0.0,
            checks=[GateCheck("fake-check", passed=ok, exit_code=0 if ok else 1)],
        )


# ── Factories / fixtures ─────────────────────────────────────────────────


def make_memory(store: FakeStore) -> SyncmillMemory:
    """Build a SyncmillMemory around a fake store without constructing the real
    MemtomemStore (which would hit the user's config/DB)."""
    m = SyncmillMemory.__new__(SyncmillMemory)
    m._store = store
    m.degraded = False
    m.last_error = None
    # Derive from memtomem's constants (same source as the real __init__) so the
    # fixtures can't silently drift from production if memtomem renames them.
    m._agent_prefix = AGENT_NAMESPACE_PREFIX
    m._shared_ns = SHARED_NAMESPACE
    return m


@pytest.fixture
def make_agent():
    return FakeAgent


@pytest.fixture
def fake_memory():
    return FakeMemory()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo (parent is tmp_path, so .syncmill-worktrees lands
    in tmp_path) with one committed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ── Autouse isolation ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_syncmill_env(monkeypatch):
    """Strip the developer's SYNCMILL_* vars so they can't leak into config tests."""
    for k in list(os.environ):
        if k.startswith("SYNCMILL_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def reset_run_async_loop():
    """Keep the module-global event loop from leaking across tests."""
    import syncmill.memory as mem

    original = mem._loop
    yield
    if mem._loop is not None and mem._loop is not original and not mem._loop.is_closed():
        mem._loop.close()
    mem._loop = original
