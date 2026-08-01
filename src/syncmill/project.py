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

import tomlkit

from syncmill import teamfile
from syncmill.config import SyncmillConfig
from syncmill.git_env import isolated_git_env


class ProjectError(RuntimeError):
    """The requested path cannot be used as a syncmill project."""


class ProjectDiscoveryError(ProjectError):
    """The requested path is not a Git repository at all.

    A SUBCLASS, so every existing ``except ProjectError`` keeps catching it. The
    distinction exists for the CLI's error wording: failing to find a repository
    at the given path is not a *configuration* problem, and reporting it as one
    ("invalid target project configuration: not a Git repository") sends the
    reader looking for a bad `syncmill.toml` that may not even exist. Everything
    raised while READING a project that WAS found stays a plain ``ProjectError``.
    """


def git_root(path: Path) -> Path:
    """Resolve ``path``'s Git top-level, or raise ``ProjectError``.

    ``ProjectError`` is the ONLY failure: a missing or non-directory ``cwd`` and an
    absent ``git`` executable both raise ``OSError`` from ``subprocess``, and letting
    that through would mean every caller had to catch two unrelated error types to
    ask one question — the CLI's target loader did not, and reported a nonexistent
    ``--repo`` path as a configuration problem.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path), env=isolated_git_env(), capture_output=True, text=True,
        )
    except OSError as exc:
        raise ProjectError(f"cannot inspect as a Git repository: {path} ({exc})") from exc
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
    env = isolated_git_env()
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


def load_project_config(
    path: Path, *, repo_root_override: str | None = None
) -> tuple[Path, SyncmillConfig]:
    """Load env > target-project TOML > defaults, independent of caller cwd.

    ``repo_root_override`` carries ``run``'s explicit CLI ``--repo`` value, where
    the flag names the repository to WORK ON and is documented to beat the
    environment, so it must replace an ambient ``SYNCMILL_REPO_ROOT`` BEFORE
    ``repo_root`` is resolved/validated here — otherwise a stale exported path
    fails the load the flag should have won.

    Callers that pass NO override mean something different by ``--repo``: which
    PROJECT's configuration to load. Their ``repo_root`` then resolves normally
    (env > that project's file > default), which is what lets a project's config
    point at a sibling checkout — the relative-collaboration layout, where owner
    A's project directs work at repository B. Pinning ``repo_root`` to the flag
    for those callers would silently retarget them at themselves.
    """
    try:
        root = git_root(path.resolve())
    except ProjectError as exc:
        # Discovery, not configuration — see ProjectDiscoveryError. The later
        # git_root call resolves the CONFIGURED repo_root and stays a plain
        # ProjectError, because a config value naming a non-repository is a
        # configuration error.
        raise ProjectDiscoveryError(str(exc)) from exc
    with _cwd(root):
        config = SyncmillConfig()
    if repo_root_override is not None:
        config.repo_root = repo_root_override
    configured_root = Path(config.repo_root).expanduser()
    if not configured_root.is_absolute():
        configured_root = root / configured_root
    config.repo_root = str(git_root(configured_root.resolve()))
    resolve_project_state_paths(config, root)
    return root, config


_NEW_PROJECT_CONFIG = (
    "config_version = 2\n"
    "repo_root = \".\"\n"
    "default_strategy = \"route\"\n\n"
    "[project]\n"
    "id = \"{pid}\"\n"
    "memory_dir = \".syncmill/memory\"\n\n"
    "[gate]\n"
    "commands = []\n"
)


_SUPPORTED_CONFIG_VERSION = 2


def _write_fresh_config(config_path: Path, pid: str) -> None:
    """Write the starter config through the one module that owns toml writing.

    Parsed and re-dumped rather than ``write_text``-ed so this branch gets the same
    atomic temp+replace as every other config write — an interrupted `init` must not
    leave a half-written active config."""
    doc = tomlkit.parse(_NEW_PROJECT_CONFIG.format(pid=pid))
    try:
        teamfile.write_document(config_path, doc)
    except teamfile.TeamFileError as exc:
        raise ProjectError(str(exc)) from exc


def _backup_path(config_path: Path) -> Path:
    """A `.bak` name that does not already exist.

    A second repair must not overwrite the first one's backup — that copy is the
    user's only remaining record of the config being replaced."""
    candidate = config_path.with_suffix(config_path.suffix + ".bak")
    counter = 1
    while candidate.exists():
        candidate = config_path.with_suffix(f"{config_path.suffix}.bak.{counter}")
        counter += 1
    return candidate


def _project_table(doc, key: str, config_path: Path):
    """Return ``doc[key]`` as a table, creating it only when the key is ABSENT.

    A key holding something else (a scalar, an array of tables) is REFUSED, never
    replaced: overwriting it would silently destroy whatever the user had there,
    which is the failure this whole change exists to stop. ``dict`` covers tomlkit's
    ``Table`` and ``InlineTable`` and excludes an array-of-tables."""
    value = doc.get(key)
    if value is None:
        table = tomlkit.table()
        doc[key] = table
        return table
    if isinstance(value, dict):
        return value
    raise ProjectError(
        f"{config_path} has a '{key}' key that is not a table — fix or remove it "
        f"before re-initializing"
    )


def _write_project_config(config_path: Path, pid: str) -> None:
    """Write the project config, MERGING into an existing file.

    ``init --force`` used to write the template over whatever was there, keeping
    only ``project.id`` — so a re-init silently discarded the user's gate commands,
    per-agent settings, strategy, model routing, and team profiles. Forcing is a
    repair verb, not a reset one: it fills in only the keys the project needs and
    leaves every other key (and every comment) alone.

    Only a PARSE failure moves the file aside (to a non-colliding ``.bak``) and
    starts fresh. A file we merely could not READ is an error, not a licence to
    replace it — the content may be perfectly good.
    """
    if not config_path.exists():
        _write_fresh_config(config_path, pid)
        return
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"cannot read {config_path}: {exc}") from exc
    try:
        doc = tomlkit.parse(text)
    except Exception:  # tomlkit raises its own ParseError hierarchy
        backup = _backup_path(config_path)
        try:
            os.replace(config_path, backup)
        except OSError as exc:
            raise ProjectError(
                f"{config_path} is unparseable and could not be moved aside: {exc}"
            ) from exc
        _write_fresh_config(config_path, pid)
        return
    version = doc.get("config_version")
    if isinstance(version, int) and version > _SUPPORTED_CONFIG_VERSION:
        # Stamping this file as version 2 would relabel a schema we do not
        # understand — and the relabelling is the kind of quiet damage this change
        # is about. Refuse instead.
        raise ProjectError(
            f"{config_path} declares config_version {version}, newer than this "
            f"syncmill writes ({_SUPPORTED_CONFIG_VERSION}) — upgrade syncmill, or "
            f"move the file aside to re-initialize"
        )
    doc.setdefault("config_version", _SUPPORTED_CONFIG_VERSION)
    doc.setdefault("repo_root", ".")
    doc.setdefault("default_strategy", "route")
    project = _project_table(doc, "project", config_path)
    project["id"] = pid
    project.setdefault("memory_dir", ".syncmill/memory")
    gate = _project_table(doc, "gate", config_path)
    gate.setdefault("commands", [])
    try:
        teamfile.write_document(config_path, doc)
    except teamfile.TeamFileError as exc:
        raise ProjectError(str(exc)) from exc


def initialize_project(path: Path, *, force: bool = False) -> tuple[Path, str]:
    root = git_root(path.resolve())
    config_path = root / "syncmill.toml"
    marker = root / ".syncmill" / "project.json"
    if config_path.exists() and not force:
        raise ProjectError(f"already initialized: {config_path}")
    pid: str | None = None
    try:
        existing = tomllib.loads(config_path.read_text(encoding="utf-8"))
        project_table = existing.get("project")
        # A `project` key holding something other than a table has no `.get` —
        # an AttributeError, which is neither a ValueError nor a TypeError, so it
        # escaped this guard and crashed `init` on a malformed config.
        candidate = project_table.get("id") if isinstance(project_table, dict) else None
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
    _write_project_config(config_path, pid)
    exclude_probe = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=str(root), env=isolated_git_env(), capture_output=True, text=True,
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
