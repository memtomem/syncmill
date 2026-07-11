"""resolve_run_id — the single run-identity resolver (S2).

Precedence: explicit > monitor-inner env (sentinel-gated) > fresh uuid4. All
hermetic: the "inner" state is just the env var + sentinel file, exactly as
`test_monitor.py` drives `inner_dir`.
"""

from __future__ import annotations

from uuid import UUID

import syncmill.monitor as M
from syncmill.report import resolve_run_id


def _make_inner(monkeypatch, tmp_path):
    """Make this process look like the in-tmux INNER: env var + sentinel file."""
    monkeypatch.setenv(M.INNER_ENV, str(tmp_path))
    (tmp_path / M.SENTINEL_NAME).write_text("")


def test_direct_mints_fresh_uuid(monkeypatch):
    monkeypatch.delenv(M.INNER_ENV, raising=False)
    monkeypatch.delenv(M.RUN_ID_ENV, raising=False)
    first = resolve_run_id()
    UUID(first)  # a real uuid4 string
    assert resolve_run_id() != first  # fresh per call


def test_explicit_wins_over_inner_env(monkeypatch, tmp_path):
    _make_inner(monkeypatch, tmp_path)
    monkeypatch.setenv(M.RUN_ID_ENV, "from-env")
    assert resolve_run_id("given") == "given"


def test_empty_explicit_counts_as_unset(monkeypatch):
    # The `-r ""` house rule: an empty string is "not provided", never an id.
    monkeypatch.delenv(M.INNER_ENV, raising=False)
    monkeypatch.delenv(M.RUN_ID_ENV, raising=False)
    UUID(resolve_run_id(""))


def test_inner_env_honored_with_sentinel(monkeypatch, tmp_path):
    _make_inner(monkeypatch, tmp_path)
    monkeypatch.setenv(M.RUN_ID_ENV, "outer-minted")
    assert resolve_run_id() == "outer-minted"


def test_run_id_env_ignored_without_inner_env(monkeypatch):
    # A stray exported SYNCMILL_RUN_ID alone must never leak into a normal run.
    monkeypatch.delenv(M.INNER_ENV, raising=False)
    monkeypatch.setenv(M.RUN_ID_ENV, "stray")
    assert resolve_run_id() != "stray"


def test_run_id_env_ignored_without_sentinel(monkeypatch, tmp_path):
    # Env var without the sentinel file → not inner (inner_dir's own gate).
    monkeypatch.setenv(M.INNER_ENV, str(tmp_path))  # no sentinel written
    monkeypatch.setenv(M.RUN_ID_ENV, "stray")
    assert resolve_run_id() != "stray"


def test_inner_without_run_id_env_falls_back_fresh(monkeypatch, tmp_path):
    # An older outer that didn't put SYNCMILL_RUN_ID on the snapshot → fresh id,
    # never a failure.
    _make_inner(monkeypatch, tmp_path)
    monkeypatch.delenv(M.RUN_ID_ENV, raising=False)
    UUID(resolve_run_id())
