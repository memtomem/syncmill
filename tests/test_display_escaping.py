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


def test_an_error_message_is_one_line(monkeypatch):
    """An id or path interpolated into an error message cannot start a line."""

    def boom(repo):
        raise click.ClickException("first line\u202e\n[syncmill] forged line")

    monkeypatch.setattr("syncmill.cli._project_artifacts", boom)
    res = CliRunner().invoke(cli, ["runs", "list"])
    assert res.exit_code == 1
    assert res.output == "Error: first line\\u202e\\x0a[syncmill] forged line\n"


def test_a_multiline_error_keeps_only_the_lines_syncmill_joined(monkeypatch):
    from syncmill.cli import _MultilineError

    def boom(repo):
        raise _MultilineError("first\u202e\n[syncmill] forged line", "Run `syncmill recover x` next.")

    monkeypatch.setattr("syncmill.cli._project_artifacts", boom)
    res = CliRunner().invoke(cli, ["runs", "list"])
    assert res.exit_code == 1
    assert res.output == (
        "Error: first\\u202e\\x0a[syncmill] forged line\nRun `syncmill recover x` next.\n"
    )


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


# ------------------------------------------- cli one-line values (#198, LF)


def test_printable_command_quotes_words_or_prints_none():
    import shlex

    from syncmill.terminal import NO_COMMAND, printable_command

    command = printable_command("syncmill", "apply", "run 1 첫", "--repo", "/a b")
    assert shlex.split(command) == ["syncmill", "apply", "run 1 첫", "--repo", "/a b"]
    for bad in ("run\n1", "run\x1b1", "run" + chr(0x202E) + "1", "run\t1"):
        assert printable_command("syncmill", "apply", bad) == f"({NO_COMMAND})"


def test_cli_workflow_completion_commands_name_the_real_run(monkeypatch):
    import shlex

    import syncmill.cli as C

    monkeypatch.setattr(C, "render_workflow", lambda record: "")

    def finish(final_run_id, repo):
        record = types.SimpleNamespace(
            invocation=lambda: {"status": "completed"}, final_run_id=final_run_id,
            workflow_id="wf-1", state="completed", manifest={},
        )

        @click.command()
        def show():
            C._render_workflow_completion(record, repo)

        return CliRunner().invoke(show).output

    out = finish("run 1", "/repo dir")
    review = next(line for line in out.splitlines() if line.startswith("[syncmill] review: "))
    assert shlex.split(review.split(": ", 1)[1]) == ["syncmill", "diff", "run 1", "--repo",
                                                     "/repo dir"]
    out = finish("run" + FORGED, "/repo")
    _no_forged_line(out)
    assert f"[syncmill] final run: {printable_name('run' + FORGED)}" in out
    assert "syncmill diff" not in out and "no command is printed" in out


def test_cli_report_list_escapes_envelope_fields(monkeypatch, tmp_path):
    runs = tmp_path / "runs"
    run_id = "run" + FORGED
    (runs / run_id).mkdir(parents=True)
    (runs / run_id / "envelope.json").write_text(json.dumps({
        "schema_version": 1, "kind": "syncmill.run-envelope", "run_id": run_id,
        "created_at": "c" + FORGED, "finished_at": "f", "strategy": "s" + FORGED,
        "team": None, "task_id": "t", "agents": ["a"], "board": None,
        "preflight": {"status": "disabled"}, "trace": {"status": "disabled"},
        "result": {"status": "x" + FORGED, "agent_id": "a" + FORGED}, "degraded": [],
    }))
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(runs))
    res = CliRunner().invoke(cli, ["report", "list"])
    assert res.exit_code == 0, res.output
    _no_forged_line(res.output)
    row = next(line for line in res.output.splitlines() if line.startswith("  run"))
    assert row.startswith("  " + printable_name(run_id) + "  ")
    assert row.endswith("  " + printable_name("a" + FORGED))


def test_cli_team_list_escapes_configured_names(monkeypatch):
    name = "team" + FORGED
    monkeypatch.setenv("SYNCMILL_TEAMS", json.dumps(
        {name: {"agents": ["codex"], "goals": ["g" + FORGED], "strategy": "route"}}
    ))
    res = CliRunner().invoke(cli, ["team", "list"])
    assert res.exit_code == 0, res.output
    _no_forged_line(res.output)
    assert printable_name(name) in res.output
    assert printable_name("g" + FORGED) in res.output
    assert len(res.output.splitlines()) == 3  # header, blank, one team


def test_cli_paths_from_the_repository_stay_on_one_line(tmp_path, monkeypatch):
    """doctor prints paths under a repository the user named; a directory name
    with an LF must not forge a line. (init refuses such a repository, and a
    run cannot snapshot it: git's alternates file is line-based.)"""
    import subprocess

    repo = tmp_path / "re\n[syncmill] forged line 첫 실행"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t.test"],
                 ["config", "user.name", "T"], ["commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.chdir(repo)
    # Hermetic: no real provider binary or auth probe runs (only an ENABLED,
    # found agent is probed). The one binary "found" sits under a directory
    # whose name carries an LF too.
    binary = str(tmp_path / "bin\n[syncmill] forged bin" / "codex")
    monkeypatch.setattr("syncmill.cli.shutil.which",
                        lambda name: binary if name == "codex" else None)
    for agent in ("CLAUDE", "CODEX", "KIMI_CODE"):
        monkeypatch.setenv(f"SYNCMILL_{agent}__ENABLED", "false")
    res = CliRunner().invoke(cli, ["doctor", "--repo", str(repo)])
    assert not any(line.startswith("[syncmill] forged") for line in res.output.splitlines())
    assert printable_name(str(repo)) in res.output, res.output
    assert f"Agent codex: {printable_name(binary)}" in res.output


@pytest.mark.parametrize("run_id", ["run 1", "run" + FORGED])
def test_apply_recovery_suggestion_is_a_real_command_or_none(tmp_path, monkeypatch, run_id):
    import shlex

    from syncmill.artifacts import ArtifactRecoveryRequired

    def broken(*a, **k):
        raise ArtifactRecoveryRequired("could not restore", source_changed=False)

    store = types.SimpleNamespace(
        reconcile_interrupted_applies=lambda run_id=None: [],
        load=lambda rid: types.SimpleNamespace(manifest={}),
        apply=broken,
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
    assert not any(line.startswith("[syncmill] forged") for line in out.splitlines())
    suggestion = out.split("To reconcile this apply, run: ", 1)[1].strip()
    if run_id == "run 1":
        assert shlex.split(suggestion) == ["syncmill", "recover", "run 1"]
    else:
        assert "no command is printed" in suggestion


def test_unknown_team_errors_escape_the_configured_names(monkeypatch):
    name = "team" + FORGED
    monkeypatch.setenv("SYNCMILL_TEAMS", json.dumps({name: {"agents": ["codex"]}}))
    for args in (["team", "show", "ghost"], ["team", "use", "ghost"]):
        res = CliRunner().invoke(cli, args)
        assert res.exit_code != 0
        assert_display_safe(res.output)
        assert not any(line.startswith("[syncmill] forged") for line in res.output.splitlines())
        assert printable_name(name) in res.output, args


def test_board_degraded_notices_stay_on_one_line():
    from syncmill.cli import _surface_board_degraded

    supervisor = types.SimpleNamespace(
        memory=types.SimpleNamespace(degraded=True, last_error="m" + FORGED),
        breaker=types.SimpleNamespace(degraded=True, last_error="b" + FORGED),
    )

    @click.command()
    def show():
        _surface_board_degraded(supervisor)

    out = CliRunner().invoke(show).output
    _no_forged_line(out)
    assert len(out.splitlines()) == 2


def test_a_positional_that_looks_like_an_option_is_never_parsed_as_one():
    """``syncmill recover --discard=x`` would delete a workspace instead of
    reconciling run ``--discard=x``; the printed command must name the run."""
    import shlex

    from syncmill.terminal import printable_command

    assert printable_command("syncmill", "diff", args=["run-1"], options=["--repo", "/r"]) == (
        "syncmill diff run-1 --repo /r"
    )
    command = printable_command("syncmill", "diff", args=["-x"], options=["--repo", "/a b"])
    assert shlex.split(command) == ["syncmill", "diff", "--repo", "/a b", "--", "-x"]
    command = printable_command("syncmill", "recover", args=["--discard=kept"])
    res = CliRunner().invoke(cli, shlex.split(command)[1:])
    assert "unknown or corrupt run: --discard=kept" in res.output


# ------------------------------------------- one-line error messages (#198)


def _shown_error(exc: click.ClickException) -> str:
    from syncmill.cli import _escaped_errors

    with pytest.raises(click.ClickException) as caught:
        with _escaped_errors():
            raise exc
    return caught.value.message


def _raised(make):
    """The exception ``make()`` raises, with its chain set by Python."""
    try:
        make()
    except BaseException as exc:  # noqa: BLE001 - the test inspects it
        return exc
    raise AssertionError("did not raise")


def test_the_reported_paths_stay_on_one_line(tmp_path):
    """The two reproductions in #198: a path with an LF in the refusal of
    ``init`` and in ``doctor``'s discovery failure."""
    target = tmp_path / ("re" + FORGED)
    target.mkdir()
    for args in (["init", str(target)], ["doctor", "--repo", str(target)]):
        res = CliRunner().invoke(cli, args)
        assert res.exit_code == 1, (args, res.output)
        assert res.output == f"Error: not a Git repository: {printable_name(str(target))}\n"


def _team_list_error(monkeypatch, teams: str) -> str:
    monkeypatch.setenv("SYNCMILL_TEAMS", teams)
    res = CliRunner().invoke(cli, ["team", "list"])
    assert res.exit_code == 1, res.output
    return res.output


def test_a_validation_report_keeps_its_lines_but_not_an_lf_in_a_key(monkeypatch):
    """Pydantic shows a dict key in the error location as is; syncmill rebuilds
    the report so the key is escaped like any other value."""
    out = _team_list_error(monkeypatch, json.dumps({"bad" + FORGED: {"agents": []}}))
    _no_forged_line(out)
    lines = out.splitlines()
    assert lines[0] == "Error: invalid configuration:"
    assert lines[1] == "1 validation error for SyncmillConfig"
    assert lines[2] == printable_name("teams.bad" + FORGED + ".agents")
    assert lines[3].startswith("  List should have at least 1 item")
    assert len(lines) == 4


def test_a_validation_report_escapes_a_validator_message():
    from pydantic import BaseModel, ValidationError, model_validator

    class Spec(BaseModel):
        name: str

        @model_validator(mode="after")
        def _check(self):
            raise ValueError("unknown node: " + self.name)

    error = _raised(lambda: Spec(name="n" + FORGED))
    assert isinstance(error, ValidationError)
    lines = _shown_error(_raised(lambda: _reraise("invalid workflow", error))).split("\n")
    assert lines[:2] == ["invalid workflow:", "1 validation error for Spec"]
    assert lines[2] == "  " + printable_name(
        "Value error, unknown node: n" + FORGED + " [type=value_error]"
    )
    assert len(lines) == 3


def _reraise(head: str, error: BaseException):
    from syncmill.cli import _described

    raise click.ClickException(_described(head, error)) from error


def test_a_report_found_through_the_implicit_context_is_rendered():
    """``teamfile`` wraps a ValidationError without ``from``: the report is
    then only reachable through ``__context__``."""
    from syncmill.teamfile import TeamFileError, build_team

    error = _raised(lambda: build_team(agents=["codex", "codex"]))
    assert isinstance(error, TeamFileError) and error.__cause__ is None

    def convert():
        try:
            raise error
        except TeamFileError as exc:
            raise click.ClickException(str(exc)) from exc

    lines = _shown_error(_raised(convert)).split("\n")
    assert lines[:3] == ["invalid team profile:", "1 validation error for TeamConfig", "agents"]


def test_a_report_without_its_error_behind_it_is_one_line():
    from pydantic import BaseModel

    class Spec(BaseModel):
        count: int

    report = str(_raised(lambda: Spec(count="x")))
    # The same text, but nothing behind the message vouches for it.
    assert _shown_error(click.ClickException(f"head:\n{report}")) == printable_name(
        f"head:\n{report}"
    )


def test_a_reworded_report_is_one_line():
    from pydantic import BaseModel

    class Spec(BaseModel):
        count: int

    error = _raised(lambda: Spec(count="x"))

    def convert():
        raise click.ClickException(f"head:\n{str(error)[:-1]}") from error

    exc = _raised(convert)
    assert "\n" not in _shown_error(exc)


def _chained(length: int, last: BaseException) -> click.ClickException:
    """A ClickException whose message ends with ``last``'s output, ``length``
    exceptions in front of it."""
    current = last
    for index in range(length):
        nxt = RuntimeError(f"link {index}")
        nxt.__cause__ = current
        current = nxt
    exc = click.ClickException(f"head:\n{last.command_output}")
    exc.__cause__ = current
    return exc


def test_the_chain_walk_follows_branches_stops_on_cycles_and_gives_up_past_its_limit():
    from syncmill.cli import _CHAIN_LIMIT
    from syncmill.command_errors import command_error

    output = command_error(RuntimeError, "head", "two\nlines")
    # Found on the __context__ branch of an exception whose __cause__ is
    # something else.
    side = RuntimeError("side")
    side.__context__ = output
    side.__cause__ = ValueError("other")
    exc = click.ClickException(f"head:\n{output.command_output}")
    exc.__cause__ = side
    assert _shown_error(exc) == "head:\n  | two\n  | lines"
    # A cycle that never reaches a body ends the walk; the message is one line.
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    exc = click.ClickException("head:\ntwo\nlines")
    exc.__cause__ = a
    assert _shown_error(exc) == "head:\\x0atwo\\x0alines"
    # Within the limit the body is found; past it, nothing is trusted.
    assert "\n" in _shown_error(_chained(_CHAIN_LIMIT - 2, output))
    assert "\n" not in _shown_error(_chained(_CHAIN_LIMIT, output))


def test_command_output_is_a_labelled_block_under_a_one_line_head():
    from syncmill.cli import _MultilineError  # noqa: F401 - same module
    from syncmill.command_errors import command_error
    from syncmill.worktree import WorkspaceError

    output = "fatal: bad\n[syncmill] forged line\x1b[2J"
    error = command_error(WorkspaceError, "git status failed in /r" + FORGED, output)
    assert str(error) == f"git status failed in /r{FORGED}:\n{output.strip()}"

    def convert():
        raise click.ClickException(str(error)) from error

    shown = _shown_error(_raised(convert))
    _no_forged_line(shown.replace("[syncmill] forged line\\x1b", ""))
    assert shown.split("\n") == [
        printable_name(f"git status failed in /r{FORGED}:"),
        "  | fatal: bad",
        "  | [syncmill] forged line\\x1b[2J",
    ]


def test_a_command_error_without_output_is_its_head_alone():
    from syncmill.command_errors import command_error

    error = command_error(RuntimeError, "tmux new-session failed", " \n ")
    assert str(error) == "tmux new-session failed"
    assert not hasattr(error, "command_output")


def test_one_line_command_output_stays_on_the_head_line():
    """Every reader of the error (a TUI field, a stored reason) gets one line,
    as it did when the error was the bare stderr."""
    from syncmill.command_errors import command_error

    error = command_error(RuntimeError, "git rev-parse failed", "fatal: not a git repository\n")
    assert str(error) == "git rev-parse failed: fatal: not a git repository"
    assert not hasattr(error, "command_output")


def test_a_decorated_command_error_keeps_its_block_and_a_reworded_one_does_not():
    from syncmill.command_errors import command_error

    error = command_error(RuntimeError, "git apply failed", "error: a\nerror: b")

    def decorated():
        raise click.ClickException(f"apply of run x\n failed: {error}") from error

    assert _shown_error(_raised(decorated)).split("\n") == [
        "apply of run x\\x0a failed: git apply failed:",
        "  | error: a",
        "  | error: b",
    ]

    def reworded():
        raise click.ClickException(str(error).upper()) from error

    assert "\n" not in _shown_error(_raised(reworded))


def test_escaping_the_same_error_twice_changes_nothing():
    from pydantic import BaseModel

    from syncmill.cli import _escaped_errors

    class Spec(BaseModel):
        count: int

    error = _raised(lambda: Spec(count="x\u009b"))

    def convert():
        raise click.ClickException(f"bad\u009b:\n{error}") from error

    exc = _raised(convert)
    with pytest.raises(click.ClickException) as caught:
        with _escaped_errors():
            with _escaped_errors():
                raise exc
    assert caught.value.message.split("\n")[:2] == ["bad\\x9b:", "1 validation error for Spec"]


def test_isolated_verification_output_is_a_block(git_repo, tmp_path):
    """End to end through ``ArtifactStore.apply``: a verification command that
    prints a forged line."""
    from test_v2_workflow import _pending_artifact

    from syncmill.artifacts import ArtifactError
    from syncmill.worktree import repository_fingerprint

    store, _ = _pending_artifact(tmp_path, git_repo)
    command = (
        "python -c \"print('FAILED a'); print('[syncmill] forged line'); raise SystemExit(1)\""
    )
    error = _raised(lambda: store.apply(
        "run-1", git_repo, repository_fingerprint(git_repo),
        verify_commands=[command], verify_timeout=30,
    ))
    assert isinstance(error, ArtifactError)

    def convert():
        raise click.ClickException(str(error)) from error

    lines = _shown_error(_raised(convert)).split("\n")
    assert lines[0] == printable_name(f"isolated verification failed ({command}):")
    assert lines[1:] == ["  | FAILED a", "  | [syncmill] forged line"]


def test_a_failed_apply_check_shows_git_output_as_a_block(git_repo, tmp_path, monkeypatch):
    import test_v2_workflow

    from syncmill.artifacts import ArtifactError
    from syncmill.worktree import repository_fingerprint

    # A patch whose context does not match the run's tree.
    monkeypatch.setattr(test_v2_workflow, "_NEW_FILE_PATCH", (
        "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
        "@@ -1 +1 @@\n-# not this\n+# changed\n"
    ))
    store, _ = test_v2_workflow._pending_artifact(tmp_path, git_repo)
    error = _raised(lambda: store.apply("run-1", git_repo, repository_fingerprint(git_repo)))
    assert isinstance(error, ArtifactError)
    assert getattr(error, "command_output", "")

    def convert():
        raise click.ClickException(str(error)) from error

    lines = _shown_error(_raised(convert)).split("\n")
    assert lines[0] == "git apply check failed:"
    assert lines[1:] and all(line.startswith("  | ") for line in lines[1:])
    assert any("README.md" in line for line in lines[1:])


@pytest.mark.parametrize("args", [[], ["room"]])
def test_a_command_run_without_arguments_still_prints_its_help(args):
    full = CliRunner().invoke(cli, [*args, "--help"])
    bare = CliRunner().invoke(cli, args)
    assert full.exit_code == 0
    assert "\n" in bare.output.strip()
    assert bare.output == full.output


@pytest.mark.parametrize("command_fails", [False, True])
def test_errors_from_root_teardown_are_one_line(command_fails):
    """Click closes the root context after ``invoke`` returns; a close callback
    that raises (after success, or replacing the command's error) is escaped too."""

    def teardown():
        raise click.ClickException("teardown\n[syncmill] forged line")

    @click.command("teardown-probe")
    def probe():
        click.get_current_context().find_root().call_on_close(teardown)
        if command_fails:
            raise click.ClickException("command failed")

    cli.add_command(probe)
    try:
        res = CliRunner().invoke(cli, ["teardown-probe"])
    finally:
        cli.commands.pop("teardown-probe")
    assert res.exit_code == 1
    assert res.output == "Error: teardown\\x0a[syncmill] forged line\n"


@pytest.mark.parametrize("args", [
    ["session", "start", "codex"],
    ["session", "open", "codex", "--workspace", "direct"],
])
def test_a_failed_session_launch_shows_tmux_output_as_a_block(monkeypatch, git_repo, args):
    from syncmill.command_errors import command_error
    from syncmill.session import SessionLaunchError

    def fail(agent_id, workdir):
        raise command_error(
            SessionLaunchError, "tmux new-session failed", "no server\n[syncmill] forged line"
        )

    monkeypatch.chdir(git_repo)
    monkeypatch.setattr("syncmill.cli._stdout_isatty", lambda: True)
    monkeypatch.setattr("syncmill.cli.tmux_available", lambda: True)
    monkeypatch.setattr("syncmill.cli.start_session", fail)
    res = CliRunner().invoke(cli, args)
    assert res.exit_code == 1, res.output
    assert res.output.splitlines()[-3:] == [
        "Error: session failed to start: tmux new-session failed:",
        "  | no server",
        "  | [syncmill] forged line",
    ]
