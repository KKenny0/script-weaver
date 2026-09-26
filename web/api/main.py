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

Failed/cancelled/interrupted runs can be resumed from their checkpoint
(ticket #15): POST /runs/{run_id}/resume validates the success prefix, the
execution fingerprint and the project basis under the project lock, then
creates a NEW run that skips the completed stages. Growth (profile/skill
side effects) is recorded separately from content completion and never
replayed by a resume or an export.
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
    GENERATION_STEPS,
    PipelineEngine,
    PipelineStopped,
)
from script_weaver.core.project_store import (
    RUN_ACTIVE_STATUSES,
    ActiveRunConflictError,
    DataDirLock,
    DataDirLockError,
    GenerationRun,
    ProjectNotFoundError,
    ProjectRecord,
    ProjectStore,
    ProjectStoreError,
    RequestKeyConflictError,
    RevisionConflictError,
    RunStageRejectedError,
    content_signature,
    hash_run_request,
    serialize_state,
)
from script_weaver.core.refinement import RefineExecutionError, RefinementError
from script_weaver.core.resume import (
    GROWTH_STATUS_MESSAGES,
    ResumeCheckpoint,
    ResumeRejected,
    assert_fingerprint_match,
    build_checkpoint,
    current_fingerprint,
    decode_checkpoint,
    encode_checkpoint,
    update_checkpoint_envelope,
    validate_completed_steps,
)
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


class ResumeRequest(BaseModel):
    """POST /runs/{run_id}/resume body (ticket #15).

    ``request_key`` is this resume intent's idempotency key: resending the
    same intent returns the same new run; the same key against a different
    resume target or input is a 409.
    """

    request_key: str | None = Field(default=None, max_length=200)


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


def _state_skill_bindings(state: ProjectState) -> dict[str, Any]:
    """The skill bindings execution actually reads: the STATE's bindings.

    Agents consume ``state.skill_bindings`` (not the projects-table
    column), so the resume fingerprint is computed from the same source —
    a checkpoint recorded under one binding set cannot be resumed under
    another.
    """
    return {
        key: binding.model_dump(mode="json")
        for key, binding in (state.skill_bindings or {}).items()
    }


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
    """Late-binding progress sink — the run id exists only after admission.

    Each pipeline notification is broadcast live and persisted as the run's
    recoverable ``last_progress`` (stage granularity, never per token), so a
    reconnecting subscriber sees where the run is without waiting for the
    next event. Completed steps grow only inside the stage transaction.
    """

    def __init__(self, manager: _RunManager):
        self._manager = manager
        self.run_id: str | None = None

    def __call__(self, stage: str, message: str) -> None:
        if self.run_id is None:
            return
        progress = {"stage": stage, "message": message}
        self._manager.broadcast(self.run_id, "progress", progress)
        with suppress(ProjectStoreError):
            self._manager.store.update_generation_run(
                self.run_id, last_progress=progress
            )


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

        The project is loaded (404) and the request snapshot fingerprinted
        first; the idempotency decision and the row creation then happen in
        one store transaction keyed by ``(project_id, request_key)`` over
        runs in every status: same key + same snapshot returns the original
        run without ever building a model instance; same key + different
        snapshot is a conflict; any other active run makes the project busy.
        Admission also records the execution fingerprint and the resume
        envelope (ticket #15): every stage commit advances that envelope, so
        the run is resumable from its last successful stage. A genuinely
        created run registers its task INSIDE the same project lock — from
        the moment the row exists, a cancellable task owns it through
        initialization to the terminal state (engine construction happens
        in the task, so a stop during it cancels the run for good). The
        response is re-read after registration: it reports the run's
        current state, never the admission-time snapshot.
        """
        try:
            record = await asyncio.to_thread(self.store.get_required, project_id)
        except ProjectStoreError as exc:
            raise _store_error(exc) from exc
        request = {
            "user_input": user_input if user_input is not None else record.state.user_input,
            "auto_approve": bool(record.auto_approve),
            "skill_bindings": record.skill_bindings or {},
        }
        # The execution fingerprint lives on the CHECKPOINT (the resume
        # basis), not in the request snapshot: a resent request_key must
        # keep answering with its original run even if skill files or
        # model settings changed meanwhile — no new run, no model call.
        fingerprint = await current_fingerprint(
            skill_bindings=_state_skill_bindings(record.state),
            auto_approve=record.auto_approve,
        )
        request_hash = hash_run_request(request)
        key = request_key or f"auto-{request_hash}"
        checkpoint = build_checkpoint(
            user_input=request["user_input"],
            fingerprint=fingerprint,
            basis={"revision": record.revision},
        )

        async with self._project_lock(project_id):
            if project_id in self._refine_projects:
                raise RunSubmitConflict(
                    "run_active", "该项目正在处理修改请求，请稍后再提交生成。"
                )
            try:
                run, created = await asyncio.to_thread(
                    self.store.admit_generation_run,
                    project_id,
                    kind="generate",
                    request_key=key,
                    request=request,
                    request_hash=request_hash,
                    base_revision=record.revision,
                    base_state_json=record.state_json,
                    checkpoint_json=encode_checkpoint(checkpoint),
                )
            except RequestKeyConflictError as exc:
                raise RunSubmitConflict(
                    "request_conflict",
                    "同一 request_key 已携带不同输入，提交被拒绝。",
                    run=exc.run,
                ) from exc
            except ActiveRunConflictError as exc:
                raise RunSubmitConflict(
                    "run_active", "该项目已有生成运行进行中，请等待完成或先停止。",
                    run=exc.run,
                ) from exc
            if not created:
                return run, False
            # Registration joins admission under the project lock: a stop
            # coordinates through the same lock, so there is no window in
            # which a persisted run has no cancellable task behind it.
            self._spawn(run.run_id)
        # Report the run's CURRENT state: an immediate initialization
        # failure or a racing stop may already have settled the row.
        run = await asyncio.to_thread(
            self.store.get_generation_run_required, run.run_id
        )
        return run, True

    async def resume(
        self, project_id: str, run_id: str, request_key: str | None
    ) -> tuple[GenerationRun, bool]:
        """Admit a run that continues a terminal run from its checkpoint.

        Every eligibility check and the new run's admission coordinate
        under the SAME project lock as submit/refine, so the validated
        basis can never go stale between check and row creation. The
        original run row is never resurrected: a new run record is created
        that inherits the checkpointed success prefix, and the original
        (failed/cancelled/interrupted) row plus its checkpoint stay intact
        for history. All refusals raise :class:`RunSubmitConflict` with a
        stable code — none of them builds a model instance.
        """
        try:
            record = await asyncio.to_thread(self.store.get_required, project_id)
        except ProjectStoreError as exc:
            raise _store_error(exc) from exc
        async with self._project_lock(project_id):
            if project_id in self._refine_projects:
                raise RunSubmitConflict(
                    "run_active", "该项目正在处理修改请求，请稍后再恢复生成。"
                )
            original = await asyncio.to_thread(
                self.store.get_generation_run, run_id
            )
            if original is None or original.project_id != project_id:
                raise ProjectNotFoundError(f"Run '{run_id}' not found")
            if original.status in RUN_ACTIVE_STATUSES:
                raise RunSubmitConflict(
                    "run_active",
                    "原运行仍在进行中，无法恢复；请先等待完成或停止。",
                    run=original,
                )
            # A pre-#15 run stored a raw ProjectState as its checkpoint:
            # preserved and viewable, but never a basis to guess a resume.
            checkpoint: ResumeCheckpoint | None = None
            if original.checkpoint_json is not None:
                try:
                    checkpoint = decode_checkpoint(original.checkpoint_json)
                except ResumeRejected as exc:
                    raise RunSubmitConflict(
                        "resume_basis_missing",
                        "该运行缺少可用的恢复依据（旧格式或已损坏），"
                        "已保留可查看；如需继续请从头生成。",
                        run=original,
                    ) from exc
            if checkpoint is not None and checkpoint.content_complete:
                raise RunSubmitConflict(
                    "content_complete",
                    "该运行的内容已完整生成，无需恢复；可直接查看或导出。",
                    run=original,
                )
            if original.status not in ("failed", "cancelled", "interrupted"):
                raise RunSubmitConflict(
                    "resume_not_allowed",
                    f"状态为 {original.status} 的运行不能恢复。",
                    run=original,
                )
            if checkpoint is None:
                raise RunSubmitConflict(
                    "resume_basis_missing",
                    "该运行缺少恢复依据（未保存任何进度），已保留可查看；"
                    "如需继续请从头生成。",
                    run=original,
                )
            state_model = checkpoint.state_model()
            try:
                validate_completed_steps(
                    original.completed_steps, state=state_model
                )
            except ResumeRejected as exc:
                raise RunSubmitConflict(exc.code, exc.message, run=original) from exc
            if original.completed_steps and checkpoint.completed_steps != (
                original.completed_steps
            ):
                raise RunSubmitConflict(
                    "resume_basis_missing",
                    "运行进度与 checkpoint 记录不一致，拒绝猜测续跑；已保留原记录。",
                    run=original,
                )
            # The project must still hold exactly the checkpointed content:
            # any real edit since the interruption refuses the resume (a
            # rename alone stays resumable — it is not a content change).
            if state_model is not None and content_signature(
                record.state_json
            ) != content_signature(serialize_state(state_model)):
                raise RunSubmitConflict(
                    "project_changed",
                    "项目内容在运行中断后已被修改，无法安全续跑；可从头生成"
                    "（历史版本会保留）。",
                    run=original,
                )
            fingerprint = await current_fingerprint(
                skill_bindings=_state_skill_bindings(record.state),
                auto_approve=record.auto_approve,
            )
            try:
                assert_fingerprint_match(checkpoint.fingerprint, fingerprint)
            except ResumeRejected as exc:
                raise RunSubmitConflict(exc.code, exc.message, run=original) from exc

            request = {
                "user_input": checkpoint.user_input or record.state.user_input,
                "auto_approve": bool(record.auto_approve),
                "skill_bindings": record.skill_bindings or {},
                "resume_of": original.run_id,
            }
            request_hash = hash_run_request(request)
            key = request_key or f"auto-resume-{run_id[:8]}-{request_hash}"
            resumed_checkpoint = checkpoint.model_copy(deep=True)
            resumed_checkpoint.user_input = request["user_input"]
            resumed_checkpoint.growth_status = "pending"
            resumed_checkpoint.growth_error = None
            resumed_checkpoint.basis = {
                "revision": record.revision,
                "resumed_from": original.run_id,
            }
            try:
                run, created = await asyncio.to_thread(
                    self.store.admit_generation_run,
                    project_id,
                    kind="generate",
                    request_key=key,
                    request=request,
                    request_hash=request_hash,
                    base_revision=record.revision,
                    base_state_json=record.state_json,
                    checkpoint_json=encode_checkpoint(resumed_checkpoint),
                    completed_steps=list(original.completed_steps),
                )
            except RequestKeyConflictError as exc:
                raise RunSubmitConflict(
                    "request_conflict",
                    "同一 request_key 已绑定其他恢复目标或输入，提交被拒绝。",
                    run=exc.run,
                ) from exc
            except ActiveRunConflictError as exc:
                raise RunSubmitConflict(
                    "run_active", "该项目已有生成运行进行中，请等待完成或先停止。",
                    run=exc.run,
                ) from exc
            if not created:
                return run, False
            self._spawn(run.run_id)
        run = await asyncio.to_thread(
            self.store.get_generation_run_required, run.run_id
        )
        return run, True

    @asynccontextmanager
    async def refine_slot(self, project_id: str):
        """Admission for the synchronous refine endpoint.

        Refine is a model run too: inside the project lock both an active
        generation and an in-flight refine are rejected, so two concurrent
        refines can never both reach the model. The slot is held by its
        owner across the whole operation — base snapshot, model call,
        validation and CAS save — and released only on success or failure.
        """
        async with self._project_lock(project_id):
            if project_id in self._refine_projects:
                raise RunSubmitConflict(
                    "run_active", "该项目正在处理另一条修改请求，请稍后再试。"
                )
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

    def _spawn(self, run_id: str) -> None:
        stop_event = asyncio.Event()
        self._stop_events[run_id] = stop_event
        task = asyncio.create_task(self._execute(run_id, stop_event))
        self._tasks[run_id] = task

        def _retrieve(t: asyncio.Task) -> None:
            # Single guaranteed owner of registry cleanup and subscriber
            # finish — it runs even when the coroutine was cancelled before
            # its first step (so _execute's body never ran). Also consume
            # the outcome so no "exception was never retrieved" warning
            # survives a shutdown.
            self._tasks.pop(run_id, None)
            self._stop_events.pop(run_id, None)
            self._finish_subscribers(run_id)
            if not t.cancelled():
                with suppress(BaseException):
                    t.exception()

        task.add_done_callback(_retrieve)

    async def _converge_revoked_run(self, run: GenerationRun) -> None:
        """Finish a run that left ``running`` before its task could execute
        (a stop or shutdown won the admission race): converge the row and
        tell live subscribers — without ever building an engine."""
        if run.status == "stopping":
            run = await asyncio.to_thread(
                self.store.settle_generation_run,
                run.run_id,
                status="cancelled",
                last_progress={
                    "stage": "stop",
                    "message": "生成已停止；已完成阶段保留。",
                },
            )
        self.broadcast(run.run_id, "done", _run_done_payload(run))

    async def _execute(
        self, run_id: str, stop_event: asyncio.Event
    ) -> None:
        # The initial database read lives inside the try: a stop (or
        # shutdown) that lands during it still converges to a terminal row.
        relay = _ProgressRelay(self)
        relay.run_id = run_id
        try:
            run = await asyncio.to_thread(
                self.store.get_generation_run_required, run_id
            )
            if run.status != "running":
                # The run ended before this task got its turn: converge
                # without ever touching the model.
                await self._converge_revoked_run(run)
                return
            # Engine initialization belongs to the server-owned task: from
            # admission to the terminal state a cancellable task is
            # responsible for the run — a stop during construction cancels
            # it right here instead of racing a later _spawn.
            try:
                engine, ctx = await _get_engine(
                    run.project_id, progress_callback=relay
                )
            except HTTPException as exc:
                # The admitted run can never start (e.g. model not
                # configured): settle it failed so the row never claims to
                # be active, and report through the run's own channel.
                detail = exc.detail
                message = (
                    detail.get("message", str(detail))
                    if isinstance(detail, dict)
                    else str(detail)
                )
                run = await asyncio.to_thread(
                    self.store.settle_generation_run,
                    run_id,
                    status="failed",
                    error=f"生成无法启动：{message}",
                )
                self.broadcast(run_id, "done", _run_done_payload(run))
                return
            # Initialization may have taken a while: re-read before
            # executing, so a stop accepted during construction is never
            # overridden by this task.
            run = await asyncio.to_thread(
                self.store.get_generation_run_required, run_id
            )
            if run.status != "running":
                await self._converge_revoked_run(run)
                return
            base_revision = run.base_revision
            base_state_json = run.base_state_json
            completed: list[str] = list(run.completed_steps)

            # Ticket #15: a run admitted with a resume envelope continues
            # from the checkpointed success prefix. The envelope was fully
            # validated at admission; decoding here feeds the SAME public
            # contract into the pipeline, which re-validates prefix and
            # fingerprint against its own resolved instance before the
            # first model call — check and execution share one basis.
            resume_from: ResumeCheckpoint | None = None
            if run.checkpoint_json is not None:
                try:
                    resume_from = decode_checkpoint(run.checkpoint_json)
                except ResumeRejected as exc:
                    if completed:
                        # A broken basis under a claimed prefix must never
                        # silently restart those stages from zero.
                        raise _StageConflict(
                            f"运行检查点不可用，已停止：{exc.message}", ctx.state
                        ) from None
                    resume_from = None
            initial_state = (
                resume_from.state_model() if resume_from is not None else None
            )
            if resume_from is not None:
                completed = list(resume_from.completed_steps)

            async def _on_stage_complete(step: str, working: ProjectState) -> None:
                nonlocal base_revision, base_state_json, completed
                if stop_event.is_set():
                    raise PipelineStopped()
                label = GENERATION_STEP_LABELS.get(step, step)
                progress = {
                    "stage": step,
                    "message": f"阶段完成：{label}",
                    "completed_steps": [*completed, step],
                }
                try:
                    # One transaction: revision CAS + project snapshot +
                    # version history + run checkpoint. Its returned rows are
                    # the exact writes this stage committed — never a later
                    # re-read — and a run that already left ``running``
                    # (accepted stop) rejects the stage outright.
                    record, updated_run = await asyncio.to_thread(
                        self.store.commit_generation_stage,
                        run_id,
                        state=working,
                        base_revision=base_revision,
                        base_state_json=base_state_json,
                        step=step,
                        summary=f"生成：{label}",
                        progress=progress,
                    )
                except RunStageRejectedError:
                    # Stop was accepted first: refuse the new stage; the run
                    # settles as cancelled with earlier stages kept.
                    raise PipelineStopped() from None
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
                completed = list(updated_run.completed_steps)
                progress["revision"] = record.revision
                self.broadcast(run_id, "progress", progress)

            async def _on_growth_event(status: str, error: str | None) -> None:
                """Persist each growth transition onto the run's checkpoint.

                ``running`` lands before any side effect; a terminal status
                records the outcome. Non-envelope runs (legacy seeds) are
                skipped — nothing to record on.
                """
                try:
                    current = await asyncio.to_thread(
                        self.store.get_generation_run_required, run_id
                    )
                except ProjectStoreError:
                    return
                if current.checkpoint_json is None:
                    return
                updated = update_checkpoint_envelope(
                    current.checkpoint_json,
                    growth_status=status,
                    growth_error=error,
                )
                with suppress(ProjectStoreError):
                    await asyncio.to_thread(
                        self.store.update_generation_run,
                        run_id,
                        checkpoint_json=updated,
                    )
                message = GROWTH_STATUS_MESSAGES.get(status, status)
                if status == "failed" and error:
                    message = f"{message}（{error}）"
                self.broadcast(run_id, "progress", {"stage": "growth", "message": message})

            result = await engine.run_full_pipeline(
                user_input=run.request.get("user_input") or ctx.state.user_input,
                title=ctx.state.meta.title or None,
                initial_state=initial_state,
                completed_steps=completed,
                on_stage_complete=_on_stage_complete,
                resume_from=resume_from,
                on_growth_event=_on_growth_event,
            )
            if not completed:
                # Engines that never invoked the stage callback (pre-#14
                # stubs) still get their single atomic final commit through
                # the same stage-transaction path.
                if stop_event.is_set():
                    raise PipelineStopped()
                try:
                    record, updated_run = await asyncio.to_thread(
                        self.store.commit_generation_stage,
                        run_id,
                        state=result,
                        base_revision=base_revision,
                        base_state_json=base_state_json,
                        step="finalize",
                        summary="完整生成",
                        progress={"stage": "complete", "message": "Pipeline complete!"},
                    )
                except RunStageRejectedError:
                    raise PipelineStopped() from None
                except RevisionConflictError as exc:
                    raise _StageConflict(
                        "项目内容在生成期间被修改，生成结果未应用。", result
                    ) from exc
                except ProjectStoreError as exc:
                    raise _StageConflict(
                        f"保存生成结果失败，已停止：{exc}", result
                    ) from exc
                base_revision = record.revision
                base_state_json = record.state_json
                completed = list(updated_run.completed_steps)
            summary = _build_result_summary(result)
            # Atomic terminal write: a stop accepted first refuses success —
            # settle itself decides the recorded outcome by transition order.
            run = await asyncio.to_thread(
                self.store.settle_generation_run,
                run_id,
                status="succeeded",
                error=None,
                result_summary=summary,
                last_progress={"stage": "complete", "message": "Pipeline complete!"},
            )
            self.broadcast(run_id, "done", _run_done_payload(run))
        except PipelineStopped:
            run = await self._settle_terminal(
                run_id,
                status="cancelled",
                last_progress={"stage": "stop", "message": "生成已停止；已完成阶段保留。"},
            )
            self.broadcast(run_id, "done", _run_done_payload(run))
        except _StageConflict as exc:
            run = await self._settle_terminal(
                run_id,
                status="failed",
                error=exc.message,
                unapplied_json=serialize_state(exc.unapplied),
            )
            self.broadcast(run_id, "done", _run_done_payload(run))
        except asyncio.CancelledError:
            # Stop/shutdown cancelled the task — possibly before its first
            # store read or even before the coroutine ever ran. The row is
            # converged by whoever observes the cancellation (here, or in
            # request_stop, or the shutdown sweep).
            status = "cancelled" if stop_event.is_set() else "interrupted"
            message = (
                "生成已停止；已完成阶段保留。"
                if status == "cancelled"
                else "服务关闭，生成中断；已完成阶段保留，不会自动继续。"
            )
            with suppress(asyncio.CancelledError, ProjectStoreError):
                run = await self._settle_terminal(run_id, status=status, error=message)
                self.broadcast(run_id, "done", _run_done_payload(run))
        except Exception as exc:
            logger.exception("Generation run %s failed", run_id)
            run = await self._settle_terminal(
                run_id, status="failed", error=f"生成失败：{exc}"
            )
            self.broadcast(run_id, "done", _run_done_payload(run))

    async def _settle_terminal(
        self, run_id: str, *, status: str, **updates: Any
    ) -> GenerationRun:
        """Settle a run, converging a still-running growth record first.

        A run that ends while its growth was mid-flight (stop, crash,
        shutdown) leaves ``growth_status=running`` on its checkpoint — an
        unfinished side-effect window. It is converged to ``interrupted``
        in the same settle write, so the record never implies completed
        growth effects and a later export never replays them.
        """
        if status != "succeeded":
            with suppress(ProjectStoreError):
                current = await asyncio.to_thread(
                    self.store.get_generation_run, run_id
                )
                if current is not None and current.checkpoint_json:
                    try:
                        envelope = json.loads(current.checkpoint_json)
                    except json.JSONDecodeError:
                        envelope = None
                    if (
                        isinstance(envelope, dict)
                        and envelope.get("growth_status") == "running"
                    ):
                        updates.setdefault(
                            "checkpoint_json",
                            update_checkpoint_envelope(
                                current.checkpoint_json,
                                growth_status="interrupted",
                                growth_error=(
                                    "成长任务随运行中止而中断，"
                                    "其副作用可能已部分发生；不会自动重放。"
                                ),
                            ),
                        )
        return await asyncio.to_thread(
            self.store.settle_generation_run, run_id, status=status, **updates
        )

    # ── Stop / shutdown ────────────────────────────────

    async def request_stop(self, run_id: str) -> tuple[GenerationRun, bool]:
        """User stop: no new stages, local waits cancelled. Idempotent.

        The stopping transition, the stop flag and the task cancellation
        run under the project lock — the same lock that joins a run's
        admission with its task registration — so a stop always meets either
        a registered task or a run that already ended; an admitted run can
        never slip past a completed stop into execution. Waiting for the
        cancelled task happens OUTSIDE the lock (its convergence may take
        further store transactions) so the project never blocks behind it.
        The running→stopping transition itself is one atomic store
        operation, so a repeated stop (or one racing completion) is decided
        by transition order: terminal rows answer ``stopped=False``
        untouched, an already-stopping row is not cancelled again, and a
        success that settles first simply wins.
        """
        run = await asyncio.to_thread(self.store.get_generation_run, run_id)
        if run is None:
            raise ProjectNotFoundError(f"Run '{run_id}' not found")
        task: asyncio.Task | None = None
        async with self._project_lock(run.project_id):
            run, accepted = await asyncio.to_thread(
                self.store.transition_generation_run_stopping, run_id
            )
            if not accepted:
                return run, False
            stop_event = self._stop_events.get(run_id)
            if stop_event is not None:
                stop_event.set()
            task = self._tasks.get(run_id)
            if task is not None:
                # Cancels local waits only; whether a remote provider
                # request lands (and bills) is not claimed either way.
                task.cancel()
        if task is not None:
            with suppress(BaseException):
                await task
        # The coroutine settles itself; if it never got to run (cancelled
        # before its first step) the row still says stopping — finish the
        # stop here so the run always converges to a terminal state.
        run = await asyncio.to_thread(
            self.store.get_generation_run_required, run_id
        )
        if run.status == "stopping":
            run = await asyncio.to_thread(
                self.store.settle_generation_run,
                run_id,
                status="cancelled",
                last_progress={"stage": "stop", "message": "生成已停止；已完成阶段保留。"},
            )
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
    """API shape of a run — progress facts only, no bulky state blobs.

    ``content_complete`` and ``next_step``/``next_step_label`` are the
    ticket-#15 resume facts: what already finished and where a resume
    would continue from. Pure local computation — no store or model work.
    """
    completed = run.completed_steps
    pending = [s for s in GENERATION_STEPS if s not in completed]
    next_step = pending[0] if pending else None
    return {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "kind": run.kind,
        "request_key": run.request_key,
        "status": run.status,
        "base_revision": run.base_revision,
        "completed_steps": completed,
        "completed_step_labels": [
            GENERATION_STEP_LABELS.get(s, s) for s in completed
        ],
        "next_step": next_step,
        "next_step_label": GENERATION_STEP_LABELS.get(next_step, next_step) if next_step else None,
        "content_complete": "finalize" in completed,
        "last_progress": run.last_progress,
        "error": run.error,
        "result_summary": run.result_summary,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _run_done_payload(run: GenerationRun) -> dict:
    completed = run.completed_steps
    pending = [s for s in GENERATION_STEPS if s not in completed]
    next_step = pending[0] if pending else None
    return {
        "run_id": run.run_id,
        "status": run.status,
        "error": run.error,
        "completed_steps": completed,
        "completed_step_labels": [
            GENERATION_STEP_LABELS.get(s, s) for s in completed
        ],
        "next_step": next_step,
        "next_step_label": (
            GENERATION_STEP_LABELS.get(next_step, next_step) if next_step else None
        ),
        "content_complete": "finalize" in completed,
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


@app.post("/api/projects/{project_id}/runs/{run_id}/resume")
async def resume_run(project_id: str, run_id: str, req: ResumeRequest) -> dict:
    """Resume a failed/cancelled/interrupted run from its checkpoint.

    Creates a NEW run that reuses every still-valid successful stage; the
    original row and its checkpoint are preserved untouched (a failed run
    is never flipped back to running). The response matches the generate
    submit contract (``run`` + ``created``); resending one intent's
    ``request_key`` returns the same new run. Every refusal is a 409 with
    a stable ``detail.code`` — run_active / content_complete /
    resume_basis_missing / invalid_prefix / config_changed /
    project_changed / resume_not_allowed — and none of them ever builds a
    model instance.
    """
    try:
        run, created = await _runs().resume(project_id, run_id, req.request_key)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            404,
            detail={"code": "run_not_found", "message": f"运行 '{run_id}' 不存在"},
        ) from exc
    except RunSubmitConflict as exc:
        detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.run is not None:
            detail["run"] = _serialize_run(exc.run)
        raise HTTPException(409, detail=detail) from exc
    return {"run": _serialize_run(run), "created": created}


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
    # The refine slot keeps the one-active-model-run rule intact across the
    # WHOLE operation — base snapshot, model call, validation and CAS save —
    # so a generation can never slip in between the model's answer and its
    # save, and it is released by the holder on success or failure alike.
    try:
        async with _runs().refine_slot(project_id):
            try:
                updated = await engine.refine(ctx.state, req.message)
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
    except RunSubmitConflict as exc:
        raise HTTPException(
            409, detail={"code": exc.code, "message": exc.message}
        ) from exc

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
