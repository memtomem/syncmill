from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import click
import pytest
from markdown_it import MarkdownIt

from syncmill.cli import cli
from syncmill.git_env import isolated_git_env


ROOT = Path(__file__).resolve().parents[1]
GUIDES = (ROOT / "docs" / "user-guide.md", ROOT / "docs" / "user-guide.ko.md")
# A real CommonMark parser, not hand-rolled pattern matching. Two hand-written
# passes were tried first and both hid genuinely broken links: a fence inside a
# list item whose closer read as a new opener, a backtick inside an info string,
# backslash-escaped backticks read as delimiters, and reference-style links
# (`[label][ref]` + `[ref]: target.md`) that no inline regex sees at all.
# Walking the token stream gets every one of those for free, and code spans and
# fenced/indented blocks never become link tokens in the first place.
_PARSER = MarkdownIt("commonmark")


def _srcset_candidates(value: str) -> list[str]:
    """The URLs in a ``srcset``, per the HTML parsing algorithm.

    NOT a split on commas: a URL may legitimately contain them (a `data:` URI
    with encoded markup is the common case), and splitting there produced
    fragments like ``%3Csvg%3E`` that then read as missing local files and failed
    valid documentation. The spec instead reads a URL as a run of non-whitespace;
    if that run ends in commas, the candidate stops there, otherwise its
    descriptors run to the next comma.
    """
    candidates: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        while index < length and (value[index].isspace() or value[index] == ","):
            index += 1
        start = index
        while index < length and not value[index].isspace():
            index += 1
        url = value[start:index]
        if not url:
            break
        if url.endswith(","):
            candidates.append(url.rstrip(","))
            continue
        candidates.append(url)
        # Skip this candidate's descriptors up to the separating comma.
        while index < length and value[index] != ",":
            index += 1
    return candidates


class _HTMLTargets(HTMLParser):
    """Collect every destination attribute from raw HTML embedded in Markdown.

    CommonMark passes raw HTML through as ``html_block``/``html_inline`` tokens
    without parsing its attributes, so `<a href="missing.md">` is invisible to a
    token walk — and the guides do use raw HTML (centered images, details
    blocks). Parsing those tokens is what makes "every destination" true.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # By ATTRIBUTE, not by a whitelist of tags: `<link href>`, `<script src>`,
        # `<iframe src>`, `<audio src>` and friends all carry destinations, and a
        # four-tag list silently skipped them while claiming "every destination".
        for name, value in attrs:
            if not value:
                continue
            key = name.lower()
            # `xlink:href` is how `<use>` and inline SVG reference a document;
            # `data-*` attributes are application state and are NOT destinations.
            if key in ("href", "src", "xlink:href"):
                self.targets.append(value)
            elif key in ("srcset", "imagesrcset"):
                self.targets.extend(_srcset_candidates(value))


def _html_targets(html: str) -> list[str]:
    parser = _HTMLTargets()
    # Malformed HTML in prose is the author's problem, not this guard's: parse
    # what we can and check the destinations we found.
    parser.feed(html)
    parser.close()
    return parser.targets


def _link_targets(text: str) -> list[str]:
    """Every link/image destination in ``text``, in document order.

    Markdown links and images come from the token stream; raw-HTML anchors come
    from ``_html_targets``. Code spans and fenced or indented blocks never become
    either, which is exactly the exclusion this guard needs.
    """
    targets: list[str] = []
    for token in _PARSER.parse(text):
        if token.type == "html_block":
            targets.extend(_html_targets(token.content))
        for child in token.children or ():
            if child.type == "link_open":
                targets.append(child.attrGet("href") or "")
            elif child.type == "image":
                targets.append(child.attrGet("src") or "")
            elif child.type == "html_inline":
                targets.extend(_html_targets(child.content))
    return targets


def _relative_target(raw: str) -> str | None:
    """The repository-relative path a destination points at, or ``None``.

    ``None`` means "not ours to resolve": an empty destination, a pure fragment,
    or anything with a scheme or host (https, mailto, //cdn).
    """
    value = raw.strip()
    if not value or value.startswith("#"):
        return None
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
    return [Path(entry.decode()) for entry in listed.stdout.split(b"\0") if entry]


def test_repository_markdown_relative_links_resolve():
    broken: list[str] = []
    for relative in _tracked_markdown():
        markdown = ROOT / relative
        # A tracked path can be absent from the working tree (deleted, not yet
        # committed); there is nothing to scan and nothing to report.
        if not markdown.is_file():
            continue
        for destination in _link_targets(markdown.read_text(encoding="utf-8")):
            target = _relative_target(destination)
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


# ── what the link guard does and does not look at ─────────────────────────────
# Every case below is one a hand-written scanner got wrong. A false NEGATIVE
# (a link the guard never sees) silently retires the guard; a false POSITIVE
# fails CI on documentation that is perfectly correct.


def _command_paths(group, prefix=()):
    """Every non-hidden command path in the CLI tree, as tuples of names."""
    context = click.Context(cli)
    for name in sorted(group.list_commands(context)):
        command = group.get_command(context, name)
        if command is None or command.hidden:
            continue
        path = (*prefix, name)
        if isinstance(command, click.Group):
            yield from _command_paths(command, path)
        else:
            yield path


def test_every_command_is_documented_somewhere():
    # The guard that was missing: the option check below covers `run`'s flags
    # only, so whole COMMANDS could ship undocumented — `board compact` and
    # `recover --discard` both did, and each is the only way to undo an
    # unbounded-growth problem. A command must appear in the README or in at
    # least one user guide; where it is documented is the author's call.
    corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "README.md", *GUIDES)
        if path.is_file()
    )
    undocumented = [
        " ".join(path)
        for path in _command_paths(cli)
        if f"syncmill {' '.join(path)}" not in corpus
    ]
    assert not undocumented, (
        "Commands absent from README.md and both user guides:\n"
        + "\n".join(undocumented)
    )


def test_code_is_not_scanned():
    fenced = "```\n[x](nope.md)\n```\n"
    indented = "    [x](nope.md)\n"
    inline = "text `[x](nope.md)` tail"
    for sample in (fenced, indented, inline):
        assert _link_targets(sample) == []


def test_a_fence_inside_a_list_item_still_contains_its_link():
    # The closing fence carries the list item's container prefix, which a
    # line-anchored scanner read as a NEW top-level opener — reopening the block
    # and swallowing every link after it.
    text = "- item\n\n  ```\n  [x](nope.md)\n  ```\n\n[after](real.md)\n"
    assert _link_targets(text) == ["real.md"]


def test_a_backtick_in_an_info_string_does_not_open_a_fence():
    # ```` ```a`b ```` is not a valid fence opener, so the link below it is live
    # prose the guard must check.
    assert _link_targets("```a`b\n[x](live.md)\n") == ["live.md"]


def test_escaped_backticks_are_punctuation_not_delimiters():
    assert _link_targets(r"a \` [x](live.md) \` b") == ["live.md"]


def test_reference_style_links_are_collected():
    # No inline `](...)` to match — an inline regex misses these entirely.
    assert _link_targets("see [guide][ref]\n\n[ref]: target.md\n") == ["target.md"]


def test_images_and_angle_bracket_destinations_are_collected():
    assert _link_targets("![alt](docs/assets/x.png)") == ["docs/assets/x.png"]
    # The parser normalizes a destination (a space becomes %20), which is why
    # _relative_target unquotes before touching the filesystem.
    assert _link_targets("[x](<a file.md>)") == ["a%20file.md"]
    assert _relative_target("a%20file.md") == "a file.md"


def test_external_and_fragment_destinations_are_not_repository_paths():
    for destination in ("https://example.com/x.md", "mailto:a@b", "#anchor", ""):
        assert _relative_target(destination) is None
    assert _relative_target("docs/user-guide.md#config") == "docs/user-guide.md"


def test_raw_html_anchors_and_images_are_collected():
    # CommonMark hands raw HTML through unparsed, so a token walk alone never
    # sees these destinations — and the guides do use raw HTML for layout.
    assert _link_targets('<a href="docs/user-guide.md">guide</a>') == [
        "docs/user-guide.md"
    ]
    assert _link_targets('<p align="center"><img src="docs/assets/x.png"></p>') == [
        "docs/assets/x.png"
    ]
    # Inline HTML inside a paragraph is a different token type than a block.
    assert _link_targets('text <a href="real.md">x</a> tail') == ["real.md"]


def test_html_inside_code_is_still_not_scanned():
    assert _link_targets('```\n<a href="nope.md">x</a>\n```\n') == []
    assert _link_targets('`<a href="nope.md">x</a>`') == []


def test_every_resource_attribute_is_collected_not_a_tag_whitelist():
    # A four-tag whitelist (a/img/source/video) silently skipped these while the
    # docstring claimed "every destination".
    assert _link_targets('<link href="theme.css">') == ["theme.css"]
    assert _link_targets('<script src="app.js"></script>') == ["app.js"]
    assert _link_targets('<iframe src="demo.html"></iframe>') == ["demo.html"]
    # Uppercase attributes, single quotes, unquoted values, self-closing tags.
    assert _link_targets("<IMG SRC='a.png'/>") == ["a.png"]
    assert _link_targets("<a href=b.md>x</a>") == ["b.md"]


def test_srcset_candidates_are_each_collected():
    # Attributes are reported in source order, so srcset's two candidates come
    # before the src fallback here.
    assert _link_targets('<img srcset="a.png 1x, b.png 2x" src="c.png">') == [
        "a.png",
        "b.png",
        "c.png",
    ]
    assert _srcset_candidates("a.png 1x, b.png 2x") == ["a.png", "b.png"]


def test_a_comma_bearing_srcset_url_is_one_candidate():
    # Splitting on every comma turned this data URI into two fragments, and
    # `%3Csvg%3E` then read as a missing local file — a valid page failing CI.
    value = "data:image/svg+xml,%3Csvg%3E 1x, assets/fallback.png 2x"
    assert _srcset_candidates(value) == [
        "data:image/svg+xml,%3Csvg%3E",
        "assets/fallback.png",
    ]
    # A candidate with no descriptor ends at its own trailing comma.
    assert _srcset_candidates("a.png, b.png") == ["a.png", "b.png"]
    assert _relative_target("data:image/svg+xml,%3Csvg%3E") is None


def test_namespaced_destinations_count_and_data_attributes_do_not():
    assert _link_targets('<use xlink:href="icons.svg#check"/>') == ["icons.svg#check"]
    # Application state, not a destination the repository has to resolve.
    assert _link_targets('<div data-href="nope.md" data-src="nope.png"></div>') == []
