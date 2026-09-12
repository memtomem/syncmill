"""Supported execution platforms, checked before state or process side effects."""

import sys


class UnsupportedPlatformError(RuntimeError):
    """Execution requires the supported POSIX process and filesystem semantics."""


def require_supported_platform() -> None:
    if sys.platform not in {"darwin", "linux"}:
        raise UnsupportedPlatformError(
            "syncmill supports macOS and Linux only; Windows execution is not supported"
        )
