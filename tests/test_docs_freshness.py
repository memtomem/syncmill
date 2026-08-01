from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import click
import pytest

from syncmill.cli import cli


ROOT = Path(__file__).resolve().parents[1]
GUIDES = (ROOT / "docs" / "user-guide.md", ROOT / "docs" / "user-guide.ko.md")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\n]+)\)")
# Fenced blocks and inline spans hold ILLUSTRATIVE paths (`[label](path/to/repo)`
# in a sample document, a shell snippet quoting markdown), which are not links the
# repository has to resolve. Scanning them would fail this test on a perfectly
# good example, so they are removed before the link scan runs.
FENCE = re.compile(r"^[ \t]*(?P<fence>```+|~~~+).*?^[ \t]*(?P=fence)[ \t]*$", re.M | re.S)
INLINE_CODE = re.compile(r"`+[^`\n]*`+")


def _prose_only(text: str) -> str:
    return INLINE_CODE.sub(" ", FENCE.sub("\n", text))


def _relative_target(raw: str) -> str | None:
    value = raw.strip()
    if not value or value.startswith("#"):
        return None
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        value = value.split(maxsplit=1)[0]
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return unquote(parsed.path)


def _tracked_markdown() -> list[Path]:
    """Tracked ``*.md`` paths, or skip when this isn't a git checkout.

    The suite also runs from an unpacked sdist, where there is no `.git` and no
    file list to check — that is a missing precondition, not a documentation
    failure, so it skips rather than erroring out of a `check=True` call.
    """
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout — no tracked file list to scan")
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        pytest.skip("git ls-files unavailable")
    return [
        Path(entry.decode()) for entry in listed.stdout.split(b"\0") if entry
    ]


def test_repository_markdown_relative_links_resolve():
    broken: list[str] = []
    for relative in _tracked_markdown():
        markdown = ROOT / relative
        # A tracked path can be absent from the working tree (deleted, not yet
        # committed); there is nothing to scan and nothing to report.
        if not markdown.is_file():
            continue
        text = _prose_only(markdown.read_text(encoding="utf-8"))
        for match in LINK.finditer(text):
            target = _relative_target(match.group(1))
            if target is None:
                continue
            resolved = (markdown.parent / target).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                broken.append(f"{relative}: {target}")
    assert not broken, "Broken local Markdown links:\n" + "\n".join(broken)


def test_both_user_guides_cover_every_run_long_option():
    run_command = cli.get_command(click.Context(cli), "run")
    assert run_command is not None
    long_options = {
        option
        for parameter in run_command.params
        # `hidden` options are internal plumbing (the monitor/worker handshakes),
        # deliberately absent from the user guides — requiring them here would
        # force documenting an option users are not meant to pass.
        if isinstance(parameter, click.Option) and not parameter.hidden
        for option in parameter.opts
        if option.startswith("--")
    }

    for guide in GUIDES:
        text = guide.read_text(encoding="utf-8")
        # Bounded match: a guide mentioning `--monitor-only` must not satisfy
        # `--monitor`.
        missing = sorted(
            option
            for option in long_options
            if not re.search(re.escape(option) + r"(?![\w-])", text)
        )
        assert not missing, f"{guide.relative_to(ROOT)} misses run options: {missing}"
