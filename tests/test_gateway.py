"""gateway.py — the read-only MCP gateway.

Covers: exactly-three-read-only-tools registration, per-tool behavior, the
agent-isolation guard, the top_k clamp, healthy-empty-vs-unavailable (per-call,
not the sticky degraded flag), degradation, and host/port-at-construction (the
DNS-rebinding policy seam). No real server is started and no real memtomem is
constructed — tools are driven directly via FastMCP.call_tool against a
fake-store-backed SyncmillMemory.
"""

from __future__ import annotations

import pytest

from syncmill.board import Board
from syncmill.gateway import MAX_TOP_K, build_gateway
from syncmill.memory import run_async

from conftest import FakeStore, make_memory


def _call(mcp, name, args):
    """Drive a tool and return its text result.

    FastMCP.call_tool returns ``(content_blocks, {"result": <str>})`` for a
    str-returning tool.
    """
    _content, structured = run_async(mcp.call_tool(name, args))
    return structured["result"]


SHARED_HIT = {
    "id": "abc",
    "content": "shared knowledge here",
    "score": 0.87,
    "source": "notes.md",
    "namespace": "shared",
}


# ── registration: read-only, exactly six tools ─────────────────────────────


def test_exactly_six_readonly_tools():
    # The board_* tools are registered UNCONDITIONALLY (a stable surface) — here with
    # no board, so they exist but report "not enabled" (see test_board_tools_disabled).
    mcp = build_gateway(make_memory(FakeStore()))
    names = {t.name for t in mcp._tool_manager.list_tools()}
    # Exact-set equality is the regression guard: no write/delete/index tool — for
    # memory OR the board — can ever sneak in, and the surface stays read-only.
    assert names == {
        "shared_search", "shared_read", "shared_stats",
        "board_list", "board_get", "board_stats",
    }


# ── shared_search ──────────────────────────────────────────────────────────


def test_search_formats_hits_with_chunk_id():
    store = FakeStore(search_results={"shared": [SHARED_HIT]})
    out = _call(build_gateway(make_memory(store)), "shared_search", {"query": "q"})
    assert "abc" in out  # id present so a client can follow up with shared_read
    assert "shared knowledge here" in out


def test_search_no_matches_message():
    out = _call(build_gateway(make_memory(FakeStore())), "shared_search", {"query": "q"})
    assert out == "No matches in shared memory."


def test_search_unavailable_when_store_errors():
    m = make_memory(FakeStore(raise_on={"search"}))
    out = _call(build_gateway(m), "shared_search", {"query": "q"})
    assert "unavailable" in out.lower()


@pytest.mark.parametrize("bad", [-5, 0, 10_000])
def test_search_clamps_top_k(bad):
    store = FakeStore(search_results={"shared": [SHARED_HIT]})
    _call(build_gateway(make_memory(store)), "shared_search", {"query": "q", "top_k": bad})
    sent = store.searched[-1]["top_k"]
    assert 1 <= sent <= MAX_TOP_K


def test_search_empty_vs_unavailable_is_per_call_not_sticky():
    # Even with the memory object already flagged degraded, a HEALTHY empty search
    # must say "No matches", not "unavailable" — the tool reads the per-call
    # return value, never the sticky degraded flag.
    m = make_memory(FakeStore())
    m.degraded = True
    out = _call(build_gateway(m), "shared_search", {"query": "q"})
    assert out == "No matches in shared memory."


# ── shared_read ────────────────────────────────────────────────────────────


def test_read_returns_full_shared_chunk():
    cid = "55555555-5555-5555-5555-555555555555"
    store = FakeStore(get_results={cid: {"id": cid, "content": "FULL",
                                         "namespace": "shared", "source": "a.md",
                                         "tags": ["x"]}})
    out = _call(build_gateway(make_memory(store)), "shared_read", {"chunk_id": cid})
    assert "FULL" in out
    assert cid in out


def test_read_refuses_agent_private_chunk():
    cid = "66666666-6666-6666-6666-666666666666"
    store = FakeStore(get_results={cid: {"id": cid, "content": "PRIVATE",
                                         "namespace": "agent-runtime:claude"}})
    out = _call(build_gateway(make_memory(store)), "shared_read", {"chunk_id": cid})
    assert "not found" in out.lower()
    assert "PRIVATE" not in out  # the private content never leaks


def test_read_malformed_id_not_found():
    out = _call(build_gateway(make_memory(FakeStore())), "shared_read", {"chunk_id": "nope"})
    assert "not found" in out.lower()


# ── shared_stats ───────────────────────────────────────────────────────────


def test_stats_reports_count():
    store = FakeStore(existing_ns={"shared"}, ns_counts={"shared": 12})
    out = _call(build_gateway(make_memory(store)), "shared_stats", {})
    assert out == "shared: 12 chunks"


def test_stats_unavailable_when_store_errors():
    m = make_memory(FakeStore(raise_on={"list_namespace_meta"}))
    out = _call(build_gateway(m), "shared_stats", {})
    assert "unavailable" in out.lower()


# ── host/port at construction (DNS-rebinding policy seam) ───────────────────


def test_loopback_construction_enables_dns_rebinding_protection():
    mcp = build_gateway(make_memory(FakeStore()))  # default 127.0.0.1
    assert mcp.settings.host == "127.0.0.1"
    # FastMCP auto-enables DNS-rebinding protection for a loopback bind in
    # __init__ — proves host reached construction, not a post-hoc mutation.
    assert mcp.settings.transport_security is not None


def test_nonloopback_host_set_at_construction():
    mcp = build_gateway(make_memory(FakeStore()), host="0.0.0.0", port=8765)
    assert mcp.settings.host == "0.0.0.0"
    assert mcp.settings.port == 8765


# ── board_* read tools (Phase 2, D3) ─────────────────────────────────────────


@pytest.fixture
def board(tmp_path):
    bd = Board(str(tmp_path / "board.db"), now=lambda: 1000.0, stall_seconds=100)
    yield bd
    bd.close()


def test_board_tools_disabled_when_no_board():
    # All three board_* tools EXIST (stable surface) but report "not enabled" with no
    # board injected — they never crash or leak.
    mcp = build_gateway(make_memory(FakeStore()))  # board=None
    assert "not enabled" in _call(mcp, "board_list", {})
    assert "not enabled" in _call(mcp, "board_get", {"item_id": "x"})
    assert "not enabled" in _call(mcp, "board_stats", {})


def test_board_list_renders_items(board):
    board.add("first task", goal="epic")
    board.add("second task", goal="epic")
    mcp = build_gateway(make_memory(FakeStore()), board=board)
    out = _call(mcp, "board_list", {})
    assert "first task" in out and "second task" in out


def test_board_list_filters_and_empty_message(board):
    board.add("only planned")
    mcp = build_gateway(make_memory(FakeStore()), board=board)
    assert "No board items match." == _call(mcp, "board_list", {"state": "done"})
    assert "only planned" in _call(mcp, "board_list", {"state": "planned"})


def test_board_list_clamps_limit(board):
    for i in range(5):
        board.add(f"task {i}")
    mcp = build_gateway(make_memory(FakeStore()), board=board)
    # an absurd limit is clamped (no crash); a limit of 2 returns at most 2 lines
    out = _call(mcp, "board_list", {"limit": 2})
    assert len([ln for ln in out.splitlines() if ln.strip()]) == 2
    assert "currently unavailable" not in _call(mcp, "board_list", {"limit": 10**9})


def test_board_list_unknown_state_is_surfaced_not_crash(board):
    mcp = build_gateway(make_memory(FakeStore()), board=board)
    out = _call(mcp, "board_list", {"state": "bogus"})
    assert "unavailable" in out.lower()  # BoardError surfaced, server intact


def test_board_get_found_and_not_found(board):
    it = board.add("inspect me", goal="g")
    mcp = build_gateway(make_memory(FakeStore()), board=board)
    out = _call(mcp, "board_get", {"item_id": it.id})
    assert it.id in out and "inspect me" in out and "history:" in out
    assert "not found" in _call(
        mcp, "board_get", {"item_id": "11111111-2222-3333-4444-555555555555"}
    )


def test_board_get_malformed_id_is_not_found(board):
    mcp = build_gateway(make_memory(FakeStore()), board=board)
    # a non-uuid id is a client typo → not-found, never a probe (board.get guards it)
    assert "not found" in _call(mcp, "board_get", {"item_id": "definitely-not-a-uuid"})


def test_board_stats_reports(board):
    board.add("a")
    board.add("b")
    mcp = build_gateway(make_memory(FakeStore()), board=board)
    out = _call(mcp, "board_stats", {})
    assert "items: 2" in out and "planned=2" in out
