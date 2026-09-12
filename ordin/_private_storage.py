"""Private, bounded SQLite transactions shared by local integration stores."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal


MAX_DATABASE_BYTES = 64 * 1024 * 1024
_APPLICATION_IDS = {"session": 0x4F524453, "trace": 0x4F524454}


@contextmanager
def private_database(
    path: str | Path, kind: Literal["session", "trace"], *, readonly: bool = False
) -> Iterator[sqlite3.Connection]:
    target = Path(path).absolute()
    if not target.parent.is_dir():
        raise ValueError("private database directory must already exist")
    parent = target.parent.stat()
    if os.name == "posix" and (parent.st_uid != os.geteuid() or parent.st_mode & 0o022):
        raise ValueError("private database directory must be owned and writable only by its user")
    flags = (
        (os.O_RDONLY if readonly else os.O_CREAT | os.O_RDWR)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(target, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if info.st_size > MAX_DATABASE_BYTES:
            raise ValueError("private database exceeds its size limit")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or target.is_symlink():
            raise ValueError("private database must be a regular, singly linked file")
        if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ValueError("private database requires owner-only permissions")
    finally:
        os.close(descriptor)
    connection = None
    try:
        connection = (
            sqlite3.connect(
                target.as_uri() + "?mode=ro", uri=True, timeout=10, isolation_level=None
            )
            if readonly
            else sqlite3.connect(target, timeout=10, isolation_level=None)
        )
        if connection.execute("PRAGMA page_size").fetchone()[0] != 4096:
            raise ValueError("unsupported private database page size")
        connection.execute("PRAGMA trusted_schema=OFF")
        if not readonly:
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA max_page_count=16384")
        connection.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        expected = _APPLICATION_IDS[kind]
        if application_id == 0:
            if readonly:
                raise ValueError("uninitialized private database")
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables and not (kind == "session" and tables == {"sessions"}):
                raise ValueError("private database type mismatch")
            # Existing v1 session stores predate the application identifier.
            connection.execute(f"PRAGMA application_id={expected}")
        elif application_id != expected:
            raise ValueError("private database type mismatch")
        if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            raise ValueError("unsupported private database version")
        if not readonly:
            connection.execute("PRAGMA user_version=1")
        yield connection
        connection.commit()
    except sqlite3.Error as exc:
        raise ValueError("private database unavailable or corrupt") from exc
    finally:
        if connection is not None:
            connection.close()
