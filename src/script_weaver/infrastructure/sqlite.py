"""SQLite per-thread connections, migrations, and single-process write serialization."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def default_data_dir() -> Path:
    root = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME")
    return Path(root) / "script-weaver" if root else Path.home() / ".local" / "share" / "script-weaver"


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
        # Repair the one exact prerelease v4 shape before any later migration is
        # allowed to build on it. A valid published v4 is a read-only no-op here.
        needs_v4_repair = bool(
            4 in applied
            and (
                {"source_document_version_id", "derived_from_ids_json"}
                - _table_columns(connection, "creative_document_versions")
                or "status" not in _table_columns(connection, "episodes")
            )
        )
        if needs_v4_repair:
            _repair_unreleased_v4(connection, path, max(applied))
        if 6 in applied:
            _repair_unreleased_v6(connection, path)
        if 4 in applied and not needs_v4_repair:
            _repair_unreleased_v4(connection, path, max(applied))
        if applied and latest in applied:
            # Stage 3's v4 migration was exercised locally before release. Its
            # final contract added three columns without changing the version;
            # repair only that exact additive prerelease shape so developer DBs
            # are not stranded. Published migrations remain immutable.
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
        has_ledger = True
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


def _repair_unreleased_v4(
    connection: sqlite3.Connection, path: Path, expected_version: int = 4
) -> None:
    from .schema_shape import expected_tables

    expected = expected_tables(expected_version)
    known_missing = {
        "creative_document_versions": {"source_document_version_id", "derived_from_ids_json"},
        "episodes": {"status"},
    }
    actual_missing: dict[str, set[str]] = {}
    problems: list[str] = []
    for table, required in expected.items():
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            problems.append(f"missing table {table}")
            continue
        actual = _table_columns(connection, table)
        missing = required - actual
        extra = actual - required
        if missing:
            actual_missing[table] = missing
        if extra:
            problems.append(f"table {table} has unexpected columns: {sorted(extra)}")
    if not actual_missing and not problems:
        _repair_legacy_h3_jobs(connection, path)
        return
    if problems or actual_missing != known_missing:
        detail = problems or [
            f"table {table} missing columns: {sorted(columns)}"
            for table, columns in actual_missing.items()
        ]
        raise SchemaError("database schema is incompatible with this version: " + "; ".join(detail))
    _backup_existing_database(connection, path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "ALTER TABLE creative_document_versions ADD COLUMN source_document_version_id TEXT REFERENCES creative_document_versions(id)"
        )
        connection.execute(
            "ALTER TABLE creative_document_versions ADD COLUMN derived_from_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(derived_from_ids_json))"
        )
        connection.execute(
            "ALTER TABLE episodes ADD COLUMN status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','removed'))"
        )
        connection.execute(
            """UPDATE episodes SET status='removed'
            WHERE is_stale=1 AND stale_reason='removed from accepted development map'"""
        )
        connection.execute(
            """UPDATE creative_document_versions AS version
            SET source_document_version_id=(
                  SELECT document.source_document_version_id
                  FROM creative_documents AS document
                  WHERE document.current_version_id=version.id
                ),
                derived_from_ids_json=(
                  SELECT document.derived_from_ids_json
                  FROM creative_documents AS document
                  WHERE document.current_version_id=version.id
                )
            WHERE version.id IN (
              SELECT current_version_id FROM creative_documents WHERE current_version_id IS NOT NULL
            )"""
        )
        connection.execute("DROP TRIGGER IF EXISTS creative_document_versions_lineage_immutable")
        connection.execute(
            """CREATE TRIGGER creative_document_versions_lineage_immutable
            BEFORE UPDATE OF source_document_version_id,derived_from_ids_json ON creative_document_versions
            BEGIN SELECT RAISE(ABORT, 'creative document version lineage is immutable'); END"""
        )
        connection.commit()
        _repair_legacy_h3_jobs(connection, path)
    except Exception:
        connection.rollback()
        raise


def _repair_legacy_h3_jobs(connection: sqlite3.Connection, path: Path) -> None:
    """Close or upgrade prerelease H3 jobs so restart never loops on validation errors."""
    rows = connection.execute(
        """SELECT * FROM generation_jobs WHERE adapter='h3-fl2va'
        AND state IN ('AWAITING_CONFIRMATION','CONFIRMED','SUBMITTING','H3_SUBMITTED','H3_RUNNING')"""
    ).fetchall()
    legacy = []
    for row in rows:
        try:
            spec = json.loads(row["spec_json"])
        except (TypeError, json.JSONDecodeError):
            spec = {}
        if "keyframe_media_ids" in spec or "keyframes" not in spec or "seed" not in spec:
            legacy.append((row, spec))
    if not legacy:
        return
    _backup_existing_database(connection, path)
    timestamp = _utcnow()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row, spec in legacy:
            try:
                media_ids = spec.pop("keyframe_media_ids")
                if not isinstance(media_ids, list) or not 1 <= len(media_ids) <= 2:
                    raise ValueError("invalid legacy keyframe list")
                conditions = []
                keyframes = []
                for index, media_id in enumerate(media_ids):
                    matches = connection.execute(
                        """SELECT m.sha256,m.accepted_asset_version_id,v.asset_id,a.revision,
                        b.id,b.asset_version_id,b.binding_mode,b.is_stale,b.stale_reason,
                        b.source_document_version_id,b.source_projection_revision,
                        b.derived_from_ids_json
                        FROM media_versions m JOIN asset_versions v
                          ON v.id=m.accepted_asset_version_id
                        JOIN assets a ON a.id=v.asset_id
                        JOIN reference_bindings b ON b.shot_id=?
                          AND b.asset_version_id=m.accepted_asset_version_id
                        WHERE m.id=? AND m.project_id=? AND m.kind='image'
                          AND m.mime='image/png' AND m.candidate_status='accepted'
                          AND m.is_stale=0 AND b.binding_mode='frozen' AND b.is_stale=0""",
                        (row["owner_id"], media_id, row["project_id"]),
                    ).fetchall()
                    if len(matches) != 1:
                        raise ValueError("legacy keyframe binding is ambiguous")
                    match = matches[0]
                    frame_index = 0 if index == 0 else -1
                    if index == 0 and match[2] != spec.get("target_asset_id"):
                        raise ValueError("legacy target does not match start keyframe")
                    binding = {
                        "id": match[4], "asset_version_id": match[5],
                        "mode": match[6], "is_stale": match[7],
                        "stale_reason": match[8],
                        "source_document_version_id": match[9],
                        "source_projection_revision": match[10],
                        "derived_from_ids_json": match[11],
                    }
                    binding_hash = hashlib.sha256(json.dumps(
                        binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest()
                    keyframes.append({
                        "media_id": media_id, "binding_id": match[4],
                        "frame_index": frame_index,
                    })
                    conditions.append({
                        "role": "start" if index == 0 else "end",
                        "frame_index": frame_index, "media_id": media_id,
                        "sha256": match[0], "asset_version_id": match[1],
                        "asset_id": match[2], "asset_revision": match[3],
                        "binding": binding, "binding_fingerprint": binding_hash,
                    })
                sampling = {
                    "num_outputs_per_prompt": 1, "num_inference_steps": 50,
                    "flow_shift": 12.0, "audio_flow_shift": 3.0, "seed": 42,
                }
                spec.update({"keyframes": keyframes, **sampling})
                inputs = json.loads(row["input_hashes_json"])
                inputs["conditions"] = conditions
                inputs["sampling"] = sampling
                inputs.setdefault("target_asset", {})["binding_id"] = keyframes[0]["binding_id"]
                inputs["target_asset"].pop("binding_fingerprint", None)
                exact = hashlib.sha256(json.dumps(
                    {"spec": spec, "input_hashes": inputs}, ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                connection.execute(
                    """UPDATE generation_jobs SET spec_json=?,input_hashes_json=?,fingerprint=?,
                    submitted_fingerprint=CASE WHEN submitted_fingerprint IS NULL THEN NULL ELSE ? END,
                    updated_at=? WHERE id=?""",
                    (json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                     json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                     exact, exact, timestamp, row["id"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                connection.execute(
                    """UPDATE generation_jobs SET state='FAILED',error_json=?,completed_at=?,
                    updated_at=? WHERE id=?""",
                    (json.dumps({"code": "legacy_incompatible", "message": str(error)},
                                ensure_ascii=False, separators=(",", ":")),
                     timestamp, timestamp, row["id"]),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _repair_unreleased_v6(connection: sqlite3.Connection, path: Path) -> None:
    """Repair only the exact locally exercised v6 shape from before video slots.

    Stage 5 was browser-tested before shot-owned video acceptance was finalized.
    The repair is intentionally narrower than a migration: every published v6
    table must otherwise match exactly, so an unrelated damaged database is
    rejected before the first write.
    """
    from .schema_shape import expected_tables

    expected = expected_tables(6)
    known_missing = {"media_versions": {"accepted_shot_revision"}}
    actual_missing: dict[str, set[str]] = {}
    problems: list[str] = []
    for table, required in expected.items():
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            problems.append(f"missing table {table}")
            continue
        actual = _table_columns(connection, table)
        missing, extra = required - actual, actual - required
        if missing:
            actual_missing[table] = missing
        if extra:
            problems.append(f"table {table} has unexpected columns: {sorted(extra)}")
    if not actual_missing and not problems:
        return
    if problems or actual_missing != known_missing:
        detail = problems or [
            f"table {table} missing columns: {sorted(columns)}"
            for table, columns in actual_missing.items()
        ]
        raise SchemaError("database schema is incompatible with this version: " + "; ".join(detail))

    _backup_existing_database(connection, path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TRIGGER IF EXISTS media_candidate_accept_transition")
        connection.execute(
            "ALTER TABLE media_versions ADD COLUMN accepted_shot_revision INTEGER"
        )
        # The prerelease implementation registered accepted videos as Asset REF
        # versions. Restore each affected asset to its nearest non-video ancestor
        # before detaching the video candidate from that obsolete AssetVersion.
        affected = connection.execute(
            """SELECT DISTINCT av.asset_id FROM media_versions m
            JOIN asset_versions av ON av.id=m.accepted_asset_version_id
            JOIN assets a ON a.id=av.asset_id AND a.current_version_id=av.id
            WHERE m.kind='video' AND m.owner_type='shot'
            AND m.candidate_status='accepted'"""
        ).fetchall()
        for (asset_id,) in affected:
            current = connection.execute(
                "SELECT current_version_id FROM assets WHERE id=?", (asset_id,)
            ).fetchone()[0]
            replacement = current
            while replacement:
                row = connection.execute(
                    """SELECT av.supersedes_id,m.kind FROM asset_versions av
                    LEFT JOIN media_versions m ON m.accepted_asset_version_id=av.id
                    WHERE av.id=? AND av.asset_id=?""",
                    (replacement, asset_id),
                ).fetchone()
                if not row:
                    replacement = None
                    break
                if row[1] != "video":
                    break
                replacement = row[0]
            if replacement != current:
                connection.execute(
                    "UPDATE assets SET current_version_id=?,revision=revision+1 WHERE id=?",
                    (replacement, asset_id),
                )
                if replacement:
                    connection.execute(
                        "UPDATE media_versions SET is_current=1 WHERE accepted_asset_version_id=?",
                        (replacement,),
                    )
        connection.execute(
            """UPDATE media_versions SET accepted_shot_revision=(
              SELECT revision FROM shots WHERE shots.id=media_versions.owner_id
            ),accepted_asset_version_id=NULL
            WHERE kind='video' AND owner_type='shot' AND candidate_status='accepted'"""
        )
        connection.execute(
            """CREATE TRIGGER media_candidate_accept_transition
            BEFORE UPDATE OF candidate_status,accepted_asset_version_id,accepted_shot_revision,accepted_at
            ON media_versions
            WHEN NOT (
              OLD.candidate_status='candidate' AND NEW.candidate_status='accepted'
              AND OLD.accepted_asset_version_id IS NULL AND OLD.accepted_shot_revision IS NULL
              AND NEW.accepted_at IS NOT NULL AND NEW.is_current=1
              AND (
                (OLD.kind='image' AND NEW.accepted_asset_version_id IS NOT NULL
                  AND NEW.accepted_shot_revision IS NULL)
                OR
                (OLD.kind='video' AND OLD.owner_type='shot'
                  AND NEW.accepted_asset_version_id IS NULL AND NEW.accepted_shot_revision IS NOT NULL)
              )
            )
            BEGIN SELECT RAISE(ABORT, 'invalid media candidate status transition'); END"""
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


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
