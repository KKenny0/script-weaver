"""SQLite per-thread connections, migrations, and single-process write serialization."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def default_data_dir() -> Path:
    root = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME")
    return Path(root) / "ScriptWeaver" if root else Path.home() / ".local" / "share" / "script-weaver"


class SchemaError(RuntimeError):
    """Raised when an existing database has a shape this version cannot trust."""


MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class Database:
    """One SQLite connection per thread behind a global single-writer lock.

    Reads use the thread's own connection under WAL; all writes serialize on
    ``self._lock`` and run inside ``BEGIN IMMEDIATE`` so checks and facts share
    one transaction. Migrations run once on the constructing thread before any
    other thread can obtain a connection.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_data_dir() / "workbench.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._local.connection = self._new_connection()
        self._connections.append(self._local.connection)
        with self._lock:
            _run_migrations(self._local.connection, self.path)

    def _new_connection(self) -> sqlite3.Connection:
        # Per-thread usage is enforced by the thread-local property below; the
        # flag stays False so close() may reap connections owned by dead threads.
        connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._new_connection()
            self._local.connection = connection
            with self._lock:
                self._connections.append(connection)
        return connection

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                # Roll back even on KeyboardInterrupt/CancelledError: a leaked open
                # transaction would hold SQLite's write lock and wedge every writer.
                connection.rollback()
                raise
            else:
                connection.commit()

    def close(self) -> None:
        with self._lock:
            while self._connections:
                self._connections.pop().close()
            self._local.connection = None


def _split_statements(script: str) -> list[str]:
    buffer, statements = "", []
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _run_migrations(connection: sqlite3.Connection, path: Path) -> None:
    from .schema_shape import verify_shape, verify_v1_shape

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    latest_versions = sorted(int(file.name.split("_", 1)[0]) for file in files)
    latest = latest_versions[-1] if latest_versions else 0

    has_ledger = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    has_facts = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if has_ledger:
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        if applied and latest in applied:
            verify_shape(connection, latest)
            return
        if applied and applied - set(latest_versions):
            raise SchemaError(f"database {path} records unknown migration versions: {sorted(applied - set(latest_versions))}")
    elif has_facts:
        # Managed tables without a ledger: verify the shape, then backfill v1.
        verify_v1_shape(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)", (_utcnow(),))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        return
    else:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    _backup_existing_database(connection, path)

    for file in files:
        version = int(file.name.split("_", 1)[0])
        already = has_ledger and connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
        ).fetchone()
        if already:
            continue
        statements = _split_statements(file.read_text(encoding="utf-8"))
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _utcnow()),
            )
            connection.execute("COMMIT")
        except Exception as error:
            connection.execute("ROLLBACK")
            raise SchemaError(f"migration {file.name} failed for {path}: {error}") from error
    verify_shape(connection, latest)


def _backup_existing_database(connection: sqlite3.Connection, path: Path) -> None:
    """Copy an existing database aside before changing it. Never deletes originals."""
    has_facts = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if not has_facts:
        return
    backup = path.with_name(f"{path.name}.backup-{_utcnow().replace(':', '').replace('+', 'plus')}")
    source = sqlite3.connect(path)
    try:
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def _utcnow() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = ["Database", "SchemaError", "default_data_dir"]
