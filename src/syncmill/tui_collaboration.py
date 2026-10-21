"""Textual widgets and renderers for project sessions and comparison rooms."""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, TextArea

from syncmill.artifacts import printable_name, printable_text
from syncmill.config import SyncmillConfig
from syncmill.room_compare import (
    CandidateRow,
    RoundGroup,
    SelectionView,
    gate_label,
    conversation_label,
    gate_settings_label,
    member_conversation,
    round_conversation_label,
    row_start_label,
    selection_lines,
    starting_tree_label,
    usage_label,
)
from syncmill.session import interactive_agents
from syncmill.tui_render import PlainLabel, PlainStatic, plain_select


class _CollabModal(ModalScreen[dict | bool | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionOpenModal(_CollabModal):
    def __init__(self, config: SyncmillConfig):
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        choices = [
            (agent, agent)
            for agent in self._config.agent_priority
            if agent in interactive_agents()
        ]
        with Vertical(classes="form-dialog"):
            yield PlainLabel("Open isolated native CLI session")
            if not choices:
                yield PlainStatic("No interactive agents are configured.", classes="warning")
                yield Button("Cancel", id="cancel")
                return
            yield plain_select(choices, value=choices[0][1], allow_blank=False, id="agent")
            yield PlainStatic(
                "The provider runs in a persistent isolated worktree. "
                "Attach separately; source changes require checkpoint + apply.",
                classes="warning",
            )
            with Horizontal(classes="form-buttons"):
                yield Button("Open", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        value = self.query_one("#agent", Select).value
        self.dismiss({"agent": value})


class RoomCreateModal(_CollabModal):
    def __init__(self, config: SyncmillConfig):
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        enabled = [
            agent
            for agent in self._config.agent_priority
            if self._config.agent_config(agent).enabled
        ][:3]
        with Vertical(classes="wide-dialog"):
            yield PlainLabel("Create comparison room")
            yield TextArea(id="room-prompt", language=None)
            yield Input(value=", ".join(enabled), id="room-agents")
            yield PlainStatic(
                "Creating a room records its prompt and members without calling a "
                "provider. Starting a round with x makes one isolated provider call "
                "per listed agent and preserves every candidate.",
                classes="warning",
            )
            with Horizontal(classes="form-buttons"):
                yield Button("Create", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        self.dismiss(
            {
                "prompt": self.query_one("#room-prompt", TextArea).text,
                "agents": self.query_one("#room-agents", Input).value,
            }
        )


class RoomRoundModal(_CollabModal):
    """Per-round prompt + lineage input for the Rooms tab.

    Replaces the bare confirm the round used to run through: the TextArea is
    prefilled with the PREVIOUS round's prompt (creation prompt on a fresh
    room) so every round can carry a new instruction, and lineage/base-candidate
    are selectable in place. The review checkbox safety gate is preserved.
    Dumb like the other modals — dismisses raw strings; parsing/validation
    stays in the app funnel."""

    _LINEAGES = [
        ("continue-own (resume each member)", "continue-own"),
        ("fresh (cold, from-scratch trees)", "fresh"),
        ("branch-from (seed all from one candidate)", "branch-from"),
    ]

    def __init__(self, room: dict, default_prompt: str, eligible: list[dict]):
        super().__init__()
        self._room = room
        self._default_prompt = default_prompt
        self._eligible = eligible

    def compose(self) -> ComposeResult:
        members = self._room.get("members", [])
        with Vertical(classes="wide-dialog"):
            yield PlainLabel(
                f"Run round {int(self._room.get('round_count') or 0) + 1} — "
                f"{len(members)} provider call(s)"
            )
            area = TextArea(id="round-prompt", language=None)
            # Escaped prefill: new prompts cannot contain controls (the
            # coordinator rejects them), but a row stored before that
            # validation could — never hand raw sequences to the terminal.
            area.text = escape_controls(self._default_prompt)
            yield area
            yield plain_select(
                self._LINEAGES,
                value="continue-own",
                allow_blank=False,
                id="round-lineage",
            )
            if self._eligible:
                yield plain_select(
                    [
                        (f"{row['id'][:8]} {row['member_id']}", row["id"])
                        for row in self._eligible
                    ],
                    allow_blank=True,
                    prompt="base candidate (branch-from only)",
                    id="round-base",
                )
            yield PlainStatic(
                "The detached worker keeps running if the TUI closes. An empty "
                "prompt reuses the room creation prompt.",
                classes="warning",
            )
            yield Checkbox("I reviewed this exact action", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button("Run round", variant="warning", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the action", severity="warning")
            return
        lineage = self.query_one("#round-lineage", Select).value
        base = None
        if self._eligible:
            value = self.query_one("#round-base", Select).value
            # An unselected Select yields a sentinel (NULL/BLANK depending on
            # the textual major) — only a real candidate-id string counts.
            base = value if isinstance(value, str) and value else None
        if lineage == "branch-from" and base is None:
            self.app.notify(
                "branch-from requires a base candidate", severity="warning"
            )
            return
        if lineage != "branch-from":
            base = None  # a picked base is meaningful only under branch-from
        self.dismiss(
            {
                "prompt": self.query_one("#round-prompt", TextArea).text,
                "lineage": lineage,
                "base_candidate": base,
            }
        )


class CollaborationConfirmModal(_CollabModal):
    def __init__(self, title: str, detail: str, action: str):
        super().__init__()
        # Callers escape each interpolated value with printable_name, so a
        # value cannot add a line; this keeps anything else off the screen.
        self._title = printable_name(title)
        self._detail = printable_text(detail)
        self._action = action

    def compose(self) -> ComposeResult:
        with Vertical(classes="wide-dialog"):
            yield PlainLabel(self._title)
            yield PlainStatic(self._detail)
            yield Checkbox("I reviewed this exact action", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button(self._action, variant="warning", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the action", severity="warning")
            return
        self.dismiss(True)


class CandidateSelectModal(_CollabModal):
    """Confirm a candidate selection, with an optional rationale recorded
    alongside it. Dumb like the other modals: it dismisses the raw text and
    the store validates it."""

    def __init__(self, detail: str):
        super().__init__()
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="wide-dialog"):
            yield PlainLabel("Select candidate (source remains unchanged)")
            # The detail and rationale scroll; the confirmation and the
            # buttons stay outside, so they are reachable at 80x24 too.
            with VerticalScroll(id="selection-body"):
                yield PlainStatic(self._detail)
                yield PlainLabel("Rationale (optional) — why this candidate:")
                yield TextArea(id="selection-rationale", language=None)
            yield Checkbox("I reviewed this exact candidate", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button("Select", variant="warning", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the action", severity="warning")
            return
        self.dismiss(
            {"rationale": self.query_one("#selection-rationale", TextArea).text}
        )


def _patch_is_binary(head: bytes) -> bool:
    """Whether a patch head carries binary content. Derived — no stored flag:
    patches are captured with `git diff --binary`, whose binary hunks are
    marked in-band."""
    return (
        b"GIT binary patch" in head
        or b"\nBinary files " in head
        or head.startswith(b"Binary files ")
        or b"\x00" in head
    )


def _p(value: object) -> str:
    """A stored value in a one-line field, as the CLI shows it."""
    return printable_name(str(value))


def render_session(value: dict) -> str:
    checkpoints = ", ".join(_p(c) for c in value.get("checkpoints", []))
    return "\n".join(
        [
            f"Session: {_p(value.get('session_id'))}",
            f"Agent: {_p(value.get('agent_id'))}",
            f"State: {_p(value.get('state'))} | live: {_p(value.get('live', False))}",
            f"Source: {'STALE' if value.get('source_stale') else 'clean snapshot'}",
            f"Worktree: {_p(value.get('worktree_path'))}",
            f"Checkpoints: {checkpoints or '-'}",
            "",
            "x attach | c checkpoint | k close",
        ]
    )


def render_room(room: dict, candidates: list[dict], events: list[dict]) -> str:
    members = ", ".join(_p(member["member_id"]) for member in room.get("members", []))
    tail = events[-5:]
    lines = [
        f"Room: {_p(room.get('id'))}",
        f"Prompt: {_prompt_first_line(room.get('prompt'))}",
        f"State: {_p(room.get('state'))} | revision: {_p(room.get('revision'))}",
        f"Rounds: {_p(room.get('round_count'))} | members: {members}",
        f"Selected: {_p(room.get('selected_candidate_id') or '-')}",
        f"Apply run: {_p(room.get('apply_run_id') or '-')}",
        f"Applied run: {_p(room.get('applied_run_id') or '-')}",
        "",
        "Recent events:",
        *(f"{_p(event['sequence'])} {_p(event['kind'])}" for event in tail),
        "",
        "x run round | e select candidate | g apply selected",
    ]
    return "\n".join(lines)


#: C0 (minus tab/newlines) + DEL + C1 — the terminal-interpretable range.
#: New prompts are rejected at the coordinator boundary; this escaping is the
#: display-side defense for rows stored before that validation existed.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def escape_controls(text: str) -> str:
    """Visibly escape C0/C1 controls — Textual passes sequences like OSC 52
    through to the terminal even with ``markup=False``."""
    return _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def _prompt_first_line(prompt: object, limit: int = 100) -> str:
    """A prompt's first line trimmed for one-line detail views ('-' when the
    field is absent — a corrupt row must render, not crash the poll)."""
    # Escaped first, so a CR, NEL or U+2028 stays visible: strip() would drop
    # one at either end, and splitlines() would also end the line there.
    shown = printable_text(prompt).strip() if isinstance(prompt, str) else ""
    if not shown:
        return "-"
    line = shown.split("\n")[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"


def render_candidate_diff(
    manifest: dict,
    patch_head: bytes,
    *,
    truncated: bool,
    binary: bool | None = None,
) -> str:
    """The candidate Diff view: the digest-bound patch window, binary-marked.

    ``binary`` is the loader's whole-stream verdict (a binary hunk may sit
    beyond the displayed head); ``None`` falls back to sniffing the head.
    """
    lines = [
        f"Candidate: {_p(manifest.get('candidate_id'))}  [Diff]",
        f"Patch: {_p(manifest.get('patch_bytes'))} bytes {_p(manifest.get('patch_sha256'))}",
    ]
    if _patch_is_binary(patch_head) if binary is None else binary:
        lines.append("[contains binary patch content]")
    text = printable_text(patch_head.decode("utf-8", errors="surrogateescape"))
    lines.extend(["", text or "(empty patch)"])
    if truncated:
        lines.append("\n[... truncated — press d again to load more ...]")
    return "\n".join(lines)


def render_candidate_output(manifest: dict) -> str:
    """The candidate Output view: state and error first, then the bounded raw
    output — a failed candidate's review starts with why it failed. Gate,
    usage and eligibility are interpreted in one place only (the comparison
    read model behind Details and Compare), so this view does not repeat them."""
    lines = [
        f"Candidate: {_p(manifest.get('candidate_id'))}  [Output]",
        f"State: {_p(manifest.get('state'))} | exit: {_p(manifest.get('exit_code'))} | "
        f"{_p(manifest.get('duration_seconds'))}s",
    ]
    if manifest.get("error"):
        lines.append(f"Error: {printable_text(str(manifest['error']))}")
    lines.append("Gate, usage, eligibility: see Details (m) or Compare (c)")
    if manifest.get("output_truncated"):
        lines.append("[stored output was truncated at capture]")
    # `or ""`: a manifest with an explicit `"output": null` must render, not
    # TypeError out of the room poll.
    output = printable_text(str(manifest.get("output") or ""))
    if len(output) > 100_000:
        output = output[:50_000] + "\n\n[... display truncated ...]\n\n" + output[-50_000:]
    lines.extend(["", output or "(no output)"])
    return "\n".join(lines)


def render_candidate_details(
    row: CandidateRow,
    *,
    round_number: int | None,
    round_prompt: str | None = None,
    as_of: str | None = None,
    selection: SelectionView | None = None,
    group: RoundGroup | None = None,
) -> str:
    """The candidate Details view, rendered from the comparison read model's
    row, so its gate, eligibility, usage, patch and blocker wording is the one
    ``room compare`` and the Compare view use. Every stored string is
    escaped. ``selection`` is shown only on the selected row; ``group`` (the
    row's round) adds its starting tree and recorded gate settings."""
    p = printable_name
    attempt = "?" if row.attempt is None else row.attempt
    lines = [
        f"Candidate: {p(row.candidate_id)}  [Details]",
        *([f"evidence as of {as_of}"] if as_of else []),
        f"Member: {p(row.member_id)} ({p(row.provider_id)})",
        f"Round: {round_number if round_number is not None else '?'} | attempt: {attempt}",
        *(
            [f"Round prompt: {_prompt_first_line(round_prompt)}"]
            if round_prompt is not None
            else []
        ),
    ]
    if group is not None:
        lines.append(f"Starting tree: {starting_tree_label(group)}")
        if (start := row_start_label(group, row)) is not None:
            lines.append(f"Starts from: {start}")
        settings = group.gate_settings
        lines.append(
            f"Gate settings: {gate_settings_label(settings)}"
            + (
                ": " + "; ".join(p(c) for c in settings.commands)
                if settings.status == "recorded" and settings.kind == "commands"
                else ""
            )
        )
        lines.append(f"Conversations: {round_conversation_label(group)}")
        if (conversation := conversation_label(group, row)) is not None:
            lines.append(f"Conversation: {conversation}")
            entry = member_conversation(group, row)
            lines.append(
                "Checkpoint candidates: "
                + (", ".join(p(c) for c in entry.candidates) if entry and entry.candidates else "none")
            )
        # The labels above point at "see warnings": the round's own warnings
        # (provenance reasons, report cross-check) live on the group.
        lines.extend(f"Round warning: {p(w)}" for w in group.warnings)
    if not row.readable:
        lines.append(f"Unreadable: {p(row.unreadable_reason)}")
    eligible = "yes" if row.eligible else "no"
    if not row.eligible and row.not_eligible_reason:
        eligible += f" ({p(row.not_eligible_reason)})"
    duration = "?" if row.duration_seconds is None else f"{row.duration_seconds:.1f}s"
    exit_code = "?" if row.exit_code is None else row.exit_code
    lines.extend(
        [
            f"State: {p(row.state)} | eligible: {eligible}",
            f"Exit: {exit_code} | duration: {duration}",
        ]
    )
    if row.error:
        lines.append(f"Error: {p(row.error)}")
    lines.append(f"Gate: {gate_label(row.gate)}")
    for check in row.gate.checks:
        check_exit = "?" if check.exit_code is None else check.exit_code
        check_time = (
            "?" if check.duration_seconds is None else f"{check.duration_seconds:.1f}s"
        )
        lines.append(
            f"  {'✓' if check.passed else '×'} {p(check.command)} "
            f"(exit {check_exit}, {check_time})"
        )
    lines.append(f"Usage: {usage_label(row.usage)}")
    files = "unknown" if row.files_changed is None else (
        ", ".join(p(name) for name in row.files_changed) or "-"
    )
    patch = "unknown" if row.patch_bytes is None else f"{row.patch_bytes} bytes"
    flags = []
    if row.patch_binary:
        flags.append("binary")
    if row.patch_large:
        flags.append("large")
    output_bytes = "?" if row.output_bytes is None else row.output_bytes
    truncated = "?" if row.output_truncated is None else row.output_truncated
    lines.extend(
        [
            f"Files: {files}",
            f"Patch: {patch}" + (f" ({', '.join(flags)})" if flags else ""),
            f"Patch sha256: {p(row.patch_sha256)}",
            f"Output: {output_bytes} bytes | truncated: {truncated}",
            f"Output sha256: {p(row.output_sha256 or '?')}",
            f"Session: {p(row.provider_session_id or '-')}",
            f"Created: {p(row.created_at or '?')} | base: {p(row.base_commit)}",
        ]
    )
    if row.apply_blockers:
        lines.append("Apply blockers:")
        lines.extend(f"  - {p(blocker)}" for blocker in row.apply_blockers)
    else:
        lines.append("Apply blockers: none known (select/apply still revalidate)")
    for warning in row.warnings:
        lines.append(f"Warning: {p(warning)}")
    if row.selected and selection is not None:
        lines.extend(selection_lines(selection))
    return "\n".join(lines)
