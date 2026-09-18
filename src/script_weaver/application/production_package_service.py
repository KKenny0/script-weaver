"""Deterministic, immutable production-package manifests over accepted facts."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from script_weaver.domain.models import ConflictError, NotFoundError
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database, default_data_dir

from .changeset_service import fingerprint
from .workbench_service import dump, now, row_dict


class ProductionPackageService:
    def __init__(
        self, database: Database, root: str | Path | None = None,
        media_store: MediaStore | None = None,
    ):
        self.db = database
        self.root = Path(root) if root else default_data_dir() / "production-packages"
        self.root.mkdir(parents=True, exist_ok=True)
        self.media_store = media_store

    def _facts(self, conn, project_id: str) -> dict[str, Any]:
        project = conn.execute(
            "SELECT id,title,revision FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not project:
            raise NotFoundError("project not found")
        episodes = [row_dict(row) for row in conn.execute(
            """SELECT id,episode_number,title,status,source_document_version_id
            FROM episodes WHERE project_id=? ORDER BY episode_number,id""", (project_id,)
        )]
        documents = [row_dict(row) for row in conn.execute(
            """SELECT d.id,d.title,d.kind,d.episode_id,d.current_version_id,v.version_number,
            v.content,v.projection_status,v.projection_revision,d.is_stale,d.stale_reason
            FROM creative_documents d JOIN creative_document_versions v ON v.id=d.current_version_id
            WHERE d.project_id=? AND v.status='ACCEPTED'
            AND (d.episode_id IS NULL OR EXISTS(
              SELECT 1 FROM episodes de WHERE de.id=d.episode_id AND de.status='active'
            )) ORDER BY d.kind,d.episode_id,d.id""",
            (project_id,),
        )]
        for document in documents:
            document["content_sha256"] = hashlib.sha256(
                document.pop("content").encode()
            ).hexdigest()
        assets = [row_dict(row) for row in conn.execute(
            """SELECT a.id,a.kind,a.name,a.current_version_id,v.version_number,v.content_json,
            m.id AS media_version_id,m.sha256,m.mime,m.storage_path
            FROM assets a JOIN asset_versions v ON v.id=a.current_version_id
            LEFT JOIN media_versions m ON m.accepted_asset_version_id=v.id AND m.is_current=1
            WHERE a.project_id=? ORDER BY a.kind,a.name,a.id""", (project_id,)
        )]
        for asset in assets:
            if asset["media_version_id"] and self.media_store:
                self._validate_media(asset["storage_path"], asset["sha256"])
            asset.pop("storage_path", None)
        shots = [row_dict(row) for row in conn.execute(
            """SELECT sh.id,e.id AS episode_id,e.episode_number,g.code AS segment_code,
            sh.order_index,sh.revision,sh.is_stale,sh.stale_reason,
            sh.source_document_version_id,sh.source_projection_revision
            FROM shots sh JOIN segments g ON g.id=sh.segment_id
            JOIN episodes e ON e.id=g.episode_id
            WHERE e.project_id=? AND e.status='active' AND g.status='active'
            AND sh.status<>'retired'
            ORDER BY e.episode_number,g.order_index,sh.order_index,sh.id""", (project_id,)
        )]
        videos = [row_dict(row) for row in conn.execute(
            """SELECT m.id AS media_version_id,m.owner_id AS shot_id,m.sha256,m.mime,m.storage_path,
            m.accepted_shot_revision,sh.revision AS current_shot_revision
            FROM media_versions m JOIN shots sh ON sh.id=m.owner_id
            JOIN segments g ON g.id=sh.segment_id JOIN episodes e ON e.id=g.episode_id
            WHERE m.project_id=? AND m.owner_type='shot' AND m.kind='video'
            AND m.candidate_status='accepted' AND m.is_current=1
            AND sh.status<>'retired' AND g.status='active' AND e.status='active'
            ORDER BY m.owner_id,m.id""",
            (project_id,),
        )]
        video_by_shot: dict[str, dict[str, Any]] = {}
        stale_video_ids: list[str] = []
        for video in videos:
            if video["accepted_shot_revision"] != video["current_shot_revision"]:
                stale_video_ids.append(video["media_version_id"])
                continue
            if video["shot_id"] in video_by_shot:
                raise ValueError("shot has multiple current accepted videos; package not ready")
            if self.media_store:
                self._validate_media(video["storage_path"], video["sha256"])
            video.pop("storage_path", None)
            video_by_shot[video["shot_id"]] = video
        for shot in shots:
            video = video_by_shot.get(shot["id"])
            shot["video_media_version_id"] = video["media_version_id"] if video else None
            shot["video_sha256"] = video["sha256"] if video else None
            shot["video_accepted_shot_revision"] = (
                video["accepted_shot_revision"] if video else None
            )
        stale = {
            "documents": [item["id"] for item in documents if item["is_stale"]],
            "shots": [item["id"] for item in shots if item["is_stale"]],
            "bindings": [row[0] for row in conn.execute(
                """SELECT b.id FROM reference_bindings b JOIN assets a ON a.id=b.asset_id
                JOIN shots sh ON sh.id=b.shot_id JOIN segments g ON g.id=sh.segment_id
                JOIN episodes e ON e.id=g.episode_id
                WHERE a.project_id=? AND b.is_stale=1 AND sh.status<>'retired'
                AND g.status='active' AND e.status='active' ORDER BY b.id""", (project_id,),
            )],
            "media": sorted(set(stale_video_ids) | {row[0] for row in conn.execute(
                """SELECT m.id FROM media_versions m WHERE m.project_id=? AND m.is_stale=1
                AND m.candidate_status='accepted' AND m.is_current=1 AND (
                  (m.owner_type='asset' AND EXISTS(
                    SELECT 1 FROM assets a WHERE a.id=m.owner_id AND a.project_id=m.project_id
                  )) OR (m.owner_type='shot' AND EXISTS(
                    SELECT 1 FROM shots sh JOIN segments g ON g.id=sh.segment_id
                    JOIN episodes e ON e.id=g.episode_id WHERE sh.id=m.owner_id
                    AND sh.status<>'retired' AND g.status='active' AND e.status='active'
                  ))
                ) ORDER BY m.id""", (project_id,),
            )}),
        }
        failed = [row_dict(row) for row in conn.execute(
            """SELECT j.id,j.owner_type,j.owner_id,j.kind,j.adapter,j.state,j.error_json
            FROM generation_jobs j
            WHERE j.project_id=? AND j.state='FAILED'
            AND NOT EXISTS(
              SELECT 1 FROM media_versions m
              WHERE m.project_id=j.project_id AND m.owner_type=j.owner_type
              AND m.owner_id=j.owner_id AND m.kind=j.kind
              AND m.candidate_status='accepted' AND m.is_current=1
              AND COALESCE(m.accepted_at,m.created_at)>
                  COALESCE(j.completed_at,j.updated_at,j.created_at)
            )
            AND (
              j.owner_type='asset' OR EXISTS(
                SELECT 1 FROM shots fsh JOIN segments fg ON fg.id=fsh.segment_id
                JOIN episodes fe ON fe.id=fg.episode_id
                WHERE fsh.id=j.owner_id AND fsh.status<>'retired'
                AND fg.status='active' AND fe.status='active'
              )
            ) ORDER BY j.created_at,j.id""", (project_id,),
        )]
        accepted_screenplay_episodes = {
            item["episode_id"] for item in documents if item["kind"] == "screenplay"
        }
        video_shots = set(video_by_shot)
        missing = []
        for episode in episodes:
            if episode["status"] == "active" and episode["id"] not in accepted_screenplay_episodes:
                missing.append({"type": "accepted_screenplay", "episode_id": episode["id"]})
        for shot in shots:
            if shot["id"] not in video_shots:
                missing.append({"type": "accepted_video", "shot_id": shot["id"]})
        excluded = [
            {"type": "removed_episode", "episode_id": item["id"]}
            for item in episodes if item["status"] == "removed"
        ]
        ready = not missing and not failed and not any(stale.values())
        stale_media_details: list[dict[str, Any]] = []
        if stale["media"]:
            marks = ",".join("?" for _ in stale["media"])
            stale_media_details = [row_dict(row) for row in conn.execute(
                f"""SELECT m.id,m.owner_type,m.owner_id,m.kind,e.id AS episode_id,
                e.episode_number,g.id AS segment_id,g.code AS segment_code,
                sh.order_index AS shot_order
                FROM media_versions m LEFT JOIN shots sh
                  ON m.owner_type='shot' AND sh.id=m.owner_id
                LEFT JOIN segments g ON g.id=sh.segment_id
                LEFT JOIN episodes e ON e.id=g.episode_id
                WHERE m.id IN ({marks}) ORDER BY e.episode_number,g.order_index,sh.order_index,m.id""",
                tuple(stale["media"]),
            )]
        stale_details = {
            "documents": [
                {
                    "id": item["id"], "title": item["title"],
                    "kind": item["kind"], "episode_id": item["episode_id"],
                    "reason": item["stale_reason"],
                }
                for item in documents if item["is_stale"]
            ],
            "media": stale_media_details,
        }
        return {
            "format": "script-weaver-production-package-v1",
            "project": {"id": project[0], "title": project[1], "revision": project[2]},
            "accepted": {"documents": documents, "assets": assets, "shots": shots},
            "missing": missing,
            "failed": failed,
            "stale": stale,
            "stale_details": stale_details,
            "excluded": excluded,
            "ready": ready,
        }

    def _validate_media(self, storage_path: str, expected_sha256: str) -> None:
        if not self.media_store:
            return
        path = Path(storage_path)
        try:
            if path.is_symlink():
                raise ValueError("accepted media object is a symlink; package not ready")
            resolved = path.resolve(strict=True)
            objects = self.media_store.objects.resolve(strict=True)
        except OSError as error:
            raise ValueError("accepted media object is missing; package not ready") from error
        if objects not in resolved.parents or not resolved.is_file():
            raise ValueError("accepted media object escaped Media Store; package not ready")
        if MediaStore._hash_file(resolved) != expected_sha256:
            raise ValueError("accepted media object is corrupt; package not ready")

    def build(self, project_id: str, expected_revision: int) -> dict[str, Any]:
        with self.db.write() as conn:
            facts = self._facts(conn, project_id)
            if facts["project"]["revision"] != expected_revision:
                raise ConflictError({project_id: facts["project"]["revision"]})
            exact = fingerprint(facts)
            existing = conn.execute(
                "SELECT * FROM production_packages WHERE project_id=? AND fingerprint=?",
                (project_id, exact),
            ).fetchone()
            if existing:
                return self._detail(row_dict(existing))
            version = conn.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM production_packages WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            package_id = f"pkg-{exact[:32]}"
            manifest = {"package_id": package_id, "version_number": version, **facts}
            encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            project_root = (self.root / project_id).resolve()
            root = self.root.resolve()
            if root not in project_root.parents:
                raise ValueError("production package path escaped its managed root")
            project_root.mkdir(parents=True, exist_ok=True)
            target = project_root / f"v{version:04d}"
            if target.exists():
                raise ConflictError({project_id: version}, "production package target already exists")
            temp = project_root / f".v{version:04d}.{secrets.token_hex(8)}.tmp"
            published = False
            try:
                temp.mkdir()
                manifest_path = temp / "manifest.json"
                with manifest_path.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
                published = True
                created = now()
                conn.execute(
                    """INSERT INTO production_packages(
                    id,project_id,version_number,fingerprint,manifest_json,storage_path,sha256,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (package_id, project_id, version, exact, dump(manifest),
                     str(target / "manifest.json"), digest, created),
                )
            except BaseException:
                shutil.rmtree(temp, ignore_errors=True)
                if published:
                    shutil.rmtree(target, ignore_errors=True)
                raise
        return self.get(package_id)

    def get(self, package_id: str) -> dict[str, Any]:
        row = self.db.connection.execute(
            "SELECT * FROM production_packages WHERE id=?", (package_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("production package not found")
        return self._detail(row_dict(row))

    def list(self, project_id: str) -> list[dict[str, Any]]:
        return [self._detail(row_dict(row)) for row in self.db.connection.execute(
            "SELECT * FROM production_packages WHERE project_id=? ORDER BY version_number DESC",
            (project_id,),
        )]

    @staticmethod
    def _detail(row: dict[str, Any]) -> dict[str, Any]:
        path = Path(row["storage_path"])
        if not path.is_file() or MediaDigest.hash(path) != row["sha256"]:
            raise ValueError("production package manifest is missing or corrupt")
        return row


class MediaDigest:
    @staticmethod
    def hash(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        return hasher.hexdigest()
