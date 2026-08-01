from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import click
import pytest

from syncmill.cli import cli
from syncmill.git_env import isolated_git_env


ROOT = Path(__file__).resolve().parents[1]
GUIDES = (ROOT / "docs" / "user-guide.md", ROOT / "docs" / "user-guide.ko.md")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\n]+)\)")
FENCE_OPEN = re.compile(r"^ {0,3}(?P<char>[`~])(?P=char){2,}")


def _strip_fenced_blocks(text: str) -> str:
    """Blank out fenced code blocks, CommonMark-style.

    A closing fence must use the SAME character and be AT LEAST as long as the
    opener, so a ```` ```` ```` block legitimately closes with a longer run — a
    regex backreference demands an exact-length closer and would leave the block's
    tail in the scanned prose. An unterminated fence runs to end of document.
    """
    out: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        if fence is None:
            opened = FENCE_OPEN.match(line)
            if opened:
                char = opened.group("char")
                fence = (char, len(opened.group(0).lstrip()))
                out.append("")
                continue
            out.append(line)
            continue
        out.append("")
        closer = FENCE_OPEN.match(line)
        if closer and closer.group("char") == fence[0]:
            run = len(closer.group(0).lstrip())
            # A closer carries nothing but the fence run and trailing whitespace.
            if run >= fence[1] and not line.strip().strip(fence[0]):
                fence = None
    return "\n".join(out)


def _strip_inline_code(text: str) -> str:
    """Blank out inline code spans, matching backtick RUNS of equal length.

    ``re`` cannot express "a run of exactly N backticks" without a backreference
    per length, and a span may cross a newline, so this scans by hand: a run of N
    backticks opens a span that only a later run of EXACTLY N closes. An unclosed
    run is literal text and is kept.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "`":
            out.append(char)
            index += 1
            continue
        start = index
        while index < length and text[index] == "`":
            index += 1
        run = index - start
        cursor = index
        while cursor < length:
            if text[cursor] != "`":
                cursor += 1
                continue
            close = cursor
            while cursor < length and text[cursor] == "`":
                cursor += 1
            if cursor - close == run:
                # Preserve newlines so link line numbers and block structure hold.
                out.append("\n" * text.count("\n", start, cursor))
                index = cursor
                break
        else:
            out.append("`" * run)
    return "".join(out)


def _prose_only(text: str) -> str:
    """Documentation prose with code removed.

    Fenced blocks and inline spans hold ILLUSTRATIVE paths (`[label](path/to/repo)`
    in a sample document, a shell snippet quoting markdown) that the repository
    does not have to resolve — scanning them would fail this test on a perfectly
    good example.
    """
    return _strip_inline_code(_strip_fenced_blocks(text))


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
    """Tracked ``*.md`` paths under ``ROOT``.

    SKIPS only on a genuinely missing precondition — no git binary, or no
    checkout (the suite also runs from an unpacked sdist, where there is no file
    list to check). Once both are present a failing ``git ls-files`` is a real
    error and FAILS with its stderr: turning it into a skip would silently retire
    this guard the moment git started misbehaving.

    ``isolated_git_env`` is the same environment agents and gates get: an ambient
    ``GIT_DIR``/``GIT_WORK_TREE`` would otherwise point this at another repository's
    index and list its files instead of ours.
    """
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout — no tracked file list to scan")
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        env=isolated_git_env(),
    )
    assert listed.returncode == 0, (
        f"git ls-files failed ({listed.returncode}): "
        f"{listed.stderr.decode(errors='replace')}"
    )
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
        # Bounded on BOTH sides: `--monitor-only` must not satisfy `--monitor`,
        # and a typo'd `---repo` must not satisfy `--repo`.
        missing = sorted(
            option
            for option in long_options
            if not re.search(r"(?<![\w-])" + re.escape(option) + r"(?![\w-])", text)
        )
        assert not missing, f"{guide.relative_to(ROOT)} misses run options: {missing}"


# ── the code-stripping scanner itself ─────────────────────────────────────────
# `_prose_only` decides what the link guard even looks at, so its delimiter
# handling is pinned directly: a false positive fails CI on a good example, and a
# false negative hides a genuinely broken link. Each case below broke one of the
# two regexes this scanner replaced.


def test_a_longer_closing_fence_still_closes_the_block():
    text = "before\n```\n[x](nope.md)\n`````\nafter [y](nope.md)\n"
    stripped = _prose_only(text)
    assert "nope.md" not in stripped.split("after")[0]
    assert "after [y](nope.md)" in stripped


def test_a_shorter_run_inside_a_fence_does_not_close_it():
    text = "```````\n``\n[x](nope.md)\n```````\ntail\n"
    assert "nope.md" not in _prose_only(text)


def test_a_tilde_fence_is_not_closed_by_backticks():
    text = "~~~\n```\n[x](nope.md)\n~~~\ntail\n"
    assert "nope.md" not in _prose_only(text)


def test_an_unterminated_fence_swallows_the_rest_of_the_document():
    assert "nope.md" not in _prose_only("```\n[x](nope.md)\n")


def test_an_inline_span_may_hold_backticks_and_cross_a_line():
    # A double-backtick span legitimately contains a single backtick, and an
    # inline span may span a newline — the old `+[^`\n]*`+ pattern closed on the
    # inner backtick and left the link exposed.
    assert "nope.md" not in _prose_only("text `` a ` [x](nope.md) `` tail")
    assert "nope.md" not in _prose_only("text `[x](\nnope.md)` tail")


def test_an_unmatched_backtick_run_is_literal_text():
    # No closing run of equal length: the text is prose, and a real link in it
    # must still be checked.
    assert "real.md" in _prose_only("a ``` stray run with [x](real.md)")


def test_prose_links_survive_stripping():
    assert "keep.md" in _prose_only("see [keep](keep.md) and `code` here")
