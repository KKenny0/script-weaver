"""Validated workbench contracts shared by HTTP and MCP boundaries."""

from __future__ import annotations

from typing import Annotated, Any, Literal

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


class ProjectIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    intake_kind: Literal["idea", "novel", "single_script", "multi_script"]
    content: str = Field(min_length=1, max_length=1_500_000)
    format: str = Field(default="short_drama", max_length=50)
    aspect_ratio: Literal["16:9", "9:16", "1:1", "21:9"] = "9:16"
    prompt_language: str = Field(default="zh", max_length=20)
    source_project_id: str | None = None
    continuation_kind: Literal["season"] | None = None

    @model_validator(mode="after")
    def continuation_is_paired(self) -> ProjectIntake:
        if (self.source_project_id is None) != (self.continuation_kind is None):
            raise ValueError("source_project_id and continuation_kind must be provided together")
        return self


class ProjectInputCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intake_kind: Literal["supplement", "revision"]
    title: str = Field(default="新的输入", min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=1_500_000)


class ProjectArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)


class DraftSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    content: str = Field(max_length=1_500_000)


class DocumentSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_document_revision: int = Field(ge=0)
    expected_draft_revision: int = Field(ge=0)


class DocumentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_document_revision: int = Field(ge=0)
    action: Literal["accept", "reject"]
    feedback: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def reject_requires_feedback(self) -> DocumentDecision:
        if self.action == "reject" and not (self.feedback or "").strip():
            raise ValueError("reject requires feedback")
        return self


class DocumentRestore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_revision: int = Field(ge=0)
    source_version_id: str


class ProjectionRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_document_revision: int = Field(ge=0)


class SingleScriptAdopt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_document_revision: int = Field(ge=0)
    expected_draft_revision: int = Field(ge=0)


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


class DocumentEditScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_revision: int = Field(ge=0, strict=True)
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)


class TaskStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    capability: str = Field(min_length=1, max_length=100)
    intent: str = Field(min_length=1, max_length=10000)
    surface_session_id: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    edit_scope: DocumentEditScope | None = None
    batch_key: str | None = Field(default=None, min_length=1, max_length=100)
    asset_id: str | None = None
    media_candidate_limit: int = Field(default=3, ge=1, le=8)
    parent_candidate_id: str | None = None
    skill_manifest: list[dict[str, str]] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def exactly_one_matching_skill(self) -> TaskStart:
        if self.edit_scope and (not self.document_id or self.capability != "short-drama-write"):
            raise ValueError("局部修订必须指定剧本文档并使用写作能力")
        if not self.skill_manifest:
            self.skill_manifest = [{"name": self.capability, "version": "local"}]
        if len(self.skill_manifest) != 1:
            raise ValueError("a task must pin exactly one skill")
        if self.skill_manifest[0].get("name") != self.capability:
            raise ValueError("pinned skill name must equal task capability")
        return self


class TaskClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_label: str = Field(min_length=1, max_length=100)


class TaskFail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    message: str = Field(min_length=1, max_length=10000)


class ChangeSetCreate(BaseModel):
    task_id: str
    run_id: str
    summary: str = Field(default="", max_length=10000)


class ChangeSetApply(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)


class AgentRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    skill_name: str = Field(min_length=1, max_length=100)
    skill_version: str = Field(min_length=1, max_length=100)


class DocumentCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["development", "screenplay", "review"]
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=1_500_000)
    episode_id: str | None = None


class DocumentVersionCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=1_500_000)


class OperationBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class SegmentCreateOperation(OperationBase):
    op: Literal["segment.create"]
    target_type: Literal["episode"] = "episode"
    payload: SegmentCreate


class DocumentCreateOperation(OperationBase):
    op: Literal["document.create"]
    target_type: Literal["project"] = "project"
    payload: DocumentCreatePayload


class DocumentVersionCreateOperation(OperationBase):
    op: Literal["document.version.create"]
    target_type: Literal["document"] = "document"
    target_id: str
    expected_revision: int = Field(ge=0)
    payload: DocumentVersionCreatePayload


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
    DocumentCreateOperation | DocumentVersionCreateOperation | SegmentCreateOperation
    | ShotCreateOperation | ShotUpdateOperation | ShotRetireOperation
    | ShotReorderOperation | AssetCreateOperation | AssetVersionCreateOperation
    | ReferenceBindOperation | ReferenceRebindOperation | ReferenceSetModeOperation
    | PromptVersionCreateOperation
)


class ProposalSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    summary: str = Field(default="", max_length=10000)
    operations: list[Annotated[DomainOperation, Field(discriminator="op")]] = Field(
        min_length=1, max_length=500
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
    parent_candidate_id: str | None = None
    target_asset_id: str | None = None


class MediaCandidateAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = None
    expected_asset_revision: int | None = Field(default=None, ge=0)
    expected_shot_revision: int | None = Field(default=None, ge=0)


class MediaCandidateImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    task_id: str
    run_id: str
    owner_type: Literal["shot", "asset"]
    owner_id: str
    prompt: str = Field(min_length=1, max_length=50000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: Literal["image/png"] = "image/png"
    parent_candidate_id: str | None = None
    target_asset_id: str | None = None


class GenerationConfirm(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)


class GenerationRun(BaseModel):
    confirmation_token: str = Field(min_length=1, max_length=200)


class H3Keyframe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_id: str
    binding_id: str
    frame_index: Literal[0, -1]


class H3VideoPrepare(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    shot_id: str
    target_asset_id: str
    prompt: str = Field(min_length=1, max_length=50000)
    keyframes: list[H3Keyframe] = Field(min_length=1, max_length=2)
    duration_seconds: int = Field(ge=4, le=15)

    @model_validator(mode="after")
    def validate_keyframes(self):
        expected = [0] if len(self.keyframes) == 1 else [0, -1]
        if [item.frame_index for item in self.keyframes] != expected:
            raise ValueError("keyframes must be ordered start frame 0, then optional end frame -1")
        if len({item.media_id for item in self.keyframes}) != len(self.keyframes):
            raise ValueError("keyframe media must be distinct")
        if len({item.binding_id for item in self.keyframes}) != len(self.keyframes):
            raise ValueError("keyframe bindings must be distinct")
        return self


class ProductionPackageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_project_revision: int = Field(ge=0)
