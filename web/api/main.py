"""FastAPI backend for Script-Weaver Web UI.

Provides REST API + SSE streaming for real-time progress.
The web frontend calls these endpoints to drive the pipeline.

Projects are persisted in SQLite under ``<data_dir>/main-web`` (ticket #13):
the current ProjectState snapshot and its immutable version history live in
one database, so content survives refreshes and backend restarts. A single
API instance owns the data directory via an OS-level lock; a second instance
on the same directory refuses to start.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from anyio import CancelScope
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from script_weaver.core.config import get_settings
from script_weaver.core.pipeline import PipelineEngine
from script_weaver.core.project_store import (
    DataDirLock,
    DataDirLockError,
    ProjectRecord,
    ProjectStore,
    ProjectStoreError,
    RevisionConflictError,
)
from script_weaver.core.types import ProjectState
from script_weaver.exporters.fountain_exporter import export_fountain
from script_weaver.exporters.json_exporter import export_json
from script_weaver.exporters.video_gen_exporter import export_video_gen_prompts
from script_weaver.llm.client import LLMClient
from script_weaver.memory.profile import get_profile_manager
from script_weaver.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


@dataclass
class _Runtime:
    store: ProjectStore
    lock: DataDirLock


# Set by the lifespan; module-level so direct-call tests can substitute it.
_runtime: _Runtime | None = None
# Projects with an in-flight generation stream (task state, not content).
_active_generations: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: take the data-dir lock and open the persistent store."""
    global _runtime
    logger.info("Script-Weaver API starting up...")
    namespace = get_settings().data_dir / "main-web"
    lock = DataDirLock(namespace)
    try:
        lock.acquire()
    except DataDirLockError as exc:
        logger.error(" refusing to start: %s", exc)
        raise
    try:
        store = ProjectStore(namespace / "projects.sqlite3")
    except Exception:
        lock.release()
        raise
    _runtime = _Runtime(store=store, lock=lock)
    try:
        yield
    finally:
        _runtime = None
        store.close()
        lock.release()
        logger.info("Script-Weaver API shutting down.")


app = FastAPI(
    title="Script-Weaver API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request/Response Models ─────────────────────────────


class CreateProjectRequest(BaseModel):
    user_input: str
    title: str | None = None
    auto_approve_gates: bool = True
    active_skills: dict[str, list[dict]] = {}  # {stage: [skill_bindings]}


class RenameProjectRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)


class RefineRequest(BaseModel):
    message: str


class SkillActivateRequest(BaseModel):
    skill_id: str
    stage: str
    priority: int = 0
    params: dict[str, Any] = {}


def _store() -> ProjectStore:
    if _runtime is None:
        raise HTTPException(503, "项目存储尚未初始化，请等待服务启动完成")
    return _runtime.store


def _store_error(exc: ProjectStoreError) -> HTTPException:
    if isinstance(exc, RevisionConflictError):
        return HTTPException(
            409,
            detail={
                "code": "revision_conflict",
                "message": str(exc),
                "current_revision": exc.current_revision,
            },
        )
    return HTTPException(
        404, detail={"code": "project_not_found", "message": str(exc)}
    )


# ── Helper: get or create engine ────────────────────────────


@dataclass
class _GenerationContext:
    """Everything a generation/refine run needs to commit atomically."""

    project_id: str
    revision: int
    base_state_json: str
    state: ProjectState
    store: ProjectStore

    def commit(self, new_state: ProjectState, source: str, summary: str) -> ProjectRecord:
        return self.store.replace_state(
            self.project_id,
            new_state,
            base_revision=self.revision,
            base_state_json=self.base_state_json,
            source=source,
            summary=summary,
        )

    def save(self, new_state: ProjectState, source: str, summary: str) -> ProjectRecord:
        return self.store.save_state(
            self.project_id,
            new_state,
            self.revision,
            source=source,
            summary=summary,
        )


async def _get_engine(project_id: str) -> tuple[PipelineEngine, _GenerationContext]:
    """Load a project from the store and build a pipeline engine for it.

    Raises 404 when the project is unknown and 400 when the configured model
    is unusable (e.g. missing API key) — before any streaming starts.
    """
    store = _store()
    try:
        record = await asyncio.to_thread(store.get_required, project_id)
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc
    try:
        llm = LLMClient()
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "model_not_configured", "message": f"模型不可用: {exc}"}
        ) from exc
    registry = SkillRegistry()
    engine = PipelineEngine(
        llm_client=llm,
        skill_registry=registry,
        auto_approve_gates=record.auto_approve,
    )
    ctx = _GenerationContext(
        project_id=record.project_id,
        revision=record.revision,
        base_state_json=record.state_json,
        state=record.state,
        store=store,
    )
    return engine, ctx


# ── Project CRUD ────────────────────────────────────────


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest) -> dict:
    """Create a new project from a story idea."""
    record = await asyncio.to_thread(
        _store().create_project,
        user_input=req.user_input,
        title=req.title,
        auto_approve=req.auto_approve_gates,
        skill_bindings=req.active_skills,
    )
    return {
        "project_id": record.project_id,
        "title": record.title,
        "revision": record.revision,
        "created_at": record.created_at,
    }


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    """Get current project state (full)."""
    try:
        record = await asyncio.to_thread(_store().get_required, project_id)
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc
    return _serialize_project(
        record.project_id,
        record.state,
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
        status="running" if project_id in _active_generations else record.state.meta.status.value,
    )


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    """List all projects, most recently updated first."""
    return await asyncio.to_thread(_store().list_projects)


@app.patch("/api/projects/{project_id}")
async def rename_project(project_id: str, req: RenameProjectRequest) -> dict:
    """Rename a project. Naming advances the revision."""
    title = req.title.strip()
    if not title:
        raise HTTPException(422, detail={"code": "invalid_title", "message": "标题不能为空白"})
    try:
        record = await asyncio.to_thread(
            _store().rename_project,
            project_id,
            title,
            req.expected_revision,
        )
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc
    return {
        "project_id": record.project_id,
        "title": record.title,
        "revision": record.revision,
        "updated_at": record.updated_at,
    }


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict:
    """Delete a project and its history."""
    deleted = await asyncio.to_thread(_store().delete_project, project_id)
    if not deleted:
        raise HTTPException(404, detail={"code": "project_not_found",
                                         "message": f"Project '{project_id}' not found"})
    return {"deleted": True}


# ── Version History (read-only) ──────────────────────────


@app.get("/api/projects/{project_id}/versions")
async def list_versions(project_id: str) -> dict:
    """List immutable version summaries for a project."""
    try:
        record = await asyncio.to_thread(_store().get_required, project_id)
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc
    versions = await asyncio.to_thread(_store().list_versions, project_id)
    return {
        "project_id": project_id,
        "current_revision": record.revision,
        "versions": [
            {
                "revision": v.revision,
                "title": v.title,
                "source": v.source,
                "summary": v.summary,
                "created_at": v.created_at,
            }
            for v in versions
        ],
    }


@app.get("/api/projects/{project_id}/versions/{revision}")
async def get_version(project_id: str, revision: int) -> dict:
    """Read one historical version (read-only; no restore in this slice)."""
    try:
        await asyncio.to_thread(_store().get_required, project_id)
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc
    snapshot = await asyncio.to_thread(_store().get_version, project_id, revision)
    if snapshot is None:
        raise HTTPException(
            404,
            detail={
                "code": "version_not_found",
                "message": f"项目 '{project_id}' 不存在 revision {revision}",
            },
        )
    return _serialize_project(
        project_id,
        snapshot.state,
        revision=snapshot.revision,
        created_at=snapshot.created_at,
        updated_at=snapshot.created_at,
        status=snapshot.state.meta.status.value,
        source=snapshot.source,
        summary=snapshot.summary,
    )


# ── Pipeline Execution (SSE Streaming) ───────────────


@app.get("/api/projects/{project_id}/generate")
async def generate(project_id: str) -> StreamingResponse:
    """Run the full pipeline with SSE progress streaming."""
    engine, ctx = await _get_engine(project_id)

    async def event_generator():
        yield _sse_event("status", {"message": "Starting pipeline...", "stage": "init"})

        state: ProjectState = ctx.state
        _active_generations.add(project_id)

        # Bridge the sync progress callback to the async SSE stream via a queue
        queue: asyncio.Queue = asyncio.Queue()

        def on_progress(stage: str, message: str) -> None:
            queue.put_nowait({"stage": stage, "message": message})

        engine._progress = on_progress

        async def _run() -> ProjectState:
            return await engine.run_full_pipeline(
                user_input=state.user_input,
                title=state.meta.title or None,
            )

        task = asyncio.create_task(_run())
        try:
            # Stream progress events as the pipeline runs
            while not task.done():
                try:
                    yield _sse_event("progress", queue.get_nowait())
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.1)

            # Drain any remaining queued events
            while not queue.empty():
                yield _sse_event("progress", queue.get_nowait())

            result_state = await task

            # Persist the result atomically; the service project ID wins so
            # exports always carry meta.id == project_id.
            result_state.meta.id = project_id
            try:
                await asyncio.to_thread(
                    ctx.commit, result_state, "pipeline", "完整生成"
                )
            except RevisionConflictError as exc:
                logger.warning("Generation result rejected for %s: %s", project_id, exc)
                yield _sse_event("error", {
                    "message": "项目内容在生成期间被修改，本次生成结果未覆盖当前内容。请重新生成。",
                })
                yield _sse_event("done", {"project_id": project_id, "error": "revision_conflict"})
                return

            yield _sse_event("progress", {
                "stage": "complete",
                "message": "Pipeline complete!",
                "result_summary": _build_result_summary(result_state),
            })
            yield _sse_event("done", {"project_id": project_id})

        except Exception as e:
            logger.exception("Pipeline error")
            yield _sse_event("error", {"message": str(e)})
            yield _sse_event("done", {"project_id": project_id, "error": str(e)})
        finally:
            if not task.done():
                task.cancel()
            # Always retrieve the result/exception, including disconnect cancellation.
            with CancelScope(shield=True):
                with suppress(asyncio.CancelledError, Exception):
                    await task
            _active_generations.discard(project_id)

    return EventSourceResponse(event_generator())


@app.post("/api/projects/{project_id}/refine")
async def refine(project_id: str, req: RefineRequest) -> dict:
    """Send an iterative refinement request."""
    engine, ctx = await _get_engine(project_id)

    # Run on a copy so a failed refine cannot partially write content.
    updated = await engine.refine(copy.deepcopy(ctx.state), req.message)
    updated.meta.id = project_id
    try:
        record = await asyncio.to_thread(
            ctx.save, updated, "manual", f"修改: {req.message[:60]}"
        )
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc

    return {
        "project_id": project_id,
        "revision": record.revision,
        "stage": updated.current_stage_status().value,
        "message": "Refinement applied",
    }


# ── Skills Management ─────────────────────────────────


@app.get("/api/skills")
async def list_skills(stage: str | None = None) -> list[dict]:
    """List available skills."""
    registry = SkillRegistry()
    await registry.discover()
    return registry.list_available(stage)


@app.post("/api/skills/{skill_id}/activate")
async def activate_skill(skill_id: str, req: SkillActivateRequest) -> dict:
    """Activate a skill for a stage (global for now)."""
    registry = SkillRegistry()
    await registry.discover()

    # For MVP: activate globally (future: per-project)
    bindings = {}
    registry.activate(bindings, req.stage, skill_id, priority=req.priority, params=req.params)

    skill = registry.get(skill_id)
    return {
        "activated": True,
        "skill_id": skill_id,
        "name": skill.name if skill else skill_id,
        "stage": req.stage,
    }


# ── Export ──────────────────────────────────────────────


@app.get("/api/projects/{project_id}/export/{format_type}")
async def export_project(project_id: str, format_type: str) -> JSONResponse:
    """Export the persisted current project in various formats."""
    try:
        record = await asyncio.to_thread(_store().get_required, project_id)
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc

    state: ProjectState = record.state

    if format_type == "json":
        content = export_json(state)
        return JSONResponse({"content": content, "meta": {"id": state.meta.id, "title": state.meta.title}})
    elif format_type == "fountain":
        if not state.script:
            raise HTTPException(400, "No script to export as Fountain")
        content = export_fountain(state.script)
        return JSONResponse({"content": content})
    elif format_type == "video_gen":
        if not state.storyboard:
            raise HTTPException(400, "No storyboard to export")
        results = export_video_gen_prompts(state)
        if "error" in results:
            raise HTTPException(400, results["error"])
        return JSONResponse(results)
    else:
        raise HTTPException(400, f"Unknown format: {format_type}")


# ── Profile ────────────────────────────────────────────


@app.get("/api/profile")
async def get_profile() -> dict:
    """Get user profile."""
    pm = get_profile_manager()
    p = pm.profile
    return p.model_dump()


# ── Helpers ────────────────────────────────────────────────


def _serialize_project(
    project_id: str,
    state: ProjectState,
    *,
    revision: int,
    created_at: str,
    updated_at: str,
    status: str,
    source: str | None = None,
    summary: str | None = None,
) -> dict:
    """Serialize a ProjectState for the frontend (field contract preserved)."""
    result = {
        "project_id": project_id,
        "status": status,
        "meta": state.meta.model_dump(),
        "revision": revision,
        "created_at": created_at,
        "updated_at": updated_at,
        "has_refined_idea": bool(state.refined_idea),
        "has_outline": bool(state.outline),
        "characters_count": len(state.characters) if state.characters else 0,
        "scenes_count": len(state.scenes) if state.scenes else 0,
        "has_art_style": bool(state.art_style),
        "script_scenes_count": len(state.script.scenes) if state.script else 0,
        "storyboard_shots_count": state.storyboard.total_shot_count if state.storyboard else 0,
        "visual_highlights_count": len(state.visual_highlights) if state.visual_highlights else 0,
        "decisions_count": len(state.memory.decisions),
        # Full data for frontend rendering
        "refined_idea": state.refined_idea,
        "outline": state.outline.model_dump() if state.outline else None,
        "characters": [c.model_dump() for c in (state.characters or [])],
        "scenes": [s.model_dump() for s in (state.scenes or [])],
        "art_style": state.art_style.model_dump() if state.art_style else None,
        "script": state.script.model_dump() if state.script else None,
        "storyboard": state.storyboard.model_dump() if state.storyboard else None,
        "visual_highlights": [vh.model_dump() for vh in (state.visual_highlights or [])],
        "memory_decisions": [d.model_dump() for d in state.memory.decisions],
    }
    if source is not None:
        result["source"] = source
    if summary is not None:
        result["summary"] = summary
    return result


def _sse_event(event_type: str, data: Any) -> dict:
    """Build an SSE event dict — EventSourceResponse serializes it with the
    correct `event:` / `data:` fields (yielding a pre-formatted string would
    lose the event type)."""
    return {"event": event_type, "data": json.dumps(data, ensure_ascii=False)}


def _build_result_summary(state: ProjectState) -> dict:
    """Build a summary of pipeline results."""
    parts = []
    if state.outline:
        bi = state.outline.basic_info
        parts.append(f"大纲: {bi.logline} ({len(state.outline.plot_outline)}节拍)")
    if state.characters:
        parts.append(f"角色: {len(state.characters)}个")
    if state.script:
        parts.append(f"剧本: {len(state.script.scenes)}场")
    if state.storyboard:
        parts.append(f"分镜: {state.storyboard.total_shot_count}镜头 ~{state.storyboard.total_estimated_duration:.0f}s")
    return {
        "title": state.meta.title,
        "status": state.meta.status.value,
        "details": parts,
    }


# ── Run server ──────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=8000)
