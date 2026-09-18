"""Loopback-only MiniMax-H3 FL2VA submission and durable polling."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import struct
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from script_weaver.domain.models import ConflictError, H3VideoPrepare, NotFoundError
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database

from .changeset_service import fingerprint
from .generation_service import GenerationJobService
from .workbench_service import dump, now, require_same_project, uid

H3_MODEL = "MiniMaxAI/MiniMax-H3"
H3_TASK = "fl2va"
H3_SHORT_EDGE = 768
H3_NUM_OUTPUTS = 1
H3_INFERENCE_STEPS = 50
H3_FLOW_SHIFT = 12.0
H3_AUDIO_FLOW_SHIFT = 3.0
MAX_VIDEO_BYTES = 512 * 1024 * 1024
_SAFE_REMOTE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def validate_h3_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("H3 URL must be an http(s) loopback endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("H3 URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("H3 URL must not contain a path")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise ValueError("H3 URL must use a literal loopback IP, not a DNS name") from error
    if not address.is_loopback:
        raise ValueError("H3 URL must be loopback-only")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("H3 URL has an invalid port") from error
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{parsed.scheme}://{host}{f':{port}' if port else ''}"


class H3VideoService:
    def __init__(
        self,
        database: Database,
        media_store: MediaStore,
        generation: GenerationJobService,
        endpoint: str | None = None,
        shared_root: str | Path | None = None,
    ):
        self.db, self.media_store, self.generation = database, media_store, generation
        configured_url = endpoint if endpoint is not None else os.getenv("SCRIPT_WEAVER_H3_URL")
        configured_root = (
            Path(shared_root) if shared_root is not None
            else Path(os.environ["SCRIPT_WEAVER_H3_SHARED_MEDIA_ROOT"])
            if os.getenv("SCRIPT_WEAVER_H3_SHARED_MEDIA_ROOT") else None
        )
        self.endpoint = validate_h3_url(configured_url) if configured_url else None
        self.shared_root = self._validate_shared_root(configured_root) if configured_root else None

    @staticmethod
    def _validate_shared_root(root: Path) -> Path:
        if root.is_symlink():
            raise ValueError("H3 shared media root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("H3 shared media root must be a directory")
        return resolved

    def _configured(self) -> tuple[str, Path]:
        if not self.endpoint or not self.shared_root:
            raise ValueError(
                "local H3 requires SCRIPT_WEAVER_H3_URL and "
                "SCRIPT_WEAVER_H3_SHARED_MEDIA_ROOT"
            )
        return self.endpoint, self.shared_root

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        endpoint, _root = self._configured()
        with httpx.Client(follow_redirects=False, timeout=30.0) as client:
            response = client.request(method, endpoint + path, **kwargs)
        if 300 <= response.status_code < 400:
            raise ValueError("H3 redirects are forbidden")
        response.raise_for_status()
        return response

    def doctor(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "configured": bool(self.endpoint and self.shared_root),
            "endpoint": self.endpoint,
            "requested_task": H3_TASK,
            "model": H3_MODEL,
            "requested_short_edge": H3_SHORT_EDGE,
            "server_capabilities": "unverified",
            "identity": "unavailable",
            "reachable": False,
            "ready": False,
            "shared_root": str(self.shared_root) if self.shared_root else None,
            "shared_root_read_write": False,
        }
        if not result["configured"]:
            return result
        _endpoint, root = self._configured()
        marker = root / f".script-weaver-doctor-{secrets.token_hex(8)}"
        try:
            marker.write_bytes(b"ok")
            result["shared_root_read_write"] = marker.read_bytes() == b"ok"
        finally:
            marker.unlink(missing_ok=True)
        try:
            response = self._request("GET", "/v1/models")
            result["reachable"] = True
            models = response.json().get("data", [])
            ids = {item.get("id") for item in models if isinstance(item, dict)}
            result["identity"] = "verified" if H3_MODEL in ids else "mismatch"
            result["server_capabilities"] = "models_only_task_and_resolution_unverified"
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                result["reachable"] = True
                result["identity"] = "degraded_models_endpoint_unsupported"
                result["server_capabilities"] = "degraded_unverified"
            else:
                result["error"] = str(error)
        except (httpx.HTTPError, ValueError) as error:
            result["error"] = str(error)
        result["ready"] = bool(
            result["reachable"] and result["shared_root_read_write"]
            and result["identity"] == "verified"
        )
        return result

    def _inputs(self, conn, data: H3VideoPrepare) -> dict[str, Any]:
        require_same_project(conn, data.project_id, "shot", data.shot_id)
        require_same_project(conn, data.project_id, "asset", data.target_asset_id)
        upstream = conn.execute(
            """SELECT sh.revision,sh.status,sh.is_stale,sh.source_document_version_id,
            sh.source_projection_revision,sh.derived_from_ids_json,
            g.id,g.revision,g.status,g.source_document_version_id,g.source_projection_revision,
            g.derived_from_ids_json,e.id,e.revision,e.status,e.is_stale,
            e.source_document_version_id,e.development_fingerprint,e.derived_from_ids_json
            FROM shots sh JOIN segments g ON g.id=sh.segment_id
            JOIN episodes e ON e.id=g.episode_id WHERE sh.id=?""",
            (data.shot_id,),
        ).fetchone()
        if not upstream:
            raise NotFoundError("H3 shot ancestry not found")
        if upstream[1] != "active" or upstream[2] or upstream[8] != "active":
            raise ValueError("H3 requires an active, non-stale shot and segment")
        if upstream[14] != "active" or upstream[15]:
            raise ValueError("H3 requires an active, non-stale episode")
        asset = conn.execute(
            "SELECT revision,current_version_id FROM assets WHERE id=?", (data.target_asset_id,)
        ).fetchone()
        conditions: list[dict[str, Any]] = []
        target_binding_id = None
        for keyframe in data.keyframes:
            media_id = keyframe.media_id
            row = conn.execute(
                """SELECT m.project_id,m.sha256,m.storage_path,m.mime,m.candidate_status,
                m.is_current,m.accepted_asset_version_id,v.asset_id,b.binding_mode,b.asset_version_id,
                m.is_stale,b.id,b.is_stale,b.stale_reason,b.source_document_version_id,
                b.source_projection_revision,b.derived_from_ids_json,a.current_version_id,a.revision
                FROM media_versions m JOIN asset_versions v ON v.id=m.accepted_asset_version_id
                JOIN assets a ON a.id=v.asset_id
                JOIN reference_bindings b ON b.id=? AND b.shot_id=?
                WHERE m.id=?""",
                (keyframe.binding_id, data.shot_id, media_id),
            ).fetchone()
            if not row:
                raise NotFoundError("accepted keyframe media not found")
            if row[0] != data.project_id:
                raise ValueError("keyframe belongs to another project")
            if row[3] != "image/png" or row[4] != "accepted" or row[10]:
                raise ValueError("H3 keyframe must be an accepted, non-stale PNG REF")
            if row[8] != "frozen" or row[9] != row[6] or row[12]:
                raise ValueError("H3 keyframe must be frozen on the target shot")
            if keyframe.frame_index == 0:
                if row[7] != data.target_asset_id:
                    raise ValueError("H3 target asset must match the start keyframe binding")
                target_binding_id = row[11]
            path = Path(row[2]).resolve(strict=True)
            objects = self.media_store.objects.resolve(strict=True)
            if objects not in path.parents or path.is_symlink() or not path.is_file():
                raise ValueError("keyframe is outside the managed Media Store")
            if MediaStore._hash_file(path) != row[1]:
                raise ValueError("keyframe object hash no longer matches its fact")
            binding_fact = {
                "id": row[11], "asset_version_id": row[9], "mode": row[8],
                "is_stale": row[12], "stale_reason": row[13],
                "source_document_version_id": row[14],
                "source_projection_revision": row[15], "derived_from_ids_json": row[16],
            }
            conditions.append({
                "role": "start" if keyframe.frame_index == 0 else "end",
                "frame_index": keyframe.frame_index,
                "media_id": media_id, "sha256": row[1],
                "asset_version_id": row[6], "asset_id": row[7],
                "asset_revision": row[18], "binding": binding_fact,
                "binding_fingerprint": fingerprint(binding_fact),
            })
        if target_binding_id is None:
            raise ValueError("H3 target asset must match an explicit keyframe binding")
        return {
            "owner": {
                "type": "shot", "id": data.shot_id, "revision": upstream[0],
                "status": upstream[1], "is_stale": upstream[2],
                "source_document_version_id": upstream[3],
                "source_projection_revision": upstream[4], "derived_from_ids_json": upstream[5],
            },
            "segment": {
                "id": upstream[6], "revision": upstream[7], "status": upstream[8],
                "source_document_version_id": upstream[9],
                "source_projection_revision": upstream[10], "derived_from_ids_json": upstream[11],
            },
            "episode": {
                "id": upstream[12], "revision": upstream[13], "status": upstream[14],
                "is_stale": upstream[15], "source_document_version_id": upstream[16],
                "development_fingerprint": upstream[17], "derived_from_ids_json": upstream[18],
            },
            "target_asset": {
                "id": data.target_asset_id, "revision": asset[0],
                "current_version_id": asset[1], "binding_id": target_binding_id,
            },
            "conditions": conditions,
            "prompt_sha256": hashlib.sha256(data.prompt.encode()).hexdigest(),
            "duration_seconds": data.duration_seconds,
            "task": H3_TASK,
            "model": H3_MODEL,
            "short_edge": H3_SHORT_EDGE,
        }

    def prepare(self, data: H3VideoPrepare) -> dict[str, Any]:
        self._configured()
        sampling = {
            "num_outputs_per_prompt": H3_NUM_OUTPUTS,
            "num_inference_steps": H3_INFERENCE_STEPS,
            "flow_shift": H3_FLOW_SHIFT,
            "audio_flow_shift": H3_AUDIO_FLOW_SHIFT,
            "seed": secrets.randbelow(2**31),
        }
        with self.db.write() as conn:
            inputs = self._inputs(conn, data)
        inputs["sampling"] = sampling
        spec = {
            **data.model_dump(mode="json"),
            "task": H3_TASK,
            "model": H3_MODEL,
            "short_edge": H3_SHORT_EDGE,
            **sampling,
        }
        exact = fingerprint({"spec": spec, "input_hashes": inputs})
        job_id, timestamp = uid(), now()
        with self.db.write() as conn:
            conn.execute(
                """INSERT INTO generation_jobs(
                id,project_id,owner_type,owner_id,kind,state,spec_json,fingerprint,
                input_hashes_json,adapter,confirmation_token_hash,confirmation_used,
                error_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,'AWAITING_CONFIRMATION',?,?,?,?,NULL,0,NULL,?,?)""",
                (job_id, data.project_id, "shot", data.shot_id, "video", dump(spec), exact,
                 dump(inputs), "h3-fl2va", timestamp, timestamp),
            )
        return self.generation._get(job_id)

    def latest_for_shot(self, project_id: str, shot_id: str) -> dict[str, Any] | None:
        with self.db.write() as conn:
            require_same_project(conn, project_id, "shot", shot_id)
            row = conn.execute(
                """SELECT id FROM generation_jobs
                WHERE project_id=? AND owner_type='shot' AND owner_id=?
                AND kind='video' AND adapter='h3-fl2va'
                ORDER BY created_at DESC,id DESC LIMIT 1""",
                (project_id, shot_id),
            ).fetchone()
        return self.generation._get(row[0]) if row else None

    def submit(self, job_id: str, confirmation_token: str) -> dict[str, Any]:
        token_hash = hashlib.sha256(confirmation_token.encode()).hexdigest()
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["adapter"] != "h3-fl2va":
                raise NotFoundError("H3 generation job not found")
            if row["state"] != "CONFIRMED" or row["confirmation_token_hash"] != token_hash or row["confirmation_used"]:
                raise ConflictError({job_id: 0}, "H3 job is not runnable with this token")
            spec = H3VideoPrepare(**{
                key: value for key, value in json.loads(row["spec_json"]).items()
                if key in H3VideoPrepare.model_fields
            })
            try:
                current = self._inputs(conn, spec)
                current["sampling"] = {
                    key: json.loads(row["spec_json"])[key] for key in (
                        "num_outputs_per_prompt", "num_inference_steps", "flow_shift",
                        "audio_flow_shift", "seed",
                    )
                }
                changed = dump(current) != row["input_hashes_json"]
            except (ValueError, NotFoundError):
                changed = True
            if changed:
                conn.execute(
                    """UPDATE generation_jobs SET state='STALE',confirmation_used=1,
                    error_json=?,updated_at=? WHERE id=?""",
                    (dump({"code": "inputs_changed", "message": "H3 inputs changed before submit"}),
                     now(), job_id),
                )
                raise ConflictError({job_id: 0}, "H3 inputs changed after confirmation")
            conn.execute(
                """UPDATE generation_jobs SET state='SUBMITTING',confirmation_used=1,
                submitted_fingerprint=?,submitted_at=?,updated_at=? WHERE id=?""",
                (row["fingerprint"], now(), now(), job_id),
            )
        job = self.generation._get(job_id)
        try:
            conditions = self._share_keyframes(job)
            response = self._request(
                "POST", "/v1/videos",
                headers={"Idempotency-Key": job_id},
                json={
                    "task": H3_TASK, "model": H3_MODEL,
                    "conditions": conditions,
                    "seconds": job["spec"]["duration_seconds"],
                    "target": {
                        "short_edge": H3_SHORT_EDGE,
                        "aspect_ratio": "auto",
                        "duration_seconds": job["spec"]["duration_seconds"],
                    },
                    "num_outputs_per_prompt": job["spec"]["num_outputs_per_prompt"],
                    "num_inference_steps": job["spec"]["num_inference_steps"],
                    "flow_shift": job["spec"]["flow_shift"],
                    "audio_flow_shift": job["spec"]["audio_flow_shift"],
                    "seed": job["spec"]["seed"],
                    "prompt": job["spec"]["prompt"],
                },
            )
            payload = response.json()
            remote_id = payload.get("id") or payload.get("data", {}).get("id")
            status = payload.get("status") or payload.get("data", {}).get("status") or "submitted"
            if not isinstance(remote_id, str) or not _SAFE_REMOTE_ID.fullmatch(remote_id):
                raise ValueError("H3 returned an invalid job id")
        except Exception as error:
            with self.db.write() as conn:
                conn.execute(
                    "UPDATE generation_jobs SET state='FAILED',error_json=?,updated_at=? WHERE id=?",
                    (dump({"type": type(error).__name__, "message": str(error)}), now(), job_id),
                )
            raise
        with self.db.write() as conn:
            cursor = conn.execute(
                """UPDATE generation_jobs SET state='H3_SUBMITTED',external_job_id=?,
                external_status=?,updated_at=? WHERE id=? AND state='SUBMITTING'""",
                (remote_id, str(status), now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError({job_id: 0}, "H3 submission is no longer current")
        return self.generation._get(job_id)

    def _share_keyframes(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        _endpoint, root = self._configured()
        directory = root / "script-weaver-inputs" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        if root not in directory.resolve().parents:
            raise ValueError("H3 shared input escaped its configured root")
        conditions = []
        frames = job["input_hashes"]["conditions"]
        for index, frame in enumerate(frames):
            media_id = frame["media_id"]
            row = self.db.connection.execute(
                "SELECT storage_path,sha256 FROM media_versions WHERE id=?", (media_id,)
            ).fetchone()
            source = Path(row[0]).resolve(strict=True)
            if self.media_store.objects.resolve(strict=True) not in source.parents or source.is_symlink():
                raise ValueError("keyframe source escaped Media Store")
            target = directory / f"frame-{index}.png"
            temp = directory / f".{target.name}.{secrets.token_hex(8)}.tmp"
            try:
                shutil.copyfile(source, temp)
                if MediaStore._hash_file(temp) != row[1]:
                    raise ValueError("shared keyframe hash mismatch")
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            conditions.append({
                "type": "image",
                "uri": target.resolve().as_uri(),
                "role": "keyframe",
                "frame_index": frame["frame_index"],
            })
        return conditions

    def poll(self, job_id: str) -> dict[str, Any]:
        job = self.generation._get(job_id)
        if job["adapter"] != "h3-fl2va" or not job.get("external_job_id"):
            raise NotFoundError("submitted H3 generation job not found")
        existing = self.db.connection.execute(
            "SELECT id FROM media_versions WHERE generation_job_id=?", (job_id,)
        ).fetchone()
        if existing:
            return self.generation._get(job_id)
        remote_id = job["external_job_id"]
        if not _SAFE_REMOTE_ID.fullmatch(remote_id):
            raise ValueError("stored H3 job id is invalid")
        response = self._request("GET", f"/v1/videos/{remote_id}")
        payload = response.json()
        status = str(payload.get("status") or payload.get("data", {}).get("status") or "unknown").lower()
        if status in {"failed", "error", "cancelled"}:
            with self.db.write() as conn:
                conn.execute(
                    """UPDATE generation_jobs SET state='FAILED',external_status=?,error_json=?,
                    completed_at=?,updated_at=? WHERE id=?""",
                    (status, dump({"code": "h3_failed", "response": payload}), now(), now(), job_id),
                )
            return self.generation._get(job_id)
        if status not in {"completed", "succeeded", "success"}:
            with self.db.write() as conn:
                conn.execute(
                    "UPDATE generation_jobs SET state='H3_RUNNING',external_status=?,updated_at=? WHERE id=?",
                    (status, now(), job_id),
                )
            return self.generation._get(job_id)
        try:
            media = self._download_video(job_id, remote_id)
        except Exception as error:
            with self.db.write() as conn:
                conn.execute(
                    """UPDATE generation_jobs SET state='FAILED',external_status=?,error_json=?,
                    completed_at=?,updated_at=? WHERE id=?""",
                    (status, dump({"type": type(error).__name__, "message": str(error)}),
                     now(), now(), job_id),
                )
            raise
        with self.db.write() as conn:
            row = conn.execute("SELECT * FROM generation_jobs WHERE id=?", (job_id,)).fetchone()
            existing = conn.execute(
                "SELECT id FROM media_versions WHERE generation_job_id=?", (job_id,)
            ).fetchone()
            if existing:
                return self.generation._get(job_id)
            spec = H3VideoPrepare(**{
                key: value for key, value in json.loads(row["spec_json"]).items()
                if key in H3VideoPrepare.model_fields
            })
            frozen = json.loads(row["input_hashes_json"])
            try:
                current = self._inputs(conn, spec)
                current["sampling"] = {
                    key: json.loads(row["spec_json"])[key] for key in (
                        "num_outputs_per_prompt", "num_inference_steps", "flow_shift",
                        "audio_flow_shift", "seed",
                    )
                }
                stale = dump(current) != row["input_hashes_json"]
            except (ValueError, NotFoundError):
                # The remote work is still retained as a candidate for diagnosis,
                # but it can no longer become a current fact without regeneration.
                stale = True
            media_id = uid()
            conn.execute(
                """INSERT INTO media_versions(
                id,owner_type,owner_id,kind,storage_path,sha256,mime,width,height,
                duration_seconds,is_current,is_stale,created_at,project_id,candidate_status,
                generation_job_id,prompt_text,source_kind,candidate_label,owner_revision,
                source_fingerprint,prompt_sha256,target_asset_id,target_asset_revision,
                source_document_version_id,source_projection_revision,derived_from_ids_json
                ) VALUES(?,?,?,?,?,?,?,NULL,NULL,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (media_id, "shot", spec.shot_id, "video", media["path"], media["sha256"],
                 "video/mp4", spec.duration_seconds, 1 if stale else 0, now(), spec.project_id,
                 "candidate", job_id, spec.prompt, "generation",
                 self.generation._candidate_label(conn, "shot", spec.shot_id, None),
                 frozen["owner"]["revision"], row["fingerprint"], frozen["prompt_sha256"],
                 spec.target_asset_id, frozen["target_asset"]["revision"],
                 *self.generation._owner_lineage(conn, "shot", spec.shot_id)),
            )
            conn.execute(
                """UPDATE generation_jobs SET state='SUCCEEDED',external_status=?,completed_at=?,
                updated_at=? WHERE id=?""",
                (status, now(), now(), job_id),
            )
            conn.execute(
                """INSERT INTO domain_events(project_id,aggregate_type,aggregate_id,revision,
                event_type,payload_json,causation_id,correlation_id,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (spec.project_id, "generation_job", job_id, 0, "media.video_candidate.ready",
                 dump({"candidate_id": media_id, "owner_type": "shot", "owner_id": spec.shot_id,
                       "is_stale": stale}), None, job_id, now()),
            )
        return self.generation._get(job_id)

    def _download_video(self, job_id: str, remote_id: str) -> dict[str, str]:
        endpoint, _root = self._configured()
        staging = self.media_store.staging / job_id
        staging.mkdir(parents=True, exist_ok=True)
        target = staging / "h3-output.mp4"
        try:
            with httpx.Client(follow_redirects=False, timeout=120.0) as client:
                with client.stream("GET", endpoint + f"/v1/videos/{remote_id}/content") as response:
                    if 300 <= response.status_code < 400:
                        raise ValueError("H3 content redirects are forbidden")
                    response.raise_for_status()
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "video/mp4":
                        raise ValueError("H3 content must be video/mp4")
                    total = 0
                    with target.open("wb") as output:
                        for chunk in response.iter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_VIDEO_BYTES:
                                raise ValueError("H3 video exceeds the 512 MiB limit")
                            output.write(chunk)
            self._validate_mp4(target)
            stored, digest = self.media_store.ingest(target)
            return {"path": stored, "sha256": digest}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _validate_mp4(path: Path) -> None:
        size = path.stat().st_size
        if size < 24 or size > MAX_VIDEO_BYTES:
            raise ValueError("invalid MP4 size")
        kinds: set[bytes] = set()
        offset = 0
        with path.open("rb") as handle:
            while offset + 8 <= size:
                handle.seek(offset)
                header = handle.read(16)
                box_size, kind = struct.unpack(">I4s", header[:8])
                header_size = 8
                if box_size == 1:
                    if len(header) < 16:
                        raise ValueError("invalid MP4 extended box")
                    box_size, header_size = struct.unpack(">Q", header[8:16])[0], 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or offset + box_size > size:
                    raise ValueError("invalid MP4 box length")
                kinds.add(kind)
                offset += box_size
        if offset != size or b"ftyp" not in kinds or b"mdat" not in kinds or b"moov" not in kinds:
            raise ValueError("MP4 requires valid ftyp, mdat, and moov boxes")
