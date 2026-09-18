"""Prepare/confirm/run boundary for explicitly authorized image generation."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import struct
import zlib
from pathlib import Path
from typing import Any

from script_weaver.domain.models import (
    ConflictError,
    GenerationPrepare,
    MediaCandidateAccept,
    MediaCandidateImport,
    NotFoundError,
    ProjectMismatchError,
)
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database
from script_weaver.integrations.media import FakeImageAdapter, GptImage2Adapter

from .changeset_service import fingerprint
from .dependency_service import DependencyService
from .workbench_service import (
    dump,
    now,
    require_same_project,
    row_dict,
    skill_tree_hash,
    uid,
)


class GenerationJobService:
    def __init__(self, database: Database, media_store: MediaStore):
        self.db, self.media_store = database, media_store
        self.adapters = {"fake": FakeImageAdapter(), "gpt-image-2": GptImage2Adapter()}
        self._recover_interrupted_jobs()

    def _recover_interrupted_jobs(self) -> None:
        """A RUNNING job can only mean the daemon died mid-generation; never auto-retry."""
        with self.db.write() as conn:
            rows = conn.execute(
                "SELECT id FROM generation_jobs WHERE state IN ('RUNNING','SUBMITTING')"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE generation_jobs SET state='FAILED',error_json=?,updated_at=? WHERE id=?",
                    (dump({
                        "code": "daemon_restart_during_generation",
                        "message": "daemon 在生成过程中重启；上游可能已接收请求，系统未自动重试，请重新 prepare 并确认。",
                        "type": "DaemonRestart",
                    }), now(), row[0]),
                )

    def _get(self, job_id: str) -> dict[str, Any]:
        row = self.db.connection.execute("SELECT * FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise NotFoundError("generation job not found")
        return row_dict(row)

    def _compute_input_hashes(self, conn, project_id: str, owner_type: str, owner_id: str, reference_ids: list[str], parent_candidate_id: str | None = None, target_asset_id: str | None = None) -> dict[str, Any]:
        """Owner type/id/revision plus sorted reference content hashes; everything project-owned."""
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise NotFoundError("project not found")
        require_same_project(conn, project_id, owner_type, owner_id)
        owner_table = "shots" if owner_type == "shot" else "assets"
        owner_revision = conn.execute(f"SELECT revision FROM {owner_table} WHERE id=?", (owner_id,)).fetchone()[0]
        input_hashes: dict[str, Any] = {"owner": {"type": owner_type, "id": owner_id, "revision": owner_revision}, "references": {}}
        if owner_type == "shot":
            lineage = conn.execute(
                """SELECT source_document_version_id,source_projection_revision,derived_from_ids_json
                FROM shots WHERE id=?""", (owner_id,),
            ).fetchone()
            input_hashes["owner"]["source_document_version_id"] = lineage[0]
            input_hashes["owner"]["source_projection_revision"] = lineage[1]
            input_hashes["owner"]["derived_from_ids_json"] = lineage[2]
        if target_asset_id:
            require_same_project(conn, project_id, "asset", target_asset_id)
            target_revision = conn.execute(
                "SELECT revision FROM assets WHERE id=?", (target_asset_id,)
            ).fetchone()[0]
            if owner_type == "asset" and owner_id != target_asset_id:
                raise ProjectMismatchError("asset generation target must equal its owner")
            if owner_type == "shot" and not conn.execute(
                "SELECT 1 FROM reference_bindings WHERE shot_id=? AND asset_id=?",
                (owner_id, target_asset_id),
            ).fetchone():
                raise ProjectMismatchError("shot generation target must be an existing bound asset")
            input_hashes["target_asset"] = {"id": target_asset_id, "revision": target_revision}
        for version_id in sorted(set(reference_ids)):
            row = conn.execute("SELECT content_json FROM asset_versions WHERE id=?", (version_id,)).fetchone()
            if not row:
                raise NotFoundError("reference asset version not found")
            require_same_project(conn, project_id, "asset_version", version_id)
            input_hashes["references"][version_id] = hashlib.sha256(row[0].encode()).hexdigest()
        if parent_candidate_id:
            parent = conn.execute(
                "SELECT project_id,owner_type,owner_id,sha256,candidate_status FROM media_versions WHERE id=?",
                (parent_candidate_id,),
            ).fetchone()
            if not parent:
                raise NotFoundError("parent candidate not found")
            if parent[0] != project_id or parent[1] != owner_type or parent[2] != owner_id:
                raise ProjectMismatchError("parent candidate belongs to another project or owner")
            if parent[4] not in {"candidate", "accepted"}:
                raise ValueError("parent media is not an editable candidate")
            input_hashes["parent_candidate"] = {"id": parent_candidate_id, "sha256": parent[3]}
        return input_hashes

    def prepare(self, data: GenerationPrepare) -> dict[str, Any]:
        spec = data.model_dump(mode="json")
        with self.db.write() as conn:
            input_hashes = self._compute_input_hashes(conn, data.project_id, data.owner_type, data.owner_id, data.reference_asset_version_ids, data.parent_candidate_id, data.target_asset_id)
        exact_fingerprint = fingerprint({"spec": spec, "input_hashes": input_hashes})
        job_id, timestamp = uid(), now()
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO generation_jobs(
                id,project_id,owner_type,owner_id,kind,state,spec_json,fingerprint,
                input_hashes_json,adapter,confirmation_token_hash,confirmation_used,
                error_json,created_at,updated_at,parent_candidate_id
                ) VALUES(?,?,?,?,?,'AWAITING_CONFIRMATION',?,?,?,?,NULL,0,NULL,?,?,?)""",
                (job_id, data.project_id, data.owner_type, data.owner_id, "image", dump(spec),
                 exact_fingerprint, dump(input_hashes), data.adapter, timestamp, timestamp,
                 data.parent_candidate_id),
            )
        return self._get(job_id)

    def confirm(self, job_id: str, expected_fingerprint: str) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.db.write() as conn:
            row = conn.execute("SELECT state,fingerprint FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise NotFoundError("generation job not found")
            if row[0] != "AWAITING_CONFIRMATION" or row[1] != expected_fingerprint:
                raise ConflictError({job_id: 0}, "generation job is not awaiting this confirmation")
            conn.execute("UPDATE generation_jobs SET state='CONFIRMED',confirmation_token_hash=?,updated_at=? WHERE id=?", (token_hash, now(), job_id))
        result = self._get(job_id)
        result["confirmation_token"] = token
        return result

    def run(self, job_id: str, confirmation_token: str) -> dict[str, Any]:
        token_hash = hashlib.sha256(confirmation_token.encode()).hexdigest()
        stale = False
        with self.db.write() as conn:
            row = conn.execute("SELECT state,confirmation_token_hash,confirmation_used,input_hashes_json,project_id,owner_type,owner_id FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise NotFoundError("generation job not found")
            state, stored_token, used, input_hashes_json, project_id, owner_type, owner_id = row
            if state != "CONFIRMED" or stored_token != token_hash or used:
                raise ConflictError({job_id: 0}, "generation job is not runnable with this token")
            # Re-verify inputs inside the write transaction before spending the token.
            frozen = json.loads(input_hashes_json)
            current = self._compute_input_hashes(
                conn, project_id, owner_type, owner_id, frozen["references"].keys(),
                frozen.get("parent_candidate", {}).get("id"),
                frozen.get("target_asset", {}).get("id"),
            )
            if dump(current) != input_hashes_json:
                conn.execute("UPDATE generation_jobs SET state='STALE',confirmation_used=1,error_json=?,updated_at=? WHERE id=?", (dump({"code": "inputs_changed", "message": "owner 或引用输入已变化，请重新 prepare 并确认"}), now(), job_id))
                stale = True
            else:
                conn.execute("UPDATE generation_jobs SET state='RUNNING',confirmation_used=1,updated_at=? WHERE id=?", (now(), job_id))
        if stale:
            raise ConflictError({job_id: 0}, "generation inputs changed after confirmation")
        job = self._get(job_id)
        staging = self.media_store.staging / job_id
        staging.mkdir(parents=True, exist_ok=True)
        try:
            paths = self.adapters[job["adapter"]].generate(job["spec"], staging)
            ingested = [self._ingest(path) for path in paths]
        except Exception as error:
            with self.db.write() as conn:
                conn.execute("UPDATE generation_jobs SET state='FAILED',error_json=?,updated_at=? WHERE id=?", (dump({"type": type(error).__name__, "message": str(error)}), now(), job_id))
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        with self.db.write() as conn:
            candidate_ids = []
            for index, media in enumerate(ingested):
                media_id = uid()
                candidate_ids.append(media_id)
                label = self._candidate_label(
                    conn, job["owner_type"], job["owner_id"], job.get("parent_candidate_id"), index
                )
                conn.execute(
                    """INSERT INTO media_versions(
                    id,owner_type,owner_id,kind,storage_path,sha256,mime,width,height,
                    duration_seconds,is_current,is_stale,created_at,project_id,candidate_status,
                    generation_job_id,parent_candidate_id,prompt_text,source_kind,candidate_label,
                    owner_revision,source_fingerprint,prompt_sha256,target_asset_id,target_asset_revision,
                    source_document_version_id,source_projection_revision,derived_from_ids_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,NULL,0,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (media_id, job["owner_type"], job["owner_id"], "image", media["path"],
                     media["sha256"], media["mime"], media["width"], media["height"], now(),
                     job["project_id"], "candidate", job["id"], job.get("parent_candidate_id"),
                     job["spec"]["prompt"], "edit" if job.get("parent_candidate_id") else "generation",
                     label, job["input_hashes"]["owner"]["revision"], job["fingerprint"],
                     hashlib.sha256(job["spec"]["prompt"].encode()).hexdigest(),
                     job["input_hashes"].get("target_asset", {}).get("id"),
                     job["input_hashes"].get("target_asset", {}).get("revision"),
                     job["input_hashes"]["owner"].get("source_document_version_id"),
                     job["input_hashes"]["owner"].get("source_projection_revision"),
                     job["input_hashes"]["owner"].get("derived_from_ids_json", "[]")),
                )
            cursor = conn.execute(
                "UPDATE generation_jobs SET state='SUCCEEDED',updated_at=? WHERE id=? AND state='RUNNING'",
                (now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError({job_id: 0}, "generation job is no longer running")
            conn.execute(
                """INSERT INTO domain_events(project_id,aggregate_type,aggregate_id,revision,event_type,
                payload_json,causation_id,correlation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (job["project_id"], "generation_job", job_id, 0, "media.candidates.generated",
                 dump({"candidate_ids": candidate_ids, "owner_type": job["owner_type"],
                       "owner_id": job["owner_id"]}), None, job_id, now()),
            )
        return self._get(job_id)

    @staticmethod
    def _candidate_label(conn, owner_type: str, owner_id: str, parent_id: str | None, ordinal: int = 0) -> str:
        if parent_id:
            parent = conn.execute(
                "SELECT candidate_label FROM media_versions WHERE id=?", (parent_id,)
            ).fetchone()
            base = parent[0] if parent and parent[0] else parent_id[:8]
            child_count = conn.execute(
                "SELECT COUNT(*) FROM media_versions WHERE parent_candidate_id=?", (parent_id,)
            ).fetchone()[0]
            return f"{base}{child_count + 1}"
        root_count = conn.execute(
            """SELECT COUNT(*) FROM media_versions WHERE owner_type=? AND owner_id=?
            AND candidate_status<>'formal' AND parent_candidate_id IS NULL""",
            (owner_type, owner_id),
        ).fetchone()[0]
        number = root_count
        return chr(65 + number) if number < 26 else f"C{number + 1}"

    def list_candidates(
        self, project_id: str, owner_type: str | None = None, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses, values = ["project_id=?", "candidate_status<>'formal'"], [project_id]
        if owner_type:
            clauses.append("owner_type=?")
            values.append(owner_type)
        if owner_id:
            clauses.append("owner_id=?")
            values.append(owner_id)
        rows = self.db.connection.execute(
            f"SELECT * FROM media_versions WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            values,
        )
        return [self._candidate_detail(row_dict(row)) for row in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self.db.connection.execute(
            "SELECT * FROM media_versions WHERE id=? AND candidate_status<>'formal'", (candidate_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("media candidate not found")
        return self._candidate_detail(row_dict(row))

    def _candidate_detail(self, candidate: dict[str, Any]) -> dict[str, Any]:
        asset_id = None
        if candidate.get("accepted_asset_version_id"):
            row = self.db.connection.execute(
                "SELECT asset_id FROM asset_versions WHERE id=?", (candidate["accepted_asset_version_id"],)
            ).fetchone()
            asset_id = row[0] if row else None
        elif candidate["owner_type"] == "asset":
            asset_id = candidate["owner_id"]
        bindings = []
        if asset_id:
            bindings = [row_dict(row) for row in self.db.connection.execute(
                """SELECT b.id,b.shot_id,b.asset_version_id,b.binding_mode,b.is_stale,b.stale_reason,
                s.order_index AS shot_order,g.code AS segment_code
                FROM reference_bindings b JOIN shots s ON s.id=b.shot_id
                JOIN segments g ON g.id=s.segment_id WHERE b.asset_id=? ORDER BY g.code,s.order_index""",
                (asset_id,),
            )]
        candidate["bindings"] = bindings
        candidate["stale_impact_count"] = sum(1 for item in bindings if item["is_stale"])
        return candidate

    def import_candidate(self, metadata: MediaCandidateImport, body: bytes) -> dict[str, Any]:
        if hashlib.sha256(body).hexdigest() != metadata.sha256:
            raise ValueError("uploaded media SHA-256 does not match")
        width, height = self._validate_png(body)
        # Scope is verified before any filesystem write.
        with self.db.write() as conn:
            self._verify_agent_scope(conn, metadata)
        candidate_id = uid()
        staging = self.media_store.staging / candidate_id
        staging.mkdir(parents=True, exist_ok=False)
        staged = staging / "candidate.png"
        try:
            staged.write_bytes(body)
            stored, digest = self.media_store.ingest(staged)
            with self.db.write() as conn:
                self._verify_agent_scope(conn, metadata)
                conn.execute(
                    """INSERT INTO media_versions(
                    id,owner_type,owner_id,kind,storage_path,sha256,mime,width,height,
                    duration_seconds,is_current,is_stale,created_at,project_id,candidate_status,
                    parent_candidate_id,prompt_text,source_kind,task_id,agent_run_id,candidate_label,
                    owner_revision,source_fingerprint,prompt_sha256,target_asset_id,target_asset_revision
                    ,source_document_version_id,source_projection_revision,derived_from_ids_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,NULL,0,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (candidate_id, metadata.owner_type, metadata.owner_id, "image", stored, digest,
                     metadata.mime, width, height, now(), metadata.project_id, "candidate",
                     metadata.parent_candidate_id, metadata.prompt, "agent_import", metadata.task_id,
                     metadata.run_id, self._candidate_label(
                         conn, metadata.owner_type, metadata.owner_id, metadata.parent_candidate_id
                     ), self._frozen_revision(conn, metadata.task_id, metadata.owner_id),
                     fingerprint({"task_id": metadata.task_id, "run_id": metadata.run_id,
                                  "owner_id": metadata.owner_id, "prompt": metadata.prompt,
                                  "sha256": metadata.sha256}),
                     hashlib.sha256(metadata.prompt.encode()).hexdigest(), metadata.target_asset_id,
                     self._frozen_revision(conn, metadata.task_id, metadata.target_asset_id)
                     if metadata.target_asset_id else None,
                     *self._owner_lineage(conn, metadata.owner_type, metadata.owner_id)),
                )
                conn.execute(
                    """INSERT INTO domain_events(project_id,aggregate_type,aggregate_id,revision,
                    event_type,payload_json,causation_id,correlation_id,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (metadata.project_id, "media_candidate", candidate_id, 0,
                     "media.candidate.imported", dump({"candidate_id": candidate_id,
                      "owner_type": metadata.owner_type, "owner_id": metadata.owner_id,
                      "sha256": digest}), metadata.run_id, metadata.task_id, now()),
                )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return self.get_candidate(candidate_id)

    def _verify_agent_scope(self, conn, metadata: MediaCandidateImport) -> None:
        row = conn.execute(
            """SELECT t.project_id,t.status,r.status,r.task_id,s.selection_json,s.skill_manifest_json,
            r.skill_hash,s.expected_revisions_json FROM tasks t JOIN task_snapshots s ON s.task_id=t.id
            JOIN agent_runs r ON r.id=? WHERE t.id=?""",
            (metadata.run_id, metadata.task_id),
        ).fetchone()
        if not row:
            raise NotFoundError("running task/agent run not found")
        if row[0] != metadata.project_id or row[3] != metadata.task_id:
            raise ProjectMismatchError("task or run belongs to another project")
        if row[1] != "RUNNING" or row[2] != "RUNNING":
            raise ConflictError({metadata.task_id: 0}, "task and agent run must be RUNNING")
        manifest = json.loads(row[5])
        if len(manifest) != 1 or manifest[0]["hash"] != row[6] or skill_tree_hash(manifest[0]["name"]) != row[6]:
            raise ConflictError({metadata.task_id: 0}, "pinned skill tree changed")
        require_same_project(conn, metadata.project_id, metadata.owner_type, metadata.owner_id)
        selection = json.loads(row[4])
        revisions = json.loads(row[7])
        selected_shots = selection.get("selected_shot_ids", [])
        allowed = metadata.owner_type == "shot" and metadata.owner_id in selected_shots
        if metadata.owner_type == "asset":
            allowed = selection.get("target_asset_id") == metadata.owner_id
        if not allowed:
            raise ProjectMismatchError("candidate owner is outside the frozen task selection")
        if metadata.owner_id not in revisions:
            raise ProjectMismatchError("candidate owner revision was not frozen by the task")
        if metadata.target_asset_id != selection.get("target_asset_id"):
            raise ProjectMismatchError("candidate target asset is outside the frozen task selection")
        if metadata.parent_candidate_id != selection.get("parent_candidate_id"):
            raise ProjectMismatchError("parent candidate was not frozen by this task")
        if metadata.target_asset_id and metadata.target_asset_id not in revisions:
            raise ProjectMismatchError("candidate target asset revision was not frozen by the task")
        owner_table = "shots" if metadata.owner_type == "shot" else "assets"
        current_owner_revision = conn.execute(
            f"SELECT revision FROM {owner_table} WHERE id=?", (metadata.owner_id,)
        ).fetchone()[0]
        if current_owner_revision != revisions[metadata.owner_id]:
            raise ConflictError(
                {metadata.owner_id: current_owner_revision}, "frozen candidate owner changed"
            )
        if metadata.target_asset_id:
            current_target_revision = conn.execute(
                "SELECT revision FROM assets WHERE id=?", (metadata.target_asset_id,)
            ).fetchone()[0]
            if current_target_revision != revisions[metadata.target_asset_id]:
                raise ConflictError(
                    {metadata.target_asset_id: current_target_revision},
                    "frozen candidate target asset changed",
                )
        imported = conn.execute(
            "SELECT COUNT(*) FROM media_versions WHERE agent_run_id=?", (metadata.run_id,)
        ).fetchone()[0]
        if imported >= selection.get("media_candidate_limit", 3):
            raise ConflictError({metadata.run_id: imported}, "agent media candidate budget exhausted")
        if metadata.parent_candidate_id:
            parent = conn.execute(
                "SELECT project_id,owner_type,owner_id,candidate_status,sha256 FROM media_versions WHERE id=?",
                (metadata.parent_candidate_id,),
            ).fetchone()
            if not parent or tuple(parent[:3]) != (metadata.project_id, metadata.owner_type, metadata.owner_id):
                raise ProjectMismatchError("parent candidate belongs to another task owner")
            if parent[3] not in {"candidate", "accepted"}:
                raise ValueError("parent media is not an editable candidate")
            if parent[4] != selection.get("parent_candidate_sha256"):
                raise ConflictError({metadata.parent_candidate_id: 0}, "frozen parent candidate changed")

    def accept_candidate(self, candidate_id: str, data: MediaCandidateAccept) -> dict[str, Any]:
        preflight = self.db.connection.execute(
            "SELECT storage_path,sha256 FROM media_versions WHERE id=?", (candidate_id,)
        ).fetchone()
        if not preflight:
            raise NotFoundError("media candidate not found")
        validated_stat = self._verify_media_object(preflight[0], preflight[1])
        with self.db.write() as conn:
            candidate = conn.execute("SELECT * FROM media_versions WHERE id=?", (candidate_id,)).fetchone()
            if not candidate or candidate["candidate_status"] != "candidate":
                raise ConflictError({candidate_id: 0}, "media candidate is not awaiting acceptance")
            if candidate["is_stale"]:
                raise ConflictError({candidate_id: 0}, "stale media candidate cannot become a formal REF")
            if (candidate["storage_path"], candidate["sha256"]) != tuple(preflight):
                raise ConflictError({candidate_id: 0}, "media candidate changed during integrity validation")
            try:
                current_stat = Path(candidate["storage_path"]).stat()
            except OSError as error:
                raise ConflictError({candidate_id: 0}, "media candidate object disappeared") from error
            if (current_stat.st_ino, current_stat.st_size, current_stat.st_mtime_ns) != validated_stat:
                raise ConflictError({candidate_id: 0}, "media candidate object changed during integrity validation")
            if candidate["kind"] == "video":
                if candidate["owner_type"] != "shot":
                    raise ProjectMismatchError("video candidate must belong to a shot")
                if data.asset_id is not None or data.expected_asset_revision is not None:
                    raise ValueError("shot video acceptance must not target an Asset REF")
                if data.expected_shot_revision is None:
                    raise ValueError("expected_shot_revision is required for video acceptance")
                shot = conn.execute(
                    """SELECT e.project_id,sh.revision,sh.status,sh.is_stale,
                    a.revision AS target_asset_revision
                    FROM shots sh JOIN segments g ON g.id=sh.segment_id
                    JOIN episodes e ON e.id=g.episode_id
                    LEFT JOIN assets a ON a.id=? WHERE sh.id=?""",
                    (candidate["target_asset_id"], candidate["owner_id"]),
                ).fetchone()
                if not shot:
                    raise NotFoundError("candidate shot not found")
                if shot[0] != candidate["project_id"]:
                    raise ProjectMismatchError("candidate and shot belong to different projects")
                if shot[2] != "active" or shot[3]:
                    raise ConflictError(
                        {candidate["owner_id"]: shot[1]},
                        "inactive or stale shot cannot accept video",
                    )
                if candidate["owner_revision"] != shot[1] or data.expected_shot_revision != shot[1]:
                    raise ConflictError(
                        {candidate["owner_id"]: shot[1]},
                        "candidate shot changed after generation",
                    )
                if (
                    candidate["target_asset_id"]
                    and candidate["target_asset_revision"] != shot[4]
                ):
                    raise ConflictError(
                        {candidate["target_asset_id"]: shot[4]},
                        "candidate target REF changed after generation",
                    )
                conn.execute(
                    """UPDATE media_versions SET is_current=0
                    WHERE project_id=? AND owner_type='shot' AND owner_id=? AND kind='video'
                    AND candidate_status='accepted' AND is_current=1""",
                    (candidate["project_id"], candidate["owner_id"]),
                )
                conn.execute(
                    """UPDATE media_versions SET candidate_status='accepted',is_current=1,
                    accepted_shot_revision=?,accepted_at=? WHERE id=?""",
                    (shot[1], now(), candidate_id),
                )
                conn.execute(
                    """INSERT INTO domain_events(project_id,aggregate_type,aggregate_id,revision,event_type,
                    payload_json,causation_id,correlation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        candidate["project_id"], "shot", candidate["owner_id"], shot[1],
                        "shot.video_ref.accepted",
                        dump({"media_version_id": candidate_id, "shot_revision": shot[1]}),
                        None, None, now(),
                    ),
                )
                return self.get_candidate(candidate_id)

            if data.asset_id is None or data.expected_asset_revision is None:
                raise ValueError("asset_id and expected_asset_revision are required for image acceptance")
            if data.expected_shot_revision is not None:
                raise ValueError("image acceptance must not include expected_shot_revision")
            asset = conn.execute("SELECT project_id,revision,current_version_id FROM assets WHERE id=?", (data.asset_id,)).fetchone()
            if not asset:
                raise NotFoundError("asset not found")
            if candidate["project_id"] != asset[0]:
                raise ProjectMismatchError("candidate and asset belong to different projects")
            if candidate["owner_type"] == "asset" and candidate["owner_id"] != data.asset_id:
                raise ProjectMismatchError("candidate belongs to another asset")
            if candidate["target_asset_id"] != data.asset_id:
                raise ProjectMismatchError("candidate was not frozen for this target asset")
            owner_table = "shots" if candidate["owner_type"] == "shot" else "assets"
            owner_revision = conn.execute(
                f"SELECT revision FROM {owner_table} WHERE id=?", (candidate["owner_id"],)
            ).fetchone()[0]
            if candidate["owner_revision"] != owner_revision:
                raise ConflictError({candidate["owner_id"]: owner_revision}, "candidate owner changed after generation")
            if candidate["target_asset_revision"] != asset[1]:
                raise ConflictError({data.asset_id: asset[1]}, "candidate target REF changed after generation")
            if asset[1] != data.expected_asset_revision:
                raise ConflictError({data.asset_id: asset[1]})
            version_id = uid()
            number = conn.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM asset_versions WHERE asset_id=?",
                (data.asset_id,),
            ).fetchone()[0]
            content = {
                "media_version_id": candidate_id,
                "prompt": candidate["prompt_text"],
                "source_kind": candidate["source_kind"],
                "sha256": candidate["sha256"],
            }
            conn.execute(
                "INSERT INTO asset_versions(id,asset_id,version_number,content_json,supersedes_id,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, data.asset_id, number, dump(content), asset[2], now()),
            )
            conn.execute(
                "UPDATE assets SET current_version_id=?,revision=revision+1 WHERE id=? AND revision=?",
                (version_id, data.asset_id, data.expected_asset_revision),
            )
            conn.execute(
                "UPDATE media_versions SET is_current=0 WHERE accepted_asset_version_id IN (SELECT id FROM asset_versions WHERE asset_id=?)",
                (data.asset_id,),
            )
            conn.execute(
                """UPDATE media_versions SET candidate_status='accepted',is_current=1,
                accepted_asset_version_id=?,accepted_at=? WHERE id=?""",
                (version_id, now(), candidate_id),
            )
            stale = DependencyService.mark_follow_latest_stale(
                conn, data.asset_id, version_id, exclude_media_id=candidate_id
            )
            conn.execute(
                """INSERT INTO domain_events(project_id,aggregate_type,aggregate_id,revision,event_type,
                payload_json,causation_id,correlation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (asset[0], "asset", data.asset_id, asset[1] + 1, "asset.ref.accepted",
                 dump({"asset_version_id": version_id, "media_version_id": candidate_id,
                       "stale_binding_count": stale}), None, None, now()),
            )
        return self.get_candidate(candidate_id)

    def _verify_media_object(self, storage_path: str, expected_sha256: str) -> tuple[int, int, int]:
        try:
            source = Path(storage_path)
            if source.is_symlink():
                raise ValueError("media candidate object must not be a symlink")
            path = source.resolve(strict=True)
            objects = self.media_store.objects.resolve(strict=True)
        except OSError as error:
            raise ValueError("media candidate object is missing") from error
        if objects not in path.parents or not path.is_file():
            raise ValueError("media candidate object is outside the managed Media Store")
        if MediaStore._hash_file(path) != expected_sha256:
            raise ValueError("media candidate object SHA-256 mismatch")
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _ingest(self, path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        width, height = self._validate_png(data)
        stored, digest = self.media_store.ingest(path)
        return {"path": stored, "sha256": digest, "mime": "image/png", "width": width, "height": height}

    def _frozen_revision(self, conn, task_id: str, entity_id: str) -> int:
        row = conn.execute(
            "SELECT expected_revisions_json FROM task_snapshots WHERE task_id=?", (task_id,)
        ).fetchone()
        revisions = json.loads(row[0]) if row else {}
        if entity_id not in revisions:
            raise ProjectMismatchError("entity revision was not frozen by the task")
        return revisions[entity_id]

    @staticmethod
    def _owner_lineage(conn, owner_type: str, owner_id: str) -> tuple[Any, Any, str]:
        if owner_type != "shot":
            return None, None, "[]"
        row = conn.execute(
            """SELECT source_document_version_id,source_projection_revision,derived_from_ids_json
            FROM shots WHERE id=?""", (owner_id,),
        ).fetchone()
        return row[0], row[1], row[2]

    @staticmethod
    def _validate_png(data: bytes) -> tuple[int, int]:
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid PNG signature")
        offset, width, height, chunks, compressed = 8, None, None, [], bytearray()
        while offset + 12 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            if length > 64 * 1024 * 1024 or offset + 12 + length > len(data):
                raise ValueError("invalid PNG chunk length")
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            expected_crc = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
            if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
                raise ValueError("invalid PNG chunk CRC")
            chunks.append(kind)
            if len(chunks) == 1:
                if kind != b"IHDR" or length != 13:
                    raise ValueError("PNG must begin with IHDR")
                width, height = struct.unpack(">II", payload[:8])
                if not width or not height or width > 8192 or height > 8192 or width * height > 8_000_000:
                    raise ValueError("PNG dimensions exceed the safe image budget")
            if kind == b"IDAT":
                compressed.extend(payload)
            offset += 12 + length
            if kind == b"IEND":
                if length or offset != len(data):
                    raise ValueError("invalid PNG IEND")
                break
        if not chunks or chunks[-1] != b"IEND" or b"IDAT" not in chunks:
            raise ValueError("PNG requires IDAT and IEND")
        max_decoded = width * height * 8 + height
        if max_decoded > 64 * 1024 * 1024:
            raise ValueError("PNG decoded data exceeds the 64 MiB budget")
        try:
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(bytes(compressed), max_decoded + 1)
            if len(decoded) > max_decoded or decoder.unconsumed_tail:
                raise ValueError("PNG decoded data exceeds the safe image budget")
            decoded += decoder.flush(max_decoded + 1 - len(decoded))
            if len(decoded) > max_decoded or not decoder.eof:
                raise ValueError("invalid or oversized PNG image data")
        except zlib.error as error:
            raise ValueError("invalid PNG image data") from error
        return int(width), int(height)
