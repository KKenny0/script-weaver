"""Validated workbench contracts shared by HTTP and MCP boundaries."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkbenchError(RuntimeError):
    pass


class NotFoundError(WorkbenchError):
    pass


class ProjectMismatchError(WorkbenchError):
    pass


class ConflictError(WorkbenchError):
    def __init__(self, revisions: dict[str, int], message: str = "revision conflict"):
        super().__init__(message)
        self.revisions = revisions


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    format: str = Field(default="short_drama", max_length=50)
    aspect_ratio: Literal["16:9", "9:16", "1:1", "21:9"] = "9:16"
    prompt_language: str = Field(default="en", max_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LegacyImportRequest(BaseModel):
    state: dict[str, Any]


class EpisodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_number: int = Field(ge=1, le=10000)
    title: str = Field(default="", max_length=200)


class SceneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_index: int = Field(ge=0, le=100000)
    scene_number: str = Field(max_length=50)
    heading: dict[str, Any] = Field(default_factory=dict)
    blocks: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class SegmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str | None = None
    code: str = Field(min_length=1, max_length=50)
    order_index: int = Field(ge=0, le=100000)
    title: str = Field(default="", max_length=200)
    source_scene_ids: list[str] = Field(default_factory=list, max_length=1000)
    target_duration_seconds: float | None = Field(default=None, gt=0, le=86400)


class ShotCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str | None = None
    order_index: int = Field(ge=0, le=100000)
    duration_seconds: float = Field(default=3, gt=0, le=600)
    shot_size: str = Field(default="medium", min_length=1, max_length=50)
    camera_angle: str = Field(default="eye_level", min_length=1, max_length=50)
    camera_movement: str = Field(default="static", min_length=1, max_length=50)
    dialogue: str | None = Field(default=None, max_length=10000)
    sound: str | None = Field(default=None, max_length=10000)
    start_boundary: dict[str, Any] = Field(default_factory=dict)
    end_boundary: dict[str, Any] = Field(default_factory=dict)
    source_block_ids: list[str] = Field(default_factory=list, max_length=1000)
    image_prompt: str = Field(default="", max_length=50000)
    video_prompt: str = Field(default="", max_length=50000)


class ShotUpdateFields(BaseModel):
    """Shared by the HTTP PATCH body and ChangeSet shot.update payloads."""

    model_config = ConfigDict(extra="forbid")

    duration_seconds: float | None = Field(default=None, gt=0, le=600)
    shot_size: str | None = Field(default=None, min_length=1, max_length=50)
    camera_angle: str | None = Field(default=None, min_length=1, max_length=50)
    camera_movement: str | None = Field(default=None, min_length=1, max_length=50)
    dialogue: str | None = Field(default=None, max_length=10000)
    sound: str | None = Field(default=None, max_length=10000)
    start_boundary: dict[str, Any] | None = None
    end_boundary: dict[str, Any] | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> ShotUpdateFields:
        if not self.model_fields_set:
            raise ValueError("changes cannot be empty")
        return self


class DirectShotUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    changes: ShotUpdateFields


class ShotRestore(BaseModel):
    expected_revision: int = Field(ge=0)
    source_revision: int = Field(ge=0)


class AssetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["character", "scene", "style", "prop"]
    name: str = Field(min_length=1, max_length=200)
    content: dict[str, Any] = Field(default_factory=dict)


class AssetVersionCreate(BaseModel):
    expected_revision: int = Field(ge=0)
    content: dict[str, Any]


class AssetRestore(BaseModel):
    expected_revision: int = Field(ge=0)
    source_version_id: str


class BindingCreate(BaseModel):
    shot_id: str
    asset_version_id: str
    usage: Literal["character", "scene", "style", "prop"]
    binding_mode: Literal["frozen", "follow_latest"] = "frozen"


class BindingAction(BaseModel):
    expected_shot_revision: int = Field(ge=0)
    action: Literal["sync", "freeze", "restore"]
    asset_version_id: str | None = None


class SurfaceContextUpsert(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    project_id: str
    episode_id: str | None = None
    segment_id: str | None = None
    route: str = Field(default="storyboard", max_length=200)
    selected_shot_ids: list[str] = Field(default_factory=list, max_length=100)


class TaskStart(BaseModel):
    project_id: str
    capability: str = Field(min_length=1, max_length=100)
    intent: str = Field(min_length=1, max_length=10000)
    surface_session_id: str | None = None
    skill_manifest: list[dict[str, str]] = Field(default_factory=list, max_length=20)


class ChangeSetCreate(BaseModel):
    task_id: str
    run_id: str | None = None
    summary: str = Field(default="", max_length=10000)


class ChangeSetApply(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)


class AgentRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    skill_name: str = Field(min_length=1, max_length=100)
    skill_version: str = Field(min_length=1, max_length=100)


class OperationBase(BaseModel):
    target_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class SegmentCreateOperation(OperationBase):
    op: Literal["segment.create"]
    target_type: Literal["episode"] = "episode"
    payload: SegmentCreate


class ShotCreateOperation(OperationBase):
    op: Literal["shot.create"]
    target_type: Literal["segment"] = "segment"
    payload: ShotCreate


class ShotUpdateOperation(OperationBase):
    op: Literal["shot.update"]
    target_type: Literal["shot"] = "shot"
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: ShotUpdateFields


class ShotRetirePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShotRetireOperation(OperationBase):
    op: Literal["shot.retire"]
    target_type: Literal["shot"] = "shot"
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: ShotRetirePayload = Field(default_factory=ShotRetirePayload)


class ShotReorderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    order_index: int = Field(ge=0, le=100000)


class ShotReorderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: list[ShotReorderItem] = Field(min_length=1, max_length=1000)


class ShotReorderOperation(OperationBase):
    op: Literal["shot.reorder"]
    target_type: Literal["segment"] = "segment"
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: ShotReorderPayload


class AssetCreateOperation(OperationBase):
    op: Literal["asset.create"]
    target_type: Literal["project"] = "project"
    payload: AssetCreate


class AssetVersionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any]


class AssetVersionCreateOperation(OperationBase):
    op: Literal["asset.version.create"]
    target_type: Literal["asset"] = "asset"
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: AssetVersionPayload


class ReferenceBindPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: Literal["character", "scene", "style", "prop"]
    binding_mode: Literal["frozen", "follow_latest"] = "frozen"
    asset_version_id: str


class ReferenceRebindPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: Literal["character", "scene", "style", "prop"]
    asset_version_id: str
    asset_id: str | None = None


class ReferenceSetModePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: Literal["character", "scene", "style", "prop"]
    binding_mode: Literal["frozen", "follow_latest"]


class ReferenceBindOperation(OperationBase):
    op: Literal["reference.bind"]
    target_type: Literal["shot"] = "shot"
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: ReferenceBindPayload


class ReferenceRebindOperation(OperationBase):
    op: Literal["reference.rebind"]
    target_type: Literal["shot"] = "shot"
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: ReferenceRebindPayload


class ReferenceSetModeOperation(OperationBase):
    op: Literal["reference.set_mode"]
    target_type: Literal["shot"] = "shot"
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: ReferenceSetModePayload


class PromptVersionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["image", "video"]
    content: str = Field(min_length=1, max_length=50000)
    negative_content: str | None = Field(default=None, max_length=50000)


class PromptVersionCreateOperation(OperationBase):
    op: Literal["prompt.version.create"]
    target_type: Literal["shot", "segment", "asset"]
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: PromptVersionPayload


DomainOperation = (
    SegmentCreateOperation | ShotCreateOperation | ShotUpdateOperation | ShotRetireOperation
    | ShotReorderOperation | AssetCreateOperation | AssetVersionCreateOperation
    | ReferenceBindOperation | ReferenceRebindOperation | ReferenceSetModeOperation
    | PromptVersionCreateOperation
)


class OperationEnvelope(BaseModel):
    operation: DomainOperation = Field(discriminator="op")


class GenerationPrepare(BaseModel):
    project_id: str
    owner_type: Literal["shot", "asset"]
    owner_id: str
    prompt: str = Field(min_length=1, max_length=50000)
    count: int = Field(default=1, ge=1, le=8)
    model: str = Field(default="gpt-image-2", max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reference_asset_version_ids: list[str] = Field(default_factory=list, max_length=20)
    adapter: Literal["fake", "gpt-image-2"] = "fake"
    output_spec: dict[str, Any] = Field(default_factory=lambda: {"mime": "image/png"})


class GenerationConfirm(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)


class GenerationRun(BaseModel):
    confirmation_token: str = Field(min_length=1, max_length=200)
