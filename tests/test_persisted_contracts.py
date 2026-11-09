"""Pins for persisted contracts (docs/architecture/persisted-contracts.md, #267).

Each test freezes a value that syncmill stores and a later operation, possibly
run by a later syncmill version, re-derives and compares. The expected values
are hard-coded from the released derivation. A failure here means records that
already exist would be misread. Do not update a pin to make it pass: version
the value instead, as #262 did for the source fingerprint.
"""

from __future__ import annotations

import pytest

from syncmill.rooms import lesson_source_key


def test_lesson_source_key_is_pinned():
    # Stored: decisions.source_key, written by `room promote`.
    # Used: Board.promote_lesson looks a retried promotion up by this key and
    # returns the existing lesson; a changed derivation would create a second one.
    assert lesson_source_key("proj-1", "judgment-1", "", "Prefer small diffs") == (
        "07c854d2207230fa3abbe0de1e0568544894010ead75dfdd90a021f090b4dfdf"
    )
    # Non-ASCII text is hashed as UTF-8 of the unescaped JSON (ensure_ascii=False).
    text = "Pr" + chr(0xE9) + "f" + chr(0xE9) + "rer les petits diffs"
    assert lesson_source_key("proj-1", "judgment-1", "backend", text) == (
        "c7f9a99685848e25e991bcd6289fbad5fc94857dd03f933583c37f31c229dc08"
    )


# ── B: digests recomputed from stored data ────────────────────────────────────


def test_patch_and_output_digests_are_plain_sha256_hex():
    # Stored: patch_sha256 / output_sha256 in artifact manifests, room
    # candidates, workflow bundles, selections and judgments. Used: every load
    # re-hashes the stored bytes and compares.
    from syncmill.artifacts import _digest as artifact_digest
    from syncmill.rooms import _digest as room_digest

    expected = "a4895eb44afc336fecbba6e520cd67e178dace0276655d102fceffa8e5f70570"
    assert artifact_digest(b"patch") == expected
    assert room_digest(b"patch") == expected


def test_workflow_bundle_digest_is_pinned():
    # Stored: commit.json bundle_sha256 and a final artifact's plan. Used:
    # resume re-hashes result.json NUL patch and compares.
    from syncmill.workflow import bundle_digest

    assert bundle_digest(b'{"a":1}\n', b"diff\n") == (
        "0c0f01b07532dcbd9940124a5d67fc471ae1a02d36e462c0528a4c61938d265f"
    )


def test_workflow_prefixed_sha256_spelling_is_pinned():
    # Stored: the control-plan reference digest ("sha256:<hex>"). Used: resume
    # re-hashes control-plan.json and compares the spelled value.
    from syncmill.workflow import _sha256

    assert _sha256(b"plan") == (
        "sha256:64879f7d6b960a01909762d911a32d4582c20010c5641ee90278b644a9e3b525"
    )


def test_memory_migration_digests_and_target_id_are_pinned():
    # Stored: a migration snapshot's record digests, lineage keys and
    # source_digest; the destination copy's id. Used: apply/verify/rollback
    # recompute them, and a retried write finds its earlier copy by the id.
    from syncmill import memory_migration as mm

    record = mm._record("memtomem", "src-1", "shared", "hello " + chr(0xE9), ["a", "b"], None)
    assert record.digest == "c997f94c2c81982421f0a14c9fb86b83ee0c25a5451e0a8e4240563a1bc78313"
    assert record.lineage_key == (
        "f3c2a5dc073a75f580b02dc955ea2f3eac675f50ae638ce902ec6d20e87d13f2"
    )
    assert mm._aggregate([record]) == (
        "052dd82cdc246f60b057352bbb4073435a7c87eb9e54edd97992909ee6ab18b0"
    )
    assert mm._target_id(record.lineage_key) == "db26d35a-c0c3-594c-9bb9-ef756c776524"


def test_memory_migration_destination_binding_is_pinned():
    # Stored: a migration's destination binding digest. Used: apply, verify and
    # rollback refuse a migration whose destination no longer hashes the same.
    from syncmill import memory_migration as mm
    from syncmill.config import SyncmillConfig

    config = SyncmillConfig(
        memory={"backend": "memtomem", "db_path": "/srv/pin/memtomem.db",
                "memory_dir": "/srv/pin/mem"}
    )
    assert mm._target_storage_digest(config, "memtomem") == (
        "sha256:3c66fb3f3e57b3fdbf59d6520f5a8854e3e1a2957dd817517dbb8c494ce1c01e"
    )


# ── C: derived keys and ids ───────────────────────────────────────────────────


def test_path_hash_project_id_is_pinned():
    # Stored: state directory names (runs, rooms, sessions, workspaces) when no
    # trusted marker names the project. Used: every later command finds its
    # state by re-deriving this id.
    from pathlib import Path

    from syncmill.project import project_id

    assert project_id(Path("/syncmill-pin/does-not-exist")) == "63d5f120c50b467b4280e636"


def test_review_item_projection_is_pinned():
    # Stored: the board item (id, task, inputs, evidence) a review import
    # creates. Used: a re-import re-derives the whole projection and refuses
    # a provenance collision when any part differs from the stored item.
    from pathlib import Path

    from syncmill.review_candidates import load_report, item_spec

    candidate = load_report(
        Path(__file__).parent / "fixtures" / "tracegraph" / "v1.json"
    ).candidates[0]
    digest = "sha256:2469307ff4b7bc62fd803a18822f3d26c6620bfaa06e0037db62082f6c9c528b"
    spec = item_spec(candidate)
    assert spec.item_id == "db0ad772-7a10-587a-b48c-581e726e7b09"
    assert spec.task == "Review Tracegraph tool-retry-failure@v2 finding for syncmill::board_stats"
    assert spec.inputs == (
        "source:tracegraph.review-candidates@v1",
        "run_id:efd16fc1-758e-48fd-a765-bb3e27815952",
        "pattern:tool-retry-failure@v2",
        "tool_key:syncmill::board_stats",
        f"artifact_digest:{digest}",
    )
    assert spec.artifact_digest == digest


def test_unmapped_principal_id_is_pinned():
    # Stored: control-plan principals for agents without a toolgraph mapping.
    # Used: workflow resume re-derives and compares them.
    from syncmill.planning import unmapped_principal_id

    assert unmapped_principal_id("kimi-code") == "syncmill-unmapped-75b5135cbfd9f706"


def test_workflow_ids_are_pinned():
    # Stored: a frozen control plan's node ids; a workflow's final run id.
    # Used: resume re-derives node ids from the DAG and compares; an
    # interrupted finalization finds its artifact by the final run id.
    from syncmill.planning import plan_node_id
    from syncmill.workflow import workflow_final_run_id

    assert plan_node_id("final") == "workflow:final"
    assert workflow_final_run_id("wf-1") == "wf-1-final"


def test_memory_namespace_names_are_pinned():
    # Stored: every memory row's namespace, every day file's name, and the
    # LangGraph store namespace. Used: reads, GC and purge re-derive them; a
    # changed name hides existing memories or strands old files.
    from pathlib import Path

    from syncmill.memory import BOARD_RUN_PREFIX
    from syncmill.memory_backend import (
        _LANGGRAPH_NS,
        LangGraphBackend,
        Mem0Backend,
        _day_file,
        _tag_key,
    )

    assert BOARD_RUN_PREFIX == "board-run:"
    for backend in (Mem0Backend, LangGraphBackend):
        assert backend.agent_namespace_prefix.fget(None) == "agent-runtime:"
        assert backend.shared_namespace.fget(None) == "shared"
    assert _LANGGRAPH_NS == ("syncmill", "memory", "items")
    assert _day_file(Path("/m"), "agent-runtime:claude", "2026-11-09") == Path(
        "/m/2026-11-09.agent-runtime.claude.md"
    )
    # Purge and GC find a namespace's files by these suffixes: syncmill's own
    # day-file shape first, then memtomem's native one.
    from types import SimpleNamespace

    from syncmill.memory_backend import _namespace_day_file_suffixes

    store_config = SimpleNamespace(namespace=SimpleNamespace(default_namespace="default"))
    assert _namespace_day_file_suffixes(store_config, "agent-runtime:claude")[0] == (
        ".agent-runtime.claude.md"
    )
    assert _tag_key("council-proposal") == (
        "tag_c63ad47ea511f78d3fd72fbfa5ebe9de5bd988e9ff12255f8fb0d9caa3892159"
    )


def test_tui_job_directory_keys_are_pinned():
    # Stored: the TUI job store's directory under the state root (project_key)
    # and the legacy board-keyed root. Used: list_jobs finds current and legacy
    # job history by re-deriving both.
    from syncmill.tui_jobs import board_key, project_key

    assert project_key("/srv/repo") == (
        "b50f95e831d95532f98957e8741d88d51dfe71ba76fc8ebcbcceac24a5694b2d"
    )
    assert board_key("/srv/board.db") == (
        "f0468d3de59b33d971eeb98268528d34ab79e5f637f0604be57caa0c33349c1e"
    )


def test_session_and_viewer_names_are_pinned():
    # Stored: an isolated session's run root and tmux name; a room viewer's
    # tmux name and ownership digest. Used: stored paths are re-checked against
    # the derivation, and the names are the reattach and sweep keys.
    from syncmill.isolated_session import session_run_name, session_tmux_name
    from syncmill.room_viewer import _project_digest, viewer_session_name

    sid = "11111111-1111-4111-8111-111111111111"
    assert session_run_name(sid) == f"session-{sid}"
    assert session_tmux_name("proj-12345678", "claude", sid) == "syncmill-p-proj-123-claude-11111111"
    assert _project_digest("proj-1") == (
        "920581d82c9c3a08fc5cc38ecae2fcef37d73135da4f4d934d2e2e9f86fb95f7"
    )
    assert viewer_session_name("proj-1", sid, 2) == (
        f"syncmill-room-920581d82c9c3a08-{sid}-r2"
    )


# ── recover with an unrecognizable stored fingerprint (#264) ──────────────────



@pytest.mark.parametrize("stored", [None, "<missing>", "garbage", "v9:" + "0" * 64])
@pytest.mark.parametrize("receipt", [True, False])
def test_recover_never_decides_on_an_unrecognizable_fingerprint(tmp_path, stored, receipt):
    from test_v2_workflow import _force_applying, _pending_artifact, _repo

    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    manifest = dict(store.load("run-1").manifest)
    if stored == "<missing>":
        del manifest["source_fingerprint"]
    else:
        manifest["source_fingerprint"] = stored
    store._write_manifest(artifact.path, manifest)
    _force_applying(store, store.load("run-1"), owner=None, receipt=receipt)

    [verdict] = store.reconcile_interrupted_applies(run_id="run-1")
    assert verdict.outcome == "manual_recovery"
    assert "unrecognized stored fingerprint" in verdict.detail
    assert store.load("run-1").manifest["state"] not in {"applied", "pending"}
    assert not (repo / "winner.txt").exists()
