"""The Claude Code plugin (`plugins/syncmill-claude-plugin`) stays consistent.

Stage 1 ships one user-invoked skill that runs the offline trial through a
pinned `uvx`. These checks keep the marketplace, the manifest and the skill in
agreement, and keep the pin on the package version, so a release that bumps
`pyproject.toml` cannot leave the plugin running an older syncmill.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = ROOT / "plugins" / "syncmill-claude-plugin"
SKILL = PLUGIN / "skills" / "trial" / "SKILL.md"


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    block = text.split("---\n", 2)[1]
    fields = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _package_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_marketplace_points_at_the_plugin_and_agrees_with_its_manifest():
    marketplace = json.loads(MARKETPLACE.read_text())
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())

    assert marketplace["name"] == "syncmill" and marketplace["owner"]["name"]
    (entry,) = marketplace["plugins"]
    # A relative source resolves from the marketplace root, the repository root.
    assert (ROOT / entry["source"]).resolve() == PLUGIN
    assert entry["name"] == manifest["name"] == "syncmill"
    assert entry["version"] == manifest["version"]
    assert entry["license"] == manifest["license"] == "Apache-2.0"


def test_trial_skill_is_user_invoked_and_pins_the_package_version():
    fields = _frontmatter(SKILL)
    version = _package_version()
    command = f"uvx --from syncmill=={version} syncmill start --trial"

    assert fields["name"] == "trial"
    assert fields["disable-model-invocation"] == "true"
    # The only pre-approved commands: the uv check and the exact pinned trial.
    assert re.findall(r"Bash\(([^)]*)\)", fields["allowed-tools"]) == [
        "uvx --version",
        command,
    ]
    body = SKILL.read_text(encoding="utf-8")
    assert f"\n   {command}\n" in body  # the command the skill tells Claude to run
    assert command in (PLUGIN / "README.md").read_text(encoding="utf-8")


def test_stage_one_ships_no_hooks_mcp_server_or_binaries():
    shipped = {p.relative_to(PLUGIN).as_posix() for p in PLUGIN.rglob("*") if p.is_file()}
    assert shipped == {
        ".claude-plugin/plugin.json",
        "README.md",
        "skills/trial/SKILL.md",
    }
