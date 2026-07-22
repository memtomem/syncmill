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
from typing import TYPE_CHECKING

from syncmill.monitor import _tmux, _tmux_attach, tmux_available

if TYPE_CHECKING:  # a type hint only — importing at runtime would make rooms.py,
    # which drives teardown from the room lifecycle, import a module that imports
    # it back. Keeping it type-only makes the runtime graph rooms → room_viewer.
    from syncmill.rooms import RoomStore

_VIEWER_PREFIX = "syncmill-room-"

#: Terminal room states whose viewers a sweep may reclaim.
_TERMINAL_ROOM_STATES = {"applied", "closed", "stale"}


class RoomViewerError(RuntimeError):
    """A room viewer could not be opened. Like ``SessionLaunchError``, this has no
    fallback — the viewer IS the tmux feature — so the CLI surfaces it directly."""


def _safe(value: str) -> str:
    """A tmux-name-safe form: tmux session names cannot contain ``.``/``:`` and
    a stray separator would break scoping, so map anything but ``[A-Za-z0-9-_]``
    to ``_``. Room ids are uuid4 (already safe); this only guards project ids,
    which can be arbitrary configured strings."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)


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
    return f"{_VIEWER_PREFIX}{_safe(project_id)}-"


def viewer_session_name(project_id: str, room_id: str, round_number: int) -> str:
    """The deterministic tmux session name for a room round — the reattach key.

    Carries the project id so one host's shared tmux server can host viewers for
    many projects without a sweep in one reclaiming another's live sessions.
    """
    return f"{_project_prefix(project_id)}{_safe(room_id)}-r{round_number}"


def _room_prefix(project_id: str, room_id: str) -> str:
    """The prefix scoping a per-room kill to exactly this room's viewers. The
    trailing ``-r`` (round marker) prevents matching a room whose id is a prefix
    of another — room ids are uuid4, so this is exact to the room."""
    return f"{_project_prefix(project_id)}{_safe(room_id)}-r"


def _session_exists(name: str) -> bool:
    return _tmux("has-session", "-t", _exact(name)).returncode == 0


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
    store.get_room(room_id)  # raises RoomError for an unknown room
    round_row = _resolve_round(store, room_id, round_number)
    members = store.list_round_members(round_row["id"])
    if not members:
        raise RoomViewerError(
            f"round {round_row['number']} has no monitor data "
            "(it predates per-member progress); terminal unavailable"
        )
    name = viewer_session_name(project_id, room_id, int(round_row["number"]))
    if _session_exists(name):
        return name
    _create_viewer(store, name, room_id, round_row, members)
    return name


def _resolve_member_log(
    store: RoomStore, room_id: str, round_id: str, member: dict
) -> str | None:
    """Fail-closed absolute log path for a member, or ``None`` to skip it.

    The path is RECOMPUTED from authoritative room/round/member ids (never the
    stored string is trusted), required to equal the stored ``log_path``,
    resolved beneath ``candidates.root``, and rejected if it is a symlink or a
    non-regular file — so an absolute/``..``/symlinked ``log_path`` in the DB can
    never make the viewer tail a file outside the room's own tree.
    """
    stored = member.get("log_path")
    canonical = store.round_log_relpath(room_id, round_id, member["member_id"])
    if stored != canonical:
        return None  # drift from the authoritative path → do not follow it
    root = store.candidates.root.resolve()
    resolved = (root / canonical).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None  # escaped the artifact root
    if resolved.is_symlink() or (resolved.exists() and not resolved.is_file()):
        return None
    return str(resolved)


def _create_viewer(
    store: RoomStore, name: str, room_id: str, round_row: dict, members: list[dict]
) -> None:
    """Create the detached read-only session ATOMICALLY: one ``tail -F`` pane per
    member. If any pane after the first fails, the partial session is killed and
    the create raises — a viewer is never left showing only some members."""
    logs = [
        (m["member_id"], _resolve_member_log(store, room_id, round_row["id"], m))
        for m in members
    ]
    logs = [(mid, path) for mid, path in logs if path]
    if not logs:
        raise RoomViewerError("no valid member logs to follow for this round")
    # Seed each log so `tail -F` opens cleanly even before the first write.
    for _, path in logs:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a"):
                pass
        except OSError:
            pass  # best-effort; tail -F tolerates a not-yet-existent file
    first_member, first_log = logs[0]
    created = _tmux(
        "new-session", "-d", "-s", name, "-P", "-F", "#{pane_id}",
        "tail", "-n", "+1", "-F", first_log,
    )
    if created.returncode != 0:
        # Race: a concurrent attach may have created it between the has-session
        # check and here. Accept the winner rather than failing.
        if _session_exists(name):
            return
        raise RoomViewerError(
            created.stderr.strip() or "tmux new-session failed for the room viewer"
        )
    # Pane/window cosmetics are best-effort; a pane title/border failure must not
    # tear down a working viewer.
    _tmux("set-option", "-t", _window(name), "pane-border-status", "top")
    _tmux("set-option", "-t", _window(name), "pane-border-format", " #{pane_title} ")
    first_pane = created.stdout.strip()
    if first_pane:
        _tmux("select-pane", "-t", first_pane, "-T", f"{first_member} (round {round_row['number']})")
    for member_id, path in logs[1:]:
        res = _tmux(
            "split-window", "-t", _window(name), "-d", "-P", "-F", "#{pane_id}",
            "tail", "-n", "+1", "-F", path,
        )
        if res.returncode != 0:
            # Atomic: a partial viewer that silently drops a member is worse than
            # no viewer. Tear it down; the next attach recreates it cleanly.
            _tmux("kill-session", "-t", _exact(name))
            raise RoomViewerError(
                res.stderr.strip() or f"failed to add a viewer pane for {member_id}"
            )
        pane_id = res.stdout.strip()
        if pane_id:
            _tmux("select-pane", "-t", pane_id, "-T", member_id)
        _tmux("select-layout", "-t", _window(name), "tiled")


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
        if session.startswith(prefix) and _tmux(
            "kill-session", "-t", _exact(session)
        ).returncode == 0:
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
    killed = 0
    for session in _list_project_viewers(project_id):
        room_id = _room_id_from_name(session, project_id)
        if room_id is None:
            continue  # unparseable under our own prefix → leave it alone
        state = live.get(room_id)
        if state is None or state in _TERMINAL_ROOM_STATES:
            if _tmux("kill-session", "-t", _exact(session)).returncode == 0:
                killed += 1
    return killed


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

    The name is ``{project_prefix}{room_id}-r{n}``; the project prefix is KNOWN
    (passed in) and stripped exactly, so the ambiguity of hyphens in a project id
    never arises. The remainder is ``{room_id}-r{digits}`` — strip the trailing
    round marker to get the room id (uuid4s carry no ``-r<digits>`` tail)."""
    prefix = _project_prefix(project_id)
    if not name.startswith(prefix):
        return None
    body = name[len(prefix):]
    marker = body.rfind("-r")
    if marker <= 0 or not body[marker + 2:].isdigit():
        return None
    return body[:marker]
