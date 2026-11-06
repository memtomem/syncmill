"""Textual widgets and renderers for project sessions and comparison rooms."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, TextArea

from syncmill.artifacts import printable_name, printable_text
from syncmill.board import (
    Decision,
    has_control_characters,
    lesson_reach,
    lesson_source,
    select_lessons,
)
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
from syncmill.tui_render import ExactInput, PlainLabel, PlainStatic, plain, plain_select


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
            yield PlainLabel("Goal (optional, fixed for the room; adds its lessons):")
            yield ExactInput(placeholder="blank = no goal", id="room-goal")
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
                "goal": self.query_one("#room-goal", ExactInput).value,
            }
        )


def round_prompt(raw: object) -> str | None:
    """The round prompt a submitted TextArea value means: the text VERBATIM
    (the raw-user-text contract the stored rounds.prompt keeps), or None —
    reuse the room creation prompt — when it is empty or all whitespace. The
    lesson fit check and the launch both use this, so they cannot disagree."""
    text = str(raw or "")
    return text if text.strip() else None


@dataclasses.dataclass(frozen=True)
class RoundLessonsView:
    """What the round dialog shows about approved lessons (#174), read once
    when the dialog opens.

    ``board_off`` says the board is disabled, so no lessons can be sent.
    ``lessons`` are the project's active project-wide lessons, oldest first;
    the dialog re-runs the CLI's ``select_lessons`` on every change, so a
    skipped lesson frees budget exactly as ``--skip-lesson`` does.
    ``unavailable`` is why lessons could not be read (the round runs without
    them). ``history`` maps a lineage to whether a round launched with it now
    would resume a conversation that earlier rounds sent lessons to (a lineage
    missing from it could not be checked). ``fits(prompt, texts)`` is the
    coordinator's own fit check, with a None prompt meaning the creation
    prompt."""

    lessons: tuple[Decision, ...] = ()
    board_off: bool = False
    unavailable: str | None = None
    history: dict[str, bool] = dataclasses.field(default_factory=dict)
    fits: Callable[[str | None, list[str]], bool] = lambda prompt, texts: True


_HISTORY_NOTE = (
    "Resumed conversations may still hold lessons sent in earlier rounds: "
    "unchecking a lesson stops sending it, it does not take back what a "
    "conversation was already sent. The fresh lineage starts without that history."
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

    def __init__(
        self,
        room: dict,
        default_prompt: str,
        eligible: list[dict],
        lessons: RoundLessonsView | None = None,
    ):
        super().__init__()
        self._room = room
        self._default_prompt = default_prompt
        self._eligible = eligible
        self._lessons = lessons if lessons is not None else RoundLessonsView()

    def compose(self) -> ComposeResult:
        members = self._room.get("members", [])
        with Vertical(classes="wide-dialog"):
            yield PlainLabel(
                f"Run round {int(self._room.get('round_count') or 0) + 1} — "
                f"{len(members)} provider call(s)"
            )
            # Everything but the confirmation and the buttons scrolls, so they
            # stay reachable at 80x24 with a full lesson list.
            with VerticalScroll(id="round-body"):
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
                if self._lessons.board_off:
                    yield PlainStatic(
                        "Lessons: the board is off, so this round sends no approved "
                        "lessons."
                    )
                if self._lessons.unavailable is not None:
                    yield PlainStatic(
                        f"Lessons unavailable ({printable_name(self._lessons.unavailable)}); "
                        "this round runs without them.",
                        classes="warning",
                    )
                if self._lessons.lessons:
                    yield PlainStatic(
                        "Approved project lessons (sent to every member's prompt "
                        "this round; uncheck to leave one out of this round):"
                    )
                    for index, lesson in enumerate(self._lessons.lessons):
                        # The label is only the id: a Checkbox label does not
                        # wrap, so a long lesson would be cut off at the dialog
                        # edge while every word of it is sent. The full text is
                        # a wrapping PlainStatic below it. plain(): a str label
                        # is parsed as markup by Checkbox.
                        yield Checkbox(
                            plain(f"lesson {lesson.id[:8]}"),
                            value=True,
                            id=f"round-lesson-{index}",
                        )
                        yield PlainStatic(
                            lesson.text, classes="lesson-text", id=f"round-lesson-text-{index}"
                        )
                        if lesson.scope:
                            # Its own wrapping line, not the label: a goal can
                            # be up to 200 bytes and a Checkbox label is cut.
                            yield PlainStatic(
                                f"for this room's goal {printable_name(lesson.scope)}",
                                classes="lesson-text",
                            )
                        source = lesson_source(lesson)
                        if source:
                            yield PlainStatic(source, classes="lesson-text")
                    yield PlainStatic(
                        "Replace a lesson with p on a judged room, or "
                        '`syncmill board decide --supersedes ID "<new text>"`'
                        + (
                            " (add --scope GOAL for a goal lesson)."
                            if any(lesson.scope for lesson in self._lessons.lessons)
                            else "."
                        )
                    )
                yield PlainStatic("", id="round-lessons-status")
                yield PlainStatic(
                    "The detached worker keeps running if the TUI closes. An empty "
                    "prompt reuses the room creation prompt.",
                    classes="warning",
                )
            yield Checkbox("I reviewed this exact action", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button("Run round", variant="warning", id="apply")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._update_lessons_status()

    def _lesson_choice(self) -> tuple[list[Decision], list[Decision], list[Decision]]:
        """``(used, skipped, over_budget)`` for the boxes as they stand: the
        CLI's selection, with an unchecked box as ``--skip-lesson``."""
        skip = frozenset(
            lesson.id
            for index, lesson in enumerate(self._lessons.lessons)
            if not self.query_one(f"#round-lesson-{index}", Checkbox).value
        )
        return select_lessons(list(self._lessons.lessons), skip=skip)

    def _lineage(self) -> object:
        return self.query_one("#round-lineage", Select).value

    def _status_text(self) -> tuple[str, bool]:
        """The lessons status block, and whether the chosen lessons fit."""
        lines: list[str] = []
        fits = True
        if self._lessons.lessons:
            used, skipped, over = self._lesson_choice()
            prompt = round_prompt(self.query_one("#round-prompt", TextArea).text)
            fits = not used or self._lessons.fits(prompt, [d.text for d in used])
            lines.append(f"Lessons this round: {len(used)} of {len(self._lessons.lessons)}")
            lines.extend(
                f"  left out: {d.id[:8]}{lesson_reach(d)} (unchecked)" for d in skipped
            )
            lines.extend(
                f"  left out: {d.id[:8]}{lesson_reach(d)} (over the per-run lesson budget)"
                for d in over
            )
            if not fits:
                lines.append(
                    "  this round's prompt leaves no room for the checked lessons: "
                    "uncheck some or shorten the prompt"
                )
        if self._lessons.history.get(str(self._lineage())):
            lines.append(_HISTORY_NOTE)
        return "\n".join(lines), fits

    def _update_lessons_status(self) -> None:
        text, _ = self._status_text()
        self.query_one("#round-lessons-status", PlainStatic).update(text)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if (event.checkbox.id or "").startswith("round-lesson-"):
            self._update_lessons_status()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "round-lineage":
            self._update_lessons_status()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "round-prompt" and self._lessons.lessons:
            self._update_lessons_status()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the action", severity="warning")
            return
        used: list[Decision] = []
        if self._lessons.lessons:
            used, _, _ = self._lesson_choice()
            _, fits = self._status_text()
            if not fits:
                # Refused, not dropped: the dialog showed these lessons as going
                # in, so running without them would not be what was confirmed.
                self.app.notify(
                    "this round's prompt leaves no room for the checked lessons; "
                    "uncheck some or shorten the prompt",
                    severity="warning",
                )
                return
        lineage = self._lineage()
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
                "lessons": [lesson.id for lesson in used],
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


class RoomJudgeModal(_CollabModal):
    """Record a human verdict on a room's applied change (#174), with an
    optional rationale. The verdict has no default, so a stray keypress cannot
    record one. Dumb like the other modals: the store validates the text."""

    _VERDICTS = [("accepted", "accepted"), ("rejected", "rejected")]

    def __init__(self, detail: str):
        super().__init__()
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="wide-dialog"):
            yield PlainLabel("Record your verdict on the applied change")
            with VerticalScroll(id="judge-body"):
                yield PlainStatic(self._detail)
                yield plain_select(
                    self._VERDICTS,
                    allow_blank=True,
                    prompt="verdict",
                    id="judge-verdict",
                )
                yield PlainLabel("Rationale (optional) — why you accept or reject it:")
                yield TextArea(id="judge-rationale", language=None)
            yield Checkbox("I reviewed this exact change", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button("Record", variant="warning", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        verdict = self.query_one("#judge-verdict", Select).value
        if verdict not in ("accepted", "rejected"):
            self.app.notify("choose accepted or rejected", severity="warning")
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the action", severity="warning")
            return
        self.dismiss(
            {
                "verdict": verdict,
                "rationale": self.query_one("#judge-rationale", TextArea).text,
            }
        )


class LessonPromoteModal(_CollabModal):
    """Promote a lesson the person writes from a judged room (#174).

    ``supersedable`` are ``(label, decision id, scope)`` for this project's
    active lessons. A replacement must have the same scope (the board refuses
    otherwise); the dialog checks that before closing so the error sits next
    to the fields."""

    def __init__(self, detail: str, supersedable: list[tuple[str, str, str]]):
        super().__init__()
        self._detail = detail
        self._supersedable = supersedable

    def compose(self) -> ComposeResult:
        with Vertical(classes="wide-dialog"):
            yield PlainLabel("Promote a lesson for reuse in this project's runs")
            with VerticalScroll(id="promote-body"):
                yield PlainStatic(self._detail)
                yield PlainLabel("Lesson (your own words, at most 1000 bytes):")
                yield TextArea(id="promote-lesson", language=None)
                yield PlainLabel(
                    "Scope: a goal (its board items and rooms created with it), "
                    "or blank for the whole project"
                )
                # Exact: the scope must equal a room's goal as typed, and a
                # plain Input keeps only a paste's first line.
                yield ExactInput(placeholder="blank = whole project", id="promote-scope")
                yield plain_select(
                    [
                        ("(replaces nothing)", ""),
                        *((label, decision_id) for label, decision_id, _ in self._supersedable),
                    ],
                    value="",
                    allow_blank=False,
                    id="promote-supersedes",
                )
            yield Checkbox("I reviewed this lesson and its source", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button("Promote", variant="warning", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the action", severity="warning")
            return
        scope = self.query_one("#promote-scope", ExactInput).value
        if has_control_characters(scope):
            # Kept open with the typed lesson: the coordinator would refuse it.
            self.app.notify(
                "a scope is one line: remove line breaks, tabs and other control "
                "characters",
                severity="warning",
            )
            return
        value = self.query_one("#promote-supersedes", Select).value
        supersedes = value if isinstance(value, str) and value else None
        if supersedes is not None:
            target_scope = next(
                (scope_ for _, decision_id, scope_ in self._supersedable
                 if decision_id == supersedes),
                None,
            )
            if target_scope != scope:
                self.app.notify(
                    "a lesson can only replace one of the same scope",
                    severity="warning",
                )
                return
        self.dismiss(
            {
                "lesson": self.query_one("#promote-lesson", TextArea).text,
                "scope": scope,
                "supersedes": supersedes,
            }
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


def render_room(
    room: dict,
    candidates: list[dict],
    events: list[dict],
    outcome: list[str] | tuple[str, ...] = (),
) -> str:
    members = ", ".join(_p(member["member_id"]) for member in room.get("members", []))
    tail = events[-5:]
    lines = [
        f"Room: {_p(room.get('id'))}",
        f"Prompt: {_prompt_first_line(room.get('prompt'))}",
        *([f"Goal: {_p(room['goal'])}"] if room.get("goal") is not None else []),
        f"State: {_p(room.get('state'))} | revision: {_p(room.get('revision'))}",
        f"Rounds: {_p(room.get('round_count'))} | members: {members}",
        f"Selected: {_p(room.get('selected_candidate_id') or '-')}",
        f"Apply run: {_p(room.get('apply_run_id') or '-')}",
        f"Applied run: {_p(room.get('applied_run_id') or '-')}",
        *outcome,
        "",
        "Recent events:",
        *(f"{_p(event['sequence'])} {_p(event['kind'])}" for event in tail),
        "",
        "x run round | e select candidate | g apply selected | j verdict | p promote",
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
