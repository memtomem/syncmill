"""Project discovery, identity, and first-run initialization."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from syncmill.config import SyncmillConfig


class ProjectError(RuntimeError):
    """The requested path cannot be used as a syncmill project."""


def git_root(path: Path) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(path), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ProjectError(f"not a Git repository: {path}")
    return Path(proc.stdout.strip()).resolve()


def project_id(root: Path, configured: str | None = None) -> str:
    if configured:
        return configured
    marker = root / ".syncmill" / "project.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        value = data.get("id")
        if isinstance(value, str) and value:
            return value
    except (OSError, ValueError, TypeError):
        pass
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]


def resolve_project_state_paths(config: SyncmillConfig, root: Path) -> SyncmillConfig:
    """Bind relative durable-state paths to the project that owns the config.

    ``ArtifactStore`` and the workspace helpers eventually call ``resolve()``.
    Leaving a configured relative path untouched until then would therefore make
    its meaning depend on the caller's current working directory.  Resolve both
    correctness-state roots at the project boundary so regular runs, Rooms, and
    isolated sessions all address the same storage.
    """
    base = root.expanduser().resolve()
    state_paths: list[tuple[str, Path]] = []
    for label, section in (("runs.root", config.runs), ("workspace.root", config.workspace)):
        value = Path(section.root).expanduser()
        if not value.is_absolute():
            value = base / value
        resolved = value.resolve()
        section.root = str(resolved)
        state_paths.append((label, resolved))
    repositories = {base}
    configured_repo = Path(config.repo_root).expanduser()
    if not configured_repo.is_absolute():
        configured_repo = base / configured_repo
    repositories.add(configured_repo.resolve())
    for label, state_path in state_paths:
        for repository in repositories:
            _require_ignored_state_root(repository, state_path, label)
    return config


def _require_ignored_state_root(
    repository: Path, state_root: Path, label: str
) -> None:
    """Reject correctness state that would become repository source drift."""
    try:
        relative = state_root.relative_to(repository)
    except ValueError:
        return
    if not relative.parts or relative.parts[0] == ".git":
        raise ProjectError(f"{label} must be outside the repository or Git-ignored")
    env = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    relative_text = relative.as_posix().rstrip("/")
    probes = (relative_text, f"{relative_text}/", f"{relative_text}/.syncmill-probe")
    for probe in probes:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "--", probe],
            cwd=str(repository),
            env=env,
            capture_output=True,
        )
        if ignored.returncode == 0:
            return
        if ignored.returncode not in {1}:
            raise ProjectError(f"could not verify Git ignore status for {label}")
    raise ProjectError(f"{label} inside the repository must be Git-ignored")


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def load_project_config(path: Path) -> tuple[Path, SyncmillConfig]:
    """Load env > target-project TOML > defaults, independent of caller cwd."""
    root = git_root(path.resolve())
    with _cwd(root):
        config = SyncmillConfig()
    configured_root = Path(config.repo_root).expanduser()
    if not configured_root.is_absolute():
        configured_root = root / configured_root
    config.repo_root = str(git_root(configured_root.resolve()))
    resolve_project_state_paths(config, root)
    return root, config


def initialize_project(path: Path, *, force: bool = False) -> tuple[Path, str]:
    root = git_root(path.resolve())
    config_path = root / "syncmill.toml"
    marker = root / ".syncmill" / "project.json"
    if config_path.exists() and not force:
        raise ProjectError(f"already initialized: {config_path}")
    pid: str | None = None
    try:
        existing = tomllib.loads(config_path.read_text(encoding="utf-8"))
        candidate = existing.get("project", {}).get("id")
        if isinstance(candidate, str) and candidate:
            pid = candidate
    except (OSError, ValueError, TypeError):
        pass
    if pid is None:
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
            candidate = marker_data.get("id")
            if isinstance(candidate, str) and candidate:
                pid = candidate
        except (OSError, ValueError, TypeError):
            pass
    pid = pid or str(uuid4())
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"id": pid}, indent=2) + "\n", encoding="utf-8")
    os.chmod(marker, 0o600)
    local_ignore = marker.parent / ".gitignore"
    try:
        ignore_text = local_ignore.read_text(encoding="utf-8")
    except FileNotFoundError:
        ignore_text = ""
    if "memory/" not in ignore_text.splitlines():
        with local_ignore.open("a", encoding="utf-8") as handle:
            if ignore_text and not ignore_text.endswith("\n"):
                handle.write("\n")
            handle.write("memory/\n")
    config_path.write_text(
        "config_version = 2\n"
        "repo_root = \".\"\n"
        "default_strategy = \"route\"\n\n"
        "[project]\n"
        f"id = \"{pid}\"\n"
        "memory_dir = \".syncmill/memory\"\n\n"
        "[gate]\n"
        "commands = []\n",
        encoding="utf-8",
    )
    exclude_probe = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=str(root), capture_output=True, text=True,
    )
    if exclude_probe.returncode == 0:
        exclude_path = Path(exclude_probe.stdout.strip())
        if not exclude_path.is_absolute():
            exclude_path = root / exclude_path
        try:
            existing = exclude_path.read_text(encoding="utf-8")
            rule = ".syncmill/memory/"
            if rule not in existing.splitlines():
                exclude_path.parent.mkdir(parents=True, exist_ok=True)
                with exclude_path.open("a", encoding="utf-8") as handle:
                    if existing and not existing.endswith("\n"):
                        handle.write("\n")
                    handle.write(f"{rule}\n")
        except OSError as exc:
            raise ProjectError(f"could not protect project memory in Git: {exc}") from exc
    return config_path, pid
