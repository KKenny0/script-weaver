"""Prepare/confirm/run boundary for explicitly authorized image generation."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import struct
from pathlib import Path
from typing import Any

from script_weaver.domain.models import ConflictError, GenerationPrepare, NotFoundError
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database
from script_weaver.integrations.media import FakeImageAdapter, GptImage2Adapter

from .changeset_service import fingerprint
from .workbench_service import dump, now, require_same_project, row_dict, uid


class GenerationJobService:
    def __init__(self, database: Database, media_store: MediaStore):
        self.db, self.media_store = database, media_store
        self.adapters = {"fake": FakeImageAdapter(), "gpt-image-2": GptImage2Adapter()}
        self._recover_interrupted_jobs()

    def _recover_interrupted_jobs(self) -> None:
        """A RUNNING job can only mean the daemon died mid-generation; never auto-retry."""
        with self.db.write() as conn:
            rows = conn.execute("SELECT id FROM generation_jobs WHERE state='RUNNING'").fetchall()
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

    def _compute_input_hashes(self, conn, project_id: str, owner_type: str, owner_id: str, reference_ids: list[str]) -> dict[str, Any]:
        """Owner type/id/revision plus sorted reference content hashes; everything project-owned."""
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise NotFoundError("project not found")
        require_same_project(conn, project_id, owner_type, owner_id)
        owner_table = "shots" if owner_type == "shot" else "assets"
        owner_revision = conn.execute(f"SELECT revision FROM {owner_table} WHERE id=?", (owner_id,)).fetchone()[0]
        input_hashes: dict[str, Any] = {"owner": {"type": owner_type, "id": owner_id, "revision": owner_revision}, "references": {}}
        for version_id in sorted(set(reference_ids)):
            row = conn.execute("SELECT content_json FROM asset_versions WHERE id=?", (version_id,)).fetchone()
            if not row:
                raise NotFoundError("reference asset version not found")
            require_same_project(conn, project_id, "asset_version", version_id)
            input_hashes["references"][version_id] = hashlib.sha256(row[0].encode()).hexdigest()
        return input_hashes

    def prepare(self, data: GenerationPrepare) -> dict[str, Any]:
        spec = data.model_dump(mode="json")
        with self.db.write() as conn:
            input_hashes = self._compute_input_hashes(conn, data.project_id, data.owner_type, data.owner_id, data.reference_asset_version_ids)
        exact_fingerprint = fingerprint({"spec": spec, "input_hashes": input_hashes})
        job_id, timestamp = uid(), now()
        with self.db.write() as conn:
            conn.execute("INSERT INTO generation_jobs VALUES(?,?,?,?,?,'AWAITING_CONFIRMATION',?,?,?,?,NULL,NULL,0,NULL,?,?)", (job_id, data.project_id, data.owner_type, data.owner_id, "image", dump(spec), exact_fingerprint, dump(input_hashes), data.adapter, timestamp, timestamp))
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
            current = self._compute_input_hashes(conn, project_id, owner_type, owner_id, json.loads(input_hashes_json)["references"].keys())
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
            count = len(ingested)
            for index, media in enumerate(ingested):
                conn.execute("UPDATE media_versions SET is_current=0 WHERE owner_type=? AND owner_id=? AND kind='image'", (job["owner_type"], job["owner_id"]))
                # Every output of this job is ingested; only the last one becomes current,
                # the rest stay as same-run historical versions.
                conn.execute("INSERT INTO media_versions VALUES(?,?,?,?,?,?,?,?,?,NULL,?,0,?)", (uid(), job["owner_type"], job["owner_id"], "image", media["path"], media["sha256"], media["mime"], media["width"], media["height"], 1 if index == count - 1 else 0, now()))
            cursor = conn.execute(
                "UPDATE generation_jobs SET state='SUCCEEDED',updated_at=? WHERE id=? AND state='RUNNING'",
                (now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError({job_id: 0}, "generation job is no longer running")
        return self._get(job_id)

    def _ingest(self, path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
            raise ValueError("adapter returned invalid PNG")
        width, height = struct.unpack(">II", data[16:24])
        stored, digest = self.media_store.ingest(path)
        return {"path": stored, "sha256": digest, "mime": "image/png", "width": width, "height": height}
