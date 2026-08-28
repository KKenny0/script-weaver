"""Direct commands and queries for persistent workbench facts."""

from __future__ import annotations

import hashlib
import json
import os
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
    EpisodeCreate,
    NotFoundError,
    ProjectCreate,
    ProjectMismatchError,
    SceneCreate,
    SegmentCreate,
    ShotCreate,
    ShotUpdateFields,
    SurfaceContextUpsert,
    TaskStart,
)
from script_weaver.infrastructure.sqlite import Database

from .dependency_service import DependencyService


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
    """Stable SHA-256 over POSIX relative paths + file bytes, independent of creation order."""
    base = root if root is not None else skills_root()
    directory = base / name
    if not directory.is_dir():
        raise NotFoundError(f"skill not found: {name}")
    entries: list[tuple[str, bytes]] = []
    for file in directory.rglob("*"):
        if file.is_file():
            entries.append((file.relative_to(base).as_posix(), file.read_bytes()))
    hasher = hashlib.sha256()
    for relative_path, content in sorted(entries):
        hasher.update(relative_path.encode("utf-8"))
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
            conn.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,0,?,?)", (entity_id, data.title, data.format, data.aspect_ratio, data.prompt_language, dump(data.metadata), timestamp, timestamp))
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
            conn.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,0,?,?)", (project_id, project_data.title, project_data.format, project_data.aspect_ratio, project_data.prompt_language, dump(project_data.metadata), timestamp, timestamp))
            self._event(conn, project_id, "project", project_id, 0, "project.created", project_data.model_dump())
            conn.execute("INSERT INTO episodes VALUES(?,?,?,?,0,?,?)", (episode_id, project_id, episode_data.episode_number, episode_data.title, timestamp, timestamp))
            self._event(conn, project_id, "episode", episode_id, 0, "episode.created", episode_data.model_dump())
            segment_ids: dict[str, str] = {}
            for scene_data, segment_data, scene_key in scene_plans:
                scene_id = uid()
                conn.execute("INSERT INTO script_scenes VALUES(?,?,?,?,?,?,0)", (scene_id, episode_id, scene_data.order_index, scene_data.scene_number, dump(scene_data.heading), dump(scene_data.blocks)))
                self._event(conn, project_id, "script_scene", scene_id, 0, "script_scene.created", scene_data.model_dump())
                segment_id = uid()
                conn.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,0)", (segment_id, episode_id, segment_data.code, segment_data.order_index, segment_data.title, dump(segment_data.source_scene_ids), segment_data.target_duration_seconds))
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
                    conn.execute("INSERT INTO media_versions VALUES(?,?,?,?,?,?,?,?,?,NULL,1,0,?)", (uid(), "shot", shot_id, "external_reference", reference_url, hashlib.sha256(reference_url.encode()).hexdigest(), "application/x-external", None, None, now()))
            for capability, intent, manifest in task_plans:
                task_id, run_id = uid(), uid()
                selection = {"project_id": project_id, "selected_shot_ids": []}
                revisions = {project_id: 0}
                conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", (task_id, project_id, capability, intent, "started", now()))
                conn.execute("INSERT INTO task_snapshots VALUES(?,?,?,?,?)", (task_id, dump(selection), dump(revisions), dump(manifest), now()))
                conn.execute("INSERT INTO agent_runs VALUES(?,?,'running',?,?,?,?,NULL,NULL)", (run_id, task_id, capability, "legacy", manifest[0]["hash"], now()))
                conn.execute("INSERT INTO agent_run_events VALUES(?,0,'run.started','{}',?)", (run_id, now()))
        return {"project": self.get_project(project_id), "warnings": warnings}

    def list_projects(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return [row_dict(r) for r in self.db.connection.execute("SELECT * FROM projects ORDER BY updated_at DESC LIMIT ? OFFSET ?", (min(limit, 100), max(offset, 0)))]

    def get_project(self, project_id: str) -> dict[str, Any]:
        project = self._one("SELECT * FROM projects WHERE id=?", (project_id,))
        project["episodes"] = [row_dict(r) for r in self.db.connection.execute("SELECT * FROM episodes WHERE project_id=? ORDER BY episode_number", (project_id,))]
        project["assets"] = self.list_assets(project_id)
        return project

    def create_episode(self, project_id: str, data: EpisodeCreate) -> dict[str, Any]:
        entity_id, timestamp = uid(), now()
        with self.db.write() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise NotFoundError("project not found")
            conn.execute("INSERT INTO episodes VALUES(?,?,?,?,0,?,?)", (entity_id, project_id, data.episode_number, data.title, timestamp, timestamp))
            self._event(conn, project_id, "episode", entity_id, 0, "episode.created", data.model_dump())
        return self.get_episode(entity_id)

    def get_episode(self, episode_id: str) -> dict[str, Any]:
        episode = self._one("SELECT * FROM episodes WHERE id=?", (episode_id,))
        episode["script_scenes"] = [row_dict(r) for r in self.db.connection.execute("SELECT * FROM script_scenes WHERE episode_id=? ORDER BY order_index", (episode_id,))]
        episode["segments"] = [
            row_dict(r)
            for r in self.db.connection.execute(
                """SELECT g.*,(SELECT COUNT(*) FROM shots s WHERE s.segment_id=g.id AND s.status!='retired') shot_count
                FROM segments g WHERE g.episode_id=? ORDER BY g.order_index""",
                (episode_id,),
            )
        ]
        return episode

    def create_scene(self, episode_id: str, data: SceneCreate) -> dict[str, Any]:
        episode = self._one("SELECT project_id FROM episodes WHERE id=?", (episode_id,))
        entity_id = uid()
        with self.db.write() as conn:
            conn.execute("INSERT INTO script_scenes VALUES(?,?,?,?,?,?,0)", (entity_id, episode_id, data.order_index, data.scene_number, dump(data.heading), dump(data.blocks)))
            self._event(conn, episode["project_id"], "script_scene", entity_id, 0, "script_scene.created", data.model_dump())
        return self._one("SELECT * FROM script_scenes WHERE id=?", (entity_id,))

    def create_segment(self, episode_id: str, data: SegmentCreate) -> dict[str, Any]:
        episode = self._one("SELECT project_id FROM episodes WHERE id=?", (episode_id,))
        entity_id = uid()
        with self.db.write() as conn:
            conn.execute("INSERT INTO segments VALUES(?,?,?,?,?,?,?,0)", (entity_id, episode_id, data.code, data.order_index, data.title, dump(data.source_scene_ids), data.target_duration_seconds))
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
        conn.execute("INSERT INTO shots VALUES(?,?,?,'active',0,?,?,?,?,?,?,?,?,?)", (entity_id, segment_id, data.order_index, data.duration_seconds, data.shot_size, data.camera_angle, data.camera_movement, data.dialogue, data.sound, dump(data.start_boundary), dump(data.end_boundary), dump(data.source_block_ids)))
        timestamp = now()
        for kind, content in (("image", data.image_prompt), ("video", data.video_prompt)):
            if content:
                conn.execute("INSERT INTO prompt_versions VALUES(?,?,?,?,?,?,?,1,?,?)", (uid(), "shot", entity_id, kind, 1, content, None, source_changeset_id, timestamp))
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
        shot["bindings"] = [row_dict(r) for r in self.db.connection.execute("SELECT rb.*,a.name,a.kind FROM reference_bindings rb JOIN assets a ON a.id=rb.asset_id WHERE rb.shot_id=?", (shot_id,))]
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
            conn.execute("INSERT INTO reference_bindings VALUES(?,?,?,?,?,?,0,NULL)", (binding_id, data.shot_id, version["asset_id"], data.asset_version_id, data.usage, data.binding_mode))
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
        with self.db.write() as conn:
            row = conn.execute("SELECT revision FROM projects WHERE id=?", (data.project_id,)).fetchone()
            if not row:
                raise NotFoundError("project not found")
            manifest = []
            for entry in data.skill_manifest:
                name = (entry.get("name") or "").strip()
                if not name:
                    raise ValueError("skill manifest entry requires a name")
                manifest.append({"name": name, "version": entry.get("version") or "0", "hash": skill_tree_hash(name)})
            selection: dict[str, Any] = {"project_id": data.project_id, "selected_shot_ids": []}
            if data.surface_session_id:
                context_row = conn.execute("SELECT * FROM surface_contexts WHERE session_id=?", (data.surface_session_id,)).fetchone()
                if not context_row:
                    raise NotFoundError("surface context not found")
                context = row_dict(context_row)
                if context["project_id"] != data.project_id:
                    raise ProjectMismatchError(f"surface context belongs to project {context['project_id']}, task targets {data.project_id}")
                selection["route"] = context["route"]
                selection["episode_id"] = context["episode_id"]
                selection["segment_id"] = context["segment_id"]
                selection["selected_shot_ids"] = context["selected_shot_ids"]
                self._require_ancestry(conn, data.project_id, selection)
            revisions = {data.project_id: row[0]}
            for key, table in (("episode_id", "episodes"), ("segment_id", "segments")):
                if selection.get(key):
                    revisions[selection[key]] = conn.execute(f"SELECT revision FROM {table} WHERE id=?", (selection[key],)).fetchone()[0]
            for shot_id in selection["selected_shot_ids"]:
                revisions[shot_id] = conn.execute("SELECT revision FROM shots WHERE id=?", (shot_id,)).fetchone()[0]
            conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", (task_id, data.project_id, data.capability, data.intent, "started", now()))
            conn.execute("INSERT INTO task_snapshots VALUES(?,?,?,?,?)", (task_id, dump(selection), dump(revisions), dump(manifest), now()))
        return self.get_task_snapshot(task_id)

    def get_task_snapshot(self, task_id: str) -> dict[str, Any]:
        return self._one("SELECT t.*,s.selection_json,s.expected_revisions_json,s.skill_manifest_json,s.created_at snapshot_created_at FROM tasks t JOIN task_snapshots s ON s.task_id=t.id WHERE t.id=?", (task_id,))

    def create_agent_run(self, task_id: str, skill_name: str, skill_version: str) -> dict[str, Any]:
        with self.db.write() as conn:
            if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise NotFoundError("task not found")
            skill_hash = skill_tree_hash(skill_name)
            run_id = uid()
            conn.execute("INSERT INTO agent_runs VALUES(?,?,'running',?,?,?,?,NULL,NULL)", (run_id, task_id, skill_name, skill_version, skill_hash, now()))
            conn.execute("INSERT INTO agent_run_events VALUES(?,0,'run.started','{}',?)", (run_id, now()))
        return row_dict(self.db.connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone())

    def append_agent_run_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.db.write() as conn:
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),-1)+1 FROM agent_run_events WHERE run_id=?", (run_id,)).fetchone()[0]
            conn.execute("INSERT INTO agent_run_events VALUES(?,?,?,?,?)", (run_id, sequence, event_type[:100], dump(payload), now()))
        return row_dict(self.db.connection.execute("SELECT * FROM agent_run_events WHERE run_id=? AND sequence=?", (run_id, sequence)).fetchone())

    def search_project(self, project_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        pattern = f"%{query[:200]}%"
        sql = """SELECT 'shot' entity_type,s.id,s.dialogue text FROM shots s JOIN segments g ON g.id=s.segment_id JOIN episodes e ON e.id=g.episode_id WHERE e.project_id=? AND s.dialogue LIKE ? UNION ALL SELECT 'asset',id,name FROM assets WHERE project_id=? AND name LIKE ? LIMIT ?"""
        return [dict(r) for r in self.db.connection.execute(sql, (project_id, pattern, project_id, pattern, min(limit, 100)))]

    def list_events(self, project_id: str, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return [row_dict(r) for r in self.db.connection.execute("SELECT * FROM domain_events WHERE project_id=? AND id>? ORDER BY id LIMIT ?", (project_id, max(after_id, 0), min(limit, 500)))]

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
