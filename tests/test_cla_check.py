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
    # The pages run out while `total_commits` says there are more. Answering
    # with what arrived would drop whoever authored the rest.
    _paged(monkeypatch, {
        1: {"commits": [{"author": {"login": "a", "id": 1}}], "total_commits": 5},
        2: {"commits": [], "total_commits": 5},
    })
    with pytest.raises(RuntimeError, match="1 of 5"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


def test_a_compare_without_a_total_cannot_prove_completeness(monkeypatch):
    monkeypatch.setattr(
        cla, "gh_api",
        lambda endpoint, **kw: {"commits": [{"author": {"login": "a", "id": 1}}]},
    )
    with pytest.raises(RuntimeError, match="no usable"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


def _paged(monkeypatch, pages):
    def fake_api(endpoint, **kw):
        return pages[int(endpoint.rsplit("page=", 1)[1])]

    monkeypatch.setattr(cla, "gh_api", fake_api)


def _full_page(total):
    return {"commits": [{"author": {"login": f"p{i}", "id": i}} for i in range(100)],
            "total_commits": total}


def test_a_shrinking_total_between_pages_is_refused(monkeypatch):
    # Two fixed SHAs describe one comparison whose size cannot change. A page
    # claiming otherwise is answering about something else — and believing the
    # smaller number would stop the walk short of the commits that remain.
    _paged(monkeypatch, {1: _full_page(300), 2: _full_page(200)})
    with pytest.raises(RuntimeError, match="reported 300 commits and then 200"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


def test_a_growing_total_between_pages_is_refused(monkeypatch):
    # The same defect in the other direction, where the walk would never end.
    class _Growing(dict):
        def __missing__(self, page):
            return _full_page(100 * (page + 1))

    _paged(monkeypatch, _Growing())
    with pytest.raises(RuntimeError, match="cannot enumerate contributors"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


def test_a_boolean_total_is_not_mistaken_for_a_count(monkeypatch):
    # `True` is an int in Python, and would read as a total of one.
    monkeypatch.setattr(
        cla, "gh_api",
        lambda endpoint, **kw: {"commits": [{"author": {"login": "a", "id": 1}}],
                                "total_commits": True},
    )
    with pytest.raises(RuntimeError, match="no usable"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


def test_an_empty_comparison_enumerates_only_the_author(monkeypatch):
    monkeypatch.setattr(
        cla, "gh_api", lambda endpoint, **kw: {"commits": [], "total_commits": 0}
    )
    assert cla.pr_contributors("o/r", _pr(), set(), "tok") == {"contributor"}


# ── The whole gate, end to end ───────────────────────────────────────────


def _run_main(monkeypatch, tmp_path, event, responses, statuses, *, mutable_commits=()):
    """Drive `main()` with every GitHub call answered from `responses`.

    `mutable_commits` is what `pulls/{n}/commits` — the list a force-push moves
    — would return. A correct gate never reads it, so a test can point it at a
    DIFFERENT head's commits and still expect the verdict for the enumerated
    one. That disagreement is what makes these fixtures bite on their own
    rather than needing the source edited by hand.
    """
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
                return value(endpoint) if callable(value) else value
        raise AssertionError(f"unexpected call {endpoint}")

    monkeypatch.setattr(cla, "gh_api", fake_api)
    monkeypatch.setattr(
        cla, "gh_paginated",
        lambda endpoint, **kw: list(mutable_commits) if "/commits" in endpoint else [],
    )
    return cla.main()


def _signature_store(*logins):
    import base64

    return base64.b64encode(
        json.dumps({"signedContributors": [{"name": n} for n in logins]}).encode()
    ).decode()


def test_an_unsigned_head_is_failed_even_after_an_a_b_a_force_push(monkeypatch, tmp_path):
    """The regression the immutable enumeration exists for.

    Head A carries a commit by someone who never signed. Between the two head
    reads the branch goes to head B — whose every author HAS signed — and back
    to A, so a before/after comparison sees no movement at all.

    Everything except A's own commit author is signed here, the pull request
    author included, so the failure can only come from having enumerated A. A
    check reading the mutable commit list would answer for B and pass.
    """
    statuses: list[tuple[str, str]] = []
    head_reads = iter(["aaa", "aaa"])  # A both times: the sequence is invisible
    ranges_asked: list[str] = []

    def compare(endpoint):
        ranges_asked.append(endpoint)
        if endpoint.startswith("repos/o/r/compare/base0...bbb"):
            return _compare("signed-on-b")
        return _compare("unsigned-on-a")

    rc = _run_main(
        monkeypatch,
        tmp_path,
        {"pull_request": {"number": 7}, "repository": {"id": 1},
         "action": "synchronize"},
        [
            ("repos/o/r/pulls/7", lambda _: _pr(head=next(head_reads))),
            ("repos/o/r/compare/", compare),
            ("repos/o/r/contents/",
             {"content": _signature_store("contributor", "signed-on-b"), "sha": "s1"}),
            ("repos/o/r/issues/7/comments", []),
        ],
        statuses,
        # What the mutable list holds at the moment it would be read: head B's
        # work, every author signed. A gate reading it reports success.
        mutable_commits=[{"author": {"login": "signed-on-b", "id": 3}}],
    )
    assert rc == 1, "the unsigned author on head A must fail the run"
    assert statuses == [("aaa", "failure")]
    assert all("...aaa" in asked for asked in ranges_asked), ranges_asked


def test_a_fully_signed_pull_request_turns_its_head_green(monkeypatch, tmp_path):
    # The green path. A signed commit author beside the signed pull request
    # author keeps the fixture realistic, but note that this case alone cannot
    # show the verdict depends on enumeration — an empty commit list would pass
    # it too. The unsigned cases above and below are what pin that.
    statuses: list[tuple[str, str]] = []
    rc = _run_main(
        monkeypatch,
        tmp_path,
        {"pull_request": {"number": 7}, "repository": {"id": 1}, "action": "opened"},
        [
            ("repos/o/r/pulls/7", lambda _: _pr()),
            ("repos/o/r/compare/", lambda _: _compare("signed-collaborator")),
            ("repos/o/r/contents/",
             {"content": _signature_store("contributor", "signed-collaborator"),
              "sha": "s1"}),
            ("repos/o/r/issues/7/comments", []),
        ],
        statuses,
    )
    assert rc == 0
    assert statuses == [("aaa", "success")]


def test_one_unsigned_commit_author_is_enough_to_fail(monkeypatch, tmp_path):
    # The pull request author and one collaborator have signed; a third has not.
    statuses: list[tuple[str, str]] = []
    rc = _run_main(
        monkeypatch,
        tmp_path,
        {"pull_request": {"number": 7}, "repository": {"id": 1}, "action": "opened"},
        [
            ("repos/o/r/pulls/7", lambda _: _pr()),
            ("repos/o/r/compare/", lambda _: _compare("signed-collaborator", "newcomer")),
            ("repos/o/r/contents/",
             {"content": _signature_store("contributor", "signed-collaborator"),
              "sha": "s1"}),
            ("repos/o/r/issues/7/comments", []),
        ],
        statuses,
    )
    assert rc == 1
    assert statuses == [("aaa", "failure")]


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
            ("repos/o/r/pulls/7", lambda _: _pr(head=next(head_reads))),
            ("repos/o/r/compare/base0...aaa", _compare("someone")),
            ("repos/o/r/contents/", {"content": "", "sha": None}),
        ],
        statuses,
    )
    assert rc == 0
    assert statuses == []
