"""Loopback-only authenticated FastAPI daemon; the sole SQLite writer."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from script_weaver.application.changeset_service import ChangeSetService
from script_weaver.application.generation_service import GenerationJobService
from script_weaver.application.h3_service import H3VideoService
from script_weaver.application.production_package_service import ProductionPackageService
from script_weaver.application.workbench_service import WorkbenchService
from script_weaver.domain.models import (
    AgentRunCreate,
    AssetCreate,
    AssetRestore,
    AssetVersionCreate,
    BindingAction,
    BindingCreate,
    ChangeSetApply,
    ChangeSetCreate,
    ConflictError,
    DirectShotUpdate,
    DocumentDecision,
    DocumentRestore,
    DocumentSubmit,
    DraftSave,
    EpisodeCreate,
    GenerationConfirm,
    GenerationPrepare,
    GenerationRun,
    H3VideoPrepare,
    LegacyImportRequest,
    MediaCandidateAccept,
    MediaCandidateImport,
    NotFoundError,
    OperationEnvelope,
    ProjectCreate,
    ProjectArchive,
    ProjectInputCreate,
    ProjectIntake,
    ProjectMismatchError,
    ProductionPackageCreate,
    ProjectionRetry,
    ProposalSubmit,
    SceneCreate,
    SegmentCreate,
    ShotCreate,
    ShotRestore,
    SurfaceContextUpsert,
    TaskStart,
    TaskClaim,
    TaskFail,
)
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database, default_data_dir
from script_weaver.runtime import require_supported_python

MAX_BODY_BYTES = 2 * 1024 * 1024


class BodyLimitMiddleware:
    """Pure-ASGI body cap that counts real bytes instead of trusting Content-Length."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() in {"GET", "HEAD"}:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) < 0:
                        raise ValueError
                except ValueError:
                    response = JSONResponse({"detail": "invalid content-length header"}, status_code=400)
                    await response(scope, receive, send)
                    return
                break
        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if chunk:
                # Cap the buffered copy even if the ASGI server hands us one huge message.
                remaining = self.max_bytes + 1 - len(buffered)
                buffered.extend(chunk[:remaining] if len(chunk) > remaining else chunk)
            if len(buffered) > self.max_bytes:
                response = JSONResponse({"detail": "request body too large"}, status_code=413)
                await response(scope, receive, send)
                return
            if not message.get("more_body"):
                break
        replayed = [{"type": "http.request", "body": bytes(buffered), "more_body": False}]

        async def replay_receive():
            if replayed:
                return replayed.pop(0)
            await asyncio.sleep(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


def runtime_token_path() -> Path:
    configured = os.getenv("SCRIPT_WEAVER_TOKEN_FILE")
    return Path(configured) if configured else default_data_dir() / "runtime.token"


def agent_token_path() -> Path:
    configured = os.getenv("SCRIPT_WEAVER_AGENT_TOKEN_FILE")
    return Path(configured) if configured else default_data_dir() / "agent.token"


def rotate_token(path: Path) -> str:
    token = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def create_app(
    database_path: str | Path | None = None,
    media_root: str | Path | None = None,
    token: str | None = None,
    agent_token: str | None = None,
    h3_url: str | None = None,
    h3_shared_root: str | Path | None = None,
    production_package_root: str | Path | None = None,
) -> FastAPI:
    db = Database(database_path)
    workbench = WorkbenchService(db)
    changesets = ChangeSetService(db)
    media_store = MediaStore(media_root)
    generation = GenerationJobService(db, media_store)
    h3 = H3VideoService(db, media_store, generation, h3_url, h3_shared_root)
    packages = ProductionPackageService(db, production_package_root, media_store)
    creator_path, proposal_path = runtime_token_path(), agent_token_path()
    if creator_path.resolve() == proposal_path.resolve():
        raise ValueError("creator and agent token files must be different")
    creator_token = token or rotate_token(creator_path)
    proposal_token = agent_token or rotate_token(proposal_path)
    if secrets.compare_digest(creator_token, proposal_token):
        raise ValueError("creator and agent tokens must be different")
    app = FastAPI(title="script-weaverd", version="1.0.0")
    app.state.db, app.state.workbench, app.state.changesets = db, workbench, changesets
    app.state.generation, app.state.h3, app.state.packages = generation, h3, packages
    app.add_middleware(BodyLimitMiddleware)

    def token_capability(authorization: str | None) -> str | None:
        if authorization and secrets.compare_digest(authorization, f"Bearer {creator_token}"):
            return "creator"
        if authorization and secrets.compare_digest(authorization, f"Bearer {proposal_token}"):
            return "agent"
        return None

    def authorize_read(authorization: Annotated[str | None, Header()] = None) -> None:
        if token_capability(authorization) is None:
            raise HTTPException(status_code=401, detail="invalid daemon session token")

    def authorize_creator(authorization: Annotated[str | None, Header()] = None) -> None:
        capability = token_capability(authorization)
        if capability is None:
            raise HTTPException(status_code=401, detail="invalid daemon session token")
        if capability != "creator":
            raise HTTPException(status_code=403, detail="creator capability required")

    def authorize_agent_action(authorization: Annotated[str | None, Header()] = None) -> None:
        if token_capability(authorization) is None:
            raise HTTPException(status_code=401, detail="invalid daemon session token")

    def authorize_agent(authorization: Annotated[str | None, Header()] = None) -> None:
        capability = token_capability(authorization)
        if capability is None:
            raise HTTPException(status_code=401, detail="invalid daemon session token")
        if capability != "agent":
            raise HTTPException(status_code=403, detail="agent capability required")

    read_auth = [Depends(authorize_read)]
    creator_auth = [Depends(authorize_creator)]
    agent_action_auth = [Depends(authorize_agent_action)]
    agent_only_auth = [Depends(authorize_agent)]
    auth = read_auth

    @app.exception_handler(NotFoundError)
    async def not_found(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(ProjectMismatchError)
    async def project_mismatch(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid_value(_request, error):
        payload = {"detail": str(error)}
        if isinstance(getattr(error, "code", None), str):
            payload["code"] = error.code
        return JSONResponse(payload, status_code=422)

    @app.exception_handler(ConflictError)
    async def conflict(_request, error):
        return JSONResponse({"detail": str(error), "current_revisions": error.revisions}, status_code=409)

    @app.get("/health")
    def health(): return {"status": "ok"}

    @app.get("/api/workbench/doctor", dependencies=auth)
    def doctor():
        versions = [row[0] for row in db.connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        required = ("short-drama-novel-analyze", "short-drama-develop", "short-drama-write")
        skills = []
        for name in required:
            try:
                from script_weaver.application.workbench_service import skill_tree_hash
                skills.append({"name": name, "available": True, "hash": skill_tree_hash(name)})
            except NotFoundError:
                skills.append({"name": name, "available": False})
        return {"daemon": "ok", "schema_versions": versions, "skills": skills, "h3": h3.doctor()}

    @app.get("/api/projects", dependencies=auth)
    def projects(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), archived: bool = False): return workbench.list_projects(limit, offset, archived)

    @app.post("/api/projects", dependencies=creator_auth, status_code=201)
    def create_project(body: ProjectCreate): return workbench.create_project(body)

    @app.post("/api/project-intakes", dependencies=creator_auth, status_code=201)
    def create_project_intake(body: ProjectIntake): return workbench.create_from_intake(body)

    @app.post("/api/import/legacy-project-state", dependencies=creator_auth, status_code=201)
    def import_legacy(body: LegacyImportRequest): return workbench.import_legacy(body.state)

    @app.get("/api/projects/{project_id}", dependencies=auth)
    def get_project(project_id: str): return workbench.get_project(project_id)

    @app.post("/api/projects/{project_id}/archive", dependencies=creator_auth)
    def archive_project(project_id: str, body: ProjectArchive): return workbench.set_project_archived(project_id, body.expected_revision, True)

    @app.post("/api/projects/{project_id}/restore", dependencies=creator_auth)
    def restore_project(project_id: str, body: ProjectArchive): return workbench.set_project_archived(project_id, body.expected_revision, False)

    @app.post("/api/projects/{project_id}/inputs", dependencies=creator_auth, status_code=201)
    def add_project_input(project_id: str, body: ProjectInputCreate): return workbench.add_project_input(project_id, body)

    @app.get("/api/projects/{project_id}/documents", dependencies=auth)
    def list_documents(project_id: str): return workbench.list_documents(project_id)

    @app.get("/api/documents/{document_id}", dependencies=auth)
    def get_document(document_id: str): return workbench.get_document(document_id)

    @app.put("/api/documents/{document_id}/draft", dependencies=creator_auth)
    def save_document_draft(document_id: str, body: DraftSave): return workbench.save_document_draft(document_id, body)

    @app.post("/api/documents/{document_id}/submit", dependencies=creator_auth)
    def submit_document(document_id: str, body: DocumentSubmit): return workbench.submit_document(document_id, body)

    @app.post("/api/documents/{document_id}/versions/{version_id}/decision", dependencies=creator_auth)
    def decide_document(document_id: str, version_id: str, body: DocumentDecision): return workbench.decide_document_version(document_id, version_id, body)

    @app.post("/api/documents/{document_id}/draft/restore", dependencies=creator_auth)
    def restore_document_draft(document_id: str, body: DocumentRestore): return workbench.restore_document_draft(document_id, body)

    @app.post("/api/documents/{document_id}/project", dependencies=creator_auth)
    def retry_screenplay_projection(document_id: str, body: ProjectionRetry):
        return workbench.retry_screenplay_projection(document_id, body)

    @app.post("/api/documents/{document_id}/adopt-source", dependencies=creator_auth)
    def adopt_single_script(document_id: str, body: DocumentSubmit): return workbench.adopt_single_script(document_id, body)

    @app.post("/api/projects/{project_id}/episodes", dependencies=creator_auth, status_code=201)
    def create_episode(project_id: str, body: EpisodeCreate): return workbench.create_episode(project_id, body)

    @app.get("/api/episodes/{episode_id}", dependencies=auth)
    def get_episode(episode_id: str): return workbench.get_episode(episode_id)

    @app.post("/api/episodes/{episode_id}/scenes", dependencies=creator_auth, status_code=201)
    def create_scene(episode_id: str, body: SceneCreate): return workbench.create_scene(episode_id, body)

    @app.post("/api/episodes/{episode_id}/segments", dependencies=creator_auth, status_code=201)
    def create_segment(episode_id: str, body: SegmentCreate): return workbench.create_segment(episode_id, body)

    @app.get("/api/segments/{segment_id}", dependencies=auth)
    def get_segment(segment_id: str): return workbench.get_segment(segment_id)

    @app.post("/api/segments/{segment_id}/shots", dependencies=creator_auth, status_code=201)
    def create_shot(segment_id: str, body: ShotCreate): return workbench.create_shot(segment_id, body)

    @app.get("/api/shots/{shot_id}", dependencies=auth)
    def get_shot(shot_id: str): return workbench.get_shot(shot_id)

    @app.patch("/api/shots/{shot_id}", dependencies=creator_auth)
    def update_shot(shot_id: str, body: DirectShotUpdate): return workbench.update_shot(shot_id, body.expected_revision, body.changes)

    @app.post("/api/shots/{shot_id}/restore", dependencies=creator_auth)
    def restore_shot(shot_id: str, body: ShotRestore): return workbench.restore_shot(shot_id, body.expected_revision, body.source_revision)

    @app.post("/api/projects/{project_id}/assets", dependencies=creator_auth, status_code=201)
    def create_asset(project_id: str, body: AssetCreate): return workbench.create_asset(project_id, body)

    @app.get("/api/projects/{project_id}/assets", dependencies=auth)
    def list_assets(project_id: str): return workbench.list_assets(project_id)

    @app.get("/api/assets/{asset_id}", dependencies=auth)
    def get_asset(asset_id: str): return workbench.get_asset(asset_id)

    @app.get("/api/asset-versions/{version_id}", dependencies=auth)
    def get_asset_version(version_id: str): return workbench.get_asset_version(version_id)

    @app.post("/api/assets/{asset_id}/versions", dependencies=creator_auth, status_code=201)
    def create_asset_version(asset_id: str, body: AssetVersionCreate): return workbench.create_asset_version(asset_id, body)

    @app.post("/api/assets/{asset_id}/restore", dependencies=creator_auth, status_code=201)
    def restore_asset_version(asset_id: str, body: AssetRestore): return workbench.restore_asset_version(asset_id, body.source_version_id, body.expected_revision)

    @app.post("/api/reference-bindings", dependencies=creator_auth, status_code=201)
    def bind_reference(body: BindingCreate): return workbench.bind_reference(body)

    @app.post("/api/reference-bindings/{binding_id}/actions", dependencies=creator_auth)
    def binding_action(binding_id: str, body: BindingAction): return workbench.binding_action(binding_id, body)

    @app.put("/api/surface-contexts/{session_id}", dependencies=creator_auth)
    def surface_context(session_id: str, body: SurfaceContextUpsert):
        if session_id != body.session_id:
            raise HTTPException(400, "session_id mismatch")
        return workbench.set_surface_context(body)

    @app.get("/api/active-context", dependencies=auth)
    def active_context(session_id: str | None = None): return workbench.get_active_context(session_id)

    @app.post("/api/tasks", dependencies=creator_auth, status_code=201)
    def start_task(body: TaskStart): return workbench.start_task(body)

    @app.get("/api/tasks", dependencies=auth)
    def list_tasks(project_id: str | None = None, status: str | None = None):
        return workbench.list_tasks(project_id, status)

    @app.post("/api/tasks/{task_id}/claim", dependencies=agent_action_auth)
    def claim_task(task_id: str, body: TaskClaim):
        return workbench.claim_task(task_id, body.worker_label)

    @app.get("/api/tasks/{task_id}/context", dependencies=auth)
    def task_context(
        task_id: str,
        section: str = Query("all", pattern="^(all|source|target)$"),
        cursor: int = Query(0, ge=0),
    ):
        return workbench.get_task_context(task_id, section, cursor)

    @app.post("/api/tasks/{task_id}/fail", dependencies=agent_action_auth)
    def fail_task(task_id: str, body: TaskFail): return workbench.fail_task(task_id, body)

    @app.get("/api/tasks/{task_id}/snapshot", dependencies=auth)
    def task_snapshot(task_id: str): return workbench.get_task_snapshot(task_id)

    @app.post("/api/agent-runs", dependencies=creator_auth, status_code=201)
    def create_agent_run(body: AgentRunCreate): return workbench.create_agent_run(body.task_id, body.skill_name, body.skill_version)

    @app.post("/api/changesets", dependencies=creator_auth, status_code=201)
    def create_changeset(body: ChangeSetCreate): return changesets.create(body.task_id, body.run_id, body.summary)

    @app.post("/api/tasks/{task_id}/changesets/submit", dependencies=agent_action_auth, status_code=201)
    def submit_proposal(task_id: str, body: ProposalSubmit):
        return changesets.submit_proposal(task_id, body)

    @app.get("/api/projects/{project_id}/changesets", dependencies=auth)
    def list_changesets(project_id: str, limit: int = Query(50, ge=1, le=100)): return changesets.list(project_id, limit)

    @app.get("/api/changesets/{changeset_id}", dependencies=auth)
    def get_changeset(changeset_id: str): return changesets.get(changeset_id)

    @app.post("/api/changesets/{changeset_id}/operations", dependencies=creator_auth, status_code=201)
    def append_operation(changeset_id: str, body: OperationEnvelope): return changesets.append(changeset_id, body.operation)

    @app.post("/api/changesets/{changeset_id}/validate", dependencies=creator_auth)
    def validate_changeset(changeset_id: str): return changesets.validate(changeset_id)

    @app.post("/api/changesets/{changeset_id}/submit", dependencies=creator_auth)
    def submit_changeset(changeset_id: str): return changesets.submit(changeset_id)

    @app.post("/api/changesets/{changeset_id}/apply", dependencies=creator_auth)
    def apply_changeset(changeset_id: str, body: ChangeSetApply): return changesets.apply(changeset_id, body.fingerprint)

    @app.post("/api/changesets/{changeset_id}/reject", dependencies=creator_auth)
    def reject_changeset(changeset_id: str): return changesets.reject(changeset_id)

    @app.get("/api/projects/{project_id}/search", dependencies=auth)
    def search_project(project_id: str, q: str = Query(max_length=200), limit: int = Query(50, ge=1, le=100)): return workbench.search_project(project_id, q, limit)

    @app.get("/api/shots/{shot_id}/preview", dependencies=auth)
    def shot_preview(shot_id: str): return workbench.render_shot_preview(shot_id)

    @app.get("/api/segments/{segment_id}/contact-sheet", dependencies=auth)
    def contact_sheet(segment_id: str): return workbench.render_contact_sheet(segment_id)

    @app.get("/api/shots/{shot_id}/compare", dependencies=auth)
    def compare_shot(shot_id: str): return workbench.compare_shot_versions(shot_id)

    @app.get("/api/projects/{project_id}/asset-board", dependencies=auth)
    def asset_board(project_id: str): return workbench.render_asset_board(project_id)

    @app.get("/api/deep-link/{entity_type}/{entity_id}", dependencies=auth)
    def deep_link(entity_type: str, entity_id: str): return workbench.deep_link(entity_type, entity_id)

    @app.post("/api/generation-jobs", dependencies=agent_action_auth, status_code=201)
    def prepare_job(body: GenerationPrepare): return generation.prepare(body)

    @app.get("/api/generation-jobs/{job_id}", dependencies=auth)
    def get_job(job_id: str): return generation._get(job_id)

    @app.get("/api/media/{media_id}", dependencies=auth)
    def get_media(media_id: str):
        row = db.connection.execute("SELECT storage_path,mime FROM media_versions WHERE id=?", (media_id,)).fetchone()
        if not row:
            raise NotFoundError("media not found")
        path = Path(row[0]).resolve()
        if media_store.objects.resolve() not in path.parents or not path.is_file():
            raise NotFoundError("media object unavailable")
        return FileResponse(path, media_type=row[1])

    @app.get("/api/projects/{project_id}/media-candidates", dependencies=auth)
    def list_media_candidates(
        project_id: str, owner_type: str | None = None, owner_id: str | None = None
    ):
        if owner_type not in {None, "shot", "asset"}:
            raise HTTPException(422, "invalid owner_type")
        return generation.list_candidates(project_id, owner_type, owner_id)

    @app.get("/api/media-candidates/{candidate_id}", dependencies=auth)
    def get_media_candidate(candidate_id: str):
        return generation.get_candidate(candidate_id)

    @app.post("/api/media-candidates/{candidate_id}/accept", dependencies=creator_auth)
    def accept_media_candidate(candidate_id: str, body: MediaCandidateAccept):
        return generation.accept_candidate(candidate_id, body)

    @app.post("/api/media-candidates/import", dependencies=agent_only_auth, status_code=201)
    async def import_media_candidate(
        request: Request,
        project_id: str,
        task_id: str,
        run_id: str,
        owner_type: str,
        owner_id: str,
        prompt: str,
        sha256: str,
        parent_candidate_id: str | None = None,
        target_asset_id: str | None = None,
        content_type: Annotated[str | None, Header(alias="Content-Type")] = None,
    ):
        if content_type != "application/octet-stream":
            raise HTTPException(415, "agent candidate import requires application/octet-stream")
        metadata = MediaCandidateImport(
            project_id=project_id, task_id=task_id, run_id=run_id,
            owner_type=owner_type, owner_id=owner_id, prompt=prompt, sha256=sha256,
            parent_candidate_id=parent_candidate_id, target_asset_id=target_asset_id,
        )
        return generation.import_candidate(metadata, await request.body())

    @app.post("/api/generation-jobs/{job_id}/confirm", dependencies=creator_auth)
    def confirm_job(job_id: str, body: GenerationConfirm): return generation.confirm(job_id, body.fingerprint)

    @app.post("/api/generation-jobs/{job_id}/run", dependencies=creator_auth)
    def run_job(job_id: str, body: GenerationRun): return generation.run(job_id, body.confirmation_token)

    @app.post("/api/h3/video-jobs", dependencies=creator_auth, status_code=201)
    def prepare_h3_video(body: H3VideoPrepare): return h3.prepare(body)

    @app.get("/api/projects/{project_id}/shots/{shot_id}/h3-video-job", dependencies=auth)
    def latest_h3_video(project_id: str, shot_id: str):
        return h3.latest_for_shot(project_id, shot_id)

    @app.post("/api/h3/video-jobs/{job_id}/submit", dependencies=creator_auth)
    def submit_h3_video(job_id: str, body: GenerationRun):
        return h3.submit(job_id, body.confirmation_token)

    @app.post("/api/h3/video-jobs/{job_id}/poll", dependencies=creator_auth)
    def poll_h3_video(job_id: str): return h3.poll(job_id)

    @app.get("/api/projects/{project_id}/production-packages", dependencies=auth)
    def list_production_packages(project_id: str): return packages.list(project_id)

    @app.post("/api/projects/{project_id}/production-packages", dependencies=creator_auth, status_code=201)
    def build_production_package(project_id: str, body: ProductionPackageCreate):
        return packages.build(project_id, body.expected_project_revision)

    @app.get("/api/projects/{project_id}/events", dependencies=auth)
    async def events(
        project_id: str,
        request: Request,
        after: int | None = Query(None, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        try:
            header_cursor = int(last_event_id) if last_event_id else None
        except ValueError as error:
            raise HTTPException(400, "invalid Last-Event-ID") from error
        initial_cursor = max(
            value for value in (after, header_cursor, workbench.latest_event_id(project_id))
            if value is not None
        ) if after is None and header_cursor is None else max(
            value for value in (after, header_cursor, 0) if value is not None
        )
        async def stream():
            cursor = initial_cursor
            while not await request.is_disconnected():
                for event in workbench.list_events(project_id, cursor):
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


def main() -> None:
    import uvicorn
    require_supported_python()
    uvicorn.run(create_app(), host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
