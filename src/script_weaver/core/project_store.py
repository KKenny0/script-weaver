"""SQLite-backed persistent storage for web projects (``main-web`` namespace).

Ticket #13 scope: the ``projects`` table holds the current effective
ProjectState snapshot; ``project_versions`` keeps immutable history. Ticket
#14 adds ``generation_runs`` so a generation outlives any single page
connection: the run row is the durable progress record (status, base
revision, completed steps, checkpoint), restarted in-memory tasks are
reconciled from it, and stale active rows are marked interrupted — never
silently resumed.

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
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from script_weaver.core.types import ProjectState

SCHEMA_VERSION = 2

# Run statuses that mean "an in-memory task may still be driving this run".
RUN_ACTIVE_STATUSES = ("running", "stopping")
# Every status the run lifecycle can end in.
RUN_STATUSES = (
    "running", "stopping", "succeeded", "failed", "cancelled", "interrupted",
)
# Statuses a run can never leave once entered.
RUN_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")

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
    """
    CREATE TABLE IF NOT EXISTS generation_runs (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'generate',
        request_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        base_revision INTEGER NOT NULL,
        base_state_json TEXT NOT NULL,
        request_json TEXT NOT NULL,
        completed_steps_json TEXT NOT NULL DEFAULT '[]',
        checkpoint_json TEXT,
        unapplied_json TEXT,
        last_progress_json TEXT,
        error TEXT,
        result_summary_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_generation_runs_project
        ON generation_runs (project_id, created_at DESC)
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


class RunAdmissionError(ProjectStoreError):
    """A generation run cannot be admitted for this project."""


class RequestKeyConflictError(RunAdmissionError):
    """The request_key already exists with a different input snapshot."""

    def __init__(self, message: str, run: GenerationRun):
        super().__init__(message)
        self.run = run


class ActiveRunConflictError(RunAdmissionError):
    """Another active model run already owns the project."""

    def __init__(self, message: str, run: GenerationRun):
        super().__init__(message)
        self.run = run


class RunStageRejectedError(ProjectStoreError):
    """A stage commit was refused because the run left the running state."""


def _utcnow() -> str:
    # timezone.utc (not datetime.UTC): the project still supports Python 3.10.
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


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


@dataclass
class GenerationRun:
    """A persisted generation run: durable progress for a background task.

    ``request``/``request_hash`` identify the submitted input/config snapshot
    (idempotency), ``base_revision``/``base_state_json`` anchor the per-stage
    CAS commits, ``completed_steps``/``checkpoint_json`` record how far the
    pipeline got, and ``unapplied_json`` keeps a stage result that could not
    be applied because the user edited the project meanwhile.
    """

    run_id: str
    project_id: str
    kind: str
    request_key: str
    request_hash: str
    status: str
    base_revision: int
    base_state_json: str
    request: dict[str, Any] = field(default_factory=dict)
    completed_steps: list[str] = field(default_factory=list)
    checkpoint_json: str | None = None
    unapplied_json: str | None = None
    last_progress: dict[str, Any] | None = None
    error: str | None = None
    result_summary: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""


def hash_run_request(request: dict[str, Any]) -> str:
    """Stable fingerprint of a run's input/config snapshot."""
    return sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


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


# Run field name → column, shared by every run-field update path.
_RUN_UPDATE_COLUMNS = {
    "status": "status",
    "base_revision": "base_revision",
    "base_state_json": "base_state_json",
    "completed_steps": "completed_steps_json",
    "checkpoint_json": "checkpoint_json",
    "unapplied_json": "unapplied_json",
    "last_progress": "last_progress_json",
    "error": "error",
    "result_summary": "result_summary_json",
}


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
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback_quietly()
                raise

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        """Short exclusive write transaction; rolls back fully on any error."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                # COMMIT stays inside the guarded block: a rejected COMMIT
                # must roll back instead of leaving an open transaction with
                # half-applied writes visible to later reads.
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback_quietly()
                raise

    def _rollback_quietly(self) -> None:
        """Roll back if a transaction is still open; never mask the cause."""
        if self._conn.in_transaction:
            with suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")

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

    # ── Generation runs (ticket #14) ──────────────────────

    _RUN_COLUMNS = (
        "id, project_id, kind, request_key, request_hash, status, base_revision,"
        " base_state_json, request_json, completed_steps_json, checkpoint_json,"
        " unapplied_json, last_progress_json, error, result_summary_json,"
        " created_at, updated_at"
    )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> GenerationRun:
        def _load_json(text: str | None) -> Any:
            return json.loads(text) if text else None

        return GenerationRun(
            run_id=row["id"],
            project_id=row["project_id"],
            kind=row["kind"],
            request_key=row["request_key"],
            request_hash=row["request_hash"],
            status=row["status"],
            base_revision=row["base_revision"],
            base_state_json=row["base_state_json"],
            request=_load_json(row["request_json"]) or {},
            completed_steps=_load_json(row["completed_steps_json"]) or [],
            checkpoint_json=row["checkpoint_json"],
            unapplied_json=row["unapplied_json"],
            last_progress=_load_json(row["last_progress_json"]),
            error=row["error"],
            result_summary=_load_json(row["result_summary_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_generation_run(
        self,
        project_id: str,
        *,
        kind: str,
        request_key: str,
        request: dict[str, Any],
        base_revision: int,
        base_state_json: str,
        checkpoint_json: str | None,
    ) -> GenerationRun:
        """Insert a run in ``running`` status; the caller then starts its task."""
        run_id = uuid.uuid4().hex[:12]
        now = _utcnow()
        with self._write_tx() as conn:
            conn.execute(
                "INSERT INTO generation_runs (id, project_id, kind, request_key,"
                " request_hash, status, base_revision, base_state_json,"
                " request_json, completed_steps_json, checkpoint_json,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, '[]', ?, ?, ?)",
                (
                    run_id, project_id, kind, request_key,
                    hash_run_request(request), base_revision, base_state_json,
                    json.dumps(request, ensure_ascii=False), checkpoint_json,
                    now, now,
                ),
            )
        return self.get_generation_run_required(run_id)

    def admit_generation_run(
        self,
        project_id: str,
        *,
        kind: str,
        request_key: str,
        request: dict[str, Any],
        request_hash: str,
        base_revision: int,
        base_state_json: str,
        checkpoint_json: str | None,
    ) -> tuple[GenerationRun, bool]:
        """Idempotently admit a run; lookup and insert share one transaction.

        The ``(project_id, request_key)`` lookup covers runs in **every**
        status, earliest record first — a key keeps pointing at its original
        request even when later runs (other keys) overshadow it, and input
        equality is judged against the stored snapshot's hash, never by
        re-interpreting the project's current content. Same key + same input
        returns the original run (``created=False``); same key + different
        input raises :class:`RequestKeyConflictError`; another active run
        raises :class:`ActiveRunConflictError`; otherwise the row is created
        ``running`` in this same transaction.
        """
        run_id = uuid.uuid4().hex[:12]
        now = _utcnow()
        with self._write_tx() as conn:
            existing = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs"
                " WHERE project_id = ? AND request_key = ?"
                " ORDER BY created_at ASC, rowid ASC LIMIT 1",
                (project_id, request_key),
            ).fetchone()
            if existing is not None:
                run = self._row_to_run(existing)
                if run.request_hash != request_hash:
                    raise RequestKeyConflictError(
                        "同一 request_key 已绑定其他输入，提交被拒绝。", run=run
                    )
                return run, False
            active = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs"
                f" WHERE project_id = ? AND status IN ({','.join('?' * len(RUN_ACTIVE_STATUSES))})"
                " ORDER BY created_at ASC, rowid ASC LIMIT 1",
                (project_id, *RUN_ACTIVE_STATUSES),
            ).fetchone()
            if active is not None:
                raise ActiveRunConflictError(
                    "该项目已有生成运行进行中。", run=self._row_to_run(active)
                )
            conn.execute(
                "INSERT INTO generation_runs (id, project_id, kind, request_key,"
                " request_hash, status, base_revision, base_state_json,"
                " request_json, completed_steps_json, checkpoint_json,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, '[]', ?, ?, ?)",
                (
                    run_id, project_id, kind, request_key, request_hash,
                    base_revision, base_state_json,
                    json.dumps(request, ensure_ascii=False), checkpoint_json,
                    now, now,
                ),
            )
            row = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return self._row_to_run(row), True

    def transition_generation_run_stopping(self, run_id: str) -> tuple[GenerationRun, bool]:
        """Atomically move a ``running`` run to ``stopping``.

        Returns ``(run, accepted)``: ``accepted`` is True only when this call
        performed the running→stopping transition. Already-stopping or
        terminal runs return their current state untouched — terminal states
        never regress, and a repeated stop must not re-cancel a task that is
        already winding down.
        """
        with self._write_tx() as conn:
            row = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Run '{run_id}' not found")
            if row["status"] != "running":
                return self._row_to_run(row), False
            conn.execute(
                "UPDATE generation_runs SET status = 'stopping',"
                " last_progress_json = ?, updated_at = ?"
                " WHERE id = ? AND status = 'running'",
                (
                    json.dumps(
                        {"stage": "stop", "message": "正在停止生成…"},
                        ensure_ascii=False,
                    ),
                    _utcnow(),
                    run_id,
                ),
            )
            updated = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return self._row_to_run(updated), True

    def settle_generation_run(self, run_id: str, *, status: str, **updates: Any) -> GenerationRun:
        """Write a run's terminal state with atomic stop-vs-success ordering.

        Terminal rows are returned unchanged (double finalization is safe).
        A ``succeeded`` settle on a ``stopping`` row is refused as success —
        the accepted stop wins and the row settles ``cancelled`` — while a
        success that committed first simply makes a later stop a no-op. The
        database transition order is the single source of truth.
        """
        if status not in RUN_TERMINAL_STATUSES:
            raise ValueError(f"Not a terminal run status: {status!r}")
        with self._write_tx() as conn:
            row = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Run '{run_id}' not found")
            if row["status"] in RUN_TERMINAL_STATUSES:
                return self._row_to_run(row)
            effective = status
            if row["status"] == "stopping" and status == "succeeded":
                effective = "cancelled"
            assignments = ["status = ?", "updated_at = ?"]
            params: list[Any] = [effective, _utcnow()]
            for attr, column, value in self._run_update_fields(updates):
                assignments.append(f"{column} = ?")
                params.append(value)
            params.append(run_id)
            conn.execute(
                f"UPDATE generation_runs SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            updated = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return self._row_to_run(updated)

    def commit_generation_stage(
        self,
        run_id: str,
        *,
        state: ProjectState,
        base_revision: int,
        base_state_json: str,
        step: str,
        summary: str,
        progress: dict[str, Any],
    ) -> tuple[ProjectRecord, GenerationRun]:
        """Commit one pipeline stage atomically: project + version + run.

        A single SQLite transaction performs the revision CAS, the project
        snapshot update, the immutable version insert and the run's
        base-revision/completed-steps/checkpoint/last-progress update. Any
        failure rolls the whole stage back; the returned record and run are
        the exact rows this transaction wrote (never a later re-read). A run
        that already left ``running`` (e.g. an accepted stop) rejects the
        stage instead of recording it.
        """
        with self._write_tx() as conn:
            run_row = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise ProjectNotFoundError(f"Run '{run_id}' not found")
            if run_row["status"] != "running":
                raise RunStageRejectedError(
                    f"运行已进入 {run_row['status']}，拒绝提交阶段「{step}」"
                )
            project_id = run_row["project_id"]
            proj = conn.execute(
                "SELECT revision, title, state_json, review_json, auto_approve,"
                " skill_bindings_json, created_at FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if proj is None:
                raise ProjectNotFoundError(f"Project '{project_id}' not found")
            if proj["revision"] != base_revision and _content_signature(
                proj["state_json"]
            ) != _content_signature(base_state_json):
                raise RevisionConflictError(
                    "项目内容在生成期间已被修改，生成结果未覆盖当前内容",
                    current_revision=proj["revision"],
                )
            new_revision, new_state_json, updated_at = self._write_new_state(
                conn, project_id, state, proj["revision"], proj["title"],
                proj["review_json"], "pipeline", summary,
            )
            completed = json.loads(run_row["completed_steps_json"] or "[]")
            completed.append(step)
            conn.execute(
                "UPDATE generation_runs SET base_revision = ?, base_state_json = ?,"
                " completed_steps_json = ?, checkpoint_json = ?,"
                " last_progress_json = ?, updated_at = ? WHERE id = ?",
                (
                    new_revision, new_state_json,
                    json.dumps(completed, ensure_ascii=False),
                    new_state_json,
                    json.dumps(progress, ensure_ascii=False),
                    _utcnow(), run_id,
                ),
            )
            record = ProjectRecord(
                project_id=project_id,
                title=proj["title"],
                revision=new_revision,
                state=state,
                state_json=new_state_json,
                review=json.loads(proj["review_json"] or "{}"),
                auto_approve=bool(proj["auto_approve"]),
                skill_bindings=json.loads(proj["skill_bindings_json"] or "{}"),
                created_at=proj["created_at"],
                updated_at=updated_at,
            )
            updated_run = conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return record, self._row_to_run(updated_run)

    def get_generation_run(self, run_id: str) -> GenerationRun | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def get_generation_run_required(self, run_id: str) -> GenerationRun:
        run = self.get_generation_run(run_id)
        if run is None:
            raise ProjectNotFoundError(f"Run '{run_id}' not found")
        return run

    def latest_generation_run(self, project_id: str) -> GenerationRun | None:
        """The project's most recent run of any status."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs"
                " WHERE project_id = ? ORDER BY created_at DESC, rowid DESC"
                " LIMIT 1",
                (project_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def active_generation_run(self, project_id: str) -> GenerationRun | None:
        """The project's active run, if any (running/stopping)."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs"
                f" WHERE project_id = ? AND status IN ({','.join('?' * len(RUN_ACTIVE_STATUSES))})"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id, *RUN_ACTIVE_STATUSES),
            ).fetchone()
        return self._row_to_run(row) if row else None


    @staticmethod
    def _run_update_fields(
        updates: dict[str, Any]
    ) -> list[tuple[str, str, Any]]:
        """Validate/serialize run field updates into (attr, column, value)."""
        fields: list[tuple[str, str, Any]] = []
        for attr, column in _RUN_UPDATE_COLUMNS.items():
            if attr not in updates:
                continue
            value = updates.pop(attr)
            if attr in ("completed_steps", "last_progress", "result_summary"):
                value = (
                    json.dumps(value, ensure_ascii=False)
                    if value is not None else None
                )
            fields.append((attr, column, value))
        if updates:
            raise TypeError(f"Unknown generation-run fields: {sorted(updates)}")
        return fields

    def update_generation_run(
        self, run_id: str, **updates: Any
    ) -> GenerationRun:
        """Persist run progress fields; unknown fields are a programming error."""
        fields = self._run_update_fields(updates)
        if not fields:
            return self.get_generation_run_required(run_id)
        assignments = [f"{column} = ?" for _, column, _ in fields]
        params = [value for _, _, value in fields]
        assignments.append("updated_at = ?")
        params.extend([_utcnow(), run_id])
        with self._write_tx() as conn:
            cursor = conn.execute(
                f"UPDATE generation_runs SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            if cursor.rowcount != 1:
                raise ProjectNotFoundError(f"Run '{run_id}' not found")
        return self.get_generation_run_required(run_id)

    def interrupt_stale_generation_runs(self) -> int:
        """Mark leftover active runs as interrupted (startup reconciliation).

        Runs whose in-memory task died with the previous process are facts to
        report, never work to silently resume: no model call happens here.
        """
        now = _utcnow()
        with self._write_tx() as conn:
            cursor = conn.execute(
                "UPDATE generation_runs SET status = 'interrupted',"
                " error = COALESCE(error, '服务在运行期间重启，生成已中断；不会自动继续。'),"
                " updated_at = ? WHERE status IN ('running', 'stopping')",
                (now,),
            )
            return cursor.rowcount

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
    ) -> tuple[int, str, str]:
        """Append the next version and refresh the current snapshot.

        Must run inside a write transaction. The service project ID always
        wins over any meta.id carried by the state. Returns the exact
        ``(revision, state_json, updated_at)`` this transaction wrote, so
        callers never have to re-read the project as their new base.
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
        return new_revision, state_json, now
