"""Validated, fingerprinted, atomic ChangeSet proposal boundary."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ValidationError

from script_weaver.domain.models import (
    AssetCreate,
    AssetVersionPayload,
    ConflictError,
    NotFoundError,
    ProjectMismatchError,
    PromptVersionPayload,
    ReferenceBindPayload,
    ReferenceRebindPayload,
    ReferenceSetModePayload,
    SegmentCreate,
    ShotCreate,
    ShotRetirePayload,
    ShotReorderPayload,
    ShotUpdateFields,
)
from script_weaver.domain.operations import ALLOWED_OPERATIONS
from script_weaver.infrastructure.sqlite import Database

from .dependency_service import DependencyService
from .workbench_service import WorkbenchService, dump, now, require_same_project, row_dict, uid


def fingerprint(value: Any) -> str:
    return hashlib.sha256(dump(value).encode()).hexdigest()


# Tables that can appear in a task snapshot's expected_revisions, keyed by id.
_ENTITY_TABLES = ("projects", "episodes", "segments", "shots", "assets")

_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "segment.create": SegmentCreate,
    "shot.create": ShotCreate,
    "shot.update": ShotUpdateFields,
    "shot.retire": ShotRetirePayload,
    "shot.reorder": ShotReorderPayload,
    "asset.create": AssetCreate,
    "asset.version.create": AssetVersionPayload,
    "reference.bind": ReferenceBindPayload,
    "reference.rebind": ReferenceRebindPayload,
    "reference.set_mode": ReferenceSetModePayload,
    "prompt.version.create": PromptVersionPayload,
}


def _parse_payload(op: dict[str, Any]) -> Any:
    """Parse the stored payload with the same typed model used to execute it."""
    model = _PAYLOAD_MODELS.get(op["op"])
    if model is None:
        raise ValueError(f"unsupported operation: {op['op']}")
    try:
        return model.model_validate(op["payload"])
    except ValidationError as error:
        raise ValueError(f"invalid payload for {op['op']}: {error}") from error


class ChangeSetService:
    def __init__(self, database: Database):
        self.db = database

    def create(self, task_id: str, run_id: str | None = None, summary: str = "") -> dict[str, Any]:
        entity_id = uid()
        with self.db.write() as conn:
            task = conn.execute("SELECT project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise NotFoundError("task not found")
            if run_id is not None and not conn.execute("SELECT 1 FROM agent_runs WHERE id=?", (run_id,)).fetchone():
                raise NotFoundError("agent run not found")
            snapshot = conn.execute("SELECT expected_revisions_json FROM task_snapshots WHERE task_id=?", (task_id,)).fetchone()
            if not snapshot:
                raise NotFoundError("task snapshot not found")
            timestamp = now()
            conn.execute("INSERT INTO changesets VALUES(?,?,?,?,'DRAFT',?,?,NULL,?,?)", (entity_id, task_id, run_id, task[0], snapshot[0], summary, timestamp, timestamp))
        return self.get(entity_id)

    def append(self, changeset_id: str, operation) -> dict[str, Any]:
        if operation.op not in ALLOWED_OPERATIONS:
            raise ValueError("operation not allowed")
        raw = operation.model_dump(mode="json")
        raw["payload"] = operation.payload.model_dump(mode="json", exclude_unset=True)
        if operation.op.endswith(".create") and not raw.get("target_id"):
            raw["target_id"] = uid()
        with self.db.write() as conn:
            changeset = self._get_locked(conn, changeset_id)
            if changeset["status"] != "DRAFT":
                raise ConflictError({changeset_id: 0}, f"changeset is {changeset['status']}; only DRAFT accepts operations")
            ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM changeset_operations WHERE changeset_id=?", (changeset_id,)).fetchone()[0]
            conn.execute("INSERT INTO changeset_operations VALUES(?,?,?,?,?,?,?)", (changeset_id, ordinal, raw["op"], raw["target_type"], raw.get("target_id"), raw.get("expected_revision"), dump(raw["payload"])))
            conn.execute("UPDATE changesets SET validated_fingerprint=NULL,updated_at=? WHERE id=?", (now(), changeset_id))
        return self.get(changeset_id)

    def get(self, changeset_id: str) -> dict[str, Any]:
        row = self.db.connection.execute("SELECT * FROM changesets WHERE id=?", (changeset_id,)).fetchone()
        if not row:
            raise NotFoundError("changeset not found")
        return self._assemble(row, self.db.connection)

    @staticmethod
    def _assemble(row, conn) -> dict[str, Any]:
        result = row_dict(row)
        result["operations"] = [row_dict(r) for r in conn.execute("SELECT * FROM changeset_operations WHERE changeset_id=? ORDER BY ordinal", (result["id"],))]
        result["impacts"] = [row_dict(r) for r in conn.execute("SELECT * FROM changeset_impacts WHERE changeset_id=?", (result["id"],))]
        result["warnings"] = [row_dict(r) for r in conn.execute("SELECT * FROM changeset_warnings WHERE changeset_id=?", (result["id"],))]
        return result

    def _get_locked(self, conn, changeset_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM changesets WHERE id=?", (changeset_id,)).fetchone()
        if not row:
            raise NotFoundError("changeset not found")
        return self._assemble(row, conn)

    def list(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        ids = self.db.connection.execute("SELECT id FROM changesets WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, min(limit, 100)))
        return [self.get(r[0]) for r in ids]

    @staticmethod
    def _current_revision(conn, target_type: str, target_id: str | None) -> int | None:
        tables = {"project": "projects", "episode": "episodes", "segment": "segments", "shot": "shots", "asset": "assets"}
        table = tables.get(target_type)
        if not table or not target_id:
            return None
        row = conn.execute(f"SELECT revision FROM {table} WHERE id=?", (target_id,)).fetchone()
        return row[0] if row else None

    @classmethod
    def _entity_revision(cls, conn, entity_id: str) -> int | None:
        for table in _ENTITY_TABLES:
            row = conn.execute(f"SELECT revision FROM {table} WHERE id=?", (entity_id,)).fetchone()
            if row:
                return row[0]
        return None

    @staticmethod
    def _material(changeset: dict[str, Any]) -> list[dict[str, Any]]:
        return [{k: op[k] for k in ("ordinal", "op", "target_type", "target_id", "expected_revision", "payload")} for op in changeset["operations"]]

    def _validate_locked(self, conn, changeset: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        """Payload semantics, containment and revision checks; raises before any write is proposed as valid."""
        conflicts: dict[str, int] = {}
        impacts, warnings = [], []
        for op in changeset["operations"]:
            self._check_operation(conn, changeset, op)
            if op["expected_revision"] is not None:
                current = self._current_revision(conn, op["target_type"], op["target_id"])
                if current != op["expected_revision"]:
                    conflicts[op["target_id"]] = -1 if current is None else current
            impacts.append({"entity_type": op["target_type"], "entity_id": op["target_id"] or changeset["project_id"], "impact_type": op["op"], "severity": "warning" if op["op"] in {"shot.retire", "asset.version.create"} else "info", "detail": {"ordinal": op["ordinal"]}})
            if op["op"] == "shot.retire":
                warnings.append({"code": "SHOT_RETIRED", "severity": "warning", "message": "镜头将退出当前分镜，但历史保留", "detail": {"shot_id": op["target_id"]}})
        if conflicts:
            raise ConflictError(conflicts)
        validated = fingerprint({"base_revisions": changeset["base_revisions"], "operations": self._material(changeset)})
        return impacts, warnings, validated

    def _check_operation(self, conn, changeset: dict[str, Any], op: dict[str, Any]) -> None:
        """Typed payload parse plus project containment for every target and payload id."""
        name = op["op"]
        project_id = changeset["project_id"]
        parsed = _parse_payload(op)
        if name == "segment.create":
            if not parsed.episode_id:
                raise ValueError("segment.create requires payload.episode_id")
            require_same_project(conn, project_id, "episode", parsed.episode_id)
        elif name == "shot.create":
            if not parsed.segment_id:
                raise ValueError("shot.create requires payload.segment_id")
            require_same_project(conn, project_id, "segment", parsed.segment_id)
        elif name in {"shot.update", "shot.retire"}:
            require_same_project(conn, project_id, "shot", op["target_id"])
            if name == "shot.update":
                WorkbenchService.shot_update_values(parsed)
        elif name == "shot.reorder":
            require_same_project(conn, project_id, "segment", op["target_id"])
            for item in parsed.shots:
                row = conn.execute("SELECT segment_id FROM shots WHERE id=?", (item.id,)).fetchone()
                if not row:
                    raise NotFoundError(f"shot not found: {item.id}")
                if row[0] != op["target_id"]:
                    raise ProjectMismatchError(f"shot {item.id} does not belong to segment {op['target_id']}")
        elif name == "asset.create":
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise NotFoundError(f"project not found: {project_id}")
        elif name == "asset.version.create":
            require_same_project(conn, project_id, "asset", op["target_id"])
        elif name == "reference.bind":
            require_same_project(conn, project_id, "shot", op["target_id"])
            row = conn.execute("SELECT v.asset_id, a.current_version_id FROM asset_versions v JOIN assets a ON a.id=v.asset_id WHERE v.id=?", (parsed.asset_version_id,)).fetchone()
            if not row:
                raise NotFoundError(f"asset version not found: {parsed.asset_version_id}")
            require_same_project(conn, project_id, "asset_version", parsed.asset_version_id)
            if parsed.binding_mode == "follow_latest" and parsed.asset_version_id != row["current_version_id"]:
                raise ValueError("follow_latest binding requires the asset's current version")
            if conn.execute("SELECT 1 FROM reference_bindings WHERE shot_id=? AND asset_id=? AND usage=?", (op["target_id"], row["asset_id"], parsed.usage)).fetchone():
                raise ValueError(f"shot already binds asset for usage {parsed.usage}")
        elif name in {"reference.rebind", "reference.set_mode"}:
            require_same_project(conn, project_id, "shot", op["target_id"])
            binding = conn.execute("SELECT id,asset_id FROM reference_bindings WHERE shot_id=? AND usage=?", (op["target_id"], parsed.usage)).fetchone()
            if not binding:
                raise NotFoundError(f"binding for usage {parsed.usage} not found on shot {op['target_id']}")
            if name == "reference.rebind":
                version_asset = conn.execute("SELECT asset_id FROM asset_versions WHERE id=?", (parsed.asset_version_id,)).fetchone()
                if not version_asset:
                    raise NotFoundError(f"asset version not found: {parsed.asset_version_id}")
                if parsed.asset_id and parsed.asset_id != version_asset[0]:
                    raise ValueError("asset_id does not match asset_version_id")
                if not parsed.asset_id and version_asset[0] != binding["asset_id"]:
                    raise ValueError("rebind may only switch versions of the same asset unless asset_id is provided")
                require_same_project(conn, project_id, "asset_version", parsed.asset_version_id)
        elif name == "prompt.version.create":
            require_same_project(conn, project_id, op["target_type"], op["target_id"])

    def validate(self, changeset_id: str) -> dict[str, Any]:
        with self.db.write() as conn:
            changeset = self._get_locked(conn, changeset_id)
            if changeset["status"] not in {"DRAFT", "SUBMITTED"}:
                raise ConflictError({changeset_id: 0}, f"changeset is {changeset['status']}")
            impacts, warnings, validated = self._validate_locked(conn, changeset)
            conn.execute("DELETE FROM changeset_impacts WHERE changeset_id=?", (changeset_id,))
            conn.execute("DELETE FROM changeset_warnings WHERE changeset_id=?", (changeset_id,))
            conn.executemany("INSERT INTO changeset_impacts VALUES(?,?,?,?,?,?)", [(changeset_id, i["entity_type"], i["entity_id"], i["impact_type"], i["severity"], dump(i["detail"])) for i in impacts])
            conn.executemany("INSERT INTO changeset_warnings VALUES(?,?,?,?,?)", [(changeset_id, w["code"], w["severity"], w["message"], dump(w["detail"])) for w in warnings])
            conn.execute("UPDATE changesets SET validated_fingerprint=?,updated_at=? WHERE id=?", (validated, now(), changeset_id))
        return self.get(changeset_id)

    def submit(self, changeset_id: str) -> dict[str, Any]:
        with self.db.write() as conn:
            changeset = self._get_locked(conn, changeset_id)
            if changeset["status"] not in {"DRAFT", "SUBMITTED"}:
                raise ConflictError({changeset_id: 0}, f"changeset is {changeset['status']}")
            if changeset["status"] == "DRAFT":
                impacts, warnings, validated = self._validate_locked(conn, changeset)
                conn.execute("DELETE FROM changeset_impacts WHERE changeset_id=?", (changeset_id,))
                conn.execute("DELETE FROM changeset_warnings WHERE changeset_id=?", (changeset_id,))
                conn.executemany("INSERT INTO changeset_impacts VALUES(?,?,?,?,?,?)", [(changeset_id, i["entity_type"], i["entity_id"], i["impact_type"], i["severity"], dump(i["detail"])) for i in impacts])
                conn.executemany("INSERT INTO changeset_warnings VALUES(?,?,?,?,?)", [(changeset_id, w["code"], w["severity"], w["message"], dump(w["detail"])) for w in warnings])
                conn.execute("UPDATE changesets SET status='SUBMITTED',validated_fingerprint=?,updated_at=? WHERE id=?", (validated, now(), changeset_id))
        return self.get(changeset_id)

    def apply(self, changeset_id: str, expected_fingerprint: str) -> dict[str, Any]:
        conflicts: dict[str, int] | None = None
        with self.db.write() as conn:
            changeset = self._get_locked(conn, changeset_id)
            status = changeset["status"]
            if status == "APPLIED":
                # Idempotent replay still must present the fingerprint that was applied.
                if changeset["validated_fingerprint"] != expected_fingerprint:
                    raise ConflictError({changeset_id: 0}, "validated fingerprint mismatch")
            else:
                if status != "SUBMITTED":
                    raise ConflictError({changeset_id: 0}, f"changeset is {status}, not SUBMITTED")
                if changeset["validated_fingerprint"] != expected_fingerprint:
                    raise ConflictError({changeset_id: 0}, "validated fingerprint mismatch")
                if fingerprint({"base_revisions": changeset["base_revisions"], "operations": self._material(changeset)}) != expected_fingerprint:
                    raise ConflictError({changeset_id: 0}, "operations changed since validation")
                conflicts = self._collect_conflicts(conn, changeset)
                if conflicts:
                    conn.execute("UPDATE changesets SET status='CONFLICTED',updated_at=? WHERE id=?", (now(), changeset_id))
                else:
                    touched: set[tuple[str, str]] = set()
                    for op in changeset["operations"]:
                        self._apply_operation(conn, changeset, op, touched)
                    for table, entity_id in touched:
                        if table == "shots":
                            WorkbenchService._snapshot_shot(conn, entity_id, changeset_id)
                    conn.execute("UPDATE changesets SET status='APPLIED',updated_at=? WHERE id=?", (now(), changeset_id))
        if conflicts:
            raise ConflictError(conflicts)
        return self.get(changeset_id)

    def _collect_conflicts(self, conn, changeset: dict[str, Any]) -> dict[str, int]:
        conflicts: dict[str, int] = {}
        for entity_id, revision in changeset["base_revisions"].items():
            current = self._entity_revision(conn, entity_id)
            if current != revision:
                conflicts[entity_id] = -1 if current is None else current
        for op in changeset["operations"]:
            if op["expected_revision"] is not None:
                current = self._current_revision(conn, op["target_type"], op["target_id"])
                if current != op["expected_revision"]:
                    conflicts[op["target_id"]] = -1 if current is None else current
        return conflicts

    def reject(self, changeset_id: str) -> dict[str, Any]:
        with self.db.write() as conn:
            row = conn.execute("SELECT status FROM changesets WHERE id=?", (changeset_id,)).fetchone()
            if not row:
                raise NotFoundError("changeset not found")
            status = row[0]
            if status not in {"REJECTED", "DRAFT", "SUBMITTED"}:
                raise ConflictError({changeset_id: 0}, f"changeset already {status}; cannot reject")
            if status != "REJECTED":
                conn.execute("UPDATE changesets SET status='REJECTED',updated_at=? WHERE id=? AND status=?", (now(), changeset_id, status))
        return self.get(changeset_id)

    def _bump(self, conn, table: str, entity_id: str, touched: set[tuple[str, str]]) -> int:
        key = (table, entity_id)
        if key not in touched:
            cursor = conn.execute(f"UPDATE {table} SET revision=revision+1 WHERE id=?", (entity_id,))
            if cursor.rowcount != 1:
                raise NotFoundError(f"{table} row not found: {entity_id}")
            touched.add(key)
        return conn.execute(f"SELECT revision FROM {table} WHERE id=?", (entity_id,)).fetchone()[0]

    def _apply_operation(self, conn, changeset: dict[str, Any], op: dict[str, Any], touched: set[tuple[str, str]]) -> None:
        name, target_id = op["op"], op["target_id"]
        project_id = changeset["project_id"]
        self._check_operation(conn, changeset, op)
        parsed = _parse_payload(op)
        if name == "segment.create":
            conn.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,0)", (target_id, parsed.episode_id, parsed.code, parsed.order_index, parsed.title, dump(parsed.source_scene_ids), parsed.target_duration_seconds))
            aggregate_type, aggregate_id, revision = "segment", target_id, 0
        elif name == "shot.create":
            WorkbenchService._insert_shot(conn, target_id, parsed.segment_id, parsed, changeset["id"])
            aggregate_type, aggregate_id, revision = "shot", target_id, 0
        elif name == "shot.update":
            values = WorkbenchService.shot_update_values(parsed)
            conn.execute(f"UPDATE shots SET {','.join(f'{key}=?' for key in values)} WHERE id=?", (*values.values(), target_id))
            aggregate_type, aggregate_id = "shot", target_id
            revision = self._bump(conn, "shots", target_id, touched)
        elif name == "shot.retire":
            conn.execute("UPDATE shots SET status='retired' WHERE id=?", (target_id,))
            aggregate_type, aggregate_id = "shot", target_id
            revision = self._bump(conn, "shots", target_id, touched)
        elif name == "shot.reorder":
            for item in parsed.shots:
                conn.execute("UPDATE shots SET order_index=? WHERE id=? AND segment_id=?", (item.order_index, item.id, target_id))
                self._bump(conn, "shots", item.id, touched)
            aggregate_type, aggregate_id = "segment", target_id
            revision = self._bump(conn, "segments", target_id, touched)
        elif name == "asset.create":
            version_id = uid()
            conn.execute("INSERT INTO assets VALUES(?,?,?,?,?,0)", (target_id, project_id, parsed.kind, parsed.name, version_id))
            conn.execute("INSERT INTO asset_versions VALUES(?,?,1,?,NULL,?)", (version_id, target_id, dump(parsed.content), now()))
            aggregate_type, aggregate_id, revision = "asset", target_id, 0
        elif name == "asset.version.create":
            current = conn.execute("SELECT current_version_id FROM assets WHERE id=?", (target_id,)).fetchone()[0]
            number = conn.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM asset_versions WHERE asset_id=?", (target_id,)).fetchone()[0]
            version_id = uid()
            conn.execute("INSERT INTO asset_versions VALUES(?,?,?,?,?,?)", (version_id, target_id, number, dump(parsed.content), current, now()))
            conn.execute("UPDATE assets SET current_version_id=? WHERE id=?", (version_id, target_id))
            DependencyService.mark_follow_latest_stale(conn, target_id, version_id)
            aggregate_type, aggregate_id = "asset", target_id
            revision = self._bump(conn, "assets", target_id, touched)
        elif name == "reference.bind":
            asset_id = conn.execute("SELECT asset_id FROM asset_versions WHERE id=?", (parsed.asset_version_id,)).fetchone()[0]
            conn.execute("INSERT INTO reference_bindings VALUES(?,?,?,?,?,?,0,NULL)", (uid(), target_id, asset_id, parsed.asset_version_id, parsed.usage, parsed.binding_mode))
            aggregate_type, aggregate_id = "shot", target_id
            revision = self._bump(conn, "shots", target_id, touched)
        elif name in {"reference.rebind", "reference.set_mode"}:
            binding = conn.execute("SELECT id,asset_version_id,binding_mode FROM reference_bindings WHERE shot_id=? AND usage=?", (target_id, parsed.usage)).fetchone()
            if name == "reference.rebind":
                new_version = parsed.asset_version_id
                new_mode = binding["binding_mode"]
            else:
                new_version = binding["asset_version_id"]
                new_mode = parsed.binding_mode
            if (new_version, new_mode) == (binding["asset_version_id"], binding["binding_mode"]):
                aggregate_type, aggregate_id = "shot", target_id
                revision = self._current_revision(conn, "shot", target_id) or 0
            else:
                conn.execute("UPDATE reference_bindings SET asset_version_id=?,binding_mode=?,is_stale=0,stale_reason=NULL WHERE id=?", (new_version, new_mode, binding["id"]))
                aggregate_type, aggregate_id = "shot", target_id
                revision = self._bump(conn, "shots", target_id, touched)
        elif name == "prompt.version.create":
            conn.execute("UPDATE prompt_versions SET is_current=0 WHERE owner_type=? AND owner_id=? AND kind=?", (op["target_type"], target_id, parsed.kind))
            number = conn.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM prompt_versions WHERE owner_type=? AND owner_id=? AND kind=?", (op["target_type"], target_id, parsed.kind)).fetchone()[0]
            conn.execute("INSERT INTO prompt_versions VALUES(?,?,?,?,?,?,?,1,?,?)", (uid(), op["target_type"], target_id, parsed.kind, number, parsed.content, parsed.negative_content, changeset["id"], now()))
            aggregate_type, aggregate_id = op["target_type"], target_id
            table = {"shot": "shots", "segment": "segments", "asset": "assets"}[op["target_type"]]
            revision = self._bump(conn, table, target_id, touched)
        else:
            raise ValueError(f"unsupported operation: {name}")
        WorkbenchService._event(conn, project_id, aggregate_type, aggregate_id, revision, name, op["payload"], changeset["id"], changeset["task_id"])


__all__ = ["ChangeSetService", "fingerprint"]
