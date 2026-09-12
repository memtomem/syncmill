#!/usr/bin/env python3
"""Fail-closed release tag, CI, and distribution contract checks.

Every check here answers one question: does the thing about to be published
match the tag that asked for it? The script is stdlib-only so it runs under
``uv run --no-project`` with no environment to resolve, and it raises rather
than warns — a release invariant that cannot be verified is a failed release.

Adapted from memtomem's ``tools/release_preflight.py``; the security-dependency
and extras contracts there are specific to that package and are not reproduced.

Subcommands:
  contract          tag, pyproject, uv.lock and CHANGELOG agree on one version
  artifacts         exactly one wheel and one sdist, with matching metadata
  wait-ci           the exact main-push CI run for this commit succeeded
  verify-published  the index serves the bytes this run just built
"""

from __future__ import annotations

import argparse
import email
import email.message
import hashlib
import json
import os
import re
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable


_PROD_TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
_TEST_TAG_RE = re.compile(r"^test-v(?P<version>\d+\.\d+\.\d+)a\d+$")
_TERMINAL_FAILURES = {"action_required", "cancelled", "failure", "stale", "timed_out"}
_EXPECTED_LICENSE = "Apache-2.0"
# Everything the sdist is allowed to carry at its top level. New entries are a
# deliberate decision, not an accident of what happened to be in the tree: a
# local `syncmill.toml`, a `dist/` or a stray worktree must never ship.
_SDIST_TOP_LEVEL_ALLOWLIST = {
    ".github",
    ".gitignore",
    ".python-version",
    "CHANGELOG.md",
    "CLA.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "contracts",
    "docs",
    "examples",
    "pyproject.toml",
    "scripts",
    "src",
    "syncmill.toml.example",
    "tests",
    "uv.lock",
}


class ReleaseCheckError(RuntimeError):
    """A release invariant was not met."""


# ── Version contract ─────────────────────────────────────────────────────


def version_from_tag(tag: str) -> str:
    """Return the distribution version encoded by a supported release tag.

    A TestPyPI rehearsal tag (``test-v0.2.0a1``) deliberately maps to the SAME
    distribution version as the production tag, so the rehearsal publishes the
    artifact that will be released rather than a lookalike. The consequence is
    that a rehearsal consumes that version on TestPyPI — see verify_published.
    """
    for pattern in (_PROD_TAG_RE, _TEST_TAG_RE):
        match = pattern.fullmatch(tag)
        if match:
            return match.group("version")
    raise ReleaseCheckError(f"unsupported release tag {tag!r}; expected vX.Y.Z or test-vX.Y.ZaN")


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseCheckError(f"cannot read valid TOML from {path}: {exc}") from exc


def project_version(repo_root: Path) -> str:
    pyproject = repo_root / "pyproject.toml"
    value = _load_toml(pyproject).get("project", {}).get("version")
    if not isinstance(value, str) or not value:
        raise ReleaseCheckError(f"missing project.version in {pyproject}")
    return value


def lock_version(repo_root: Path) -> str:
    """The version uv.lock records for syncmill itself.

    The lock carries the project's own version, so a bump that forgets
    ``uv lock`` would otherwise ship a wheel whose pinned environment describes
    the previous release.
    """
    lock_path = repo_root / "uv.lock"
    packages = _load_toml(lock_path).get("package", [])
    matches = [
        row
        for row in packages
        if isinstance(row, dict)
        and row.get("name") == "syncmill"
        and row.get("source") == {"editable": "."}
    ]
    if len(matches) != 1:
        raise ReleaseCheckError(
            f"expected one editable syncmill package in {lock_path}, found {len(matches)}"
        )
    value = matches[0].get("version")
    if not isinstance(value, str) or not value:
        raise ReleaseCheckError(f"missing syncmill version in {lock_path}")
    return value


def changelog_has_release(repo_root: Path, version: str) -> None:
    """Require a DATED release heading, never a bare `## Unreleased`.

    This repository's convention is ``## X.Y.Z — YYYY-MM-DD``; requiring the
    date is what stops a tag being pushed while the section is still a draft.
    """
    path = repo_root / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseCheckError(f"cannot read {path}: {exc}") from exc
    pattern = rf"^## {re.escape(version)} [—-] \d{{4}}-\d{{2}}-\d{{2}}\s*$"
    if not re.search(pattern, text, re.MULTILINE):
        raise ReleaseCheckError(
            f"CHANGELOG.md has no dated release heading for {version} "
            f"(expected '## {version} — YYYY-MM-DD')"
        )


def validate_contract(tag: str, repo_root: Path) -> str:
    """Validate that tag, package, lock and changelog all name one version."""
    expected = version_from_tag(tag)
    actual_project = project_version(repo_root)
    actual_lock = lock_version(repo_root)
    if actual_project != expected:
        raise ReleaseCheckError(
            f"tag version {expected} does not match project version {actual_project}"
        )
    if actual_lock != expected:
        raise ReleaseCheckError(f"tag version {expected} does not match lock version {actual_lock}")
    changelog_has_release(repo_root, expected)
    return expected


# ── Artifact contract ────────────────────────────────────────────────────


def _metadata_from_wheel(path: Path) -> email.message.Message:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            ]
            if len(names) != 1:
                raise ReleaseCheckError(f"expected one dist-info METADATA in {path.name}")
            raw = archive.read(names[0])
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseCheckError(f"cannot read wheel {path}: {exc}") from exc
    return email.message_from_bytes(raw)


def _sdist_members(path: Path) -> tuple[email.message.Message, set[str]]:
    """Return the sdist's PKG-INFO and its top-level entry names."""
    try:
        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
            roots = {name.split("/")[0] for name in names if "/" in name or name}
            if len(roots) != 1:
                raise ReleaseCheckError(f"sdist {path.name} has {len(roots)} top-level directories")
            root = next(iter(roots))
            top_level = {
                name.split("/")[1]
                for name in names
                if name.startswith(f"{root}/") and len(name.split("/")) >= 2 and name.split("/")[1]
            }
            member = archive.extractfile(f"{root}/PKG-INFO")
            if member is None:
                raise ReleaseCheckError(f"sdist {path.name} has no PKG-INFO")
            raw = member.read()
    except (OSError, tarfile.TarError, KeyError) as exc:
        raise ReleaseCheckError(f"cannot read sdist {path}: {exc}") from exc
    return email.message_from_bytes(raw), top_level


def _validate_metadata(metadata: email.message.Message, expected: str, kind: str) -> None:
    name = metadata.get("Name")
    version = metadata.get("Version")
    if name != "syncmill":
        raise ReleaseCheckError(f"{kind} metadata names {name!r}, expected 'syncmill'")
    if version != expected:
        raise ReleaseCheckError(f"{kind} metadata version {version!r} does not match {expected}")
    license_expression = metadata.get("License-Expression")
    if license_expression != _EXPECTED_LICENSE:
        raise ReleaseCheckError(
            f"{kind} metadata License-Expression is {license_expression!r}, "
            f"expected {_EXPECTED_LICENSE!r}"
        )
    if not metadata.get_all("License-File"):
        raise ReleaseCheckError(f"{kind} metadata carries no License-File")


def validate_artifacts(dist: Path, expected: str) -> tuple[Path, Path]:
    """Validate exactly one wheel and one sdist plus their published metadata."""
    wheels = sorted(dist.glob("syncmill-*.whl"))
    sdists = sorted(dist.glob("syncmill-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseCheckError(
            f"expected one wheel and one sdist in {dist}; found {len(wheels)} and {len(sdists)}"
        )
    _validate_metadata(_metadata_from_wheel(wheels[0]), expected, "wheel")
    sdist_metadata, top_level = _sdist_members(sdists[0])
    _validate_metadata(sdist_metadata, expected, "sdist")
    unexpected = sorted(top_level - _SDIST_TOP_LEVEL_ALLOWLIST)
    if unexpected:
        raise ReleaseCheckError(
            f"sdist {sdists[0].name} carries unexpected top-level entries: {', '.join(unexpected)}"
        )
    return wheels[0], sdists[0]


# ── CI contract ──────────────────────────────────────────────────────────


def _request_json(url: str, token: str | None, *, timeout: float = 20.0) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "syncmill-release-preflight",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.load(response)


def _request_runs(repository: str, sha: str, token: str) -> list[dict[str, Any]]:
    encoded_sha = urllib.parse.quote(sha, safe="")
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/ci.yml/runs"
        f"?event=push&head_sha={encoded_sha}&per_page=20"
    )
    payload = _request_json(url, token)
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise ReleaseCheckError("GitHub workflow-runs response has no workflow_runs list")
    return [row for row in runs if isinstance(row, dict)]


def wait_for_exact_main_ci(
    *,
    repository: str,
    sha: str,
    token: str,
    timeout_seconds: float,
    interval_seconds: float,
    fetch_runs: Callable[[str, str, str], list[dict[str, Any]]] = _request_runs,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for the exact main-push CI run, failing closed on errors or failures.

    "Exact" means the same commit, on main, from a push — not a pull request
    run of the same tree, which was green before the merge changed it.
    """
    deadline = monotonic() + timeout_seconds
    consecutive_errors = 0
    while monotonic() < deadline:
        try:
            rows = fetch_runs(repository, sha, token)
            consecutive_errors = 0
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ReleaseCheckError) as exc:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                raise ReleaseCheckError(
                    f"GitHub Actions API failed {consecutive_errors} consecutive times: {exc}"
                ) from exc
            sleep(interval_seconds)
            continue

        exact = [
            row
            for row in rows
            if row.get("head_sha") == sha
            and row.get("head_branch") == "main"
            and row.get("event") == "push"
        ]
        successes = [
            row
            for row in exact
            if row.get("status") == "completed" and row.get("conclusion") == "success"
        ]
        if successes:
            return successes[0]
        failures = [
            row
            for row in exact
            if row.get("status") == "completed" and row.get("conclusion") in _TERMINAL_FAILURES
        ]
        if failures:
            run = failures[0]
            raise ReleaseCheckError(
                f"exact main CI run {run.get('id')} completed as {run.get('conclusion')}"
            )
        sleep(interval_seconds)
    raise ReleaseCheckError(
        f"timed out after {timeout_seconds:g}s waiting for successful main CI at {sha}"
    )


# ── Published-bytes contract ─────────────────────────────────────────────


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch_index_files(index_api: str, version: str) -> dict[str, str]:
    """Map filename → sha256 for one version, from a PEP 691 JSON index."""
    url = f"{index_api.rstrip('/')}/syncmill/"
    payload = _request_json(url, None)
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ReleaseCheckError(f"index {url} did not return a PEP 691 file listing")
    digests: dict[str, str] = {}
    for row in payload["files"]:
        if not isinstance(row, dict):
            continue
        filename = row.get("filename")
        hashes = row.get("hashes")
        if not isinstance(filename, str) or not isinstance(hashes, dict):
            continue
        if f"-{version}" not in filename:
            continue
        digest = hashes.get("sha256")
        if isinstance(digest, str):
            digests[filename] = digest
    return digests


def verify_published(
    *,
    dist: Path,
    version: str,
    index_api: str,
    attempts: int = 10,
    interval_seconds: float = 15.0,
    fetch: Callable[[str, str], dict[str, str]] = _fetch_index_files,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Require the index to serve exactly the bytes this run built.

    ``skip-existing`` lets a publish succeed when the index already holds a file
    of the same name — which is precisely what happens when a rehearsal is
    retried under a new ``aN`` tag, since every ``test-vX.Y.ZaN`` maps to the
    same distribution version. Without this check a corrected build would
    report a green release while the index still served the first attempt.
    Once a version exists on an index, a changed build needs a NEW version.
    """
    local = {path.name: _digest(path.read_bytes()) for path in sorted(dist.iterdir())
             if path.suffix == ".whl" or path.name.endswith(".tar.gz")}
    if not local:
        raise ReleaseCheckError(f"no distributions found in {dist}")
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            remote = fetch(index_api, version)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ReleaseCheckError) as exc:
            last_error = str(exc)
            remote = {}
        missing = sorted(name for name in local if name not in remote)
        if not missing:
            mismatched = sorted(name for name, value in local.items() if remote[name] != value)
            if mismatched:
                raise ReleaseCheckError(
                    "the index serves different bytes than this run built for "
                    f"{', '.join(mismatched)}; version {version} is already taken — "
                    "publish a new version rather than re-tagging"
                )
            return
        if attempt < attempts:
            sleep(interval_seconds)
    detail = f" (last index error: {last_error})" if last_error else ""
    raise ReleaseCheckError(
        f"index has not published {', '.join(sorted(local))} for {version} "
        f"after {attempts} attempts{detail}"
    )


# ── CLI ──────────────────────────────────────────────────────────────────


def _write_github_output(path: str | None, key: str, value: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed release checks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    contract = subparsers.add_parser("contract")
    contract.add_argument("--tag", required=True)
    contract.add_argument("--repo-root", type=Path, default=Path("."))
    contract.add_argument("--github-output")

    artifacts = subparsers.add_parser("artifacts")
    artifacts.add_argument("--dist", type=Path, required=True)
    artifacts.add_argument("--version", required=True)

    wait_ci = subparsers.add_parser("wait-ci")
    wait_ci.add_argument("--repository", required=True)
    wait_ci.add_argument("--sha", required=True)
    wait_ci.add_argument("--timeout-seconds", type=float, default=2400.0)
    wait_ci.add_argument("--interval-seconds", type=float, default=15.0)

    published = subparsers.add_parser("verify-published")
    published.add_argument("--dist", type=Path, required=True)
    published.add_argument("--version", required=True)
    published.add_argument("--index-api", required=True)
    published.add_argument("--attempts", type=int, default=10)
    published.add_argument("--interval-seconds", type=float, default=15.0)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "contract":
            version = validate_contract(args.tag, args.repo_root)
            _write_github_output(args.github_output, "version", version)
            print(version)
        elif args.command == "artifacts":
            wheel, sdist = validate_artifacts(args.dist, args.version)
            print(f"{wheel.name} {sdist.name}")
        elif args.command == "wait-ci":
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                raise ReleaseCheckError("GITHUB_TOKEN is required for wait-ci")
            run = wait_for_exact_main_ci(
                repository=args.repository,
                sha=args.sha,
                token=token,
                timeout_seconds=args.timeout_seconds,
                interval_seconds=args.interval_seconds,
            )
            print(f"main CI run {run.get('id')} succeeded")
        elif args.command == "verify-published":
            verify_published(
                dist=args.dist,
                version=args.version,
                index_api=args.index_api,
                attempts=args.attempts,
                interval_seconds=args.interval_seconds,
            )
            print(f"index serves the built artifacts for {args.version}")
    except ReleaseCheckError as exc:
        print(f"release preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
