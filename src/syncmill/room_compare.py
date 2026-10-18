"""Read-only comparison of room candidates (#172, PR 1).

One read model, :func:`build_room_comparison`, feeds every surface: the
``room compare`` text table, its ``--json`` form, and (later) the TUI table,
candidate Details and review-summary export. The model only reports what is
recorded and never upgrades an absence into a verdict:

- **The round is the comparison unit.** A room keeps one source fingerprint
  (a change makes the room stale) and ``run_round`` builds one gate per round,
  so the candidates of one round share their source and gate settings by
  construction. Round 1 always starts from a fresh snapshot; the lineage of
  later rounds is not persisted, so their starting tree reads ``not recorded``.
- **A missing gate report is not a pass, and missing usage is not zero.** A
  manifest is mutable display data, so its gate is read only from a recognized
  record and cross-checked against the authoritative SQLite ``eligible``
  column; a contradiction reads ``unknown`` instead of being believed.
- **Damage stays visible.** A candidate whose artifact fails its bindings or
  digest keeps its row (and an apply blocker); a member with no candidate is
  listed with whatever progress evidence exists.

Nothing here writes: the caller owns the open :class:`RoomStore`. A flat
top-level module; it imports ``rooms`` and ``artifacts`` and nothing imports it
back except the CLI.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from syncmill.artifacts import printable_name
from syncmill.models import TokenUsage
from syncmill.rooms import _SELECTABLE_ROOM_STATES, RoomError, RoomStore

SCHEMA_VERSION = 1
KIND = "syncmill.room-compare"

#: A patch above this size is flagged ``large`` (display only).
LARGE_PATCH_BYTES = 1024 * 1024

_ATTEMPT_STATES = frozenset({"completed", "failed", "timeout"})
_IN_PROGRESS_MEMBER_STATES = frozenset({"queued", "running"})

#: The same union the TUI room poll and ``room candidates`` treat as a damaged
#: record rather than a crash.
_ROW_ERRORS = (RoomError, KeyError, TypeError, AttributeError, OSError, ValueError)

SourceCheck = Callable[[], str]

_UNDECIDABLE_REASON = {
    "no_report": "no gate report recorded",
    "unknown": "no completed candidate with a usable record",
    "mixed": "gate records disagree within the round",
}


class UnknownRoundError(RoomError):
    """The requested round number does not exist in the room (a bad argument,
    not a damaged store)."""


@dataclass(frozen=True)
class GateCheckView:
    command: str
    passed: bool


@dataclass(frozen=True)
class GateView:
    #: passed | failed | not_run | no_report | unknown
    verdict: str
    reason: str | None = None
    checks: tuple[GateCheckView, ...] = ()

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(check.command for check in self.checks)


@dataclass(frozen=True)
class CandidateRow:
    candidate_id: str
    member_id: str
    provider_id: str
    #: None only on a damaged row whose stored attempt is malformed.
    attempt: int | None
    state: str
    eligible: bool
    selected: bool
    patch_sha256: str
    #: Display only: a per-snapshot synthetic commit that can differ between
    #: rounds with identical source, so never a comparability key.
    base_commit: str
    readable: bool
    unreadable_reason: str | None
    gate: GateView
    duration_seconds: float | None
    usage: TokenUsage | None
    #: None = the file set is unknown (unreadable or malformed), never "empty".
    files_changed: tuple[str, ...] | None
    patch_bytes: int | None
    patch_binary: bool | None
    patch_large: bool | None
    not_eligible_reason: str | None
    apply_blockers: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class MissingMember:
    member_id: str
    provider_id: str
    #: in_progress | no_candidate
    status: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class GateProfile:
    #: commands | no_report | unknown | mixed
    kind: str
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileOverlap:
    compared: tuple[str, ...]
    common: tuple[str, ...]
    only: dict[str, tuple[str, ...]]
    partial: dict[str, tuple[str, ...]]
    no_changes: tuple[str, ...]
    unknown: tuple[str, ...]


@dataclass(frozen=True)
class RoundGroup:
    number: int
    round_id: str
    state: str
    starting_tree: str
    gate_profile: GateProfile
    rows: tuple[CandidateRow, ...]
    missing: tuple[MissingMember, ...]
    overlap: FileOverlap
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RoomComparison:
    room_id: str
    room_state: str
    revision: int
    selected_candidate_id: str | None
    #: current | changed | unavailable | not_recorded
    source_status: str
    source_reason: str | None
    round_count: int
    groups: tuple[RoundGroup, ...]
    #: Only with ``all_rounds``: rounds grouped by an equal gate profile.
    cross_round: tuple[dict[str, Any], ...] = field(default=())
    #: Rounds whose own record could not be projected; always listed, in every
    #: mode, so damage is never presented as absence.
    damaged_rounds: tuple[dict[str, str], ...] = field(default=())


def _is_int(value: object) -> bool:
    return type(value) is int  # bool is an int subclass; True must not pass for 1


def _is_number(value: object) -> bool:
    return type(value) in (int, float)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
    except OverflowError:
        return None
    return float(value)


def _positive(value: object) -> int | None:
    count = _count(value)
    return count if count else None


def _attempt(value: object) -> int:
    attempt = _positive(value)
    if attempt is None:
        raise ValueError(f"malformed attempt {value!r}")
    return attempt


def _files(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return tuple(sorted(set(value)))


def _parse_gate(value: object) -> tuple[bool, GateView | None]:
    """``(recognized, view)``; ``view`` is None for an explicit ``null`` gate.

    Recognized means exactly the ``_gate_json`` shape ``RoomCandidateStore.save``
    writes: ``passed``/``score``/``checks``, each check carrying ``command``,
    ``passed``, ``exit_code``, ``output`` and ``duration_seconds``, and a
    ``passed`` flag that agrees with the checks (GateReport.passed is "every
    check passed").
    """
    if value is None:
        return True, None
    if (
        not isinstance(value, dict)
        or type(value.get("passed")) is not bool
        or not _is_number(value.get("score"))
        or not isinstance(value.get("checks"), list)
    ):
        return False, None
    parsed: list[GateCheckView] = []
    for check in value["checks"]:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("command"), str)
            or type(check.get("passed")) is not bool
            or not _is_int(check.get("exit_code"))
            or not isinstance(check.get("output"), str)
            or not _is_number(check.get("duration_seconds"))
        ):
            return False, None
        parsed.append(GateCheckView(check["command"], check["passed"]))
    if value["passed"] != all(check.passed for check in parsed):
        return False, None
    return True, GateView(
        "passed" if value["passed"] else "failed", checks=tuple(parsed)
    )


def _gate_verdict(
    *,
    state: str,
    eligible: bool,
    manifest: dict[str, Any] | None,
    patch_nonempty: bool | None,
) -> GateView:
    if state != "completed":
        return GateView("not_run", "agent did not complete")
    if manifest is None:
        return GateView("unknown", "artifact unreadable")
    version = manifest.get("schema_version")
    if not (_is_int(version) and version == 1) or manifest.get("state") != state:
        return GateView("unknown", "unrecognized record")
    # save() always writes the key; a missing one is not an explicit null.
    if "gate" not in manifest:
        return GateView("unknown", "unrecognized record")
    recognized, view = _parse_gate(manifest["gate"])
    if not recognized:
        return GateView("unknown", "unrecognized record")
    # eligible = completed AND non-empty patch AND (gate null OR gate passed)
    # (RoomCandidateStore.save). With a verified non-empty patch the stored
    # eligibility therefore pins the gate outcome; an empty patch pins nothing.
    if patch_nonempty:
        gate_ok = view is None or view.verdict == "passed"
        if gate_ok != eligible:
            return GateView("unknown", "record contradicts stored eligibility")
    return view if view is not None else GateView("no_report")


def _not_eligible_reason(
    *, state: str, patch_nonempty: bool | None, gate: GateView
) -> str:
    if state != "completed":
        return "agent did not complete"
    if patch_nonempty is False:
        return "no patch captured"
    if gate.verdict == "failed":
        return "quality gate failed"
    return "reason not determinable from the record"


def _project_row(
    store: RoomStore,
    candidate: dict[str, Any],
    *,
    selected_id: str | None,
    room_state: str,
    source_status: str,
    source_reason: str | None,
) -> CandidateRow:
    candidate_id = str(candidate["id"])
    state = str(candidate["state"])
    eligible = bool(candidate["eligible"])
    warnings: list[str] = []
    manifest: dict[str, Any] | None = None
    unreadable: str | None = None
    try:
        manifest = store.load_candidate_patch_head(candidate_id, 0)
        # Apply loads the whole artifact, so a missing or altered output file
        # blocks it too. Verify the output digest without keeping the body.
        store.load_candidate_checkpoint(candidate_id, 0)
    except _ROW_ERRORS as exc:
        manifest = None
        unreadable = str(exc) or type(exc).__name__

    patch_total = _count(manifest.get("patch_total_bytes")) if manifest else None
    patch_nonempty = None if patch_total is None else patch_total > 0
    gate = _gate_verdict(
        state=state, eligible=eligible, manifest=manifest, patch_nonempty=patch_nonempty
    )
    if state not in _ATTEMPT_STATES:
        warnings.append(f"unknown attempt state {state!r}")
    if eligible and (state != "completed" or patch_nonempty is False):
        warnings.append("stored eligibility contradicts the recorded attempt")

    files = duration = usage = binary = None
    if manifest is not None:
        files = _files(manifest.get("files_changed"))
        if files is None:
            warnings.append("files_changed is malformed; file set unknown")
        duration = _seconds(manifest.get("duration_seconds"))
        usage = TokenUsage.from_mapping(manifest.get("usage"))
        raw_binary = manifest.get("patch_binary")
        binary = raw_binary if isinstance(raw_binary, bool) else None

    blockers: list[str] = []
    if unreadable is not None:
        blockers.append(f"artifact unreadable: {unreadable}")
    not_eligible = None
    if not eligible:
        not_eligible = _not_eligible_reason(
            state=state, patch_nonempty=patch_nonempty, gate=gate
        )
        blockers.append(f"not eligible: {not_eligible}")
    blockers.extend(_room_blockers(room_state, source_status, source_reason))

    return CandidateRow(
        candidate_id=candidate_id,
        member_id=str(candidate["member_id"]),
        provider_id=str(candidate["provider_id"]),
        attempt=_attempt(candidate["attempt"]),
        state=state,
        eligible=eligible,
        selected=candidate_id == selected_id,
        patch_sha256=str(candidate["patch_sha256"]),
        base_commit=str(candidate["base_commit"]),
        readable=manifest is not None,
        unreadable_reason=unreadable,
        gate=gate,
        duration_seconds=duration,
        usage=usage,
        files_changed=files,
        patch_bytes=patch_total,
        patch_binary=binary,
        patch_large=None if patch_total is None else patch_total > LARGE_PATCH_BYTES,
        not_eligible_reason=not_eligible,
        apply_blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def _room_blockers(
    room_state: str, source_status: str, source_reason: str | None
) -> list[str]:
    blockers: list[str] = []
    if source_status == "changed":
        blockers.append("source changed since the room started")
    elif source_status == "unavailable":
        blockers.append(f"source check unavailable: {source_reason}")
    if room_state not in _SELECTABLE_ROOM_STATES:
        blockers.append(f"room is {room_state}")
    return blockers


def _damaged_row(
    candidate: dict[str, Any], reason: str, *, selected_id: str | None, room_blockers: list[str]
) -> CandidateRow:
    """A candidate whose own SQLite row could not be projected. Every field is
    taken defensively; the row stays in the comparison as unreadable."""
    candidate_id = str(candidate.get("id"))
    stored = candidate.get("eligible")
    warnings = () if stored in (0, 1) else ("stored eligibility is unreadable",)
    return CandidateRow(
        candidate_id=candidate_id,
        member_id=str(candidate.get("member_id")),
        provider_id=str(candidate.get("provider_id")),
        attempt=_positive(candidate.get("attempt")),
        state=str(candidate.get("state")),
        # Keep the stored verdict when it is readable; the unreadable blocker
        # below already prevents it from reading as ready.
        eligible=stored == 1 and not isinstance(stored, bool),
        selected=candidate_id == selected_id,
        patch_sha256=str(candidate.get("patch_sha256")),
        base_commit=str(candidate.get("base_commit")),
        readable=False,
        unreadable_reason=f"candidate record unreadable: {reason}",
        gate=GateView("unknown", "candidate record unreadable"),
        duration_seconds=None,
        usage=None,
        files_changed=None,
        patch_bytes=None,
        patch_binary=None,
        patch_large=None,
        not_eligible_reason=None,
        apply_blockers=(f"artifact unreadable: candidate record unreadable: {reason}", *room_blockers),
        warnings=warnings,
    )


def _gate_profile(rows: list[CandidateRow]) -> tuple[GateProfile, list[str]]:
    usable = [r for r in rows if r.state == "completed" and r.gate.verdict != "unknown"]
    if not usable:
        return GateProfile("unknown"), []
    gated = [r for r in usable if r.gate.verdict in {"passed", "failed"}]
    report_less = [r for r in usable if r.gate.verdict == "no_report"]
    command_sets = {r.gate.commands for r in gated}
    if (gated and report_less) or len(command_sets) > 1:
        # One gate is built per round, so this only arises from a damaged or
        # edited record.
        names = ", ".join(r.candidate_id for r in usable)
        return GateProfile("mixed"), [
            f"gate records disagree within the round (candidates: {names})"
        ]
    if gated:
        return GateProfile("commands", next(iter(command_sets))), []
    return GateProfile("no_report"), []


def _overlap(rows: list[CandidateRow]) -> FileOverlap:
    unknown = tuple(r.candidate_id for r in rows if r.files_changed is None)
    no_changes = tuple(r.candidate_id for r in rows if r.files_changed == ())
    # A known-empty file set is part of the denominator: a file is common to
    # all only if every candidate with a known set changed it.
    compared = [r for r in rows if r.files_changed is not None]
    ids = tuple(r.candidate_id for r in compared)
    owners: dict[str, list[str]] = {}
    for row in compared:
        for name in row.files_changed or ():
            owners.setdefault(name, []).append(row.candidate_id)
    if len(compared) < 2:
        common: tuple[str, ...] = ()
    else:
        common = tuple(sorted(n for n, o in owners.items() if len(o) == len(compared)))
    only = {
        row.candidate_id: tuple(
            sorted(n for n in row.files_changed or () if owners[n] == [row.candidate_id])
        )
        for row in compared
    }
    partial = {
        n: tuple(o)
        for n, o in sorted(owners.items())
        if 1 < len(o) < len(compared)
    }
    return FileOverlap(ids, common, only, partial, no_changes, unknown)


def _missing_members(
    store: RoomStore,
    room: dict[str, Any],
    round_row: dict[str, Any],
    rows: list[CandidateRow],
    now: float,
) -> tuple[list[MissingMember], list[str]]:
    warnings: list[str] = []
    members = room.get("members")
    expected: list[tuple[str, str]] = []
    if isinstance(members, list):
        for member in members:
            if isinstance(member, dict) and isinstance(member.get("member_id"), str):
                provider = member.get("provider_id")
                expected.append(
                    (member["member_id"], provider if isinstance(provider, str) else "?")
                )
            else:
                warnings.append("room member list holds a malformed entry")
    else:
        warnings.append("room member list is malformed")
    progress = {
        str(m["member_id"]): str(m["state"])
        for m in store.list_round_members(str(round_row["id"]))
    }
    have = {r.member_id for r in rows}
    lease = round_row.get("lease_expires")
    lease_live = isinstance(lease, (int, float)) and lease > now
    missing: list[MissingMember] = []
    for member_id, provider_id in expected:
        if member_id in have:
            continue
        member_state = progress.get(member_id)
        if (
            room.get("state") == "running"
            and round_row.get("state") == "running"
            and lease_live
            and (member_state is None or member_state in _IN_PROGRESS_MEMBER_STATES)
        ):
            missing.append(MissingMember(member_id, provider_id, "in_progress", ()))
            continue
        evidence = [f"member state: {member_state or 'not recorded'}"]
        if room.get("state") == "recovery_required":
            evidence.append("room needs recovery")
        if round_row.get("state") == "running" and not lease_live:
            evidence.append("lease expired")
        missing.append(
            MissingMember(member_id, provider_id, "no_candidate", tuple(evidence))
        )
    return missing, warnings


def build_room_comparison(
    store: RoomStore,
    room_id: str,
    *,
    round_number: int | None = None,
    all_rounds: bool = False,
    source_check: SourceCheck | None = None,
    now: float | None = None,
) -> RoomComparison:
    """Project one room's candidates into the comparison read model.

    ``source_check`` returns the live repository fingerprint; any exception it
    raises is reported as ``unavailable`` and never as ``current``. Raises
    :class:`RoomError` for an unknown room or round number.
    """
    if round_number is not None and all_rounds:
        raise ValueError("round_number and all_rounds are mutually exclusive")
    now = time.time() if now is None else now
    room = store.get_room(room_id)
    recorded = room.get("source_fingerprint")
    source_reason: str | None = None
    if not recorded:
        source_status = "not_recorded"
    elif source_check is None:
        source_status, source_reason = "unavailable", "not checked"
    else:
        try:
            source_status = "current" if source_check() == recorded else "changed"
        # Any failure (a non-zero git exit, git missing, an unreadable cwd)
        # means the source is unverified — never "current".
        except Exception as exc:
            source_status = "unavailable"
            source_reason = str(exc) or type(exc).__name__

    rounds = store.list_rounds(room_id)
    damaged: list[dict[str, str]] = []
    valid: list[dict[str, Any]] = []
    for round_row in rounds:
        if _positive(round_row.get("number")) is None:
            damaged.append(
                {
                    "round_id": str(round_row.get("id")),
                    "reason": f"malformed round number {round_row.get('number')!r}",
                }
            )
        else:
            valid.append(round_row)
    if round_number is not None:
        chosen = [r for r in valid if r["number"] == round_number]
        if not chosen:
            numbers = ", ".join(str(r["number"]) for r in valid) or "none"
            raise UnknownRoundError(
                f"room {room_id} has no round {round_number} (rounds: {numbers})"
            )
    elif all_rounds:
        chosen = valid
    else:
        chosen = valid[-1:]

    by_round: dict[str, list[dict[str, Any]]] = {}
    for candidate in store.list_candidates(room_id):
        by_round.setdefault(str(candidate["round_id"]), []).append(candidate)

    selected_id = room.get("selected_candidate_id")
    room_state = str(room["state"])
    room_blockers = _room_blockers(room_state, source_status, source_reason)
    groups: list[RoundGroup] = []
    for round_row in chosen:
        rows: list[CandidateRow] = []
        for candidate in by_round.get(str(round_row["id"]), []):
            try:
                rows.append(
                    _project_row(
                        store,
                        candidate,
                        selected_id=selected_id,
                        room_state=room_state,
                        source_status=source_status,
                        source_reason=source_reason,
                    )
                )
            except _ROW_ERRORS as exc:
                rows.append(
                    _damaged_row(
                        candidate,
                        str(exc) or type(exc).__name__,
                        selected_id=selected_id,
                        room_blockers=room_blockers,
                    )
                )
        try:
            profile, warnings = _gate_profile(rows)
            missing, member_warnings = _missing_members(
                store, room, round_row, rows, now
            )
            groups.append(
                RoundGroup(
                    number=int(round_row["number"]),
                    round_id=str(round_row["id"]),
                    state=str(round_row["state"]),
                    starting_tree=(
                        "snapshot" if round_row["number"] == 1 else "not_recorded"
                    ),
                    gate_profile=profile,
                    rows=tuple(rows),
                    missing=tuple(missing),
                    overlap=_overlap(rows),
                    warnings=tuple(warnings + member_warnings),
                )
            )
        except _ROW_ERRORS as exc:
            damaged.append(
                {
                    "round_id": str(round_row.get("id")),
                    "reason": str(exc) or type(exc).__name__,
                }
            )

    cross: list[dict[str, Any]] = []
    if all_rounds and len(groups) > 1:
        # Rounds are grouped by equality of their recorded gate commands (an
        # equivalence relation, so the groups carry the same information as
        # every pairwise answer). Only recorded commands establish equality: a
        # round with no gate report, an unknown or a mixed profile says nothing
        # about its settings and is listed on its own with the reason.
        by_commands: dict[tuple[str, ...], list[int]] = {}
        for group in groups:
            if group.gate_profile.kind == "commands":
                by_commands.setdefault(group.gate_profile.commands, []).append(
                    group.number
                )
        for commands, numbers in by_commands.items():
            cross.append(
                {
                    "rounds": numbers,
                    "gate_profile": "commands",
                    "commands": list(commands),
                    "reason": None,
                }
            )
        for group in groups:
            if group.gate_profile.kind != "commands":
                cross.append(
                    {
                        "rounds": [group.number],
                        "gate_profile": "undecidable",
                        "commands": [],
                        "reason": _UNDECIDABLE_REASON.get(
                            group.gate_profile.kind, group.gate_profile.kind
                        ),
                    }
                )

    return RoomComparison(
        room_id=room_id,
        room_state=str(room["state"]),
        revision=int(room["revision"]),
        selected_candidate_id=selected_id,
        source_status=source_status,
        source_reason=source_reason,
        round_count=len(rounds),
        groups=tuple(groups),
        cross_round=tuple(cross),
        damaged_rounds=tuple(damaged),
    )


# --------------------------------------------------------------------- JSON


def _usage_dict(usage: TokenUsage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "total_input_tokens": usage.total_input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "total_cost_usd": usage.total_cost_usd,
    }


def _row_dict(row: CandidateRow) -> dict[str, Any]:
    return {
        "candidate_id": row.candidate_id,
        "member_id": row.member_id,
        "provider_id": row.provider_id,
        "attempt": row.attempt,
        "state": row.state,
        "eligible": row.eligible,
        "not_eligible_reason": row.not_eligible_reason,
        "selected": row.selected,
        "base_commit": row.base_commit,
        "readable": row.readable,
        "unreadable_reason": row.unreadable_reason,
        "gate": {
            "verdict": row.gate.verdict,
            "reason": row.gate.reason,
            "checks": [
                {"command": c.command, "passed": c.passed} for c in row.gate.checks
            ],
        },
        "duration_seconds": row.duration_seconds,
        "usage": _usage_dict(row.usage),
        "files_changed": (
            None if row.files_changed is None else list(row.files_changed)
        ),
        "patch": {
            "sha256": row.patch_sha256,
            "bytes": row.patch_bytes,
            "binary": row.patch_binary,
            "large": row.patch_large,
        },
        "apply_blockers": list(row.apply_blockers),
        "warnings": list(row.warnings),
    }


def comparison_to_dict(cmp: RoomComparison) -> dict[str, Any]:
    """The versioned JSON form. Additive fields keep ``schema_version`` 1;
    removing, renaming or re-typing a field bumps it. Body-free: ids, patch
    digests, sizes, verdicts, file names and gate commands — never prompt,
    output, patch or gate-check output text."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "room": {
            "room_id": cmp.room_id,
            "state": cmp.room_state,
            "revision": cmp.revision,
            "selected_candidate_id": cmp.selected_candidate_id,
            "round_count": cmp.round_count,
        },
        "source": {"status": cmp.source_status, "reason": cmp.source_reason},
        "rounds": [
            {
                "number": g.number,
                "round_id": g.round_id,
                "state": g.state,
                "starting_tree": g.starting_tree,
                "gate_profile": {
                    "kind": g.gate_profile.kind,
                    "commands": list(g.gate_profile.commands),
                },
                "candidates": [_row_dict(r) for r in g.rows],
                "missing_members": [
                    {
                        "member_id": m.member_id,
                        "provider_id": m.provider_id,
                        "status": m.status,
                        "evidence": list(m.evidence),
                    }
                    for m in g.missing
                ],
                "files": {
                    "compared": list(g.overlap.compared),
                    "common": list(g.overlap.common),
                    "only": {k: list(v) for k, v in g.overlap.only.items()},
                    "partial": {k: list(v) for k, v in g.overlap.partial.items()},
                    "no_changes": list(g.overlap.no_changes),
                    "unknown": list(g.overlap.unknown),
                },
                "warnings": list(g.warnings),
            }
            for g in cmp.groups
        ],
        "cross_round": [dict(entry) for entry in cmp.cross_round],
        "damaged_rounds": [dict(entry) for entry in cmp.damaged_rounds],
    }


# --------------------------------------------------------------------- text


def _p(value: object) -> str:
    """Every stored string is untrusted display data: escape controls and line
    breaks so nothing can drive the terminal or forge a line."""
    return printable_name(str(value))


_GATE_TEXT = {
    "passed": "passed",
    "failed": "failed",
    "not_run": "not run",
    "no_report": "no gate report",
    "unknown": "unknown",
}

_SOURCE_TEXT = {
    "current": "unchanged since the room started",
    "changed": "CHANGED since the room started — apply would be rejected",
    "not_recorded": "not recorded (no round has run)",
}


def _usage_text(usage: TokenUsage | None) -> str:
    if usage is None:
        return "not reported"
    parts = []
    if usage.total_input_tokens is not None:
        parts.append(f"input {usage.total_input_tokens:,}")
    if usage.output_tokens is not None:
        parts.append(f"output {usage.output_tokens:,}")
    if usage.cache_read_tokens is not None:
        parts.append(f"cache read {usage.cache_read_tokens:,}")
    if usage.cache_creation_tokens is not None:
        parts.append(f"cache write {usage.cache_creation_tokens:,}")
    if usage.total_cost_usd is not None:
        parts.append(f"cost ${usage.total_cost_usd:.4f}")
    return ", ".join(parts) if parts else "not reported"


def _row_lines(row: CandidateRow) -> list[str]:
    attempt = "?" if row.attempt is None else row.attempt
    head = f"  {_p(row.candidate_id)}  {_p(row.member_id)} ({_p(row.provider_id)}) #{attempt}  {_p(row.state)}"
    if row.selected:
        head += "  [selected (human)]"
    lines = [head]
    if not row.readable:
        lines.append(f"    unreadable: {_p(row.unreadable_reason)}")
    gate = _GATE_TEXT.get(row.gate.verdict, row.gate.verdict)
    if row.gate.reason:
        gate += f" ({_p(row.gate.reason)})"
    lines.append(f"    gate: {gate}")
    for check in row.gate.checks:
        lines.append(f"      {'✓' if check.passed else '×'} {_p(check.command)}")
    duration = (
        "not recorded" if row.duration_seconds is None else f"{row.duration_seconds:.1f}s"
    )
    lines.append(f"    duration: {duration} | usage: {_usage_text(row.usage)}")
    if row.patch_bytes is None:
        patch = "unknown"
    else:
        patch = f"{row.patch_bytes:,} bytes"
        if row.patch_binary:
            patch += ", binary"
        if row.patch_large:
            patch += ", large"
    files = (
        "unknown" if row.files_changed is None else str(len(row.files_changed))
    )
    lines.append(f"    patch: {patch} | files: {files} | base: {_p(row.base_commit)}")
    if row.eligible:
        lines.append("    eligible: yes")
    elif row.not_eligible_reason:
        lines.append(f"    eligible: no ({_p(row.not_eligible_reason)})")
    else:
        lines.append("    eligible: no")
    if row.apply_blockers:
        lines.append("    apply blockers:")
        lines.extend(f"      - {_p(b)}" for b in row.apply_blockers)
    else:
        lines.append(
            "    apply blockers: none known (select/apply still revalidate)"
        )
    for warning in row.warnings:
        lines.append(f"    warning: {_p(warning)}")
    if row.readable:
        lines.append(f"    diff: syncmill room diff {_p(row.candidate_id)}")
    return lines


def _profile_text(profile: GateProfile) -> str:
    if profile.kind == "commands":
        return "commands: " + "; ".join(_p(c) for c in profile.commands)
    return {
        "no_report": "no gate report on any completed candidate",
        "unknown": "unknown (no completed candidate with a usable record)",
        "mixed": "mixed (records disagree — see warnings)",
    }.get(profile.kind, profile.kind)


def _overlap_lines(overlap: FileOverlap) -> list[str]:
    lines = ["  files:"]
    if len(overlap.compared) >= 2:
        lines.append(
            f"    common to all ({len(overlap.common)}): "
            + (", ".join(_p(n) for n in overlap.common) or "-")
        )
    for candidate_id, names in overlap.only.items():
        lines.append(
            f"    only {_p(candidate_id)} ({len(names)}): "
            + (", ".join(_p(n) for n in names) or "-")
        )
    if overlap.partial:
        lines.append("    changed by some, not all:")
        for name, owners in overlap.partial.items():
            lines.append(f"      {_p(name)}: {', '.join(_p(o) for o in owners)}")
    if overlap.no_changes:
        lines.append(
            "    no files changed: " + ", ".join(_p(c) for c in overlap.no_changes)
        )
    if overlap.unknown:
        lines.append(
            "    file set unknown: " + ", ".join(_p(c) for c in overlap.unknown)
        )
    return lines


def render_room_comparison(cmp: RoomComparison) -> str:
    source = _SOURCE_TEXT.get(cmp.source_status)
    if source is None:
        source = f"check unavailable ({_p(cmp.source_reason)})"
    lines = [
        f"Room {_p(cmp.room_id)}  state: {_p(cmp.room_state)}  revision: {cmp.revision}",
        f"Source: {source}",
        "Selected: "
        + (
            f"{_p(cmp.selected_candidate_id)} (human)"
            if cmp.selected_candidate_id
            else "none"
        ),
    ]
    for entry in cmp.damaged_rounds:
        lines.append(
            f"Damaged round {_p(entry['round_id'])}: {_p(entry['reason'])}"
        )
    if not cmp.groups:
        lines.append("")
        lines.append(
            "No readable rounds in this room."
            if cmp.damaged_rounds
            else "No rounds have run in this room yet."
        )
        return "\n".join(lines)
    for group in cmp.groups:
        tree = (
            "snapshot"
            if group.starting_tree == "snapshot"
            else "not recorded (round lineage is not persisted)"
        )
        lines.extend(
            [
                "",
                f"Round {group.number} of {cmp.round_count} ({_p(group.state)}) — starting tree: {tree}",
                f"  gate profile: {_profile_text(group.gate_profile)}"
                + ("; timeout not recorded" if group.gate_profile.kind == "commands" else ""),
            ]
        )
        if not group.rows and not group.missing:
            lines.append("  (no candidates)")
        for row in group.rows:
            lines.extend(_row_lines(row))
        for member in group.missing:
            if member.status == "in_progress":
                lines.append(f"  {_p(member.member_id)} ({_p(member.provider_id)}): in progress")
            else:
                lines.append(
                    f"  {_p(member.member_id)} ({_p(member.provider_id)}): no candidate retained ("
                    + "; ".join(_p(e) for e in member.evidence)
                    + ")"
                )
        if group.rows:
            lines.extend(_overlap_lines(group.overlap))
        for warning in group.warnings:
            lines.append(f"  warning: {_p(warning)}")
    if cmp.cross_round:
        lines.extend(["", "Across rounds (gate settings; timeout not recorded):"])
        for entry in cmp.cross_round:
            rounds = ", ".join(str(n) for n in entry["rounds"])
            if entry["gate_profile"] == "undecidable":
                lines.append(
                    f"  round {rounds}: cannot establish equality ({_p(entry['reason'])})"
                )
            else:
                lines.append(
                    f"  rounds {rounds}: same commands: "
                    + "; ".join(_p(c) for c in entry["commands"])
                )
        lines.append(
            "  starting tree: only round 1 is known (snapshot); later rounds' lineage is not recorded"
        )
    return "\n".join(lines)
