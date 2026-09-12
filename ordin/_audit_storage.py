"""Private file descriptors and advisory locks for cooperating audit writers."""

from __future__ import annotations

import os
import json
import stat
import time
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


MAX_AUDIT_BYTES = 64 * 1024 * 1024


def load_audit_line(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate audit JSON member")
            result[key] = value
        return result

    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    json.dumps(payload, allow_nan=False)
    if not isinstance(payload, dict):
        raise ValueError("audit event must be an object")
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth > 32:
                raise ValueError("audit nesting exceeds limit")
            pending.extend(
                (child, depth + 1)
                for child in (value.values() if isinstance(value, dict) else value)
            )
    return payload


def signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def locked_audit(path: Path) -> Iterator[int]:
    parent = path.parent.stat()
    if os.name == "posix" and (parent.st_uid != os.geteuid() or parent.st_mode & 0o022):
        raise ValueError("audit directory must be owned and writable only by its user")
    fd = os.open(
        path,
        os.O_RDWR
        | os.O_CREAT
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    locked = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
            raise ValueError("audit must be a regular, singly linked file")
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("audit file requires owner-only permissions")
        deadline = time.monotonic() + 10
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("audit writer lock timed out") from None
                time.sleep(0.01)
        if os.fstat(fd).st_size > MAX_AUDIT_BYTES:
            raise ValueError("audit file exceeds byte limit")
        yield fd
    finally:
        if locked:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
