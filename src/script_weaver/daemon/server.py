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
    EpisodeCreate,
    GenerationConfirm,
    GenerationPrepare,
    GenerationRun,
    LegacyImportRequest,
    NotFoundError,
    OperationEnvelope,
    ProjectCreate,
    ProjectMismatchError,
    SceneCreate,
    SegmentCreate,
    ShotCreate,
    ShotRestore,
    SurfaceContextUpsert,
    TaskStart,
)
from script_weaver.infrastructure.media_store import MediaStore
from script_weaver.infrastructure.sqlite import Database, default_data_dir

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


def rotate_token() -> str:
    token = secrets.token_hex(32)
    path = runtime_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def create_app(database_path: str | Path | None = None, media_root: str | Path | None = None, token: str | None = None) -> FastAPI:
    db = Database(database_path)
    workbench = WorkbenchService(db)
    changesets = ChangeSetService(db)
    media_store = MediaStore(media_root)
    generation = GenerationJobService(db, media_store)
    session_token = token or rotate_token()
    app = FastAPI(title="script-weaverd", version="1.0.0")
    app.state.db, app.state.workbench, app.state.changesets, app.state.generation = db, workbench, changesets, generation
    app.add_middleware(BodyLimitMiddleware)

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if authorization is None or not secrets.compare_digest(authorization, f"Bearer {session_token}"):
            raise HTTPException(status_code=401, detail="invalid daemon session token")

    auth = [Depends(authorize)]

    @app.exception_handler(NotFoundError)
    async def not_found(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(ProjectMismatchError)
    async def project_mismatch(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid_value(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(ConflictError)
    async def conflict(_request, error):
        return JSONResponse({"detail": str(error), "current_revisions": error.revisions}, status_code=409)

    @app.get("/health")
    def health(): return {"status": "ok"}

    @app.get("/api/projects", dependencies=auth)
    def projects(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)): return workbench.list_projects(limit, offset)

    @app.post("/api/projects", dependencies=auth, status_code=201)
    def create_project(body: ProjectCreate): return workbench.create_project(body)

    @app.post("/api/import/legacy-project-state", dependencies=auth, status_code=201)
    def import_legacy(body: LegacyImportRequest): return workbench.import_legacy(body.state)

    @app.get("/api/projects/{project_id}", dependencies=auth)
    def get_project(project_id: str): return workbench.get_project(project_id)

    @app.post("/api/projects/{project_id}/episodes", dependencies=auth, status_code=201)
    def create_episode(project_id: str, body: EpisodeCreate): return workbench.create_episode(project_id, body)

    @app.get("/api/episodes/{episode_id}", dependencies=auth)
    def get_episode(episode_id: str): return workbench.get_episode(episode_id)

    @app.post("/api/episodes/{episode_id}/scenes", dependencies=auth, status_code=201)
    def create_scene(episode_id: str, body: SceneCreate): return workbench.create_scene(episode_id, body)

    @app.post("/api/episodes/{episode_id}/segments", dependencies=auth, status_code=201)
    def create_segment(episode_id: str, body: SegmentCreate): return workbench.create_segment(episode_id, body)

    @app.get("/api/segments/{segment_id}", dependencies=auth)
    def get_segment(segment_id: str): return workbench.get_segment(segment_id)

    @app.post("/api/segments/{segment_id}/shots", dependencies=auth, status_code=201)
    def create_shot(segment_id: str, body: ShotCreate): return workbench.create_shot(segment_id, body)

    @app.get("/api/shots/{shot_id}", dependencies=auth)
    def get_shot(shot_id: str): return workbench.get_shot(shot_id)

    @app.patch("/api/shots/{shot_id}", dependencies=auth)
    def update_shot(shot_id: str, body: DirectShotUpdate): return workbench.update_shot(shot_id, body.expected_revision, body.changes)

    @app.post("/api/shots/{shot_id}/restore", dependencies=auth)
    def restore_shot(shot_id: str, body: ShotRestore): return workbench.restore_shot(shot_id, body.expected_revision, body.source_revision)

    @app.post("/api/projects/{project_id}/assets", dependencies=auth, status_code=201)
    def create_asset(project_id: str, body: AssetCreate): return workbench.create_asset(project_id, body)

    @app.get("/api/projects/{project_id}/assets", dependencies=auth)
    def list_assets(project_id: str): return workbench.list_assets(project_id)

    @app.get("/api/assets/{asset_id}", dependencies=auth)
    def get_asset(asset_id: str): return workbench.get_asset(asset_id)

    @app.get("/api/asset-versions/{version_id}", dependencies=auth)
    def get_asset_version(version_id: str): return workbench.get_asset_version(version_id)

    @app.post("/api/assets/{asset_id}/versions", dependencies=auth, status_code=201)
    def create_asset_version(asset_id: str, body: AssetVersionCreate): return workbench.create_asset_version(asset_id, body)

    @app.post("/api/assets/{asset_id}/restore", dependencies=auth, status_code=201)
    def restore_asset_version(asset_id: str, body: AssetRestore): return workbench.restore_asset_version(asset_id, body.source_version_id, body.expected_revision)

    @app.post("/api/reference-bindings", dependencies=auth, status_code=201)
    def bind_reference(body: BindingCreate): return workbench.bind_reference(body)

    @app.post("/api/reference-bindings/{binding_id}/actions", dependencies=auth)
    def binding_action(binding_id: str, body: BindingAction): return workbench.binding_action(binding_id, body)

    @app.put("/api/surface-contexts/{session_id}", dependencies=auth)
    def surface_context(session_id: str, body: SurfaceContextUpsert):
        if session_id != body.session_id:
            raise HTTPException(400, "session_id mismatch")
        return workbench.set_surface_context(body)

    @app.get("/api/active-context", dependencies=auth)
    def active_context(session_id: str | None = None): return workbench.get_active_context(session_id)

    @app.post("/api/tasks", dependencies=auth, status_code=201)
    def start_task(body: TaskStart): return workbench.start_task(body)

    @app.get("/api/tasks/{task_id}/snapshot", dependencies=auth)
    def task_snapshot(task_id: str): return workbench.get_task_snapshot(task_id)

    @app.post("/api/agent-runs", dependencies=auth, status_code=201)
    def create_agent_run(body: AgentRunCreate): return workbench.create_agent_run(body.task_id, body.skill_name, body.skill_version)

    @app.post("/api/changesets", dependencies=auth, status_code=201)
    def create_changeset(body: ChangeSetCreate): return changesets.create(body.task_id, body.run_id, body.summary)

    @app.get("/api/projects/{project_id}/changesets", dependencies=auth)
    def list_changesets(project_id: str, limit: int = Query(50, ge=1, le=100)): return changesets.list(project_id, limit)

    @app.get("/api/changesets/{changeset_id}", dependencies=auth)
    def get_changeset(changeset_id: str): return changesets.get(changeset_id)

    @app.post("/api/changesets/{changeset_id}/operations", dependencies=auth, status_code=201)
    def append_operation(changeset_id: str, body: OperationEnvelope): return changesets.append(changeset_id, body.operation)

    @app.post("/api/changesets/{changeset_id}/validate", dependencies=auth)
    def validate_changeset(changeset_id: str): return changesets.validate(changeset_id)

    @app.post("/api/changesets/{changeset_id}/submit", dependencies=auth)
    def submit_changeset(changeset_id: str): return changesets.submit(changeset_id)

    @app.post("/api/changesets/{changeset_id}/apply", dependencies=auth)
    def apply_changeset(changeset_id: str, body: ChangeSetApply): return changesets.apply(changeset_id, body.fingerprint)

    @app.post("/api/changesets/{changeset_id}/reject", dependencies=auth)
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

    @app.post("/api/generation-jobs", dependencies=auth, status_code=201)
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

    @app.post("/api/generation-jobs/{job_id}/confirm", dependencies=auth)
    def confirm_job(job_id: str, body: GenerationConfirm): return generation.confirm(job_id, body.fingerprint)

    @app.post("/api/generation-jobs/{job_id}/run", dependencies=auth)
    def run_job(job_id: str, body: GenerationRun): return generation.run(job_id, body.confirmation_token)

    @app.get("/api/projects/{project_id}/events", dependencies=auth)
    async def events(project_id: str, request: Request, after: int = Query(0, ge=0)):
        async def stream():
            cursor = after
            while not await request.is_disconnected():
                for event in workbench.list_events(project_id, cursor):
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run("script_weaver.daemon.server:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
