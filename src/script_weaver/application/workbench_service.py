"""Direct commands and queries for persistent workbench facts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from script_weaver.core.types import ProjectState
from script_weaver.domain.models import (
    AssetCreate,
    AssetVersionCreate,
    BindingAction,
    BindingCreate,
    ConflictError,
    DocumentDecision,
    DocumentRestore,
    DocumentSubmit,
    DraftSave,
    EpisodeCreate,
    NotFoundError,
    ProjectCreate,
    ProjectInputCreate,
    ProjectIntake,
    ProjectMismatchError,
    ProjectionRetry,
    SceneCreate,
    SegmentCreate,
    ShotCreate,
    ShotUpdateFields,
    SurfaceContextUpsert,
    TaskFail,
    TaskStart,
)
from script_weaver.infrastructure.sqlite import Database

from .dependency_service import DependencyService
from .creative_projection import parse_episode_map, parse_screenplay, verify_episode_spans


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return str(uuid4())


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def row_dict(row) -> dict[str, Any]:
    if row is None:
        return {}
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json") and result[key] is not None:
            result[key[:-5]] = json.loads(result.pop(key))
    return result


def skills_root() -> Path:
    configured = os.getenv("SCRIPT_WEAVER_SKILLS_DIR")
    if configured:
        return Path(configured)
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        candidate = parent / ".agents" / "skills"
        if candidate.is_dir():
            return candidate
    return cwd / ".agents" / "skills"


def skill_tree_hash(name: str, root: Path | None = None) -> str:
    """Bounded SHA-256 over a direct, symlink-free skill directory."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name) or name in {".", ".."}:
        raise ValueError("skill name must be a safe single path segment")
    base = (root if root is not None else skills_root()).resolve(strict=True)
    raw_directory = base / name
    if raw_directory.is_symlink() or not raw_directory.is_dir():
        raise NotFoundError(f"skill not found: {name}")
    directory = raw_directory.resolve(strict=True)
    if directory.parent != base:
        raise ValueError("skill must be a direct child of the skills root")
    entries: list[tuple[str, bytes]] = []
    total_bytes = 0
    scanned_entries = 0
    for file in directory.rglob("*"):
        scanned_entries += 1
        if scanned_entries > 1024:
            raise ValueError("skill tree exceeds 1024 filesystem entries")
        if file.is_symlink():
            raise ValueError("skill trees may not contain symlinks")
        if file.is_file():
            if len(entries) >= 512:
                raise ValueError("skill tree exceeds 512 files")
            remaining = 8 * 1024 * 1024 - total_bytes
            with file.open("rb") as handle:
                content = handle.read(remaining + 1)
            total_bytes += len(content)
            if total_bytes > 8 * 1024 * 1024:
                raise ValueError("skill tree exceeds 8 MiB")
            entries.append((file.relative_to(directory).as_posix(), content))
    hasher = hashlib.sha256()
    for relative_path, content in sorted(entries):
        encoded = relative_path.encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()


_PROJECT_OF: dict[str, str] = {
    "project": "SELECT id FROM projects WHERE id=?",
    "episode": "SELECT project_id FROM episodes WHERE id=?",
    "segment": "SELECT e.project_id FROM segments s JOIN episodes e ON e.id=s.episode_id WHERE s.id=?",
    "shot": "SELECT e.project_id FROM shots s JOIN segments g ON g.id=s.segment_id JOIN episodes e ON e.id=g.episode_id WHERE s.id=?",
    "asset": "SELECT project_id FROM assets WHERE id=?",
    "asset_version": "SELECT a.project_id FROM asset_versions v JOIN assets a ON a.id=v.asset_id WHERE v.id=?",
    "reference_binding": "SELECT e.project_id FROM reference_bindings b JOIN shots s ON s.id=b.shot_id JOIN segments g ON g.id=s.segment_id JOIN episodes e ON e.id=g.episode_id WHERE b.id=?",
    "document": "SELECT project_id FROM creative_documents WHERE id=?",
    "document_version": "SELECT d.project_id FROM creative_document_versions v JOIN creative_documents d ON d.id=v.document_id WHERE v.id=?",
}


def project_for(conn, entity_type: str, entity_id: str) -> str:
    sql = _PROJECT_OF.get(entity_type)
    if not sql:
        raise ValueError(f"unknown entity type: {entity_type}")
    row = conn.execute(sql, (entity_id,)).fetchone()
    if not row:
        raise NotFoundError(f"{entity_type} not found: {entity_id}")
    return row[0]


def require_same_project(conn, project_id: str, entity_type: str, entity_id: str) -> None:
    actual = project_for(conn, entity_type, entity_id)
    if actual != project_id:
        raise ProjectMismatchError(f"{entity_type} {entity_id} belongs to project {actual}, expected {project_id}")


class WorkbenchService:
    def __init__(self, database: Database):
        self.db = database

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any]:
        row = self.db.connection.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError("entity not found")
        return row_dict(row)

    @staticmethod
    def _event(conn, project_id: str, aggregate_type: str, aggregate_id: str, revision: int, event_type: str, payload: dict[str, Any], causation_id: str | None = None, correlation_id: str | None = None) -> None:
        conn.execute(
            "INSERT INTO domain_events(project_id,aggregate_type,aggregate_id,revision,event_type,payload_json,causation_id,correlation_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, aggregate_type, aggregate_id, revision, event_type, dump(payload), causation_id, correlation_id, now()),
        )

    def create_project(self, data: ProjectCreate) -> dict[str, Any]:
        entity_id, timestamp = uid(), now()
        with self.db.write() as conn:
            conn.execute("INSERT INTO projects(id,title,format,aspect_ratio,prompt_language,metadata_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?)", (entity_id, data.title, data.format, data.aspect_ratio, data.prompt_language, dump(data.metadata), timestamp, timestamp))
            self._event(conn, entity_id, "project", entity_id, 0, "project.created", data.model_dump())
        return self.get_project(entity_id)

    def import_legacy(self, raw_state: dict[str, Any]) -> dict[str, Any]:
        """Plan the whole import in memory, validate every contract, then commit once."""
        state = ProjectState.model_validate(raw_state)
        warnings: list[dict[str, str]] = []
        metadata = {
            "legacy_project_id": state.meta.id,
            "user_input": state.user_input,
            "refined_idea": state.refined_idea,
            "outline": state.outline.model_dump(mode="json") if state.outline else None,
            "visual_highlights": [item.model_dump(mode="json") for item in state.visual_highlights or []],
            "review_history": [item.model_dump(mode="json") for item in state.review_history],
        }
        if state.review_history:
            warnings.append({"code": "LEGACY_REVIEWS_IN_METADATA", "severity": "info", "message": f"{len(state.review_history)} 条旧审查已保存到项目 metadata"})

        project_data = ProjectCreate(title=state.meta.title or "未命名导入项目", format=state.meta.format_type.value, metadata=metadata)
        episode_data = EpisodeCreate(episode_number=1, title=state.script.title if state.script else "EP01")
        scene_plans: list[tuple[SceneCreate, SegmentCreate, str]] = []
        if state.script:
            for index, scene in enumerate(state.script.scenes):
                scene_data = SceneCreate(order_index=index, scene_number=scene.heading.scene_number, heading=scene.heading.model_dump(mode="json"), blocks=[item.model_dump(mode="json") for item in scene.blocks])
                segment_data = SegmentCreate(code=f"E01-{index + 1:02d}", order_index=index, title=scene.heading.location, target_duration_seconds=scene.estimated_duration_seconds)
                scene_plans.append((scene_data, segment_data, scene.scene_id))
        if not scene_plans:
            scene_plans.append((SceneCreate(order_index=0, scene_number="1"), SegmentCreate(code="E01-01", order_index=0, title="导入分段"), ""))
            warnings.append({"code": "SCRIPT_SCENES_MISSING", "severity": "warning", "message": "旧项目没有剧本场次，已创建默认分段"})
        asset_plans: list[tuple[str, AssetCreate]] = []
        for character in state.characters or []:
            asset_plans.append(("character", AssetCreate(kind="character", name=character.name, content=character.model_dump(mode="json"))))
            for prop in character.key_props:
                if prop.strip():
                    asset_plans.append(("prop", AssetCreate(kind="prop", name=prop.strip(), content={"legacy_character_id": character.id})))
                else:
                    warnings.append({"code": "PROP_UNPARSEABLE", "severity": "warning", "message": f"角色 {character.name} 存在无法解析的空道具"})
        for scene in state.scenes or []:
            asset_plans.append(("scene", AssetCreate(kind="scene", name=scene.name, content=scene.model_dump(mode="json"))))
        if state.art_style:
            asset_plans.append(("style", AssetCreate(kind="style", name="视觉风格", content=state.art_style.model_dump(mode="json"))))
        shot_plans: list[tuple[str, ShotCreate, str | None]] = []
        for index, old_shot in enumerate(state.storyboard.shots if state.storyboard else []):
            shot_data = ShotCreate(order_index=old_shot.sequence_number or index, duration_seconds=old_shot.duration_seconds, shot_size=old_shot.shot_size.value, camera_angle=old_shot.camera_angle.value, camera_movement=old_shot.camera_movement.value, dialogue=old_shot.dialogue, sound=", ".join(old_shot.sound_effects), image_prompt=old_shot.image_prompt or "", video_prompt=old_shot.video_prompt or "")
            scene_keys = {key for *_, key in scene_plans}
            scene_key = old_shot.scene_id if old_shot.scene_id in scene_keys else next(iter(scene_keys))
            shot_plans.append((scene_key, shot_data, old_shot.reference_image_url))
        task_plans: list[tuple[str, str, list[dict[str, str]]]] = []
        for stage, binding in state.skill_bindings.items():
            skill_id = binding.skill_id or stage
            skill_hash = hashlib.sha256(dump(binding.model_dump(mode="json")).encode()).hexdigest()
            task_plans.append((skill_id, f"Imported legacy skill binding for {stage}", [{"name": skill_id, "version": "legacy", "hash": skill_hash}]))

        with self.db.write() as conn:
            duplicate = conn.execute("SELECT id FROM projects WHERE json_extract(metadata_json,'$.legacy_project_id')=?", (state.meta.id,)).fetchone()
            if duplicate:
                raise ConflictError({}, f"legacy project {state.meta.id} already imported")
            project_id, episode_id = uid(), uid()
            timestamp = now()
            conn.execute("INSERT INTO projects(id,title,format,aspect_ratio,prompt_language,metadata_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?)", (project_id, project_data.title, project_data.format, project_data.aspect_ratio, project_data.prompt_language, dump(project_data.metadata), timestamp, timestamp))
            self._event(conn, project_id, "project", project_id, 0, "project.created", project_data.model_dump())
            conn.execute("INSERT INTO episodes(id,project_id,episode_number,title,revision,created_at,updated_at) VALUES(?,?,?,?,0,?,?)", (episode_id, project_id, episode_data.episode_number, episode_data.title, timestamp, timestamp))
            self._event(conn, project_id, "episode", episode_id, 0, "episode.created", episode_data.model_dump())
            segment_ids: dict[str, str] = {}
            for scene_data, segment_data, scene_key in scene_plans:
                scene_id = uid()
                conn.execute("INSERT INTO script_scenes(id,episode_id,order_index,scene_number,heading_json,blocks_json,revision) VALUES(?,?,?,?,?,?,0)", (scene_id, episode_id, scene_data.order_index, scene_data.scene_number, dump(scene_data.heading), dump(scene_data.blocks)))
                self._event(conn, project_id, "script_scene", scene_id, 0, "script_scene.created", scene_data.model_dump())
                segment_id = uid()
                conn.execute("INSERT INTO segments(id,episode_id,code,order_index,title,source_scene_ids_json,target_duration_seconds,revision) VALUES(?,?,?,?,?,?,?,0)", (segment_id, episode_id, segment_data.code, segment_data.order_index, segment_data.title, dump(segment_data.source_scene_ids), segment_data.target_duration_seconds))
                self._event(conn, project_id, "segment", segment_id, 0, "segment.created", segment_data.model_dump())
                segment_ids[scene_key] = segment_id
            for _, asset_data in asset_plans:
                asset_id, version_id = uid(), uid()
                conn.execute("INSERT INTO assets VALUES(?,?,?,?,?,0)", (asset_id, project_id, asset_data.kind, asset_data.name, version_id))
                conn.execute("INSERT INTO asset_versions VALUES(?,?,1,?,NULL,?)", (version_id, asset_id, dump(asset_data.content), now()))
                self._event(conn, project_id, "asset", asset_id, 0, "asset.created", asset_data.model_dump())
            for scene_key, shot_data, reference_url in shot_plans:
                shot_id = uid()
                self._insert_shot(conn, shot_id, segment_ids[scene_key], shot_data, None)
                self._event(conn, project_id, "shot", shot_id, 0, "shot.created", shot_data.model_dump())
                if reference_url:
                    conn.execute("INSERT INTO media_versions(id,owner_type,owner_id,kind,storage_path,sha256,mime,width,height,duration_seconds,is_current,is_stale,created_at) VALUES(?,?,?,?,?,?,?,?,?,NULL,1,0,?)", (uid(), "shot", shot_id, "external_reference", reference_url, hashlib.sha256(reference_url.encode()).hexdigest(), "application/x-external", None, None, now()))
            for capability, intent, manifest in task_plans:
                task_id, run_id = uid(), uid()
                selection = {"project_id": project_id, "selected_shot_ids": []}
                revisions = {project_id: 0}
                conn.execute("INSERT INTO tasks(id,project_id,capability,intent,status,created_at) VALUES(?,?,?,?,?,?)", (task_id, project_id, capability, intent, "started", now()))
                conn.execute("INSERT INTO task_snapshots VALUES(?,?,?,?,?)", (task_id, dump(selection), dump(revisions), dump(manifest), now()))
                conn.execute("INSERT INTO agent_runs(id,task_id,status,skill_name,skill_version,skill_hash,started_at,finished_at,error_json,worker_label) VALUES(?,?,'RUNNING',?,?,?,?,NULL,NULL,'legacy-import')", (run_id, task_id, capability, "legacy", manifest[0]["hash"], now()))
                conn.execute("INSERT INTO agent_run_events VALUES(?,0,'run.started','{}',?)", (run_id, now()))
        return {"project": self.get_project(project_id), "warnings": warnings}

    def create_from_intake(self, data: ProjectIntake) -> dict[str, Any]:
        project_id, source_id, snapshot_id, timestamp = uid(), uid(), uid(), now()
        source_version_id, working_id = uid(), uid()
        working_kind = "screenplay" if data.intake_kind == "single_script" else "development"
        with self.db.write() as conn:
            metadata: dict[str, Any] = {}
            if data.source_project_id:
                if not conn.execute(
                    "SELECT 1 FROM projects WHERE id=?", (data.source_project_id,)
                ).fetchone():
                    raise NotFoundError("source project for season continuation not found")
                metadata = {
                    "source_project_id": data.source_project_id,
                    "continuation_kind": data.continuation_kind,
                }
            conn.execute(
                "INSERT INTO projects(id,title,format,aspect_ratio,prompt_language,metadata_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?)",
                (project_id, data.title, data.format, data.aspect_ratio,
                 data.prompt_language, dump(metadata), timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO creative_documents(id,project_id,episode_id,kind,title,current_version_id,revision,created_at,updated_at) VALUES(?,?,NULL,'source',?,?,0,?,?)",
                (source_id, project_id, f"{data.title} · 原始输入", source_version_id, timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO source_snapshots VALUES(?,?,?,?,?,?,?)",
                (snapshot_id, project_id, source_id, data.intake_kind, data.content, hashlib.sha256(data.content.encode()).hexdigest(), timestamp),
            )
            conn.execute(
                "INSERT INTO creative_document_versions(id,document_id,version_number,content,status,source_snapshot_id,decision_feedback,projection_status,created_at,decided_at,created_by) VALUES(?,?,1,?,'ACCEPTED',?,NULL,'not_applicable',?,?,'user')",
                (source_version_id, source_id, data.content, snapshot_id, timestamp, timestamp),
            )
            conn.execute(
                """INSERT INTO creative_documents(
                id,project_id,episode_id,kind,title,current_version_id,revision,created_at,updated_at,
                source_document_version_id,derived_from_ids_json
                ) VALUES(?,?,NULL,?,?,NULL,0,?,?,?,?)""",
                (working_id, project_id, working_kind, "剧本" if working_kind == "screenplay" else "故事开发稿",
                 timestamp, timestamp, source_version_id, dump([source_version_id])),
            )
            conn.execute(
                "INSERT INTO document_drafts VALUES(?,?,0,NULL,?)",
                (working_id, data.content, timestamp),
            )
            if data.intake_kind == "single_script":
                episode_id = uid()
                conn.execute(
                    """INSERT INTO episodes(
                    id,project_id,episode_number,title,revision,created_at,updated_at,
                    source_document_version_id,derived_from_ids_json
                    ) VALUES(?,?,?,?,0,?,?,?,?)""",
                    (episode_id, project_id, 1, "EP01", timestamp, timestamp,
                     source_version_id, dump([source_version_id])),
                )
                conn.execute("UPDATE creative_documents SET episode_id=? WHERE id=?", (episode_id, working_id))
            self._event(
                conn, project_id, "project", project_id, 0, "project.intake.created",
                {"intake_kind": data.intake_kind, "continuation_kind": data.continuation_kind},
            )
        return self.get_project(project_id)

    def list_projects(self, limit: int = 50, offset: int = 0, archived: bool = False) -> list[dict[str, Any]]:
        condition = "archived_at IS NOT NULL" if archived else "archived_at IS NULL"
        return [row_dict(r) for r in self.db.connection.execute(
            f"""SELECT p.*,
                (SELECT COUNT(*) FROM creative_documents d WHERE d.project_id=p.id) document_count,
                (SELECT COUNT(*) FROM episodes e WHERE e.project_id=p.id) episode_count
                FROM projects p WHERE {condition}
                ORDER BY p.updated_at DESC LIMIT ? OFFSET ?""",
            (min(limit, 100), max(offset, 0)),
        )]

    def set_project_archived(self, project_id: str, expected_revision: int, archived: bool) -> dict[str, Any]:
        with self.db.write() as conn:
            row = conn.execute("SELECT revision,archived_at FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise NotFoundError("project not found")
            if row[0] != expected_revision:
                raise ConflictError({project_id: row[0]})
            archived_at = now() if archived else None
            if (row[1] is not None) == archived:
                return self.get_project(project_id)
            conn.execute("UPDATE projects SET archived_at=?,revision=revision+1,updated_at=? WHERE id=?", (archived_at, now(), project_id))
            self._event(conn, project_id, "project", project_id, row[0] + 1, "project.archived" if archived else "project.restored", {})
        return self.get_project(project_id)

    def add_project_input(self, project_id: str, data: ProjectInputCreate) -> dict[str, Any]:
        document_id, snapshot_id, version_id, timestamp = uid(), uid(), uid(), now()
        with self.db.write() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise NotFoundError("project not found")
            conn.execute("INSERT INTO creative_documents(id,project_id,episode_id,kind,title,current_version_id,revision,created_at,updated_at) VALUES(?,?,NULL,'source',?,?,0,?,?)", (document_id, project_id, data.title, version_id, timestamp, timestamp))
            conn.execute("INSERT INTO source_snapshots VALUES(?,?,?,?,?,?,?)", (snapshot_id, project_id, document_id, data.intake_kind, data.content, hashlib.sha256(data.content.encode()).hexdigest(), timestamp))
            conn.execute("INSERT INTO creative_document_versions(id,document_id,version_number,content,status,source_snapshot_id,decision_feedback,projection_status,created_at,decided_at,created_by) VALUES(?,?,1,?,'ACCEPTED',?,NULL,'not_applicable',?,?,'user')", (version_id, document_id, data.content, snapshot_id, timestamp, timestamp))
            conn.execute("UPDATE projects SET revision=revision+1,updated_at=? WHERE id=?", (timestamp, project_id))
            revision = conn.execute("SELECT revision FROM projects WHERE id=?", (project_id,)).fetchone()[0]
            self._event(conn, project_id, "document", document_id, 0, "project.input.added", {"intake_kind": data.intake_kind})
        return {"document": self.get_document(document_id), "project_revision": revision}

    def get_project(self, project_id: str) -> dict[str, Any]:
        project = self._one("SELECT * FROM projects WHERE id=?", (project_id,))
        project["episodes"] = [row_dict(r) for r in self.db.connection.execute("SELECT * FROM episodes WHERE project_id=? ORDER BY episode_number", (project_id,))]
        project["assets"] = self.list_assets(project_id)
        project["documents"] = self.list_documents(project_id)
        return project

    def list_documents(self, project_id: str) -> list[dict[str, Any]]:
        if not self.db.connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise NotFoundError("project not found")
        return [self.get_document(row["id"]) for row in self.db.connection.execute("SELECT id FROM creative_documents WHERE project_id=? ORDER BY created_at", (project_id,))]

    def get_document(self, document_id: str) -> dict[str, Any]:
        document = self._one("SELECT * FROM creative_documents WHERE id=?", (document_id,))
        draft = self.db.connection.execute("SELECT * FROM document_drafts WHERE document_id=?", (document_id,)).fetchone()
        document["draft"] = row_dict(draft) if draft else None
        document["versions"] = [row_dict(row) for row in self.db.connection.execute("SELECT * FROM creative_document_versions WHERE document_id=? ORDER BY version_number DESC", (document_id,))]
        for version in document["versions"]:
            if version.get("projection_revision") is not None:
                version["projection_status"] = "projected"
        current = next((version for version in document["versions"] if version["id"] == document["current_version_id"]), None)
        document["drives_downstream"] = bool(
            current and document["kind"] == "screenplay" and current.get("projection_revision") is not None
        )
        return document

    @staticmethod
    def _require_episode_document_active(conn, document_id: str) -> None:
        row = conn.execute(
            """SELECT document.kind,document.episode_id,episode.status
            FROM creative_documents AS document
            LEFT JOIN episodes AS episode ON episode.id=document.episode_id
            WHERE document.id=?""",
            (document_id,),
        ).fetchone()
        if not row:
            raise NotFoundError("document not found")
        if row[0] == "screenplay" and row[1] and row[2] != "active":
            raise ConflictError({row[1]: 0}, "episode is removed from the accepted development map")

    def save_document_draft(self, document_id: str, data: DraftSave) -> dict[str, Any]:
        with self.db.write() as conn:
            document = conn.execute("SELECT project_id FROM creative_documents WHERE id=?", (document_id,)).fetchone()
            if not document:
                raise NotFoundError("document not found")
            self._require_episode_document_active(conn, document_id)
            draft = conn.execute("SELECT revision FROM document_drafts WHERE document_id=?", (document_id,)).fetchone()
            if not draft:
                raise ValueError("source documents are immutable")
            if draft[0] != data.expected_revision:
                raise ConflictError({document_id: draft[0]})
            timestamp = now()
            conn.execute("UPDATE document_drafts SET content=?,revision=revision+1,updated_at=? WHERE document_id=?", (data.content, timestamp, document_id))
            conn.execute("UPDATE creative_documents SET updated_at=? WHERE id=?", (timestamp, document_id))
            self._event(conn, document[0], "document", document_id, draft[0] + 1, "document.draft.saved", {})
        return self.get_document(document_id)

    @staticmethod
    def _submission_source_version(conn, document: Any) -> str | None:
        project_id, kind, episode_id, current_source = document[0], document[2], document[3], document[4]
        if kind == "development":
            row = conn.execute(
                """SELECT d.current_version_id FROM creative_documents d
                JOIN source_snapshots s ON s.document_id=d.id
                WHERE d.project_id=? AND d.kind='source'
                ORDER BY s.created_at DESC,d.created_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            return row[0] if row else current_source
        if kind in {"screenplay", "review"} and episode_id:
            episode = conn.execute(
                "SELECT source_document_version_id,status FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            if not episode:
                raise NotFoundError("document episode not found")
            if episode[1] != "active":
                raise ConflictError({episode_id: 0}, "episode is removed from the accepted development map")
            return episode[0]
        return current_source

    def submit_document(self, document_id: str, data: DocumentSubmit) -> dict[str, Any]:
        with self.db.write() as conn:
            document = conn.execute(
                """SELECT project_id,revision,kind,episode_id,source_document_version_id
                FROM creative_documents WHERE id=?""", (document_id,)
            ).fetchone()
            if not document:
                raise NotFoundError("document not found")
            self._require_episode_document_active(conn, document_id)
            draft = conn.execute("SELECT content,revision FROM document_drafts WHERE document_id=?", (document_id,)).fetchone()
            if not draft:
                raise ValueError("source documents cannot be submitted")
            revisions = {}
            if document[1] != data.expected_document_revision:
                revisions[document_id] = document[1]
            if draft[1] != data.expected_draft_revision:
                revisions[f"draft:{document_id}"] = draft[1]
            if revisions:
                raise ConflictError(revisions)
            if not draft[0].strip():
                raise ValueError("document content cannot be empty")
            if conn.execute("SELECT 1 FROM creative_document_versions WHERE document_id=? AND status='SUBMITTED'", (document_id,)).fetchone():
                raise ConflictError({document_id: document[1]}, "document already has a submitted version")
            number = conn.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM creative_document_versions WHERE document_id=?", (document_id,)).fetchone()[0]
            version_id, timestamp = uid(), now()
            projection = "not_projected" if document[2] == "screenplay" else "not_applicable"
            source_version_id = self._submission_source_version(conn, document)
            conn.execute(
                """INSERT INTO creative_document_versions(
                id,document_id,version_number,content,status,source_snapshot_id,decision_feedback,
                projection_status,created_at,decided_at,created_by,source_document_version_id,
                derived_from_ids_json
                ) VALUES(?,?,?,?,'SUBMITTED',NULL,NULL,?,?,NULL,'user',?,?)""",
                (version_id, document_id, number, draft[0], projection, timestamp,
                 source_version_id, dump([source_version_id] if source_version_id else [])),
            )
            conn.execute("UPDATE creative_documents SET revision=revision+1,updated_at=? WHERE id=?", (timestamp, document_id))
            conn.execute("UPDATE document_drafts SET base_version_id=? WHERE document_id=?", (version_id, document_id))
            self._event(conn, document[0], "document", document_id, document[1] + 1, "document.submitted", {"version_id": version_id, "version_number": number})
        return self.get_document(document_id)

    def decide_document_version(self, document_id: str, version_id: str, data: DocumentDecision) -> dict[str, Any]:
        with self.db.write() as conn:
            document = conn.execute("SELECT project_id,revision,kind,episode_id,current_version_id FROM creative_documents WHERE id=?", (document_id,)).fetchone()
            if not document:
                raise NotFoundError("document not found")
            self._require_episode_document_active(conn, document_id)
            if document[1] != data.expected_document_revision:
                raise ConflictError({document_id: document[1]})
            version = conn.execute(
                """SELECT document_id,status,content,source_document_version_id
                FROM creative_document_versions WHERE id=?""", (version_id,)
            ).fetchone()
            if not version:
                raise NotFoundError("document version not found")
            if version[0] != document_id:
                raise ProjectMismatchError("document version belongs to another document")
            if version[1] != "SUBMITTED":
                raise ConflictError({document_id: document[1]}, "only submitted versions can be decided")
            status, timestamp = ("ACCEPTED" if data.action == "accept" else "REJECTED"), now()
            if status == "ACCEPTED":
                snapshot = conn.execute("""SELECT s.selection_json FROM creative_document_versions v
                    JOIN changesets c ON c.id=v.source_changeset_id
                    JOIN task_snapshots s ON s.task_id=c.task_id WHERE v.id=?""", (version_id,)).fetchone()
                if snapshot:
                    self.require_scoped_draft_current(conn, json.loads(snapshot[0]))
            if status == "ACCEPTED" and document[2] == "development":
                if not version[3]:
                    raise ValueError("development candidate is missing source lineage")
                self._accept_development(
                    conn, document_id, version_id, version[3], document[0], version[2], timestamp
                )
            elif status == "ACCEPTED" and document[2] in {"screenplay", "review"}:
                if not document[3]:
                    raise ValueError(f"{document[2]} must belong to an episode before acceptance")
                episode = conn.execute(
                    "SELECT source_document_version_id,status FROM episodes WHERE id=?", (document[3],)
                ).fetchone()
                if not episode or episode[1] != "active":
                    raise ConflictError({document[3]: 0}, "episode is removed from the accepted development map")
                if not version[3] or version[3] != episode[0]:
                    raise ConflictError(
                        {document_id: document[1]},
                        f"{document[2]} candidate was derived from an older development version",
                    )
                if document[2] == "screenplay":
                    self._accept_screenplay(
                        conn, document_id, version_id, document[3], document[0], version[2], timestamp
                    )
            conn.execute("UPDATE creative_document_versions SET status=?,decision_feedback=?,decided_at=? WHERE id=?", (status, (data.feedback or "").strip() or None, timestamp, version_id))
            if status == "ACCEPTED":
                conn.execute(
                    """UPDATE creative_documents SET current_version_id=?,source_document_version_id=?,
                    derived_from_ids_json=?,revision=revision+1,updated_at=?,is_stale=0,stale_reason=NULL
                    WHERE id=?""",
                    (version_id, version[3], dump([version[3]] if version[3] else []), timestamp, document_id),
                )
            else:
                conn.execute("UPDATE creative_documents SET revision=revision+1,updated_at=? WHERE id=?", (timestamp, document_id))
            self._event(conn, document[0], "document", document_id, document[1] + 1, f"document.{data.action}ed", {"version_id": version_id, "feedback": data.feedback})
        return self.get_document(document_id)

    @staticmethod
    def _mark_episode_documents_stale(conn, episode_id: str, reason: str, timestamp: str) -> None:
        conn.execute(
            """UPDATE creative_documents SET is_stale=1,stale_reason=?,revision=revision+1,updated_at=?
            WHERE episode_id=? AND kind IN ('screenplay','review')""",
            (reason, timestamp, episode_id),
        )

    def _accept_development(
        self, conn, document_id: str, version_id: str, source_version_id: str,
        project_id: str, content: str, timestamp: str
    ) -> None:
        entries = parse_episode_map(content)
        source = conn.execute(
            """SELECT ss.id,ss.content FROM creative_document_versions v
            JOIN creative_documents d ON d.id=v.document_id
            JOIN source_snapshots ss ON ss.id=v.source_snapshot_id
            WHERE v.id=? AND d.project_id=? AND d.kind='source'""",
            (source_version_id, project_id),
        ).fetchone()
        if not source:
            raise ValueError("development source lineage does not resolve to a source snapshot")
        has_spans = any("source_span" in entry for entry in entries)
        if has_spans and not all("source_span" in entry for entry in entries):
            raise ValueError("every episode requires source_span when any source span is present")
        spans = verify_episode_spans(entries, source[1]) if has_spans else None
        existing = {
            row["episode_number"]: row_dict(row)
            for row in conn.execute("SELECT * FROM episodes WHERE project_id=?", (project_id,))
        }
        active_numbers = {entry["episode"] for entry in entries}
        for number, episode in existing.items():
            if number not in active_numbers:
                conn.execute(
                    """UPDATE episodes SET status='removed',is_stale=1,stale_reason=?,
                    revision=revision+1,updated_at=? WHERE id=?""",
                    ("removed from accepted development map", timestamp, episode["id"]),
                )
                self._mark_episode_documents_stale(
                    conn, episode["id"], "episode removed from development map", timestamp
                )
        for index, entry in enumerate(entries):
            episode = existing.get(entry["episode"])
            changed = bool(episode and episode.get("development_fingerprint") and episode["development_fingerprint"] != entry["fingerprint"])
            if episode:
                episode_id = episode["id"]
                if changed:
                    self._mark_episode_documents_stale(
                        conn, episode_id, "accepted development episode changed", timestamp
                    )
                conn.execute(
                    """UPDATE episodes SET title=?,source_document_version_id=?,development_fingerprint=?,
                    derived_from_ids_json=?,status='active',is_stale=0,stale_reason=NULL,
                    revision=revision+1,updated_at=?
                    WHERE id=?""",
                    (entry["title"], version_id, entry["fingerprint"], dump([version_id]), timestamp, episode_id),
                )
            else:
                episode_id = uid()
                conn.execute(
                    """INSERT INTO episodes(
                    id,project_id,episode_number,title,revision,created_at,updated_at,
                    source_document_version_id,development_fingerprint,derived_from_ids_json
                    ) VALUES(?,?,?,?,0,?,?,?,?,?)""",
                    (episode_id, project_id, entry["episode"], entry["title"], timestamp, timestamp, version_id, entry["fingerprint"], dump([version_id])),
                )
            screenplay = conn.execute(
                "SELECT id FROM creative_documents WHERE episode_id=? AND kind='screenplay'",
                (episode_id,),
            ).fetchone()
            draft_content = spans[index]["content"] if spans else f"# EP{entry['episode']:03d} {entry['title']}\n"
            if not screenplay:
                screenplay_id = uid()
                conn.execute(
                    """INSERT INTO creative_documents(
                    id,project_id,episode_id,kind,title,current_version_id,revision,created_at,updated_at,
                    source_document_version_id,derived_from_ids_json
                    ) VALUES(?,?,?,'screenplay',?,NULL,0,?,?,?,?)""",
                    (screenplay_id, project_id, episode_id, f"EP{entry['episode']:03d} 剧本", timestamp, timestamp, version_id, dump([version_id])),
                )
                conn.execute(
                    "INSERT INTO document_drafts VALUES(?,?,0,NULL,?)",
                    (screenplay_id, draft_content, timestamp),
                )
            if spans:
                span = spans[index]
                conn.execute(
                    """INSERT INTO source_episode_spans VALUES(?,?,?,?,?,?,?,?,?)""",
                    (uid(), source[0], episode_id, entry["episode"], span["start"], span["end"], span["sha256"], version_id, timestamp),
                )
        conn.execute(
            "UPDATE projects SET revision=revision+1,updated_at=? WHERE id=?", (timestamp, project_id)
        )

    def _accept_screenplay(
        self, conn, document_id: str, version_id: str, episode_id: str,
        project_id: str, content: str, timestamp: str,
    ) -> None:
        episode = conn.execute(
            "SELECT episode_number FROM episodes WHERE id=? AND project_id=?", (episode_id, project_id)
        ).fetchone()
        if not episode:
            raise ProjectMismatchError("screenplay episode belongs to another project")
        parsed = parse_screenplay(content, episode[0])
        projection_revision = conn.execute(
            """SELECT MAX(
              COALESCE((SELECT MAX(projection_revision) FROM creative_document_versions WHERE document_id=?),0),
              COALESCE((SELECT MAX(source_projection_revision) FROM script_scenes WHERE episode_id=?),0),
              COALESCE((SELECT MAX(source_projection_revision) FROM segments WHERE episode_id=?),0)
            ) + 1""",
            (document_id, episode_id, episode_id),
        ).fetchone()[0]
        old_segments = [
            row[0] for row in conn.execute(
                """SELECT g.id FROM segments g
                JOIN creative_document_versions v ON v.id=g.source_document_version_id
                WHERE g.episode_id=? AND g.status='active'
                  AND g.source_projection_revision IS NOT NULL AND v.document_id=?""",
                (episode_id, document_id),
            )
        ]
        old_scenes = [
            row[0] for row in conn.execute(
                """SELECT s.id FROM script_scenes s
                JOIN creative_document_versions v ON v.id=s.source_document_version_id
                WHERE s.episode_id=? AND s.status='active'
                  AND s.source_projection_revision IS NOT NULL AND v.document_id=?""",
                (episode_id, document_id),
            )
        ]
        if old_segments:
            placeholders = ",".join("?" for _ in old_segments)
            shot_ids = [
                row[0] for row in conn.execute(
                    f"SELECT id FROM shots WHERE segment_id IN ({placeholders})", old_segments
                )
            ]
            if shot_ids:
                shot_marks = ",".join("?" for _ in shot_ids)
                reason = f"screenplay projection replaced by r{projection_revision}"
                conn.execute(
                    f"UPDATE shots SET is_stale=1,stale_reason=? WHERE id IN ({shot_marks})",
                    (reason, *shot_ids),
                )
                for table, owner_clause in (
                    ("prompt_versions", "owner_type='shot' AND owner_id"),
                    ("media_versions", "owner_type='shot' AND owner_id"),
                ):
                    conn.execute(
                        f"UPDATE {table} SET is_stale=1,stale_reason=? WHERE {owner_clause} IN ({shot_marks})",
                        (reason, *shot_ids),
                    )
                conn.execute(
                    f"UPDATE reference_bindings SET is_stale=1,stale_reason=? WHERE shot_id IN ({shot_marks})",
                    (reason, *shot_ids),
                )
            conn.execute(
                f"UPDATE segments SET status='retired' WHERE id IN ({placeholders})", old_segments
            )
        if old_scenes:
            scene_marks = ",".join("?" for _ in old_scenes)
            conn.execute(
                f"UPDATE script_scenes SET status='retired' WHERE id IN ({scene_marks})",
                old_scenes,
            )
        for order_index, scene in enumerate(parsed["scenes"]):
            scene_id, segment_id = uid(), uid()
            lineage = dump([version_id])
            conn.execute(
                """INSERT INTO script_scenes(
                id,episode_id,order_index,scene_number,heading_json,blocks_json,revision,
                source_document_version_id,source_projection_revision,derived_from_ids_json,status
                ) VALUES(?,?,?,?,?,?,0,?,?,?,'active')""",
                (scene_id, episode_id, order_index, scene["scene_number"], dump(scene["heading"]), dump(scene["blocks"]), version_id, projection_revision, lineage),
            )
            conn.execute(
                """INSERT INTO segments(
                id,episode_id,code,order_index,title,source_scene_ids_json,target_duration_seconds,revision,
                source_document_version_id,source_projection_revision,derived_from_ids_json,status
                ) VALUES(?,?,?,?,?,?,NULL,0,?,?,?,'active')""",
                (segment_id, episode_id, f"{scene['scene_number']}@r{projection_revision}", order_index, scene["heading"]["location"], dump([scene_id]), version_id, projection_revision, lineage),
            )
        conn.execute(
            "UPDATE creative_document_versions SET projection_revision=? WHERE id=?",
            (projection_revision, version_id),
        )
        conn.execute(
            "UPDATE episodes SET revision=revision+1,updated_at=?,is_stale=0,stale_reason=NULL WHERE id=?",
            (timestamp, episode_id),
        )

    def restore_document_draft(self, document_id: str, data: DocumentRestore) -> dict[str, Any]:
        with self.db.write() as conn:
            self._require_episode_document_active(conn, document_id)
            draft = conn.execute("SELECT revision FROM document_drafts WHERE document_id=?", (document_id,)).fetchone()
            if not draft:
                raise NotFoundError("editable document not found")
            if draft[0] != data.expected_draft_revision:
                raise ConflictError({document_id: draft[0]})
            version = conn.execute("SELECT document_id,content FROM creative_document_versions WHERE id=?", (data.source_version_id,)).fetchone()
            if not version:
                raise NotFoundError("document version not found")
            if version[0] != document_id:
                raise ProjectMismatchError("document version belongs to another document")
            conn.execute("UPDATE document_drafts SET content=?,revision=revision+1,base_version_id=?,updated_at=? WHERE document_id=?", (version[1], data.source_version_id, now(), document_id))
        return self.get_document(document_id)

    def retry_screenplay_projection(self, document_id: str, data: ProjectionRetry) -> dict[str, Any]:
        with self.db.write() as conn:
            document = conn.execute(
                """SELECT project_id,episode_id,kind,revision,current_version_id
                FROM creative_documents WHERE id=?""", (document_id,)
            ).fetchone()
            if not document:
                raise NotFoundError("document not found")
            if document[2] != "screenplay" or not document[1] or not document[4]:
                raise ValueError("only a current episode screenplay can be projected")
            if document[3] != data.expected_document_revision:
                raise ConflictError({document_id: document[3]})
            version = conn.execute(
                """SELECT content,projection_revision,source_document_version_id
                FROM creative_document_versions WHERE id=? AND document_id=? AND status='ACCEPTED'""",
                (document[4], document_id),
            ).fetchone()
            if not version:
                raise NotFoundError("current accepted screenplay version not found")
            episode = conn.execute(
                "SELECT source_document_version_id,status FROM episodes WHERE id=?", (document[1],)
            ).fetchone()
            if not episode or episode[1] != "active":
                raise ConflictError({document[1]: 0}, "episode is removed from the accepted development map")
            if version[1] is not None:
                raise ConflictError({document_id: document[3]}, "current screenplay is already projected")
            if version[2] != episode[0]:
                raise ConflictError(
                    {document_id: document[3]},
                    "accepted screenplay was derived from an older development version",
                )
            timestamp = now()
            self._accept_screenplay(
                conn, document_id, document[4], document[1], document[0], version[0], timestamp
            )
            conn.execute(
                "UPDATE creative_documents SET revision=revision+1,updated_at=? WHERE id=?",
                (timestamp, document_id),
            )
            self._event(
                conn, document[0], "document", document_id, document[3] + 1,
                "screenplay.projected", {"version_id": document[4]},
            )
        return self.get_document(document_id)

    def adopt_single_script(self, document_id: str, data: DocumentSubmit) -> dict[str, Any]:
        document = self.get_document(document_id)
        if document["kind"] != "screenplay" or document["episode_id"] is None:
            raise ValueError("direct adoption is only available for a single-script intake")
        submitted = self.submit_document(document_id, data)
        candidate = next(version for version in submitted["versions"] if version["status"] == "SUBMITTED")
        return self.decide_document_version(document_id, candidate["id"], DocumentDecision(expected_document_revision=submitted["revision"], action="accept"))

    def create_episode(self, project_id: str, data: EpisodeCreate) -> dict[str, Any]:
        entity_id, timestamp = uid(), now()
        with self.db.write() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise NotFoundError("project not found")
            conn.execute("INSERT INTO episodes(id,project_id,episode_number,title,revision,created_at,updated_at) VALUES(?,?,?,?,0,?,?)", (entity_id, project_id, data.episode_number, data.title, timestamp, timestamp))
            self._event(conn, project_id, "episode", entity_id, 0, "episode.created", data.model_dump())
        return self.get_episode(entity_id)

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        episode = self._one("SELECT * FROM episodes WHERE id=?", (episode_id,))
        episode["script_scenes"] = [row_dict(r) for r in self.db.connection.execute("SELECT * FROM script_scenes WHERE episode_id=? AND status='active' ORDER BY order_index", (episode_id,))]
        episode["segments"] = [
            row_dict(r)
            for r in self.db.connection.execute(
                """SELECT g.*,(SELECT COUNT(*) FROM shots s WHERE s.segment_id=g.id AND s.status!='retired') shot_count
                FROM segments g WHERE g.episode_id=? AND g.status='active' ORDER BY g.order_index""",
                (episode_id,),
            )
        ]
        episode["projection_history"] = [
            row_dict(r) for r in self.db.connection.execute(
                """SELECT source_projection_revision,source_document_version_id,status,
                COUNT(*) segment_count FROM segments WHERE episode_id=?
                GROUP BY source_projection_revision,source_document_version_id,status
                ORDER BY source_projection_revision DESC""",
                (episode_id,),
            )
        ]
        episode["documents"] = [
            self.get_document(row[0]) for row in self.db.connection.execute(
                "SELECT id FROM creative_documents WHERE episode_id=? ORDER BY kind,created_at",
                (episode_id,),
            )
        ]
        return episode

    def create_scene(self, episode_id: str, data: SceneCreate) -> dict[str, Any]:
        episode = self._one("SELECT project_id FROM episodes WHERE id=?", (episode_id,))
        entity_id = uid()
        with self.db.write() as conn:
            conn.execute("INSERT INTO script_scenes(id,episode_id,order_index,scene_number,heading_json,blocks_json,revision) VALUES(?,?,?,?,?,?,0)", (entity_id, episode_id, data.order_index, data.scene_number, dump(data.heading), dump(data.blocks)))
            self._event(conn, episode["project_id"], "script_scene", entity_id, 0, "script_scene.created", data.model_dump())
        return self._one("SELECT * FROM script_scenes WHERE id=?", (entity_id,))

    def create_segment(self, episode_id: str, data: SegmentCreate) -> dict[str, Any]:
        episode = self._one("SELECT project_id FROM episodes WHERE id=?", (episode_id,))
        entity_id = uid()
        with self.db.write() as conn:
            conn.execute("INSERT INTO segments(id,episode_id,code,order_index,title,source_scene_ids_json,target_duration_seconds,revision) VALUES(?,?,?,?,?,?,?,0)", (entity_id, episode_id, data.code, data.order_index, data.title, dump(data.source_scene_ids), data.target_duration_seconds))
            self._event(conn, episode["project_id"], "segment", entity_id, 0, "segment.created", data.model_dump())
        return self.get_segment(entity_id)

    def get_segment(self, segment_id: str) -> dict[str, Any]:
        segment = self._one("SELECT * FROM segments WHERE id=?", (segment_id,))
        segment["shots"] = [self.get_shot(r["id"]) for r in self.db.connection.execute("SELECT id FROM shots WHERE segment_id=? ORDER BY order_index", (segment_id,))]
        return segment

    def create_shot(self, segment_id: str, data: ShotCreate, source_changeset_id: str | None = None) -> dict[str, Any]:
        project_id = self._one("SELECT e.project_id FROM segments s JOIN episodes e ON e.id=s.episode_id WHERE s.id=?", (segment_id,))["project_id"]
        entity_id = uid()
        with self.db.write() as conn:
            self._insert_shot(conn, entity_id, segment_id, data, source_changeset_id)
            self._event(conn, project_id, "shot", entity_id, 0, "shot.created", data.model_dump(), source_changeset_id)
        return self.get_shot(entity_id)

    @staticmethod
    def _insert_shot(conn, entity_id: str, segment_id: str, data: ShotCreate, source_changeset_id: str | None = None) -> None:
        lineage = conn.execute(
            "SELECT source_document_version_id,source_projection_revision,derived_from_ids_json FROM segments WHERE id=?",
            (segment_id,),
        ).fetchone()
        conn.execute("""INSERT INTO shots(
            id,segment_id,order_index,status,revision,duration_seconds,shot_size,camera_angle,
            camera_movement,dialogue,sound,start_boundary_json,end_boundary_json,source_block_ids_json,
            source_document_version_id,source_projection_revision,derived_from_ids_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            entity_id, segment_id, data.order_index, "active", 0, data.duration_seconds, data.shot_size,
            data.camera_angle, data.camera_movement, data.dialogue, data.sound,
            dump(data.start_boundary), dump(data.end_boundary), dump(data.source_block_ids),
            lineage[0], lineage[1], lineage[2],
        ))
        timestamp = now()
        for kind, content in (("image", data.image_prompt), ("video", data.video_prompt)):
            if content:
                conn.execute("""INSERT INTO prompt_versions(
                    id,owner_type,owner_id,kind,version_number,content,negative_content,is_current,
                    source_changeset_id,created_at,source_document_version_id,
                    source_projection_revision,derived_from_ids_json
                    ) VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?)""", (
                    uid(), "shot", entity_id, kind, 1, content, None, source_changeset_id,
                    timestamp, lineage[0], lineage[1], lineage[2],
                ))
        WorkbenchService._snapshot_shot(conn, entity_id, source_changeset_id)

    @staticmethod
    def _snapshot_shot(conn, shot_id: str, source_changeset_id: str | None = None) -> None:
        row = dict(conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone())
        conn.execute("INSERT OR IGNORE INTO shot_versions VALUES(?,?,?,?,?,?)", (uid(), shot_id, row["revision"], dump(row), source_changeset_id, now()))

    @staticmethod
    def shot_update_values(changes: ShotUpdateFields) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, value in changes.model_dump(exclude_unset=True, mode="json").items():
            if value is None and key in {"duration_seconds", "shot_size", "camera_angle", "camera_movement"}:
                raise ValueError(f"{key} cannot be null")
            column = f"{key}_json" if key.endswith("boundary") else key
            values[column] = dump(value) if column.endswith("_json") else value
        return values

    def get_shot(self, shot_id: str) -> dict[str, Any]:
        shot = self._one("SELECT * FROM shots WHERE id=?", (shot_id,))
        shot["prompts"] = [row_dict(r) for r in self.db.connection.execute("SELECT * FROM prompt_versions WHERE owner_type='shot' AND owner_id=? ORDER BY kind,version_number DESC", (shot_id,))]
        shot["bindings"] = [row_dict(r) for r in self.db.connection.execute(
            """SELECT rb.*,a.name,a.kind,a.current_version_id AS current_asset_version_id
            FROM reference_bindings rb JOIN assets a ON a.id=rb.asset_id
            WHERE rb.shot_id=?""", (shot_id,),
        )]
        shot["media"] = [row_dict(r) for r in self.db.connection.execute("SELECT * FROM media_versions WHERE owner_type='shot' AND owner_id=? ORDER BY created_at DESC", (shot_id,))]
        shot["versions"] = [row_dict(r) for r in self.db.connection.execute("SELECT * FROM shot_versions WHERE shot_id=? ORDER BY revision DESC", (shot_id,))]
        return shot

    def update_shot(self, shot_id: str, expected_revision: int, changes: ShotUpdateFields | dict[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, ShotUpdateFields):
            changes = ShotUpdateFields.model_validate(changes)
        with self.db.write() as conn:
            shot = row_dict(conn.execute("SELECT s.*,e.project_id FROM shots s JOIN segments g ON g.id=s.segment_id JOIN episodes e ON e.id=g.episode_id WHERE s.id=?", (shot_id,)).fetchone())
            if not shot:
                raise NotFoundError("shot not found")
            current = conn.execute("SELECT revision FROM shots WHERE id=?", (shot_id,)).fetchone()[0]
            if current != expected_revision:
                raise ConflictError({shot_id: current})
            values = self.shot_update_values(changes)
            if values:
                clause = ",".join(f"{key}=?" for key in values)
                conn.execute(f"UPDATE shots SET {clause},revision=revision+1 WHERE id=?", (*values.values(), shot_id))
                self._snapshot_shot(conn, shot_id)
                self._event(conn, shot["project_id"], "shot", shot_id, current + 1, "shot.updated", values)
        return self.get_shot(shot_id)

    def restore_shot(self, shot_id: str, expected_revision: int, source_revision: int) -> dict[str, Any]:
        shot = self._one("SELECT s.*,e.project_id FROM shots s JOIN segments g ON g.id=s.segment_id JOIN episodes e ON e.id=g.episode_id WHERE s.id=?", (shot_id,))
        version = self._one("SELECT content_json FROM shot_versions WHERE shot_id=? AND revision=?", (shot_id, source_revision))["content"]
        fields = ("order_index", "status", "duration_seconds", "shot_size", "camera_angle", "camera_movement", "dialogue", "sound", "start_boundary_json", "end_boundary_json", "source_block_ids_json")
        with self.db.write() as conn:
            current = conn.execute("SELECT revision FROM shots WHERE id=?", (shot_id,)).fetchone()[0]
            if current != expected_revision:
                raise ConflictError({shot_id: current})
            conn.execute(f"UPDATE shots SET {','.join(f'{field}=?' for field in fields)},revision=revision+1 WHERE id=?", (*(version[field] for field in fields), shot_id))
            self._snapshot_shot(conn, shot_id)
            self._event(conn, shot["project_id"], "shot", shot_id, current + 1, "shot.restored", {"source_revision": source_revision})
        return self.get_shot(shot_id)

    def create_asset(self, project_id: str, data: AssetCreate) -> dict[str, Any]:
        asset_id, version_id = uid(), uid()
        with self.db.write() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise NotFoundError("project not found")
            conn.execute("INSERT INTO assets VALUES(?,?,?,?,?,0)", (asset_id, project_id, data.kind, data.name, version_id))
            conn.execute("INSERT INTO asset_versions VALUES(?,?,1,?,NULL,?)", (version_id, asset_id, dump(data.content), now()))
            self._event(conn, project_id, "asset", asset_id, 0, "asset.created", data.model_dump())
        return self.get_asset(asset_id)

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        asset = self._one("SELECT * FROM assets WHERE id=?", (asset_id,))
        asset["versions"] = [row_dict(r) for r in self.db.connection.execute("SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version_number DESC", (asset_id,))]
        return asset

    def get_asset_version(self, version_id: str) -> dict[str, Any]:
        return self._one("SELECT * FROM asset_versions WHERE id=?", (version_id,))

    def list_assets(self, project_id: str) -> list[dict[str, Any]]:
        return [self.get_asset(r["id"]) for r in self.db.connection.execute("SELECT id FROM assets WHERE project_id=? ORDER BY kind,name", (project_id,))]

    def create_asset_version(self, asset_id: str, data: AssetVersionCreate) -> dict[str, Any]:
        asset = self._one("SELECT * FROM assets WHERE id=?", (asset_id,))
        version_id = uid()
        with self.db.write() as conn:
            current = conn.execute("SELECT revision,current_version_id FROM assets WHERE id=?", (asset_id,)).fetchone()
            if current[0] != data.expected_revision:
                raise ConflictError({asset_id: current[0]})
            number = conn.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM asset_versions WHERE asset_id=?", (asset_id,)).fetchone()[0]
            conn.execute("INSERT INTO asset_versions VALUES(?,?,?,?,?,?)", (version_id, asset_id, number, dump(data.content), current[1], now()))
            conn.execute("UPDATE assets SET current_version_id=?,revision=revision+1 WHERE id=?", (version_id, asset_id))
            DependencyService.mark_follow_latest_stale(conn, asset_id, version_id)
            self._event(conn, asset["project_id"], "asset", asset_id, current[0] + 1, "asset.version.created", {"version_id": version_id, "version_number": number})
        return self.get_asset_version(version_id)

    def restore_asset_version(self, asset_id: str, source_version_id: str, expected_revision: int) -> dict[str, Any]:
        source = self.get_asset_version(source_version_id)
        if source["asset_id"] != asset_id:
            raise ValueError("asset version does not belong to asset")
        return self.create_asset_version(asset_id, AssetVersionCreate(expected_revision=expected_revision, content=source["content"]))

    def bind_reference(self, data: BindingCreate) -> dict[str, Any]:
        binding_id = uid()
        with self.db.write() as conn:
            shot = row_dict(conn.execute("SELECT s.id,s.revision,e.project_id FROM shots s JOIN segments g ON g.id=s.segment_id JOIN episodes e ON e.id=g.episode_id WHERE s.id=?", (data.shot_id,)).fetchone())
            if not shot:
                raise NotFoundError("shot not found")
            version = conn.execute("SELECT v.id,v.asset_id,a.current_version_id FROM asset_versions v JOIN assets a ON a.id=v.asset_id WHERE v.id=?", (data.asset_version_id,)).fetchone()
            if not version:
                raise NotFoundError("asset version not found")
            require_same_project(conn, shot["project_id"], "asset_version", data.asset_version_id)
            if data.binding_mode == "follow_latest" and data.asset_version_id != version["current_version_id"]:
                raise ValueError("follow_latest binding requires the asset's current version")
            if conn.execute("SELECT 1 FROM reference_bindings WHERE shot_id=? AND asset_id=? AND usage=?", (data.shot_id, version["asset_id"], data.usage)).fetchone():
                raise ValueError(f"shot already binds asset for usage {data.usage}")
            conn.execute("""INSERT INTO reference_bindings(
                id,shot_id,asset_id,asset_version_id,usage,binding_mode,is_stale,stale_reason,
                source_document_version_id,source_projection_revision,derived_from_ids_json
                ) SELECT ?,?,?,?,?,?,0,NULL,source_document_version_id,
                source_projection_revision,derived_from_ids_json FROM shots WHERE id=?""", (
                binding_id, data.shot_id, version["asset_id"], data.asset_version_id,
                data.usage, data.binding_mode, data.shot_id,
            ))
            conn.execute("UPDATE shots SET revision=revision+1 WHERE id=?", (data.shot_id,))
            self._snapshot_shot(conn, data.shot_id)
            self._event(conn, shot["project_id"], "shot", data.shot_id, shot["revision"] + 1, "reference.bound", data.model_dump())
        return self._one("SELECT * FROM reference_bindings WHERE id=?", (binding_id,))

    def binding_action(self, binding_id: str, data: BindingAction) -> dict[str, Any]:
        with self.db.write() as conn:
            binding = row_dict(conn.execute(
                "SELECT rb.*,a.current_version_id,e.project_id FROM reference_bindings rb JOIN assets a ON a.id=rb.asset_id JOIN shots s ON s.id=rb.shot_id JOIN segments g ON g.id=s.segment_id JOIN episodes e ON e.id=g.episode_id WHERE rb.id=?",
                (binding_id,),
            ).fetchone())
            if not binding:
                raise NotFoundError("reference binding not found")
            revision = conn.execute("SELECT revision FROM shots WHERE id=?", (binding["shot_id"],)).fetchone()[0]
            if revision != data.expected_shot_revision:
                raise ConflictError({binding["shot_id"]: revision})
            if data.action == "sync" and data.asset_version_id:
                raise ValueError("sync always follows the asset's current version")
            if data.action == "freeze" and data.asset_version_id and data.asset_version_id != binding["asset_version_id"]:
                raise ValueError("freeze keeps the currently bound version; use restore to switch")
            if data.action == "restore":
                if not data.asset_version_id:
                    raise ValueError("restore requires asset_version_id")
                target = conn.execute("SELECT asset_id FROM asset_versions WHERE id=?", (data.asset_version_id,)).fetchone()
                if not target:
                    raise NotFoundError("asset version not found")
                if target[0] != binding["asset_id"]:
                    raise ValueError("restored version must belong to the bound asset")
            if data.action == "sync":
                version_id = binding["current_version_id"]
                mode = binding["binding_mode"]
            elif data.action == "freeze":
                version_id = binding["asset_version_id"]
                mode = "frozen"
            else:
                version_id = data.asset_version_id
                mode = "frozen"
            unchanged = version_id == binding["asset_version_id"] and mode == binding["binding_mode"] and not binding["is_stale"]
            if unchanged:
                return self._one("SELECT * FROM reference_bindings WHERE id=?", (binding_id,))
            conn.execute("UPDATE reference_bindings SET asset_version_id=?,binding_mode=?,is_stale=0,stale_reason=NULL WHERE id=?", (version_id, mode, binding_id))
            conn.execute("UPDATE shots SET revision=revision+1 WHERE id=?", (binding["shot_id"],))
            self._snapshot_shot(conn, binding["shot_id"])
            self._event(conn, binding["project_id"], "shot", binding["shot_id"], revision + 1, f"reference.{data.action}", {"binding_id": binding_id, "asset_version_id": version_id})
        return self._one("SELECT * FROM reference_bindings WHERE id=?", (binding_id,))

    def set_surface_context(self, data: SurfaceContextUpsert) -> dict[str, Any]:
        with self.db.write() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (data.project_id,)).fetchone():
                raise NotFoundError("project not found")
            episode_project = None
            if data.episode_id:
                episode_project = project_for(conn, "episode", data.episode_id)
                if episode_project != data.project_id:
                    raise ProjectMismatchError(f"episode {data.episode_id} belongs to project {episode_project}")
            segment_episode = None
            if data.segment_id:
                row = conn.execute("SELECT episode_id FROM segments WHERE id=?", (data.segment_id,)).fetchone()
                if not row:
                    raise NotFoundError(f"segment not found: {data.segment_id}")
                segment_episode = row[0]
                if not data.episode_id:
                    raise ValueError("segment_id requires episode_id")
                if segment_episode != data.episode_id:
                    raise ProjectMismatchError(f"segment {data.segment_id} belongs to episode {segment_episode}")
            for shot_id in data.selected_shot_ids:
                row = conn.execute("SELECT segment_id FROM shots WHERE id=?", (shot_id,)).fetchone()
                if not row:
                    raise NotFoundError(f"shot not found: {shot_id}")
                if data.segment_id and row[0] != data.segment_id:
                    raise ProjectMismatchError(f"shot {shot_id} does not belong to segment {data.segment_id}")
                if not data.segment_id:
                    raise ValueError("selected shots require segment_id")
            conn.execute("INSERT INTO surface_contexts VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET project_id=excluded.project_id,episode_id=excluded.episode_id,segment_id=excluded.segment_id,route=excluded.route,selected_shot_ids_json=excluded.selected_shot_ids_json,updated_at=excluded.updated_at", (data.session_id, data.project_id, data.episode_id, data.segment_id, data.route, dump(data.selected_shot_ids), now()))
        return self.get_active_context(data.session_id)

    def get_active_context(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id:
            return self._one("SELECT * FROM surface_contexts WHERE session_id=?", (session_id,))
        rows = list(self.db.connection.execute("SELECT * FROM surface_contexts ORDER BY updated_at DESC LIMIT 2"))
        if len(rows) != 1:
            return {"ambiguous": True, "contexts": [row_dict(r) for r in rows]}
        return row_dict(rows[0])

    @staticmethod
    def _require_ancestry(conn, project_id: str, context: dict[str, Any]) -> None:
        if context.get("episode_id"):
            require_same_project(conn, project_id, "episode", context["episode_id"])
        if context.get("segment_id"):
            row = conn.execute("SELECT episode_id FROM segments WHERE id=?", (context["segment_id"],)).fetchone()
            if not row:
                raise NotFoundError(f"segment not found: {context['segment_id']}")
            if context.get("episode_id") and row[0] != context["episode_id"]:
                raise ProjectMismatchError(f"segment {context['segment_id']} does not belong to episode {context['episode_id']}")
        for shot_id in context.get("selected_shot_ids", []):
            row = conn.execute("SELECT segment_id FROM shots WHERE id=?", (shot_id,)).fetchone()
            if not row:
                raise NotFoundError(f"shot not found: {shot_id}")
            if context.get("segment_id") and row[0] != context["segment_id"]:
                raise ProjectMismatchError(f"shot {shot_id} does not belong to segment {context['segment_id']}")

    def start_task(self, data: TaskStart) -> dict[str, Any]:
        task_id = uid()
        entry = data.skill_manifest[0]
        manifest = [{
            "name": data.capability,
            "version": entry.get("version") or "0",
            "hash": skill_tree_hash(data.capability),
        }]
        with self.db.write() as conn:
            row = conn.execute("SELECT revision FROM projects WHERE id=?", (data.project_id,)).fetchone()
            if not row:
                raise NotFoundError("project not found")
            selection: dict[str, Any] = {"project_id": data.project_id, "selected_shot_ids": []}
            selection["media_candidate_limit"] = data.media_candidate_limit
            revisions = {data.project_id: row[0]}
            if data.asset_id:
                require_same_project(conn, data.project_id, "asset", data.asset_id)
                asset_revision = conn.execute(
                    "SELECT revision FROM assets WHERE id=?", (data.asset_id,)
                ).fetchone()[0]
                selection["target_asset_id"] = data.asset_id
                revisions[data.asset_id] = asset_revision
            if data.parent_candidate_id:
                parent = conn.execute(
                    """SELECT project_id,owner_type,owner_id,sha256,candidate_status
                    FROM media_versions WHERE id=?""", (data.parent_candidate_id,),
                ).fetchone()
                if not parent:
                    raise NotFoundError("parent media candidate not found")
                if parent[0] != data.project_id or parent[4] not in {"candidate", "accepted"}:
                    raise ProjectMismatchError("parent candidate is outside this project")
                selection["parent_candidate_id"] = data.parent_candidate_id
                selection["parent_candidate_sha256"] = parent[3]
            if data.document_id:
                require_same_project(conn, data.project_id, "document", data.document_id)
                selection["document_id"] = data.document_id
                document_row = conn.execute(
                    """SELECT revision,episode_id,title,kind,current_version_id
                    FROM creative_documents WHERE id=?""", (data.document_id,)
                ).fetchone()
                revisions[data.document_id] = document_row[0]
                if document_row[1]:
                    episode_row = conn.execute(
                        "SELECT source_document_version_id,status FROM episodes WHERE id=?",
                        (document_row[1],),
                    ).fetchone()
                    if not episode_row or episode_row[1] != "active":
                        raise ConflictError(
                            {document_row[1]: 0},
                            "episode is removed from the accepted development map",
                        )
                    if document_row[3] in {"screenplay", "review"}:
                        if not data.document_version_id or data.document_version_id != episode_row[0]:
                            raise ProjectMismatchError(
                                "episode document task source must be the current accepted development version"
                            )
                    selection["episode_id"] = document_row[1]
                selection["target"] = {
                    "id": data.document_id, "episode_id": document_row[1],
                    "title": document_row[2], "kind": document_row[3],
                    "revision": document_row[0], "current_version_id": document_row[4],
                    "rejected_feedback": [row[0] for row in conn.execute(
                        """SELECT decision_feedback FROM creative_document_versions
                        WHERE document_id=? AND status='REJECTED' AND decision_feedback IS NOT NULL
                        ORDER BY version_number DESC LIMIT 5""", (data.document_id,),
                    )],
                }
            if data.edit_scope:
                draft = conn.execute("SELECT content,revision FROM document_drafts WHERE document_id=?", (data.document_id,)).fetchone()
                scope = data.edit_scope
                if selection["target"]["kind"] != "screenplay" or not draft:
                    raise ValueError("局部修订只支持有草稿的剧本")
                if draft[1] != scope.draft_revision:
                    raise ConflictError({data.document_id: draft[1]}, "草稿已更新，请重新选择要修改的文字")
                if not 0 <= scope.start < scope.end <= len(draft[0]):
                    raise ValueError("修改范围无效，请重新选择文字")
                selection["edit_scope"] = scope.model_dump()
                selection["target"]["draft_content"] = draft[0]
                selection["target"]["draft_revision"] = draft[1]
            if data.document_version_id:
                require_same_project(
                    conn, data.project_id, "document_version", data.document_version_id
                )
                selection["document_version_id"] = data.document_version_id
            if data.surface_session_id:
                context_row = conn.execute("SELECT * FROM surface_contexts WHERE session_id=?", (data.surface_session_id,)).fetchone()
                if not context_row:
                    raise NotFoundError("surface context not found")
                context = row_dict(context_row)
                if context["project_id"] != data.project_id:
                    raise ProjectMismatchError(f"surface context belongs to project {context['project_id']}, task targets {data.project_id}")
                if selection.get("episode_id") and context.get("episode_id") != selection["episode_id"]:
                    raise ProjectMismatchError(
                        "surface context episode does not match the target document episode"
                    )
                selection["route"] = context["route"]
                if context.get("episode_id"):
                    selection["episode_id"] = context["episode_id"]
                selection["segment_id"] = context["segment_id"]
                selection["selected_shot_ids"] = context["selected_shot_ids"]
                self._require_ancestry(conn, data.project_id, selection)
            if selection.get("target_asset_id") and selection["selected_shot_ids"]:
                placeholders = ",".join("?" for _ in selection["selected_shot_ids"])
                bound_count = conn.execute(
                    f"""SELECT COUNT(DISTINCT shot_id) FROM reference_bindings
                    WHERE asset_id=? AND shot_id IN ({placeholders})""",
                    (selection["target_asset_id"], *selection["selected_shot_ids"]),
                ).fetchone()[0]
                if bound_count != len(selection["selected_shot_ids"]):
                    raise ProjectMismatchError(
                        "target asset must already be bound to every frozen selected shot"
                    )
            for key, table in (("episode_id", "episodes"), ("segment_id", "segments")):
                if selection.get(key):
                    revisions[selection[key]] = conn.execute(f"SELECT revision FROM {table} WHERE id=?", (selection[key],)).fetchone()[0]
            for shot_id in selection["selected_shot_ids"]:
                revisions[shot_id] = conn.execute("SELECT revision FROM shots WHERE id=?", (shot_id,)).fetchone()[0]
            conn.execute("INSERT INTO tasks(id,project_id,capability,intent,status,created_at,batch_key) VALUES(?,?,?,?,?,?,?)", (task_id, data.project_id, data.capability, data.intent, "QUEUED", now(), data.batch_key))
            conn.execute("INSERT INTO task_snapshots VALUES(?,?,?,?,?)", (task_id, dump(selection), dump(revisions), dump(manifest), now()))
        return self.get_task_snapshot(task_id)

    def get_task_snapshot(self, task_id: str) -> dict[str, Any]:
        return self._one("SELECT t.*,s.selection_json,s.expected_revisions_json,s.skill_manifest_json,s.created_at snapshot_created_at FROM tasks t JOIN task_snapshots s ON s.task_id=t.id WHERE t.id=?", (task_id,))

    @staticmethod
    def require_scoped_draft_current(conn, selection: dict[str, Any]) -> None:
        scope = selection.get("edit_scope")
        if not scope:
            return
        draft = conn.execute("SELECT revision FROM document_drafts WHERE document_id=?", (selection["document_id"],)).fetchone()
        if not draft or draft[0] != scope["draft_revision"]:
            raise ConflictError({selection["document_id"]: draft[0] if draft else -1}, "草稿在任务发起后已更新，请保留当前内容并重新发起局部修订")

    def list_tasks(self, project_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses, values = [], []
        if project_id:
            clauses.append("t.project_id=?")
            values.append(project_id)
        if status:
            clauses.append("t.status=?")
            values.append(status.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.connection.execute(
            f"""SELECT t.*,s.selection_json,s.expected_revisions_json,s.skill_manifest_json,
            r.id run_id,r.status run_status,r.worker_label
            FROM tasks t JOIN task_snapshots s ON s.task_id=t.id
            LEFT JOIN agent_runs r ON r.task_id=t.id
            {where} ORDER BY t.created_at DESC LIMIT 100""",
            values,
        )
        return [row_dict(row) for row in rows]

    def claim_task(self, task_id: str, worker_label: str) -> dict[str, Any]:
        snapshot = self.get_task_snapshot(task_id)
        manifest = snapshot["skill_manifest"]
        if len(manifest) != 1 or manifest[0].get("name") != snapshot["capability"]:
            raise ValueError("task snapshot must pin exactly its capability skill")
        current_hash = skill_tree_hash(manifest[0]["name"])
        with self.db.write() as conn:
            task = conn.execute(
                """SELECT t.project_id,t.status,s.skill_manifest_json
                FROM tasks t JOIN task_snapshots s ON s.task_id=t.id WHERE t.id=?""",
                (task_id,),
            ).fetchone()
            if not task:
                raise NotFoundError("task not found")
            if task[1] != "QUEUED":
                raise ConflictError({task_id: 0}, f"task is {task[1]}, not QUEUED")
            manifest = json.loads(task[2])
            if len(manifest) != 1 or manifest[0].get("name") != snapshot["capability"]:
                raise ValueError("task snapshot must pin exactly its capability skill")
            skill = manifest[0]
            if current_hash != skill["hash"]:
                raise ConflictError({task_id: 0}, "pinned skill tree changed after task creation")
            run_id, timestamp = uid(), now()
            updated = conn.execute(
                "UPDATE tasks SET status='RUNNING' WHERE id=? AND status='QUEUED'", (task_id,)
            )
            if updated.rowcount != 1:
                raise ConflictError({task_id: 0}, "task was claimed concurrently")
            conn.execute(
                """INSERT INTO agent_runs(
                id,task_id,status,skill_name,skill_version,skill_hash,started_at,
                finished_at,error_json,worker_label
                ) VALUES(?,?,'RUNNING',?,?,?,?,NULL,NULL,?)""",
                (
                    run_id, task_id, skill["name"], skill["version"], skill["hash"],
                    timestamp, worker_label,
                ),
            )
            conn.execute(
                "INSERT INTO agent_run_events VALUES(?,0,'run.started','{}',?)",
                (run_id, timestamp),
            )
            self._event(
                conn, task[0], "task", task_id, 0, "task.claimed",
                {"run_id": run_id, "worker_label": worker_label}, None, task_id,
            )
        return row_dict(
            self.db.connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        )

    def get_task_context(
        self, task_id: str, section: str = "all", cursor: int = 0, limit_chars: int = 200_000
    ) -> dict[str, Any]:
        snapshot = self.get_task_snapshot(task_id)
        selection = snapshot["selection"]
        result: dict[str, Any] = {
            "task": {key: snapshot[key] for key in ("id", "project_id", "capability", "intent", "status")},
            "selection": selection,
            "expected_revisions": snapshot["expected_revisions"],
            "skill_manifest": snapshot["skill_manifest"],
        }
        version_id = selection.get("document_version_id")
        if section in {"all", "source"} and version_id:
            version = self.db.connection.execute(
                """SELECT v.id,v.document_id,v.version_number,v.content,d.title,d.kind
                FROM creative_document_versions v JOIN creative_documents d ON d.id=v.document_id
                WHERE v.id=?""",
                (version_id,),
            ).fetchone()
            if not version:
                raise NotFoundError("pinned document version not found")
            content = version[3]
            start = max(cursor, 0)
            end = min(start + min(max(limit_chars, 1), 220_000), len(content))
            result["source"] = {
                "version_id": version[0], "document_id": version[1],
                "version_number": version[2], "title": version[4], "kind": version[5],
                "content": content[start:end], "cursor": start,
                "next_cursor": end if end < len(content) else None,
            }
        if section in {"all", "target"} and selection.get("document_id"):
            result["target"] = selection["target"]
        return result

    def fail_task(self, task_id: str, data: TaskFail) -> dict[str, Any]:
        with self.db.write() as conn:
            task = conn.execute("SELECT project_id,status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise NotFoundError("task not found")
            run = conn.execute("SELECT task_id,status FROM agent_runs WHERE id=?", (data.run_id,)).fetchone()
            if not run:
                raise NotFoundError("agent run not found")
            if run[0] != task_id:
                raise ProjectMismatchError("agent run belongs to another task")
            if task[1] != "RUNNING" or run[1] != "RUNNING":
                raise ConflictError({task_id: 0}, "only a running task can fail")
            timestamp = now()
            conn.execute("UPDATE tasks SET status='FAILED' WHERE id=?", (task_id,))
            conn.execute(
                "UPDATE agent_runs SET status='FAILED',finished_at=?,error_json=? WHERE id=?",
                (timestamp, dump({"message": data.message}), data.run_id),
            )
            conn.execute(
                "INSERT INTO agent_run_events VALUES(?,1,'run.failed',?,?)",
                (data.run_id, dump({"message": data.message}), timestamp),
            )
            self._event(
                conn, task[0], "task", task_id, 0, "task.failed",
                {"run_id": data.run_id, "message": data.message}, None, task_id,
            )
        return self.get_task_snapshot(task_id)

    def create_agent_run(self, task_id: str, skill_name: str, skill_version: str) -> dict[str, Any]:
        snapshot = self.get_task_snapshot(task_id)
        pinned = snapshot["skill_manifest"][0]
        if (skill_name, skill_version) != (pinned["name"], pinned["version"]):
            raise ValueError("agent run must use the task's pinned skill")
        return self.claim_task(task_id, "legacy-client")

    def append_agent_run_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.db.write() as conn:
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),-1)+1 FROM agent_run_events WHERE run_id=?", (run_id,)).fetchone()[0]
            conn.execute("INSERT INTO agent_run_events VALUES(?,?,?,?,?)", (run_id, sequence, event_type[:100], dump(payload), now()))
        return row_dict(self.db.connection.execute("SELECT * FROM agent_run_events WHERE run_id=? AND sequence=?", (run_id, sequence)).fetchone())

    def search_project(self, project_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        pattern = f"%{query[:200]}%"
        sql = """SELECT 'document' entity_type,v.id,d.title || ' · v' || v.version_number text
        FROM creative_document_versions v JOIN creative_documents d ON d.id=v.document_id
        WHERE d.project_id=? AND (d.title LIKE ? OR v.content LIKE ?)
        UNION ALL SELECT 'shot',s.id,s.dialogue FROM shots s JOIN segments g ON g.id=s.segment_id
        JOIN episodes e ON e.id=g.episode_id WHERE e.project_id=? AND s.dialogue LIKE ?
        UNION ALL SELECT 'asset',id,name FROM assets WHERE project_id=? AND name LIKE ? LIMIT ?"""
        values = (project_id, pattern, pattern, project_id, pattern, project_id, pattern, min(limit, 100))
        return [dict(r) for r in self.db.connection.execute(sql, values)]

    def list_events(self, project_id: str, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return [row_dict(r) for r in self.db.connection.execute("SELECT * FROM domain_events WHERE project_id=? AND id>? ORDER BY id LIMIT ?", (project_id, max(after_id, 0), min(limit, 500)))]

    def latest_event_id(self, project_id: str) -> int:
        row = self.db.connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM domain_events WHERE project_id=?", (project_id,)
        ).fetchone()
        return int(row[0])

    def render_shot_preview(self, shot_id: str) -> dict[str, Any]:
        shot = self.get_shot(shot_id)
        return {"type": "shot_preview", "shot": shot, "deep_link": f"/?shot={shot_id}"}

    def render_contact_sheet(self, segment_id: str) -> dict[str, Any]:
        segment = self.get_segment(segment_id)
        return {"type": "segment_contact_sheet", "segment_id": segment_id, "shots": [{"id": s["id"], "order_index": s["order_index"], "prompts": s["prompts"], "media": s["media"]} for s in segment["shots"]]}

    def compare_shot_versions(self, shot_id: str) -> dict[str, Any]:
        shot = self.get_shot(shot_id)
        return {"shot_id": shot_id, "current_revision": shot["revision"], "prompt_history": shot["prompts"]}

    def render_asset_board(self, project_id: str) -> dict[str, Any]:
        return {"type": "asset_board", "assets": self.list_assets(project_id)}

    def deep_link(self, entity_type: str, entity_id: str) -> dict[str, str]:
        return {"url": f"/?{entity_type}={entity_id}"}
