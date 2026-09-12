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
    people, _ = cla.pr_contributors("o/r", _pr(), set(), "tok")
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
    assert "alice" in cla.pr_contributors("o/r", _pr(), set(), "tok")[0]


def test_an_allowlisted_login_is_not_required_to_sign(monkeypatch):
    monkeypatch.setattr(cla, "gh_api", lambda endpoint, **kw: _compare("dependabot[bot]"))
    people, unresolved = cla.pr_contributors(
        "o/r", _pr(user="memtomem"), {"dependabot[bot]", "memtomem"}, "tok"
    )
    assert people == set() and unresolved == []


def test_a_compare_without_a_commit_list_is_an_error(monkeypatch):
    monkeypatch.setattr(cla, "gh_api", lambda endpoint, **kw: {"status": "identical"})
    with pytest.raises(RuntimeError, match="no commit list"):
        cla.pr_contributors("o/r", _pr(), set(), "tok")


# ── The A -> B -> A force-push sequence ──────────────────────────────────


def test_enumeration_asks_only_for_the_captured_range(monkeypatch):
    """The range asked for is pinned to the SHAs handed in, never the PR number.

    That is what makes the answer immune to the branch moving underneath: a
    URL naming two fixed SHAs describes one comparison for all time.
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
    people, _ = cla.pr_contributors("o/r", _pr(head="aaa"), set(), "tok")
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


def test_a_signature_store_missing_its_key_signs_nobody():
    # What `load_signatures` synthesizes for a branch that has no file yet.
    assert cla.signed_logins({}) == set()


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
    people, _ = cla.pr_contributors("o/r", _pr(), set(), "tok")
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
    assert cla.pr_contributors("o/r", _pr(), set(), "tok")[0] == {"contributor"}


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


def test_the_verdict_ignores_the_commit_list_the_branch_has_moved(monkeypatch, tmp_path):
    """Head A is judged even while the mutable list describes another head.

    The pull request's own commit list follows the branch, so mid-check it can
    hold head B's work — here, entirely by signed authors. The compare range
    asked for stays A's, and A carries one author who never signed, so the run
    must fail. A gate reading the mutable list would see only signed authors
    and pass, which is what reverting the enumeration makes this test show.

    This is the A -> B -> A shape reduced to what a test can actually observe:
    both head reads return A, so no before/after comparison separates them, and
    the only thing that differs between a correct and an incorrect gate is
    WHICH source it consults.
    """
    statuses: list[tuple[str, str]] = []
    head_reads = iter(["aaa", "aaa"])  # A both times: the sequence is invisible
    ranges_asked: list[str] = []

    def compare(endpoint):
        ranges_asked.append(endpoint)
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


# ── Commits GitHub cannot attribute ──────────────────────────────────────


def _unattributed(sha="c0ffee1234567890"):
    """What GitHub returns for a commit whose email is on no account."""
    return {"author": None, "sha": sha}


def test_a_commit_with_no_resolvable_author_is_reported_not_skipped(monkeypatch):
    # Skipping it means NOBODY has to sign for that commit, so the gate would
    # go green having established nothing about whoever wrote it.
    monkeypatch.setattr(
        cla, "gh_api",
        lambda endpoint, **kw: {"commits": [_unattributed()], "total_commits": 1},
    )
    people, unresolved = cla.pr_contributors("o/r", _pr(), set(), "tok")
    assert people == {"contributor"}
    assert unresolved == ["c0ffee123456"]


def test_an_author_object_without_a_login_is_also_unresolved(monkeypatch):
    monkeypatch.setattr(
        cla, "gh_api",
        lambda endpoint, **kw: {"commits": [{"author": {"id": 5}, "sha": "abc123456789"}],
                                "total_commits": 1},
    )
    _, unresolved = cla.pr_contributors("o/r", _pr(), set(), "tok")
    assert unresolved == ["abc123456789"]


def test_an_unresolvable_pull_request_author_is_reported(monkeypatch):
    monkeypatch.setattr(
        cla, "gh_api", lambda endpoint, **kw: {"commits": [], "total_commits": 0}
    )
    pr = _pr()
    pr["user"] = None
    _, unresolved = cla.pr_contributors("o/r", pr, set(), "tok")
    assert unresolved == ["the pull request author"]


def test_an_unattributed_commit_fails_the_run_even_when_everyone_signed(
    monkeypatch, tmp_path
):
    """The bypass: one commit nobody is named as having written.

    Every login the gate CAN see has signed, so a check that skips the
    unattributable commit publishes success — having established nothing about
    its author. The verdict must be failure instead.
    """
    statuses: list[tuple[str, str]] = []
    rc = _run_main(
        monkeypatch,
        tmp_path,
        {"pull_request": {"number": 7}, "repository": {"id": 1}, "action": "opened"},
        [
            ("repos/o/r/pulls/7", lambda _: _pr()),
            ("repos/o/r/compare/", lambda _: {
                "commits": [{"author": {"login": "signed-collaborator", "id": 2}},
                            _unattributed()],
                "total_commits": 2,
            }),
            ("repos/o/r/contents/",
             {"content": _signature_store("contributor", "signed-collaborator"),
              "sha": "s1"}),
            ("repos/o/r/issues/7/comments", []),
        ],
        statuses,
    )
    assert rc == 1, "an unattributable commit must not pass the gate"
    assert statuses == [("aaa", "failure")]


# ── Reading signatures as late as possible ───────────────────────────────


def test_a_signature_landing_during_the_check_is_seen(monkeypatch, tmp_path):
    """A signing run can finish while this one is enumerating.

    Both runs are about the same head, so no head comparison separates them.
    Publishing failure from the snapshot read before enumeration would leave a
    signed pull request red with nothing left to correct it — that signing run
    has already finished and will not run again.
    """
    statuses: list[tuple[str, str]] = []
    reads = iter([
        _signature_store(),                                 # before enumeration
        _signature_store("contributor", "collaborator"),    # signed meanwhile
    ])
    rc = _run_main(
        monkeypatch,
        tmp_path,
        {"pull_request": {"number": 7}, "repository": {"id": 1}, "action": "opened"},
        [
            ("repos/o/r/pulls/7", lambda _: _pr()),
            ("repos/o/r/compare/", lambda _: _compare("collaborator")),
            ("repos/o/r/contents/", lambda _: {"content": next(reads), "sha": "s1"}),
            ("repos/o/r/issues/7/comments", []),
        ],
        statuses,
    )
    assert rc == 0
    assert statuses == [("aaa", "success")]


# ── The signing path ─────────────────────────────────────────────────────
#
# This is the half that WRITES, with the elevated token, to a branch the rest
# of the gate then trusts. It had no coverage at all.


def test_a_signature_is_appended_with_its_provenance():
    store = {"signedContributors": []}
    assert cla.record_signature(
        store, login="Alice", user_id=7, comment_id=99,
        created_at="2026-09-12T00:00:00Z", repo_id=1, pull_request_no=5,
    ) is True
    (row,) = store["signedContributors"]
    assert row == {"name": "Alice", "id": 7, "comment_id": 99,
                   "created_at": "2026-09-12T00:00:00Z", "repoId": 1,
                   "pullRequestNo": 5}


def test_signing_twice_does_not_duplicate_the_record():
    store = {"signedContributors": [{"name": "alice"}]}
    assert cla.record_signature(
        store, login="Alice", user_id=7, comment_id=99,
        created_at="t", repo_id=1, pull_request_no=5,
    ) is False
    assert len(store["signedContributors"]) == 1


def test_a_missing_signature_file_reads_as_an_empty_store(monkeypatch):
    # The first signature ever recorded writes a file that does not exist yet,
    # so a 404 must be an empty store rather than an error.
    def fake_api(endpoint, **kw):
        raise cla.GitHubApiError(["gh"], "gh: HTTP 404 Not Found")

    monkeypatch.setattr(cla, "gh_api", fake_api)
    store, sha = cla.load_signatures("o/r", "cla-signatures", "s.json", "tok")
    assert store == {"signedContributors": []} and sha is None


def test_any_other_read_failure_is_not_mistaken_for_an_empty_store(monkeypatch):
    # Treating a 500 as "nobody has signed" would re-request signatures from
    # people who already gave them, and on the write path would overwrite the
    # store with an empty one.
    def fake_api(endpoint, **kw):
        raise cla.GitHubApiError(["gh"], "gh: HTTP 500 Internal Server Error")

    monkeypatch.setattr(cla, "gh_api", fake_api)
    with pytest.raises(cla.GitHubApiError):
        cla.load_signatures("o/r", "cla-signatures", "s.json", "tok")


def test_an_empty_signature_file_reads_as_an_empty_store(monkeypatch):
    import base64

    monkeypatch.setattr(
        cla, "gh_api",
        lambda endpoint, **kw: {"content": base64.b64encode(b"  \n").decode(), "sha": "s1"},
    )
    store, sha = cla.load_signatures("o/r", "b", "s.json", "tok")
    assert store == {"signedContributors": []} and sha == "s1"


def test_the_write_carries_the_blob_sha_so_a_concurrent_change_conflicts(monkeypatch):
    # Without the sha the contents API overwrites unconditionally, and two
    # signatures landing together would lose one.
    seen = {}
    monkeypatch.setattr(cla, "gh_api",
                        lambda endpoint, **kw: seen.update(kw) or {})
    cla.write_signatures("o/r", "cla-signatures", "s.json", "tok",
                         {"signedContributors": [{"name": "a"}]}, "blob1", "msg")
    assert seen["payload"]["sha"] == "blob1"
    assert seen["payload"]["branch"] == "cla-signatures"
    assert seen["method"] == "PUT"


def test_the_first_write_omits_the_sha(monkeypatch):
    seen = {}
    monkeypatch.setattr(cla, "gh_api", lambda endpoint, **kw: seen.update(kw) or {})
    cla.write_signatures("o/r", "b", "s.json", "tok", {"signedContributors": []}, None, "msg")
    assert "sha" not in seen["payload"]


def test_a_conflicting_write_is_retried_against_the_reread_store(monkeypatch):
    """Two people signing at once must both end up recorded.

    The first write loses the race and returns 409. Retrying must re-read the
    store — otherwise the retry rewrites the stale snapshot and drops whichever
    signature landed in between.
    """
    import base64

    store_state = {"signedContributors": [{"name": "first-signer"}]}
    writes = []
    attempts = {"n": 0}

    def fake_api(endpoint, *, method="GET", token=None, payload=None):
        if method == "PUT":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise cla.GitHubApiError(["gh"], "gh: HTTP 409 Conflict")
            writes.append(json.loads(base64.b64decode(payload["content"])))
            return {}
        return {"content": base64.b64encode(json.dumps(store_state).encode()).decode(),
                "sha": "s1"}

    monkeypatch.setattr(cla, "gh_api", fake_api)
    _, changed = cla.record_signature_with_retry(
        repo="o/r", branch="b", path="s.json", token="tok", login="second-signer",
        user_id=2, comment_id=3, created_at="t", repo_id=1, pull_request_no=5,
    )
    assert changed is True
    assert attempts["n"] == 2, "the conflict must be retried exactly once"
    names = {row["name"] for row in writes[-1]["signedContributors"]}
    assert names == {"first-signer", "second-signer"}, "the retry must not drop the other signer"


def test_a_second_conflict_is_not_swallowed(monkeypatch):
    import base64

    def fake_api(endpoint, *, method="GET", token=None, payload=None):
        if method == "PUT":
            raise cla.GitHubApiError(["gh"], "gh: HTTP 409 Conflict")
        return {"content": base64.b64encode(b'{"signedContributors": []}').decode(), "sha": "s"}

    monkeypatch.setattr(cla, "gh_api", fake_api)
    with pytest.raises(cla.GitHubApiError):
        cla.record_signature_with_retry(
            repo="o/r", branch="b", path="s.json", token="tok", login="x",
            user_id=1, comment_id=1, created_at="t", repo_id=1, pull_request_no=1,
        )


def test_a_non_conflict_write_failure_is_raised_immediately(monkeypatch):
    import base64
    attempts = {"n": 0}

    def fake_api(endpoint, *, method="GET", token=None, payload=None):
        if method == "PUT":
            attempts["n"] += 1
            raise cla.GitHubApiError(["gh"], "gh: HTTP 403 Forbidden")
        return {"content": base64.b64encode(b'{"signedContributors": []}').decode(), "sha": "s"}

    monkeypatch.setattr(cla, "gh_api", fake_api)
    with pytest.raises(cla.GitHubApiError):
        cla.record_signature_with_retry(
            repo="o/r", branch="b", path="s.json", token="tok", login="x",
            user_id=1, comment_id=1, created_at="t", repo_id=1, pull_request_no=1,
        )
    assert attempts["n"] == 1, "only a conflict is retried"


# ── Which comments count as signing ──────────────────────────────────────


@pytest.mark.parametrize(
    "body,expected",
    [
        ("I have read the CLA Document and I hereby sign the CLA", "sign"),
        ("  I have read the CLA Document and I hereby sign the CLA  ", "sign"),
        ("recheck", "recheck"),
        ("RECHECK", "recheck"),
        # A quoted or discussed signature is not a signature.
        ("> I have read the CLA Document and I hereby sign the CLA", "ignore"),
        ("I think I have read the CLA Document and I hereby sign the CLA", "ignore"),
        ("looks good to me", "ignore"),
        ("", "ignore"),
    ],
)
def test_only_the_exact_statement_signs(body, expected):
    event = {"comment": {"body": body}}
    assert cla.comment_command(event, cla_signature_text()) == expected


def cla_signature_text():
    return "I have read the CLA Document and I hereby sign the CLA"


def test_a_non_comment_event_has_no_command():
    assert cla.comment_command({"pull_request": {"number": 1}}, cla_signature_text()) is None


def test_a_comment_on_a_plain_issue_is_not_a_pull_request():
    # GitHub marks a pull request's comment thread by attaching a populated
    # `pull_request` object to the issue; a plain issue has none.
    assert cla.pr_number_from_event({"issue": {"number": 4}}) is None
    assert cla.pr_number_from_event(
        {"issue": {"number": 4, "pull_request": {"url": "https://api/pulls/4"}}}
    ) == 4
    assert cla.pr_number_from_event({"pull_request": {"number": 9}}) == 9
