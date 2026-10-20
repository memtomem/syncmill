from __future__ import annotations

import json
import types

import click
import pytest
from click.testing import CliRunner

from syncmill.artifacts import ApplyReconciliation, ArtifactStore, printable_name, printable_text
from syncmill.board import (
    BoardStats,
    Decision,
    Evidence,
    WorkItem,
    WorkState,
    render_board,
    render_board_stats,
    render_decisions,
    render_item,
)
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


# A stored id or state is not validated on these paths; an LF in one must not
# start a line of its own (#198).
FORGED = "x\n[syncmill] forged line" + HOSTILE


def test_board_ids_and_history_states_cannot_forge_a_line():
    item = _hostile_item()
    item.id = "id" + FORGED
    transitions = [{"from_state": "s" + FORGED, "to_state": "t" + FORGED, "actor": "a"}]
    listing = render_board([item])
    view = render_item(item, transitions)
    for text in (listing, view):
        assert_display_safe(text)
        assert not any(line.startswith("[syncmill]") for line in text.splitlines())
    assert "\n" not in listing
    assert len(view.splitlines()) == 14
    assert view.splitlines()[0] == "id:       " + printable_name("id" + FORGED)
    history = view.splitlines()[-1]
    assert history.startswith("  " + printable_name("s" + FORGED) + " → ")


def test_board_decisions_escape_id_scope_and_text():
    decisions = [Decision(id="d" + FORGED, text="t" + FORGED, scope="g" + FORGED),
                 Decision(id="d2", text="plain")]
    text = render_decisions(decisions)
    assert_display_safe(text)
    assert text.splitlines() == [
        "  " + printable_name("d" + FORGED) + "  [" + printable_name("g" + FORGED) + "] "
        + printable_name("t" + FORGED),
        "  d2  [global] plain",
    ]


def test_board_stats_escape_the_ids_they_list():
    stats = BoardStats(
        total=1, by_state={"blocked": 1}, false_done=["f" + FORGED], redispatched=[],
        reclaimed_stale=0, blocked_aged=["b" + FORGED], human_required_aged=[],
        dropped_handoffs=["h" + FORGED], stall_seconds=60,
    )
    text = render_board_stats(stats)
    assert_display_safe(text)
    assert len(text.splitlines()) == 8
    assert "(" + printable_name("f" + FORGED) + ")" in text


def test_board_listing_escapes_task_and_goal_before_truncating():
    line = render_board([_hostile_item()])
    assert not any(ch in line for ch in RAW), ascii(line)
    assert "\n" not in line
    assert "\\u202e" in line and "...  (deps=1)" in line



# ------------------------------------------------ one-line fields (#198, LF)


def _no_forged_line(text: str) -> None:
    assert_display_safe(text)
    assert not any(line.lstrip().startswith("[syncmill] forged") for line in text.splitlines())


def test_report_show_escapes_every_envelope_field():
    from syncmill.report import render_report

    def doc(value):
        return {
            "run_id": value, "created_at": value, "finished_at": value, "strategy": value,
            "team": value, "task_id": value, "agents": [value, value],
            "board": {"item_id": value, "goal": value},
            "result": {k: value for k in ("status", "agent_id", "winner", "exit_code",
                                          "duration_seconds", "gate_passed",
                                          "files_changed_count", "artifact", "digest",
                                          "patch_digest")},
            "preflight": {"status": value},
            "trace": {"status": value, "stream": {"status": value,
                                                  "exported_span_count": value,
                                                  "dropped_span_count": value}},
            "degraded": [value, value],
        }

    plain, hostile = render_report(doc("v")), render_report(doc("v" + FORGED))
    _no_forged_line(hostile)
    assert len(hostile.splitlines()) == len(plain.splitlines())
    assert hostile.splitlines()[0] == "Run:        " + printable_name("v" + FORGED)


def test_a_completed_result_escapes_agent_duration_and_gate_summary():
    out = _render({"status": "completed", "agent_id": "a" + FORGED,
                   "duration_seconds": "1" + FORGED, "gate_summary": "g" + FORGED,
                   "output": "body"})
    _no_forged_line(out)
    lines = out.splitlines()
    assert lines[0] == (f"[{printable_name('a' + FORGED)}] Completed in "
                        f"{printable_name('1' + FORGED)}s")
    assert lines[1] == f"[syncmill] Quality gate: passed ({printable_name('g' + FORGED)})"


@pytest.mark.parametrize("writable", [True, False])
def test_emit_patch_escapes_the_export_path(tmp_path, writable):
    from syncmill.monitor import emit_patch_file

    parent = tmp_path / ("d" if writable else "missing")
    parent.mkdir(exist_ok=True) if writable else None
    path = parent / ("p" + FORGED)

    @click.command()
    def show():
        emit_patch_file(path, {"status": "completed", "patch": "diff\n"})

    out = CliRunner().invoke(show).output
    _no_forged_line(out)
    assert len(out.splitlines()) == 1
    assert printable_name(str(path)) in out
    # An escaped path in a command would name a different file: none is printed.
    assert "git apply" not in out


def test_emit_patch_prints_a_command_that_names_the_written_file(tmp_path):
    import shlex

    from syncmill.monitor import emit_patch_file

    path = tmp_path / "with space 첫.patch"

    @click.command()
    def show():
        emit_patch_file(path, {"status": "completed", "patch": "diff\n"})

    out = CliRunner().invoke(show).output
    command = out.split("apply with: ", 1)[1].strip()
    assert shlex.split(command) == ["git", "apply", str(path)]


def test_team_show_escapes_configured_names():
    from syncmill.config import SyncmillConfig, TeamConfig
    from syncmill.teamfile import render_team

    value = "t" + FORGED
    team = TeamConfig.model_construct(agents=["codex", value], capabilities={value: [value]},
                                      strategy=value, goals=[value])
    plain_team = TeamConfig.model_construct(agents=["codex", "x"], capabilities={"x": ["c"]},
                                            strategy="s", goals=["g"])
    config = SyncmillConfig()
    hostile = render_team(value, team, config)
    _no_forged_line(hostile)
    assert len(hostile.splitlines()) == len(render_team("n", plain_team, config).splitlines())
    assert hostile.splitlines()[0] == "Team: " + printable_name(value)


@pytest.mark.parametrize(("attach_rc", "alive"), [(1, True), (0, True), (0, False)])
def test_session_attach_notices_escape_name_and_agent(monkeypatch, capsys, attach_rc, alive):
    import syncmill.session as S

    monkeypatch.setattr(S, "_tmux_attach", lambda name: attach_rc)
    monkeypatch.setattr(S, "session_exists", lambda name: alive)
    S._attach_and_report("n" + FORGED, "a" + FORGED)
    out = capsys.readouterr().out
    _no_forged_line(out)
    assert len(out.splitlines()) == 1
    if alive:  # an escaped agent id in a command would name another agent
        assert printable_name("n" + FORGED) in out
        assert "session attach" not in out and "no command is printed" in out
    else:
        assert printable_name("a" + FORGED) in out


def test_session_reattach_command_names_the_agent(monkeypatch, capsys):
    import shlex

    import syncmill.session as S

    monkeypatch.setattr(S, "_tmux_attach", lambda name: 0)
    monkeypatch.setattr(S, "session_exists", lambda name: True)
    S._attach_and_report("syncmill-session-x", "kimi code")
    command = capsys.readouterr().out.split("reattach: ", 1)[1].strip()
    assert shlex.split(command) == ["syncmill", "session", "attach", "kimi code"]
