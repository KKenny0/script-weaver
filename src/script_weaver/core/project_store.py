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

from script_weaver.core.resume import update_checkpoint_envelope
from script_weaver.core.types import ProjectState

SCHEMA_VERSION = 3

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
    """
    CREATE TABLE IF NOT EXISTS card_candidates (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL UNIQUE REFERENCES generation_runs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        target_id TEXT NOT NULL,
        base_revision INTEGER NOT NULL,
        original_json TEXT NOT NULL,
        proposed_json TEXT NOT NULL,
        changes_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ready','stale','accepted','rejected')),
        accepted_revision INTEGER,
        created_at TEXT NOT NULL
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

    Holds an open file description on the lock file for the process
    lifetime so a second API instance on the same data directory refuses to
    start. The OS releases the lock when the process exits, so a clean
    shutdown allows the next instance to start. ``lock_name`` lets other
    surfaces (the CLI's output directory) reuse the same capability.
    """

    def __init__(self, directory: Path, *, lock_name: str = "api.lock"):
        self._path = directory / lock_name
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


def content_signature(state_json: str) -> str:
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
            if row["revision"] != base_revision and content_signature(
                row["state_json"]
            ) != content_signature(base_state_json):
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

    def save_card_edit(
        self,
        project_id: str,
        *,
        kind: str,
        target_id: str,
        changes: dict[str, Any],
        expected_revision: int,
    ) -> tuple[ProjectRecord, bool]:
        """CAS-save edits to ONE character/scene/shot card (ticket #16).

        The whole edit is one write transaction: read the current project,
        check ``expected_revision``, locate the target by stable id, apply
        and fully validate the change, recompute derived fields for shots,
        merge the conservative downstream review flags into ``review_json``,
        and write the new snapshot + immutable version. Returns
        ``(record, changed)`` where ``record`` is the exact snapshot this
        transaction wrote (never a post-commit re-read); with
        ``changed=False`` the project is returned untouched — still fully
        validated, but without a new revision, version or review flag.
        """
        with self._write_tx() as conn:
            return self._save_card_edit(conn, project_id, kind, target_id, changes, expected_revision)

    def _save_card_edit(
        self, conn: sqlite3.Connection, project_id: str, kind: str,
        target_id: str, changes: dict[str, Any], expected_revision: int,
    ) -> tuple[ProjectRecord, bool]:
        # Function-level import: card_edit imports this module's error base
        # class, so a module-level import would be circular.
        from script_weaver.core.card_edit import (
            KIND_LABELS,
            apply_card_changes,
            build_review_flags,
            merge_review_flags,
        )

        row = conn.execute(
            "SELECT id, revision, title, state_json, review_json, auto_approve,"
            " skill_bindings_json, created_at, updated_at"
            " FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ProjectNotFoundError(f"Project '{project_id}' not found")
        if row["revision"] != expected_revision:
            raise RevisionConflictError(
                f"期望 revision {expected_revision} 已过期，当前为 {row['revision']}",
                current_revision=row["revision"],
            )
        state = ProjectState.model_validate_json(row["state_json"])
        outcome = apply_card_changes(state, kind, target_id, changes)
        current_review = json.loads(row["review_json"] or "{}")
        if not outcome.changed:
            record = self._record_from_row(row, state, row["revision"],
                                           row["state_json"], current_review,
                                           row["updated_at"])
            return record, False
        review = merge_review_flags(
            current_review,
            build_review_flags(
                kind, target_id, outcome.label,
                since_revision=expected_revision + 1,
                affected_artifacts=outcome.affected_artifacts,
            ),
        )
        new_revision, new_state_json, updated_at = self._write_new_state(
            conn, project_id, state, expected_revision, row["title"],
            json.dumps(review, ensure_ascii=False), "manual",
            f"编辑{KIND_LABELS[kind]}：{outcome.label}",
        )
        record = self._record_from_row(row, state, new_revision,
                                       new_state_json, review, updated_at)
        return record, True

    @staticmethod
    def _record_from_row(
        row: sqlite3.Row,
        state: ProjectState,
        revision: int,
        state_json: str,
        review: dict[str, Any],
        updated_at: str,
    ) -> ProjectRecord:
        """Build the record for the exact values a transaction produced."""
        return ProjectRecord(
            project_id=row["id"],
            title=row["title"],
            revision=revision,
            state=state,
            state_json=state_json,
            review=review,
            auto_approve=bool(row["auto_approve"]),
            skill_bindings=json.loads(row["skill_bindings_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=updated_at,
        )

    def confirm_review(
        self,
        project_id: str,
        *,
        expected_revision: int,
        selections: list[dict[str, Any]],
    ) -> tuple[ProjectRecord, list[dict[str, Any]]]:
        """CAS-confirm selected pending-review flags as kept as-is (ticket #17).

        One write transaction: read the current project, verify every
        selection's ``project_id`` names THIS project (two projects can hold
        byte-identical flags — the ownership field is what binds a decision
        to the project it was made in), check ``expected_revision``, match
        every selection against the CURRENT review flags by its full
        identity (artifact + reason + upstream kind/id + ``since_revision``
        — never the artifact name alone, so an upstream re-trigger survives
        an old confirm request), verify the flagged downstream artifacts
        still exist, remove exactly the matched flags while preserving all
        other review metadata, and append the new immutable version. The
        project content is untouched — the save rides the normal history
        mechanism (meta timestamps may refresh). Returns
        ``(record, confirmed)`` where both come from the exact values this
        transaction wrote (never a post-commit re-read). Any refusal —
        unknown project, foreign/missing selection ownership, stale
        revision, a selection that matches no current flag — raises before
        a single row changes.
        """
        # Function-level import: card_edit imports this module's error base
        # class, so a module-level import would be circular.
        from script_weaver.core.card_edit import (
            ARTIFACT_LABELS,
            confirm_review_flags,
            validate_review_selections,
        )

        with self._write_tx() as conn:
            row = conn.execute(
                "SELECT id, revision, title, state_json, review_json, auto_approve,"
                " skill_bindings_json, created_at, updated_at"
                " FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFoundError(f"Project '{project_id}' not found")
            # Input validity precedes state comparison: a selection owned by
            # another project is a 422-shaped refusal even when the revision
            # is stale too — re-reading the target project could never fix it.
            validate_review_selections(selections, project_id=project_id)
            if row["revision"] != expected_revision:
                raise RevisionConflictError(
                    f"期望 revision {expected_revision} 已过期，当前为 {row['revision']}",
                    current_revision=row["revision"],
                )
            state = ProjectState.model_validate_json(row["state_json"])
            current_review = json.loads(row["review_json"] or "{}")
            review, confirmed = confirm_review_flags(
                current_review, selections, state
            )
            scope = "、".join(
                dict.fromkeys(
                    ARTIFACT_LABELS.get(f.get("artifact"), str(f.get("artifact")))
                    for f in confirmed
                )
            )
            new_revision, new_state_json, updated_at = self._write_new_state(
                conn, project_id, state, expected_revision, row["title"],
                json.dumps(review, ensure_ascii=False), "manual",
                f"确认沿用待复核内容（{scope}，共 {len(confirmed)} 项）",
            )
            record = self._record_from_row(row, state, new_revision,
                                           new_state_json, review, updated_at)
            return record, confirmed

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
        completed_steps: list[str] | None = None,
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
                " VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, project_id, kind, request_key,
                    hash_run_request(request), base_revision, base_state_json,
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(completed_steps or [], ensure_ascii=False),
                    checkpoint_json,
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
        completed_steps: list[str] | None = None,
    ) -> tuple[GenerationRun, bool]:
        """Idempotently admit a run; lookup and insert share one transaction.

        The ``(project_id, request_key)`` lookup covers runs in **every**
        status, earliest record first — a key keeps pointing at its original
        request even when later runs (other keys) overshadow it, and input
        equality is judged against the stored snapshot's hash for fresh runs.
        Resume requests instead use the immutable resume_of target: progress
        and later configuration changes cannot invalidate an admitted intent.
        Same key + same input
        returns the original run (``created=False``); same key + different
        input raises :class:`RequestKeyConflictError`; another active run
        raises :class:`ActiveRunConflictError`; otherwise the row is created
        ``running`` in this same transaction. ``completed_steps`` seeds the
        row's progress prefix (a resumed run inherits the checkpointed
        stages of the run it continues).
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
                same_resume = request.get("resume_of") is not None and request.get("resume_of") == run.request.get("resume_of")
                if not same_resume and run.request_hash != request_hash:
                    raise RequestKeyConflictError(
                        "同一 request_key 已绑定其他输入，提交被拒绝。", run=run
                    )
                return run, False
            if kind in ("card_candidate", "global_candidate"):
                from script_weaver.core.card_edit import card_snapshot
                project = conn.execute("SELECT revision, state_json FROM projects WHERE id = ?", (project_id,)).fetchone()
                if project is None:
                    raise ProjectNotFoundError(f"Project '{project_id}' not found")
                if project["revision"] != base_revision or project["state_json"] != base_state_json:
                    raise RevisionConflictError("项目已变化，请载入最新内容后重新发起。", project["revision"])
                if kind == "card_candidate":
                    card_snapshot(ProjectState.model_validate_json(project["state_json"]), request["kind"], request["target_id"])
            if request.get("resume_of") is not None:
                project = conn.execute(
                    "SELECT revision, state_json FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if project is None:
                    raise ProjectNotFoundError(f"Project '{project_id}' not found")
                if content_signature(project["state_json"]) != content_signature(base_state_json):
                    raise RevisionConflictError(
                        "项目内容在恢复校验后已被修改。", current_revision=project["revision"]
                    )
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
                " VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, project_id, kind, request_key, request_hash,
                    base_revision, base_state_json,
                    json.dumps(request, ensure_ascii=False),
                    json.dumps(completed_steps or [], ensure_ascii=False),
                    checkpoint_json,
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
        base-revision/completed-steps/checkpoint/last-progress update. The
        checkpoint column stores the ticket-#15 resume envelope (input,
        state snapshot + digest, completed prefix, execution fingerprint,
        growth status, basis revision) carried forward from admission. Any
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
            if proj["revision"] != base_revision and content_signature(
                proj["state_json"]
            ) != content_signature(base_state_json):
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
            checkpoint_text = update_checkpoint_envelope(
                run_row["checkpoint_json"],
                state_json=new_state_json,
                completed_steps=completed,
                revision=new_revision,
            )
            conn.execute(
                "UPDATE generation_runs SET base_revision = ?, base_state_json = ?,"
                " completed_steps_json = ?, checkpoint_json = ?,"
                " last_progress_json = ?, updated_at = ? WHERE id = ?",
                (
                    new_revision, new_state_json,
                    json.dumps(completed, ensure_ascii=False),
                    checkpoint_text,
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

    def generation_run_for_key(self, project_id: str, request_key: str) -> GenerationRun | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs"
                " WHERE project_id = ? AND request_key = ?"
                " ORDER BY created_at ASC, rowid ASC LIMIT 1",
                (project_id, request_key),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def get_generation_run_required(self, run_id: str) -> GenerationRun:
        run = self.get_generation_run(run_id)
        if run is None:
            raise ProjectNotFoundError(f"Run '{run_id}' not found")
        return run

    def latest_generation_run(self, project_id: str) -> GenerationRun | None:
        """The latest full-generation run; card candidates have their own history."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._RUN_COLUMNS} FROM generation_runs"
                " WHERE project_id = ? AND kind = 'generate' ORDER BY created_at DESC, rowid DESC"
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
        A checkpoint whose growth was still ``running`` is converged to
        ``interrupted`` in the same transaction — the side effects may have
        partially happened and must stay recorded, never replayed.
        """
        now = _utcnow()
        with self._write_tx() as conn:
            stale = conn.execute(
                "SELECT id, checkpoint_json FROM generation_runs"
                " WHERE status IN ('running', 'stopping')"
            ).fetchall()
            if not stale:
                return 0
            conn.execute(
                "UPDATE generation_runs SET status = 'interrupted',"
                " error = COALESCE(error, '服务在运行期间重启，生成已中断；不会自动继续。'),"
                " updated_at = ? WHERE status IN ('running', 'stopping')",
                (now,),
            )
            for row in stale:
                try:
                    envelope = json.loads(row["checkpoint_json"] or "")
                except (json.JSONDecodeError, TypeError):
                    continue
                if (
                    isinstance(envelope, dict)
                    and envelope.get("growth_status") == "running"
                ):
                    conn.execute(
                        "UPDATE generation_runs SET checkpoint_json = ? WHERE id = ?",
                        (
                            update_checkpoint_envelope(
                                row["checkpoint_json"],
                                growth_status="interrupted",
                                growth_error=(
                                    "服务重启时成长任务仍在执行，"
                                    "其副作用可能已部分发生；不会自动重放。"
                                ),
                            ),
                            row["id"],
                        ),
                    )
            return len(stale)

    def list_card_candidates(self, project_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM card_candidates WHERE project_id = ? ORDER BY created_at DESC, rowid DESC", (project_id,)).fetchall()
        return [self._candidate_dict(row) for row in rows]

    def _candidate_dict(self, row) -> dict:
        result = dict(row)
        for field in ("original", "proposed", "changes"):
            result[field] = json.loads(result.pop(field + "_json"))
        if result["kind"] == "global":
            from script_weaver.core.global_revision import impacts
            run = self.get_generation_run_required(result["run_id"])
            base = ProjectState.model_validate_json(run.base_state_json)
            result["affected_artifacts"] = impacts(base, result["target_id"])
            from script_weaver.core.refinement import diff_field_changes, _MISSING
            raw = base.model_dump(mode="json")
            raw.update(result["proposed"])
            result["diff"] = [{"path": change.path,
                "before": None if change.before is _MISSING else change.before,
                "after": None if change.after is _MISSING else change.after,
                "before_exists": change.before is not _MISSING,
                "after_exists": change.after is not _MISSING,
            } for change in diff_field_changes(base, ProjectState.model_validate(raw), strict=True)]
        return result

    def candidate_runs(self, project_id: str) -> list[GenerationRun]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE project_id = ? AND kind IN ('card_candidate', 'global_candidate') ORDER BY created_at DESC, rowid DESC", (project_id,)).fetchall()
        return [self._row_to_run(row) for row in rows]

    def complete_card_candidate(self, run_id: str, output: dict) -> dict:
        from script_weaver.core.card_edit import card_snapshot, validate_candidate_output
        with self._write_tx() as conn:
            row = conn.execute(f"SELECT {self._RUN_COLUMNS} FROM generation_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or row["kind"] not in ("card_candidate", "global_candidate") or row["status"] != "running":
                raise RunStageRejectedError("候选运行已停止，结果未保存。")
            run = self._row_to_run(row)
            state = ProjectState.model_validate_json(run.base_state_json)
            if run.kind == "global_candidate":
                from script_weaver.core.global_revision import validate_global_result
                target_id, updated = validate_global_result(state, output)
                kind = "global"
                original = {target_id: state.model_dump(mode="json")[target_id]}
                proposed = {target_id: updated.model_dump(mode="json")[target_id]}
                output = {"changes": proposed}
            else:
                kind, target_id = run.request["kind"], run.request["target_id"]
                original = card_snapshot(state, kind, target_id)
                validate_candidate_output(state, kind, target_id, output)
                proposed = card_snapshot(state, kind, target_id)
            revision = conn.execute("SELECT revision FROM projects WHERE id = ?", (run.project_id,)).fetchone()[0]
            candidate_id = str(uuid.uuid4())
            conn.execute("INSERT INTO card_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)", (
                candidate_id, run.project_id, run_id, kind, target_id, run.base_revision,
                json.dumps(original, ensure_ascii=False), json.dumps(proposed, ensure_ascii=False),
                json.dumps(output["changes"], ensure_ascii=False),
                "ready" if revision == run.base_revision else "stale", _utcnow(),
            ))
            conn.execute("UPDATE generation_runs SET status = 'succeeded', result_summary_json = ?, updated_at = ? WHERE id = ?", (json.dumps({"candidate_id": candidate_id}), _utcnow(), run_id))
            return self._candidate_dict(conn.execute("SELECT * FROM card_candidates WHERE id = ?", (candidate_id,)).fetchone())

    def decide_card_candidate(self, project_id: str, candidate_id: str, *, accept: bool) -> tuple[dict, ProjectRecord | None]:
        from script_weaver.core.card_edit import card_snapshot
        with self._write_tx() as conn:
            row = conn.execute("SELECT * FROM card_candidates WHERE id = ? AND project_id = ?", (candidate_id, project_id)).fetchone()
            if row is None:
                raise ProjectNotFoundError("候选不存在于此项目。")
            candidate = self._candidate_dict(row)
            if candidate["status"] == "accepted":
                if not accept:
                    raise ProjectStoreError("候选已采用，不能放弃。")
                # Return the original adoption snapshot even after subsequent saves.
                version = conn.execute("SELECT v.*, p.auto_approve, p.skill_bindings_json, p.id, v.created_at AS updated_at FROM project_versions v JOIN projects p ON p.id = v.project_id WHERE v.project_id = ? AND v.revision = ?", (project_id, candidate["accepted_revision"])).fetchone()
                return candidate, self._row_to_record(version)
            if not accept:
                conn.execute("UPDATE card_candidates SET status = 'rejected' WHERE id = ?", (candidate_id,))
                candidate["status"] = "rejected"
                return candidate, None
            project = conn.execute("SELECT revision, state_json FROM projects WHERE id = ?", (project_id,)).fetchone()
            if candidate["status"] != "ready" or project["revision"] != candidate["base_revision"]:
                raise RevisionConflictError("候选已过期或已放弃；项目发生变化，请重新发起。", project["revision"])
            state = ProjectState.model_validate_json(project["state_json"])
            if candidate["kind"] == "global":
                from script_weaver.core.global_revision import validate_global_result, impacts
                from script_weaver.core.card_edit import build_review_flags, merge_review_flags
                artifact = candidate["target_id"]
                if {artifact: state.model_dump(mode="json")[artifact]} != candidate["original"]:
                    raise ProjectStoreError("候选依据与当前产物不一致。")
                if set(candidate["proposed"]) != {artifact} or candidate["changes"] != candidate["proposed"]:
                    raise ProjectStoreError("候选整件产物范围不一致。")
                raw = state.model_dump(mode="json")
                raw.update(candidate["proposed"])
                target, updated = validate_global_result(state, ProjectState.model_validate(raw))
                if target != artifact or {artifact: updated.model_dump(mode="json")[artifact]} != candidate["proposed"]:
                    raise ProjectStoreError("候选范围不一致。")
                full = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                review = merge_review_flags(json.loads(full["review_json"] or "{}"), build_review_flags(
                    "global", artifact, artifact, project["revision"] + 1, impacts(state, artifact)))
                revision, state_json, now = self._write_new_state(conn, project_id, updated,
                    project["revision"], full["title"], json.dumps(review, ensure_ascii=False),
                    "manual", f"采用全局候选：{artifact}")
                record = self._record_from_row(full, updated, revision, state_json, review, now)
                conn.execute("UPDATE card_candidates SET status = 'accepted', accepted_revision = ? WHERE id = ?", (revision, candidate_id))
                candidate.update(status="accepted", accepted_revision=revision)
                return candidate, record
            if card_snapshot(state, candidate["kind"], candidate["target_id"]) != candidate["original"]:
                raise RevisionConflictError("候选依据与当前卡片不一致，请重新发起。", project["revision"])
            record, changed = self._save_card_edit(conn, project_id, candidate["kind"], candidate["target_id"], candidate["changes"], candidate["base_revision"])
            if not changed or card_snapshot(record.state, candidate["kind"], candidate["target_id"]) != candidate["proposed"]:
                raise ProjectStoreError("候选内容校验失败，未采用。")
            conn.execute("UPDATE card_candidates SET status = 'accepted', accepted_revision = ? WHERE id = ?", (record.revision, candidate_id))
            candidate.update(status="accepted", accepted_revision=record.revision)
            return candidate, record

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
        conn.execute("UPDATE card_candidates SET status = 'stale' WHERE project_id = ? AND status = 'ready'", (project_id,))
        return new_revision, state_json, now
