"""Opt-in, fully local smoke for real LangGraph SQLite and FastEmbed."""

from __future__ import annotations

import asyncio
import os

import pytest

from syncmill.config import MemoryConfig
from syncmill.memory import close_event_loop, run_async
from syncmill.memory_backend import LangGraphBackend

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_LANGGRAPH_REAL") != "1",
    reason="set SYNCMILL_TEST_LANGGRAPH_REAL=1 to run the real LangGraph smoke",
)
def test_real_langgraph_persistence_tags_delete_and_fresh_loop_close(tmp_path):
    config = MemoryConfig(
        backend="langgraph",
        langgraph={"db_path": str(tmp_path / "langgraph.db")},
    )
    namespace = "board-run:real-smoke"
    content = "Syncmill LangGraph verbatim smoke — 한글"
    backend = LangGraphBackend(config)
    run_async(backend.add(content, tags=["smoke", "한글"], namespace=namespace))
    rows = run_async(
        backend.search(
            "verbatim smoke", namespace=namespace, top_k=3, tag_filter="smoke"
        )
    )
    assert rows and rows[0]["content"] == content
    assert rows[0]["tags"] == ["smoke", "한글"]
    assert run_async(backend.get(rows[0]["id"]))["namespace"] == namespace
    assert run_async(backend.count_namespace(namespace)) == 1

    # The production CLI may initialize and close the adapter on different loops.
    close_event_loop()
    asyncio.run(backend.close())

    reopened = LangGraphBackend(config)
    try:
        assert run_async(reopened.count_namespace(namespace)) == 1
        assert run_async(reopened.delete_namespace(namespace)) == 1
        assert run_async(reopened.count_namespace(namespace)) == 0
    finally:
        run_async(reopened.close())
