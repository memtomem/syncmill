"""Project discovery, identity, and first-run initialization."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tomllib
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import tomlkit
import tomlkit.exceptions

from syncmill import teamfile
from syncmill.config import SyncmillConfig
from syncmill.git_env import isolated_git_env


logger = logging.getLogger(__name__)


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


_MARKER_RELATIVE = ".syncmill/project.json"
_MARKER_DIR = ".syncmill"
_LOCAL_IGNORE_RELATIVE = ".syncmill/.gitignore"

# The id becomes a directory component under the runs/workspace state roots
# (`ArtifactStore` joins it straight into a path), so it must be a single
# conservative path token: no separators, no leading dot (which also excludes
# "." and ".."), bounded length. Everything syncmill mints — a uuid4, the
# 24-hex path digest — passes.
# ``fullmatch``, not ``match``: ``$`` also matches just before a trailing
# newline, so a marker holding "safe-id\n" would otherwise pass and become a
# directory name carrying a newline.
_SAFE_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# Windows refuses these as path components in any casing and with any
# extension, so an id built from one would break the artifact, session, and
# room constructors on a platform the foreground commands are meant to work on
# — rather than degrading to the path hash the way every other rejection does.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def _is_windows_reserved(value: str) -> bool:
    # A trailing period is refused for the same reason as the device names:
    # Win32 strips it from a path component, so `cache.` would silently address
    # `cache` — a different project's state — instead of failing loudly.
    if value.endswith("."):
        return True
    return value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES


def _aliases(one: Path, other: Path) -> bool:
    """True when both paths name the SAME file on this filesystem.

    Asked instead of assuming, because a case-INSENSITIVE match is only
    meaningful on a case-insensitive filesystem. On Linux `.Syncmill/` is a
    genuinely different directory, and folding it onto the marker would let an
    unrelated case-variant submodule block `init`, or an uppercase tracked
    sibling discard a legitimate lowercase local marker.
    """
    try:
        return os.path.samefile(one, other)
    except OSError:
        return False


_GITLINK_MODE = "160000"
_SYMLINK_MODE = "120000"


def _is_symlink_entry(meta: str) -> bool:
    """True for a Git entry recorded as a symlink (mode ``120000``).

    Used unconditionally only for the ANCESTOR half of the HEAD walk: a link
    entry never carries a tree, so descending into it would send ``ls-tree``
    into a blob, answer "unknown", and block ``init --force`` outright — and a
    link materialized as a regular file at the marker DIRECTORY's path cannot
    hold a marker file inside it either way. Everywhere an entry is matched as
    the marker FILE, use :func:`_symlink_entry_excluded` instead, which asks
    the checkout what actually sits there. The ``160000`` gitlink case is
    never excluded — there the content really does arrive with the clone.
    """
    return meta.startswith(f"{_SYMLINK_MODE} ")


def _symlink_entry_excluded(root: Path, meta: str, listed: str) -> bool:
    """True when a symlink entry supplies nothing but a LINK on this checkout.

    A ``120000`` entry is excluded from alias matching only while the checkout
    actually holds a symlink at its path. There, what Git supplies is the link
    — the target's content is whatever the filesystem resolves it to, and
    refusing to read through a link is already handled separately; left in,
    such an entry would let a repository break an ordinary project by tracking
    any symlink that happens to resolve to the marker (the round-14 exclusion).

    But under ``core.symlinks=false`` (the common Windows checkout) Git
    MATERIALIZES the entry: the link payload lands on disk as a regular file,
    ``read_text`` opens repository-controlled bytes, and no separate symlink
    guard fires. That content did arrive with the clone, so the entry must
    count like any tracked file — a repository could otherwise ship the marker
    itself as a ``120000`` payload and have its id trusted. An absent path
    participates too: exclusion is the trusting direction, so uncertainty
    matches (``islink`` reports False on anything it cannot examine).
    """
    if not meta.startswith(f"{_SYMLINK_MODE} "):
        return False
    return os.path.islink(root / listed)

# What, if anything, makes Git the SUPPLIER of the marker's content. ``None``
# means nothing does. The three named cases are all "do not trust the id"; only
# ``gitlink`` additionally means "do not write here" (see
# ``_reject_unwritable_marker_path``).
_MARKER_SUPPLY_TRACKED = "tracked"
_MARKER_SUPPLY_GITLINK = "gitlink"
_MARKER_SUPPLY_UNKNOWN = "unknown"


def _marker_supply(root: Path) -> str | None:
    """How Git supplies the marker, or ``None`` when it does not.

    "Supplied by Git" is two things, because the superproject index can carry
    the marker's content without ever naming its path. The marker's own path
    being tracked is the plain case. The other is a tracked GITLINK ancestor: a
    populated ``.syncmill`` submodule is a single ``160000`` entry at
    ``.syncmill``, so nothing names ``.syncmill/project.json`` at all, yet the
    file's content still arrives with the clone.

    Only a listing that names neither means "not tracked"; a failure (not a
    repository, no ``git``) means the question went unanswered, which distrusts
    the marker — the path-hash fallback is always safe, a shipped id never is.

    Both the INDEX and HEAD are consulted. Staging a shipped marker's removal
    (``git rm --cached``) leaves the file in the worktree while taking it out
    of the index, so an index-only question would call repository content
    machine-local.

    The pathspec carries ``:(icase)`` because on a case-insensitive filesystem
    (the macOS and Windows defaults) a repository can commit
    ``.syncmill/Project.json``, which ``read_text`` opens through the lowercase
    path while an exact-case query reports nothing. A case-VARIANT hit is then
    confirmed with ``_aliases`` before it counts, since on a case-sensitive
    filesystem it names a different file entirely.

    Entries are matched by their FULL path rather than treating any hit under
    the prefix as tracking — ``syncmill init`` writes a `.syncmill/.gitignore`
    the project is meant to commit, and a tracked sibling says nothing about
    the marker.
    """
    index = _index_supply(root)
    if index in {_MARKER_SUPPLY_GITLINK, _MARKER_SUPPLY_UNKNOWN}:
        return index
    # BOTH sources, even when the index already answered "tracked": during a
    # submodule-to-directory conversion the index can hold a staged
    # `.syncmill/project.json` while HEAD still records `.syncmill` as a
    # gitlink and the populated submodule is still on disk. Only the gitlink
    # answer stops `init` writing into that nested repository, so it — and an
    # unanswered question — take precedence over a plain "tracked".
    head = _head_supply(root)
    if head in {_MARKER_SUPPLY_GITLINK, _MARKER_SUPPLY_UNKNOWN}:
        return head
    return index or head


def _alias_pathspecs(root: Path, relative_dir: str) -> list[str]:
    """Literal pathspecs for on-disk entries that ALIAS ``relative_dir``.

    Bounded by the repository's top level, and empty in the ordinary case where
    the only name for the directory is its own. ``:(literal)`` so a name
    carrying glob characters is matched as itself.
    """
    target = root / relative_dir
    specs: list[str] = []
    try:
        names = [entry.name for entry in os.scandir(root)]
    except OSError:
        return specs
    for name in names:
        if name != relative_dir and _aliases(root / name, target):
            specs.append(f":(literal){name}")
    return specs


def _index_supply(root: Path) -> str | None:
    """The index half of :func:`_marker_supply`.

    ``:(icase)`` is Git's notion of a case match, which is NARROWER than the
    filesystem's notion of an alias — NTFS resolves names through an uppercase
    table that is not Unicode case folding, so ``.syncmıll`` can open as the
    canonical path while the pathspec skips it. The listing is therefore also
    given a literal pathspec for every on-disk name that actually aliases the
    marker directory, which the filesystem is asked directly (see
    ``_alias_pathspecs``).

    That is done instead of dropping the pathspec altogether, which would mean
    a ``samefile`` pair per INDEX ENTRY — the whole repository — on a call
    every command makes. Scoping by the aliases that exist on disk costs one
    directory scan and covers the same ground: a name Git will not match but
    the filesystem will resolve is, by definition, a name sitting there to be
    found.
    """
    argv = ["git", "ls-files", "-z", "--stage", "--", f":(icase){_MARKER_DIR}"]
    argv.extend(_alias_pathspecs(root, _MARKER_DIR))
    try:
        proc = subprocess.run(
            argv, cwd=str(root), env=isolated_git_env(), capture_output=True,
        )
    except OSError:
        return _MARKER_SUPPLY_UNKNOWN
    if proc.returncode != 0:
        return _MARKER_SUPPLY_UNKNOWN
    # Decoded with surrogateescape, never `text=True`: the listing covers the
    # whole marker directory, and a tracked sibling whose name is not valid
    # UTF-8 (legal on POSIX) would raise UnicodeDecodeError straight out of
    # `project_id`, which is required to degrade rather than fail. The names
    # being compared are pure ASCII, so an undecodable sibling simply cannot
    # match.
    listing = proc.stdout.decode("utf-8", errors="surrogateescape")
    supply = None
    for record in listing.split("\0"):
        meta, _, path = record.partition("\t")
        if not path or _symlink_entry_excluded(root, meta, path):
            continue
        if meta.startswith(f"{_GITLINK_MODE} ") and _names_marker_path(
            root, path, _MARKER_DIR
        ):
            # Reported in preference to a plain tracked path: it is the one
            # that also makes the location unsafe to WRITE.
            return _MARKER_SUPPLY_GITLINK
        if _names_marker_path(root, path, _MARKER_RELATIVE):
            supply = _MARKER_SUPPLY_TRACKED
    return supply


def _names_marker_path(root: Path, listed: str, expected: str) -> bool:
    """True when a listed Git path refers to ``expected`` under ``root``.

    An exact string match needs nothing more. EVERY other candidate is put to
    the filesystem rather than pre-filtered by a case comparison, because
    "these two names open the same file" is not a question Python's
    ``casefold`` can answer for someone else's filesystem: NTFS resolves names
    through an uppercase table that is not Unicode case folding (dotless ``ı``
    uppercases to ``I`` there), so ``.syncmıll/project.json`` can open through
    the canonical path while the two spellings fold differently. ``_aliases``
    costs a ``stat`` pair on a listing bounded by the marker's own directory
    and the repository's top level, and returns False for a path that does not
    exist — which is every unrelated entry.
    """
    if listed == expected:
        return True
    return _aliases(root / listed, root / expected)


def _head_supply(root: Path) -> str | None:
    """The committed-history half of :func:`_marker_supply`.

    HEAD is ENUMERATED rather than probed by path, because a ``HEAD:<path>``
    lookup is case-sensitive while the worktree spelling it would be built from
    is whatever the checkout currently holds — a committed ``.SyncMill``
    renamed to canonical casing after a staged removal would slip past an exact
    probe. Listing instead lets the same ``_names_marker_path`` test the index
    half uses decide, so both halves agree on when a case-variant counts.

    The ancestor is examined separately from the file, because Git does not
    traverse a gitlink: for a committed ``.syncmill`` submodule whose entry was
    staged away (``git rm --cached .syncmill``), the populated worktree and its
    shipped marker remain while the file has no path in HEAD at all.
    """
    head = _git_query(root, ["rev-parse", "--quiet", "--verify", "HEAD"])
    if head is None:
        return _MARKER_SUPPLY_UNKNOWN
    if head.returncode == 1:
        # No commits yet — the ordinary `git init && syncmill init` sequence,
        # which must not be mistaken for a broken repository.
        return None
    if head.returncode != 0:
        return _MARKER_SUPPLY_UNKNOWN
    top = _head_entries(root, "HEAD")
    if top is None:
        return _MARKER_SUPPLY_UNKNOWN
    for meta, path in top:
        if _is_symlink_entry(meta):
            continue
        if not _names_marker_path(root, path, _MARKER_DIR):
            continue
        if meta.startswith(f"{_GITLINK_MODE} "):
            return _MARKER_SUPPLY_GITLINK
        children = _head_entries(root, f"HEAD:{path}")
        if children is None:
            return _MARKER_SUPPLY_UNKNOWN
        for child_meta, child in children:
            # `ls-tree` of a subtree reports bare names, so the marker's own
            # path is rebuilt before it is compared.
            if _symlink_entry_excluded(root, child_meta, f"{path}/{child}"):
                continue
            if _names_marker_path(root, f"{path}/{child}", _MARKER_RELATIVE):
                return _MARKER_SUPPLY_TRACKED
    return None


def _head_entries(root: Path, treeish: str) -> list[tuple[str, str]] | None:
    """``(meta, path)`` pairs of one committed tree, or ``None`` if unreadable."""
    listing = _git_query(root, ["ls-tree", "-z", "--full-tree", treeish])
    if listing is None or listing.returncode != 0:
        return None
    text = listing.stdout.decode("utf-8", errors="surrogateescape")
    entries = []
    for record in text.split("\0"):
        meta, _, path = record.partition("\t")
        if path:
            entries.append((meta, path))
    return entries


def _git_query(root: Path, args: list[str]) -> subprocess.CompletedProcess | None:
    """Run a read-only git query, or ``None`` when it could not run at all."""
    try:
        return subprocess.run(
            ["git", *args], cwd=str(root), env=isolated_git_env(), capture_output=True,
        )
    except OSError:
        return None


def _marker_path_is_symlinked(root: Path) -> bool:
    """True when any component of the marker path under ``root`` is a symlink.

    A trackedness query is LEXICAL: it asks about the path as written. If a
    checkout commits ``.syncmill`` as a symlink to a checked-in directory, the
    index holds the symlink and the payload under two different names, the
    query for ``.syncmill/project.json`` finds neither — and the read still
    lands on repository-controlled content. Rather than chase where a link
    goes, refuse to read through one at all: syncmill's own ``init`` writes a
    real directory holding a real file, so a link here is never syncmill's.
    A missing path is not a symlink, and the ordinary no-marker case simply
    falls through to the read, which then fails.

    Only components BELOW ``root`` are examined — the checkout itself may
    legitimately sit under a symlinked path (``/tmp`` on macOS), which says
    nothing about who controls the marker.
    """
    return _has_symlinked_component(root, _MARKER_RELATIVE)


def _has_symlinked_component(root: Path, relative: str) -> bool:
    """True when any component of ``relative`` under ``root`` is a link."""
    current = root
    for part in Path(relative).parts:
        current = current / part
        try:
            # ``is_junction`` as well as ``is_symlink``: an NTFS directory
            # junction is a traversable reparse point that ``is_symlink``
            # reports as False, so on Windows a ``.syncmill`` junction could
            # point outside the repository through a check that called the
            # path ordinary.
            if current.is_symlink() or current.is_junction():
                return True
        except OSError:
            return True
    return False


def _marker_project_id(root: Path) -> str | None:
    """Return the machine-local marker's id, or ``None`` when it isn't trusted.

    The marker is machine-local state ``syncmill init`` writes (0600), not
    project source — but it lives inside the working tree, so a cloned
    repository can SHIP one and bind this machine's state roots to an id chosen
    by whoever committed it. Three gates before the id is honored: the id must
    be a safe single path component (it becomes a directory name — ``../..``
    must never traverse the state roots), the path must not be reached through
    a symlink (which would put the read and the lexical trackedness query on
    different files), and the file must not be tracked by Git (a tracked marker
    arrived with the clone, not from this machine's ``init``; asked per read,
    not recorded at init, because the file can become tracked at any time
    after). A rejected marker degrades to the path-hash id with a warning,
    never an error — identity must not brick every command over a hostile or
    damaged tree.
    """
    marker = root / _MARKER_RELATIVE
    if _marker_path_is_symlinked(root):
        logger.warning(
            "ignoring %s: it is reached through a symlink, so its content is "
            "not necessarily this machine's own project marker",
            marker,
        )
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A malformed marker is not an error — the whole content is
        # repository-controllable, so raising here would let a checkout
        # turn every syncmill command into a traceback.
        logger.debug("no usable project marker at %s: %s", marker, exc)
        return None
    # The top level is checked BEFORE reaching for a key: valid JSON that is
    # not an object (`[]`, `"x"`, `null`) has no `.get`, and AttributeError is
    # neither OSError nor ValueError — the same trap `initialize_project`'s own
    # probe was already written against.
    value = data.get("id") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value:
        return None
    if not _SAFE_PROJECT_ID.fullmatch(value) or _is_windows_reserved(value):
        logger.warning("ignoring %s: id %r is not a safe path token", marker, value)
        return None
    if _marker_supply(root) is not None:
        logger.warning(
            "ignoring %s: it is supplied by Git, so it shipped with the "
            "repository instead of coming from this machine's `syncmill init`",
            marker,
        )
        return None
    return value


def project_id(root: Path, configured: str | None = None) -> str:
    if configured:
        # Explicit config is the operator's own trusted input (the same file
        # carries [gate] commands), so it is not second-guessed here.
        return configured
    value = _marker_project_id(root)
    if value is not None:
        return value
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


def _reserve_backup_path(config_path: Path) -> Path:
    """Claim a `.bak` name atomically and return it.

    A second repair must not overwrite the first one's backup — that copy is the
    user's only remaining record of the config being replaced. Testing `exists()`
    and then calling `os.replace` is check-then-use: two concurrent `init --force`
    runs can both pick `.bak`, and the second's rename silently overwrites the
    first's backup (`os.replace` is happy to). `O_CREAT|O_EXCL` makes the name ours
    before we move anything onto it."""
    counter = 0
    while True:
        suffix = ".bak" if counter == 0 else f".bak.{counter}"
        candidate = config_path.with_suffix(config_path.suffix + suffix)
        try:
            os.close(os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return candidate
        except FileExistsError:
            counter += 1
        except OSError as exc:
            raise ProjectError(
                f"cannot reserve a backup name beside {config_path}: {exc}"
            ) from exc


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

    The whole merge runs inside ``teamfile.config_write_lock``: the read at the
    top and the write at the bottom are one transaction, so a `team save`
    landing between them cannot be discarded by this write (the writers'
    own re-entrant acquisitions inside are then free).
    """
    try:
        with teamfile.config_write_lock(config_path):
            _merge_project_config(config_path, pid)
    except teamfile.TeamFileError as exc:
        raise ProjectError(str(exc)) from exc


def _merge_project_config(config_path: Path, pid: str) -> None:
    """The body of :func:`_write_project_config`, run under the config lock."""
    if not config_path.exists():
        _write_fresh_config(config_path, pid)
        return
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"cannot read {config_path}: {exc}") from exc
    try:
        doc = tomlkit.parse(text)
    except tomlkit.exceptions.ParseError:
        # ONLY a parse error moves the file aside. A blanket `except Exception`
        # would treat any unexpected failure in the parser as proof the user's
        # config is malformed, and replace a file that was fine.
        backup = _reserve_backup_path(config_path)
        try:
            os.replace(config_path, backup)
        except OSError as exc:
            # Remove the placeholder we reserved: leaving it behind puts an EMPTY
            # `.bak` beside a config that is still in place, which reads as "your
            # config was backed up and lost". Best-effort, and never masking the
            # failure that brought us here.
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProjectError(
                f"{config_path} is unparseable and could not be moved aside: {exc}"
            ) from exc
        _write_fresh_config(config_path, pid)
        return
    if "config_version" in doc:
        version = doc["config_version"]
        # The config model COERCES `3.0` and `"3"` to 3, so an isinstance(int) test
        # lets those bypass the guard and be relabelled as 2 — and `true` is an int
        # in Python, which would pass as version 1. Anything that is not a plain
        # integer is refused outright rather than guessed at.
        if isinstance(version, bool) or not isinstance(version, int):
            raise ProjectError(
                f"{config_path} declares a config_version that is not an integer "
                f"({version!r}) — fix it before re-initializing"
            )
        if version < 1:
            # `SyncmillConfig` requires >= 1, so preserving a 0 or negative value
            # would leave `init --force` reporting success over a config that no
            # command can load.
            raise ProjectError(
                f"{config_path} declares config_version {version}, which is not a "
                f"valid version — fix it before re-initializing"
            )
        if version > _SUPPORTED_CONFIG_VERSION:
            # Stamping this file as version 2 would relabel a schema we do not
            # understand — the relabelling is the kind of quiet damage this change
            # is about. Refuse instead.
            raise ProjectError(
                f"{config_path} declares config_version {version}, newer than this "
                f"syncmill writes ({_SUPPORTED_CONFIG_VERSION}) — upgrade syncmill, "
                f"or move the file aside to re-initialize"
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


def _reject_unwritable_marker_path(root: Path) -> None:
    """Refuse to CREATE the marker where the path is not ours to write through.

    Reading such a path merely distrusts its content; writing acts on whatever
    it leads to. A symlinked component makes ``write_text``/``chmod`` land on a
    file the checkout chose — possibly outside the repository entirely — and a
    ``.syncmill`` submodule would take an unrelated repository's working tree
    dirty. ``init`` is an explicit foreground command, so this raises rather
    than degrading: silently minting an id somewhere else is worse than saying
    the tree needs fixing first.

    An ordinary TRACKED marker is deliberately not refused — it is a normal
    local file, and rewriting it with a freshly minted id is exactly the
    upgrade path for a project that committed one.
    """
    # The ignore file beside the marker is written too (created, and appended
    # to), so it needs the same guard: a checkout can leave the marker path
    # ordinary and track `.syncmill/.gitignore` as a symlink, and `init` would
    # then append its rules to a file of the checkout's choosing.
    for relative in (_MARKER_RELATIVE, _LOCAL_IGNORE_RELATIVE):
        if _has_symlinked_component(root, relative):
            raise ProjectError(
                f"{root / relative} is reached through a symlink — remove it "
                f"before initializing, so syncmill writes where it belongs"
            )
    # UNKNOWN is refused with the confirmed gitlink: it means the question
    # could not be answered, and `.syncmill` may well BE a submodule. Reading
    # fails safe by falling back to the path hash, but writing has no such
    # fallback — the cross-repository write would simply happen. `git_root`
    # has already succeeded by here, so an unanswered question means a
    # genuinely broken repository rather than a missing `git`.
    #
    # The two sources are asked SEPARATELY here, unlike the read path's
    # `_marker_supply`: a gitlink that survives only in HEAD blocks the write
    # only while the nested checkout is still on disk. After a full removal
    # (`git rm .syncmill` — index entry and worktree both gone) there is no
    # other repository left to write into, and refusing would force an
    # intermediate commit into the ordinary submodule-to-directory
    # conversion. The read path stays conservative — a historical gitlink
    # still distrusts a shipped id — but "do not write here" is about where
    # the bytes LAND, which only the present tree can answer.
    index = _index_supply(root)
    head = _head_supply(root)
    refused = index in {_MARKER_SUPPLY_GITLINK, _MARKER_SUPPLY_UNKNOWN} or (
        head == _MARKER_SUPPLY_UNKNOWN
    ) or (head == _MARKER_SUPPLY_GITLINK and _nested_checkout_present(root))
    if refused:
        raise ProjectError(
            f"{root / _MARKER_DIR} is a Git submodule, or its status could not "
            f"be read — syncmill will not write its project marker into another "
            f"repository's working tree"
        )


def _nested_checkout_present(root: Path) -> bool:
    """True while a repository checkout still sits at the marker directory.

    A submodule checkout is a working tree carrying a ``.git`` entry — a
    gitdir-pointer file for an absorbed submodule, a directory otherwise, and
    ``lexists`` also counts a symlinked one. A leftover plain directory
    without it is not another repository's working tree, so writing the
    marker there dirties nothing but the caller's own untracked files.
    """
    return os.path.lexists(root / _MARKER_DIR / ".git")


def _path_is_ignored(root: Path, relative: str) -> bool:
    """Ask Git whether ``relative`` is covered by an ignore rule.

    ``--no-index`` (as ``_require_ignored_state_root`` already passes) because
    plain ``check-ignore`` suppresses TRACKED paths — and an already-tracked
    marker is exactly the upgrade this change exists for. Without it the answer
    is "not ignored" however good the rules are, and every ``init --force``
    would append one more identical line.
    """
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "--", relative],
            cwd=str(root), env=isolated_git_env(), capture_output=True,
        )
    except OSError:
        return False
    return proc.returncode == 0


def _ensure_ignored(root: Path, ignore_file: Path, rule: str, probe: str) -> None:
    """Append ``rule`` unless it is both PRESENT here and actually in effect.

    Two independent requirements, which is why neither test alone is enough.

    The rule must physically be in this file, because this file is the one that
    TRAVELS: `.git/info/exclude`, `core.excludesFile`, and a user's global
    excludes are all machine-local, so a project whose paths are ignored only
    through those would commit an empty `.syncmill/.gitignore` and leave the
    next clone exposed.

    And Git must actually be ignoring the path, which is asked rather than
    inferred: a `.gitignore` can hold the rule and then negate it
    (`!project.json`, or a blanket `!*`), where a membership test sees its own
    rule present and adds nothing while the path stays exposed. Appending fixes
    that too — within one file the LAST matching pattern wins — and
    `.git/info/exclude` could not rescue it, since a per-directory `.gitignore`
    outranks it. One append converges: the rule is then present AND last.
    """
    try:
        text = ignore_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if rule in text.splitlines() and _path_is_ignored(root, probe):
        return
    with ignore_file.open("a", encoding="utf-8") as handle:
        if text and not text.endswith("\n"):
            handle.write("\n")
        handle.write(f"{rule}\n")


def initialize_project(path: Path, *, force: bool = False) -> tuple[Path, str]:
    root = git_root(path.resolve())
    config_path = root / "syncmill.toml"
    marker = root / _MARKER_RELATIVE
    if config_path.exists() and not force:
        raise ProjectError(f"already initialized: {config_path}")
    # Before anything is written: the marker path must be one we may write
    # THROUGH, which is a stricter question than whether its content is trusted.
    _reject_unwritable_marker_path(root)
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
        # The same trust gates as every other marker read: a tracked or
        # unsafe id is not preserved across a re-init — adopting it here would
        # launder a shipped id into the config, where it IS trusted.
        pid = _marker_project_id(root)
    pid = pid or str(uuid4())
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"id": pid}, indent=2) + "\n", encoding="utf-8")
    os.chmod(marker, 0o600)
    # `project.json` is machine-local identity (see _marker_project_id) — the
    # ignore rule keeps a fresh project from ever committing it, since a marker
    # that becomes tracked stops being trusted.
    local_ignore = root / _LOCAL_IGNORE_RELATIVE
    # `init` documents this file as one of its artifacts and the guides tell
    # the reader to commit it, so it exists even in the corner where every rule
    # turns out to be a no-op.
    local_ignore.touch(exist_ok=True)
    _ensure_ignored(root, local_ignore, "memory/", f"{_MARKER_DIR}/memory/")
    _ensure_ignored(root, local_ignore, "project.json", _MARKER_RELATIVE)
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
            existing_lines = existing.splitlines()
            rules = [
                r
                for r in (".syncmill/memory/", _MARKER_RELATIVE)
                if r not in existing_lines
            ]
            if rules:
                exclude_path.parent.mkdir(parents=True, exist_ok=True)
                with exclude_path.open("a", encoding="utf-8") as handle:
                    if existing and not existing.endswith("\n"):
                        handle.write("\n")
                    for rule in rules:
                        handle.write(f"{rule}\n")
        except OSError as exc:
            raise ProjectError(f"could not protect project memory in Git: {exc}") from exc
    return config_path, pid
