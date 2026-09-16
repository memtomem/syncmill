"""Workflow SQLite connections close deterministically, without relying on GC."""

import sqlite3

import pytest

import syncmill.workflow as workflow_module
from syncmill.workflow import WorkflowStore


@pytest.fixture
def opened_connections(monkeypatch):
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        # Strong references make the test independent of cyclic GC timing.
        connections.append(connection)
        return connection

    monkeypatch.setattr(workflow_module.sqlite3, "connect", tracked_connect)
    try:
        yield connections
    finally:
        for connection in connections:
            connection.close()


def assert_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_repeated_workflow_reads_release_connections(tmp_path, opened_connections):
    store = WorkflowStore(tmp_path, "project")
    for _ in range(30):
        assert store.list() == []
    assert len(opened_connections) == 31
    assert_closed(opened_connections)


def test_connection_closure_preserves_commit_and_rollback(tmp_path, opened_connections):
    store = WorkflowStore(tmp_path, "project")
    with store._connect() as connection:
        connection.execute("CREATE TABLE receipts (value INTEGER)")
        connection.execute("INSERT INTO receipts VALUES (1)")
    with pytest.raises(RuntimeError, match="abort"):
        with store._connect() as connection:
            connection.execute("INSERT INTO receipts VALUES (2)")
            raise RuntimeError("abort")
    with store._connect() as connection:
        assert [row[0] for row in connection.execute("SELECT value FROM receipts")] == [
            1
        ]
    assert_closed(opened_connections)


@pytest.mark.parametrize("pragma", ["journal_mode=WAL", "foreign_keys=ON"])
def test_connection_closes_when_setup_fails(tmp_path, monkeypatch, pragma):
    connect = sqlite3.connect
    connections = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == f"PRAGMA {pragma}":
                raise sqlite3.OperationalError("setup failed")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=FailingConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(workflow_module.sqlite3, "connect", failing_connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match="setup failed"):
            WorkflowStore(tmp_path, "project")
        assert_closed(connections)
    finally:
        for connection in connections:
            connection.close()
