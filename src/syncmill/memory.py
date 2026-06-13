"""Shared memory layer — wraps memtomem MemtomemStore.

Supervisor is the sole reader/writer. Agents never touch this directly.

Memory is *advisory*: every operation is best-effort. If memtomem is
unavailable or misconfigured (e.g. an unsupported embedding provider), the
store degrades gracefully — operations log a warning, set ``degraded``, and
return safe defaults — so the Supervisor can still run agents. Memory is an
enhancement, not a hard dependency for executing a task.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from syncmill.config import MemoryConfig
from syncmill.models import Task, TaskResult

logger = logging.getLogger(__name__)


class SyncmillMemory:
    """syncmill-specific memory conventions on top of MemtomemStore.

    Namespace layout:
        agent/{agent_id}  — private namespace per agent
        shared            — cross-agent knowledge base
    """

    def __init__(self, config: MemoryConfig):
        # Memory is advisory even at construction: if memtomem can't be imported
        # or built, degrade instead of crashing Supervisor(config). With a None
        # store, each method's try/except below catches the AttributeError and
        # returns its safe default.
        self.degraded = False
        self.last_error: str | None = None
        self._store = None
        try:
            from memtomem.integrations.langgraph import MemtomemStore

            self._store = MemtomemStore(
                config_overrides={"storage": {"sqlite_path": config.db_path}}
            )
        except Exception as exc:
            self._degrade("init", exc)

    def _degrade(self, where: str, exc: Exception) -> None:
        """Record a non-fatal memtomem failure and keep going."""
        self.degraded = True
        self.last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("memory unavailable during %s: %s", where, self.last_error)

    # ── Agent registration ───────────────────────────────────────────────

    async def register_agent(self, agent_id: str) -> None:
        """Ensure agent/{agent_id} and shared namespaces exist."""
        try:
            await self._store._ensure_init()
            ns = f"agent/{agent_id}"
            storage = self._store._components.storage
            if await storage.get_namespace_meta(ns) is None:
                await storage.set_namespace_meta(ns, description=f"Agent: {agent_id}")
            if await storage.get_namespace_meta("shared") is None:
                await storage.set_namespace_meta("shared", description="Shared knowledge")
        except Exception as exc:
            self._degrade("register_agent", exc)

    # ── Task session lifecycle ───────────────────────────────────────────

    async def start_task_session(self, agent_id: str, task: Task) -> str | None:
        """Start an episodic memory session. Returns the session id, or None
        if memory is unavailable."""
        try:
            ns = f"agent/{agent_id}"
            session_id = await self._store.start_session(agent_id=agent_id, namespace=ns)
            await self._store.log_event("task_start", f"Prompt: {task.prompt[:200]}")
            return session_id
        except Exception as exc:
            self._degrade("start_task_session", exc)
            return None

    async def end_task_session(self, session_id: str | None, result: TaskResult) -> None:
        """End the session and log the outcome."""
        try:
            summary = (
                f"{result.agent_id}: {result.status.value} "
                f"(exit={result.exit_code}, {result.duration_seconds}s)"
            )
            if result.error:
                await self._store.log_event("task_error", result.error[:500])
            await self._store.log_event(
                "task_end",
                f"exit_code={result.exit_code}, duration={result.duration_seconds}s",
            )
            await self._store.end_session(summary=summary)
        except Exception as exc:
            self._degrade("end_task_session", exc)

    # ── Result sharing ───────────────────────────────────────────────────

    async def share_result(self, task: Task, result: TaskResult) -> None:
        """Write a successful result summary to the shared namespace."""
        try:
            content = (
                f"## Task Result\n\n**Prompt:** {task.prompt}\n\n"
                f"**Agent:** {result.agent_id}\n\n**Output:**\n{result.output[:1000]}"
            )
            tags = ["task-result", result.agent_id]
            if result.files_changed:
                content += f"\n\n**Files changed:** {', '.join(result.files_changed)}"
            await self._store.add(content=content, tags=tags, namespace="shared")
        except Exception as exc:
            self._degrade("share_result", exc)

    # ── Context search ───────────────────────────────────────────────────

    async def search_context(self, query: str, agent_id: str | None = None) -> str:
        """Search agent-scoped + shared memories, return as formatted text.

        Returns an empty string if memory is unavailable or nothing matches.
        """
        try:
            results: list[dict] = []

            # Search agent namespace if specified
            if agent_id:
                ns = f"agent/{agent_id}"
                results.extend(await self._store.search(query, namespace=ns, top_k=3))

            # Always search shared namespace
            shared_results = await self._store.search(query, namespace="shared", top_k=3)

            # Merge and deduplicate by chunk id
            seen: set[str] = {r["id"] for r in results}
            for r in shared_results:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    results.append(r)

            # Sort by score descending
            results.sort(key=lambda r: -r["score"])

            if not results:
                return ""

            # Format as text for prompt injection
            lines = [
                f"[{r.get('source', 'unknown')}] {r['content'][:300]}"
                for r in results[:5]
            ]
            return "\n---\n".join(lines)
        except Exception as exc:
            self._degrade("search_context", exc)
            return ""

    # ── Status ───────────────────────────────────────────────────────────

    async def status(self) -> dict:
        """Check memtomem connection status."""
        try:
            await self._store._ensure_init()
            db_path = self._store._config_overrides.get("storage", {}).get(
                "sqlite_path", "default"
            )
            return {"connected": True, "db_path": db_path}
        except Exception as e:
            return {"connected": False, "error": str(e)}

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        try:
            await self._store.close()
        except Exception as exc:
            self._degrade("close", exc)


_loop: asyncio.AbstractEventLoop | None = None


def run_async(coro: Any) -> Any:
    """Run an async coroutine from sync context, reusing the event loop.

    memtomem is async; the rest of syncmill is sync. A single reused loop
    avoids the "Event loop is closed" error that ``asyncio.run`` triggers on
    memtomem's lazily-initialized resources.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(coro)


def close_event_loop() -> None:
    """Close the shared event loop. Call once at process shutdown (the CLI
    does this in its finally blocks) so the loop's resources are released."""
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.close()
    _loop = None
