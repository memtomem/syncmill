"""Hermetic regressions for #166: no artifact path may redirect I/O."""

import hashlib
import json
import os

import pytest

from syncmill.artifact_io import ArtifactIO
from syncmill.artifacts import ArtifactError, ArtifactStore
from syncmill.models import Task, TaskResult, TaskStatus
from test_v2_workflow import _pending_artifact, _repo
from syncmill.worktree import repository_fingerprint


def _record(path, **extra):
    path.mkdir(parents=True, exist_ok=True)
    manifest = {"state": "pending", **extra}
    (path / "manifest.json").write_text(json.dumps(manifest))
    return path / "manifest.json"


@pytest.mark.parametrize(
    "value", ["", ".", "..", "../external", "/external", "a/b", "a\\b", "a\0b"]
)
def test_invalid_ids_do_not_touch_outside(tmp_path, value):
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path / "new-store", value)
    assert not (tmp_path / "new-store").exists()
    store = ArtifactStore(tmp_path / "store", "project")
    task = Task(prompt="test")
    calls = [
        lambda: store.load(value),
        lambda: store.patch(value),
        lambda: store.discard(value),
        lambda: store.supersede(value, "child"),
        lambda: store.read_result(value),
        lambda: store.close_conversation(value),
        lambda: store.recovery_required(value, "test"),
        lambda: store.begin(
            run_id=value,
            task=task,
            repo_root=tmp_path,
            snapshot_commit="",
            source_fingerprint="",
            strategy="route",
            agents=[],
            plan={},
        ),
        lambda: store.save(
            run_id=value,
            task=task,
            result=TaskResult(
                task_id=task.id,
                agent_id="test",
                status=TaskStatus.FAILED,
                output="",
                exit_code=1,
                duration_seconds=0,
            ),
            repo_root=tmp_path,
            snapshot_commit="",
            source_fingerprint="",
            strategy="route",
        ),
    ]
    for call in calls:
        with pytest.raises(ArtifactError):
            call()
    assert set(store.root.iterdir()) <= {store.root / ".apply.lock"}


@pytest.mark.parametrize(
    "entry", ["run", "manifest.json", "winner.patch", "result.json", ".apply.lock"]
)
def test_symlink_reads_and_writes_leave_external_unchanged(tmp_path, entry):
    store = ArtifactStore(tmp_path / "store", "project")
    external = _record(tmp_path / "external")
    before = external.read_bytes(), external.stat().st_mode
    run = store.root / "run-1"
    _record(run, patch_sha256=hashlib.sha256(before[0]).hexdigest())
    if entry == "run":
        (run / "manifest.json").unlink()
        run.rmdir()
        run.symlink_to(external.parent)
        operations = [lambda: store.load("run-1"), lambda: store.discard("run-1")]
        assert store.list() == []
    else:
        dest = (store.root if entry == ".apply.lock" else run) / entry
        dest.unlink(missing_ok=True)
        dest.symlink_to(external)
        operations = {
            "manifest.json": [
                lambda: store.load("run-1"),
                lambda: store.discard("run-1"),
            ],
            "winner.patch": [lambda: store.patch("run-1")],
            "result.json": [lambda: store.read_result("run-1")],
            ".apply.lock": [lambda: store.discard("run-1")],
        }[entry]
    for call in operations:
        with pytest.raises(ArtifactError):
            call()
    assert (external.read_bytes(), external.stat().st_mode) == before


@pytest.mark.parametrize("key", ["run_id", "project_id"])
@pytest.mark.parametrize("value", ["different", None, 123])
def test_manifest_binding_and_legacy_compatibility(tmp_path, key, value):
    store = ArtifactStore(tmp_path, "project")
    _record(store.root / "legacy")
    store.discard("legacy")
    assert "run_id" not in store.load("legacy").manifest
    _record(store.root / "bad", **{key: value})
    with pytest.raises(ArtifactError, match="mismatch"):
        store.discard("bad")
    assert [a.run_id for a in store.list()] == ["legacy"]


def test_run_swap_between_read_and_write_does_not_redirect(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "store", "project")
    run = store.root / "run-1"
    _record(run)
    external = _record(tmp_path / "external")
    original = store._write_manifest

    def swap(path, manifest):
        run.rename(store.root / "original")
        run.symlink_to(external.parent)
        return original(path, manifest)

    monkeypatch.setattr(store, "_write_manifest", swap)
    with pytest.raises(ArtifactError):
        store.discard("run-1")
    assert json.loads(external.read_text())["state"] == "pending"
    assert (
        json.loads((store.root / "original/manifest.json").read_text())["state"]
        == "pending"
    )


def test_patch_replacement_after_verification_never_reaches_git(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    real_patch = store.patch

    def replace_patch(run_id):
        data = real_patch(run_id)
        (artifact.path / "winner.patch").write_bytes(data.replace(b"winner", b"evilxx"))
        return data

    monkeypatch.setattr(store, "patch", replace_patch)
    store.apply("run-1", repo, repository_fingerprint(repo))
    assert (repo / "winner.txt").read_text() == "winner\n"
    assert not (repo / "evilxx.txt").exists()


def test_receipt_directory_symlink_rolls_back_without_external_write(tmp_path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    external = tmp_path / "external"
    external.mkdir()
    (artifact.path / "applications").symlink_to(external)
    with pytest.raises(ArtifactError, match="rolled back"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    assert list(external.iterdir()) == []
    assert not (repo / "winner.txt").exists()
    assert store.load("run-1").manifest["state"] == "pending"


def test_prune_does_not_follow_nested_symlink(tmp_path):
    store = ArtifactStore(tmp_path / "store", "project")
    _record(store.root / "run-1", state="discarded")
    external = _record(tmp_path / "external")
    (store.root / "run-1/subdir").symlink_to(external.parent)
    assert store.prune(0) == ["run-1"]
    assert external.exists()


def test_symlink_project_or_runs_rejected_before_chmod(tmp_path):
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    mode = external.stat().st_mode
    (tmp_path / "project").symlink_to(external)
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path, "project")
    assert external.stat().st_mode == mode


def test_special_and_hardlinked_files_rejected(tmp_path):
    store = ArtifactStore(tmp_path / "store", "project")
    run = store.root / "run"
    _record(run)
    os.mkfifo(run / "winner.patch")
    with pytest.raises(ArtifactError):
        store.patch("run")
    external = tmp_path / "external"
    external.write_bytes(b"private")
    os.link(external, run / "result.json")
    with pytest.raises(ArtifactError):
        store.read_result("run")


def test_atomic_write_directory_swap_does_not_follow_new_name(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    run = base / "run"
    run.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    io = ArtifactIO(base)
    original = os.fsync

    def swap(fd):
        original(fd)
        if run.is_dir() and not run.is_symlink():
            run.rename(base / "old")
            run.symlink_to(outside)

    monkeypatch.setattr(os, "fsync", swap)
    try:
        with pytest.raises(OSError):
            io.write(run / "manifest.json", b"new")
    finally:
        io.close()
    assert list(outside.iterdir()) == []
    assert list((base / "old").iterdir()) == []


@pytest.mark.parametrize("entry", ["winner.patch", "result.json"])
def test_save_rejects_sidecar_symlink(tmp_path, entry):
    store = ArtifactStore(tmp_path / "store", "project")
    task = Task(prompt="test")
    store.begin(
        run_id="run-1",
        task=task,
        repo_root=tmp_path,
        snapshot_commit="",
        source_fingerprint="",
        strategy="route",
        agents=[],
        plan={},
    )
    external = tmp_path / "external"
    external.write_bytes(b"unchanged")
    (store.root / "run-1" / entry).symlink_to(external)
    result = TaskResult(
        task_id=task.id,
        agent_id="test",
        status=TaskStatus.FAILED,
        output="",
        exit_code=1,
        duration_seconds=0,
    )
    with pytest.raises(ArtifactError):
        store.save(
            run_id="run-1",
            task=task,
            result=result,
            repo_root=tmp_path,
            snapshot_commit="",
            source_fingerprint="",
            strategy="route",
        )
    assert external.read_bytes() == b"unchanged"
    assert store.load("run-1").manifest["state"] == "running"


@pytest.mark.parametrize("target", ["../external", "link"])
def test_apply_refuses_external_artifact(tmp_path, target):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    moved = store.root.parent / "external"
    artifact.path.rename(moved)
    (store.root / "link").symlink_to(moved)
    original = (moved / "manifest.json").read_bytes()
    with pytest.raises(ArtifactError):
        store.apply(target, repo, repository_fingerprint(repo))
    assert not (repo / "winner.txt").exists()
    assert (moved / "manifest.json").read_bytes() == original


def test_receipt_symlink_is_not_recovery_evidence(tmp_path):
    store = ArtifactStore(tmp_path / "store", "project")
    run = store.root / "run"
    manifest = {
        "state": "applying",
        "run_id": "run",
        "apply_id": "attempt",
        "patch_sha256": "digest",
    }
    _record(run, **manifest)
    external = tmp_path / "receipt.json"
    external.write_text(json.dumps(manifest))
    (run / "applications").mkdir()
    (run / "applications" / "receipt.json").symlink_to(external)
    assert store._bound_receipt(run, manifest) is None


def test_listing_closes_per_run_descriptors(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path, "project")
    for n in range(100):
        _record(store.root / str(n))
    high_water = 0
    original = ArtifactIO.open_file

    def observed(self, *args, **kwargs):
        nonlocal high_water
        fd = original(self, *args, **kwargs)
        high_water = max(high_water, len(self.fds))
        return fd

    monkeypatch.setattr(ArtifactIO, "open_file", observed)
    assert len(store.list()) == 100
    assert high_water <= 4  # base, project, runs, one run


def test_io_closes_fd_when_fdopen_fails(tmp_path, monkeypatch):
    io = ArtifactIO(tmp_path)
    path = tmp_path / "file"
    path.write_bytes(b"ok")
    opened = []
    original = os.open

    def track(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", track)

    def fail(*args, **kwargs):
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(os, "fdopen", fail)
    try:
        for call in (lambda: io.read(path), lambda: io.write(path, b"new")):
            with pytest.raises(OSError, match="fdopen"):
                call()
            with pytest.raises(OSError):
                os.fstat(opened[-1])
    finally:
        io.close()
    assert path.read_bytes() == b"ok"


@pytest.mark.parametrize(
    "command", [["discard"], ["diff"], ["runs", "show"], ["conversations", "show"]]
)
def test_cli_refuses_linked_run(tmp_path, monkeypatch, command):
    import importlib
    from click.testing import CliRunner

    module = importlib.import_module("syncmill.cli")
    store = ArtifactStore(tmp_path / "store", "project")
    external = _record(tmp_path / "external")
    before = external.read_bytes()
    (store.root / "linked-run").symlink_to(external.parent)
    monkeypatch.setattr(module, "_project_artifacts", lambda _: (tmp_path, None, store))
    result = CliRunner().invoke(module.cli, [*command, "linked-run"])
    assert result.exit_code != 0
    assert "Error:" in result.output
    assert external.read_bytes() == before


def test_non_utf8_patch_bytes_are_applied_losslessly(tmp_path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    patch = (
        (artifact.path / "winner.patch")
        .read_bytes()
        .replace(b"+winner\n", b"+\xff\xfe\n")
    )
    (artifact.path / "winner.patch").write_bytes(patch)
    manifest = store.load("run-1").manifest
    manifest["patch_sha256"] = hashlib.sha256(patch).hexdigest()
    store._write_manifest(artifact.path, manifest)
    store.apply("run-1", repo, repository_fingerprint(repo))
    assert (repo / "winner.txt").read_bytes() == b"\xff\xfe\n"


def test_locked_reads_and_recovery_have_bounded_descriptors(tmp_path):
    """Exercise the reviewer's real EMFILE scenario in an isolated process."""
    import subprocess
    import sys

    store = ArtifactStore(tmp_path, "project")
    for n in range(300):
        _record(store.root / f"run-{n}")
    script = """
import resource, sys
from pathlib import Path
from syncmill.artifacts import ArtifactStore
store = ArtifactStore(Path(sys.argv[1]), "project")
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(256, hard), hard))
with store.apply_lock():
    for n in range(300):
        assert store.load(f"run-{n}").manifest["state"] == "pending"
    assert store.reconcile_interrupted_applies() == []
print("300 locked reads and recovery passed")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "300 locked reads and recovery passed" in result.stdout


@pytest.mark.parametrize(
    "command", [["runs", "list"], ["runs", "prune"], ["conversations", "list"]]
)
def test_artifact_commands_after_directory_removal_report_clean_error(
    tmp_path, monkeypatch, command
):
    import importlib
    from click.testing import CliRunner

    module = importlib.import_module("syncmill.cli")
    store = ArtifactStore(tmp_path, "project")
    store.root.rmdir()
    monkeypatch.setattr(module, "_project_artifacts", lambda _: (tmp_path, None, store))
    result = CliRunner().invoke(module.cli, command)
    assert result.exit_code == 1
    assert "Error: artifact not found:" in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_replaced_root_requires_explicit_reopen(tmp_path):
    store = ArtifactStore(tmp_path, "project")
    store.root.rename(store.root.with_name("old-runs"))
    store.root.mkdir()
    with pytest.raises(ArtifactError, match="restart or reopen"):
        store.list()
    assert ArtifactStore(tmp_path, "project").list() == []


def test_expected_absence_and_duplicate_have_specific_messages(tmp_path):
    store = ArtifactStore(tmp_path, "project")
    task = Task(prompt="test")
    args = dict(
        run_id="run-1",
        task=task,
        repo_root=tmp_path,
        snapshot_commit="",
        source_fingerprint="",
        strategy="route",
        agents=[],
        plan={},
    )
    store.begin(**args)
    with pytest.raises(ArtifactError, match="artifact already exists:"):
        store.begin(**args)
    with pytest.raises(ArtifactError, match="artifact not found: result.json"):
        store.read_result("run-1")
    assert store.read_result("run-1", missing_ok=True) is None


def test_descriptor_writer_creates_private_file_without_fchmod(tmp_path, monkeypatch):
    import stat

    monkeypatch.delattr(os, "fchmod", raising=False)
    io = ArtifactIO(tmp_path)
    try:
        io.write(tmp_path / "winner.patch", b"patch")
    finally:
        io.close()
    assert (tmp_path / "winner.patch").read_bytes() == b"patch"
    assert stat.S_IMODE((tmp_path / "winner.patch").stat().st_mode) == 0o600
