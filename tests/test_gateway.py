"""gateway.py — the read-only MCP gateway.

Covers: exactly-six-read-only-tools registration, per-tool behavior, the
agent-isolation guard, the top_k clamp, healthy-empty-vs-unavailable (per-call,
not the sticky degraded flag), and degradation. No real server is started and no
real memtomem is constructed — tools are driven directly via MCPServer.call_tool
against a fake-store-backed SyncmillMemory. (Host/port moved to run-time
transport kwargs on the mcp 2.0 SDK — the CLI plumbing is covered in
test_cli.py's serve tests; the REAL SDK's kwargs + loopback DNS-rebinding
auto-protection are pinned here, at the end.)
"""

from __future__ import annotations

import pytest

from syncmill.board import Board
from syncmill.gateway import MAX_TOP_K, build_gateway
from syncmill.memory import run_async

from conftest import FakeBackend, make_memory


def _call(mcp, name, args):
    """Drive a tool and return its text result.

    mcp 2.0's MCPServer.call_tool returns a ``CallToolResult`` whose
    ``structured_content`` is ``{"result": <str>}`` for a str-returning tool.
    """
    result = run_async(mcp.call_tool(name, args))
    return result.structured_content["result"]


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
    mcp = build_gateway(make_memory(FakeBackend()))
    names = {t.name for t in run_async(mcp.list_tools())}
    # Exact-set equality is the regression guard: no write/delete/index tool — for
    # memory OR the board — can ever sneak in, and the surface stays read-only.
    assert names == {
        "shared_search", "shared_read", "shared_stats",
        "board_list", "board_get", "board_stats",
    }


# ── shared_search ──────────────────────────────────────────────────────────


def test_search_formats_hits_with_chunk_id():
    store = FakeBackend(search_results={"shared": [SHARED_HIT]})
    out = _call(build_gateway(make_memory(store)), "shared_search", {"query": "q"})
    assert "abc" in out  # id present so a client can follow up with shared_read
    assert "shared knowledge here" in out


def test_search_no_matches_message():
    out = _call(build_gateway(make_memory(FakeBackend())), "shared_search", {"query": "q"})
    assert out == "No matches in shared memory."


def test_search_unavailable_when_store_errors():
    m = make_memory(FakeBackend(raise_on={"search"}))
    out = _call(build_gateway(m), "shared_search", {"query": "q"})
    assert "unavailable" in out.lower()


@pytest.mark.parametrize("bad", [-5, 0, 10_000])
def test_search_clamps_top_k(bad):
    store = FakeBackend(search_results={"shared": [SHARED_HIT]})
    _call(build_gateway(make_memory(store)), "shared_search", {"query": "q", "top_k": bad})
    sent = store.searched[-1]["top_k"]
    assert 1 <= sent <= MAX_TOP_K


def test_search_empty_vs_unavailable_is_per_call_not_sticky():
    # Even with the memory object already flagged degraded, a HEALTHY empty search
    # must say "No matches", not "unavailable" — the tool reads the per-call
    # return value, never the sticky degraded flag.
    m = make_memory(FakeBackend())
    m.degraded = True
    out = _call(build_gateway(m), "shared_search", {"query": "q"})
    assert out == "No matches in shared memory."


# ── shared_read ────────────────────────────────────────────────────────────


def test_read_returns_full_shared_chunk():
    cid = "55555555-5555-5555-5555-555555555555"
    store = FakeBackend(get_results={cid: {"id": cid, "content": "FULL",
                                         "namespace": "shared", "source": "a.md",
                                         "tags": ["x"]}})
    out = _call(build_gateway(make_memory(store)), "shared_read", {"chunk_id": cid})
    assert "FULL" in out
    assert cid in out


def test_read_refuses_agent_private_chunk():
    cid = "66666666-6666-6666-6666-666666666666"
    store = FakeBackend(get_results={cid: {"id": cid, "content": "PRIVATE",
                                         "namespace": "agent-runtime:claude"}})
    out = _call(build_gateway(make_memory(store)), "shared_read", {"chunk_id": cid})
    assert "not found" in out.lower()
    assert "PRIVATE" not in out  # the private content never leaks


def test_read_malformed_id_not_found():
    # even with a backend that would fail: the id is rejected before any read
    memory = make_memory(FakeBackend(raise_on={"get"}))
    out = _call(build_gateway(memory), "shared_read", {"chunk_id": "nope"})
    assert "not found" in out.lower()
    assert "unavailable" not in out.lower()
    assert memory.degraded is False


def test_read_backend_failure_reports_unavailable_not_missing():
    # #169: a storage failure on THIS call is "unavailable", never "not found"
    cid = "55555555-5555-5555-5555-555555555555"
    out = _call(
        build_gateway(make_memory(FakeBackend(raise_on={"get"}))),
        "shared_read",
        {"chunk_id": cid},
    )
    assert out == "shared memory is currently unavailable"
    assert "not found" not in out.lower()


def test_read_outcomes_are_per_call_across_consecutive_calls():
    # One gateway, four consecutive calls: error → unavailable, then a healthy
    # miss → not found (the earlier degrade never poisons it), a shared chunk →
    # content, a private chunk → not found with no content leak.
    missing = "55555555-5555-5555-5555-555555555555"
    shared = "77777777-7777-7777-7777-777777777777"
    private = "66666666-6666-6666-6666-666666666666"
    store = FakeBackend(
        raise_on={"get"},
        get_results={
            shared: {"id": shared, "content": "FULL", "namespace": "shared",
                     "source": "a.md", "tags": []},
            private: {"id": private, "content": "PRIVATE",
                      "namespace": "agent-runtime:claude"},
        },
    )
    mcp = build_gateway(make_memory(store))
    assert _call(mcp, "shared_read", {"chunk_id": missing}) == (
        "shared memory is currently unavailable"
    )
    store._raise_on.discard("get")
    out = _call(mcp, "shared_read", {"chunk_id": missing})
    assert "not found" in out.lower() and "unavailable" not in out.lower()
    assert "FULL" in _call(mcp, "shared_read", {"chunk_id": shared})
    out = _call(mcp, "shared_read", {"chunk_id": private})
    assert "not found" in out.lower() and "PRIVATE" not in out


# ── shared_stats ───────────────────────────────────────────────────────────


def test_stats_reports_count():
    store = FakeBackend(existing_ns={"shared"}, ns_counts={"shared": 12})
    out = _call(build_gateway(make_memory(store)), "shared_stats", {})
    assert out == "shared: 12 chunks"


def test_stats_unavailable_when_store_errors():
    m = make_memory(FakeBackend(raise_on={"count_namespace"}))
    out = _call(build_gateway(m), "shared_stats", {})
    assert "unavailable" in out.lower()


# ── board_* read tools (Phase 2, D3) ─────────────────────────────────────────


@pytest.fixture
def board(tmp_path):
    bd = Board(str(tmp_path / "board.db"), now=lambda: 1000.0, stall_seconds=100)
    yield bd
    bd.close()


def test_board_tools_disabled_when_no_board():
    # All three board_* tools EXIST (stable surface) but report "not enabled" with no
    # board injected — they never crash or leak.
    mcp = build_gateway(make_memory(FakeBackend()))  # board=None, no error
    assert "not enabled" in _call(mcp, "board_list", {})
    assert "not enabled" in _call(mcp, "board_get", {"item_id": "x"})
    assert "not enabled" in _call(mcp, "board_stats", {})


def test_board_tools_unavailable_when_board_errored():
    # codex-found major: an ENABLED-but-broken board (build failed) must report
    # "unavailable", NOT "not enabled" — the disabled-vs-broken distinction. board=None
    # + board_error set → the tools surface the error while the gateway stays alive.
    mcp = build_gateway(make_memory(FakeBackend()), board_error="corrupt db at /x")
    for tool, args in (("board_list", {}), ("board_get", {"item_id": "x"}),
                       ("board_stats", {})):
        out = _call(mcp, tool, args)
        assert "currently unavailable" in out and "corrupt db at /x" in out
        assert "not enabled" not in out


def test_board_list_renders_items_most_recent_first(board):
    board.add("first task", goal="epic")
    board.add("second task", goal="epic")
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    out = _call(mcp, "board_list", {})
    assert "first task" in out and "second task" in out
    # the surface advertises most-recent-first — the newest item leads (NOT FIFO)
    assert out.index("second task") < out.index("first task")


def test_board_list_filters_and_empty_message(board):
    board.add("only planned")
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    assert "No board items match." == _call(mcp, "board_list", {"state": "done"})
    assert "only planned" in _call(mcp, "board_list", {"state": "planned"})


def test_board_list_limit_keeps_newest_not_oldest(board):
    for i in range(5):
        board.add(f"task {i}")  # rowid order preserved (constant clock)
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    out = _call(mcp, "board_list", {"limit": 2})
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 2
    # under truncation the NEWEST items survive (task 4, task 3) — NOT the oldest;
    # and they are most-recent-first. (The bug fixed here was `items[:limit]` keeping
    # the oldest and dropping all recent work, mislabeled "most-recent-first".)
    assert "task 4" in lines[0] and "task 3" in lines[1]
    assert "task 0" not in out and "task 1" not in out
    # an absurd limit is clamped, never a crash
    assert "currently unavailable" not in _call(mcp, "board_list", {"limit": 10**9})


def test_board_list_materializes_only_the_returned_rows(board, monkeypatch):
    # #167: limit=1 on a 50-item board converts ONE row, not the whole board
    import syncmill.board as board_module

    for i in range(50):
        board.add(f"task {i}")
    calls = []
    original = board_module._row_to_item

    def counting(row):
        calls.append(row["id"])
        return original(row)

    monkeypatch.setattr(board_module, "_row_to_item", counting)
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    out = _call(mcp, "board_list", {"limit": 1})
    assert "task 49" in out and "task 48" not in out
    assert len(calls) == 1
    calls.clear()
    assert "task 49" in _call(mcp, "board_list", {"limit": 3})
    assert len(calls) == 3


def test_board_list_clamps_the_limit_before_the_store_call(board, monkeypatch):
    # the bound handed to the store is the CLAMPED one: negative/zero → 1,
    # oversized → MAX_BOARD_LIMIT (removing the clamp would pass -5 through)
    board.add("x")
    seen = []
    original = board.list_recent

    def recording(**kwargs):
        seen.append(kwargs["limit"])
        return original(**kwargs)

    monkeypatch.setattr(board, "list_recent", recording)
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    for limit in (-5, 0, 10**9):
        assert "x" in _call(mcp, "board_list", {"limit": limit})
    assert seen == [1, 1, 200]


def test_board_list_store_failures_report_unavailable(board, monkeypatch):
    # a SQLite failure or a corrupt stored row is a backend outage
    # ("unavailable"), distinct from the client-argument error above
    import sqlite3

    import syncmill.board as board_module

    board.add("x")
    mcp = build_gateway(make_memory(FakeBackend()), board=board)

    def corrupt_row(row):
        raise ValueError("'bogus' is not a valid WorkState")

    # monkeypatch.context(), never undo(): undo() would also reverse the
    # autouse isolation fixtures
    with monkeypatch.context() as patched:
        patched.setattr(board_module, "_row_to_item", corrupt_row)
        out = _call(mcp, "board_list", {})
    assert out.startswith("the work-board is currently unavailable: list failed")

    real_execute = board._conn.execute

    def failing(sql, *args):
        if "ORDER BY created_at DESC" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return real_execute(sql, *args)

    with monkeypatch.context() as patched:
        patched.setattr(board, "_conn", _FailingConn(board._conn, failing))
        out = _call(mcp, "board_list", {})
    assert "currently unavailable" in out and "disk I/O error" in out


class _FailingConn:
    def __init__(self, conn, execute):
        self._conn, self._execute = conn, execute

    def execute(self, sql, *args):
        return self._execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_board_list_filter_and_limit_return_the_newest_match(board):
    # a filter applies before the limit: the newest MATCHING item is returned
    # even when newer non-matching items exist
    board.add("old epic work", goal="epic")
    for i in range(3):
        board.add(f"unrelated {i}", goal="other")
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    out = _call(mcp, "board_list", {"goal": "epic", "limit": 1})
    assert "old epic work" in out and "unrelated" not in out


def test_board_list_unknown_state_is_a_client_error_not_unavailable(board):
    # A bad `state` filter is a CLIENT argument error on a HEALTHY board — it must NOT
    # be mislabeled "unavailable" (a backend outage), which would make an LLM abandon a
    # perfectly good board. It returns a corrective message naming the valid states.
    # MUTATION GUARD: dropping the up-front state validation lets board.list raise
    # BoardError → the catch reports "...unavailable: unknown state..." → this fails.
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    out = _call(mcp, "board_list", {"state": "bogus"})
    assert "unavailable" not in out.lower()  # a healthy board is NOT an outage
    assert "bogus" in out  # the offending value is echoed back
    assert "planned" in out and "done" in out  # the valid set is offered to the client


def test_board_get_found_and_not_found(board):
    it = board.add("inspect me", goal="g")
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    out = _call(mcp, "board_get", {"item_id": it.id})
    assert it.id in out and "inspect me" in out and "history:" in out
    assert "not found" in _call(
        mcp, "board_get", {"item_id": "11111111-2222-3333-4444-555555555555"}
    )


def test_board_get_malformed_id_is_not_found(board):
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    # a non-uuid id is a client typo → not-found, never a probe (board.get guards it)
    assert "not found" in _call(mcp, "board_get", {"item_id": "definitely-not-a-uuid"})


def test_board_stats_reports(board):
    board.add("a")
    board.add("b")
    mcp = build_gateway(make_memory(FakeBackend()), board=board)
    out = _call(mcp, "board_stats", {})
    assert "items: 2" in out and "planned=2" in out


# ── real-SDK HTTP transport seam ─────────────────────────────────────────────


def test_real_sdk_runtime_host_port_and_loopback_rebinding_protection():
    """Pin the 2.0 seam `serve --http` rides against the REAL SDK.

    host/port are RUN-time transport kwargs (`run(transport="streamable-http",
    host=, port=)` forwards to `run_streamable_http_async`), and the SDK
    auto-enables DNS-rebinding protection exactly for loopback hosts — the
    condition the CLI's non-loopback warning mirrors. The FakeServer-based CLI
    tests can't see either fact, so a 2.x minor that renames the kwargs or
    changes the loopback host list must fail HERE instead of silently breaking
    `serve --http` or dropping the protection.
    """
    import inspect

    from mcp.server.mcpserver import MCPServer

    params = inspect.signature(MCPServer.run_streamable_http_async).parameters
    assert {"host", "port"} <= set(params)

    loopback = MCPServer("loopback-probe")
    loopback.streamable_http_app(host="127.0.0.1")
    settings = loopback._lowlevel_server._session_manager.security_settings
    assert settings is not None and settings.enable_dns_rebinding_protection
    assert set(settings.allowed_hosts) == {"127.0.0.1:*", "localhost:*", "[::1]:*"}

    # a non-loopback bind gets NO auto-protection — exactly why the CLI warns
    exposed = MCPServer("nonloopback-probe")
    exposed.streamable_http_app(host="0.0.0.0")
    assert exposed._lowlevel_server._session_manager.security_settings is None
