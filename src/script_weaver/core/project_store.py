"""SQLite-backed persistent storage for web projects (``main-web`` namespace).

Ticket #13 scope: the ``projects`` table holds the current effective
ProjectState snapshot; ``project_versions`` keeps immutable history. Run and
candidate records arrive with later phase-1 tickets.

Concurrency model: a single API process owns the data directory (enforced by
:class:`DataDirLock`); one connection guarded by a re-entrant lock keeps write
transactions short. ``sqlite3`` runs in autocommit mode and every write uses an
explicit ``BEGIN IMMEDIATE`` … ``COMMIT``/``ROLLBACK`` block with no await or
network calls inside, so a failed save never leaves partial writes behind.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from script_weaver.core.types import ProjectState

SCHEMA_VERSION = 1

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        revision INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        review_json TEXT NOT NULL DEFAULT '{}',
        auto_approve INTEGER NOT NULL DEFAULT 1,
        skill_bindings_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_versions (
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,
        title TEXT NOT NULL,
        state_json TEXT NOT NULL,
        review_json TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        PRIMARY KEY (project_id, revision)
    )
    """,
)


class ProjectStoreError(Exception):
    """Base class for project storage errors."""


class ProjectNotFoundError(ProjectStoreError):
    """The requested project does not exist."""


class RevisionConflictError(ProjectStoreError):
    """The expected revision no longer matches the stored revision."""

    def __init__(self, message: str, current_revision: int):
        super().__init__(message)
        self.current_revision = current_revision


class UnsupportedDatabaseVersionError(ProjectStoreError):
    """The database format version is newer than this program supports."""


class DataDirLockError(ProjectStoreError):
    """Another process already owns the web data directory."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


if sys.platform == "win32":
    import msvcrt

    def _lock_file_nonblocking(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_file(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file_nonblocking(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class DataDirLock:
    """Exclusive OS-level lock on the web data directory.

    Holds an open file description on ``api.lock`` for the process lifetime so
    a second API instance on the same data directory refuses to start. The OS
    releases the lock when the process exits, so a clean shutdown allows the
    next instance to start.
    """

    def __init__(self, directory: Path):
        self._path = directory / "api.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            _lock_file_nonblocking(fd)
        except OSError:
            os.close(fd)
            raise DataDirLockError(
                f"数据目录已被其他 API 实例锁定: {self._path.parent}。"
                "同一数据目录仅允许一个 API 实例运行。"
            ) from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock_file(fd)
        finally:
            os.close(fd)


@dataclass
class ProjectRecord:
    """The current effective snapshot of a project."""

    project_id: str
    title: str
    revision: int
    state: ProjectState
    state_json: str
    review: dict[str, Any]
    auto_approve: bool
    skill_bindings: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass
class VersionSummary:
    revision: int
    title: str
    source: str
    summary: str
    created_at: str


@dataclass
class VersionSnapshot:
    revision: int
    title: str
    source: str
    summary: str
    created_at: str
    state: ProjectState


def serialize_state(state: ProjectState) -> str:
    """Serialize a ProjectState deterministically for comparison/storage."""
    return state.model_dump_json(indent=2, ensure_ascii=False)


def _content_signature(state_json: str) -> str:
    """Signature of a state ignoring fields a rename may legitimately touch.

    Renaming syncs ``meta.title`` and ``meta.updated_at`` inside the stored
    state; those must not make generated content look concurrently edited.
    """
    try:
        data = json.loads(state_json)
    except (json.JSONDecodeError, TypeError):
        return state_json
    meta = data.get("meta") if isinstance(data, dict) else None
    if isinstance(meta, dict):
        meta.pop("title", None)
        meta.pop("updated_at", None)
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


class ProjectStore:
    """Persistent project + history storage backed by standard-library SQLite."""

    def __init__(self, db_path: Path):
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path), timeout=5.0, check_same_thread=False, isolation_level=None
        )
        self._lock = threading.RLock()
        self._configure()
        self._migrate()

    # ── Lifecycle ─────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _configure(self) -> None:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version > SCHEMA_VERSION:
                raise UnsupportedDatabaseVersionError(
                    f"数据库格式版本 {version} 超出当前程序支持"
                    f"（最高 {SCHEMA_VERSION}），拒绝写入。"
                )
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA_STATEMENTS:
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        """Short exclusive write transaction; rolls back fully on any error."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    # ── Reads ─────────────────────────────────────────────

    def _row_to_record(self, row: sqlite3.Row | None) -> ProjectRecord | None:
        if row is None:
            return None
        return ProjectRecord(
            project_id=row["id"],
            title=row["title"],
            revision=row["revision"],
            state=ProjectState.model_validate_json(row["state_json"]),
            state_json=row["state_json"],
            review=json.loads(row["review_json"] or "{}"),
            auto_approve=bool(row["auto_approve"]),
            skill_bindings=json.loads(row["skill_bindings_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, project_id: str) -> ProjectRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._row_to_record(row)

    def get_required(self, project_id: str) -> ProjectRecord:
        record = self.get(project_id)
        if record is None:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        return record

    def list_projects(self) -> list[dict[str, Any]]:
        """List project summaries, most recently updated first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, revision, state_json, created_at, updated_at"
                " FROM projects ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        result = []
        for row in rows:
            try:
                meta = json.loads(row["state_json"]).get("meta", {})
                stage = meta.get("status", "idea_input")
            except (json.JSONDecodeError, TypeError):
                stage = "idea_input"
            result.append({
                "project_id": row["id"],
                "title": row["title"],
                "revision": row["revision"],
                "stage": stage,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return result

    def list_versions(self, project_id: str) -> list[VersionSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT revision, title, source, summary, created_at"
                " FROM project_versions WHERE project_id = ?"
                " ORDER BY revision DESC",
                (project_id,),
            ).fetchall()
        return [
            VersionSummary(
                revision=r["revision"], title=r["title"], source=r["source"],
                summary=r["summary"], created_at=r["created_at"],
            )
            for r in rows
        ]

    def get_version(self, project_id: str, revision: int) -> VersionSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT revision, title, source, summary, created_at, state_json"
                " FROM project_versions WHERE project_id = ? AND revision = ?",
                (project_id, revision),
            ).fetchone()
        if row is None:
            return None
        return VersionSnapshot(
            revision=row["revision"], title=row["title"], source=row["source"],
            summary=row["summary"], created_at=row["created_at"],
            state=ProjectState.model_validate_json(row["state_json"]),
        )

    # ── Writes ────────────────────────────────────────────

    def create_project(
        self,
        user_input: str,
        title: str | None = None,
        auto_approve: bool = True,
        skill_bindings: dict[str, Any] | None = None,
    ) -> ProjectRecord:
        """Create a project whose service ID and state meta.id are the same."""
        project_id = uuid.uuid4().hex[:12]
        resolved_title = (title or user_input[:40]) if (title or user_input) else ""
        state = ProjectState(
            user_input=user_input,
            meta={
                "id": project_id,
                "title": resolved_title,
            },
        )
        state_json = serialize_state(state)
        now = _utcnow()
        with self._write_tx() as conn:
            conn.execute(
                "INSERT INTO projects (id, title, revision, state_json, review_json,"
                " auto_approve, skill_bindings_json, created_at, updated_at)"
                " VALUES (?, ?, 1, ?, '{}', ?, ?, ?, ?)",
                (
                    project_id, resolved_title, state_json,
                    1 if auto_approve else 0,
                    json.dumps(skill_bindings or {}, ensure_ascii=False),
                    now, now,
                ),
            )
            conn.execute(
                "INSERT INTO project_versions (project_id, revision, title,"
                " state_json, source, summary, created_at) VALUES (?, 1, ?, ?, 'manual', ?, ?)",
                (project_id, resolved_title, state_json, "项目创建", now),
            )
        return self.get_required(project_id)

    def rename_project(
        self, project_id: str, title: str, expected_revision: int
    ) -> ProjectRecord:
        """Rename a project. Naming also advances the revision."""
        title = title.strip()
        if not title:
            raise ProjectStoreError("Title must not be empty")
        return self._save(
            project_id,
            title=title,
            state=None,
            expected_revision=expected_revision,
            source="manual",
            summary=f"命名: {title}",
        )

    def save_state(
        self,
        project_id: str,
        state: ProjectState,
        expected_revision: int,
        *,
        source: str,
        summary: str = "",
        review: dict[str, Any] | None = None,
    ) -> ProjectRecord:
        """Strict compare-and-swap save of a new effective state."""
        return self._save(
            project_id, title=None, state=state,
            expected_revision=expected_revision, source=source,
            summary=summary, review=review,
        )

    def replace_state(
        self,
        project_id: str,
        state: ProjectState,
        *,
        base_revision: int,
        base_state_json: str,
        source: str,
        summary: str = "",
    ) -> ProjectRecord:
        """Save pipeline output that started from ``base_state_json``.

        If the revision moved on meanwhile but the stored content is still the
        exact base content (e.g. the user only renamed the project), the save
        adopts the newer revision instead of discarding the generated result.
        Any real content change rejects the save with a revision conflict.
        """
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT revision, title, state_json, review_json FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project '{project_id}' not found")
            if row["revision"] != base_revision and _content_signature(
                row["state_json"]
            ) != _content_signature(base_state_json):
                raise RevisionConflictError(
                    "项目内容在生成期间已被修改，生成结果未覆盖当前内容",
                    current_revision=row["revision"],
                )
            # Content unchanged since our base: keep the current (possibly
            # renamed) title and adopt its revision.
            title = row["title"]
            review_json = row["review_json"]
            self._write_new_state(conn, project_id, state, row["revision"], title,
                                  review_json, source, summary)
        return self.get_required(project_id)

    def delete_project(self, project_id: str) -> bool:
        """Delete a project and its history. Returns False if it was absent."""
        with self._write_tx() as conn:
            cursor = conn.execute(
                "DELETE FROM projects WHERE id = ?", (project_id,)
            )
            return cursor.rowcount > 0

    # ── Internals ─────────────────────────────────────────

    def _save(
        self,
        project_id: str,
        *,
        title: str | None,
        state: ProjectState | None,
        expected_revision: int,
        source: str,
        summary: str,
        review: dict[str, Any] | None = None,
    ) -> ProjectRecord:
        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT revision, title, state_json, review_json FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project '{project_id}' not found")
            if row["revision"] != expected_revision:
                raise RevisionConflictError(
                    f"期望 revision {expected_revision} 已过期，当前为 {row['revision']}",
                    current_revision=row["revision"],
                )

            if state is None:
                # Rename-only: rewrite the stored state's title, keep content.
                state = ProjectState.model_validate_json(row["state_json"])
                state.meta.title = title
            new_title = title if title is not None else state.meta.title
            review_json = (
                json.dumps(review, ensure_ascii=False)
                if review is not None
                else row["review_json"]
            )
            self._write_new_state(
                conn, project_id, state, expected_revision, new_title,
                review_json, source, summary,
            )
        return self.get_required(project_id)

    def _write_new_state(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        state: ProjectState,
        base_revision: int,
        title: str,
        review_json: str,
        source: str,
        summary: str,
    ) -> None:
        """Append the next version and refresh the current snapshot.

        Must run inside a write transaction. The service project ID always
        wins over any meta.id carried by the state.
        """
        state.meta.id = project_id
        state.meta.title = title
        state.meta.touch()
        state_json = serialize_state(state)
        new_revision = base_revision + 1
        now = _utcnow()
        conn.execute(
            "UPDATE projects SET title = ?, revision = ?, state_json = ?,"
            " review_json = ?, updated_at = ? WHERE id = ?",
            (title, new_revision, state_json, review_json, now, project_id),
        )
        conn.execute(
            "INSERT INTO project_versions (project_id, revision, title, state_json,"
            " review_json, source, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, new_revision, title, state_json, review_json,
             source, summary, now),
        )
