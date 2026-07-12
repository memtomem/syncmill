"""Opt-in, fully local smoke for the real mem0/Qdrant/FastEmbed stack."""

from __future__ import annotations

import os

import pytest

from syncmill.config import MemoryConfig
from syncmill.memory import run_async
from syncmill.memory_backend import Mem0Backend

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_MEM0_REAL") != "1",
    reason="set SYNCMILL_TEST_MEM0_REAL=1 to run the real mem0 smoke",
)
def test_real_mem0_add_search_get_count_delete_close(tmp_path):
    backend = Mem0Backend(
        MemoryConfig(
            backend="mem0",
            mem0={"data_dir": str(tmp_path / "mem0")},
        )
    )
    namespace = "board-run:real-smoke"
    content = "Syncmill mem0 verbatim smoke — 한글"
    try:
        run_async(backend.add(content, tags=["smoke"], namespace=namespace))
        assert run_async(backend.count_namespace(namespace)) == 1
        rows = run_async(
            backend.search(
                "verbatim smoke", namespace=namespace, top_k=3, tag_filter="smoke"
            )
        )
        assert rows and rows[0]["content"] == content
        assert run_async(backend.get(rows[0]["id"]))["namespace"] == namespace
        assert run_async(backend.delete_namespace(namespace)) == 1
        assert run_async(backend.count_namespace(namespace)) == 0
    finally:
        run_async(backend.close())
