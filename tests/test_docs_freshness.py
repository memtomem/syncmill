from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import click

from syncmill.cli import cli


ROOT = Path(__file__).resolve().parents[1]
GUIDES = (ROOT / "docs" / "user-guide.md", ROOT / "docs" / "user-guide.ko.md")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\n]+)\)")


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


def test_repository_markdown_relative_links_resolve():
    broken: list[str] = []
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    for raw_relative in tracked:
        if not raw_relative:
            continue
        relative = Path(raw_relative.decode())
        markdown = ROOT / relative
        text = markdown.read_text(encoding="utf-8")
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
        if isinstance(parameter, click.Option)
        for option in parameter.opts
        if option.startswith("--")
    }

    for guide in GUIDES:
        text = guide.read_text(encoding="utf-8")
        missing = sorted(option for option in long_options if option not in text)
        assert not missing, f"{guide.relative_to(ROOT)} misses run options: {missing}"
