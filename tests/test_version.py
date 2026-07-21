"""The package version has exactly one source of truth.

`pyproject`, `syncmill.__version__`, the CLI's `--version`, and the Phoenix
tracer's instrumentation version had each hardcoded their own number and drifted
apart (0.2.0 / 0.1.1 / 0.2.0). These tests fail the moment any of them stops
deriving from installed distribution metadata.
"""

from __future__ import annotations

from importlib.metadata import version as package_version

from click.testing import CliRunner

import syncmill
from syncmill.cli import cli


def test_dunder_version_matches_distribution_metadata():
    assert syncmill.__version__ == package_version("syncmill")


def test_cli_version_matches_dunder_version():
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert syncmill.__version__ in result.output


def test_no_module_hardcodes_a_version_literal():
    # The tracer used to pin its own "0.2.0"; catch any module reintroducing a
    # literal version string instead of importing __version__.
    from pathlib import Path
    import re

    pattern = re.compile(r'"\d+\.\d+\.\d+"')
    offenders = []
    for path in (Path(syncmill.__file__).parent).rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "version" not in line.lower() or "schema" in line.lower():
                continue
            if pattern.search(line) and "__version__" not in line:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "hardcoded version literals: " + "; ".join(offenders)
