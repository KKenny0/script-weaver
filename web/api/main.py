"""FastAPI backend for Script-Weaver Web UI.

Provides REST API + SSE streaming for real-time progress.
The web frontend calls these endpoints to drive the pipeline.

Projects are persisted in SQLite under ``<data_dir>/main-web`` (ticket #13):
the current ProjectState snapshot and its immutable version history live in
one database, so content survives refreshes and backend restarts. A single
API instance owns the data directory via an OS-level lock; a second instance
on the same directory refuses to start.

Generation runs independently of the page (ticket #14): POST submits a run,
the FastAPI lifespan owns its asyncio task, every successful pipeline step
is CAS-committed as it validates, and GET endpoints only observe. A page
refresh, close or SSE disconnect never cancels a run; an explicit stop or a
service restart does (restarts mark leftover runs ``interrupted`` and never
re-arm model calls on their own).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
import zipfile
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from script_weaver.core import refinement
from script_weaver.core.config import get_settings
from script_weaver.core.pipeline import (
    GENERATION_STEP_LABELS,
    PipelineEngine,
    PipelineStopped,
)
from script_weaver.core.project_store import (
    RUN_ACTIVE_STATUSES,
    DataDirLock,
    DataDirLockError,
    GenerationRun,
    ProjectRecord,
    ProjectStore,
    ProjectStoreError,
    RevisionConflictError,
    hash_run_request,
    serialize_state,
)
from script_weaver.core.refinement import RefineExecutionError, RefinementError
from script_weaver.core.types import ProjectState, Shot
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
    runs: _RunManager


# Set by the lifespan; module-level so direct-call tests can substitute it.
_runtime: _Runtime | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: take the data-dir lock, open the store, reconcile runs.

    Runs left ``running``/``stopping`` by a previous process are marked
    ``interrupted`` — reported as fact, never silently resumed, and no model
    call is made here.
    """
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
    runs = _RunManager(store)
    _runtime = _Runtime(store=store, lock=lock, runs=runs)
    try:
        stale = await asyncio.to_thread(store.interrupt_stale_generation_runs)
        if stale:
            logger.info("Marked %d leftover generation run(s) interrupted", stale)
        yield
    finally:
        await runs.shutdown()
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


class GenerateRequest(BaseModel):
    """POST /generate body (ticket #14).

    ``request_key`` is the client's idempotency key: retries of one submit
    intent reuse it, a new intent mints a new one. ``user_input`` overrides
    the idea text for this run (defaults to the project's stored idea).
    """

    request_key: str | None = Field(default=None, max_length=200)
    user_input: str | None = Field(default=None, max_length=20000)


class SkillActivateRequest(BaseModel):
    skill_id: str
    stage: str
    priority: int = 0
    params: dict[str, Any] = {}


def _store() -> ProjectStore:
    if _runtime is None:
        raise HTTPException(503, "项目存储尚未初始化，请等待服务启动完成")
    return _runtime.store


def _runs() -> _RunManager:
    if _runtime is None:
        raise HTTPException(503, "项目存储尚未初始化，请等待服务启动完成")
    return _runtime.runs


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
    auto_approve: bool = True
    skill_bindings: dict[str, Any] = field(default_factory=dict)

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


async def _get_engine(
    project_id: str, *, progress_callback=None
) -> tuple[PipelineEngine, _GenerationContext]:
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
        progress_callback=progress_callback,
    )
    ctx = _GenerationContext(
        project_id=record.project_id,
        revision=record.revision,
        base_state_json=record.state_json,
        state=record.state,
        store=store,
        auto_approve=record.auto_approve,
        skill_bindings=record.skill_bindings,
    )
    return engine, ctx


# ── Background generation runs (ticket #14) ────────────


class RunSubmitConflict(Exception):
    """A submission violates idempotency or the one-active-run rule."""

    def __init__(self, code: str, message: str, run: GenerationRun | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.run = run


class _StageConflict(Exception):
    """A stage result could not be applied; the run must stop as failed."""

    def __init__(self, message: str, unapplied: ProjectState):
        super().__init__(message)
        self.message = message
        self.unapplied = unapplied


class _ProgressRelay:
    """Late-binding progress sink — the run id exists only after the row."""

    def __init__(self, manager: _RunManager):
        self._manager = manager
        self.run_id: str | None = None

    def __call__(self, stage: str, message: str) -> None:
        if self.run_id is not None:
            self._manager.broadcast(self.run_id, "progress",
                                    {"stage": stage, "message": message})


class _RunManager:
    """Lifespan-owned generation runs (ticket #14).

    A run's lifecycle belongs to the server process, never to an SSE
    connection: submit() enforces request_key idempotency and the
    one-active-model-run-per-project rule, then spawns the pipeline task.
    Every successful pipeline step is CAS-committed through
    ``on_stage_complete``; a user edit, a store failure or a stop aborts
    before the next stage while everything already saved stays saved.
    Subscribers only observe — disconnecting one changes nothing.
    """

    def __init__(self, store: ProjectStore):
        self.store = store
        self._tasks: dict[str, asyncio.Task] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._refine_projects: set[str] = set()

    # ── Admission ──────────────────────────────────────

    def _project_lock(self, project_id: str) -> asyncio.Lock:
        return self._project_locks.setdefault(project_id, asyncio.Lock())

    async def submit(
        self, project_id: str, request_key: str | None, user_input: str | None
    ) -> tuple[GenerationRun, bool]:
        """Admit one generation run; returns (run, created).

        The engine is built first so 404/400 answers before any row exists.
        Idempotency: an active run with the same key and identical input is
        returned as-is; the same key with different input is a conflict, and
        any other active run makes the project busy. Resending a request
        that already succeeded returns the finished run — no new model call.
        """
        relay = _ProgressRelay(self)
        engine, ctx = await _get_engine(project_id, progress_callback=relay)
        request = {
            "user_input": user_input if user_input is not None else ctx.state.user_input,
            "auto_approve": bool(getattr(ctx, "auto_approve", True)),
            "skill_bindings": getattr(ctx, "skill_bindings", {}) or {},
        }
        request_hash = hash_run_request(request)
        key = request_key or f"auto-{request_hash}"

        async with self._project_lock(project_id):
            if project_id in self._refine_projects:
                raise RunSubmitConflict(
                    "run_active", "该项目正在处理修改请求，请稍后再提交生成。"
                )
            active = await asyncio.to_thread(
                self.store.active_generation_run, project_id
            )
            if active is not None:
                if active.request_key == key:
                    if active.request_hash != request_hash:
                        raise RunSubmitConflict(
                            "request_conflict",
                            "同一 request_key 已携带不同输入在运行中，提交被拒绝。",
                            run=active,
                        )
                    return active, False
                raise RunSubmitConflict(
                    "run_active", "该项目已有生成运行进行中，请等待完成或先停止。",
                    run=active,
                )
            latest = await asyncio.to_thread(
                self.store.latest_generation_run, project_id
            )
            if (
                latest is not None
                and latest.request_key == key
                and latest.status == "succeeded"
                and latest.request_hash == request_hash
            ):
                return latest, False
            run = await asyncio.to_thread(
                self.store.create_generation_run,
                project_id,
                kind="generate",
                request_key=key,
                request=request,
                base_revision=ctx.revision,
                base_state_json=ctx.base_state_json,
                checkpoint_json=ctx.base_state_json,
            )
            relay.run_id = run.run_id
            self._spawn(run.run_id, engine, ctx)
            return run, True

    @asynccontextmanager
    async def refine_slot(self, project_id: str):
        """Admission for the synchronous refine endpoint.

        Refine is a model run too: it must not overlap an active generation,
        and generation must not start while a refine is processing.
        """
        async with self._project_lock(project_id):
            active = await asyncio.to_thread(
                self.store.active_generation_run, project_id
            )
            if active is not None:
                raise RunSubmitConflict(
                    "run_active", "项目正在生成中，请先停止或等待完成后再修改。",
                    run=active,
                )
            self._refine_projects.add(project_id)
        try:
            yield
        finally:
            self._refine_projects.discard(project_id)

    # ── Execution ──────────────────────────────────────

    def _spawn(self, run_id: str, engine, ctx: _GenerationContext) -> None:
        stop_event = asyncio.Event()
        self._stop_events[run_id] = stop_event
        task = asyncio.create_task(self._execute(run_id, engine, ctx, stop_event))
        self._tasks[run_id] = task

        def _retrieve(t: asyncio.Task) -> None:
            # Consume the outcome so shutdown never leaves an
            # "exception was never retrieved" warning behind.
            if not t.cancelled():
                with suppress(BaseException):
                    t.exception()

        task.add_done_callback(_retrieve)

    async def _execute(
        self, run_id: str, engine, ctx: _GenerationContext, stop_event: asyncio.Event
    ) -> None:
        project_id = ctx.project_id
        run = await asyncio.to_thread(self.store.get_generation_run_required, run_id)
        base_revision = run.base_revision
        base_state_json = run.base_state_json
        completed: list[str] = list(run.completed_steps)

        async def _on_stage_complete(step: str, working: ProjectState) -> None:
            nonlocal base_revision, base_state_json
            if stop_event.is_set():
                raise PipelineStopped()
            label = GENERATION_STEP_LABELS.get(step, step)
            try:
                record = await asyncio.to_thread(
                    self.store.replace_state,
                    project_id,
                    working,
                    base_revision=base_revision,
                    base_state_json=base_state_json,
                    source="pipeline",
                    summary=f"生成：{label}",
                )
            except RevisionConflictError as exc:
                raise _StageConflict(
                    f"项目内容在生成期间被修改，「{label}」阶段的结果未应用，已停止后续阶段。",
                    working,
                ) from exc
            except ProjectStoreError as exc:
                raise _StageConflict(
                    f"保存「{label}」阶段结果失败，已停止后续阶段：{exc}", working
                ) from exc
            base_revision = record.revision
            base_state_json = record.state_json
            completed.append(step)
            progress = {
                "stage": step,
                "message": f"阶段完成：{label}",
                "completed_steps": list(completed),
                "revision": record.revision,
            }
            await asyncio.to_thread(
                self.store.update_generation_run,
                run_id,
                base_revision=base_revision,
                base_state_json=base_state_json,
                completed_steps=completed,
                checkpoint_json=record.state_json,
                last_progress=progress,
            )
            self.broadcast(run_id, "progress", progress)

        try:
            result = await engine.run_full_pipeline(
                user_input=run.request.get("user_input") or ctx.state.user_input,
                title=ctx.state.meta.title or None,
                initial_state=None,
                completed_steps=[],
                on_stage_complete=_on_stage_complete,
            )
            if not completed:
                # Engines that never invoked the stage callback (pre-#14
                # stubs) still get their single atomic final commit.
                try:
                    record = await asyncio.to_thread(
                        self.store.replace_state,
                        project_id,
                        result,
                        base_revision=base_revision,
                        base_state_json=base_state_json,
                        source="pipeline",
                        summary="完整生成",
                    )
                except RevisionConflictError as exc:
                    raise _StageConflict(
                        "项目内容在生成期间被修改，生成结果未应用。", result
                    ) from exc
                base_revision = record.revision
                base_state_json = record.state_json
                completed.append("finalize")
                await asyncio.to_thread(
                    self.store.update_generation_run,
                    run_id,
                    base_revision=base_revision,
                    base_state_json=base_state_json,
                    completed_steps=completed,
                    checkpoint_json=record.state_json,
                )
            summary = _build_result_summary(result)
            run = await asyncio.to_thread(
                self.store.update_generation_run,
                run_id,
                status="succeeded",
                error=None,
                result_summary=summary,
                last_progress={"stage": "complete", "message": "Pipeline complete!"},
            )
            self.broadcast(run_id, "done", _run_done_payload(run))
        except PipelineStopped:
            run = await asyncio.to_thread(
                self.store.update_generation_run,
                run_id,
                status="cancelled",
                error=None,
                last_progress={"stage": "stop", "message": "生成已停止；已完成阶段保留。"},
            )
            self.broadcast(run_id, "done", _run_done_payload(run))
        except _StageConflict as exc:
            run = await asyncio.to_thread(
                self.store.update_generation_run,
                run_id,
                status="failed",
                error=exc.message,
                unapplied_json=serialize_state(exc.unapplied),
            )
            self.broadcast(run_id, "done", _run_done_payload(run))
        except asyncio.CancelledError:
            # Stop/shutdown cancelled the task mid-stage: classify by intent.
            status = "cancelled" if stop_event.is_set() else "interrupted"
            message = (
                "生成已停止；已完成阶段保留。"
                if status == "cancelled"
                else "服务关闭，生成中断；已完成阶段保留，不会自动继续。"
            )
            with suppress(asyncio.CancelledError, ProjectStoreError):
                run = await asyncio.to_thread(
                    self.store.update_generation_run,
                    run_id,
                    status=status,
                    error=message,
                )
                self.broadcast(run_id, "done", _run_done_payload(run))
        except Exception as exc:
            logger.exception("Generation run %s failed", run_id)
            run = await asyncio.to_thread(
                self.store.update_generation_run,
                run_id,
                status="failed",
                error=f"生成失败：{exc}",
            )
            self.broadcast(run_id, "done", _run_done_payload(run))
        finally:
            self._tasks.pop(run_id, None)
            self._stop_events.pop(run_id, None)
            self._finish_subscribers(run_id)

    # ── Stop / shutdown ────────────────────────────────

    async def request_stop(self, run_id: str) -> tuple[GenerationRun, bool]:
        """User stop: no new stages, local waits cancelled. Idempotent."""
        run = await asyncio.to_thread(
            self.store.get_generation_run_required, run_id
        )
        if run.status not in RUN_ACTIVE_STATUSES:
            return run, False
        run = await asyncio.to_thread(
            self.store.update_generation_run,
            run_id,
            status="stopping",
            last_progress={"stage": "stop", "message": "正在停止生成…"},
        )
        stop_event = self._stop_events.get(run_id)
        if stop_event is not None:
            stop_event.set()
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            # Cancels local waits only; whether a remote provider request
            # lands (and bills) is not claimed either way.
            task.cancel()
        return run, True

    async def shutdown(self) -> None:
        """Lifespan teardown: cancel and await every run task, then sweep."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for run_id in list(self._subscribers):
            self._finish_subscribers(run_id)
        with suppress(ProjectStoreError):
            await asyncio.to_thread(self.store.interrupt_stale_generation_runs)

    # ── Subscription ───────────────────────────────────

    def broadcast(self, run_id: str, event: str, data: Any) -> None:
        payload = _sse_event(event, data)
        for queue in list(self._subscribers.get(run_id, ())):
            queue.put_nowait(payload)

    def register(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(queue)
        return queue

    def unregister(self, run_id: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(run_id)
        if queues is not None:
            queues.discard(queue)
            if not queues:
                self._subscribers.pop(run_id, None)

    def _finish_subscribers(self, run_id: str) -> None:
        for queue in list(self._subscribers.get(run_id, ())):
            queue.put_nowait(None)  # sentinel: the run reached a terminal state


async def _run_event_stream(manager: _RunManager, run_id: str):
    """SSE generator observing a run: snapshot first, then live events.

    Disconnecting the generator only unregisters the queue — the run keeps
    going. Terminal runs replay their final state and close immediately, so
    a reconnect never regenerates anything.
    """
    queue = manager.register(run_id)
    try:
        run = await asyncio.to_thread(manager.store.get_generation_run, run_id)
        if run is None:
            return
        yield _sse_event("status", _serialize_run(run))
        if run.status not in RUN_ACTIVE_STATUSES:
            yield _sse_event("done", _run_done_payload(run))
            return
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        manager.unregister(run_id, queue)


def _serialize_run(run: GenerationRun) -> dict:
    """API shape of a run — progress facts only, no bulky state blobs."""
    return {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "kind": run.kind,
        "request_key": run.request_key,
        "status": run.status,
        "base_revision": run.base_revision,
        "completed_steps": run.completed_steps,
        "last_progress": run.last_progress,
        "error": run.error,
        "result_summary": run.result_summary,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _run_done_payload(run: GenerationRun) -> dict:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "error": run.error,
        "completed_steps": run.completed_steps,
        "result_summary": run.result_summary,
    }


async def _latest_run_or_none(project_id: str) -> GenerationRun | None:
    try:
        await asyncio.to_thread(_store().get_required, project_id)
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc
    return await asyncio.to_thread(_store().latest_generation_run, project_id)


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
    active_run = await asyncio.to_thread(
        _store().active_generation_run, project_id
    )
    return _serialize_project(
        record.project_id,
        record.state,
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
        status="running" if active_run is not None else record.state.meta.status.value,
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


# ── Pipeline Execution (background runs, ticket #14) ────────


@app.post("/api/projects/{project_id}/generate")
async def submit_generation(project_id: str, req: GenerateRequest) -> dict:
    """Submit a generation run owned by the server, not the page.

    Returns 200 with ``created: true`` for a new run and ``created: false``
    when an idempotent match was returned (active run with the same
    request_key and identical input, or a resent request that already
    succeeded) — those never trigger a new model call. 409 ``run_active``
    for any other active run, 409 ``request_conflict`` for the same key
    with different input.
    """
    try:
        run, created = await _runs().submit(
            project_id, req.request_key, req.user_input
        )
    except RunSubmitConflict as exc:
        detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.run is not None:
            detail["run"] = _serialize_run(exc.run)
        raise HTTPException(409, detail=detail) from exc
    return {"run": _serialize_run(run), "created": created}


@app.get("/api/projects/{project_id}/generate")
async def generate_alias(project_id: str) -> StreamingResponse:
    """Read-only subscription alias onto the project's most recent run.

    Compatibility shim for the pre-#14 endpoint shape: generation is
    submitted with POST now, and this GET never starts one. A project
    without any run answers 404 ``no_run`` with a submit-first hint.
    """
    run = await _latest_run_or_none(project_id)
    if run is None:
        raise HTTPException(
            404,
            detail={
                "code": "no_run",
                "message": "该项目尚无生成运行；请先 POST /api/projects/{id}/generate 提交。",
            },
        )
    return EventSourceResponse(_run_event_stream(_runs(), run.run_id))


@app.get("/api/projects/{project_id}/runs/latest")
async def latest_run(project_id: str) -> dict:
    """The project's most recent run (progress facts; 404 no_run if none)."""
    run = await _latest_run_or_none(project_id)
    if run is None:
        raise HTTPException(
            404,
            detail={
                "code": "no_run",
                "message": "该项目尚无生成运行。",
            },
        )
    return _serialize_run(run)


@app.get("/api/projects/{project_id}/runs/{run_id}")
async def get_run(project_id: str, run_id: str) -> dict:
    run = await asyncio.to_thread(_runs().store.get_generation_run, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(
            404,
            detail={"code": "run_not_found", "message": f"运行 '{run_id}' 不存在"},
        )
    return _serialize_run(run)


@app.get("/api/projects/{project_id}/runs/{run_id}/events")
async def run_events(project_id: str, run_id: str) -> StreamingResponse:
    """Subscribe to a run: current snapshot first, then live SSE updates.

    Terminal runs replay their result and close — reconnecting never
    regenerates. Dropping the connection never cancels the run.
    """
    run = await asyncio.to_thread(_runs().store.get_generation_run, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(
            404,
            detail={"code": "run_not_found", "message": f"运行 '{run_id}' 不存在"},
        )
    return EventSourceResponse(_run_event_stream(_runs(), run_id))


@app.post("/api/projects/{project_id}/runs/{run_id}/stop")
async def stop_run(project_id: str, run_id: str) -> dict:
    """Explicit stop: no further stages start, local waits are cancelled.

    Idempotent — stopping a terminal run just reports it. The run keeps
    every stage that was already saved; whether an in-flight provider
    request landed (or billed) is not claimed either way.
    """
    manager = _runs()
    run = await asyncio.to_thread(manager.store.get_generation_run, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(
            404,
            detail={"code": "run_not_found", "message": f"运行 '{run_id}' 不存在"},
        )
    if run.status not in RUN_ACTIVE_STATUSES:
        return {"run": _serialize_run(run), "stopped": False}
    run, stopped = await manager.request_stop(run_id)
    return {"run": _serialize_run(run), "stopped": stopped}


@app.post("/api/projects/{project_id}/refine")
async def refine(project_id: str, req: RefineRequest) -> dict:
    """Send an iterative refinement request.

    A 200 means a validated substantive change was CAS-saved and the body
    reports the diff computed from the before/after snapshots — never the
    model's own claim of completion. Refusals answer 422 with a stable
    detail.code; model/protocol failures answer a sanitized 502; nothing is
    written in any non-200 path.
    """
    engine, ctx = await _get_engine(project_id)

    # PipelineEngine.refine runs on an internal copy and raises before any
    # write when the result lacks a substantive, constraint-respecting change.
    # The refine slot keeps the one-active-model-run rule intact: refine and
    # generation never overlap on the same project.
    try:
        async with _runs().refine_slot(project_id):
            updated = await engine.refine(ctx.state, req.message)
    except RunSubmitConflict as exc:
        raise HTTPException(
            409, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except RefinementError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": str(exc)}) from exc
    except RefineExecutionError as exc:
        logger.error("refine model failure for %s: %s", project_id, exc)
        raise HTTPException(
            502,
            detail={"code": "refine_model_failed", "message": "模型调用失败，修改未应用"},
        ) from exc

    updated.meta.id = project_id
    summary = refinement.build_change_summary(ctx.state, updated)
    try:
        record = await asyncio.to_thread(
            ctx.save, updated, "manual", f"修改: {req.message[:60]}"
        )
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc

    body = {
        "project_id": project_id,
        "revision": record.revision,
        "stage": updated.current_stage_status().value,
        "message": "Refinement applied",
        "changed_artifacts": summary["changed_artifacts"],
        "changes": summary["changes"],
        "total_changes": summary["total_changes"],
    }
    # Editing the script never re-runs the storyboard: say so instead of
    # letting stale shots/video prompts masquerade as up to date.
    if "script" in summary["changed_artifacts"] and (
        updated.storyboard or updated.visual_highlights
    ):
        body["notice"] = "本次仅更新剧本，已有分镜和视频提示词未自动同步。"
    return body


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


def _validate_video_gen_shot_ids(shots: list[Shot]) -> None:
    """Shot ids become filenames inside the export ZIP; unsafe ones are refused.

    A shot_id must be a non-empty, unique, single path segment — no path
    separators, no parent-directory references — otherwise it could escape
    the archive's shots/ layout or silently overwrite another shot's file.
    """
    seen: set[str] = set()
    for shot in shots:
        sid = shot.shot_id
        if not sid or "/" in sid or "\\" in sid or sid in {".", ".."} or "\x00" in sid:
            raise HTTPException(422, detail=f"不安全的 shot_id，无法导出: {sid!r}")
        if sid in seen:
            raise HTTPException(422, detail=f"重复的 shot_id，无法导出: {sid!r}")
        seen.add(sid)


def _build_video_gen_zip(state: ProjectState) -> bytes:
    """Build the complete VideoGen ZIP in memory via the existing exporter.

    The exporter is disk-based, so it runs inside a TemporaryDirectory; the
    archive is closed and reduced to bytes before that directory disappears,
    so the returned Response never points at deleted files and both success
    and failure paths clean up. Members come only from this run's output dir
    (relative arcnames); anything unexpected fails the export instead of
    shipping a partial archive as a successful download.
    """
    shots = state.storyboard.shots
    _validate_video_gen_shot_ids(shots)
    expected = {"video_gen_shots.json", "video_gen_shots.csv"} | {
        f"shots/{shot.shot_id}.txt" for shot in shots
    }

    with tempfile.TemporaryDirectory(prefix="scriptweaver-export-") as tmp:
        out_dir = Path(tmp)
        results = export_video_gen_prompts(state, output_dir=out_dir)
        if "error" in results:
            raise HTTPException(400, detail=str(results["error"]))

        actual = {
            p.relative_to(out_dir).as_posix()
            for p in out_dir.rglob("*")
            if p.is_file()
        }
        if actual != expected:
            raise RuntimeError(
                f"VideoGen export produced unexpected files: {sorted(actual ^ expected)}"
            )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in sorted(actual):
                zf.write(out_dir / rel, arcname=rel)
        return buf.getvalue()


@app.get("/api/projects/{project_id}/export/{format_type}")
async def export_project(project_id: str, format_type: str) -> Response:
    """Export the persisted current project as a real, tool-readable file.

    One read of the persistent snapshot serves the whole export; the response
    is a file download (Content-Disposition: attachment), not a JSON envelope:
    json → the raw ProjectState object, fountain → Fountain text,
    video_gen → a ZIP_DEFLATED archive (video_gen_shots.json/.csv + shots/*).
    """
    try:
        record = await asyncio.to_thread(_store().get_required, project_id)
    except ProjectStoreError as exc:
        raise _store_error(exc) from exc

    state: ProjectState = record.state

    if format_type == "json":
        return Response(
            content=export_json(state),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{project_id}.json"'},
        )
    elif format_type == "fountain":
        if not state.script:
            raise HTTPException(400, "No script to export as Fountain")
        return Response(
            content=export_fountain(state.script),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{project_id}.fountain"'},
        )
    elif format_type == "video_gen":
        if not state.storyboard or not state.storyboard.shots:
            raise HTTPException(400, "No storyboard to export")
        zip_bytes = await asyncio.to_thread(_build_video_gen_zip, state)
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{project_id}_video_gen.zip"'
            },
        )
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
