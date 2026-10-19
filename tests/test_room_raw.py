"""room_raw: explicit raw content in ``room export`` (#189).

Candidates are seeded through the production write path
(``RoomCandidateStore.save`` + ``RoomStore.add_candidate``); damage and
tampering cases then edit those real records, the test_room_compare recipe.
"""

from __future__ import annotations

import base64
import json
import stat
import subprocess
import types
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from markdown_it import MarkdownIt

from syncmill import room_raw
from syncmill.cli import cli
from syncmill.models import GateCheck, GateReport, TaskResult, TaskStatus
from syncmill.room_compare import _md, build_room_comparison, render_review_markdown
from syncmill.room_raw import (
    RawSpec,
    collect_raw,
    display_lines,
    parse_include,
    preview_token,
    render_markdown_section,
    scope_of,
)
from syncmill.rooms import RoomError, RoomStore

FP = "fp-1"
PATCH = "diff --git a/a.py b/a.py\n+line\n"
_GENERATED = "2026-10-19T00:00:00+00:00"


def _gate(*checks: tuple[str, bool, str]) -> GateReport:
    return GateReport.from_checks(
        [GateCheck(command=c, passed=p, exit_code=0 if p else 1, output=o) for c, p, o in checks]
    )


class Room:
    def __init__(self, root: Path, members: list[str], prompt: str = "the prompt"):
        self.store = RoomStore(root / "rooms")
        self.members = members
        self.prompt = prompt
        self.id = self.store.create_room(repo_root=root, prompt=prompt, agents=members)["id"]

    def start(self, prompt: str | None = None) -> dict:
        rnd = self.store.start_round(
            self.id,
            prompt=prompt or self.prompt,
            source_fingerprint=FP,
            snapshot_commit="base",
            lease_owner="worker",
            lease_timeout_seconds=3600,
            max_rounds=10,
        )
        self.store.init_round_members(
            self.id, rnd["id"], [{"member_id": m, "provider_id": m} for m in self.members]
        )
        return rnd

    def save(
        self,
        rnd: dict,
        member: str,
        *,
        patch: str | None = PATCH,
        output: str = "agent output\n",
        gate: GateReport | None = None,
    ) -> dict:
        result = TaskResult(
            task_id=uuid4(),
            agent_id=member,
            status=TaskStatus.COMPLETED,
            output=output,
            exit_code=0,
            duration_seconds=1.0,
        )
        manifest = self.store.candidates.save(
            candidate_id=str(uuid4()),
            room_id=self.id,
            round_id=rnd["id"],
            member_id=member,
            provider_id=member,
            attempt=1,
            result=result,
            attempt_state="completed",
            patch=patch,
            files_changed=["a.py"],
            gate=gate,
            base_commit="base",
            source_fingerprint=FP,
        )
        self.store.add_candidate(manifest)
        self.store.set_member_state(rnd["id"], member, "completed")
        return manifest

    def finish(self, rnd: dict) -> None:
        self.store.finish_round(self.id, rnd["id"])

    def compare(self, **kwargs):
        kwargs.setdefault("source_check", lambda: FP)
        return build_room_comparison(self.store, self.id, **kwargs)


@pytest.fixture
def make_room(tmp_path):
    stores: list[RoomStore] = []

    def factory(members: list[str], **kwargs) -> Room:
        room = Room(tmp_path / uuid4().hex, members, **kwargs)
        stores.append(room.store)
        return room

    yield factory
    for store in stores:
        store.close()


def _one(make_room, **save_kwargs):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", **save_kwargs)
    room.finish(rnd)
    return room, rnd, saved


def _edit_manifest(saved: dict, edit) -> None:
    path = Path(saved["artifact_path"]) / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _collect(room, specs, max_bytes=room_raw.DEFAULT_MAX_BYTES, **compare_kwargs):
    return collect_raw(room.store, room.compare(**compare_kwargs), specs, max_bytes)


# ------------------------------------------------------------- includes


def test_parse_include_canonicalizes_and_rejects():
    assert parse_include("patch:abc") == RawSpec("patch", "abc")
    assert parse_include("gate-output:abc") == RawSpec("gate-output", "abc")
    assert parse_include("prompt:01") == parse_include("prompt:1") == RawSpec("prompt", "1")
    for bad in ("patch", "diff:abc", "patch:", "prompt:0", "prompt:x", "prompt:-1", "prompt: 1"):
        with pytest.raises(ValueError):
            parse_include(bad)


# ------------------------------------------------------------- collection


def test_patch_and_output_are_the_stored_bytes_bound_to_the_row(make_room):
    room, rnd, saved = _one(make_room, output="hello\n")
    patch_item, output_item = _collect(
        room, [RawSpec("output", saved["candidate_id"]), RawSpec("patch", saved["candidate_id"])]
    )[::-1]
    assert (patch_item.kind, output_item.kind) == ("patch", "output")
    assert patch_item.head == PATCH.encode()
    assert patch_item.sha256 == saved["patch_sha256"] == sha256(PATCH.encode()).hexdigest()
    assert output_item.head == b"hello\n"
    assert output_item.sha256 == saved["output_sha256"]
    assert {patch_item.integrity, output_item.integrity} == {"sqlite-digest"}
    assert (patch_item.room_id, patch_item.round_id) == (room.id, rnd["id"])
    assert output_item.source_note == "stored provider output"
    assert not patch_item.truncated and patch_item.included_bytes == len(PATCH)


def test_output_truncated_at_save_is_noted(make_room):
    room, _, saved = _one(make_room)
    _edit_manifest(saved, lambda d: d.update(output_truncated=True))
    (item,) = _collect(room, [RawSpec("output", saved["candidate_id"])])
    assert item.source_note == "stored provider output, truncated at save"


def test_cap_truncates_the_head_but_digest_covers_everything(make_room):
    room, _, saved = _one(make_room)
    (item,) = _collect(room, [RawSpec("patch", saved["candidate_id"])], max_bytes=5)
    assert item.head == PATCH.encode()[:5]
    assert (item.included_bytes, item.size, item.truncated) == (5, len(PATCH), True)
    assert item.sha256 == saved["patch_sha256"]


def test_targets_outside_the_export_scope_are_refused(make_room):
    room = make_room(["a"])
    first = room.start()
    old = room.save(first, "a")
    room.finish(first)
    second = room.start()
    new = room.save(second, "a")
    room.finish(second)
    other, _, foreign = _one(make_room)

    # The default scope is the latest round only.
    (item,) = _collect(room, [RawSpec("patch", new["candidate_id"])])
    assert item.round_id == second["id"]
    for spec, message in (
        (RawSpec("patch", old["candidate_id"]), "not in this export"),
        (RawSpec("prompt", "1"), "round 1 is not in this export"),
        (RawSpec("patch", new["candidate_id"][:8]), "not in this export"),
        (RawSpec("patch", foreign["candidate_id"]), "not in this export"),
        (RawSpec("output", str(uuid4())), "not in this export"),
    ):
        with pytest.raises(RoomError, match=message):
            _collect(room, [spec])
    # --round 1 and --all-rounds bring them in.
    assert _collect(room, [RawSpec("patch", old["candidate_id"])], round_number=1)
    assert len(_collect(room, [RawSpec("prompt", "1"), RawSpec("prompt", "2")], all_rounds=True)) == 2


@pytest.mark.parametrize("column", ["round_id", "patch_sha256", "output_sha256"])
def test_candidate_row_changed_after_scope_resolution_is_refused(make_room, column):
    room, _, saved = _one(make_room)
    cmp = room.compare()
    # round_id is a foreign key: move the row to a real, different round.
    value = room.start()["id"] if column == "round_id" else "0" * 64
    room.store._conn.execute(
        f"UPDATE candidates SET {column}=? WHERE id=?", (value, saved["candidate_id"])
    )
    for kind in ("patch", "output", "gate-output"):
        with pytest.raises(RoomError, match="changed since the export resolved it"):
            collect_raw(room.store, cmp, [RawSpec(kind, saved["candidate_id"])], 100)


def test_tampered_artifact_bytes_are_refused(make_room):
    room, _, saved = _one(make_room)
    (Path(saved["artifact_path"]) / "candidate.patch").write_bytes(b"tampered\n")
    cmp = room.compare()
    with pytest.raises(RoomError, match="digest mismatch"):
        collect_raw(room.store, cmp, [RawSpec("patch", saved["candidate_id"])], 100)


def test_head_is_exactly_the_bytes_hashed_even_if_the_file_changes_later(
    make_room, monkeypatch
):
    room, _, saved = _one(make_room)
    path = Path(saved["artifact_path"]) / "candidate.patch"
    real = RoomStore._validate_candidate_bindings

    def mutate_then_validate(self, candidate_id, row, artifact):
        # After the stream, before the binding check: the reported digest and
        # the returned head must still describe the bytes that were read.
        path.write_bytes(b"changed underneath\n")
        return real(self, candidate_id, row, artifact)

    monkeypatch.setattr(RoomStore, "_validate_candidate_bindings", mutate_then_validate)
    read = room.store.load_candidate_raw_head(
        saved["candidate_id"],
        "patch",
        1000,
        expected={
            "room_id": room.id,
            "round_id": saved["round_id"],
            "patch_sha256": saved["patch_sha256"],
            "output_sha256": saved["output_sha256"],
        },
    )
    assert read["head"] == PATCH.encode()
    assert sha256(read["head"]).hexdigest() == read["sha256"] == saved["patch_sha256"]


def test_prompt_is_read_by_round_id_and_bound_to_its_number(make_room):
    room = make_room(["a"], prompt="first prompt")
    rnd = room.start(prompt="round one\twith tab")
    room.save(rnd, "a")
    room.finish(rnd)
    (item,) = _collect(room, [RawSpec("prompt", "1")])
    assert item.head == "round one\twith tab".encode()
    assert (item.integrity, item.round_id) == ("room-database", rnd["id"])
    assert "not part of this text" in item.source_note

    cmp = room.compare()
    room.store._conn.execute("UPDATE rounds SET number=7 WHERE id=?", (rnd["id"],))
    with pytest.raises(RoomError, match="round 1 changed since the export resolved it"):
        collect_raw(room.store, cmp, [RawSpec("prompt", "1")], 100)


# ------------------------------------------------------------- gate output


def test_gate_checks_are_distinct_items_carrying_index_and_command(make_room):
    room, _, saved = _one(
        make_room, gate=_gate(("pytest -q", True, "same"), ("ruff check", True, "same"))
    )
    items = _collect(room, [RawSpec("gate-output", saved["candidate_id"])])
    assert [(i.check, i.command, i.head) for i in items] == [
        (1, "pytest -q", b"same"),
        (2, "ruff check", b"same"),
    ]
    assert {i.integrity for i in items} == {"manifest-record"}
    assert items[0].descriptor() != items[1].descriptor()


def test_changing_a_check_command_changes_the_token(make_room):
    room, _, saved = _one(make_room, gate=_gate(("pytest -q", True, "out")))
    spec = [RawSpec("gate-output", saved["candidate_id"])]
    cmp = room.compare()
    before = preview_token(room.id, scope_of(cmp, all_rounds=False), 100, _collect(room, spec))

    def edit(data):
        data["gate"]["checks"][0]["command"] = "pytest -x"

    _edit_manifest(saved, edit)
    after = preview_token(room.id, scope_of(cmp, all_rounds=False), 100, _collect(room, spec))
    assert before != after


def _wrong_state(data):
    data["state"] = "failed"


def _contradicts_eligibility(data):
    # Structurally valid failing gate on an eligible (passing) candidate.
    data["gate"]["passed"] = False
    data["gate"]["score"] = 0.0
    data["gate"]["checks"][0]["passed"] = False


def _inconsistent_verdict(data):
    data["gate"]["passed"] = False


def _null_output(data):
    data["gate"]["checks"][0]["output"] = None


def _checks_not_list(data):
    data["gate"]["checks"] = {"0": "x"}


@pytest.mark.parametrize(
    "edit",
    [None, _wrong_state, _contradicts_eligibility, _inconsistent_verdict, _null_output, _checks_not_list],
)
def test_gate_output_is_accepted_exactly_when_the_comparison_accepts_the_gate(
    make_room, edit
):
    room, _, saved = _one(make_room, gate=_gate(("pytest -q", True, "out")))
    if edit is not None:
        _edit_manifest(saved, edit)
    cmp = room.compare()
    (row,) = cmp.groups[0].rows
    spec = [RawSpec("gate-output", saved["candidate_id"])]
    if row.gate.verdict in ("passed", "failed"):
        assert collect_raw(room.store, cmp, spec, 100)
    else:
        with pytest.raises(RoomError, match="no usable gate record"):
            collect_raw(room.store, cmp, spec, 100)
    if edit is not None:
        assert row.gate.verdict == "unknown"


def _alter_patch(saved):
    (Path(saved["artifact_path"]) / "candidate.patch").write_bytes(b"altered\n")


def _delete_output(saved):
    (Path(saved["artifact_path"]) / "output.txt").unlink()


def _alter_output(saved):
    (Path(saved["artifact_path"]) / "output.txt").write_bytes(b"altered\n")


@pytest.mark.parametrize("damage", [_alter_patch, _delete_output, _alter_output])
def test_gate_output_of_an_unreadable_artifact_is_refused(make_room, damage):
    # The comparison reads a gate only from an artifact whose patch and output
    # both verify; raw export must not show checks of one it reads "unknown".
    room, _, saved = _one(make_room, gate=_gate(("pytest -q", True, "out")))
    damage(saved)
    cmp = room.compare()
    assert cmp.groups[0].rows[0].gate.verdict == "unknown"
    with pytest.raises(RoomError, match="corrupt candidate artifact|digest mismatch"):
        collect_raw(room.store, cmp, [RawSpec("gate-output", saved["candidate_id"])], 100)


def test_no_gate_report_is_refused_for_gate_output(make_room):
    room, _, saved = _one(make_room)
    with pytest.raises(RoomError, match="no usable gate record: no gate report"):
        _collect(room, [RawSpec("gate-output", saved["candidate_id"])])


def test_gate_report_with_zero_checks_is_refused_not_an_empty_include(make_room):
    # GateReport.from_checks([]) passes (all of nothing), and _gate_verdict
    # accepts it; an include that yields no item must still be refused, or it
    # would export nothing and leave the token unchanged by its presence.
    room, _, saved = _one(make_room, gate=GateReport.from_checks([]))
    assert room.compare().groups[0].rows[0].gate.verdict == "passed"
    cid = saved["candidate_id"]
    with pytest.raises(RoomError, match="gate report with no checks"):
        _collect(room, [RawSpec("gate-output", cid)])
    with pytest.raises(RoomError, match="gate report with no checks"):
        _collect(room, [RawSpec("patch", cid), RawSpec("gate-output", cid)])


@pytest.mark.parametrize("field", ["output", "command"])
def test_lone_surrogate_in_gate_text_is_refused(make_room, field):
    room, _, saved = _one(make_room, gate=_gate(("pytest -q", True, "out")))
    path = Path(saved["artifact_path"]) / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["gate"]["checks"][0][field] = "PLACEHOLDER"
    # json.dumps would refuse to emit the surrogate itself; write it escaped.
    path.write_text(json.dumps(data).replace("PLACEHOLDER", "\\ud800"), encoding="utf-8")
    with pytest.raises(RoomError, match="gate check 1 is not valid Unicode text"):
        _collect(room, [RawSpec("gate-output", saved["candidate_id"])])


# ------------------------------------------------------------- token


def test_token_is_stable_and_covers_includes_scope_and_cap(make_room):
    room, _, saved = _one(make_room, gate=_gate(("pytest -q", True, "out")))
    cid = saved["candidate_id"]
    cmp = room.compare()
    scope = scope_of(cmp, all_rounds=False)

    def token(specs, cap=100, sc=scope):
        return preview_token(room.id, sc, cap, collect_raw(room.store, cmp, specs, cap))

    base = token([RawSpec("patch", cid), RawSpec("output", cid)])
    assert base == token([RawSpec("output", cid), RawSpec("patch", cid), RawSpec("patch", cid)])
    assert base != token([RawSpec("patch", cid)])
    assert base != token([RawSpec("patch", cid), RawSpec("output", cid)], cap=101)
    assert base != token([RawSpec("patch", cid), RawSpec("output", cid)], sc={"all_rounds": True})


def test_token_golden_vector():
    item = room_raw.RawItem(
        kind="patch",
        target="c1",
        check=None,
        command=None,
        room_id="r1",
        round_id="rd1",
        sha256="0" * 64,
        size=3,
        included_bytes=3,
        truncated=False,
        integrity="sqlite-digest",
        source_note="stored candidate patch",
        head=b"abc",
    )
    canonical = (
        '{"cap":10,"items":[{"check":null,"command":null,"included_bytes":3,'
        '"integrity":"sqlite-digest","kind":"patch","room_id":"r1","round_id":"rd1",'
        '"sha256":"' + "0" * 64 + '","size":3,"source_note":"stored candidate patch",'
        '"target":"c1","truncated":false}],"room":"r1","scope":{"round":1},"v":1}'
    )
    assert preview_token("r1", {"round": 1}, 10, [item]) == sha256(canonical.encode()).hexdigest()


# ------------------------------------------------------------- display rendering


@pytest.mark.parametrize(
    ("data", "lines", "ends"),
    [
        (b"", [], True),
        (b"a", ["a"], False),
        (b"a\n", ["a"], True),
        (b"a\n\n", ["a", ""], True),
        (b"x\ty\n", ["x\ty"], True),
        (b"\x1b[31mred\r\n", ["\\x1b[31mred\\x0d"], True),
        (b"nul\x00 bidi\xe2\x80\xae end", ["nul\\x00 bidi\\u202e end"], False),
        (b"bad \xff\xfe utf8", ["bad \\xff\\xfe utf8"], False),
        ("café".encode()[:4], ["caf\\xc3"], False),
        ("line sep".encode(), ["line\\u2028sep"], False),
    ],
)
def test_display_lines(data, lines, ends):
    assert display_lines(data) == (lines, ends)


def _section_markdown(room, specs, max_bytes=room_raw.DEFAULT_MAX_BYTES):
    cmp = room.compare()
    items = collect_raw(room.store, cmp, specs, max_bytes)
    return render_review_markdown(
        cmp, generated_at=_GENERATED, appendix=render_markdown_section(items, "f" * 64)
    )


_INJECTED_OUTPUT = (
    "reviewed title\n===\n# h\n- item\n| a | [x](http://e) <b>\n"
    "https://x.invalid www.x.com a@b.com\n``````````\n\x1b]52;c;AAAA\x07\r\n\n\n"
)
_BENIGN_OUTPUT = "plain one\nplain two\nplain three\nplain four\nplain five\nplain six\nplain seven\nplain eight\n\n\n"


def test_raw_markdown_section_forms_no_structure_and_keeps_content(make_room):
    benign_room, _, benign = _one(make_room, output=_BENIGN_OUTPUT)
    injected_room, _, injected = _one(make_room, output=_INJECTED_OUTPUT)
    benign_md = _section_markdown(benign_room, [RawSpec("output", benign["candidate_id"])])
    injected_md = _section_markdown(injected_room, [RawSpec("output", injected["candidate_id"])])
    parse = MarkdownIt("gfm-like").parse
    benign_tokens, injected_tokens = parse(benign_md), parse(injected_md)
    assert [t.type for t in injected_tokens] == [t.type for t in benign_tokens]
    for token in injected_tokens:
        assert not token.type.startswith("html"), token
        for child in token.children or ():
            assert child.type == "text", (child.type, child.content)
    expected_lines, _ = display_lines(_INJECTED_OUTPUT.encode())
    content = [t for t in injected_tokens if t.type == "fence"][-1]
    assert content.content == "\n".join(expected_lines) + "\n"
    assert content.markup == "`" * 11
    assert "\x1b" not in injected_md and "\x07" not in injected_md and "\r" not in injected_md


@pytest.mark.parametrize(
    ("output", "body", "no_newline_marker"),
    [("", "", False), ("a", "a\n", True), ("a\n", "a\n", False), ("a\n\n", "a\n\n", False)],
)
def test_raw_block_body_is_exact_through_the_final_composition(
    make_room, output, body, no_newline_marker
):
    room, _, saved = _one(make_room, output=output)
    text = _section_markdown(room, [RawSpec("output", saved["candidate_id"])])
    fence = [t for t in MarkdownIt("gfm-like").parse(text) if t.type == "fence"][-1]
    assert fence.content == body
    assert (_md("[no newline at end of content]") in text) is no_newline_marker


def test_markdown_gate_blocks_show_index_and_command_and_truncation(make_room):
    room, _, saved = _one(
        make_room, gate=_gate(("pytest -q", True, "same"), ("ruff‮check", True, "same"))
    )
    text = _section_markdown(room, [RawSpec("gate-output", saved["candidate_id"])], max_bytes=2)
    fences = [t.content for t in MarkdownIt("gfm-like").parse(text) if t.type == "fence"]
    assert "pytest -q\n" in fences and "ruff\\u202echeck\n" in fences
    assert "| Check | 1 |" in text and "| Check | 2 |" in text
    assert text.count(_md("[truncated: 2 of 4 bytes shown]")) == 2
    assert "‮" not in text


# ------------------------------------------------------------- JSON


def _git_binary_patch(tmp_path: Path) -> str:
    repo = tmp_path / "bin-repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    (repo / "blob.bin").write_bytes(bytes(range(256)))
    run("add", "blob.bin")
    return subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=repo, check=True, capture_output=True
    ).stdout.decode("utf-8", errors="surrogateescape")


@pytest.mark.parametrize("kind", ["non_utf8", "git_binary"])
def test_json_carries_the_exact_bytes(make_room, tmp_path, kind):
    patch = (
        "diff --git a/x b/x\n+caf\udce9 \udcff\n"
        if kind == "non_utf8"
        else _git_binary_patch(tmp_path)
    )
    room, _, saved = _one(make_room, patch=patch)
    (item,) = _collect(room, [RawSpec("patch", saved["candidate_id"])])
    section = room_raw.json_section([item], "f" * 64)
    data = base64.b64decode(section["items"][0]["data"])
    assert data == patch.encode("utf-8", errors="surrogateescape")
    assert sha256(data).hexdigest() == section["items"][0]["sha256"] == saved["patch_sha256"]
    assert section["warning"] == room_raw.WARNING


# ------------------------------------------------------------- CLI


def _cli_room(tmp_path, monkeypatch, **save_kwargs):
    room = Room(tmp_path / "cli", ["claude"])
    rnd = room.start()
    saved = room.save(rnd, "claude", **save_kwargs)
    room.finish(rnd)
    coordinator = types.SimpleNamespace(
        store=room.store, repo_root=tmp_path, close=lambda: None
    )
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), coordinator),
    )
    monkeypatch.setattr("syncmill.cli.repository_fingerprint", lambda root: FP)
    return room, saved


@pytest.fixture
def runner():
    return CliRunner()


@pytest.mark.parametrize(
    "argv",
    [
        ["--preview"],
        ["--confirm", "a" * 64],
        ["--raw-max-bytes", "10"],
        ["--include", "patch:x"],
        ["--include", "patch:x", "--preview", "--confirm", "a" * 64],
        ["--include", "patch:x", "--preview", "--output", "out.md"],
    ],
)
def test_raw_flag_combinations_are_usage_errors_before_opening(monkeypatch, runner, argv):
    def boom(repo):
        raise AssertionError("must not open the store")

    monkeypatch.setattr("syncmill.cli._room_coordinator", boom)
    res = runner.invoke(cli, ["room", "export", "r", *argv])
    assert res.exit_code == 2, res.output


@pytest.mark.parametrize(
    "argv",
    [
        ["--include", "patch:x", "--confirm", "abc"],
        ["--include", "patch:x", "--confirm", "A" * 64],
        ["--include", "patch:x", "--confirm", "g" * 64],
        ["--include", "diff:x", "--preview"],
        ["--include", "prompt:0", "--preview"],
    ],
)
def test_malformed_token_or_include_is_a_bad_parameter(monkeypatch, runner, argv):
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (_ for _ in ()).throw(AssertionError("must not open the store")),
    )
    res = runner.invoke(cli, ["room", "export", "r", *argv])
    assert res.exit_code == 2
    assert "Invalid value" in res.output


def _token(output: str) -> str:
    (line,) = [line for line in output.splitlines() if line.startswith("Token: ")]
    return line.removeprefix("Token: ")


def test_preview_then_confirm_writes_the_section(monkeypatch, runner, tmp_path):
    room, saved = _cli_room(tmp_path, monkeypatch, output="hello\n")
    include = ["--include", f"output:{saved['candidate_id']}", "--include", "prompt:1"]
    preview = runner.invoke(cli, ["room", "export", room.id, *include, "--preview"])
    assert preview.exit_code == 0, preview.output
    assert "2 item(s), " in preview.stdout
    assert "WARNING: " + room_raw.WARNING in preview.stdout
    assert "    | hello" in preview.stdout
    assert "To write: rerun the same command with --preview replaced by --confirm " in preview.stdout
    assert "--repo" not in preview.stdout
    token = _token(preview.stdout)

    target = tmp_path / "review.md"
    res = runner.invoke(
        cli,
        ["room", "export", room.id, *include, "--confirm", token, "--output", str(target)],
    )
    assert res.exit_code == 0, res.output
    assert res.stdout == ""
    assert f"Raw content confirmed with token {token}" in res.stderr
    assert "WARNING: " in res.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    text = target.read_text(encoding="utf-8")
    assert "## Raw content (explicitly included)" in text
    assert f"Confirmed with token: {token}" in text


def test_confirm_json_adds_only_raw_content_and_is_ascii(monkeypatch, runner, tmp_path):
    # Stored metadata (a gate command) carries non-ASCII and a C1 control, so
    # the comparison part of the document is what ensure_ascii must escape.
    room, saved = _cli_room(
        tmp_path,
        monkeypatch,
        output="café \u009b\n",
        gate=_gate(("pytest café \u009b", True, "o")),
    )
    include = ["--include", f"output:{saved['candidate_id']}", "--format", "json"]
    token = _token(runner.invoke(cli, ["room", "export", room.id, *include, "--preview"]).stdout)
    res = runner.invoke(cli, ["room", "export", room.id, *include, "--confirm", token])
    assert res.exit_code == 0, res.output
    assert res.stdout.isascii()
    document = json.loads(res.stdout)
    raw = document.pop("raw_content")
    compare = runner.invoke(cli, ["room", "compare", room.id, "--json"])
    assert document == json.loads(compare.stdout)
    assert raw["token"] == token
    assert base64.b64decode(raw["items"][0]["data"]) == "café \u009b\n".encode()


def _tamper_gate_output(room, saved):
    _edit_manifest(saved, lambda d: d["gate"]["checks"][0].update(output="changed"))


def _tamper_prompt(room, saved):
    room.store._conn.execute("UPDATE rounds SET prompt='changed' WHERE room_id=?", (room.id,))


def _tamper_delete_output(room, saved):
    (Path(saved["artifact_path"]) / "output.txt").unlink()


@pytest.mark.parametrize("tamper", [_tamper_gate_output, _tamper_prompt, _tamper_delete_output])
def test_content_changed_after_preview_is_refused_and_nothing_written(
    monkeypatch, runner, tmp_path, tamper
):
    room, saved = _cli_room(tmp_path, monkeypatch, gate=_gate(("pytest -q", True, "out")))
    include = [
        "--include", f"gate-output:{saved['candidate_id']}", "--include", "prompt:1",
    ]
    token = _token(runner.invoke(cli, ["room", "export", room.id, *include, "--preview"]).stdout)
    tamper(room, saved)
    target = tmp_path / "review.md"
    for extra in ([], ["--output", str(target)]):
        res = runner.invoke(cli, ["room", "export", room.id, *include, "--confirm", token, *extra])
        assert res.exit_code == 1
        # A changed record fails the token; a damaged artifact fails earlier,
        # at collection. Either way nothing is printed or written.
        assert "run --preview again" in res.output or "corrupt candidate artifact" in res.output
        assert res.stdout == ""
    assert not target.exists()


def test_confirm_with_different_cap_or_includes_is_refused(monkeypatch, runner, tmp_path):
    room, saved = _cli_room(tmp_path, monkeypatch)
    include = ["--include", f"patch:{saved['candidate_id']}"]
    token = _token(runner.invoke(cli, ["room", "export", room.id, *include, "--preview"]).stdout)
    for argv in (
        [*include, "--raw-max-bytes", "3"],
        [*include, "--include", f"output:{saved['candidate_id']}"],
    ):
        res = runner.invoke(cli, ["room", "export", room.id, *argv, "--confirm", token])
        assert res.exit_code == 1 and res.stdout == "", res.output
    ok = runner.invoke(cli, ["room", "export", room.id, *include, "--confirm", token])
    assert ok.exit_code == 0, ok.output


def test_preview_metadata_cannot_drive_the_terminal(monkeypatch, runner, tmp_path):
    room, saved = _cli_room(
        tmp_path, monkeypatch, gate=_gate(("pytest \x1b]0;t\x07 \u009b‮", True, "o\x1bk"))
    )
    res = runner.invoke(
        cli,
        ["room", "export", room.id, "--include", f"gate-output:{saved['candidate_id']}", "--preview"],
    )
    assert res.exit_code == 0, res.output
    for ch in ("\x1b", "\x07", "\u009b", "‮"):
        assert ch not in res.stdout
