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
from dataclasses import dataclass, field, replace
from typing import Any

from syncmill.artifacts import printable_name
from syncmill.models import TokenUsage
from syncmill.rooms import (
    _ROOM_CANDIDATE_CONTEXT_MAX_BYTES,
    _SELECTABLE_ROOM_STATES,
    ProvenanceCheck,
    RoomError,
    RoomStore,
    SelectionCheck,
    round_conversation_status,
    round_provenance_status,
    selection_record_status,
)

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
    exit_code: int | None = None
    duration_seconds: float | None = None


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
    # Display metadata for the candidate Details view. Identifiers and numbers
    # reach the JSON; ``error`` does not (it can quote agent output).
    created_at: str | None = None
    output_sha256: str | None = None
    exit_code: int | None = None
    output_bytes: int | None = None
    output_truncated: bool | None = None
    provider_session_id: str | None = None
    error: str | None = None


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
class LineageView:
    """A round's recorded effective starting tree (``rounds.provenance_json``).

    Values are shown only when the record is ``recorded``: a malformed record
    is reported by its reason (a round warning), never believed."""

    #: recorded | not_recorded | malformed
    status: str
    reason: str | None = None
    #: the lineage the user asked for: continue-own | fresh | branch-from
    requested: str | None = None
    #: snapshot | branch | continued
    tree: str | None = None
    base_candidate_id: str | None = None
    base_patch_sha256: str | None = None
    base_round_number: int | None = None
    #: continued only: member id -> (parent candidate id, parent round), or
    #: None when the previous round held no single candidate for the member.
    parents: dict[str, tuple[str, int] | None] = field(default_factory=dict)


@dataclass(frozen=True)
class GateSettingsView:
    """The gate settings a round recorded, kept apart from the gate REPORTS
    (what actually ran) and cross-checked against them."""

    #: recorded | not_recorded | malformed
    status: str
    #: commands | none (recorded only)
    kind: str | None = None
    commands: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    #: consistent | contradicted | indeterminate | n/a
    consistency: str = "n/a"


@dataclass(frozen=True)
class MemberConversation:
    #: cold | resume | fork — what the provider was handed, not what it did
    session: str
    #: full | delta
    context: str
    since_round: int | None
    #: candidate ids whose output excerpts entered the prompt, oldest first
    candidates: tuple[str, ...]
    #: older eligible candidates were left out for the prompt size limit
    omitted: bool
    #: included | dropped | none
    memory: str


@dataclass(frozen=True)
class ConversationView:
    """A round's recorded per-member conversations
    (``rounds.conversation_json``); values only when ``recorded``."""

    #: recorded | not_recorded | malformed
    status: str
    reason: str | None = None
    members: dict[str, MemberConversation] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundGroup:
    number: int
    round_id: str
    state: str
    #: Report-era v1 field, unchanged: ``snapshot`` for round 1, else
    #: ``not_recorded``. The recorded tree is :attr:`lineage`.
    starting_tree: str
    #: Report-derived (what the candidates' gate reports show); the recorded
    #: configuration is :attr:`gate_settings`.
    gate_profile: GateProfile
    rows: tuple[CandidateRow, ...]
    missing: tuple[MissingMember, ...]
    overlap: FileOverlap
    warnings: tuple[str, ...]
    lineage: LineageView = field(default_factory=lambda: LineageView("not_recorded"))
    gate_settings: GateSettingsView = field(
        default_factory=lambda: GateSettingsView("not_recorded")
    )
    conversation: ConversationView = field(
        default_factory=lambda: ConversationView("not_recorded")
    )


@dataclass(frozen=True)
class SelectionView:
    """The room's current human selection and how its record holds up.

    Record values are shown only when the record is ``bound``: a mismatched
    record is reported by its reason, never believed."""

    #: bound | mismatch | not_recorded
    status: str
    candidate_id: str | None
    reason: str | None = None
    patch_sha256: str | None = None
    confirmed_revision: int | None = None
    revision: int | None = None
    rationale: str | None = None
    surface: str | None = None
    selected_at: str | None = None


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
    #: Only with ``all_rounds``: rounds grouped by equal RECORDED gate settings
    #: (commands and timeout); rounds without a usable record stand alone.
    cross_round_settings: tuple[dict[str, Any], ...] = field(default=())
    #: Rounds whose own record could not be projected; always listed, in every
    #: mode, so damage is never presented as absence.
    damaged_rounds: tuple[dict[str, str], ...] = field(default=())
    #: None when nothing is selected and no selection record exists.
    selection: SelectionView | None = None


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
        parsed.append(
            GateCheckView(
                check["command"],
                check["passed"],
                exit_code=check["exit_code"],
                duration_seconds=_seconds(check["duration_seconds"]),
            )
        )
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


#: The manifest fields ``RoomCoordinator.apply`` converts when it builds the
#: run artifact, with the exact conversion it applies.
_APPLY_CONSUMED_FIELDS = (
    ("exit_code", int),
    ("duration_seconds", float),
    ("files_changed", list),
)


def _apply_unusable_fields(manifest: dict[str, Any]) -> list[str]:
    """Fields apply would fail to convert. Mirrors apply's own conversions
    rather than this module's stricter display validation, so a blocker is
    reported exactly when apply would raise."""
    bad: list[str] = []
    for key, convert in _APPLY_CONSUMED_FIELDS:
        try:
            convert(manifest[key])
        except (KeyError, TypeError, ValueError, OverflowError):
            bad.append(key)
    return bad


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
    exit_code = output_bytes = truncated = session = error = None
    if manifest is not None:
        raw_exit = manifest.get("exit_code")
        exit_code = raw_exit if _is_int(raw_exit) else None
        output_bytes = _count(manifest.get("output_bytes"))
        raw_truncated = manifest.get("output_truncated")
        truncated = raw_truncated if type(raw_truncated) is bool else None
        session = _str(manifest.get("provider_session_id"))
        error = _str(manifest.get("error"))
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
    elif manifest is not None and (unusable := _apply_unusable_fields(manifest)):
        blockers.append(
            "artifact metadata apply cannot use: " + ", ".join(unusable)
        )
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
        created_at=_str(candidate.get("created_at")),
        output_sha256=_str(candidate.get("output_sha256")),
        exit_code=exit_code,
        output_bytes=output_bytes,
        output_truncated=truncated,
        provider_session_id=session,
        error=error,
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


def _selection_view(selected_id: object, check: SelectionCheck) -> SelectionView:
    candidate_id = selected_id if isinstance(selected_id, str) else None
    record = check.record
    if check.status != "bound" or record is None:
        return SelectionView(check.status, candidate_id, check.reason)
    return SelectionView(
        "bound",
        candidate_id,
        patch_sha256=record["patch_sha256"],
        confirmed_revision=record["confirmed_revision"],
        revision=record["revision"],
        rationale=record["rationale"],
        surface=record["surface"],
        selected_at=record["selected_at"],
    )


def _selection_blockers(room: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Apply refuses an inconsistent selection record; say so on the selected
    row (the same validator apply runs, on the same candidate row)."""
    selected_id = room.get("selected_candidate_id")
    if not selected_id or candidate.get("id") != selected_id:
        return []
    check = selection_record_status(
        room.get("selection_json"),
        selected_candidate_id=selected_id,
        candidate_row=candidate,
    )
    if check.status != "mismatch":
        return []
    return [f"selection record inconsistent: {check.reason}; select it again"]


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


def project_candidate(
    store: RoomStore,
    candidate: dict[str, Any],
    *,
    room: dict[str, Any],
    source_status: str,
    source_reason: str | None,
) -> CandidateRow:
    """One candidate as a :class:`CandidateRow` — the single projection the
    CLI, the TUI Compare view and candidate Details all read. Never raises for
    damage inside the candidate's own record: that becomes an unreadable row."""
    room_state = str(room["state"])
    selected_id = room.get("selected_candidate_id")
    try:
        row = _project_row(
            store,
            candidate,
            selected_id=selected_id,
            room_state=room_state,
            source_status=source_status,
            source_reason=source_reason,
        )
    except _ROW_ERRORS as exc:
        row = _damaged_row(
            candidate,
            str(exc) or type(exc).__name__,
            selected_id=selected_id,
            room_blockers=_room_blockers(room_state, source_status, source_reason),
        )
    if blockers := _selection_blockers(room, candidate):
        row = replace(row, apply_blockers=(*row.apply_blockers, *blockers))
    return row


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


def _provenance_views(
    check: ProvenanceCheck, rows: list[CandidateRow]
) -> tuple[LineageView, GateSettingsView, list[str]]:
    if check.status != "recorded" or check.record is None:
        warnings = (
            [f"round provenance record unreadable: {check.reason}"]
            if check.status == "malformed"
            else []
        )
        return (
            LineageView(check.status, reason=check.reason),
            GateSettingsView(check.status),
            warnings,
        )
    record = check.record
    tree = record["tree"]
    parents: dict[str, tuple[str, int] | None] = {}
    if tree["kind"] == "continued":
        parents = {
            member_id: (
                None
                if parent is None
                else (parent["parent_candidate_id"], parent["parent_round_number"])
            )
            for member_id, parent in tree["members"].items()
        }
    lineage = LineageView(
        "recorded",
        requested=record["requested_lineage"],
        tree=tree["kind"],
        base_candidate_id=tree.get("base_candidate_id"),
        base_patch_sha256=tree.get("base_patch_sha256"),
        base_round_number=tree.get("base_round_number"),
        parents=parents,
    )
    gate = record["gate"]
    commands = tuple(gate.get("commands", ()))
    consistency, warnings = _gate_consistency(gate["kind"], commands, rows)
    settings = GateSettingsView(
        "recorded",
        kind=gate["kind"],
        commands=commands,
        timeout_seconds=gate.get("timeout_seconds"),
        consistency=consistency,
    )
    return lineage, settings, warnings


def _conversation_view(check: ProvenanceCheck) -> tuple[ConversationView, list[str]]:
    if check.status != "recorded" or check.record is None:
        warnings = (
            [f"round conversation record unreadable: {check.reason}"]
            if check.status == "malformed"
            else []
        )
        return ConversationView(check.status, reason=check.reason), warnings
    return (
        ConversationView(
            "recorded",
            members={
                member_id: MemberConversation(
                    session=entry["session"],
                    context=entry["context"],
                    since_round=entry["since_round"],
                    candidates=tuple(entry["candidates"]),
                    omitted=entry["omitted"],
                    memory=entry["memory"],
                )
                for member_id, entry in check.record["members"].items()
            },
        ),
        [],
    )


def _gate_consistency(
    kind: str, commands: tuple[str, ...], rows: list[CandidateRow]
) -> tuple[str, list[str]]:
    """Cross-check a round's recorded gate settings against what its
    completed candidates' reports show. A report that cannot be read makes the
    result ``indeterminate`` — never folded into ``consistent``."""
    contradicted: list[str] = []
    indeterminate: list[str] = []
    for row in rows:
        if row.state != "completed":
            continue
        verdict = row.gate.verdict
        if verdict == "unknown":
            indeterminate.append(row.candidate_id)
        elif kind == "commands" and (
            verdict == "no_report" or row.gate.commands != commands
        ):
            contradicted.append(row.candidate_id)
        elif kind == "none" and verdict in {"passed", "failed"}:
            contradicted.append(row.candidate_id)
    warnings = []
    if contradicted:
        warnings.append(
            "gate reports contradict the recorded gate settings (candidates: "
            + ", ".join(contradicted)
            + ")"
        )
    if indeterminate:
        warnings.append(
            "gate reports could not be checked against the recorded gate settings "
            "(candidates: " + ", ".join(indeterminate) + ")"
        )
    if contradicted:
        return "contradicted", warnings
    if indeterminate:
        return "indeterminate", warnings
    return "consistent", warnings


def _cross_round_settings(groups: list[RoundGroup]) -> list[dict[str, Any]]:
    """Rounds grouped by equal recorded gate settings. Equality is
    established only by two records; a round without a usable one is listed
    alone, never grouped with a recorded round."""
    keyed: dict[tuple[Any, ...], list[RoundGroup]] = {}
    alone: list[dict[str, Any]] = []
    for group in groups:
        settings = group.gate_settings
        if settings.status != "recorded":
            alone.append(
                {
                    "rounds": [group.number],
                    "settings": "undecidable",
                    "commands": [],
                    "timeout_seconds": None,
                    "reason": (
                        "gate settings record unreadable"
                        if settings.status == "malformed"
                        else "gate settings not recorded"
                    ),
                    "contradicted_rounds": [],
                    "indeterminate_rounds": [],
                }
            )
            continue
        key = (settings.kind, settings.commands, settings.timeout_seconds)
        keyed.setdefault(key, []).append(group)
    entries = [
        {
            "rounds": [g.number for g in members],
            "settings": kind,
            "commands": list(commands),
            "timeout_seconds": timeout,
            "reason": None,
            "contradicted_rounds": [
                g.number for g in members if g.gate_settings.consistency == "contradicted"
            ],
            "indeterminate_rounds": [
                g.number for g in members if g.gate_settings.consistency == "indeterminate"
            ],
        }
        for (kind, commands, timeout), members in keyed.items()
    ]
    return entries + alone


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
    candidate_rows: dict[str, dict[str, Any]] = {}
    for candidate in store.list_candidates(room_id):
        by_round.setdefault(str(candidate["round_id"]), []).append(candidate)
        candidate_rows[str(candidate["id"])] = candidate
    members = room.get("members")
    member_ids = (
        [m["member_id"] for m in members if isinstance(m, dict) and "member_id" in m]
        if isinstance(members, list)
        else []
    )

    selected_id = room.get("selected_candidate_id")
    selection: SelectionView | None = None
    if selected_id:
        try:
            selected_row: dict[str, Any] | None = store.get_candidate(selected_id)
        except RoomError:
            selected_row = None
        selection = _selection_view(
            selected_id,
            selection_record_status(
                room.get("selection_json"),
                selected_candidate_id=selected_id,
                candidate_row=selected_row,
            ),
        )
    elif room.get("selection_json") is not None:
        selection = SelectionView(
            "mismatch", None, "a selection record exists but no candidate is selected"
        )
    groups: list[RoundGroup] = []
    for round_row in chosen:
        rows = [
            project_candidate(
                store,
                candidate,
                room=room,
                source_status=source_status,
                source_reason=source_reason,
            )
            for candidate in by_round.get(str(round_row["id"]), [])
        ]
        try:
            profile, warnings = _gate_profile(rows)
            lineage, settings, provenance_warnings = _provenance_views(
                round_provenance_status(
                    round_row.get("provenance_json"),
                    round_number=int(round_row["number"]),
                    member_ids=member_ids,
                    candidates=candidate_rows,
                ),
                rows,
            )
            conversation, conversation_warnings = _conversation_view(
                round_conversation_status(
                    round_row.get("conversation_json"),
                    round_number=int(round_row["number"]),
                    members=members if isinstance(members, list) else [],
                    candidates=candidate_rows,
                )
            )
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
                    warnings=tuple(
                        warnings + provenance_warnings + conversation_warnings + member_warnings
                    ),
                    lineage=lineage,
                    gate_settings=settings,
                    conversation=conversation,
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
    cross_settings: list[dict[str, Any]] = []
    if all_rounds and len(groups) > 1:
        cross_settings = _cross_round_settings(groups)
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
        cross_round_settings=tuple(cross_settings),
        damaged_rounds=tuple(damaged),
        selection=selection,
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
                {
                    "command": c.command,
                    "passed": c.passed,
                    "exit_code": c.exit_code,
                    "duration_seconds": c.duration_seconds,
                }
                for c in row.gate.checks
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
        "created_at": row.created_at,
        "exit_code": row.exit_code,
        "output": {
            "sha256": row.output_sha256,
            "bytes": row.output_bytes,
            "truncated": row.output_truncated,
        },
        "provider_session_id": row.provider_session_id,
    }


def _selection_dict(selection: SelectionView | None) -> dict[str, Any] | None:
    if selection is None:
        return None
    return {
        "status": selection.status,
        "candidate_id": selection.candidate_id,
        "reason": selection.reason,
        "patch_sha256": selection.patch_sha256,
        "confirmed_revision": selection.confirmed_revision,
        "revision": selection.revision,
        "rationale": selection.rationale,
        "surface": selection.surface,
        "selected_at": selection.selected_at,
    }


def _lineage_dict(lineage: LineageView) -> dict[str, Any]:
    return {
        "status": lineage.status,
        "reason": lineage.reason,
        "requested": lineage.requested,
        "tree": lineage.tree,
        "base_candidate_id": lineage.base_candidate_id,
        "base_patch_sha256": lineage.base_patch_sha256,
        "base_round_number": lineage.base_round_number,
        "members": {
            member_id: (
                None
                if parent is None
                else {"parent_candidate_id": parent[0], "parent_round_number": parent[1]}
            )
            for member_id, parent in lineage.parents.items()
        },
    }


def _gate_settings_dict(settings: GateSettingsView) -> dict[str, Any]:
    return {
        "status": settings.status,
        "kind": settings.kind,
        "commands": list(settings.commands),
        "timeout_seconds": settings.timeout_seconds,
        "consistency": settings.consistency,
    }


def _member_conversation_dict(entry: MemberConversation | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "session": entry.session,
        "context": entry.context,
        "since_round": entry.since_round,
        "candidates": list(entry.candidates),
        "omitted": entry.omitted,
        "memory": entry.memory,
    }


def _conversation_dict(view: ConversationView) -> dict[str, Any]:
    return {
        "status": view.status,
        "reason": view.reason,
        "members": {
            member_id: _member_conversation_dict(entry)
            for member_id, entry in view.members.items()
        },
    }


def starts_from(group: RoundGroup, row: CandidateRow) -> dict[str, Any] | None:
    """Where one candidate's worktree started, from the round's record, or
    None when the round has no usable record. Differs between the candidates
    of a round only when the round continued each member's own worktree."""
    lineage = group.lineage
    if lineage.status != "recorded":
        return None
    if lineage.tree == "continued":
        parent = lineage.parents.get(row.member_id)
        return {
            "kind": "continued",
            "parent_candidate_id": None if parent is None else parent[0],
            "parent_round_number": None if parent is None else parent[1],
        }
    if lineage.tree == "branch":
        return {
            "kind": "branch",
            "base_candidate_id": lineage.base_candidate_id,
            "base_patch_sha256": lineage.base_patch_sha256,
        }
    return {"kind": "snapshot"}


def comparison_to_dict(cmp: RoomComparison) -> dict[str, Any]:
    """The versioned JSON form. Additive fields keep ``schema_version`` 1;
    removing, renaming or re-typing a field bumps it. Body-free: ids, patch
    digests, sizes, verdicts, file names and gate commands — never prompt,
    output, patch or gate-check output text. The one free-text field is the
    selection rationale, which is the human reviewer's own words."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "room": {
            "room_id": cmp.room_id,
            "state": cmp.room_state,
            "revision": cmp.revision,
            "selected_candidate_id": cmp.selected_candidate_id,
            "round_count": cmp.round_count,
            "selection": _selection_dict(cmp.selection),
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
                "candidates": [
                    {
                        **_row_dict(r),
                        "starts_from": starts_from(g, r),
                        "conversation": _member_conversation_dict(
                            g.conversation.members.get(r.member_id)
                        ),
                    }
                    for r in g.rows
                ],
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
                "lineage": _lineage_dict(g.lineage),
                "gate_settings": _gate_settings_dict(g.gate_settings),
                "conversation": _conversation_dict(g.conversation),
            }
            for g in cmp.groups
        ],
        "cross_round": [dict(entry) for entry in cmp.cross_round],
        "cross_round_settings": [dict(entry) for entry in cmp.cross_round_settings],
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


def gate_label(gate: GateView) -> str:
    """The one wording of a gate verdict, shared by every surface."""
    text = _GATE_TEXT.get(gate.verdict, gate.verdict)
    if gate.reason:
        text += f" ({_p(gate.reason)})"
    return text


def usage_label(usage: TokenUsage | None) -> str:
    """The one long-form wording of reported usage; ``not reported`` when the
    provider sent none (never 0)."""
    return _usage_text(usage)


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


_SURFACE_TEXT = {"cli": "CLI", "tui": "TUI", "api": "API"}

_UNREADABLE_RECORD = "unknown (round provenance record unreadable, see warnings)"


def starting_tree_label(group: RoundGroup) -> str:
    """The one wording of where a round's candidates started, shared by every
    surface. It names ids and digests but no free text."""
    lineage = group.lineage
    if lineage.status == "malformed":
        return _UNREADABLE_RECORD
    if lineage.status != "recorded":
        # Round 1 can only snapshot (branch-from needs an earlier eligible
        # candidate; continue-own has no workspaces yet).
        if group.number == 1:
            return "snapshot"
        return "not recorded (this round predates lineage records)"
    requested = f" (requested lineage: {_p(lineage.requested)})"
    if lineage.tree == "branch":
        return (
            f"snapshot plus candidate {_p(lineage.base_candidate_id)}'s patch "
            f"(round {lineage.base_round_number}, sha256 {_p(lineage.base_patch_sha256)}), "
            "seeded into every member" + requested
        )
    if lineage.tree == "continued":
        return (
            "each member continues its own retained worktree; "
            "a common starting tree is not established" + requested
        )
    return "snapshot" + requested


def row_start_label(group: RoundGroup, row: CandidateRow) -> str | None:
    """Where one candidate's worktree started, when that differs per candidate
    (a round that continued each member's own worktree); else None."""
    if group.lineage.status != "recorded" or group.lineage.tree != "continued":
        return None
    parent = group.lineage.parents.get(row.member_id)
    if parent is None:
        return "parent not recorded"
    return f"the worktree left by candidate {_p(parent[0])} (round {parent[1]})"


def gate_settings_label(settings: GateSettingsView) -> str:
    """The one wording of a round's recorded gate settings. It never includes
    the commands themselves (free text each surface places its own way)."""
    if settings.status == "malformed":
        return _UNREADABLE_RECORD
    if settings.status != "recorded":
        return "not recorded (timeout not recorded)"
    if settings.kind == "none":
        text = "none configured"
    else:
        text = f"commands, timeout {settings.timeout_seconds}s"
    if settings.consistency == "contradicted":
        text += "; gate reports CONTRADICT these settings (see warnings)"
    elif settings.consistency == "indeterminate":
        text += "; some gate reports could not be checked (see warnings)"
    return text


_SESSION_TEXT = {
    "cold": "cold session",
    "resume": "resume of its stored session requested",
    "fork": "fork of its stored session requested",
}
_MEMORY_TEXT = {
    "included": "memory context included",
    "dropped": "memory context dropped (prompt size limit)",
    "none": "no memory context found",
}


def member_conversation(group: RoundGroup, row: CandidateRow) -> MemberConversation | None:
    if group.conversation.status != "recorded":
        return None
    return group.conversation.members.get(row.member_id)


def conversation_label(group: RoundGroup, row: CandidateRow) -> str | None:
    """The one wording of the conversation one candidate's provider was
    handed, or None for a round without a usable record (its round label
    says why). Counts only; the ids are in the JSON and the Details."""
    entry = member_conversation(group, row)
    if entry is None:
        return None
    count = len(entry.candidates)
    excerpts = f"{count} candidate excerpt{'' if count == 1 else 's'}"
    if entry.context == "delta":
        checkpoint = f"peers' outputs since round {entry.since_round}: {excerpts}"
    elif count or entry.omitted:
        checkpoint = f"full checkpoint: {excerpts}"
    else:
        checkpoint = "no earlier candidates"
    if entry.omitted:
        checkpoint += (
            ", older ones omitted for size" if count else " (all omitted for size)"
        )
    return f"{_SESSION_TEXT[entry.session]}; {checkpoint}; {_MEMORY_TEXT[entry.memory]}"


def round_conversation_label(group: RoundGroup) -> str:
    """Counts of the recorded modes, never a claim that members shared one
    context: each member has its own provider conversation, and two delta
    checkpoints exclude different members' candidates."""
    view = group.conversation
    if view.status == "malformed":
        return "unknown (round conversation record unreadable, see warnings)"
    if view.status != "recorded":
        # NULL is a round from before these records, or one whose record write
        # failed and stopped it before any provider ran — never assert which.
        return "not recorded (an older round, or one that stopped before any provider ran)"
    entries = list(view.members.values())

    def counts(values: list[str], order: tuple[str, ...]) -> str:
        return ", ".join(f"{v} {values.count(v)}" for v in order if v in values)

    memory = [e.memory for e in entries]
    memory_text = (
        "none found"
        if set(memory) == {"none"}
        else counts(memory, ("included", "dropped"))
    )
    return (
        "each member has its own; sessions: "
        + counts([e.session for e in entries], ("resume", "fork", "cold"))
        + f"; memory context: {memory_text}"
        + f"; checkpoint excerpts are capped at {_ROOM_CANDIDATE_CONTEXT_MAX_BYTES:,} bytes each"
    )


def _timeout_note(group: RoundGroup) -> bool:
    """Whether the report-derived gate profile still needs its ``timeout not
    recorded`` qualification (no usable settings record for the round)."""
    return group.gate_profile.kind == "commands" and group.gate_settings.status != "recorded"


def selection_lines(selection: SelectionView | None) -> list[str]:
    """The selection block every text surface shows (``room compare``, the TUI
    Compare view and the selected candidate's Details)."""
    if selection is None:
        return ["Selected: none"]
    who = _p(selection.candidate_id) if selection.candidate_id else "none"
    if selection.status == "not_recorded":
        return [
            f"Selected: {who} (human)",
            "  selection record: not recorded (selected before records were kept)",
        ]
    if selection.status != "bound":
        return [
            f"Selected: {who} (human)",
            f"  selection record: INCONSISTENT — {_p(selection.reason)}",
        ]
    confirmed = (
        f"revision {selection.confirmed_revision} confirmed"
        if selection.confirmed_revision is not None
        else "no revision confirmed (selected against the state at that moment)"
    )
    lines = [
        f"Selected: {who} (human)",
        f"  patch sha256: {_p(selection.patch_sha256)}",
        f"  {confirmed}; recorded as revision {selection.revision} via "
        f"{_SURFACE_TEXT.get(selection.surface or '', _p(selection.surface))} "
        f"at {_p(selection.selected_at)}",
    ]
    if selection.rationale is None:
        lines.append("  rationale: none recorded")
    else:
        lines.append("  rationale:")
        lines.extend(f"    {_p(line)}" for line in selection.rationale.split("\n"))
    return lines


def _row_lines(row: CandidateRow, group: RoundGroup) -> list[str]:
    attempt = "?" if row.attempt is None else row.attempt
    head = f"  {_p(row.candidate_id)}  {_p(row.member_id)} ({_p(row.provider_id)}) #{attempt}  {_p(row.state)}"
    if row.selected:
        head += "  [selected (human)]"
    lines = [head]
    if (start := row_start_label(group, row)) is not None:
        lines.append(f"    starts from: {start}")
    if (conversation := conversation_label(group, row)) is not None:
        lines.append(f"    conversation: {conversation}")
    if not row.readable:
        lines.append(f"    unreadable: {_p(row.unreadable_reason)}")
    lines.append(f"    gate: {gate_label(row.gate)}")
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
        *selection_lines(cmp.selection),
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
        settings = group.gate_settings
        lines.extend(
            [
                "",
                f"Round {group.number} of {cmp.round_count} ({_p(group.state)}) — "
                f"starting tree: {starting_tree_label(group)}",
                f"  gate settings: {gate_settings_label(settings)}"
                + (
                    ": " + "; ".join(_p(c) for c in settings.commands)
                    if settings.status == "recorded" and settings.kind == "commands"
                    else ""
                ),
                f"  gate reports: {_profile_text(group.gate_profile)}"
                + ("; timeout not recorded" if _timeout_note(group) else ""),
                f"  conversations: {round_conversation_label(group)}",
            ]
        )
        if not group.rows and not group.missing:
            lines.append("  (no candidates)")
        for row in group.rows:
            lines.extend(_row_lines(row, group))
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
    if cmp.cross_round or cmp.cross_round_settings:
        lines.extend(["", "Across rounds:"])
        lines.extend(f"  {line}" for line in cross_round_lines(cmp, _p))
    return "\n".join(lines)


def cross_round_lines(cmp: RoomComparison, esc: Callable[[object], str]) -> list[str]:
    """The cross-round summary every surface shows: recorded gate settings,
    report-observed commands, and each round's starting tree. ``esc`` escapes
    the free-text parts (commands, reasons) for the target surface."""
    lines: list[str] = []
    if cmp.cross_round_settings:
        lines.append("recorded gate settings:")
        for entry in cmp.cross_round_settings:
            rounds = ", ".join(str(n) for n in entry["rounds"])
            if entry["settings"] == "undecidable":
                lines.append(f"  round {rounds}: cannot establish equality ({esc(entry['reason'])})")
                continue
            what = (
                "no gate configured"
                if entry["settings"] == "none"
                else f"timeout {entry['timeout_seconds']}s, commands: "
                + "; ".join(esc(c) for c in entry["commands"])
            )
            line = f"  rounds {rounds}: same settings: {what}"
            notes = []
            if entry["contradicted_rounds"]:
                notes.append(
                    "reports contradict them in round "
                    + ", ".join(str(n) for n in entry["contradicted_rounds"])
                )
            if entry["indeterminate_rounds"]:
                notes.append(
                    "reports could not all be checked in round "
                    + ", ".join(str(n) for n in entry["indeterminate_rounds"])
                )
            if notes:
                line += " (" + "; ".join(notes) + ")"
            lines.append(line)
    if cmp.cross_round:
        lines.append("gate commands observed in reports (reports carry no timeout):")
        for entry in cmp.cross_round:
            rounds = ", ".join(str(n) for n in entry["rounds"])
            if entry["gate_profile"] == "undecidable":
                lines.append(f"  round {rounds}: cannot establish equality ({esc(entry['reason'])})")
            else:
                lines.append(
                    f"  rounds {rounds}: same commands: "
                    + "; ".join(esc(c) for c in entry["commands"])
                )
    lines.append("starting tree:")
    lines.extend(f"  round {g.number}: {starting_tree_label(g)}" for g in cmp.groups)
    return lines


# ------------------------------------------------------------ compact table

#: Rendered file names per overlap section in the compact table; a large patch
#: set must not turn one render into an unbounded string build.
TABLE_FILE_CAP = 200


def _clip(value: object, width: int) -> str:
    text = _p(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _size(n: int | None) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _usage_short(usage: TokenUsage | None) -> str:
    if usage is None:
        return "not reported"

    def k(n: int) -> str:
        # Integer arithmetic: a huge (but valid) count from a corrupt manifest
        # must not overflow a float division.
        return f"{n // 1000}.{n % 1000 // 100}k" if n >= 1000 else str(n)

    parts = []
    if usage.total_input_tokens is not None:
        parts.append(f"in {k(usage.total_input_tokens)}")
    if usage.output_tokens is not None:
        parts.append(f"out {k(usage.output_tokens)}")
    if usage.total_cost_usd is not None:
        parts.append(f"${usage.total_cost_usd:.2f}")
    if not parts and usage.cache_read_tokens is not None:
        parts.append(f"cache {k(usage.cache_read_tokens)}")
    if not parts and usage.cache_creation_tokens is not None:
        parts.append(f"cache write {k(usage.cache_creation_tokens)}")
    return " ".join(parts) or "not reported"


def _capped(names: tuple[str, ...]) -> str:
    shown = ", ".join(_p(n) for n in names[:TABLE_FILE_CAP]) or "-"
    if len(names) > TABLE_FILE_CAP:
        shown += f", ... {len(names) - TABLE_FILE_CAP} more"
    return shown


def find_round(cmp: RoomComparison, round_number: int) -> RoundGroup | None:
    for group in cmp.groups:
        if group.number == round_number:
            return group
    return None


def find_row(cmp: RoomComparison, candidate_id: str) -> tuple[RoundGroup, CandidateRow] | None:
    for group in cmp.groups:
        for row in group.rows:
            if row.candidate_id == candidate_id:
                return group, row
    return None


def render_round_table(
    cmp: RoomComparison,
    round_number: int,
    *,
    highlighted_id: str | None = None,
    as_of: str | None = None,
) -> str:
    """One round of a comparison as a compact, width-bounded table for the TUI
    Compare view. The highlighted (UI cursor) row is marked ``>``; the
    persisted human choice is marked ``selected`` — they are different things."""
    group = find_round(cmp, round_number)
    source = _SOURCE_TEXT.get(cmp.source_status)
    if source is None:
        source = f"check unavailable ({_p(cmp.source_reason)})"
    lines = [f"[Compare]  room {_p(cmp.room_id[:8])} ({_p(cmp.room_state)})"]
    for entry in cmp.damaged_rounds:
        lines.append(f"Damaged round {_p(entry['round_id'])}: {_p(entry['reason'])}")
    if group is None:
        lines.append(f"Round {round_number} is not in this comparison.")
        return "\n".join(lines)
    settings = group.gate_settings
    lines.extend(
        [
            f"Round {group.number} of {cmp.round_count} ({_p(group.state)})",
            f"starting tree: {starting_tree_label(group)}",
            f"gate settings: {gate_settings_label(settings)}"
            + (
                ": " + "; ".join(_p(c) for c in settings.commands)
                if settings.status == "recorded" and settings.kind == "commands"
                else ""
            ),
            f"gate reports: {_profile_text(group.gate_profile)}"
            + (" (timeout not recorded)" if _timeout_note(group) else ""),
            f"conversations: {round_conversation_label(group)}",
            f"source: {source}",
            *selection_lines(cmp.selection),
            *([f"evidence as of {as_of}"] if as_of else []),
            "",
        ]
    )
    # Stacked, not columnar: the TUI candidate pane is ~45 columns wide, so
    # each candidate is three short lines instead of one wrapped table row.
    for row in group.rows:
        head = (
            (">" if row.candidate_id == highlighted_id else " ")
            + f" {_clip(row.candidate_id[:8], 8)} {_clip(row.member_id, 12)} {_p(row.state)}"
        )
        if not row.readable:
            head += " UNREADABLE"
        if row.selected:
            head += " [selected]"
        time_text = "?" if row.duration_seconds is None else f"{row.duration_seconds:.1f}s"
        patch = _size(row.patch_bytes)
        if row.patch_binary:
            patch += " bin"
        if row.patch_large:
            patch += " large"
        files = "? files" if row.files_changed is None else f"{len(row.files_changed)} files"
        start = []
        if group.lineage.status == "recorded" and group.lineage.tree == "continued":
            parent = group.lineage.parents.get(row.member_id)
            start = [
                "  from: parent not recorded"
                if parent is None
                else f"  from: {_clip(parent[0][:8], 8)} (round {parent[1]})"
            ]
        entry = member_conversation(group, row)
        conv = []
        if entry is not None:
            conv = [
                f"  conv: {entry.session}, "
                + (f"delta since r{entry.since_round}" if entry.context == "delta" else "full")
                + f", {len(entry.candidates)} cand"
                + (" +omitted" if entry.omitted else "")
                + f", mem {entry.memory}"
            ]
        lines.extend(
            [
                head,
                *start,
                *conv,
                f"  gate {_GATE_TEXT.get(row.gate.verdict, row.gate.verdict)}"
                f" · {time_text} · {_usage_short(row.usage)}",
                f"  {patch} · {files} · "
                + ("eligible" if row.eligible else "ineligible")
                + f" · blockers {len(row.apply_blockers)}",
            ]
        )
    if not group.rows:
        lines.append("  (no candidates retained in this round)")
    for member in group.missing:
        if member.status == "in_progress":
            lines.append(f"  {_p(member.member_id)}: in progress")
        else:
            lines.append(
                f"  {_p(member.member_id)}: no candidate retained ("
                + "; ".join(_p(e) for e in member.evidence)
                + ")"
            )
    overlap = group.overlap
    if group.rows:
        lines.append("")
        lines.append("files:")
        if len(overlap.compared) >= 2:
            lines.append(f"  common to all ({len(overlap.common)}): {_capped(overlap.common)}")
        for candidate_id, names in overlap.only.items():
            lines.append(f"  only {_p(candidate_id[:8])} ({len(names)}): {_capped(names)}")
        if overlap.partial:
            partial = tuple(overlap.partial)
            lines.append(f"  changed by some, not all ({len(partial)}): {_capped(partial)}")
        if overlap.no_changes:
            lines.append(
                "  no files changed: " + ", ".join(_p(c[:8]) for c in overlap.no_changes)
            )
        if overlap.unknown:
            lines.append(
                "  file set unknown: " + ", ".join(_p(c[:8]) for c in overlap.unknown)
            )
    for warning in group.warnings:
        lines.append(f"warning: {_p(warning)}")
    for row in group.rows:
        for warning in row.warnings:
            lines.append(f"warning ({_p(row.candidate_id[:8])}): {_p(warning)}")
    lines.append("")
    lines.append("d/o/m on a row for its diff/output/details; r rebuilds this snapshot")
    return "\n".join(lines)


# ------------------------------------------------------------ review export

#: Fixed last line of every review summary.
REVIEW_FOOTER = (
    "Recorded evidence only. Selection is not apply; apply re-verifies the source and patch."
)

_ASCII_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _md(value: object) -> str:
    """An inline Markdown value: control characters escaped, then EVERY ASCII
    punctuation character backslash-escaped, so no heading, list, link,
    autolink, HTML or table cell boundary can form from it. Only ids, numbers
    and closed-vocabulary values are placed inline; free text goes through
    :func:`_md_fence`."""
    return "".join(
        "\\" + ch if ch in _ASCII_PUNCTUATION else ch for ch in _p(value)
    )


def _md_fence(lines: list[str]) -> list[str]:
    """Free-form stored text as a fenced code block, literal in every
    CommonMark/GFM renderer: the fence is a backtick run longer than any run
    in the content (minimum 3), so no content line can close it."""
    body = [_p(line) for line in lines]
    longest = 0
    for line in body:
        run = 0
        for ch in line:
            run = run + 1 if ch == "`" else 0
            longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return ["", fence, *body, fence, ""]


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """A GFM table; every cell must already be :func:`_md` output (or fixed
    template text)."""
    return [
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
        *("| " + " | ".join(row) + " |" for row in rows),
        "",
    ]


def _md_capped(names: tuple[str, ...]) -> list[str]:
    shown = [f"  {name}" for name in names[:TABLE_FILE_CAP]]
    if len(names) > TABLE_FILE_CAP:
        shown.append(f"  ... {len(names) - TABLE_FILE_CAP} more")
    return shown


def _md_selection(selection: SelectionView | None) -> list[str]:
    lines = ["", "## Selection", ""]
    if selection is None:
        return [*lines, "Selected: none"]
    who = _md(selection.candidate_id) if selection.candidate_id else "none"
    if selection.status == "not_recorded":
        return [
            *lines,
            f"Selected (human): {who}",
            "",
            "Selection record: not recorded (selected before records were kept)",
        ]
    if selection.status != "bound":
        return [
            *lines,
            f"Selected (human): {who}",
            "",
            "Selection record: INCONSISTENT, reason:",
            *_md_fence([selection.reason or ""]),
        ]
    confirmed = (
        _md(selection.confirmed_revision)
        if selection.confirmed_revision is not None
        else "none (selected against the state at that moment)"
    )
    surface = _SURFACE_TEXT.get(selection.surface or "")
    lines.extend(
        _md_table(
            ["Field", "Value"],
            [
                ["Candidate (human choice)", who],
                ["Patch sha256", _md(selection.patch_sha256)],
                ["Confirmed revision", confirmed],
                ["Recorded as revision", _md(selection.revision)],
                ["Surface", surface or _md(selection.surface)],
                ["Selected at", _md(selection.selected_at)],
            ],
        )
    )
    if selection.rationale is None:
        lines.append("Rationale: none recorded")
    else:
        lines.append("Rationale:")
        lines.extend(_md_fence(selection.rationale.split("\n")))
    return lines


def _md_patch(row: CandidateRow) -> str:
    if row.patch_bytes is None:
        return "unknown"
    patch = f"{row.patch_bytes:,} bytes"
    if row.patch_binary:
        patch += ", binary"
    if row.patch_large:
        patch += ", large"
    return _md(patch)


def _md_candidate(
    row: CandidateRow,
    start: str | None = None,
    conversation: str | None = None,
    entry: MemberConversation | None = None,
) -> list[str]:
    attempt = "unknown" if row.attempt is None else _md(row.attempt)
    lines = ["", f"### Candidate {_md(row.candidate_id)}"]
    lines.extend(
        _md_table(
            ["Field", "Value"],
            [
                ["Member", _md(row.member_id)],
                ["Provider", _md(row.provider_id)],
                ["Attempt", attempt],
                ["State", _md(row.state)],
                ["Selected", "yes (human)" if row.selected else "no"],
                ["Readable", "yes" if row.readable else "no"],
                ["Patch sha256", _md(row.patch_sha256)],
                ["Base commit", _md(row.base_commit)],
                *([["Starts from", _md(start)]] if start is not None else []),
                *([["Conversation", _md(conversation)]] if conversation is not None else []),
                *(
                    [["Checkpoint candidates", _md(", ".join(entry.candidates)) or "none"]]
                    if entry is not None
                    else []
                ),
            ],
        )
    )
    if not row.readable:
        lines.append("Unreadable, reason:")
        lines.extend(_md_fence([row.unreadable_reason or ""]))
    lines.append(f"Gate: {_md(_GATE_TEXT.get(row.gate.verdict, row.gate.verdict))}")
    if row.gate.reason:
        lines.extend(["", "Gate reason:", *_md_fence([row.gate.reason])])
    if row.gate.checks:
        lines.extend(
            _md_table(
                ["Check", "Result", "Exit code", "Duration"],
                [
                    [
                        _md(index),
                        "pass" if check.passed else "fail",
                        "not recorded" if check.exit_code is None else _md(check.exit_code),
                        "not recorded"
                        if check.duration_seconds is None
                        else _md(f"{check.duration_seconds:.1f}s"),
                    ]
                    for index, check in enumerate(row.gate.checks, 1)
                ],
            )
        )
        lines.append("Check commands:")
        lines.extend(
            _md_fence([f"{i}: {check.command}" for i, check in enumerate(row.gate.checks, 1)])
        )
    if row.eligible:
        lines.extend(["", "Eligible: yes"])
    elif row.not_eligible_reason:
        lines.extend(["", "Eligible: no, reason:", *_md_fence([row.not_eligible_reason])])
    else:
        lines.extend(["", "Eligible: no"])
    if row.apply_blockers:
        lines.extend(["", "Apply blockers:", *_md_fence(list(row.apply_blockers))])
    else:
        lines.extend(["", "Apply blockers: none known (select/apply still revalidate)"])
    if row.warnings:
        lines.extend(["", "Warnings:", *_md_fence(list(row.warnings))])
    return lines


def _md_overlap(overlap: FileOverlap) -> list[str]:
    body: list[str] = []
    if len(overlap.compared) >= 2:
        body.append(f"common to all ({len(overlap.common)}):")
        body.extend(_md_capped(overlap.common))
    for candidate_id, names in overlap.only.items():
        body.append(f"only {candidate_id} ({len(names)}):")
        body.extend(_md_capped(names))
    if overlap.partial:
        partial = tuple(overlap.partial)
        body.append(f"changed by some, not all ({len(partial)}):")
        for name in partial[:TABLE_FILE_CAP]:
            body.append(f"  {name}: {', '.join(overlap.partial[name])}")
        if len(partial) > TABLE_FILE_CAP:
            body.append(f"  ... {len(partial) - TABLE_FILE_CAP} more")
    if overlap.no_changes:
        body.append("no files changed: " + ", ".join(overlap.no_changes))
    if overlap.unknown:
        body.append("file set unknown: " + ", ".join(overlap.unknown))
    if not body:
        return []
    return ["", "Files:", *_md_fence(body)]


def _md_round(group: RoundGroup, round_count: int) -> list[str]:
    lines = [
        "",
        f"## Round {_md(group.number)} of {_md(round_count)} ({_md(group.state)})",
        "",
        f"Round id: {_md(group.round_id)}",
        "",
        f"Starting tree: {_md(starting_tree_label(group))}",
        "",
    ]
    lines.extend([f"Conversations: {_md(round_conversation_label(group))}", ""])
    settings = group.gate_settings
    if settings.status == "recorded" and settings.kind == "commands":
        lines.append(f"Gate settings: {_md(gate_settings_label(settings))}:")
        lines.extend(_md_fence(list(settings.commands)))
    else:
        lines.extend([f"Gate settings: {_md(gate_settings_label(settings))}", ""])
    profile = group.gate_profile
    if profile.kind == "commands":
        lines.append(
            "Gate reports: commands (timeout not recorded):"
            if _timeout_note(group)
            else "Gate reports: commands:"
        )
        lines.extend(_md_fence(list(profile.commands)))
    else:
        lines.append(
            "Gate reports: "
            + {
                "no_report": "no gate report on any completed candidate",
                "unknown": "unknown (no completed candidate with a usable record)",
                "mixed": "mixed (records disagree, see warnings)",
            }.get(profile.kind, _md(profile.kind))
        )
    if not group.rows and not group.missing:
        lines.extend(["", "No candidates."])
    if group.rows:
        lines.extend(
            _md_table(
                [
                    "Candidate", "Provider", "State", "Gate", "Eligible",
                    "Blockers", "Duration", "Usage", "Files", "Patch",
                ],
                [
                    [
                        _md(row.candidate_id)
                        + (" (selected, human)" if row.selected else ""),
                        _md(row.provider_id),
                        _md(row.state),
                        _md(_GATE_TEXT.get(row.gate.verdict, row.gate.verdict)),
                        "yes" if row.eligible else "no",
                        _md(len(row.apply_blockers)),
                        "not recorded"
                        if row.duration_seconds is None
                        else _md(f"{row.duration_seconds:.1f}s"),
                        _md(_usage_text(row.usage)),
                        "unknown"
                        if row.files_changed is None
                        else _md(len(row.files_changed)),
                        _md_patch(row),
                    ]
                    for row in group.rows
                ],
            )
        )
    if group.missing:
        lines.extend(["", "Members without a retained candidate:"])
        lines.extend(
            _md_table(
                ["Member", "Provider", "Status"],
                [
                    [
                        _md(m.member_id),
                        _md(m.provider_id),
                        "in progress" if m.status == "in_progress" else "no candidate retained",
                    ]
                    for m in group.missing
                ],
            )
        )
        evidence = [
            f"{m.member_id}: " + "; ".join(m.evidence)
            for m in group.missing
            if m.status != "in_progress"
        ]
        if evidence:
            lines.append("Progress evidence:")
            lines.extend(_md_fence(evidence))
    if group.rows:
        lines.extend(_md_overlap(group.overlap))
    if group.warnings:
        lines.extend(["", "Round warnings:", *_md_fence(list(group.warnings))])
    for row in group.rows:
        lines.extend(
            _md_candidate(
                row,
                row_start_label(group, row),
                conversation_label(group, row),
                member_conversation(group, row),
            )
        )
    return lines


def render_review_markdown(cmp: RoomComparison, *, generated_at: str) -> str:
    """A portable, body-free review summary of one comparison.

    It carries the same qualifications as :func:`render_room_comparison` (the
    starting tree, ``timeout not recorded``, every gate check), so a handoff
    never implies equal starting trees or gate settings. Escaping is by
    position: inline cells and header values hold only :func:`_md` output,
    and every free-form stored string (rationale, file names, gate commands,
    warnings, reasons) sits in a :func:`_md_fence` block."""
    source = _SOURCE_TEXT.get(cmp.source_status)
    lines = [
        f"# Room review: {_md(cmp.room_id)}",
        *_md_table(
            ["Field", "Value"],
            [
                ["Room", _md(cmp.room_id)],
                ["State", _md(cmp.room_state)],
                ["Revision", _md(cmp.revision)],
                ["Source", _md(source) if source else "check unavailable"],
                ["Generated", _md(generated_at)],
            ],
        ),
    ]
    if source is None:
        lines.extend(["Source check unavailable, reason:", *_md_fence([cmp.source_reason or ""])])
    lines.extend(_md_selection(cmp.selection))
    if cmp.damaged_rounds:
        lines.extend(["", "## Damaged rounds"])
        for entry in cmp.damaged_rounds:
            lines.extend(["", f"Round {_md(entry['round_id'])}:", *_md_fence([entry["reason"]])])
    if not cmp.groups:
        lines.extend(
            [
                "",
                "No readable rounds in this room."
                if cmp.damaged_rounds
                else "No rounds have run in this room yet.",
            ]
        )
    for group in cmp.groups:
        lines.extend(_md_round(group, cmp.round_count))
    if cmp.cross_round or cmp.cross_round_settings:
        lines.extend(["", "## Across rounds"])
        # Commands and reasons are free text, so the whole summary is fenced.
        lines.extend(_md_fence(cross_round_lines(cmp, str)))
    lines.extend(["", "---", "", REVIEW_FOOTER])
    return "\n".join(_squeeze(lines)) + "\n"


def _squeeze(lines: list[str]) -> list[str]:
    """Collapse runs of blank lines between blocks (block helpers pad both
    sides), never inside a fence: a blank line there is stored content."""
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        if fence is None and len(line) >= 3 and set(line) == {"`"}:
            fence = line
        elif fence is not None and line == fence:
            fence = None
        elif fence is None and line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out
