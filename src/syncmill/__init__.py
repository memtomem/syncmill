"""syncmill — Multi-agent orchestration with shared memory."""

from importlib.metadata import PackageNotFoundError, version as _package_version

try:
    # Single source of truth: the installed distribution metadata, which
    # `syncmill --version` also reads. Hardcoding the number here is what let
    # the package, the CLI, and the tracer drift apart across releases.
    __version__ = _package_version("syncmill")
except PackageNotFoundError:  # source tree without an install
    __version__ = "0.0.0+unknown"
