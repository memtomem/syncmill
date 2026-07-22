"""Read-only tmux viewer for a comparison-room round (#0.1.2 Phase 2).

A room round already tees each member's output to a private bounded log
(:class:`syncmill.rooms.RoundLogSink`). This module surfaces those logs live:
``room attach`` (and the TUI Rooms ``t`` key) open a **read-only** tmux session
with one ``tail -F`` pane per member. It is observation-only — the panes run
``tail``, never a provider — so there is no steering here (interactive steering
stays in ``session``).

It is deliberately a flat top-level module (the gateway/monitor/session/board
precedent) that **reuses monitor's tmux primitives** (``_tmux``/``_tmux_attach``/
``tmux_available``) rather than reimplementing them, and follows session.py's
ownership discipline: a deterministic name is the reattach key, and lifecycle
operations target EXACT names (``=name``) so they can never touch a foreign
session. Two invariants carry over from session.py: it is **not** an agent and
**not** a strategy (it touches neither agents nor memory — only tmux and the
room store's already-written logs), and — unlike the round itself, whose
execution never touches tmux — an explicit attach with no tmux available is a
hard error (this IS the tmux feature, so there is no fallback).

Ownership is **project-scoped**. One tmux server is shared across every project
on a host, so the session name carries the project id: reclaiming viewers for
project B must never touch project A's live sessions. A sweep only ever
considers names under *this* project's prefix, and a per-room kill scopes to
*this* room's prefix — the project id + uuid4 room id together are the
independent ownership record, not a bare pattern match.

Round execution is untouched: the viewer only ever runs on an explicit user
attach, reading logs the round already wrote, so a tmux failure here can never
affect a running round.

Two attach state machines, deliberately separate because their lifecycles
differ:

* **External** (not inside tmux) — a FOREGROUND ``attach-session`` that blocks
  until the user detaches (``Ctrl-b d``); the caller (the CLI, or the TUI under
  ``App.suspend``) regains control on return.
* **Inside tmux** (``$TMUX`` set) — a non-blocking ``switch-client -E -t
  =<session>``; nesting an attach is refused by tmux, and switch does not block,
  so there is no suspend/return dance — the user returns with ``Prefix + L``.
"""

from __future__ import annotations

import os
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from syncmill.monitor import _tmux, _tmux_attach, tmux_available

if TYPE_CHECKING:  # a type hint only — importing at runtime would make rooms.py,
    # which drives teardown from the room lifecycle, import a module that imports
    # it back. Keeping it type-only makes the runtime graph rooms → room_viewer.
    from syncmill.rooms import RoomStore

_VIEWER_PREFIX = "syncmill-room-"

#: A tmux user option stamped on every viewer we create, carrying the project
#: key. Verified before ANY kill so a coincidental same-named user session (or a
#: session from a project whose key collides) is never reclaimed — the name is
#: the scope, this marker is the ownership PROOF.
_OWNER_OPTION = "@syncmill_room_viewer"

#: Terminal room states whose viewers a sweep may reclaim / a create must refuse.
_TERMINAL_ROOM_STATES = {"applied", "closed", "stale"}


class RoomViewerError(RuntimeError):
    """A room viewer could not be opened. Like ``SessionLaunchError``, this has no
    fallback — the viewer IS the tmux feature — so the CLI surfaces it directly."""


def _project_digest(project_id: str) -> str:
    """The full SHA-256 of the raw project id, stored in the ownership marker so
    the ownership check does not lean on the truncated name key."""
    return sha256(project_id.encode("utf-8", errors="surrogateescape")).hexdigest()


def _project_key(project_id: str) -> str:
    """A tmux-name-safe, collision-resistant key for a project. A lossy character
    substitution collides (``team/a`` and ``team:a`` map to one name, so a sweep
    of one would kill the other's viewers), so this is a digest of the RAW id.
    The name uses a short prefix of the digest; the full digest lives in the
    ownership marker (:func:`_project_digest`) as the authoritative check."""
    return _project_digest(project_id)[:16]


def _exact(name: str) -> str:
    """A tmux ``target-session`` spec forcing EXACT matching (session.py's rule):
    tmux resolves a raw target exact-THEN-prefix, so ``=name`` is what keeps a
    lookup from matching a longer foreign session when the exact one is absent.
    Used for SESSION targets (``has-session``/``kill-session``/``attach``/``switch``)."""
    return f"={name}"


def _window(name: str) -> str:
    """An exact WINDOW target for the session's current window. ``split-window``
    and ``select-layout`` need a window/pane target — a bare ``=session`` does not
    resolve as a pane ("can't find pane"), the trailing ``:`` picks its window."""
    return f"={name}:"


def _project_prefix(project_id: str) -> str:
    """The prefix every viewer session of one project shares — the project-scope
    ownership boundary a sweep must never cross."""
    return f"{_VIEWER_PREFIX}{_project_key(project_id)}-"


def viewer_session_name(project_id: str, room_id: str, round_number: int) -> str:
    """The deterministic tmux session name for a room round — the reattach key.

    Carries an injective project key so one host's shared tmux server can host
    viewers for many projects without a sweep in one reclaiming another's live
    sessions. Room ids are uuid4 (already tmux-safe).
    """
    return f"{_project_prefix(project_id)}{room_id}-r{round_number}"


def _room_prefix(project_id: str, room_id: str) -> str:
    """The prefix scoping a per-room kill to exactly this room's viewers. The
    trailing ``-r`` (round marker) prevents matching a room whose id is a prefix
    of another — room ids are uuid4, so this is exact to the room."""
    return f"{_project_prefix(project_id)}{room_id}-r"


def _session_exists(name: str) -> bool:
    return _tmux("has-session", "-t", _exact(name)).returncode == 0


def _is_owned_viewer(name: str, project_id: str) -> bool:
    """Whether a session carries our ownership marker for THIS project. Checked
    before every kill so a foreign/user session that merely matches the name
    pattern is never reclaimed.

    The marker holds the FULL project digest (not the truncated name key), so the
    ownership check is independent of the short prefix. tmux's ``set/show-options``
    reject a bare ``=session`` target ("no such session"), so it is addressed via
    the ``=name:`` form (the ``=``-prefixed session component still forces exact
    matching); it is a session option that survives the build-time rename."""
    res = _tmux("show-options", "-t", _window(name), "-v", _OWNER_OPTION)
    return res.returncode == 0 and res.stdout.strip() == _project_digest(project_id)


def _pane_count(name: str) -> int:
    res = _tmux("list-panes", "-t", _exact(name), "-F", "#{pane_id}")
    return len(res.stdout.split()) if res.returncode == 0 else 0


def _is_complete_owned_viewer(name: str, project_id: str, expected_panes: int) -> bool:
    """A session is a usable viewer only if it is ours AND has one pane per
    member — guards against an unowned same-named session, a pane that exited
    after publication, and an incompletely-built concurrent winner."""
    return _is_owned_viewer(name, project_id) and _pane_count(name) == expected_panes


def _resolve_round(store: RoomStore, room_id: str, round_number: int | None) -> dict:
    """The requested round (or the latest), validated to exist."""
    rounds = store.list_rounds(room_id)
    if not rounds:
        raise RoomViewerError(f"room {room_id} has no rounds yet")
    if round_number is None:
        return rounds[-1]
    for row in rounds:
        if int(row["number"]) == round_number:
            return row
    raise RoomViewerError(f"room {room_id} has no round {round_number}")


def ensure_viewer(
    store: RoomStore,
    project_id: str,
    room_id: str,
    round_number: int | None = None,
) -> str:
    """Ensure a read-only viewer session exists for a round and return its name.

    Idempotent: an existing session is returned as-is (the reattach path). A
    round with no per-member monitor rows (one that predates the progress table)
    is "terminal unavailable" — there is nothing live to follow — and raises
    rather than opening an empty session.
    """
    if not tmux_available():
        raise RoomViewerError("tmux is not available; the room viewer requires tmux")
    room = store.get_room(room_id)  # raises RoomError for an unknown room
    if room["state"] in _TERMINAL_ROOM_STATES:
        # A terminal room's viewer has nothing live to follow; refuse to recreate
        # one (else a sweep-then-attach would resurrect it every time).
        raise RoomViewerError(f"room is {room['state']}; nothing to view")
    round_row = _resolve_round(store, room_id, round_number)
    members = store.list_round_members(round_row["id"])
    if not members:
        raise RoomViewerError(
            f"round {round_row['number']} has no monitor data "
            "(it predates per-member progress); terminal unavailable"
        )
    name = viewer_session_name(project_id, room_id, int(round_row["number"]))
    if _session_exists(name):
        if _is_complete_owned_viewer(name, project_id, len(members)):
            return name  # ours and complete → reattach
        if _is_owned_viewer(name, project_id):
            # Ours but incomplete (a pane exited, or a prior build was killed
            # mid-flight): rebuild from scratch rather than attach a broken view.
            _tmux("kill-session", "-t", _exact(name))
        else:
            # An unowned session squats our exact name (a key collision, or a
            # user session). Never touch it; refuse rather than attach a foreign
            # session as if it were the viewer.
            raise RoomViewerError(
                f"a session named {name!r} exists but is not a syncmill viewer; refusing"
            )
    _create_viewer(store, project_id, name, room_id, round_row, members)
    return name


def _resolve_member_log(
    store: RoomStore, room_id: str, round_id: str, member: dict
) -> str | None:
    """Fail-closed absolute log path for a member, or ``None`` if invalid.

    The path is RECOMPUTED from authoritative room/round/member ids (never the
    stored string is trusted), required to equal the stored ``log_path``, and
    bound to the exact round directory with NO symlink indirection — so an
    absolute/``..``/symlinked ``log_path`` in the DB, or an in-root symlink that
    would rebind the member to another file, can never make the viewer tail
    outside the round's own tree.
    """
    stored = member.get("log_path")
    canonical = store.round_log_relpath(room_id, round_id, member["member_id"])
    if stored != canonical:
        return None  # drift from the authoritative path → do not follow it
    root = store.candidates.root.resolve()
    expected = root / canonical
    # Reject the leaf being a symlink, and any symlink in the chain (realpath
    # resolves every component, so a divergence means a symlink was traversed).
    if expected.is_symlink() or os.path.realpath(expected) != str(expected):
        return None
    try:
        expected.relative_to(root)
    except ValueError:
        return None
    if expected.exists() and not expected.is_file():
        return None
    return str(expected)


def _create_viewer(
    store: RoomStore,
    project_id: str,
    name: str,
    room_id: str,
    round_row: dict,
    members: list[dict],
) -> None:
    """Build the read-only session ATOMICALLY under a temp name, then rename it to
    the final name only once complete.

    Two properties this guarantees: (a) the final name never names a partial
    session — a concurrent reattach that sees it can safely attach; (b) if ANY
    member's log is invalid or ANY pane fails, the whole build is torn down and
    the create raises — a viewer never silently drops a member.
    """
    logs: list[tuple[str, str]] = []
    for member in members:
        path = _resolve_member_log(store, room_id, round_row["id"], member)
        if path is None:
            # Fail the whole viewer, not just this member — a partial viewer that
            # silently omits a member reads as complete and misleads the reviewer.
            raise RoomViewerError(
                f"member {member['member_id']} has no valid log; refusing a partial viewer"
            )
        logs.append((member["member_id"], path))
    # Seed each log so `tail -F` opens cleanly even before the first write.
    for _, path in logs:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a"):
                pass
        except OSError:
            pass  # best-effort; tail -F tolerates a not-yet-existent file
    temp = f"{name}-mk{uuid4().hex[:8]}"
    first_member, first_log = logs[0]
    created = _tmux(
        "new-session", "-d", "-s", temp, "-P", "-F", "#{pane_id}",
        "tail", "-n", "+1", "-F", first_log,
    )
    if created.returncode != 0:
        raise RoomViewerError(
            created.stderr.strip() or "tmux new-session failed for the room viewer"
        )
    try:
        # Stamp ownership FIRST and require it to succeed: an unmarked session
        # would be unreclaimable by close/sweep and unrecognizable as ours. The
        # marker (the full project digest) is addressed via =temp: since
        # set-option rejects a bare =session; it survives the rename.
        stamped = _tmux("set-option", "-t", _window(temp), _OWNER_OPTION, _project_digest(project_id))
        if stamped.returncode != 0:
            raise RoomViewerError(
                stamped.stderr.strip() or "failed to stamp viewer ownership"
            )
        _tmux("set-option", "-t", _window(temp), "pane-border-status", "top")
        _tmux("set-option", "-t", _window(temp), "pane-border-format", " #{pane_title} ")
        first_pane = created.stdout.strip()
        if first_pane:
            _tmux("select-pane", "-t", first_pane, "-T", f"{first_member} (round {round_row['number']})")
        for member_id, path in logs[1:]:
            res = _tmux(
                "split-window", "-t", _window(temp), "-d", "-P", "-F", "#{pane_id}",
                "tail", "-n", "+1", "-F", path,
            )
            if res.returncode != 0:
                raise RoomViewerError(
                    res.stderr.strip() or f"failed to add a viewer pane for {member_id}"
                )
            pane_id = res.stdout.strip()
            if pane_id:
                _tmux("select-pane", "-t", pane_id, "-T", member_id)
            _tmux("select-layout", "-t", _window(temp), "tiled")
    except BaseException:
        _tmux("kill-session", "-t", _exact(temp))
        raise
    # Publish atomically: the final name appears only now, fully built.
    renamed = _tmux("rename-session", "-t", _exact(temp), name)
    if renamed.returncode != 0:
        # A concurrent creator won the race and already published the final name;
        # drop our complete-but-duplicate temp and accept the winner only if it
        # is actually a complete, owned viewer (never a squatting foreign name).
        _tmux("kill-session", "-t", _exact(temp))
        if not _is_complete_owned_viewer(name, project_id, len(members)):
            raise RoomViewerError(
                renamed.stderr.strip() or "failed to publish the viewer (name in use)"
            )
        return
    # A terminal transition (close/apply/prune) can race publication: it may have
    # swept the temp just before the rename, then found nothing. Re-read the
    # authoritative state and reclaim our just-published session if the room is
    # now terminal, so a closed room never keeps a live viewer.
    try:
        if store.get_room(room_id)["state"] in _TERMINAL_ROOM_STATES:
            _tmux("kill-session", "-t", _exact(name))
    except Exception:
        pass


def attach_viewer(name: str) -> int:
    """Attach to a viewer session, choosing the state machine by context.

    Returns a tmux exit code (0 on a clean external detach or a successful
    in-tmux switch). Not wrapped in any Textual suspend — the TUI caller wraps
    THIS in ``App.suspend`` for the external path; the CLI needs no wrapper.
    """
    if in_tmux():
        # Inside tmux: nesting an attach is refused, and switch does not block,
        # so there is no suspend/return dance. -E resets to the pane env.
        return _tmux("switch-client", "-E", "-t", _exact(name)).returncode
    # External: foreground attach that blocks until the user detaches.
    return _tmux_attach(_exact(name))


def in_tmux() -> bool:
    """Whether this process is running inside a tmux client (selects the state
    machine and the correct return hint to print)."""
    return bool(os.environ.get("TMUX"))


def close_room_viewers(store: RoomStore, project_id: str, room_id: str) -> int:
    """Kill every viewer session for a room's rounds. Best-effort, returns the
    count killed. Called from the room lifecycle (close/apply/prune); a missing
    tmux server is a no-op.

    Scoped to ``{project}-{room}-r``: project id + uuid4 room id are the
    ownership record, so this can only ever match this project's viewers of this
    exact room, never a foreign or cross-project session.
    """
    if not tmux_available():
        return 0
    prefix = _room_prefix(project_id, room_id)
    killed = 0
    for session in _list_project_viewers(project_id):
        if (
            session.startswith(prefix)
            and _is_owned_viewer(session, project_id)  # ownership proof, not just the name
            and _tmux("kill-session", "-t", _exact(session)).returncode == 0
        ):
            killed += 1
    return killed


def sweep_orphan_viewers(store: RoomStore, project_id: str) -> int:
    """Kill THIS project's viewer sessions whose room no longer exists or is
    terminal. Best-effort; returns the count reclaimed.

    Only sessions under this project's prefix are ever considered — the
    cross-project safety boundary — so a sweep in project B can never reclaim
    project A's live viewers, and never touches a foreign/user/monitor session.
    """
    if not tmux_available():
        return 0
    live = {room["id"]: room["state"] for room in store.list_rooms()}
    sessions = _list_project_viewers(project_id)
    present = set(sessions)
    killed = 0
    for session in sessions:
        reclaim = False
        final = _final_of_temp(session)
        if final is not None and final in present:
            # An abandoned build temp whose final session already published — a
            # SIGKILL/crash mid-build leaves persistent tail processes.
            reclaim = True
        else:
            room_id = _room_id_from_name(session, project_id)
            if room_id is None:
                continue  # unparseable under our own prefix → leave it alone
            state = live.get(room_id)
            reclaim = state is None or state in _TERMINAL_ROOM_STATES
        if reclaim and _is_owned_viewer(session, project_id) and _tmux(
            "kill-session", "-t", _exact(session)
        ).returncode == 0:
            killed += 1
    return killed


def _final_of_temp(name: str) -> str | None:
    """If ``name`` is a build temp (``…-mk{hex}``), the final name it was building
    toward; else ``None``. Used to reclaim a temp abandoned by a crashed build
    once its final session exists."""
    mk = name.rfind("-mk")
    if mk <= 0:
        return None
    suffix = name[mk + 3:]
    if suffix and all(c in "0123456789abcdef" for c in suffix):
        return name[:mk]
    return None


def _list_project_viewers(project_id: str) -> list[str]:
    """Live tmux session names under THIS project's viewer prefix (``[]`` with no
    server). The project prefix is the scope boundary every reclaim respects."""
    res = _tmux("list-sessions", "-F", "#{session_name}")
    if res.returncode != 0:
        return []
    prefix = _project_prefix(project_id)
    return [line.strip() for line in res.stdout.splitlines() if line.strip().startswith(prefix)]


def _room_id_from_name(name: str, project_id: str) -> str | None:
    """Recover the room_id from a viewer session name of THIS project, or ``None``.

    The name is ``{project_prefix}{room_id}-r{n}``, or an in-flight temp
    ``…-r{n}-mk{hex}``. The project prefix is KNOWN (passed in) and stripped
    exactly, so hyphens in a project id are never ambiguous. A ``-mk{hex}`` build
    suffix is stripped first so an abandoned temp (a SIGKILL mid-build) is still
    attributed to its room and reclaimable — then the trailing ``-r{digits}``
    round marker is stripped (uuid4s carry no such tail)."""
    prefix = _project_prefix(project_id)
    if not name.startswith(prefix):
        return None
    body = name[len(prefix):]
    mk = body.rfind("-mk")
    if mk > 0 and body[mk + 3:] and all(c in "0123456789abcdef" for c in body[mk + 3:]):
        body = body[:mk]  # an abandoned build temp → attribute to its room
    marker = body.rfind("-r")
    if marker <= 0 or not body[marker + 2:].isdigit():
        return None
    return body[:marker]
