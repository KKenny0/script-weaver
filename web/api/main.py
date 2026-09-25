"""FastAPI backend for Script-Weaver Web UI.

Provides REST API + SSE streaming for real-time progress.
The web frontend calls these endpoints to drive the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from anyio import CancelScope
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from script_weaver.core.config import get_settings
from script_weaver.core.pipeline import PipelineEngine
from script_weaver.core.types import (
    DecisionRecord,
    ProjectMeta,
    ProjectState,
    SkillBinding,
    UserAction,
)
from script_weaver.exporters.fountain_exporter import export_fountain
from script_weaver.exporters.json_exporter import export_json
from script_weaver.exporters.video_gen_exporter import export_video_gen_prompts
from script_weaver.llm.client import LLMClient
from script_weaver.memory.profile import get_profile_manager
from script_weaver.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

# ── In-memory project store (production: use DB) ────────
_projects: dict[str, dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: discover skills."""
    logger.info("Script-Weaver API starting up...")
    yield
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


class RefineRequest(BaseModel):
    message: str


class SkillActivateRequest(BaseModel):
    skill_id: str
    stage: str
    priority: int = 0
    params: dict[str, Any] = {}


# ── Helper: get or create engine ────────────────────────────

def _get_engine(project_id: str) -> tuple[PipelineEngine, dict]:
    """Get or create pipeline engine for a project."""
    if project_id not in _projects:
        raise HTTPException(404, f"Project '{project_id}' not found")
    proj = _projects[project_id]
    llm = LLMClient()
    registry = SkillRegistry()
    # Load skills
    try:
        import asyncio
        asyncio.get_event_loop().run_until_complete(registry.discover())
    except RuntimeError:
        pass  # No event loop in some contexts
    engine = PipelineEngine(
        llm_client=llm,
        skill_registry=registry,
        auto_approve_gates=proj.get("auto_approve", True),
        progress_callback=lambda stage, msg: None,  # Could use WebSocket later
    )
    return engine, proj


# ── Project CRUD ────────────────────────────────────────


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest) -> dict:
    """Create a new project from a story idea."""
    project_id = uuid.uuid4().hex[:12]
    state = ProjectState(
        user_input=req.user_input,
        meta=ProjectMeta(title=req.title or req.user_input[:40]),
    )
    _projects[project_id] = {
        "state": state,
        "status": "created",
        "auto_approve": req.auto_approve_gates,
        "skill_bindings": req.active_skills,
        "created_at": "",
    }
    return {"project_id": project_id, "title": state.meta.title}


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    """Get current project state (full)."""
    if project_id not in _projects:
        raise HTTPException(404, "Project not found")
    proj = _projects[project_id]
    state: ProjectState = proj["state"]
    return {
        "project_id": project_id,
        "status": proj["status"],
        "meta": state.meta.model_dump(),
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


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    """List all projects."""
    result = []
    for pid, proj in _projects.items():
        state: ProjectState = proj["state"]
        result.append({
            "project_id": pid,
            "title": state.meta.title,
            "status": proj["status"],
            "created_at": proj.get("created_at", ""),
            "stage": state.current_stage_status().value,
        })
    return result


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict:
    """Delete a project."""
    if project_id not in _projects:
        raise HTTPException(404, "Project not found")
    del _projects[project_id]
    return {"deleted": True}


# ── Pipeline Execution (SSE Streaming) ───────────────


@app.get("/api/projects/{project_id}/generate")
async def generate(project_id: str) -> StreamingResponse:
    """Run the full pipeline with SSE progress streaming."""

    async def event_generator():
        yield _sse_event("status", {"message": "Starting pipeline...", "stage": "init"})

        engine, proj = _get_engine(project_id)
        state: ProjectState = proj["state"]
        proj["status"] = "running"

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

            # Update stored state
            proj["state"] = result_state
            proj["status"] = "complete"

            yield _sse_event("progress", {
                "stage": "complete",
                "message": "Pipeline complete!",
                "result_summary": _build_result_summary(result_state),
            })
            yield _sse_event("done", {"project_id": project_id})

        except Exception as e:
            proj["status"] = "error"
            logger.error(f"Pipeline error: {e}", exc_info=True)
            yield _sse_event("error", {"message": str(e)})
            yield _sse_event("done", {"project_id": project_id, "error": str(e)})
        finally:
            if not task.done():
                task.cancel()
            # Always retrieve the result/exception, including disconnect cancellation.
            with CancelScope(shield=True):
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if proj["status"] == "running":
                proj["status"] = "error"

    return EventSourceResponse(event_generator())


@app.post("/api/projects/{project_id}/refine")
async def refine(project_id: str, req: RefineRequest) -> dict:
    """Send an iterative refinement request."""
    engine, proj = _get_engine(project_id)
    state: ProjectState = proj["state"]

    updated = await engine.refine(state, req.message)
    proj["state"] = updated

    return {
        "project_id": project_id,
        "status": proj["status"],
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
    """Export project in various formats."""
    if project_id not in _projects:
        raise HTTPException(404, "Project not found")

    state: ProjectState = _projects[project_id]["state"]

    if format_type == "json":
        content = export_json(state)
        return JSONResponse({"content": content})
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
