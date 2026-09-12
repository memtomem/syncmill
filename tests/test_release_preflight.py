"""Hermetic tests for the fail-closed release preflight.

The script lives in ``scripts/`` rather than the package — it runs under
``uv run --no-project`` in CI with nothing installed — so it is loaded here by
path. Nothing in this file reaches the network: the CI wait and the index read
are driven through their injected fetch seams.
"""

from __future__ import annotations

import hashlib
import io
import importlib.util
import tarfile
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_preflight.py"
_spec = importlib.util.spec_from_file_location("release_preflight", _SCRIPT)
assert _spec and _spec.loader
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


# ── Tag grammar ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v0.2.0", "0.2.0"),
        ("v10.20.30", "10.20.30"),
        ("test-v0.2.0a1", "0.2.0"),
        ("test-v0.2.0a12", "0.2.0"),
    ],
)
def test_supported_tags_yield_their_distribution_version(tag, expected):
    # A rehearsal tag maps to the SAME version as production on purpose: the
    # dry run must publish the artifact that will be released, not a lookalike.
    assert rp.version_from_tag(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "0.2.0",            # no prefix
        "v0.2",             # not three components
        "v0.2.0.1",         # four
        "v0.2.0a1",         # a production tag may not be a prerelease
        "test-v0.2.0",      # a rehearsal tag must carry its aN
        "test-v0.2.0b1",    # only `a` is accepted
        "v0.2.0-rc1",
        "release-0.2.0",
        "",
        "vv0.2.0",
        "v0.2.0\n",         # fullmatch, so a trailing newline is not a version
    ],
)
def test_unsupported_tags_are_refused(tag):
    with pytest.raises(rp.ReleaseCheckError, match="unsupported release tag"):
        rp.version_from_tag(tag)


# ── Version agreement ────────────────────────────────────────────────────


def _repo(tmp_path: Path, *, project="0.2.0", lock="0.2.0", changelog="## 0.2.0 — 2026-09-12"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "syncmill"\nversion = "{project}"\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "click"\nversion = "8.1.0"\n\n'
        f'[[package]]\nname = "syncmill"\nversion = "{lock}"\nsource = {{ editable = "." }}\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## Unreleased\n\n{changelog}\n\n- something\n", encoding="utf-8"
    )
    return tmp_path


def test_contract_passes_when_every_source_agrees(tmp_path):
    assert rp.validate_contract("v0.2.0", _repo(tmp_path)) == "0.2.0"


def test_contract_accepts_a_rehearsal_tag_for_the_same_version(tmp_path):
    assert rp.validate_contract("test-v0.2.0a1", _repo(tmp_path)) == "0.2.0"


def test_contract_rejects_a_stale_project_version(tmp_path):
    with pytest.raises(rp.ReleaseCheckError, match="does not match project version"):
        rp.validate_contract("v0.2.0", _repo(tmp_path, project="0.1.4"))


def test_contract_rejects_a_lockfile_left_unbumped(tmp_path):
    # `uv lock` is a separate step from editing pyproject, and the lock carries
    # the project's own version — forgetting it ships a stale environment.
    with pytest.raises(rp.ReleaseCheckError, match="does not match lock version"):
        rp.validate_contract("v0.2.0", _repo(tmp_path, lock="0.1.4"))


def test_contract_rejects_an_undated_changelog_section(tmp_path):
    # A bare `## 0.2.0` is a draft. The date is what says the section was folded
    # deliberately rather than left mid-edit.
    with pytest.raises(rp.ReleaseCheckError, match="no dated release heading"):
        rp.validate_contract("v0.2.0", _repo(tmp_path, changelog="## 0.2.0"))


def test_contract_rejects_a_changelog_with_only_an_unreleased_section(tmp_path):
    with pytest.raises(rp.ReleaseCheckError, match="no dated release heading"):
        rp.validate_contract("v0.2.0", _repo(tmp_path, changelog="## 0.1.4 — 2026-08-01"))


def test_contract_accepts_a_hyphen_dated_heading(tmp_path):
    assert rp.validate_contract("v0.2.0", _repo(tmp_path, changelog="## 0.2.0 - 2026-09-12"))


def test_contract_reports_a_missing_file_rather_than_raising_oserror(tmp_path):
    with pytest.raises(rp.ReleaseCheckError, match="cannot read valid TOML"):
        rp.validate_contract("v0.2.0", tmp_path)


def test_lock_version_refuses_an_ambiguous_lockfile(tmp_path):
    repo = _repo(tmp_path)
    (repo / "uv.lock").write_text(
        '[[package]]\nname = "syncmill"\nversion = "0.2.0"\nsource = { editable = "." }\n\n'
        '[[package]]\nname = "syncmill"\nversion = "0.2.0"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    with pytest.raises(rp.ReleaseCheckError, match="found 2"):
        rp.lock_version(repo)


# ── Artifact contract ────────────────────────────────────────────────────


def _metadata(version="0.2.0", name="syncmill", license_expression="Apache-2.0", license_file=True):
    lines = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    if license_expression is not None:
        lines.append(f"License-Expression: {license_expression}")
    if license_file:
        lines.append("License-File: LICENSE")
    return "\n".join(lines) + "\n\nbody\n"


def _wheel(dist: Path, version="0.2.0", *, license_at="licenses", **kw):
    """`license_at`: "licenses" (PEP 639), "dist-info" (legacy), "elsewhere", or None."""
    path = dist / f"syncmill-{version}-py3-none-any.whl"
    placements = {
        "licenses": f"syncmill-{version}.dist-info/licenses/LICENSE",
        "dist-info": f"syncmill-{version}.dist-info/LICENSE",
        "elsewhere": "syncmill/vendor/LICENSE",
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"syncmill-{version}.dist-info/METADATA", _metadata(version, **kw))
        if license_at is not None:
            archive.writestr(placements[license_at], "Apache 2.0")
        archive.writestr("syncmill/__init__.py", "")
    return path


def _sdist(dist: Path, version="0.2.0", *, extra=(), license_at="root", **kw):
    """`license_at`: "root", "elsewhere", or None."""
    path = dist / f"syncmill-{version}.tar.gz"
    root = f"syncmill-{version}"
    placements = {"root": f"{root}/LICENSE", "elsewhere": f"{root}/docs/LICENSE"}
    with tarfile.open(path, "w:gz") as archive:
        for name, body in [
            (f"{root}/PKG-INFO", _metadata(version, **kw)),
            (f"{root}/pyproject.toml", ""),
            *([(placements[license_at], "Apache 2.0")] if license_at else []),
            (f"{root}/README.md", ""),
            *[(f"{root}/{name}", "") for name in extra],
        ]:
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_artifacts_accept_one_matching_wheel_and_sdist(tmp_path):
    _wheel(tmp_path)
    _sdist(tmp_path)
    wheel, sdist = rp.validate_artifacts(tmp_path, "0.2.0")
    assert wheel.name.endswith(".whl") and sdist.name.endswith(".tar.gz")


def test_artifacts_refuse_a_leftover_build_from_another_version(tmp_path):
    # A dist directory carrying two wheels is the classic stale-build mistake;
    # publishing would upload both.
    _wheel(tmp_path)
    _wheel(tmp_path, version="0.1.4")
    _sdist(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="expected one wheel and one sdist"):
        rp.validate_artifacts(tmp_path, "0.2.0")


def test_artifacts_refuse_a_missing_sdist(tmp_path):
    _wheel(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="expected one wheel and one sdist"):
        rp.validate_artifacts(tmp_path, "0.2.0")


def test_artifacts_refuse_metadata_that_disagrees_with_the_tag(tmp_path):
    _wheel(tmp_path, version="0.2.0")
    _sdist(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="metadata version"):
        rp.validate_artifacts(tmp_path, "0.3.0")


def test_artifacts_require_the_license_expression(tmp_path):
    # The whole point of the plumbing change: an unlicensed upload is worse
    # than a failed one.
    _wheel(tmp_path, license_expression=None)
    _sdist(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="License-Expression"):
        rp.validate_artifacts(tmp_path, "0.2.0")


def test_artifacts_require_the_metadata_to_name_a_license_file(tmp_path):
    _wheel(tmp_path, license_file=False)
    _sdist(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="no License-File"):
        rp.validate_artifacts(tmp_path, "0.2.0")


def test_artifacts_require_the_wheel_to_actually_carry_the_license(tmp_path):
    # The header alone proves nothing — a consumer needs the file. This is the
    # case the header-only check passed: valid metadata, absent file.
    _wheel(tmp_path, license_at=None)
    _sdist(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="wheel declares License-File"):
        rp.validate_artifacts(tmp_path, "0.2.0")


def test_artifacts_require_the_sdist_to_actually_carry_the_license(tmp_path):
    _wheel(tmp_path)
    _sdist(tmp_path, license_at=None)
    with pytest.raises(rp.ReleaseCheckError, match="sdist declares License-File"):
        rp.validate_artifacts(tmp_path, "0.2.0")


def test_a_license_somewhere_else_in_the_wheel_does_not_count(tmp_path):
    # `License-File` declares a PATH. A vendored copy under the package satisfies
    # a basename search while leaving the location the metadata promises empty,
    # which is where a consumer's tooling looks.
    _wheel(tmp_path, license_at="elsewhere")
    _sdist(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="wheel declares License-File"):
        rp.validate_artifacts(tmp_path, "0.2.0")


def test_a_license_somewhere_else_in_the_sdist_does_not_count(tmp_path):
    _wheel(tmp_path)
    _sdist(tmp_path, license_at="elsewhere")
    with pytest.raises(rp.ReleaseCheckError, match="sdist declares License-File"):
        rp.validate_artifacts(tmp_path, "0.2.0")


def test_a_wheel_from_before_pep_639_still_passes(tmp_path):
    # Older builds place the file directly in `<dist-info>/`. That is still the
    # metadata directory this METADATA came from, so it counts.
    _wheel(tmp_path, license_at="dist-info")
    _sdist(tmp_path)
    assert rp.validate_artifacts(tmp_path, "0.2.0")


def test_artifacts_refuse_an_sdist_carrying_local_state(tmp_path):
    # `syncmill.toml` is a developer's local config, `dist/` a previous build.
    # Neither belongs in a published sdist, and both sit at the repository root
    # where a packaging change could sweep them in.
    _wheel(tmp_path)
    _sdist(tmp_path, extra=["syncmill.toml", "dist"])
    with pytest.raises(rp.ReleaseCheckError, match="unexpected top-level entries: dist, syncmill"):
        rp.validate_artifacts(tmp_path, "0.2.0")


# ── CI contract ──────────────────────────────────────────────────────────


def _run(sha="abc", branch="main", event="push", status="completed", conclusion="success", id=1):
    return {
        "id": id,
        "head_sha": sha,
        "head_branch": branch,
        "event": event,
        "status": status,
        "conclusion": conclusion,
    }


def _wait(rows_sequence, **kw):
    calls = {"n": 0}

    def fetch(repository, sha, token):
        rows = rows_sequence[min(calls["n"], len(rows_sequence) - 1)]
        calls["n"] += 1
        if isinstance(rows, Exception):
            raise rows
        return rows

    clock = {"t": 0.0}

    def monotonic():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    defaults = dict(
        repository="memtomem/syncmill",
        sha="abc",
        token="t",
        timeout_seconds=100.0,
        interval_seconds=10.0,
        fetch_runs=fetch,
        monotonic=monotonic,
        sleep=sleep,
    )
    defaults.update(kw)
    return rp.wait_for_exact_main_ci(**defaults)


def test_wait_ci_accepts_the_exact_main_push_run():
    assert _wait([[_run()]])["id"] == 1


def test_wait_ci_polls_until_the_run_completes():
    assert _wait([[_run(status="in_progress", conclusion=None)], [_run()]])["id"] == 1


def test_wait_ci_ignores_a_green_run_of_a_different_commit():
    # The tagged commit is the one being published; another commit's success
    # says nothing about it.
    with pytest.raises(rp.ReleaseCheckError, match="timed out"):
        _wait([[_run(sha="other")]])


def test_wait_ci_ignores_a_pull_request_run_of_the_same_commit():
    # A pull request run tested the merge preview, not what landed on main.
    with pytest.raises(rp.ReleaseCheckError, match="timed out"):
        _wait([[_run(event="pull_request")]])


@pytest.mark.parametrize("conclusion", sorted(rp._TERMINAL_FAILURES))
def test_wait_ci_fails_closed_on_every_terminal_failure(conclusion):
    with pytest.raises(rp.ReleaseCheckError, match=f"completed as {conclusion}"):
        _wait([[_run(conclusion=conclusion)]])


def test_wait_ci_tolerates_a_transient_api_error_then_succeeds():
    assert _wait([OSError("blip"), [_run()]])["id"] == 1


def test_wait_ci_fails_closed_after_three_consecutive_api_errors():
    # An API that keeps failing is not evidence of a green build.
    with pytest.raises(rp.ReleaseCheckError, match="3 consecutive times"):
        _wait([OSError("down")])


# ── Published-bytes contract ─────────────────────────────────────────────


def _published(tmp_path):
    wheel = _wheel(tmp_path)
    sdist = _sdist(tmp_path)
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (wheel, sdist)}


def test_verify_published_passes_when_the_index_serves_the_built_bytes(tmp_path):
    remote = _published(tmp_path)
    rp.verify_published(
        dist=tmp_path,
        version="0.2.0",
        index_api="https://pypi.org/simple",
        fetch=lambda api, version: remote,
        sleep=lambda _: None,
    )


def test_verify_published_fails_when_the_index_holds_different_bytes(tmp_path):
    # The reason this check exists: `skip-existing` reports success when a file
    # of that name is already there, whatever it contains — so a corrected
    # build retried under a new rehearsal tag would look published and not be.
    remote = {name: "0" * 64 for name in _published(tmp_path)}
    with pytest.raises(rp.ReleaseCheckError, match="already taken"):
        rp.verify_published(
            dist=tmp_path,
            version="0.2.0",
            index_api="https://test.pypi.org/simple",
            fetch=lambda api, version: remote,
            sleep=lambda _: None,
        )


def test_verify_published_retries_while_the_index_propagates(tmp_path):
    remote = _published(tmp_path)
    seen = {"n": 0}

    def fetch(api, version):
        seen["n"] += 1
        return remote if seen["n"] > 2 else {}

    rp.verify_published(
        dist=tmp_path,
        version="0.2.0",
        index_api="https://pypi.org/simple",
        fetch=fetch,
        sleep=lambda _: None,
    )
    assert seen["n"] == 3


def test_verify_published_fails_when_a_file_never_appears(tmp_path):
    _published(tmp_path)
    with pytest.raises(rp.ReleaseCheckError, match="has not published"):
        rp.verify_published(
            dist=tmp_path,
            version="0.2.0",
            index_api="https://pypi.org/simple",
            attempts=2,
            fetch=lambda api, version: {},
            sleep=lambda _: None,
        )


def test_verify_published_surfaces_a_persistent_index_error(tmp_path):
    _published(tmp_path)

    def fetch(api, version):
        raise OSError("index unreachable")

    with pytest.raises(rp.ReleaseCheckError, match="last index error: index unreachable"):
        rp.verify_published(
            dist=tmp_path,
            version="0.2.0",
            index_api="https://pypi.org/simple",
            attempts=2,
            fetch=fetch,
            sleep=lambda _: None,
        )


def test_verify_published_refuses_an_empty_dist(tmp_path):
    with pytest.raises(rp.ReleaseCheckError, match="no distributions"):
        rp.verify_published(
            dist=tmp_path,
            version="0.2.0",
            index_api="https://pypi.org/simple",
            fetch=lambda api, version: {},
            sleep=lambda _: None,
        )


# ── CLI surface ──────────────────────────────────────────────────────────


def test_cli_contract_writes_the_version_to_github_output(tmp_path, capsys):
    repo = _repo(tmp_path / "repo")
    output = tmp_path / "gh-output"
    assert rp.main(
        ["contract", "--tag", "v0.2.0", "--repo-root", str(repo), "--github-output", str(output)]
    ) == 0
    assert output.read_text() == "version=0.2.0\n"
    assert capsys.readouterr().out.strip() == "0.2.0"


def test_cli_returns_one_with_a_message_rather_than_a_traceback(tmp_path, capsys):
    repo = _repo(tmp_path / "repo", project="0.1.4")
    assert rp.main(["contract", "--tag", "v0.2.0", "--repo-root", str(repo)]) == 1
    assert "release preflight failed" in capsys.readouterr().err


# ── Index request construction ───────────────────────────────────────────


def test_the_index_read_asks_for_the_pypi_json_api(monkeypatch):
    # Without this Accept header the Simple API serves HTML, and the failure
    # lands AFTER the upload. The old tests stubbed the whole fetch function,
    # so no request was ever built and this could not have been caught.
    seen = {}

    class _Response:
        def read(self):
            return b'{"files": []}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        return _Response()

    monkeypatch.setattr(rp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(rp.json, "load", lambda handle: {"files": []})
    assert rp._fetch_index_files("https://pypi.org/simple", "0.2.0") == {}
    assert seen["url"] == "https://pypi.org/simple/syncmill/"
    accept = next(v for k, v in seen["headers"].items() if k.lower() == "accept")
    assert accept == "application/vnd.pypi.simple.v1+json"
    # A PyPI read must not carry GitHub's API-version header.
    assert not [k for k in seen["headers"] if k.lower() == "x-github-api-version"]


def test_the_github_read_keeps_its_own_headers(monkeypatch):
    seen = {}

    def fake_build(url, headers):
        seen.update(headers)
        raise RuntimeError("stop before the network")

    monkeypatch.setattr(rp, "_build_request", fake_build)
    with pytest.raises(RuntimeError):
        rp._request_json("https://api.github.com/x", "tok")
    assert seen["Accept"] == "application/vnd.github+json"
    assert seen["X-GitHub-Api-Version"] == "2022-11-28"
    assert seen["Authorization"] == "Bearer tok"


def test_a_partial_index_listing_is_not_mistaken_for_success(tmp_path):
    # Only the wheel published; the sdist is still missing. Reporting success
    # here would leave a release half on the index.
    wheel = _wheel(tmp_path)
    _sdist(tmp_path)
    remote = {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()}
    with pytest.raises(rp.ReleaseCheckError, match="has not published"):
        rp.verify_published(
            dist=tmp_path,
            version="0.2.0",
            index_api="https://pypi.org/simple",
            attempts=2,
            fetch=lambda api, version: remote,
            sleep=lambda _: None,
        )


def _index_payload(monkeypatch, payload):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(rp.urllib.request, "urlopen", lambda request, timeout=None: _Response())
    monkeypatch.setattr(rp.json, "load", lambda handle: payload)
    return rp._fetch_index_files("https://pypi.org/simple", "0.2.0")


def test_an_index_row_without_a_sha256_is_dropped_rather_than_trusted(monkeypatch):
    # A row carrying only md5 cannot answer "are these my bytes?", so it must
    # not count as published — dropping it makes verify_published wait or fail
    # rather than accept an unverifiable file.
    files = _index_payload(
        monkeypatch,
        {
            "files": [
                {"filename": "syncmill-0.2.0-py3-none-any.whl", "hashes": {"md5": "x"}},
                {"filename": "syncmill-0.2.0.tar.gz", "hashes": {"sha256": "abc"}},
            ]
        },
    )
    assert files == {"syncmill-0.2.0.tar.gz": "abc"}


def test_the_index_read_ignores_other_versions(monkeypatch):
    files = _index_payload(
        monkeypatch,
        {
            "files": [
                {"filename": "syncmill-0.2.0.tar.gz", "hashes": {"sha256": "new"}},
                {"filename": "syncmill-0.1.4.tar.gz", "hashes": {"sha256": "old"}},
            ]
        },
    )
    assert files == {"syncmill-0.2.0.tar.gz": "new"}


def test_a_listing_that_is_not_a_file_index_is_an_error(monkeypatch):
    with pytest.raises(rp.ReleaseCheckError, match="PEP 691"):
        _index_payload(monkeypatch, {"meta": {"api-version": "1.0"}})
