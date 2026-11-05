"""Thread-safe SQLite connection and transaction manager."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Self

from .migrations import apply_migrations


class Database:
    """Own one serialized SQLite connection and apply schema migrations."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.path,
            timeout=timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        if self.path != ":memory:":
            os.chmod(Path(self.path).expanduser(), 0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            f"PRAGMA busy_timeout = {max(0, int(timeout_seconds * 1000))}"
        )
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        with self._lock:
            apply_migrations(self._connection)

    @property
    def journal_mode(self) -> str:
        with self.read() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0])

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            yield self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._ensure_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("database connection is closed")

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
