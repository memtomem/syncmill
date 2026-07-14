"""Mandatory, durable run artifacts and explicit patch application."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from syncmill.models import Task, TaskResult, TaskStatus


class ArtifactError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class RunArtifact:
    run_id: str
    path: Path
    manifest: dict[str, Any]


class ArtifactStore:
    def __init__(self, root: Path, project_id: str):
        self.root = root.expanduser().resolve() / project_id / "runs"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def save(
        self,
        *,
        run_id: str,
        task: Task,
        result: TaskResult,
        repo_root: Path,
        snapshot_commit: str,
        source_fingerprint: str,
        strategy: str,
        parent_run_id: str | None = None,
        plan: dict[str, Any] | None = None,
    ) -> RunArtifact:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing: dict[str, Any] = {}
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ArtifactError(f"corrupt running artifact: {run_id}") from exc
            if existing.get("state") != "running":
                raise ArtifactError(f"run artifact already exists: {run_id}")
        patch = (result.patch or "").encode("utf-8", errors="surrogateescape")
        state = "pending" if result.status == TaskStatus.COMPLETED and patch else (
            "no_changes" if result.status == TaskStatus.COMPLETED else "failed"
        )
        manifest = {
            **existing,
            "schema_version": 1,
            "run_id": run_id,
            "project_id": self.root.parent.name,
            "parent_run_id": parent_run_id,
            "created_at": existing.get("created_at", _now()),
            "updated_at": _now(),
            "state": state,
            "repo_root": str(repo_root.resolve()),
            "source_fingerprint": source_fingerprint,
            "snapshot_commit": snapshot_commit,
            "task_id": str(task.id),
            "prompt": task.prompt,
            "strategy": strategy,
            "agent_id": result.agent_id,
            "files_changed": sorted(result.files_changed),
            "patch_sha256": _digest(patch),
            "patch_bytes": len(patch),
            "provider_session_id": result.provider_session_id,
            "conversation_id": result.conversation_id,
            "plan": plan,
        }
        _atomic_write(run_dir / "winner.patch", patch)
        _atomic_write(
            run_dir / "result.json",
            (json.dumps({
                "status": result.status.value,
                "output": result.output,
                "error": result.error,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            }, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        self._write_manifest(run_dir, manifest)
        return RunArtifact(run_id, run_dir, manifest)

    def begin(
        self,
        *,
        run_id: str,
        task: Task,
        repo_root: Path,
        snapshot_commit: str,
        source_fingerprint: str,
        strategy: str,
        agents: list[str],
        plan: dict[str, Any],
        parent_run_id: str | None = None,
    ) -> RunArtifact:
        """Create the crash-visible running manifest before any agent starts."""
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "project_id": self.root.parent.name,
            "parent_run_id": parent_run_id,
            "created_at": _now(),
            "updated_at": _now(),
            "state": "running",
            "repo_root": str(repo_root.resolve()),
            "source_fingerprint": source_fingerprint,
            "snapshot_commit": snapshot_commit,
            "task_id": str(task.id),
            "prompt": task.prompt,
            "strategy": strategy,
            "agents": agents,
            "plan": plan,
        }
        self._write_manifest(run_dir, manifest)
        return RunArtifact(run_id, run_dir, manifest)

    def load(self, run_id: str) -> RunArtifact:
        path = self.root / run_id
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ArtifactError(f"unknown or corrupt run: {run_id}") from exc
        return RunArtifact(run_id, path, manifest)

    def list(self) -> list[RunArtifact]:
        if not self.root.exists():
            return []
        artifacts: list[RunArtifact] = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if path.is_dir():
                try:
                    artifacts.append(self.load(path.name))
                except ArtifactError:
                    continue
        return artifacts

    def prune(self, keep: int, *, dry_run: bool = False) -> list[str]:
        """Remove old terminal runs; pending/recovery state is never pruned."""
        removable_states = {
            "applied", "no_changes", "failed", "discarded", "superseded"
        }
        artifacts = self.list()
        protected = set(artifact.run_id for artifact in artifacts[:keep])
        removed: list[str] = []
        for artifact in artifacts:
            if artifact.run_id in protected:
                continue
            if artifact.manifest.get("state") not in removable_states:
                continue
            removed.append(artifact.run_id)
            if not dry_run:
                shutil.rmtree(artifact.path)
        return removed

    def patch(self, run_id: str) -> bytes:
        artifact = self.load(run_id)
        data = (artifact.path / "winner.patch").read_bytes()
        if _digest(data) != artifact.manifest.get("patch_sha256"):
            raise ArtifactError(f"patch digest mismatch for run {run_id}")
        return data

    def apply(
        self,
        run_id: str,
        repo_root: Path,
        source_fingerprint: str,
        *,
        verify_commands: list[str] | None = None,
        verify_timeout: int = 300,
    ) -> Path:
        artifact = self.load(run_id)
        manifest = artifact.manifest
        if manifest.get("state") != "pending":
            raise ArtifactError(
                f"run {run_id} is {manifest.get('state')!r}, not pending"
            )
        if Path(manifest["repo_root"]).resolve() != repo_root.resolve():
            raise ArtifactError("run belongs to a different repository")
        if manifest.get("source_fingerprint") != source_fingerprint:
            raise ArtifactError(
                "working tree changed since the run; review the diff and rerun"
            )
        patch_path = artifact.path / "winner.patch"
        for args in (["git", "apply", "--check", "--binary", str(patch_path)],
                     ["git", "apply", "--binary", str(patch_path)]):
            proc = subprocess.run(args, cwd=str(repo_root), capture_output=True, text=True)
            if proc.returncode != 0:
                raise ArtifactError(proc.stderr.strip() or "git apply failed")
        for command in verify_commands or []:
            try:
                check = subprocess.run(
                    shlex.split(command), cwd=str(repo_root), capture_output=True,
                    text=True, timeout=verify_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                check = None
                failure = str(exc)
            else:
                failure = check.stderr.strip() or check.stdout.strip()
            if check is None or check.returncode != 0:
                rollback = subprocess.run(
                    ["git", "apply", "-R", "--binary", str(patch_path)],
                    cwd=str(repo_root), capture_output=True, text=True,
                )
                suffix = ""
                if rollback.returncode != 0:
                    suffix = f"; rollback failed: {rollback.stderr.strip()}"
                raise ArtifactError(
                    f"post-apply verification failed ({command}): {failure}{suffix}"
                )
        receipt_dir = artifact.path / "applications"
        receipt_dir.mkdir(mode=0o700, exist_ok=True)
        receipt = receipt_dir / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        _atomic_write(receipt, (json.dumps({
            "applied_at": _now(), "repo_root": str(repo_root.resolve())
        }, indent=2) + "\n").encode())
        manifest["state"] = "applied"
        manifest["updated_at"] = _now()
        self._write_manifest(artifact.path, manifest)
        return receipt

    def discard(self, run_id: str) -> None:
        artifact = self.load(run_id)
        if artifact.manifest.get("state") != "pending":
            raise ArtifactError("only a pending run can be discarded")
        artifact.manifest["state"] = "discarded"
        artifact.manifest["updated_at"] = _now()
        self._write_manifest(artifact.path, artifact.manifest)

    def supersede(self, run_id: str, child_run_id: str) -> None:
        artifact = self.load(run_id)
        if artifact.manifest.get("state") != "pending":
            return
        artifact.manifest["state"] = "superseded"
        artifact.manifest["superseded_by"] = child_run_id
        artifact.manifest["updated_at"] = _now()
        self._write_manifest(artifact.path, artifact.manifest)

    def close_conversation(self, run_id: str) -> None:
        artifact = self.load(run_id)
        artifact.manifest["conversation_closed"] = True
        artifact.manifest["updated_at"] = _now()
        self._write_manifest(artifact.path, artifact.manifest)

    def recovery_required(self, run_id: str, reason: str) -> None:
        artifact = self.load(run_id)
        artifact.manifest["state"] = "recovery_required"
        artifact.manifest["recovery_reason"] = reason[:1000]
        artifact.manifest["updated_at"] = _now()
        self._write_manifest(artifact.path, artifact.manifest)

    @staticmethod
    def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
        _atomic_write(
            run_dir / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        )
