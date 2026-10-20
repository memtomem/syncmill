from __future__ import annotations

import json
import types

import click
import pytest
from click.testing import CliRunner

from syncmill.artifacts import ApplyReconciliation, ArtifactStore, printable_name, printable_text
from syncmill.board import Evidence, WorkItem, WorkState, render_board, render_item
from syncmill.cli import _first_line, _reconciliation_line, _run_apply, cli
from syncmill.monitor import render_result

# What an agent, a provider or an import can put in a stored string: a C1 CSI,
# an OSC sequence (ESC ... BEL), a bidi override and a CR that would overwrite
# the line. Korean must stay readable.
HOSTILE = "evil\u009b2J\x1b]52;c;QQ==\x07\u202etxt\rforged 첫 실행"
RAW = ("\u009b", "\x1b", "\x07", "\u202e", "\r")


def assert_display_safe(text: str) -> None:
    assert not any(ch in text for ch in RAW), ascii(text)
    assert "첫 실행" in text


# ------------------------------------------------------------ printable_text


def test_printable_text_keeps_lines_and_tabs_and_escapes_the_rest():
    text = printable_text("line one\n\tindented " + HOSTILE + "\nlast\n")
    assert_display_safe(text)
    assert text.split("\n")[:2] == ["line one", "\tindented " + printable_name(HOSTILE)]
    assert text.endswith("\nlast\n")
    assert "\\x9b" in text and "\\u202e" in text and "\\x0d" in text


@pytest.mark.parametrize("ch", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\u200e", "\ufeff"])
def test_printable_text_leaves_lf_as_the_only_line_break(ch):
    assert printable_text(f"a{ch}b").splitlines() == [f"a{printable_name(ch)}b"]


def test_printable_text_shows_invalid_utf8_bytes_as_hex():
    value = b"ok \xff\xfe".decode("utf-8", errors="surrogateescape")
    assert printable_text(value) == "ok \\xff\\xfe"


def test_printable_text_matches_printable_name_on_one_line():
    for value in (HOSTILE, "plain", "첫 실행 ✓ 🚀", "\x00\x7f\xad"):
        assert printable_text(value) == printable_name(value)


# ---------------------------------------------------------------- run results


def _render(data: dict) -> str:
    @click.command()
    def show():
        render_result(data)

    return CliRunner().invoke(show).output


def test_a_completed_result_escapes_file_names_and_output():
    out = _render(
        {
            "status": "completed", "agent_id": "codex", "duration_seconds": 1.0,
            "files_changed": [HOSTILE, "ok.txt"], "output": "first\n" + HOSTILE,
        }
    )
    assert_display_safe(out)
    assert f"[syncmill] Files changed: {printable_name(HOSTILE)}, ok.txt" in out
    assert "\nfirst\n" in out  # the output's own lines survive


def test_a_failed_result_escapes_the_error():
    out = _render({"status": "failed", "error": "provider said\n" + HOSTILE})
    assert_display_safe(out)
    assert "provider said\n" in out


# ------------------------------------------------------------------- run ids


def _store_with_run(tmp_path, run_id: str, manifest: dict) -> ArtifactStore:
    store = ArtifactStore(tmp_path / "store", "project")
    run_dir = store.root / run_id
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    return store


def test_run_listings_escape_run_and_session_ids(monkeypatch, tmp_path):
    run_id = "run-\u009b2J\u202e"
    store = _store_with_run(
        tmp_path, run_id,
        {"state": "pending", "agent_id": "codex", "provider_session_id": HOSTILE},
    )
    monkeypatch.setattr("syncmill.cli._project_artifacts", lambda repo: (tmp_path, None, store))
    runner = CliRunner()
    for args in (["runs", "list"], ["conversations", "list"]):
        res = runner.invoke(cli, args)
        assert res.exit_code == 0, res.output
        assert printable_name(run_id) in res.output
        assert not any(ch in res.output for ch in RAW), (args, ascii(res.output))
    res = runner.invoke(cli, ["conversations", "list"])
    assert printable_name(HOSTILE) in res.output


def test_run_listings_escape_manifest_fields(monkeypatch, tmp_path):
    store = _store_with_run(
        tmp_path, "run-1",
        {"state": "pending" + HOSTILE, "agent_id": "codex" + HOSTILE,
         "created_at": "now" + HOSTILE, "provider_session_id": "s-1"},
    )
    monkeypatch.setattr("syncmill.cli._project_artifacts", lambda repo: (tmp_path, None, store))
    for args in (["runs", "list"], ["conversations", "list"]):
        res = CliRunner().invoke(cli, args, color=True)
        assert res.exit_code == 0, res.output
        assert not any(ch in res.output for ch in RAW), (args, ascii(res.output))
        assert printable_name("codex" + HOSTILE) in res.output


@pytest.mark.parametrize(
    "args", [["discard"], ["conversations", "close"], ["runs", "show"], ["diff"]]
)
def test_error_messages_escape_the_id_they_name(monkeypatch, tmp_path, args):
    store = ArtifactStore(tmp_path / "store", "project")
    monkeypatch.setattr("syncmill.cli._project_artifacts", lambda repo: (tmp_path, None, store))
    res = CliRunner().invoke(cli, [*args, "run-\u009b2J\x1b]52;c;QQ==\x07"], color=True)
    assert res.exit_code == 1
    assert "Error: unknown or corrupt run: run-\\x9b2J\\x1b]52;c;QQ==\\x07" in res.output
    assert not any(ch in res.output for ch in RAW), ascii(res.output)


@pytest.mark.parametrize("args", [["--bad\x1b]52;c;QQ\x07"], ["runs", "list", "--bad\x1b]52;c;QQ\x07"]])
def test_option_parsing_errors_are_escaped_at_the_root_too(args):
    # Root options are parsed before the group invokes anything.
    res = CliRunner().invoke(cli, args, color=True)
    assert res.exit_code == 2
    assert "\x1b" not in res.output and "\x07" not in res.output, ascii(res.output)
    assert "--bad\\x1b]52;c;QQ\\x07" in res.output


def test_a_multiline_error_message_keeps_its_lines(monkeypatch):
    def boom(repo):
        raise click.ClickException("first line\u202e\nRun `syncmill recover x` next.")

    monkeypatch.setattr("syncmill.cli._project_artifacts", boom)
    res = CliRunner().invoke(cli, ["runs", "list"])
    assert res.exit_code == 1
    assert res.output == "Error: first line\\u202e\nRun `syncmill recover x` next.\n"


def test_run_id_confirmations_escape_the_id(monkeypatch, tmp_path):
    run_id = "run-\u009b2J"
    calls = []
    store = types.SimpleNamespace(
        discard=lambda rid: calls.append(("discard", rid)),
        close_conversation=lambda rid: calls.append(("close", rid)),
        prune=lambda keep, dry_run: [run_id],
    )
    monkeypatch.setattr("syncmill.cli._project_artifacts", lambda repo: (tmp_path, None, store))
    runner = CliRunner()
    for args in (
        ["discard", run_id], ["conversations", "close", run_id], ["runs", "prune", "--keep", "1"]
    ):
        res = runner.invoke(cli, args)
        assert res.exit_code == 0, (args, res.output)
        assert "\u009b" not in res.output and printable_name(run_id) in res.output, args
    assert calls == [("discard", run_id), ("close", run_id)]


def test_apply_escapes_the_run_id_and_its_receipt(monkeypatch, tmp_path):
    run_id = "run-\u009b2J"
    receipt = tmp_path / run_id / "applications" / "r.json"
    store = types.SimpleNamespace(
        reconcile_interrupted_applies=lambda run_id=None: [],
        load=lambda rid: types.SimpleNamespace(manifest={}),
        apply=lambda *a, **k: receipt,
    )
    monkeypatch.setattr("syncmill.cli.repository_fingerprint", lambda root: "fp")
    monkeypatch.setattr("syncmill.cli._heal_workflow_approval", lambda *a, **k: None)
    config = types.SimpleNamespace(
        gate=types.SimpleNamespace(commands=[], timeout_seconds=1),
        workspace=types.SimpleNamespace(root=str(tmp_path)),
    )

    @click.command()
    def apply():
        _run_apply(store, tmp_path, config, run_id, False)

    out = CliRunner().invoke(apply).output
    assert "\u009b" not in out
    assert f"Applied run {printable_name(run_id)}" in out
    assert f"Receipt: {printable_name(str(receipt))}" in out


@pytest.mark.parametrize(
    "outcome", ["completed", "not_applied", "manual_recovery", "skipped_live_owner", "error"]
)
def test_reconciliation_lines_escape_id_receipt_and_detail(outcome):
    report = ApplyReconciliation(
        run_id="run-\u009b2J", outcome=outcome, detail=HOSTILE,
        receipt="/runs/run-\u009b2J/applications/r.json",
    )
    line = _reconciliation_line(report)
    assert not any(ch in line for ch in RAW), ascii(line)
    assert printable_name("run-\u009b2J") in line


# ----------------------------------------------------------------- prompts


def test_first_line_escapes_bidi_controls_too():
    line = _first_line("fine\u202eevil prompt\nsecond")
    assert "\u202e" not in line and "\\u202e" in line
    assert "second" not in line


# ------------------------------------------------------------------- board


def _hostile_item() -> WorkItem:
    return WorkItem(
        id="11111111-2222-4333-8444-555555555555",
        goal="goal" + HOSTILE,
        task="import a::\u202eb then\nforged: line " + HOSTILE,
        state=WorkState.BLOCKED,
        owner_agent="codex" + HOSTILE,
        inputs=["in" + HOSTILE],
        outputs=["out\n" + HOSTILE],
        evidence=[
            Evidence(kind="manual", passed=True, ref="ref" + HOSTILE, at=0.0, by="by" + HOSTILE)
        ],
        handoff="next\n" + HOSTILE,
        blocked_reason="why" + HOSTILE,
        depends_on=["dep" + HOSTILE],
    )


def test_board_item_view_escapes_every_free_text_field():
    transitions = [
        {"from_state": "planned", "to_state": "blocked", "actor": "agent" + HOSTILE,
         "note": "note\n" + HOSTILE},
    ]
    text = render_item(_hostile_item(), transitions)
    assert_display_safe(text)
    lines = text.splitlines()
    # An embedded LF cannot forge a field line.
    assert not any(line.startswith("forged") for line in lines)
    assert len(lines) == 14  # 10 fields, evidence + 1, history + 1
    for label in ("goal:", "task:", "owner:", "depends:", "inputs:", "outputs:",
                  "handoff:", "blocked:"):
        assert sum(line.startswith(label) for line in lines) == 1, label


def test_board_listing_escapes_task_and_goal_before_truncating():
    line = render_board([_hostile_item()])
    assert not any(ch in line for ch in RAW), ascii(line)
    assert "\n" not in line
    assert "\\u202e" in line and "...  (deps=1)" in line

