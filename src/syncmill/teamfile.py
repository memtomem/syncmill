"""Team-profile persistence — the tomlkit ``syncmill.toml`` write path (P3 TEAM pane).

The first (and only) toml-*writing* module: `team save` / `team use` and the
dashboard TEAM pane all funnel through these helpers, while reads stay on
pydantic-settings' ``TomlConfigSettingsSource``. A flat top-level module
(gateway/monitor/session/board/dashboard precedent): two consumers (cli +
dashboard) share one genuinely new concern, and putting the writers in
``config.py`` would mix declarative reads with imperative writes and force a
``config → agents`` import cycle (the unknown-agent check needs
``_AGENT_CONSTRUCTORS``). Import graph stays acyclic:
``teamfile → config, agents``; ``cli → teamfile``; ``dashboard → teamfile``.

Design rules this module owns:

- **Comment-preserving round-trip.** Every write parses the existing document
  with tomlkit, mutates the smallest span, and dumps — comments/whitespace
  everywhere else are preserved byte-for-byte. Replacing ``[teams.<name>]`` is
  deliberately *wholesale* (flags define the full profile; a partial merge
  could not express "clear goals"), so only that table's own comments are lost.
- **Validate against the RESOLVED config, then write toml only.** A save must
  never produce a file that makes the next ``SyncmillConfig()`` construction
  die (which would brick every command): ``validate_team_save`` runs the shared
  ``check_team_goals`` over ``{**config.teams, name: team}`` — the env+toml
  merged view with the candidate substituted. For every other name the merged
  view is unchanged by the write; for the saved name either env doesn't define
  it (merged == the validated candidate) or env wins the deep-merge (already
  valid, since the resolved config loaded before the save).
- **Atomic writes** — temp file + ``os.replace`` (the breaker/board recipe).
- **Serialized read-modify-write.** Atomicity alone only guarantees no reader
  ever sees a half-written file; it does NOT stop two writers from each parsing
  the same document and the second replace discarding the first's table (a lost
  update — `team save` racing the dashboard/TUI TEAM pane, or `init --force`).
  Every write therefore runs inside :func:`config_write_lock`, a BLOCKING
  cross-process lock kept inside Git's common directory (see
  :func:`_lock_path_for` — never in the working tree, which a snapshot would
  copy into every agent worktree and a user could commit). Blocking, not
  fail-fast: the critical section is a sub-millisecond parse-mutate-dump, so a
  waiter serializes invisibly, where a busy error would turn a benign micro-race
  into a user-visible failure. The lock is re-entrant per thread, so a caller
  that spans validate→save (closing the stale-validation window) costs the
  inner acquisitions nothing.
- ``TeamFileError`` is the single error type at the seam (``BoardError``
  precedent): the CLI converts to ``BadParameter``/``ClickException``, the
  dashboard mirrors it into ``last_team_error`` — this module never imports
  click.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

import tomlkit
from pydantic import ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    TomlConfigSettingsSource,
)
from tomlkit.items import Table

from syncmill.agents import _AGENT_CONSTRUCTORS
from syncmill.artifacts import ArtifactError, _cross_process_lock
from syncmill.config import SyncmillConfig, TeamConfig, check_team_goals
from syncmill.git_env import isolated_git_env


class TeamFileError(Exception):
    """A team-profile write/validation failure (parse error, invalid profile,
    unwritable file, unobtainable lock). The single error type at this seam."""


#: Lock file name inside Git's common directory — one per repository, shared by
#: every checkout/worktree of it, so two processes editing the same config
#: contend on the same lock however they reached it.
_REPO_LOCK_NAME = ".syncmill-config.lock"


def _repository_lock_path(directory: Path) -> Path | None:
    """Git's common directory for ``directory``, or ``None`` when there is none.

    The ``ArtifactStore._repository_apply_lock_path`` recipe: ask Git rather
    than looking for a ``.git`` entry, so a worktree/submodule resolves to the
    one canonical directory instead of its private per-worktree one.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(directory),
        env=isolated_git_env(),
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    common = Path(probe.stdout.strip())
    if not common.is_absolute():
        common = directory / common
    return common if common.is_dir() else None


def _lock_path_for(path: Path) -> Path:
    """Where ``path``'s write lock lives.

    Inside Git's common directory when there is one. That directory is never
    tracked, never captured by a repository snapshot, and never shown by
    ``git status``, so the lock cannot pollute a consumer's checkout, be
    committed by accident, or be copied into every agent worktree — and Git
    refuses to check files out into it, so a cloned repository cannot plant a
    symlink there for the ``fchmod`` below to follow.

    Outside a repository there is nowhere better than beside the config, and
    there the lock path is verified not to be a symlink first. That check is not
    airtight on its own — a racing attacker could swap the path afterwards — but
    it defeats the realistic vector (a symlink that arrives WITH the directory),
    and winning a race inside the user's own working directory is a different
    threat model from opening a file that was already there.
    """
    directory = path.parent
    common = None
    try:
        common = _repository_lock_path(directory)
    except OSError:
        common = None  # no git binary: fall through to the sibling lock
    if common is not None:
        return common / _REPO_LOCK_NAME
    sibling = directory / f".{path.name}.lock"
    if sibling.is_symlink():
        raise TeamFileError(
            f"refusing to use {sibling} as a lock file: it is a symlink"
        )
    return sibling


@contextmanager
def config_write_lock(path: Path) -> Iterator[None]:
    """Serialize read-modify-write cycles on ``path`` across processes.

    The lock file (see :func:`_lock_path_for`) is never unlinked: removing it
    would let a later acquirer create a *different* inode and hold a lock nobody
    else contends on (the ``JobStore`` lock-file rule).

    Acquisition BLOCKS — every writer holds it for one parse-mutate-dump, so a
    waiter serializes invisibly, and failing fast would report contention as an
    error the user can do nothing about. Re-entrant per thread (the underlying
    :func:`syncmill.artifacts._cross_process_lock`), so a caller may wrap a
    whole validate→save sequence and the writers' own acquisitions are free.

    Only the ACQUISITION is wrapped into ``TeamFileError`` — the body's own
    exceptions propagate unchanged, so a write failure is never relabelled as a
    locking one (every existing converter, CLI → ``ClickException``, dashboard →
    ``last_team_error``, TUI → notify, ``project.py`` → ``ProjectError``, keeps
    working because the new failure is the same error type).
    """
    stack = ExitStack()
    try:
        stack.enter_context(_cross_process_lock(_lock_path_for(path)))
    except (ArtifactError, OSError) as exc:
        raise TeamFileError(f"cannot lock {path}: {exc}") from exc
    with stack:
        yield


def toml_path() -> Path:
    """The file every helper reads and writes — derived from
    ``SyncmillConfig.model_config["toml_file"]`` so the write path is
    read/write-symmetric with discovery by construction (``syncmill.toml``
    discovered cwd-relative in production; the hermetic suite's autouse
    ``isolate_toml_discovery`` repoints it into ``tmp_path``, so a save test
    can never touch a real repo-root file even if it forgets to repoint).

    ABSOLUTIZED against the cwd at call time: consumers hold the returned path
    across their lifetime (the dashboard/TUI anchor ``_toml_path.parent`` for
    project roots and worker launches), and a relative ``Path("syncmill.toml")``
    would silently re-resolve against whatever the process cwd has drifted to
    by then — the "resolved once at launch" claim those comments make is only
    true because this pins an absolute path. Deliberately NOT ``resolve()``:
    a ``syncmill.toml`` that is a symlink to a shared config elsewhere must
    keep the LINK's directory as the anchor — dereferencing it would point
    ``_toml_path.parent`` (project root, jobs, room workers) at the symlink
    target's directory instead of the checkout holding the link.
    ``Path.cwd()`` is already symlink-free on POSIX (getcwd resolves)."""
    path = Path(
        SyncmillConfig.model_config.get("toml_file") or "syncmill.toml"
    ).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def load_document(path: Path) -> tomlkit.TOMLDocument:
    """Parse ``path`` into a tomlkit document; a missing file is an empty one.

    Raises ``TeamFileError`` on unreadable/unparseable content or an existing
    top-level ``teams`` key that isn't a table (writing under it would corrupt
    the file).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return tomlkit.document()
    except OSError as exc:
        raise TeamFileError(f"cannot read {path}: {exc}")
    try:
        doc = tomlkit.parse(text)
    except Exception as exc:  # tomlkit raises its own ParseError hierarchy
        raise TeamFileError(f"cannot parse {path}: {exc}")
    teams = doc.get("teams")
    if teams is not None and not isinstance(teams, (Table, dict)):
        raise TeamFileError(
            f"{path} has a top-level 'teams' key that is not a table — "
            f"fix the file before saving a team profile"
        )
    return doc


def build_team(
    *,
    agents: Sequence[str],
    strategy: str | None = None,
    goals: Sequence[str] = (),
    capabilities: Mapping[str, Sequence[str]] | None = None,
) -> TeamConfig:
    """Construct a validated ``TeamConfig``, converting the pydantic
    ``ValidationError`` into a one-line ``TeamFileError`` (the CLI
    ``_load_config()`` conversion pattern)."""
    try:
        return TeamConfig(
            agents=list(agents),
            strategy=strategy,
            goals=list(goals),
            capabilities={k: list(v) for k, v in (capabilities or {}).items()},
        )
    except ValidationError as exc:
        raise TeamFileError(f"invalid team profile:\n{exc}")


def validate_team_save(
    config: SyncmillConfig, name: str, team: TeamConfig, *, path: Path | None = None
) -> None:
    """Pre-write validation for ``save_team`` — raises ``TeamFileError``.

    Checks the candidate against TWO views (codex design-review Major — each
    can mask a conflict the other sees):

    - the RESOLVED (env+toml) config with the new team substituted under its
      name — guarantees the next config construction under the current env
      can't die (see the module docstring);
    - when ``path`` is given, the FILE-only teams with the same substitution —
      an env override (env > file, per-field deep-merge) can hide a dup-goal
      that would remain in the file once that env is removed.

    Plus the same unknown-agent-id check ``cli._apply_team`` applies at
    selection time — catching the typo at save time instead of first use.
    Free-form capability *tokens* are never validated (the matcher's
    graceful-fallback contract).
    """
    if not name:
        raise TeamFileError("team name must be a non-empty string")
    unknown = [a for a in team.agents if a not in _AGENT_CONSTRUCTORS]
    if unknown:
        raise TeamFileError(
            f"team {name!r} names unknown agent(s): {', '.join(unknown)}. "
            f"Valid agents: {', '.join(sorted(_AGENT_CONSTRUCTORS))}"
        )
    try:
        check_team_goals({**config.teams, name: team})
    except ValueError as exc:
        raise TeamFileError(str(exc))
    if path is not None:
        _validate_file_standalone(path, name, team)


def _validate_file_standalone(path: Path, name: str, team: TeamConfig) -> None:
    """Reject a save that would leave ``path`` unloadable once env overrides are
    dropped (codex post-code review Major). The resolved-view check above can be
    satisfied by an env override that MASKS a broken file team — so validate the
    file STANDING ALONE: build a real ``SyncmillConfig`` from **only** the toml
    file (env/dotenv sources dropped), which validates every file team's
    structure AND its dup-goals **exactly** as ``_load_config()`` would minus
    env (crucially env-free — a standalone ``TeamConfig(**table)`` would instead
    read bare ``AGENTS``/``GOALS`` env for any field the table omits, silently
    re-introducing the very mask this guards against). Then run the shared
    dup-goal rule over the file teams plus the candidate. Any malformed file
    team surfaces as ``TeamFileError`` — a save can't paper over a file that
    won't load without env.

    (A bad top-level toml key can't reach here: it isn't env-maskable, so the
    caller's own ``_load_config()`` would already have failed the command.)
    """

    class _FileOnly(SyncmillConfig):
        model_config = {**SyncmillConfig.model_config, "toml_file": str(path)}

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            # Deliberately DROP env_settings/dotenv_settings — the file alone.
            return (
                init_settings,
                TomlConfigSettingsSource(settings_cls),
                file_secret_settings,
            )

    try:
        file_cfg = _FileOnly()
    except ValidationError as exc:
        raise TeamFileError(
            f"{path} has a team profile that is invalid standing alone (an "
            f"environment override currently masks it):\n{exc}"
        )
    try:
        check_team_goals({**file_cfg.teams, name: team})
    except ValueError as exc:
        raise TeamFileError(
            f"{exc} (the conflict is inside {path} standing alone — an "
            f"environment override currently masks it)"
        )


def save_team(path: Path, name: str, team: TeamConfig) -> None:
    """Create/replace ``[teams.<name>]`` in ``path``, preserving everything else.

    The named table is replaced WHOLESALE (flags define the full profile —
    partial merge can't express "clear goals"); ``strategy`` is emitted only
    when set, ``goals`` and the ``[teams.<name>.capabilities]`` sub-table only
    when non-empty, so the file mirrors what ``team show`` reports as unset.
    The table name and capability keys are verbatim (tomlkit quotes as needed).
    Atomic (temp + ``os.replace``) and serialized against every other writer by
    :func:`config_write_lock`, so a concurrent save of a DIFFERENT team can no
    longer be discarded by this one's replace. Raises ``TeamFileError``.
    """
    with config_write_lock(path):
        doc = load_document(path)
        teams = doc.get("teams")
        if teams is None:
            # Super-table: renders as bare `[teams.<name>]` headers, no `[teams]`.
            teams = tomlkit.table(is_super_table=True)
            doc["teams"] = teams
        entry = tomlkit.table()
        entry["agents"] = list(team.agents)
        if team.strategy is not None:
            entry["strategy"] = team.strategy
        if team.goals:
            entry["goals"] = list(team.goals)
        if team.capabilities:
            caps = tomlkit.table()
            for agent_id, tokens in team.capabilities.items():
                caps[agent_id] = list(tokens)
            entry["capabilities"] = caps
        teams[name] = entry
        _atomic_write(path, tomlkit.dumps(doc))


def set_default_team(path: Path, name: str | None) -> None:
    """Upsert (or, with ``None``, remove) the top-level ``default_team`` key.

    Clearing an absent key is a no-op and never creates a missing file (so
    ``team use --clear`` stays a safe fix path). tomlkit places a new top-level
    scalar BEFORE the first table (regression-tested — a value appended after
    ``[teams.x]`` would re-parse as that table's member and corrupt the file).
    Serialized by :func:`config_write_lock`. The "no file to clear" check stays
    OUTSIDE the lock: taking it would create the lock file (and, on a missing
    parent directory, fail) for a call that is defined to touch nothing.
    Raises ``TeamFileError``.
    """
    if name is None and not path.exists():
        return
    with config_write_lock(path):
        if name is None:
            doc = load_document(path)
            if "default_team" not in doc:
                return
            del doc["default_team"]
        else:
            doc = load_document(path)
            doc["default_team"] = name
        _atomic_write(path, tomlkit.dumps(doc))


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory, so the rename itself is durable.

    Not every platform lets you open a directory for this; where it fails, the file
    contents are still fsynced and the rename is still atomic — only the ordering
    guarantee across a power loss is weaker."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _restore_mode(fd: int, tmp: Path, mode: int) -> None:
    """Put back the bits ``os.open``'s umask masking cleared.

    ``os.fchmod`` is Unix-only, so on Windows it would raise ``AttributeError`` —
    outside the ``OSError`` boundary this module converts — and fail every write to
    an existing config. Fall back to chmod-by-path there; the temp is ours and
    freshly created, and Windows only honours the read-only bit anyway.

    The fallback's pathname is not a meaningful race: it runs only where
    ``fchmod`` is absent (Windows), and there a file this process holds open
    cannot be renamed or deleted by another process unless it was opened with
    delete sharing, which CPython does not request."""
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)
    else:  # pragma: no cover - exercised on Windows
        os.chmod(tmp, mode)


def _atomic_write(path: Path, text: str) -> None:
    """Temp file + ``os.replace`` in ``path.parent`` (the breaker/board recipe),
    so a failed write can never leave a truncated ``syncmill.toml``.

    The destination's mode is PRESERVED. ``NamedTemporaryFile`` creates at 0600 and
    ``os.replace`` carries that onto the destination, so writing a checked-in 0644
    project config silently narrowed it — a spurious mode change in git and a break
    for group-readable checkouts.

    A file that does not exist yet is created with ``O_CREAT|O_EXCL`` and mode 0666,
    so the KERNEL applies the process umask — exactly what a plain ``open()`` would
    produce. Reading the umask in userspace means setting it to 0 and restoring it,
    which is process-global: any thread (the dashboard runs Textual) or fork inside
    that window would create its files world-writable.

    The temp is created with the DESTINATION's mode, not widened-then-narrowed: a
    private 0600 config written into a 0644 temp is world-readable for as long as it
    takes to write it. ``os.open``'s mode is umask-masked, so the following
    ``fchmod`` restores any bit the umask cleared — by then the file has only ever
    been at most as permissive as its destination.

    Durability order is fchmod → fsync → replace → fsync(parent): the mode is set
    before the fsync that has to cover it, and the parent sync is what makes the
    rename survive a power loss rather than leaving a zero-length config.
    """
    try:
        mode: int | None = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None  # genuinely new: let the umask decide, via O_CREAT below
    except OSError as exc:
        # Only "the file is not there" means "new file". Treating any stat failure
        # that way makes a transient inspection error (EIO, a stale NFS handle)
        # silently republish a PRIVATE config at the umask default. Refuse before
        # creating anything.
        raise TeamFileError(f"cannot inspect {path}: {exc}")
    tmp = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    created = False
    try:
        fd = os.open(
            str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else mode
        )
        created = True  # only now do we own this path, and may remove it
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            if mode is not None:
                _restore_mode(fh.fileno(), tmp, mode)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        created = False  # renamed away; nothing of ours is left at `tmp`
        _fsync_dir(path.parent)
    except OSError as exc:
        raise TeamFileError(f"cannot write {path}: {exc}")
    finally:
        # Every exit path, not just OSError: a KeyboardInterrupt or an encoding
        # failure mid-write would otherwise strand a partial temp. Guarded by
        # `created` so an O_EXCL collision never unlinks a file we did not make.
        if created:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def write_document(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Dump ``doc`` over ``path`` atomically, preserving mode and comments.

    Public so ``project.py`` can refresh a project config through the one module
    that owns toml writing, rather than hand-serializing a second time — which
    also puts `init`'s writes under the same :func:`config_write_lock` as the
    team writers, so an `init --force` repair and a `team save` cannot interleave
    into a lost update. The caller read ``doc`` before this call, so it should
    hold the lock across that read whenever the two must be one transaction."""
    with config_write_lock(path):
        _atomic_write(path, tomlkit.dumps(doc))


def file_team_names(path: Path) -> set[str]:
    """The team names defined in ``path`` ITSELF (not the resolved view) —
    `team use` warns when the chosen name resolves only via ``SYNCMILL_TEAMS``,
    since the persisted default would dangle without that env (codex
    design-review Major). Raises ``TeamFileError`` like ``load_document``."""
    return set(load_document(path).get("teams") or {})


def load_config_at(path: Path) -> SyncmillConfig:
    """Construct the resolved config with the toml source pointed at ``path``.

    The dashboard's post-write reload seam (codex design-review Major): a plain
    ``SyncmillConfig()`` reads ``model_config["toml_file"]`` relative to the
    *current* cwd, which need not be the file the TEAM pane just wrote — this
    pins the toml layer to the exact injected path while preserving the normal
    env > file > defaults precedence (a one-off subclass, so the global
    ``SyncmillConfig.model_config`` is never mutated). Raises pydantic's
    ``ValidationError`` like any construction; callers degrade best-effort.
    """

    class _ConfigAt(SyncmillConfig):
        model_config = {**SyncmillConfig.model_config, "toml_file": str(path)}

    return _ConfigAt()


def parse_capability_specs(specs: Sequence[str]) -> dict[str, list[str]]:
    """Parse ``"agent=tok1,tok2"`` specs (the ``--capability`` flag / TEAM-pane
    field shape) into a capabilities map. Raises ``TeamFileError`` on a missing
    ``=``, an empty agent id, no non-empty tokens, or a duplicate agent key."""
    caps: dict[str, list[str]] = {}
    for spec in specs:
        agent_id, sep, raw = spec.partition("=")
        agent_id = agent_id.strip()
        if not sep or not agent_id:
            raise TeamFileError(
                f"invalid capability spec {spec!r} — expected 'agent=tok1,tok2'"
            )
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if not tokens:
            raise TeamFileError(
                f"capability spec {spec!r} declares no tokens — "
                f"expected 'agent=tok1,tok2'"
            )
        if agent_id in caps:
            raise TeamFileError(f"duplicate capability spec for agent {agent_id!r}")
        caps[agent_id] = tokens
    return caps


def render_team(name: str, team: TeamConfig, config: SyncmillConfig) -> str:
    """One team profile as plain text — the shared renderer `team show` echoes
    and the dashboard TEAM pane displays verbatim, so the two surfaces stay
    byte-identical (the ``render_item``/``render_board_stats`` precedent)."""
    lines = [f"Team: {name}", "", "Agents (team priority order):", ""]
    for i, agent_id in enumerate(team.agents, 1):
        if agent_id not in _AGENT_CONSTRUCTORS:
            status = "unknown agent"
        elif config.agent_config(agent_id).enabled:
            status = "enabled"
        else:
            status = "disabled"
        line = f"  {i}. {agent_id} [{status}]"
        caps = team.capabilities.get(agent_id)
        if caps:
            line += f"  capabilities: {', '.join(caps)}"
        lines.append(line)
    strategy = team.strategy or f"(inherits default_strategy: {config.default_strategy})"
    lines.append("")
    lines.append(f"strategy: {strategy}")
    lines.append(f"goals:    {', '.join(team.goals) if team.goals else '—'}")
    return "\n".join(lines)
