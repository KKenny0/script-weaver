"""Validated, fingerprinted, atomic ChangeSet proposal boundary."""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ValidationError

from script_weaver.domain.models import (
    AssetCreate,
    AssetVersionPayload,
    ConflictError,
    DocumentCreatePayload,
    DocumentVersionCreatePayload,
    NotFoundError,
    ProjectMismatchError,
    PromptVersionPayload,
    ProposalSubmit,
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
from .workbench_service import (
    WorkbenchService,
    dump,
    now,
    require_same_project,
    row_dict,
    skill_tree_hash,
    uid,
)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(dump(value).encode()).hexdigest()


# Tables that can appear in a task snapshot's expected_revisions, keyed by id.
_ENTITY_TABLES = ("projects", "episodes", "segments", "shots", "assets", "creative_documents")

_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "document.create": DocumentCreatePayload,
    "document.version.create": DocumentVersionCreatePayload,
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
        if run_id is None:
            raise ValueError("a running agent run is required")
        entity_id = uid()
        with self.db.write() as conn:
            task = conn.execute("SELECT project_id,status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise NotFoundError("task not found")
            run = conn.execute("SELECT task_id,status FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise NotFoundError("agent run not found")
            if run[0] != task_id:
                raise ProjectMismatchError("agent run belongs to another task")
            if task[1] != "RUNNING" or run[1] != "RUNNING":
                raise ConflictError({task_id: 0}, "task and agent run must both be RUNNING")
            if conn.execute("SELECT 1 FROM changesets WHERE run_id=?", (run_id,)).fetchone():
                raise ConflictError({task_id: 0}, "agent run already has a proposal")
            snapshot = conn.execute("SELECT expected_revisions_json FROM task_snapshots WHERE task_id=?", (task_id,)).fetchone()
            if not snapshot:
                raise NotFoundError("task snapshot not found")
            timestamp = now()
            conn.execute("INSERT INTO changesets VALUES(?,?,?,?,'DRAFT',?,?,NULL,?,?)", (entity_id, task_id, run_id, task[0], snapshot[0], summary, timestamp, timestamp))
        return self.get(entity_id)

    @staticmethod
    def _require_running_owner(conn, changeset: dict[str, Any]) -> None:
        if not changeset.get("run_id"):
            raise ConflictError({changeset["task_id"]: 0}, "changeset has no owning agent run")
        task = conn.execute("SELECT status FROM tasks WHERE id=?", (changeset["task_id"],)).fetchone()
        run = conn.execute(
            "SELECT task_id,status FROM agent_runs WHERE id=?", (changeset["run_id"],)
        ).fetchone()
        if not task or not run:
            raise NotFoundError("changeset task or agent run not found")
        if run[0] != changeset["task_id"]:
            raise ProjectMismatchError("agent run belongs to another task")
        if task[0] != "RUNNING" or run[1] != "RUNNING":
            raise ConflictError(
                {changeset["task_id"]: 0}, "task and agent run must both be RUNNING"
            )

    def append(self, changeset_id: str, operation) -> dict[str, Any]:
        if operation.op not in ALLOWED_OPERATIONS:
            raise ValueError("operation not allowed")
        raw = operation.model_dump(mode="json")
        raw["payload"] = operation.payload.model_dump(mode="json", exclude_unset=True)
        if operation.op.endswith(".create") and not raw.get("target_id"):
            raw["target_id"] = uid()
        with self.db.write() as conn:
            changeset = self._get_locked(conn, changeset_id)
            self._require_running_owner(conn, changeset)
            if changeset["status"] != "DRAFT":
                raise ConflictError({changeset_id: 0}, f"changeset is {changeset['status']}; only DRAFT accepts operations")
            ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM changeset_operations WHERE changeset_id=?", (changeset_id,)).fetchone()[0]
            conn.execute("INSERT INTO changeset_operations VALUES(?,?,?,?,?,?,?)", (changeset_id, ordinal, raw["op"], raw["target_type"], raw.get("target_id"), raw.get("expected_revision"), dump(raw["payload"])))
            conn.execute("UPDATE changesets SET validated_fingerprint=NULL,updated_at=? WHERE id=?", (now(), changeset_id))
        return self.get(changeset_id)

    def submit_proposal(self, task_id: str, proposal: ProposalSubmit) -> dict[str, Any]:
        """Atomically persist a typed agent proposal and finish its claimed run."""
        changeset_id, timestamp = uid(), now()
        snapshot = self.db.connection.execute(
            "SELECT skill_manifest_json FROM task_snapshots WHERE task_id=?", (task_id,)
        ).fetchone()
        if not snapshot:
            raise NotFoundError("task snapshot not found")
        manifest = row_dict({"skill_manifest_json": snapshot[0]})["skill_manifest"]
        if len(manifest) != 1:
            raise ValueError("task snapshot must pin exactly one skill")
        current_skill_hash = skill_tree_hash(manifest[0]["name"])
        with self.db.write() as conn:
            task = conn.execute(
                "SELECT project_id,status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task:
                raise NotFoundError("task not found")
            run = conn.execute(
                "SELECT task_id,status FROM agent_runs WHERE id=?", (proposal.run_id,)
            ).fetchone()
            if not run:
                raise NotFoundError("agent run not found")
            if run[0] != task_id:
                raise ProjectMismatchError("agent run belongs to another task")
            if task[1] != "RUNNING" or run[1] != "RUNNING":
                raise ConflictError({task_id: 0}, "task and agent run must both be RUNNING")
            if conn.execute(
                "SELECT 1 FROM changesets WHERE run_id=?", (proposal.run_id,)
            ).fetchone():
                raise ConflictError({task_id: 0}, "agent run already has a proposal")
            pinned = conn.execute(
                "SELECT skill_manifest_json FROM task_snapshots WHERE task_id=?", (task_id,)
            ).fetchone()
            current_manifest = row_dict({"skill_manifest_json": pinned[0]})["skill_manifest"]
            if current_manifest != manifest or current_skill_hash != manifest[0]["hash"]:
                raise ConflictError({task_id: 0}, "pinned skill tree changed before proposal submission")
            snapshot = conn.execute(
                "SELECT expected_revisions_json FROM task_snapshots WHERE task_id=?", (task_id,)
            ).fetchone()
            if not snapshot:
                raise NotFoundError("task snapshot not found")
            conn.execute(
                "INSERT INTO changesets VALUES(?,?,?,?,'DRAFT',?,?,NULL,?,?)",
                (
                    changeset_id, task_id, proposal.run_id, task[0], snapshot[0],
                    proposal.summary, timestamp, timestamp,
                ),
            )
            for ordinal, operation in enumerate(proposal.operations):
                raw = operation.model_dump(mode="json")
                raw["payload"] = operation.payload.model_dump(mode="json", exclude_unset=True)
                if operation.op.endswith(".create") and not raw.get("target_id"):
                    raw["target_id"] = uid()
                conn.execute(
                    "INSERT INTO changeset_operations VALUES(?,?,?,?,?,?,?)",
                    (
                        changeset_id, ordinal, raw["op"], raw["target_type"],
                        raw.get("target_id"), raw.get("expected_revision"), dump(raw["payload"]),
                    ),
                )
            changeset = self._get_locked(conn, changeset_id)
            impacts, warnings, validated = self._validate_locked(conn, changeset)
            self._store_evaluation(conn, changeset_id, impacts, warnings, validated, "SUBMITTED")
            conn.execute(
                "UPDATE agent_runs SET status='SUCCEEDED',finished_at=? WHERE id=?",
                (timestamp, proposal.run_id),
            )
            conn.execute(
                "INSERT INTO agent_run_events VALUES(?,1,'run.succeeded',?,?)",
                (proposal.run_id, dump({"changeset_id": changeset_id}), timestamp),
            )
            conn.execute("UPDATE tasks SET status='SUBMITTED' WHERE id=?", (task_id,))
            WorkbenchService._event(
                conn, task[0], "changeset", changeset_id, 0, "changeset.submitted",
                {"summary": proposal.summary}, changeset_id, task_id,
            )
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
        tables = {"project": "projects", "episode": "episodes", "segment": "segments", "shot": "shots", "asset": "assets", "document": "creative_documents"}
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
        snapshot = conn.execute("SELECT selection_json FROM task_snapshots WHERE task_id=?", (changeset["task_id"],)).fetchone()
        selection = row_dict({"selection_json": snapshot[0]})["selection"] if snapshot else {}
        scope = selection.get("edit_scope")
        if scope:
            WorkbenchService.require_scoped_draft_current(conn, selection)
            if len(changeset["operations"]) != 1 or name != "document.version.create" or op["target_id"] != selection["document_id"]:
                raise ValueError("局部修订只能为当前剧本提交一个候选，不能修改其他对象")
            original = selection["target"]["draft_content"]
            prefix, suffix = original[:scope["start"]], original[scope["end"]:]
            if len(parsed.content) < len(prefix) + len(suffix) or not parsed.content.startswith(prefix) or not parsed.content.endswith(suffix):
                raise ValueError("候选修改了选区以外的文字，请仅修改指定范围后重新提交")
        if name == "document.create":
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise NotFoundError(f"project not found: {project_id}")
            if parsed.episode_id:
                require_same_project(conn, project_id, "episode", parsed.episode_id)
            snapshot = conn.execute(
                "SELECT selection_json FROM task_snapshots WHERE task_id=?",
                (changeset["task_id"],),
            ).fetchone()
            selection = row_dict({"selection_json": snapshot[0]})["selection"] if snapshot else {}
            if selection.get("episode_id") and parsed.episode_id != selection["episode_id"]:
                raise ProjectMismatchError("document episode is outside the task snapshot")
            if parsed.episode_id:
                episode = conn.execute(
                    "SELECT source_document_version_id,status FROM episodes WHERE id=?",
                    (parsed.episode_id,),
                ).fetchone()
                if episode[1] != "active":
                    raise ConflictError({parsed.episode_id: 0}, "episode is removed")
                if selection.get("document_version_id") != episode[0]:
                    raise ProjectMismatchError("document source is outside the current episode lineage")
        elif name == "document.version.create":
            require_same_project(conn, project_id, "document", op["target_id"])
            snapshot = conn.execute(
                "SELECT selection_json FROM task_snapshots WHERE task_id=?",
                (changeset["task_id"],),
            ).fetchone()
            selection = row_dict({"selection_json": snapshot[0]})["selection"] if snapshot else {}
            if selection.get("document_id") != op["target_id"]:
                raise ProjectMismatchError("document version target is outside the task snapshot")
            document = conn.execute(
                "SELECT kind,episode_id FROM creative_documents WHERE id=?", (op["target_id"],)
            ).fetchone()
            kind = document[0]
            if kind == "source":
                raise ValueError("agents may not create versions of source documents")
            if kind in {"screenplay", "review"} and document[1]:
                episode = conn.execute(
                    "SELECT source_document_version_id,status FROM episodes WHERE id=?",
                    (document[1],),
                ).fetchone()
                if episode[1] != "active":
                    raise ConflictError({document[1]: 0}, "episode is removed")
                if selection.get("document_version_id") != episode[0]:
                    raise ProjectMismatchError("candidate source is outside the current episode lineage")
            if conn.execute(
                "SELECT 1 FROM creative_document_versions WHERE document_id=? AND status='SUBMITTED'",
                (op["target_id"],),
            ).fetchone():
                raise ConflictError(
                    {op["target_id"]: self._current_revision(conn, "document", op["target_id"]) or 0},
                    "document already has a submitted candidate",
                )
        elif name == "segment.create":
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
            self._require_running_owner(conn, changeset)
            if changeset["status"] != "DRAFT":
                raise ConflictError({changeset_id: 0}, f"changeset is {changeset['status']}")
            impacts, warnings, validated = self._validate_locked(conn, changeset)
            self._store_evaluation(conn, changeset_id, impacts, warnings, validated)
        return self.get(changeset_id)

    def submit(self, changeset_id: str) -> dict[str, Any]:
        with self.db.write() as conn:
            changeset = self._get_locked(conn, changeset_id)
            self._require_running_owner(conn, changeset)
            if changeset["status"] != "DRAFT":
                raise ConflictError({changeset_id: 0}, f"changeset is {changeset['status']}")
            impacts, warnings, validated = self._validate_locked(conn, changeset)
            self._store_evaluation(
                conn, changeset_id, impacts, warnings, validated, "SUBMITTED"
            )
            timestamp = now()
            conn.execute(
                "UPDATE agent_runs SET status='SUCCEEDED',finished_at=? WHERE id=?",
                (timestamp, changeset["run_id"]),
            )
            conn.execute(
                "INSERT INTO agent_run_events VALUES(?,1,'run.succeeded',?,?)",
                (changeset["run_id"], dump({"changeset_id": changeset_id}), timestamp),
            )
            conn.execute("UPDATE tasks SET status='SUBMITTED' WHERE id=?", (changeset["task_id"],))
        return self.get(changeset_id)

    @staticmethod
    def _store_evaluation(conn, changeset_id, impacts, warnings, validated, status=None) -> None:
        conn.execute("DELETE FROM changeset_impacts WHERE changeset_id=?", (changeset_id,))
        conn.execute("DELETE FROM changeset_warnings WHERE changeset_id=?", (changeset_id,))
        conn.executemany(
            "INSERT INTO changeset_impacts VALUES(?,?,?,?,?,?)",
            [(changeset_id, i["entity_type"], i["entity_id"], i["impact_type"], i["severity"], dump(i["detail"])) for i in impacts],
        )
        conn.executemany(
            "INSERT INTO changeset_warnings VALUES(?,?,?,?,?)",
            [(changeset_id, w["code"], w["severity"], w["message"], dump(w["detail"])) for w in warnings],
        )
        clause = ",status=?" if status else ""
        values = (validated, now(), status, changeset_id) if status else (validated, now(), changeset_id)
        conn.execute(
            f"UPDATE changesets SET validated_fingerprint=?,updated_at=?{clause} WHERE id=?",
            values,
        )

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
                    conn.execute("UPDATE tasks SET status='APPLIED' WHERE id=?", (changeset["task_id"],))
                    WorkbenchService._event(
                        conn, changeset["project_id"], "changeset", changeset_id, 0,
                        "changeset.applied", {}, changeset_id, changeset["task_id"],
                    )
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
                task = conn.execute(
                    "SELECT task_id,project_id FROM changesets WHERE id=?", (changeset_id,)
                ).fetchone()
                conn.execute("UPDATE tasks SET status='REJECTED' WHERE id=?", (task[0],))
                WorkbenchService._event(
                    conn, task[1], "changeset", changeset_id, 0, "changeset.rejected", {},
                    changeset_id, task[0],
                )
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
        if name == "document.create":
            version_id, timestamp = uid(), now()
            projection = "not_projected" if parsed.kind == "screenplay" else "not_applicable"
            selection = row_dict(conn.execute(
                "SELECT selection_json FROM task_snapshots WHERE task_id=?",
                (changeset["task_id"],),
            ).fetchone())["selection"]
            source_version_id = selection.get("document_version_id")
            conn.execute(
                """INSERT INTO creative_documents(
                id,project_id,episode_id,kind,title,current_version_id,revision,created_at,updated_at,
                source_document_version_id,derived_from_ids_json
                ) VALUES(?,?,?,?,?,NULL,0,?,?,?,?)""",
                (target_id, project_id, parsed.episode_id, parsed.kind, parsed.title, timestamp, timestamp,
                 source_version_id, dump([source_version_id] if source_version_id else [])),
            )
            conn.execute(
                """INSERT INTO creative_document_versions(
                id,document_id,version_number,content,status,source_snapshot_id,
                decision_feedback,projection_status,created_at,decided_at,source_changeset_id,created_by,
                source_document_version_id,derived_from_ids_json
                ) VALUES(?,?,1,?,'SUBMITTED',NULL,NULL,?,?,NULL,?,'agent',?,?)""",
                (version_id, target_id, parsed.content, projection, timestamp, changeset["id"],
                 source_version_id, dump([source_version_id] if source_version_id else [])),
            )
            conn.execute(
                "INSERT INTO document_drafts VALUES(?,?,0,NULL,?)",
                (target_id, parsed.content, timestamp),
            )
            aggregate_type, aggregate_id, revision = "document", target_id, 0
        elif name == "document.version.create":
            document = conn.execute(
                "SELECT kind,revision FROM creative_documents WHERE id=?", (target_id,)
            ).fetchone()
            number = conn.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM creative_document_versions WHERE document_id=?",
                (target_id,),
            ).fetchone()[0]
            version_id, timestamp = uid(), now()
            projection = "not_projected" if document[0] == "screenplay" else "not_applicable"
            selection = row_dict(conn.execute(
                "SELECT selection_json FROM task_snapshots WHERE task_id=?",
                (changeset["task_id"],),
            ).fetchone())["selection"]
            source_version_id = selection.get("document_version_id")
            conn.execute(
                """INSERT INTO creative_document_versions(
                id,document_id,version_number,content,status,source_snapshot_id,
                decision_feedback,projection_status,created_at,decided_at,source_changeset_id,created_by,
                source_document_version_id,derived_from_ids_json
                ) VALUES(?,?,?,?,'SUBMITTED',NULL,NULL,?,?,NULL,?,'agent',?,?)""",
                (version_id, target_id, number, parsed.content, projection, timestamp, changeset["id"],
                 source_version_id, dump([source_version_id] if source_version_id else [])),
            )
            conn.execute(
                "UPDATE creative_documents SET revision=revision+1,updated_at=? WHERE id=?",
                (timestamp, target_id),
            )
            aggregate_type, aggregate_id, revision = "document", target_id, document[1] + 1
        elif name == "segment.create":
            conn.execute("INSERT INTO segments(id,episode_id,code,order_index,title,source_scene_ids_json,target_duration_seconds,revision) VALUES(?,?,?,?,?,?,?,0)", (target_id, parsed.episode_id, parsed.code, parsed.order_index, parsed.title, dump(parsed.source_scene_ids), parsed.target_duration_seconds))
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
            conn.execute("""INSERT INTO reference_bindings(
                id,shot_id,asset_id,asset_version_id,usage,binding_mode,is_stale,stale_reason,
                source_document_version_id,source_projection_revision,derived_from_ids_json
                ) SELECT ?,?,?,?,?,?,0,NULL,source_document_version_id,
                source_projection_revision,derived_from_ids_json FROM shots WHERE id=?""",
                (uid(), target_id, asset_id, parsed.asset_version_id, parsed.usage, parsed.binding_mode, target_id),
            )
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
            if op["target_type"] == "shot":
                lineage = conn.execute(
                    "SELECT source_document_version_id,source_projection_revision,derived_from_ids_json FROM shots WHERE id=?",
                    (target_id,),
                ).fetchone()
            else:
                lineage = (None, None, "[]")
            conn.execute("""INSERT INTO prompt_versions(
                id,owner_type,owner_id,kind,version_number,content,negative_content,is_current,
                source_changeset_id,created_at,source_document_version_id,
                source_projection_revision,derived_from_ids_json
                ) VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?)""", (
                uid(), op["target_type"], target_id, parsed.kind, number, parsed.content,
                parsed.negative_content, changeset["id"], now(), lineage[0], lineage[1], lineage[2],
            ))
            aggregate_type, aggregate_id = op["target_type"], target_id
            table = {"shot": "shots", "segment": "segments", "asset": "assets"}[op["target_type"]]
            revision = self._bump(conn, table, target_id, touched)
        else:
            raise ValueError(f"unsupported operation: {name}")
        event_payload = {"operation": name}
        if name.startswith("document."):
            event_payload.update({
                "document_id": aggregate_id,
                "version_id": version_id,
                "content_sha256": hashlib.sha256(parsed.content.encode("utf-8")).hexdigest(),
            })
        WorkbenchService._event(conn, project_id, aggregate_type, aggregate_id, revision, name, event_payload, changeset["id"], changeset["task_id"])


__all__ = ["ChangeSetService", "fingerprint"]
