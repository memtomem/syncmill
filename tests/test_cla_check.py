"""Hermetic tests for the CLA gate's contributor enumeration and status writes.

The checker lives in `.github/` because a workflow runs it with `python3` and
nothing installed, so it is loaded here by path. Every GitHub call goes through
one `gh_api` seam, which these tests replace — nothing reaches the network, and
no test needs a `gh` binary.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "cla-check.py"
_spec = importlib.util.spec_from_file_location("cla_check", _SCRIPT)
assert _spec and _spec.loader
cla = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cla)


def _pr(head="aaa", base="base0", user="contributor", number=7):
    return {
        "number": number,
        "user": {"login": user, "id": 1},
        "head": {"sha": head},
        "base": {"sha": base},
    }


def _compare(*logins, total=None):
    commits = [{"author": {"login": login, "id": 2}} for login in logins]
    return {"commits": commits, "total_commits": total if total is not None else len(commits)}


# ── Enumeration is bound to immutable SHAs ───────────────────────────────


def test_contributors_are_read_from_the_compare_range(monkeypatch):
    seen = {}

    def fake_api(endpoint, **kw):
        seen["endpoint"] = endpoint
        return _compare("alice", "bob")

    monkeypatch.setattr(cla, "gh_api", fake_api)
    people = cla.pr_contributors("o/r", _pr(), set(), "tok")
    assert people == {"contributor", "alice", "bob"}
    # The range must name both SHAs, not the pull request number.
    assert seen["endpoint"].startswith("repos/o/r/compare/base0...aaa")


def test_enumeration_never_reads_the_mutable_commit_list(monkeypatch):
    # The whole point: `pulls/{n}/commits` follows the head wherever a
    # force-push moves it, so asking it can answer for a different head than
    # the one a verdict is published against.
    def fake_api(endpoint, **kw):
        assert "/pulls/" not in endpoint, endpoint
        return _compare("alice")

    monkeypatch.setattr(cla, "gh_api", fake_api)
    assert "alice" in cla.pr_contributors("o/r", _pr(), set(), "tok")


def test_an_allowlisted_login_is_not_required_to_sign(monkeypatch):
    monkeypatch.setattr(cla, "gh_api", lambda endpoint, **kw: _compare("dependabot[bot]"))
    assert cla.pr_contributors("o/r", _pr(user="memtomem"), {"dependabot[bot]", "memtomem"},
                               "tok") == set()


def test_a_compare_without_a_commit_list_is_an_error(monkeypatch):
    monkeypatch.setattr(cla, "gh_api", lambda endpoint, **kw: {"status": "identical"})
    with pytest.raises(RuntimeError, match="no commit list"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


# ── The A -> B -> A force-push sequence ──────────────────────────────────


def test_a_force_push_away_and_back_cannot_launder_another_heads_commits(monkeypatch):
    """The sequence a before/after head comparison cannot see.

    Head A is unsigned. Between the two reads the branch is pushed to signed
    head B and back to A. Comparing the head before and after sees A both
    times and concludes nothing moved — so the verdict must instead come from
    commits enumerated by SHA, which are A's whatever the branch did.
    """
    calls = []

    def fake_api(endpoint, **kw):
        calls.append(endpoint)
        if endpoint.startswith("repos/o/r/compare/"):
            # Whoever asks for A's range gets A's commits. That is the property
            # under test: the answer is pinned to the SHA in the URL.
            assert endpoint.startswith("repos/o/r/compare/base0...aaa"), endpoint
            return _compare("unsigned-person")
        raise AssertionError(f"unexpected call {endpoint}")

    monkeypatch.setattr(cla, "gh_api", fake_api)
    people = cla.pr_contributors("o/r", _pr(head="aaa"), set(), "tok")
    assert people == {"contributor", "unsigned-person"}
    assert not any("/pulls/" in endpoint for endpoint in calls)


# ── Status publication ───────────────────────────────────────────────────


def test_the_status_is_posted_against_the_given_head(monkeypatch):
    seen = {}

    def fake_api(endpoint, *, method="GET", token=None, payload=None):
        seen["endpoint"], seen["method"], seen["payload"] = endpoint, method, payload
        return {}

    monkeypatch.setattr(cla, "gh_api", fake_api)
    cla.publish_status(
        "o/r", "deadbeef", state="failure", description="Unsigned: alice",
        context="cla", target_url="https://example.test/CLA.md", token="tok",
    )
    assert seen["endpoint"] == "repos/o/r/statuses/deadbeef"
    assert seen["method"] == "POST"
    assert seen["payload"]["state"] == "failure"
    assert seen["payload"]["context"] == "cla"


def test_a_long_description_is_truncated_to_the_api_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(cla, "gh_api", lambda endpoint, **kw: seen.update(kw) or {})
    cla.publish_status(
        "o/r", "deadbeef", state="failure", description="x" * 500,
        context="cla", target_url="u", token="tok",
    )
    assert len(seen["payload"]["description"]) == 140


def test_publishing_a_status_is_fatal_rather_than_warned(monkeypatch):
    # The status IS the gate once branch protection requires it, so a failure to
    # publish must fail the run rather than report a gate that was never set.
    def fake_api(endpoint, **kw):
        raise cla.GitHubApiError(["gh", "api"], "HTTP 403")

    monkeypatch.setattr(cla, "gh_api", fake_api)
    with pytest.raises(cla.GitHubApiError):
        cla.publish_status(
            "o/r", "deadbeef", state="success", description="ok",
            context="cla", target_url="u", token="tok",
        )


# ── Signature records ────────────────────────────────────────────────────


def test_a_signature_is_matched_case_insensitively():
    signatures = {"signedContributors": [{"name": "Alice"}]}
    assert "alice" in cla.signed_logins(signatures)


def test_an_empty_signature_store_signs_nobody():
    assert cla.signed_logins({"signedContributors": []}) == set()
    assert json.dumps({"signedContributors": []})  # the shape the branch holds


# ── Pagination ───────────────────────────────────────────────────────────


def test_a_contributor_on_a_later_page_is_still_required_to_sign(monkeypatch):
    # The compare endpoint caps a page at 100. Reading only the first page would
    # drop everyone after it, and an unseen contributor reads as a signed one.
    pages = {
        1: {"commits": [{"author": {"login": f"p{i}", "id": i}} for i in range(100)],
            "total_commits": 101},
        2: {"commits": [{"author": {"login": "late-unsigned", "id": 999}}],
            "total_commits": 101},
    }

    def fake_api(endpoint, **kw):
        page = int(endpoint.rsplit("page=", 1)[1])
        assert "per_page=100" in endpoint, endpoint
        return pages[page]

    monkeypatch.setattr(cla, "gh_api", fake_api)
    people = cla.pr_contributors("o/r", _pr(), set(), "tok")
    assert "late-unsigned" in people
    assert len(people) == 102  # 100 + the late one + the PR author


def test_a_short_read_is_refused_rather_than_answered(monkeypatch):
    # The pages stop early while `total_commits` says there are more.
    monkeypatch.setattr(
        cla, "gh_api",
        lambda endpoint, **kw: {"commits": [{"author": {"login": "a", "id": 1}}],
                                "total_commits": 5},
    )
    with pytest.raises(RuntimeError, match="1 of 5"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


def test_a_compare_without_a_total_cannot_prove_completeness(monkeypatch):
    monkeypatch.setattr(
        cla, "gh_api",
        lambda endpoint, **kw: {"commits": [{"author": {"login": "a", "id": 1}}]},
    )
    with pytest.raises(RuntimeError, match="no total_commits"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


# ── The whole gate, end to end ───────────────────────────────────────────


def _run_main(monkeypatch, tmp_path, event, responses, statuses):
    """Drive `main()` with every GitHub call answered from `responses`."""
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("PERSONAL_ACCESS_TOKEN", raising=False)

    def fake_api(endpoint, *, method="GET", token=None, payload=None):
        if endpoint.startswith("repos/o/r/statuses/"):
            statuses.append((endpoint.rsplit("/", 1)[1], payload["state"]))
            return {}
        for prefix, value in responses:
            if endpoint.startswith(prefix):
                return value() if callable(value) else value
        raise AssertionError(f"unexpected call {endpoint}")

    monkeypatch.setattr(cla, "gh_api", fake_api)
    monkeypatch.setattr(cla, "gh_paginated", lambda endpoint, **kw: [])
    return cla.main()


def test_an_unsigned_head_is_failed_even_after_an_a_b_a_force_push(monkeypatch, tmp_path):
    """The regression the immutable enumeration exists for.

    Head A is unsigned. Between the reads the branch goes to signed head B and
    back to A, so a before/after head comparison sees no movement at all. The
    verdict must still be A's, because A's commits are what was enumerated.
    """
    statuses: list[tuple[str, str]] = []
    head_reads = iter(["aaa", "aaa"])  # A both times: the sequence is invisible

    rc = _run_main(
        monkeypatch,
        tmp_path,
        {"pull_request": {"number": 7}, "repository": {"id": 1},
         "action": "synchronize"},
        [
            ("repos/o/r/pulls/7", lambda: _pr(head=next(head_reads))),
            ("repos/o/r/compare/base0...aaa",
             _compare("unsigned-person")),
            ("repos/o/r/contents/", {"content": "", "sha": None}),
            ("repos/o/r/issues/7/comments", []),
        ],
        statuses,
    )
    assert rc == 1, "an unsigned contributor must fail the run"
    assert statuses == [("aaa", "failure")]


def test_a_fully_signed_pull_request_turns_its_head_green(monkeypatch, tmp_path):
    import base64

    signed = base64.b64encode(
        json.dumps({"signedContributors": [{"name": "contributor"}]}).encode()
    ).decode()
    statuses: list[tuple[str, str]] = []
    rc = _run_main(
        monkeypatch,
        tmp_path,
        {"pull_request": {"number": 7}, "repository": {"id": 1}, "action": "opened"},
        [
            ("repos/o/r/pulls/7", lambda: _pr()),
            ("repos/o/r/compare/", _compare()),
            ("repos/o/r/contents/", {"content": signed, "sha": "s1"}),
            ("repos/o/r/issues/7/comments", []),
        ],
        statuses,
    )
    assert rc == 0
    assert statuses == [("aaa", "success")]


def test_a_head_that_moved_on_publishes_nothing(monkeypatch, tmp_path):
    # That push starts its own run, which evaluates the new head from scratch;
    # publishing here would race it with an answer about the old head.
    statuses: list[tuple[str, str]] = []
    head_reads = iter(["aaa", "bbb"])
    rc = _run_main(
        monkeypatch,
        tmp_path,
        {"pull_request": {"number": 7}, "repository": {"id": 1},
         "action": "synchronize"},
        [
            ("repos/o/r/pulls/7", lambda: _pr(head=next(head_reads))),
            ("repos/o/r/compare/base0...aaa", _compare("someone")),
            ("repos/o/r/contents/", {"content": "", "sha": None}),
        ],
        statuses,
    )
    assert rc == 0
    assert statuses == []
