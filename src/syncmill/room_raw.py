"""Explicit raw content for ``room export`` (#189).

``room export`` is body-free by default. This module adds stored prompts, agent
output, gate check output and patches only for targets the user names, and
only in two steps: ``--preview`` shows what would be included and prints a
token; ``--confirm TOKEN`` collects everything again and writes it only when
the recomputed token is the same. The token is a digest of every item's
descriptor (kind, target, observed room and round, the full artifact digest
and size, how much is included, the integrity class and the source note), so
a confirmation is bound to exactly what was previewed.

Integrity differs by kind and every item says which applies:

- ``sqlite-digest``: patch and output, streamed once and checked against the
  digest the SQLite candidate row holds.
- ``manifest-record``: gate check output, stored in the candidate manifest,
  which has no independent digest; it is read through the comparison's own
  path (:func:`room_compare.read_candidate_gate`) and accepted only when that
  path accepts the record.
- ``room-database``: a round's prompt, read from its SQLite row.

Raw bytes are never redacted (they would no longer match their digests); a
warning says they may contain secrets. ``room_compare`` stays body-free: it
only places the lines this module renders before its footer. A flat top-level
module imported only by the CLI.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any

from syncmill.artifacts import printable_name
from syncmill.room_compare import (
    _GATE_TEXT,
    RoomComparison,
    RoundGroup,
    _md,
    _md_table,
    read_candidate_gate,
)
from syncmill.rooms import RoomError, RoomStore

KINDS = ("patch", "output", "gate-output", "prompt")

#: Bytes included per item; digests always cover the whole artifact.
DEFAULT_MAX_BYTES = 256 * 1024

#: Lines of each item the preview shows.
PREVIEW_LINES = 8

TOKEN_VERSION = 1

WARNING = (
    "Raw content is included verbatim: prompts, agent output, gate output and "
    "patches. It may contain secrets such as tokens, credentials, file paths "
    "or environment values. Review it before sharing."
)

DISPLAY_NOTE = (
    "Each block is a display rendering: control characters and invalid UTF-8 "
    "are shown as escapes such as \\x1b, which look the same as literal text "
    "such as \\x41. The sha256 is of the stored bytes; use --format json for "
    "byte-exact content."
)

_SOURCE_NOTES = {
    "patch": "stored candidate patch",
    "output": "stored provider output",
    "gate-output": (
        "stored gate check output text (the shipped CommandGate keeps only the "
        "last 4000 characters of a check's output); the sha256 identifies this "
        "stored text, not the original process output"
    ),
    "prompt": (
        "the round's user-authored prompt as stored in the room database; each "
        "provider also received checkpoint and memory context that is not part "
        "of this text and may differ per member"
    ),
}

_INTEGRITY_TEXT = {
    "sqlite-digest": "sha256 matches the SQLite candidate row",
    "manifest-record": "manifest record; no independent digest",
    "room-database": "room database row",
}


@dataclass(frozen=True, order=True)
class RawSpec:
    """One ``--include KIND:TARGET``, canonicalized."""

    kind: str
    target: str


def parse_include(value: str) -> RawSpec:
    """Parse ``KIND:TARGET``. A candidate id must be given in full (an export
    binds content, so no prefix matching); a round number is canonicalized so
    ``prompt:01`` and ``prompt:1`` are the same include."""
    kind, sep, target = value.partition(":")
    if not sep or kind not in KINDS:
        raise ValueError(f"expected KIND:TARGET with KIND one of {', '.join(KINDS)}")
    if kind == "prompt":
        if not re.fullmatch(r"[0-9]+", target) or int(target) < 1:
            raise ValueError("prompt takes a round number (1 or more)")
        return RawSpec(kind, str(int(target)))
    if not target:
        raise ValueError(f"{kind} takes a full candidate id")
    return RawSpec(kind, target)


@dataclass(frozen=True)
class RawItem:
    """One included block. Every field but ``head`` is its descriptor, and the
    whole descriptor is bound into the token."""

    kind: str
    target: str
    #: 1-based gate check index (gate-output only).
    check: int | None
    #: The check's stored command (gate-output only).
    command: str | None
    room_id: str
    round_id: str
    sha256: str
    size: int
    included_bytes: int
    truncated: bool
    integrity: str
    source_note: str
    head: bytes = field(repr=False)

    def descriptor(self) -> dict[str, Any]:
        data = asdict(self)
        del data["head"]
        return data


def _item(
    spec: RawSpec,
    *,
    room_id: str,
    round_id: str,
    data: bytes,
    size: int,
    digest: str,
    integrity: str,
    source_note: str,
    max_bytes: int,
    check: int | None = None,
    command: str | None = None,
) -> RawItem:
    head = data[:max_bytes]
    return RawItem(
        kind=spec.kind,
        target=spec.target,
        check=check,
        command=command,
        room_id=room_id,
        round_id=round_id,
        sha256=digest,
        size=size,
        included_bytes=len(head),
        truncated=size > len(head),
        integrity=integrity,
        source_note=source_note,
        head=head,
    )


def collect_raw(
    store: RoomStore,
    comparison: RoomComparison,
    specs: Iterable[RawSpec],
    max_bytes: int,
) -> list[RawItem]:
    """Read every requested item from the store, verified, in canonical order.

    Each target must lie inside the comparison's rounds; the reads are bound
    to the room, round and digests the comparison resolved, so a row that
    changed since is refused rather than exported under the old scope."""
    candidates = {
        row.candidate_id: (group, row) for group in comparison.groups for row in group.rows
    }
    rounds = {group.number: group for group in comparison.groups}
    items: list[RawItem] = []
    for spec in sorted(set(specs)):
        if spec.kind == "prompt":
            group = rounds.get(int(spec.target))
            if group is None:
                raise RoomError(
                    f"round {spec.target} is not in this export "
                    "(choose it with --round or use --all-rounds)"
                )
            items.append(_prompt_item(store, comparison.room_id, group, spec, max_bytes))
            continue
        found = candidates.get(spec.target)
        if found is None:
            raise RoomError(f"candidate {spec.target} is not in this export's rounds")
        group, row = found
        expected = {
            "room_id": comparison.room_id,
            "round_id": group.round_id,
            "patch_sha256": row.patch_sha256,
            "output_sha256": row.output_sha256,
        }
        if spec.kind == "gate-output":
            items.extend(_gate_items(store, spec, expected, max_bytes))
            continue
        read = store.load_candidate_raw_head(
            spec.target, spec.kind, max_bytes, expected=expected
        )
        note = _SOURCE_NOTES[spec.kind]
        if spec.kind == "output" and read["manifest"].get("output_truncated") is True:
            note += ", truncated at save"
        items.append(
            _item(
                spec,
                room_id=str(read["row"]["room_id"]),
                round_id=str(read["row"]["round_id"]),
                data=read["head"],
                size=read["size"],
                digest=read["sha256"],
                integrity="sqlite-digest",
                source_note=note,
                max_bytes=max_bytes,
            )
        )
    return items


def _prompt_item(
    store: RoomStore, room_id: str, group: RoundGroup, spec: RawSpec, max_bytes: int
) -> RawItem:
    row = store.get_round(group.round_id)
    if (
        row.get("room_id") != room_id
        or row.get("number") != group.number
        or row.get("id") != group.round_id
    ):
        raise RoomError(f"round {group.number} changed since the export resolved it")
    prompt = row.get("prompt")
    try:
        if not isinstance(prompt, str):
            raise TypeError("prompt is not text")
        data = prompt.encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise RoomError(f"round {group.number} prompt is not valid Unicode text") from exc
    return _item(
        spec,
        room_id=room_id,
        round_id=group.round_id,
        data=data,
        size=len(data),
        digest=sha256(data).hexdigest(),
        integrity="room-database",
        source_note=_SOURCE_NOTES["prompt"],
        max_bytes=max_bytes,
    )


def _gate_items(
    store: RoomStore, spec: RawSpec, expected: dict[str, Any], max_bytes: int
) -> list[RawItem]:
    row = store.get_candidate(spec.target)
    store._check_expected_row(spec.target, row, expected)
    # The comparison's own path (artifact verification, then the gate policy),
    # not a copy of it: raw export never shows checks the comparison discards.
    read = read_candidate_gate(
        store, spec.target, state=str(row["state"]), eligible=bool(row["eligible"])
    )
    verdict, manifest = read.gate, read.manifest
    if verdict.verdict not in ("passed", "failed") or manifest is None:
        reason = read.unreadable or verdict.reason or _GATE_TEXT.get(verdict.verdict, verdict.verdict)
        raise RoomError(f"candidate {spec.target} has no usable gate record: {reason}")
    # The manifest was validated against the row as it was at read time; it
    # must still be the row the comparison resolved this target from.
    for key in ("room_id", "round_id", "patch_sha256", "output_sha256"):
        if manifest.get(key) != expected[key]:
            raise RoomError(
                f"candidate {key} changed since the export resolved it: {spec.target}"
            )
    if not manifest["gate"]["checks"]:
        # An include must add at least one item: zero items would leave the
        # token unchanged by the include and export nothing for it.
        raise RoomError(f"candidate {spec.target} has a gate report with no checks")
    items: list[RawItem] = []
    for index, check in enumerate(manifest["gate"]["checks"], 1):
        try:
            command = check["command"]
            command.encode("utf-8")
            data = check["output"].encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RoomError(
                f"candidate {spec.target} gate check {index} is not valid Unicode text"
            ) from exc
        items.append(
            _item(
                spec,
                room_id=expected["room_id"],
                round_id=expected["round_id"],
                data=data,
                size=len(data),
                digest=sha256(data).hexdigest(),
                integrity="manifest-record",
                source_note=_SOURCE_NOTES["gate-output"],
                max_bytes=max_bytes,
                check=index,
                command=command,
            )
        )
    return items


def scope_of(comparison: RoomComparison, *, all_rounds: bool) -> dict[str, Any]:
    """The resolved export scope bound into the token: the default (latest)
    round is named by its number, so a new round changes it."""
    if all_rounds:
        return {"all_rounds": True}
    return {"round": comparison.groups[0].number if comparison.groups else None}


def preview_token(
    room_id: str, scope: dict[str, Any], max_bytes: int, items: list[RawItem]
) -> str:
    document = {
        "v": TOKEN_VERSION,
        "room": room_id,
        "scope": scope,
        "cap": max_bytes,
        "items": [item.descriptor() for item in items],
    }
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return sha256(canonical.encode("ascii")).hexdigest()


# ------------------------------------------------------------- rendering


def _render_line(line: str) -> str:
    out: list[str] = []
    for ch in line:
        if "\udc80" <= ch <= "\udcff":
            # A byte that was not valid UTF-8 (surrogateescape).
            out.append(f"\\x{ord(ch) - 0xDC00:02x}")
        elif ch == "\t" or ch.isprintable():
            out.append(ch)
        else:
            out.append(printable_name(ch))
    return "".join(out)


def display_lines(data: bytes) -> tuple[list[str], bool]:
    """``(lines, ends_with_newline)`` of the display rendering. LF splits lines
    and one final LF is a terminator; TAB stays literal; every other
    non-printable character and every invalid UTF-8 byte becomes an escape."""
    parts = data.decode("utf-8", errors="surrogateescape").split("\n")
    ends_with_newline = parts[-1] == ""
    if ends_with_newline:
        parts.pop()
    return [_render_line(part) for part in parts], ends_with_newline


def _p(value: object) -> str:
    return printable_name(str(value))


def render_preview(items: list[RawItem], token: str, room_id: str) -> str:
    lines = [f"Raw content preview for room {_p(room_id)}", ""]
    for number, item in enumerate(items, 1):
        title = f"[{number}] {item.kind} {_p(item.target)}"
        if item.check is not None:
            title += f" check {item.check}"
        lines.append(title)
        if item.command is not None:
            lines.append(f"    command: {_p(item.command)}")
        lines.append(f"    integrity: {_INTEGRITY_TEXT[item.integrity]}")
        lines.append(f"    size: {item.size:,} bytes, sha256 {item.sha256}")
        lines.append(
            f"    included: {item.included_bytes:,} bytes"
            + (" (truncated)" if item.truncated else "")
        )
        lines.append(f"    source: {item.source_note}")
        body, _ = display_lines(item.head)
        shown = body[:PREVIEW_LINES]
        lines.append(f"    first {len(shown)} line(s):")
        lines.extend(f"    | {line}" for line in shown)
        if len(body) > len(shown) or item.truncated:
            lines.append("    | ...")
        lines.append("")
    total = sum(item.included_bytes for item in items)
    lines.extend(
        [
            f"{len(items)} item(s), {total:,} bytes to include.",
            "",
            f"WARNING: {WARNING}",
            "",
            f"Token: {token}",
            f"To write: rerun the same command with --preview replaced by --confirm {token}",
            "(add --output FILE to write a file).",
        ]
    )
    return "\n".join(lines) + "\n"


def _md_raw_fence(lines: list[str]) -> list[str]:
    """Already-rendered lines as one fenced block whose fence outlasts every
    backtick run in them (the #186 rule). Fence and body are separate list
    entries so the review renderer's blank-line squeeze sees the fence."""
    longest = 0
    for line in lines:
        run = 0
        for ch in line:
            run = run + 1 if ch == "`" else 0
            longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return ["", fence, *lines, fence, ""]


def render_markdown_section(items: list[RawItem], token: str) -> list[str]:
    lines = [
        "",
        "## Raw content (explicitly included)",
        "",
        f"WARNING: {_md(WARNING)}",
        "",
        _md(DISPLAY_NOTE),
        "",
        _md("The comparison above was built when this export was confirmed."),
        "",
        f"Confirmed with token: {_md(token)}",
    ]
    for number, item in enumerate(items, 1):
        title = f"Raw {number}: {item.kind} {item.target}"
        if item.check is not None:
            title += f" check {item.check}"
        lines.extend(["", f"### {_md(title)}"])
        lines.extend(
            _md_table(
                ["Field", "Value"],
                [
                    ["Kind", _md(item.kind)],
                    ["Target", _md(item.target)],
                    *([["Check", _md(item.check)]] if item.check is not None else []),
                    ["Round id", _md(item.round_id)],
                    ["Integrity", _md(_INTEGRITY_TEXT[item.integrity])],
                    ["Size", _md(f"{item.size:,} bytes")],
                    ["Sha256", _md(item.sha256)],
                    ["Included", _md(f"{item.included_bytes:,} bytes")],
                    ["Truncated", "yes" if item.truncated else "no"],
                ],
            )
        )
        if item.command is not None:
            lines.append("Command:")
            lines.extend(_md_raw_fence([_render_line(item.command)]))
        lines.append("Source:")
        lines.extend(_md_raw_fence([item.source_note]))
        body, ends_with_newline = display_lines(item.head)
        lines.append("Content:")
        lines.extend(_md_raw_fence(body))
        if item.truncated:
            lines.append(
                _md(f"[truncated: {item.included_bytes:,} of {item.size:,} bytes shown]")
            )
        elif body and not ends_with_newline:
            lines.append(_md("[no newline at end of content]"))
    return lines


def json_section(items: list[RawItem], token: str) -> dict[str, Any]:
    return {
        "token": token,
        "warning": WARNING,
        "items": [
            {
                **item.descriptor(),
                "encoding": "base64",
                "data": base64.b64encode(item.head).decode("ascii"),
            }
            for item in items
        ],
    }
