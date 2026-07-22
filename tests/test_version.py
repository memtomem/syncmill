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


def test_phoenix_tracer_version_is_not_a_literal():
    # The exact regression: phoenix_exporter passed get_tracer("syncmill",
    # "0.2.0"). A keyword/line heuristic misses that call (it contains no word
    # "version"), so this asserts on the AST of the call itself.
    import ast
    from pathlib import Path

    source = (Path(syncmill.__file__).parent / "phoenix_exporter.py").read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_tracer"
    ]
    assert calls, "expected a get_tracer call in phoenix_exporter"
    for call in calls:
        # Requiring the identifier itself (rather than merely rejecting a
        # literal) also rules out an indirect hardcode like
        # TRACER_VERSION = "0.2.0" passed through a name.
        names = {
            arg.id
            for arg in [*call.args, *(kw.value for kw in call.keywords)]
            if isinstance(arg, ast.Name)
        }
        assert "__version__" in names, (
            "get_tracer must receive syncmill.__version__, not a version literal "
            "or an indirectly hardcoded name"
        )


def test_cli_version_survives_missing_distribution_metadata(monkeypatch):
    # Click queries distribution metadata independently of __version__; without
    # an explicit version= it raises RuntimeError in a source tree that was
    # never installed, so --version must not depend on that lookup.
    import importlib.metadata as md

    def _missing(name):
        raise md.PackageNotFoundError(name)

    monkeypatch.setattr(md, "version", _missing)
    monkeypatch.setattr(md, "distribution", _missing)
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert syncmill.__version__ in result.output
