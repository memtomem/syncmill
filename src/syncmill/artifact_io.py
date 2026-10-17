"""Descriptor-relative artifact I/O beneath a trusted, canonical state root.

This prevents path traversal and symlink redirection, not arbitrary tampering by
another process with the same OS credentials. Directory handles live for one
operation and are checked against their names before use.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path
from uuid import uuid4


def validate_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(c in value for c in ("/", "\\", "\0"))
    ):
        raise ValueError("artifact ID must be a single non-empty path component")


class ArtifactIO:
    def __init__(self, base: Path):
        self.base = base
        self.fds: dict[Path, int] = {}
        self.fds[base] = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self) -> None:
        for fd in reversed(list(self.fds.values())):
            os.close(fd)
        self.fds.clear()

    def release(self, path: Path) -> None:
        """Bound descriptor use when listing many runs."""
        for opened in reversed(list(self.fds)):
            if opened == path or path in opened.parents:
                os.close(self.fds.pop(opened))

    def directory(self, path: Path, *, create: bool = False) -> int:
        if path == self.base:
            return self.fds[path]
        # Reject lexical traversal before any OS operation (Path normalizes '.').
        parts = path.relative_to(self.base).parts
        for part in parts:
            validate_id(part)
        parent = self.directory(path.parent, create=create)
        if path in self.fds:
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(self.fds[path])
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError(errno.ESTALE, "artifact directory changed", str(path))
            return self.fds[path]
        if create:
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        fd = os.open(
            path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        self.fds[path] = fd
        return fd

    def mkdir(self, path: Path, *, exist_ok: bool = True) -> None:
        parent = self.directory(path.parent)
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
        except FileExistsError:
            if not exist_ok:
                raise
        self.directory(path)

    def open_file(self, path: Path, flags: int = os.O_RDONLY) -> int:
        parent = self.directory(path.parent)
        fd = os.open(
            path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(
                    errno.EINVAL,
                    "artifact file must be a singly linked regular file",
                    str(path),
                )
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read(self, path: Path, limit: int = -1) -> bytes:
        fd = self.open_file(path)
        try:
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            return stream.read(limit)

    def exists(self, path: Path) -> bool:
        try:
            fd = self.open_file(path)
        except FileNotFoundError:
            return False
        os.close(fd)
        return True

    def write(self, path: Path, data: bytes) -> None:
        parent = self.directory(path.parent)
        # Refuse existing links/special files even though rename would replace
        # rather than follow the final component.
        self.exists(path)
        name = f".{path.name}.{uuid4().hex}.tmp"
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            try:
                stream = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.directory(path.parent)  # detect a renamed/replaced run
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass

    def names(self, path: Path) -> list[str]:
        return os.listdir(self.directory(path))

    def is_directory(self, path: Path) -> bool:
        info = os.stat(
            path.name, dir_fd=self.directory(path.parent), follow_symlinks=False
        )
        return stat.S_ISDIR(info.st_mode)

    def is_symlink(self, path: Path) -> bool:
        info = os.stat(
            path.name, dir_fd=self.directory(path.parent), follow_symlinks=False
        )
        return stat.S_ISLNK(info.st_mode)

    def unlink(self, path: Path) -> None:
        parent = self.directory(path.parent)
        try:
            os.unlink(path.name, dir_fd=parent)
        except FileNotFoundError:
            pass

    def rmtree(self, path: Path) -> None:
        self.directory(path)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise OSError("descriptor-relative directory removal unavailable")
        try:
            shutil.rmtree(path.name, dir_fd=self.directory(path.parent))
        finally:
            self.release(path)
