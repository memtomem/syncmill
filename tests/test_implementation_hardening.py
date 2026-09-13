"""Regression coverage for implementation review R1–R6."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tracemalloc

import pytest
from click.testing import CliRunner

from syncmill import agents as A, worktree as W
from syncmill.artifacts import ArtifactError, ArtifactStore
from syncmill.cli import cli
from syncmill.config import AgentConfig, SyncmillConfig
from syncmill.gates import CommandGate
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.platform_support import UnsupportedPlatformError


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, capture_output=True, check=True).stdout


@pytest.mark.parametrize('prefix', ['session-', 'room-'])
def test_durable_pool_never_claimed_by_run_sweep(git_repo, tmp_path, prefix):
    snapshot = W.capture_repository_snapshot(git_repo, prefix + 'durable', tmp_path / 'ws')
    pool = W.WorktreePool(git_repo, 'durable', snapshot=snapshot)
    tree = pool.acquire('codex')
    try:
        assert not (snapshot.run_root / W._RUN_SENTINEL).exists()
        # Legacy versions incorrectly claimed these workspaces. Even both
        # ownership markers must not expose them through generic discard.
        (snapshot.run_root / W._RUN_SENTINEL).write_text('0')
        assert W.sweep_orphaned_workspaces(tmp_path / 'ws', older_than=0) == 0
        (snapshot.run_root / W._RECOVERY_MARKER).touch()
        assert W.list_retained_workspaces(tmp_path / 'ws') == []
        with pytest.raises(W.WorkspaceError, match='session/room'):
            W.discard_retained_workspace(tmp_path / 'ws', snapshot.run_root.name)
        with pytest.raises(W.WorkspaceError, match='session/room'):
            W.discard_retained_workspace(tmp_path / 'ws', './' + snapshot.run_root.name)
        assert tree.path.exists()
    finally:
        pool.cleanup_all()


def test_private_patch_index_roundtrips_and_preserves_real_index(git_repo, tmp_path):
    (git_repo / 'remove.txt').write_text('remove me\n')
    git(git_repo, 'add', 'remove.txt')
    git(git_repo, 'commit', '-qm', 'removal base')
    (git_repo / 'remove.txt').unlink()
    (git_repo / 'binary').write_bytes(b'\0\xff\xfe\n')
    (git_repo / 'line\nbreak.txt').write_bytes(b'\xffnot utf8\n')
    before = (git_repo / '.git/index').read_bytes()
    (git_repo / '.git/index.lock').touch()
    captured = W.capture_changes(git_repo)
    assert (git_repo / '.git/index').read_bytes() == before
    assert set(captured.files) == {'remove.txt', 'binary', 'line\nbreak.txt'}
    target = tmp_path / 'target'
    subprocess.run(['git', 'clone', '-q', str(git_repo), str(target)], check=True)
    subprocess.run(['git', 'apply', '--binary', '-'], cwd=target,
                   input=captured.patch.encode('utf-8', 'surrogateescape'), check=True)
    assert (target / 'binary').read_bytes() == b'\0\xff\xfe\n'
    assert (target / 'line\nbreak.txt').read_bytes() == b'\xffnot utf8\n'
    assert not (target / 'remove.txt').exists()


@pytest.mark.parametrize('command', ['read-tree', 'add', 'diff'])
def test_patch_command_failure_is_never_empty_patch(git_repo, monkeypatch, command):
    original = W._run_git

    def failing(argv, **kwargs):
        if argv[1] == command:
            return subprocess.CompletedProcess(argv, 124, b'', b'capture timeout')
        return original(argv, **kwargs)

    monkeypatch.setattr(W, '_run_git', failing)
    with pytest.raises(W.PatchCaptureError, match='capture timeout'):
        W.capture_changes(git_repo)


def test_supervisor_retains_failed_capture_and_does_not_share(git_repo, tmp_path, monkeypatch):
    from conftest import FakeAgent, FakeMemory, FakeRegistry
    from test_supervisor import _supervisor
    from syncmill.strategies import base

    class EditingAgent(FakeAgent):
        def execute(self, prompt, workdir, timeout):
            (workdir / 'winner.txt').write_text('irreplaceable\n')
            return super().execute(prompt, workdir, timeout)

    def fail(*args, **kwargs):
        raise W.PatchCaptureError('injected capture failure')

    monkeypatch.setattr(base, 'capture_changes', fail)
    memory = FakeMemory()
    config = SyncmillConfig(repo_root=str(git_repo), agent_priority=['codex'],
                            workspace={'root': str(tmp_path / 'ws')},
                            runs={'root': str(tmp_path / 'runs')})
    sup = _supervisor(FakeRegistry([EditingAgent('codex')]), memory, config=config)
    result = sup.run(Task(prompt='edit'))
    assert result.status == TaskStatus.FAILED
    assert 'recovery workspace:' in result.error
    assert list(sup.recovery_workspace.glob('worktrees/*/winner.txt'))
    manifest = json.loads(next((tmp_path / 'runs').glob('*/runs/*/manifest.json')).read_text())
    assert manifest['state'] == 'recovery_required'
    assert memory.shared == []


def test_missing_patch_rejected_but_explicit_noop_saved(git_repo, tmp_path):
    store = ArtifactStore(tmp_path / 'runs', 'test')
    task = Task(prompt='noop')
    result = TaskResult(task_id=task.id, agent_id='codex', status=TaskStatus.COMPLETED,
                        output='', exit_code=0, duration_seconds=0)
    kwargs = dict(task=task, result=result, repo_root=git_repo, snapshot_commit='base',
                  source_fingerprint='fingerprint', strategy='route')
    with pytest.raises(ArtifactError, match='mandatory patch'):
        store.save(run_id='missing', **kwargs)
    assert not (store.root / 'missing').exists()
    result.patch = W.capture_changes(git_repo).patch
    assert result.patch == ''
    assert store.save(run_id='noop', **kwargs).manifest['state'] == 'no_changes'


@pytest.mark.parametrize('state', ['staged', 'committed', 'deleted'])
def test_gitlinks_rejected_before_workspace_creation(git_repo, tmp_path, state):
    head = git(git_repo, 'rev-parse', 'HEAD').decode().strip()
    git(git_repo, 'update-index', '--add', '--cacheinfo', f'160000,{head},sub')
    if state != 'staged':
        git(git_repo, 'commit', '-qm', 'gitlink')
    if state == 'deleted':
        git(git_repo, 'update-index', '--force-remove', 'sub')
    with pytest.raises(W.WorkspaceError, match='submodule repositories are not supported'):
        W.capture_repository_snapshot(git_repo, 'bad', tmp_path / 'ws')
    assert not (tmp_path / 'ws').exists()


def test_gitmodules_without_gitlinks_is_allowed(git_repo, tmp_path):
    (git_repo / '.gitmodules').write_text('# not a submodule\n')
    snapshot = W.capture_repository_snapshot(git_repo, 'ok', tmp_path / 'ws')
    W.WorktreePool(git_repo, 'ok', snapshot=snapshot).cleanup_all()


def test_windows_refused_before_side_effects(monkeypatch, tmp_path):
    import syncmill.platform_support as platforms
    monkeypatch.setattr(platforms.sys, 'platform', 'win32')
    monkeypatch.setattr(os, 'kill', lambda *a: pytest.fail('Windows PID signal'))
    for args in (['--help'], ['--version']):
        assert CliRunner().invoke(cli, args).exit_code == 0
    result = CliRunner().invoke(cli, ['agents'])
    assert result.exit_code == 1
    assert 'macOS and Linux only' in result.output
    with pytest.raises(UnsupportedPlatformError):
        ArtifactStore(tmp_path / 'never', 'test')
    with pytest.raises(UnsupportedPlatformError):
        W.sweep_orphaned_workspaces(tmp_path)
    with pytest.raises(UnsupportedPlatformError):
        W._process_is_alive(123)
    with pytest.raises(UnsupportedPlatformError):
        A._sync_run(['never-launch'], timeout=1)
    assert not (tmp_path / 'never').exists()


@pytest.mark.parametrize('mode', ['sync', 'async', 'tee'])
@pytest.mark.parametrize('extra', [0, 1])
def test_provider_combined_output_budget(monkeypatch, tmp_path, mode, extra):
    # Exact byte boundary, with independently buffered stdout and stderr.
    agent = A.CodexAgent(AgentConfig(output_max_bytes=4096))
    code = f'import os; os.write(1,b"x"*2048); os.write(2,b"y"*{2048 + extra})'
    monkeypatch.setattr(agent, '_build_command', lambda _: [sys.executable, '-c', code])
    chunks = []

    class Sink:
        def feed(self, agent_id, chunk):
            chunks.append(chunk)

    if mode == 'sync':
        result = agent.execute('go', tmp_path, 5)
    elif mode == 'async':
        result = asyncio.run(agent.aexecute('go', tmp_path, 5))
    else:
        with A.stream_to(Sink()):
            result = asyncio.run(agent.aexecute('go', tmp_path, 5))
    if extra:
        assert result.status == TaskStatus.FAILED
        assert 'output limit exceeded' in result.error
        assert result.provider_session_id is None
        assert result.output == ''
        if mode == 'tee':
            assert b'output limit exceeded' in b''.join(chunks)
    else:
        assert result.status == TaskStatus.COMPLETED


def test_gate_output_memory_is_bounded(tmp_path):
    code = 'import os; [os.write(1,b"x"*4096) for _ in range(4096)]; print("TAIL")'
    import shlex
    command = shlex.join([sys.executable, '-c', code])
    tracemalloc.start()
    try:
        report = CommandGate([command], 15).evaluate(W.Worktree(tmp_path, None, 'gate', False))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report.passed
    assert report.checks[0].output.endswith('TAIL')
    assert 3900 <= len(report.checks[0].output) <= 4000
    assert peak < 4 * 1024 * 1024


def test_output_budget_config_env_and_validation(monkeypatch):
    monkeypatch.setenv('SYNCMILL_CODEX__OUTPUT_MAX_BYTES', '12345')
    assert SyncmillConfig().codex.output_max_bytes == 12345
    assert AgentConfig().output_max_bytes == 32 * 1024 * 1024
    with pytest.raises(ValueError):
        AgentConfig(output_max_bytes=0)


def test_apply_verification_uses_gate_isolation(git_repo, tmp_path, monkeypatch):
    import shlex
    from test_v2_workflow import _pending_artifact

    store, _ = _pending_artifact(tmp_path, git_repo)
    before = W.repository_fingerprint(git_repo)
    for key in ('PWD', 'OLDPWD', 'CDPATH', 'GIT_DIR', 'GIT_WORK_TREE',
                'SYNCMILL_MONITOR_DIR', 'SYNCMILL_RUN_ID'):
        monkeypatch.setenv(key, 'poison')
    code = (
        'import os; from pathlib import Path; '
        'assert Path(os.environ["PWD"]).resolve() == Path.cwd(); '
        'assert not any(k in os.environ for k in '
        '("OLDPWD","CDPATH","GIT_DIR","GIT_WORK_TREE","SYNCMILL_MONITOR_DIR","SYNCMILL_RUN_ID")); '
        'assert Path("winner.txt").exists(); os.write(1,b"\\xffvalid\\n")'
    )
    ArtifactStore._verify_in_isolation(
        run_id='run-1', repo_root=git_repo, patch=(store.load('run-1').path / 'winner.patch').read_bytes(),
        commands=[shlex.join([sys.executable, '-c', code])], timeout=5,
        workspace_root=tmp_path / 'verify',
    )
    assert W.repository_fingerprint(git_repo) == before
    assert not (git_repo / 'winner.txt').exists()


@pytest.mark.parametrize('command', ['"unterminated', '', 'nonexistent-syncmill-check',
                                    'python -c "import sys; sys.exit(1)"'])
def test_apply_bad_verification_keeps_source_and_pending_artifact(git_repo, tmp_path, command):
    from test_v2_workflow import _pending_artifact
    store, _ = _pending_artifact(tmp_path, git_repo)
    before = W.repository_fingerprint(git_repo)
    with pytest.raises(ArtifactError, match='isolated verification failed'):
        store.apply('run-1', git_repo, before, verify_commands=[command], verify_timeout=5)
    assert W.repository_fingerprint(git_repo) == before
    assert store.load('run-1').manifest['state'] == 'pending'


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_infinite_provider_output_kills_owned_process_group(tmp_path, monkeypatch, mode):
    import time
    import signal
    pidfile = tmp_path / 'child.pid'
    code = (
        'import subprocess, sys, os; from pathlib import Path; '
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
        f'Path({str(pidfile)!r}).write_text(str(child.pid)); '
        '\nwhile True: os.write(1,b"x"*4096)'
    )
    agent = A.CodexAgent(AgentConfig(output_max_bytes=8192))
    monkeypatch.setattr(agent, '_build_command', lambda _: [sys.executable, '-c', code])
    start = time.monotonic()
    result = agent.execute('go', tmp_path, 10) if mode == 'sync' else asyncio.run(
        agent.aexecute('go', tmp_path, 10))
    assert time.monotonic() - start < 8
    assert result.status == TaskStatus.FAILED
    assert 'output limit exceeded' in result.error
    pid = int(pidfile.read_text())
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            if status.returncode != 0 or status.stdout.strip().startswith('Z'):
                break
            time.sleep(0.02)
        else:
            pytest.fail('provider descendant survived output overflow')
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
