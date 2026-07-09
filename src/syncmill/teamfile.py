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
- ``TeamFileError`` is the single error type at the seam (``BoardError``
  precedent): the CLI converts to ``BadParameter``/``ClickException``, the
  dashboard mirrors it into ``last_team_error`` — this module never imports
  click.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import tomlkit
from pydantic import ValidationError
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    TomlConfigSettingsSource,
)
from tomlkit.items import Table

from syncmill.agents import _AGENT_CONSTRUCTORS
from syncmill.config import SyncmillConfig, TeamConfig, check_team_goals


class TeamFileError(Exception):
    """A team-profile write/validation failure (parse error, invalid profile,
    unwritable file). The single error type at this seam."""


def toml_path() -> Path:
    """The file every helper reads and writes — derived from
    ``SyncmillConfig.model_config["toml_file"]`` so the write path is
    read/write-symmetric with discovery by construction (cwd-relative
    ``syncmill.toml`` in production; the hermetic suite's autouse
    ``isolate_toml_discovery`` repoints it into ``tmp_path``, so a save test
    can never touch a real repo-root file even if it forgets to repoint)."""
    return Path(SyncmillConfig.model_config.get("toml_file") or "syncmill.toml")


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
    Atomic (temp + ``os.replace``). Raises ``TeamFileError``.
    """
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
    Raises ``TeamFileError``.
    """
    if name is None:
        if not path.exists():
            return
        doc = load_document(path)
        if "default_team" not in doc:
            return
        del doc["default_team"]
    else:
        doc = load_document(path)
        doc["default_team"] = name
    _atomic_write(path, tomlkit.dumps(doc))


def _atomic_write(path: Path, text: str) -> None:
    """Temp file + ``os.replace`` in ``path.parent`` (the breaker/board recipe),
    so a failed write can never leave a truncated ``syncmill.toml``."""
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp = Path(fh.name)
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        raise TeamFileError(f"cannot write {path}: {exc}")


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
