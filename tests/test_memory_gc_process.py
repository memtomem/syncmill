"""Opt-in real memtomem, file locks, watcher and independent-process evidence."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from syncmill.config import MemoryConfig
from syncmill.memory import run_async
from syncmill.memory_backend import MemtomemBackend

NS = "board-run:11111111-1111-4111-8111-111111111111"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("SYNCMILL_TEST_MEMTOMEM_REAL") != "1",
                      reason="set SYNCMILL_TEST_MEMTOMEM_REAL=1 for real GC processes"),
]


@pytest.fixture
def process_store(tmp_path):
    (tmp_path / "mem").mkdir()
    backend = MemtomemBackend(MemoryConfig(
        db_path=str(tmp_path / "mm.db"), memory_dir=str(tmp_path / "mem"),
    ))
    run_async(backend.add("private seed stage", tags=["pipeline-stage"], namespace=NS))
    run_async(backend.add("public result must survive", tags=["task-result"], namespace="shared"))
    children = []

    def spawn(mode, name):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MEMTOMEM_", "SYNCMILL_"))}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("memory_gc_worker.py")),
             str(tmp_path), mode, name],
            cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        children.append(proc)
        return proc

    try:
        yield backend, spawn
    finally:
        for proc in children:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)
        run_async(backend.close())


def wait_marker(root, name, stage, proc):
    deadline = time.monotonic() + 20
    while not (root / f"{name}.{stage}").exists():
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=5)
            pytest.fail(f"worker {name} exited {proc.returncode}: {out}\n{err}")
        assert time.monotonic() < deadline, f"worker {name} did not reach {stage}"
        time.sleep(0.01)


def finished(proc, expected=0):
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == expected, (out, err)
    return json.loads(out) if expected == 0 else None


def assert_clean(backend, root):
    comp = run_async(backend._store._ensure_init())
    # Exercise real incremental reindex, then read authoritative namespace rows.
    for path in (root / "mem").glob("*.md"):
        run_async(comp.index_engine.index_file(path, already_scanned=True))
    assert run_async(backend.count_namespace(NS)) == 0
    assert run_async(backend.count_namespace("shared")) > 0
    assert not list((root / "mem").glob("*.board-run.*.md"))
    run_async(backend._invalidate_search_cache(fail_closed=True))
    rows = run_async(backend.search("private seed stage", namespace="shared", top_k=20))
    assert not any("private seed stage" in row["content"] for row in rows)


def test_process_writer_before_purge(tmp_path, process_store):
    backend, spawn = process_store
    writer = spawn("paused-write", "writer")
    wait_marker(tmp_path, "writer", "write-held", writer)
    purge = spawn("purge", "purge")
    wait_marker(tmp_path, "purge", "namespace-attempt", purge)
    assert not (tmp_path / "purge.namespace-held").exists()
    (tmp_path / "writer.release").touch()
    assert finished(writer)["ids"]
    assert finished(purge)["removed"] > 0
    assert_clean(backend, tmp_path)


def test_process_writer_during_purge_recreates_only_scoped_rows(tmp_path, process_store):
    backend, spawn = process_store
    purge = spawn("paused-purge", "purge")
    wait_marker(tmp_path, "purge", "namespace-held", purge)
    writer = spawn("write", "writer")
    wait_marker(tmp_path, "writer", "namespace-attempt", writer)
    assert not (tmp_path / "writer.namespace-held").exists()
    (tmp_path / "purge.release").touch()
    assert finished(purge)["removed"] > 0
    assert finished(writer)["ids"]
    assert run_async(backend.count_namespace(NS)) > 0
    # Late activity is deliberately allowed; the board re-arms its tombstone.
    assert run_async(backend.purge_namespace(NS)) > 0
    assert_clean(backend, tmp_path)


def test_two_process_purges_serialize_and_are_idempotent(tmp_path, process_store):
    backend, spawn = process_store
    first = spawn("paused-purge", "first")
    wait_marker(tmp_path, "first", "namespace-held", first)
    second = spawn("purge", "second")
    wait_marker(tmp_path, "second", "namespace-attempt", second)
    assert not (tmp_path / "second.namespace-held").exists()
    (tmp_path / "first.release").touch()
    assert finished(first)["removed"] > 0
    assert finished(second)["removed"] == 0
    assert_clean(backend, tmp_path)


def test_process_crash_between_files_and_rows_is_retryable(tmp_path, process_store):
    backend, spawn = process_store
    proc = spawn("crash-after-unlink", "crash")
    finished(proc, expected=23)
    assert (tmp_path / "crash.files-removed").exists()
    assert not list((tmp_path / "mem").glob("*.board-run.*.md"))
    assert run_async(backend.count_namespace(NS)) > 0
    # OS released the dead writer's real namespace + source locks.
    assert run_async(backend.purge_namespace(NS)) == 0
    assert_clean(backend, tmp_path)


def test_purge_races_actual_watcher_after_read_before_commit(tmp_path, process_store):
    backend, spawn = process_store
    watcher = spawn("watcher", "watcher")
    wait_marker(tmp_path, "watcher", "ready", watcher)
    source = next((tmp_path / "mem").glob("*.board-run.*.md"))
    with source.open("a") as handle:
        handle.write("\n\n## Watcher update\n\nprivate watcher stage\n")
    wait_marker(tmp_path, "watcher", "read", watcher)
    purge = spawn("purge", "purge")
    wait_marker(tmp_path, "purge", "source-attempt", purge)
    assert not (tmp_path / "purge.source-held").exists()
    (tmp_path / "watcher.release").touch()
    assert finished(purge)["removed"] > 0
    (tmp_path / "watcher.stop").touch()
    assert finished(watcher)["watcher_observed"]
    assert_clean(backend, tmp_path)


@pytest.mark.parametrize("count", [255, 256, 257])
def test_real_source_lock_limit_and_descriptor_cleanup(tmp_path, monkeypatch, count):
    from contextlib import asynccontextmanager
    from datetime import date, timedelta
    import resource

    from memtomem.context import _atomic
    from syncmill.memory_backend import UnprovablePurgeError

    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    backend = MemtomemBackend(MemoryConfig(
        db_path=str(tmp_path / "mm.db"), memory_dir=str(mem_dir),
    ))
    paths = [mem_dir / f"{date(2025, 1, 1) + timedelta(days=i)}.{NS.replace(':', '.')}.md"
             for i in range(count)]
    for path in paths:
        path.write_text("scratch")
    held = 0
    peak = 0
    original_lock = _atomic.async_file_lock

    @asynccontextmanager
    async def observed(path, **kwargs):
        nonlocal held, peak
        async with original_lock(path, **kwargs):
            held += 1
            peak = max(peak, held)
            try:
                yield
            finally:
                held -= 1

    monkeypatch.setattr(_atomic, "async_file_lock", observed)
    original_limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    # This test owns its process limit; production never changes it. A separate
    # test below checks safe refusal with real low descriptor headroom.
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(512, original_limits[0]), original_limits[1]))
    try:
        run_async(backend.ensure_ready())
        fd_root = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
        before = len(list(fd_root.iterdir()))
        if count > 256:
            with pytest.raises(UnprovablePurgeError):
                run_async(backend.purge_namespace(NS))
            assert all(path.exists() for path in paths)
            assert peak == 1  # namespace lock only; refuses before source locks
        else:
            assert run_async(backend.purge_namespace(NS)) == count
            assert peak == count + 1
            assert not any(path.exists() for path in paths)
        assert held == 0
        assert len(list(fd_root.iterdir())) == before
    finally:
        run_async(backend.close())
        resource.setrlimit(resource.RLIMIT_NOFILE, original_limits)


def test_real_low_descriptor_headroom_refuses_before_source_locks(tmp_path, process_store):
    import resource
    from datetime import date, timedelta
    from syncmill.memory_backend import UnprovablePurgeError

    backend, _ = process_store
    for i in range(40):
        (tmp_path / "mem" / f"{date(2025, 1, 1) + timedelta(days=i)}.{NS.replace(':', '.')}.md").write_text("scratch")
    paths = list((tmp_path / "mem").glob("*.md"))
    fd_root = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
    before = len(list(fd_root.iterdir()))
    limits = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (before + 40, limits[1]))
        with pytest.raises(UnprovablePurgeError, match="file descriptors"):
            run_async(backend.purge_namespace(NS))
        assert all(path.exists() for path in paths)
        assert run_async(backend.count_namespace(NS)) > 0
        assert len(list(fd_root.iterdir())) == before
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, limits)
