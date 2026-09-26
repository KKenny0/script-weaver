"""Background generation-run lifecycle checks with a controlled pipeline.

Ticket #14 deliberately replaces the PR #12 "disconnect cancels the
pipeline" contract: a run belongs to the server process, so dropping the SSE
subscription changes nothing. What does end a run is an explicit stop, a
failing stage, a conflicting user edit — or process shutdown, which leaves
``interrupted`` runs that a restart reports but never resumes. Every case
below runs offline against the real store via a scripted engine.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from script_weaver.core.project_store import ProjectStoreError
from script_weaver.core.types import ProjectState


@pytest.fixture
async def client(api_factory):
    api = api_factory()
    async with api.app.router.lifespan_context(api.app):
        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as c:
            yield api, c


async def create_project(c: httpx.AsyncClient, idea: str) -> dict:
    r = await c.post("/api/projects", json={"user_input": idea})
    return r.json()


def scripted_engine(api, project_id, pipeline, *, engine_calls=None):
    """Patch ``_get_engine`` with a scripted run_full_pipeline over the real store."""

    async def fake_get_engine(pid, **kwargs):
        if engine_calls is not None:
            engine_calls.append(pid)
        record = api._runtime.store.get_required(pid)
        engine = SimpleNamespace(run_full_pipeline=pipeline)
        ctx = api._GenerationContext(
            project_id=record.project_id,
            revision=record.revision,
            base_state_json=record.state_json,
            state=record.state,
            store=api._runtime.store,
            auto_approve=record.auto_approve,
            skill_bindings=record.skill_bindings,
        )
        return engine, ctx

    api._get_engine = fake_get_engine


async def await_terminal(api, run_id: str, timeout: float = 5.0) -> dict:
    for _ in range(200):
        run = api._runtime.store.get_generation_run(run_id)
        assert run is not None
        if run.status not in ("running", "stopping"):
            return {
                "status": run.status,
                "error": run.error,
                "completed_steps": run.completed_steps,
                "unapplied": run.unapplied_json,
            }
        await asyncio.sleep(timeout / 200)
    raise AssertionError(f"run {run_id} did not reach a terminal state in {timeout}s")


def state_with(user_input: str, logline: str) -> ProjectState:
    from script_weaver.core.types import BasicInfo, Outline, ProjectStatus

    state = ProjectState(user_input=user_input)
    state.refined_idea = logline
    state.outline = Outline(basic_info=BasicInfo(logline=logline))
    state.meta.status = ProjectStatus.STRUCTURED
    return state


# ── Idempotency and the one-active-run rule ────────────


async def test_duplicate_submit_same_key_same_input_returns_original_run(client):
    api, c = client
    p = await create_project(c, "幂等故事")
    calls = []
    release = asyncio.Event()

    async def pipeline(**kwargs):
        await release.wait()
        return state_with("幂等故事", "最终大纲")

    scripted_engine(api, p["project_id"], pipeline, engine_calls=calls)

    first = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k1"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True

    again = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k1"}
    )
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["created"] is False
    assert body["run"]["run_id"] == first.json()["run"]["run_id"]
    assert body["run"]["status"] == "running"

    # Only the first submission ever started a task; no second run row exists.
    latest = api._runtime.store.latest_generation_run(p["project_id"])
    assert latest.run_id == first.json()["run"]["run_id"]
    release.set()
    terminal = await await_terminal(api, first.json()["run"]["run_id"])
    assert terminal["status"] == "succeeded"


async def test_same_key_different_input_conflicts_and_other_key_is_busy(client):
    api, c = client
    p = await create_project(c, "互斥故事")
    release = asyncio.Event()

    async def pipeline(**kwargs):
        await release.wait()
        return state_with("互斥故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline)

    first = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k1"}
    )
    assert first.status_code == 200

    conflict = await c.post(
        f"/api/projects/{p['project_id']}/generate",
        json={"request_key": "k1", "user_input": "换一个完全不同的输入"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "request_conflict"

    busy = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k2"}
    )
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "run_active"

    release.set()
    assert (await await_terminal(api, first.json()["run"]["run_id"]))["status"] == "succeeded"


async def test_resend_of_succeeded_request_returns_result_without_new_run(client):
    api, c = client
    p = await create_project(c, "重发故事")

    async def pipeline(**kwargs):
        return state_with("重发故事", "一次就够")

    scripted_engine(api, p["project_id"], pipeline)

    first = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k1"}
    )
    run_id = first.json()["run"]["run_id"]
    assert (await await_terminal(api, run_id))["status"] == "succeeded"

    resent = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k1"}
    )
    assert resent.status_code == 200
    body = resent.json()
    assert body["created"] is False
    assert body["run"]["run_id"] == run_id
    assert body["run"]["status"] == "succeeded"
    # No second run row was created for the resent request.
    latest = await c.get(f"/api/projects/{p['project_id']}/runs/latest")
    assert latest.json()["run_id"] == run_id


# ── Detachment: disconnect ≠ failure ──────────────────


async def test_subscriber_disconnect_does_not_cancel_the_run(client):
    """The updated PR #12 contract: dropping the SSE stream is observation
    only — the run keeps going, saves its stages, and reaches ``succeeded``."""
    api, c = client
    p = await create_project(c, "断连故事")
    release = asyncio.Event()

    async def pipeline(**kwargs):
        on_stage = kwargs["on_stage_complete"]
        await on_stage("idea_refiner", state_with("断连故事", "概念"))
        await release.wait()
        await on_stage("structurer", state_with("断连故事", "断连后的完整大纲"))
        return state_with("断连故事", "断连后的完整大纲")

    scripted_engine(api, p["project_id"], pipeline)

    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    run_id = submitted.json()["run"]["run_id"]

    response = await api.run_events(p["project_id"], run_id)
    iterator = response.body_iterator
    assert (await anext(iterator))["event"] == "status"
    # Subscriber leaves mid-run — exactly the old disconnect scenario.
    pending = asyncio.create_task(anext(iterator))
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await iterator.aclose()

    release.set()
    terminal = await await_terminal(api, run_id)
    assert terminal["status"] == "succeeded"

    # The project really advanced while nobody was listening.
    project = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert project["revision"] == 3  # creation + two checkpointed stages
    assert project["outline"]["basic_info"]["logline"] == "断连后的完整大纲"


async def test_terminal_reconnect_replays_result_and_never_regenerates(client):
    api, c = client
    p = await create_project(c, "重连故事")
    calls = []

    async def pipeline(**kwargs):
        calls.append("pipeline")
        return state_with("重连故事", "最终大纲")

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    run_id = submitted.json()["run"]["run_id"]
    await await_terminal(api, run_id)

    response = await api.run_events(p["project_id"], run_id)
    events = [event async for event in response.body_iterator]
    assert [e["event"] for e in events] == ["status", "done"]
    assert json.loads(events[0]["data"])["status"] == "succeeded"
    assert json.loads(events[1]["data"])["status"] == "succeeded"
    assert calls == ["pipeline"]  # reconnecting added no model call


async def test_get_generate_alias_subscribes_latest_or_hints_to_submit(client):
    api, c = client
    p = await create_project(c, "别名故事")

    missing = await c.get(f"/api/projects/{p['project_id']}/generate")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "no_run"
    assert "提交" in missing.json()["detail"]["message"]

    async def pipeline(**kwargs):
        return state_with("别名故事", "别名大纲")

    scripted_engine(api, p["project_id"], pipeline)
    await c.post(f"/api/projects/{p['project_id']}/generate", json={})
    latest = await c.get(f"/api/projects/{p['project_id']}/runs/latest")
    await await_terminal(api, latest.json()["run_id"])

    response = await api.generate_alias(p["project_id"])
    events = [event async for event in response.body_iterator]
    assert events[-1]["event"] == "done"


# ── Explicit stop ─────────────────────────────────────


async def test_explicit_stop_prevents_further_stages_and_is_idempotent(client):
    api, c = client
    p = await create_project(c, "停止故事")
    stage2_entered = asyncio.Event()
    release_stage2 = asyncio.Event()
    stages_seen = []

    async def pipeline(**kwargs):
        on_stage = kwargs["on_stage_complete"]
        await on_stage("idea_refiner", state_with("停止故事", "概念"))
        stages_seen.append("idea_refiner")
        stage2_entered.set()
        await release_stage2.wait()  # cancelled by stop
        stages_seen.append("structurer")
        await on_stage("structurer", state_with("停止故事", "不该到这里"))
        return state_with("停止故事", "不该完成")

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    run_id = submitted.json()["run"]["run_id"]
    await stage2_entered.wait()

    stop = await c.post(f"/api/projects/{p['project_id']}/runs/{run_id}/stop")
    assert stop.status_code == 200
    assert stop.json()["stopped"] is True
    # request_stop converges the run before answering: the atomic
    # running→stopping transition plus the settled terminal state.
    assert stop.json()["run"]["status"] == "cancelled"

    terminal = await await_terminal(api, run_id)
    assert terminal["status"] == "cancelled"
    assert terminal["completed_steps"] == ["idea_refiner"]
    assert stages_seen == ["idea_refiner"]  # stage 2 never completed

    # Repeated stop is safe and reports the terminal run untouched.
    again = await c.post(f"/api/projects/{p['project_id']}/runs/{run_id}/stop")
    assert again.status_code == 200
    assert again.json()["stopped"] is False
    assert again.json()["run"]["status"] == "cancelled"

    # The checkpointed stage survives the stop.
    project = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert project["refined_idea"] == "概念"


# ── Stage persistence and honest failure ──────────────


async def test_each_successful_stage_is_persisted_and_failure_keeps_priors(client):
    api, c = client
    p = await create_project(c, "分阶段故事")

    async def pipeline(**kwargs):
        on_stage = kwargs["on_stage_complete"]
        await on_stage("idea_refiner", state_with("分阶段故事", "概念"))
        await on_stage("structurer", state_with("分阶段故事", "第一版大纲"))
        raise RuntimeError("model exploded at script stage")

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    terminal = await await_terminal(api, submitted.json()["run"]["run_id"])
    assert terminal["status"] == "failed"
    assert "model exploded" in terminal["error"]
    assert terminal["completed_steps"] == ["idea_refiner", "structurer"]

    # Two stage checkpoints + creation: prior results were saved in time.
    versions = (await c.get(f"/api/projects/{p['project_id']}/versions")).json()
    pipeline_versions = [v for v in versions["versions"] if v["source"] == "pipeline"]
    assert [v["summary"] for v in pipeline_versions] == ["生成：故事大纲", "生成：概念精炼"]
    project = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert project["outline"]["basic_info"]["logline"] == "第一版大纲"


async def test_storyboard_partial_scene_failure_is_not_a_completed_stage(client):
    """A storyboard agent that shots some scenes then fails must not be
    checkpointed as if the stage completed — partial shots never masquerade
    as the full storyboard stage."""
    api, c = client
    p = await create_project(c, "分镜断点故事")

    async def pipeline(**kwargs):
        on_stage = kwargs["on_stage_complete"]
        await on_stage("idea_refiner", state_with("分镜断点故事", "概念"))
        await on_stage("structurer", state_with("分镜断点故事", "大纲"))
        # StoryboardArtist's own per-scene contract: scene 1 done, scene 2
        # fails → the agent returns an error payload, _run_agent raises.
        raise RuntimeError(
            "storyboard_artist failed: {'error': 'max_iterations_exceeded',"
            " 'failed_scene_id': 'sc_2', 'completed_scenes': 1}"
        )

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    terminal = await await_terminal(api, submitted.json()["run"]["run_id"])
    assert terminal["status"] == "failed"
    assert "storyboard_artist" in terminal["error"]
    assert terminal["completed_steps"] == ["idea_refiner", "structurer"]
    run = api._runtime.store.get_generation_run(submitted.json()["run"]["run_id"])
    assert "storyboard_artist" not in run.completed_steps
    project = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert project["storyboard"] is None  # nothing partial was saved


async def test_user_edit_during_generation_keeps_result_unapplied(client):
    api, c = client
    p = await create_project(c, "并发编辑故事")
    between_stages = asyncio.Event()
    release = asyncio.Event()

    async def pipeline(**kwargs):
        on_stage = kwargs["on_stage_complete"]
        await on_stage("idea_refiner", state_with("并发编辑故事", "概念"))
        between_stages.set()
        await release.wait()
        await on_stage("structurer", state_with("并发编辑故事", "不该应用的生成大纲"))
        return state_with("并发编辑故事", "不该应用的生成大纲")

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    run_id = submitted.json()["run"]["run_id"]
    await between_stages.wait()

    # The user hand-edits while the model works on the next stage.
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"],
        state_with("并发编辑故事", "用户手工改的大纲"),
        record.revision,
        source="manual",
        summary="并发编辑",
    )
    release.set()

    terminal = await await_terminal(api, run_id)
    assert terminal["status"] == "failed"
    assert "被修改" in terminal["error"]
    # The unapplied stage result is kept as evidence, and no later stage ran.
    assert terminal["unapplied"] is not None
    assert terminal["completed_steps"] == ["idea_refiner"]

    project = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert project["outline"]["basic_info"]["logline"] == "用户手工改的大纲"


async def test_store_failure_mid_run_aborts_without_continuing(client, monkeypatch):
    api, c = client
    p = await create_project(c, "存储失败故事")
    stages_seen = []
    original = api._runtime.store.commit_generation_stage
    calls = {"n": 0}

    def flaky_stage_commit(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ProjectStoreError("disk I/O error")
        return original(*args, **kwargs)

    async def pipeline(**kwargs):
        on_stage = kwargs["on_stage_complete"]
        await on_stage("idea_refiner", state_with("存储失败故事", "概念"))
        stages_seen.append("idea_refiner")
        await on_stage("structurer", state_with("存储失败故事", "存不下来的大纲"))
        stages_seen.append("structurer")
        return state_with("存储失败故事", "存不下来的大纲")

    scripted_engine(api, p["project_id"], pipeline)
    monkeypatch.setattr(api._runtime.store, "commit_generation_stage", flaky_stage_commit)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    terminal = await await_terminal(api, submitted.json()["run"]["run_id"])
    assert terminal["status"] == "failed"
    assert "保存" in terminal["error"]
    assert stages_seen == ["idea_refiner"]  # aborted before any new stage
    # The first stage stayed committed (atomic per-stage commits).
    project = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert project["refined_idea"] == "概念"


# ── Refine participates in the one-active-run rule ────


async def test_refine_blocked_while_generation_active_and_vice_versa(client):
    api, c = client
    p = await create_project(c, "互斥修改故事")
    gen_release = asyncio.Event()

    async def pipeline(**kwargs):
        await gen_release.wait()
        return state_with("互斥修改故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline)
    await c.post(f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"})

    refine_blocked = await c.post(
        f"/api/projects/{p['project_id']}/refine", json={"message": "改一下"}
    )
    assert refine_blocked.status_code == 409
    assert refine_blocked.json()["detail"]["code"] == "run_active"
    gen_release.set()
    latest = await c.get(f"/api/projects/{p['project_id']}/runs/latest")
    await await_terminal(api, latest.json()["run_id"])

    # Reverse direction: a refine in flight refuses a generation submit.
    refine_release = asyncio.Event()

    async def fake_refine(state, message):
        await refine_release.wait()
        raise api.RefinementError("unapplied")  # nothing saved; just unblock

    async def idle_pipeline(**kwargs):
        return state_with("互斥修改故事", "大纲")

    scripted_engine(api, p["project_id"], idle_pipeline)

    async def refine_engine(pid, **kwargs):
        record = api._runtime.store.get_required(pid)
        engine = SimpleNamespace(run_full_pipeline=idle_pipeline, refine=fake_refine)
        return engine, api._GenerationContext(
            project_id=record.project_id, revision=record.revision,
            base_state_json=record.state_json, state=record.state,
            store=api._runtime.store, auto_approve=record.auto_approve,
            skill_bindings=record.skill_bindings,
        )

    api._get_engine = refine_engine
    refine_task = asyncio.create_task(
        c.post(f"/api/projects/{p['project_id']}/refine", json={"message": "慢修改"})
    )
    await asyncio.sleep(0.05)  # let the refine slot be taken
    gen_blocked = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k2"}
    )
    assert gen_blocked.status_code == 409
    assert gen_blocked.json()["detail"]["code"] == "run_active"
    refine_release.set()
    await refine_task


# ── Project status and shutdown/interrupted semantics ──


async def test_project_status_running_only_while_run_active(client):
    api, c = client
    p = await create_project(c, "状态故事")
    release = asyncio.Event()

    async def pipeline(**kwargs):
        await release.wait()
        return state_with("状态故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline)
    await c.post(f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"})
    during = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert during["status"] == "running"
    release.set()
    latest = await c.get(f"/api/projects/{p['project_id']}/runs/latest")
    await await_terminal(api, latest.json()["run_id"])
    after = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert after["status"] != "running"


async def test_shutdown_cancels_tasks_and_leaves_no_unretrieved_exceptions(
    api_factory,
):
    api = api_factory()
    release = asyncio.Event()

    async def pipeline(**kwargs):
        await release.wait()
        return state_with("关机故事", "大纲")

    async with api.app.router.lifespan_context(api.app):
        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            p = (await c.post("/api/projects", json={"user_input": "关机故事"})).json()
            scripted_engine(api, p["project_id"], pipeline)
            submitted = await c.post(
                f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
            )
            run_id = submitted.json()["run"]["run_id"]
            manager = api._runtime.runs
            assert run_id in manager._tasks

    # Leaving the lifespan cancelled the task, awaited it, and swept the DB.
    assert api._runtime is None  # lifespan teardown completed

    # The lifespan closed its store; reopen the database read-only-facts:
    # the leftover run reads interrupted, reported as fact.
    from script_weaver.core.config import get_settings
    from script_weaver.core.project_store import ProjectStore as _Store

    store = _Store(get_settings().data_dir / "main-web" / "projects.sqlite3")
    try:
        final = store.get_generation_run(run_id)
    finally:
        store.close()
    assert final.status == "interrupted"
    assert final.error and "中断" in final.error
    release.set()  # the abandoned pipeline may finish; nothing observes it


async def test_startup_marks_stale_active_runs_interrupted_without_model_calls(
    api_factory,
):
    api = api_factory()
    async with api.app.router.lifespan_context(api.app):
        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            p = (await c.post("/api/projects", json={"user_input": "遗留故事"})).json()
            store = api._runtime.store
            # A crash leftover: a run row that claims to be active.
            stale = store.create_generation_run(
                p["project_id"],
                kind="generate",
                request_key="stale",
                request={"user_input": "遗留故事"},
                base_revision=1,
                base_state_json=store.get_required(p["project_id"]).state_json,
                checkpoint_json=None,
            )

    # "Restart": a fresh process on the same data directory.
    api2 = api_factory()
    async with api2.app.router.lifespan_context(api2.app):
        transport = httpx.ASGITransport(app=api2.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c2:
            run = (
                await c2.get(f"/api/projects/{p['project_id']}/runs/{stale.run_id}")
            ).json()
            assert run["status"] == "interrupted"
            assert "不会自动继续" in run["error"]
            # No in-memory task was spawned for the stale run.
            assert not api2._runtime.runs._tasks
            # And the alias still subscribes (read-only) to the interrupted run.
            response = await api2.generate_alias(p["project_id"])
            events = [event async for event in response.body_iterator]
            assert events[-1]["event"] == "done"
            assert json.loads(events[-1]["data"])["status"] == "interrupted"


# ── Review round 1: durable idempotency, refine mutex, stop ordering ──────


def _seed_run_row(api, project_id: str, key: str, *, status: str | None = None):
    """Admit a run row directly (no engine, no task) like a previous run."""
    import hashlib
    import json as _json

    record = api._runtime.store.get_required(project_id)
    request = {
        "user_input": record.state.user_input,
        "auto_approve": bool(record.auto_approve),
        "skill_bindings": record.skill_bindings or {},
    }
    request_hash = hashlib.sha256(
        _json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    run, _ = api._runtime.store.admit_generation_run(
        project_id, kind="generate", request_key=key, request=request,
        request_hash=request_hash,
        base_revision=record.revision, base_state_json=record.state_json,
        checkpoint_json=record.state_json,
    )
    if status is not None:
        run = api._runtime.store.settle_generation_run(run.run_id, status=status)
    return run


async def test_resend_after_any_terminal_status_returns_original_without_engine(client):
    """A key keeps pointing at its original run across every terminal
    status — a resend never builds a model instance, never adds a row, and
    cannot be overshadowed by later runs on other keys."""
    api, c = client
    calls = []

    async def idle_pipeline(**kwargs):  # pragma: no cover - must not run
        calls.append("engine")
        return state_with("x", "y")

    for status in ("failed", "cancelled", "interrupted"):
        p = await create_project(c, f"终态重发故事{status}")
        scripted_engine(api, p["project_id"], idle_pipeline, engine_calls=calls)
        seeded = _seed_run_row(api, p["project_id"], f"k-{status}", status=status)

        resent = await c.post(
            f"/api/projects/{p['project_id']}/generate",
            json={"request_key": f"k-{status}"},
        )
        assert resent.status_code == 200, resent.text
        body = resent.json()
        assert body["created"] is False
        assert body["run"]["run_id"] == seeded.run_id
        assert body["run"]["status"] == status
        latest = api._runtime.store.latest_generation_run(p["project_id"])
        assert latest.run_id == seeded.run_id  # no second row appeared

    # Overshadowed key: an older failed run is still what its key answers
    # with, even after a newer successful run on another key.
    p = await create_project(c, "遮蔽重发故事")
    scripted_engine(api, p["project_id"], idle_pipeline, engine_calls=calls)
    old = _seed_run_row(api, p["project_id"], "k-old", status="failed")
    _seed_run_row(api, p["project_id"], "k-new", status="succeeded")
    found = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k-old"}
    )
    assert found.json()["run"]["run_id"] == old.run_id
    assert calls == []  # the engine was never even built


async def test_concurrent_duplicate_submits_admit_exactly_one_run(client):
    api, c = client
    p = await create_project(c, "并发重发故事")
    calls = []
    release = asyncio.Event()

    async def pipeline(**kwargs):
        await release.wait()
        return state_with("并发重发故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline, engine_calls=calls)

    first, second = await asyncio.gather(
        c.post(f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}),
        c.post(f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}),
    )
    assert first.status_code == 200 and second.status_code == 200
    created = [first.json()["created"], second.json()["created"]]
    assert sorted(created) == [False, True]  # exactly one admission
    assert first.json()["run"]["run_id"] == second.json()["run"]["run_id"]
    assert len(calls) == 1  # one engine, one task, one eventual model call
    release.set()
    run_id = first.json()["run"]["run_id"]
    assert (await await_terminal(api, run_id))["status"] == "succeeded"


async def test_two_concurrent_refines_admit_one_and_survive_failure(client):
    api, c = client
    p = await create_project(c, "互斥并发修改故事")
    model_entered = asyncio.Event()
    release = asyncio.Event()
    refine_calls = {"n": 0}

    async def fake_refine(state, message):
        refine_calls["n"] += 1
        if refine_calls["n"] == 1:
            model_entered.set()
            await release.wait()
            raise api.RefinementError("unapplied")  # nothing saved
        model_entered.set()
        await release.wait()
        raise api.RefinementError("unapplied")

    async def idle_pipeline(**kwargs):  # pragma: no cover
        return state_with("互斥并发修改故事", "大纲")

    async def refine_engine(pid, **kwargs):
        record = api._runtime.store.get_required(pid)
        engine = SimpleNamespace(run_full_pipeline=idle_pipeline, refine=fake_refine)
        return engine, api._GenerationContext(
            project_id=record.project_id, revision=record.revision,
            base_state_json=record.state_json, state=record.state,
            store=api._runtime.store, auto_approve=record.auto_approve,
            skill_bindings=record.skill_bindings,
        )

    api._get_engine = refine_engine
    first_refine = asyncio.create_task(
        c.post(f"/api/projects/{p['project_id']}/refine", json={"message": "慢修改"})
    )
    await asyncio.wait_for(model_entered.wait(), timeout=5)

    # The second refine is rejected while the first holds the slot.
    second = await c.post(
        f"/api/projects/{p['project_id']}/refine", json={"message": "并发修改"}
    )
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "run_active"

    release.set()
    first = await first_refine
    assert first.status_code == 422  # failure released the slot

    # The slot survives an exception: a new refine reaches the model again.
    release = asyncio.Event()
    third = asyncio.create_task(
        c.post(f"/api/projects/{p['project_id']}/refine", json={"message": "再次修改"})
    )
    await asyncio.wait_for(model_entered.wait(), timeout=5)
    release.set()
    await third
    assert refine_calls["n"] == 2

    # Another project is fully independent.
    other = await create_project(c, "独立项目")
    blocked = await c.post(f"/api/projects/{other['project_id']}/refine",
                           json={"message": "修改"})
    assert blocked.status_code in (200, 422)  # admission succeeded either way


async def test_generation_rejected_while_refine_is_saving(client, monkeypatch):
    api, c = client
    p = await create_project(c, "保存期互斥故事")
    model_done = asyncio.Event()
    save_entered = threading.Event()
    save_release = threading.Event()

    async def fake_refine(state, message):
        model_done.set()
        updated = state.model_copy(deep=True)
        updated.refined_idea = "refined"
        return updated

    async def idle_pipeline(**kwargs):  # pragma: no cover
        return state_with("保存期互斥故事", "大纲")

    async def refine_engine(pid, **kwargs):
        record = api._runtime.store.get_required(pid)
        engine = SimpleNamespace(run_full_pipeline=idle_pipeline, refine=fake_refine)
        ctx = api._GenerationContext(
            project_id=record.project_id, revision=record.revision,
            base_state_json=record.state_json, state=record.state,
            store=api._runtime.store, auto_approve=record.auto_approve,
            skill_bindings=record.skill_bindings,
        )
        return engine, ctx

    original_save = api._runtime.store.save_state

    def blocked_save(*args, **kwargs):
        save_entered.set()
        save_release.wait(5)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(api._runtime.store, "save_state", blocked_save)
    api._get_engine = refine_engine

    calls = []
    scripted = None

    async def generation_pipeline(**kwargs):  # pragma: no cover
        calls.append("engine")
        return state_with("保存期互斥故事", "大纲")

    refine_task = asyncio.create_task(
        c.post(f"/api/projects/{p['project_id']}/refine", json={"message": "修改"})
    )
    await asyncio.wait_for(model_done.wait(), timeout=5)
    await asyncio.to_thread(save_entered.wait, 5)

    # While the refine result is being CAS-saved, generation is refused.
    async def generation_engine(pid, **kwargs):
        record = api._runtime.store.get_required(pid)
        calls.append("engine")
        engine = SimpleNamespace(run_full_pipeline=generation_pipeline)
        ctx = api._GenerationContext(
            project_id=record.project_id, revision=record.revision,
            base_state_json=record.state_json, state=record.state,
            store=api._runtime.store, auto_approve=record.auto_approve,
            skill_bindings=record.skill_bindings,
        )
        return engine, ctx

    api._get_engine = generation_engine
    scripted = generation_engine
    blocked = await c.post(f"/api/projects/{p['project_id']}/generate", json={})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "run_active"

    save_release.set()
    refined = await refine_task
    assert refined.status_code == 200, refined.text
    assert calls == []  # the refused generation never built an engine
    assert scripted is generation_engine


async def test_stop_during_first_db_read_and_before_first_step_converge(
    client, monkeypatch
):
    api, c = client
    p = await create_project(c, "读期停止故事")
    entered = threading.Event()
    release = threading.Event()
    original_get = api._runtime.store.get_generation_run_required

    def blocked_get(run_id):
        entered.set()
        release.wait(5)
        return original_get(run_id)

    async def pipeline(**kwargs):  # pragma: no cover - never reached
        return state_with("读期停止故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline)
    monkeypatch.setattr(
        api._runtime.store, "get_generation_run_required", blocked_get
    )

    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    run_id = submitted.json()["run"]["run_id"]
    await asyncio.to_thread(entered.wait, 5)
    assert run_id in api._runtime.runs._tasks

    stop_task = asyncio.create_task(
        c.post(f"/api/projects/{p['project_id']}/runs/{run_id}/stop")
    )
    # The stop converges the task (cancelled at its first read) even while
    # the read thread is still held; request_stop's own follow-up read then
    # waits on the same blocker, so release before awaiting the response.
    for _ in range(100):
        row = api._runtime.store.get_generation_run(run_id)
        if row.status in ("cancelled", "interrupted"):
            break
        await asyncio.sleep(0.02)
    release.set()
    stop = await stop_task
    assert stop.status_code == 200
    assert stop.json()["stopped"] is True
    row = api._runtime.store.get_generation_run(run_id)
    assert row.status == "cancelled"  # terminal, not stranded as stopping
    assert run_id not in api._runtime.runs._tasks  # registry cleaned


async def test_stop_before_the_coroutine_body_ever_runs(client, monkeypatch):
    api, c = client
    p = await create_project(c, "未启停止故事")
    entered = asyncio.Event()

    original_execute = api._RunManager._execute

    async def frozen_execute(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()  # never proceeds; only cancel ends it

    monkeypatch.setattr(api._RunManager, "_execute", frozen_execute)

    async def pipeline(**kwargs):  # pragma: no cover
        return state_with("未启停止故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    run_id = submitted.json()["run"]["run_id"]
    await entered.wait()

    stop = await c.post(f"/api/projects/{p['project_id']}/runs/{run_id}/stop")
    assert stop.status_code == 200 and stop.json()["stopped"] is True
    assert stop.json()["run"]["status"] == "cancelled"
    assert api._runtime.store.get_generation_run(run_id).status == "cancelled"
    assert run_id not in api._runtime.runs._tasks

    # A fresh submission on a new key is admitted afterwards.
    monkeypatch.setattr(api._RunManager, "_execute", original_execute)
    release = asyncio.Event()

    async def pipeline2(**kwargs):
        await release.wait()
        return state_with("未启停止故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline2)
    fresh = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k2"}
    )
    assert fresh.status_code == 200 and fresh.json()["created"] is True
    release.set()
    assert (
        await await_terminal(api, fresh.json()["run"]["run_id"])
    )["status"] == "succeeded"


async def test_success_versus_stop_race_order_decides_outcome(client, monkeypatch):
    api, c = client

    # (a) Success settles first: a late stop is a no-op on a terminal row.
    p1 = await create_project(c, "先成功故事")

    async def pipeline1(**kwargs):
        on_stage = kwargs["on_stage_complete"]
        await on_stage("idea_refiner", state_with("先成功故事", "概念"))
        return state_with("先成功故事", "概念")

    scripted_engine(api, p1["project_id"], pipeline1)
    done1 = await c.post(f"/api/projects/{p1['project_id']}/generate", json={})
    run1 = done1.json()["run"]["run_id"]
    assert (await await_terminal(api, run1))["status"] == "succeeded"
    late_stop = await c.post(
        f"/api/projects/{p1['project_id']}/runs/{run1}/stop"
    )
    assert late_stop.json()["stopped"] is False
    assert late_stop.json()["run"]["status"] == "succeeded"  # success kept

    # (b) Stop accepted first: the success settle cannot land, the run ends
    # cancelled even though the pipeline finished. The settle is held just
    # before its transaction so the stop's transition commits first.
    p2 = await create_project(c, "先停止故事")
    settle_entered = threading.Event()
    settle_release = threading.Event()
    original_settle = api._runtime.store.settle_generation_run

    def held_settle(*args, **kwargs):
        if kwargs.get("status") == "succeeded":
            settle_entered.set()
            settle_release.wait(5)
        return original_settle(*args, **kwargs)

    monkeypatch.setattr(api._runtime.store, "settle_generation_run", held_settle)

    async def pipeline2(**kwargs):
        return state_with("先停止故事", "大纲")

    scripted_engine(api, p2["project_id"], pipeline2)
    submitted = await c.post(f"/api/projects/{p2['project_id']}/generate", json={})
    run2 = submitted.json()["run"]["run_id"]
    await asyncio.to_thread(settle_entered.wait, 5)

    stop = await c.post(f"/api/projects/{p2['project_id']}/runs/{run2}/stop")
    assert stop.status_code == 200 and stop.json()["stopped"] is True
    assert stop.json()["run"]["status"] == "cancelled"
    settle_release.set()
    final = api._runtime.store.get_generation_run(run2)
    assert final.status == "cancelled"  # the held success never overwrote it


async def test_cancelled_stage_commit_await_is_not_treated_as_done(
    client, monkeypatch
):
    """A stop that cancels the task during an in-flight stage commit must
    not assume the database work ended: the orphan commit either recorded a
    full stage before the stop's transition (kept), or — arriving after the
    run settled — is rejected and rolled back with no phantom writes."""
    api, c = client
    p = await create_project(c, "取消提交交错故事")
    commit_entered = threading.Event()
    commit_release = threading.Event()
    original_commit = api._runtime.store.commit_generation_stage
    first = {"done": False}

    def held_commit(*args, **kwargs):
        if not first["done"]:
            first["done"] = True
            commit_entered.set()
            commit_release.wait(5)
        return original_commit(*args, **kwargs)

    async def pipeline(**kwargs):
        on_stage = kwargs["on_stage_complete"]
        await on_stage("idea_refiner", state_with("取消提交交错故事", "概念"))
        return state_with("取消提交交错故事", "概念")

    scripted_engine(api, p["project_id"], pipeline)
    monkeypatch.setattr(
        api._runtime.store, "commit_generation_stage", held_commit
    )

    submitted = await c.post(f"/api/projects/{p['project_id']}/generate", json={})
    run_id = submitted.json()["run"]["run_id"]
    await asyncio.to_thread(commit_entered.wait, 5)
    revision_before = api._runtime.store.get_required(p["project_id"]).revision

    stop = await c.post(f"/api/projects/{p['project_id']}/runs/{run_id}/stop")
    assert stop.status_code == 200 and stop.json()["stopped"] is True
    assert stop.json()["run"]["status"] == "cancelled"

    # Now release the orphan commit: the run is terminal, so it must be
    # rejected wholesale — no phantom version, no checkpoint update.
    commit_release.set()
    await asyncio.sleep(0.05)
    store = api._runtime.store
    assert store.get_required(p["project_id"]).revision == revision_before
    row = store.get_generation_run(run_id)
    assert row.completed_steps == []  # the cancelled stage never recorded
    versions = (await c.get(f"/api/projects/{p['project_id']}/versions")).json()
    assert all(v["source"] != "pipeline" for v in versions["versions"])


async def test_progress_relay_persists_recoverable_last_progress(client):
    """Each pipeline notification becomes the run's durable last_progress
    (stage granularity) and a live broadcast — reconnects resume from it."""
    api, c = client
    p = await create_project(c, "进度持久故事")
    release = asyncio.Event()

    async def pipeline(**kwargs):
        await release.wait()
        return state_with("进度持久故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(f"/api/projects/{p['project_id']}/generate", json={})
    run_id = submitted.json()["run"]["run_id"]

    relay = api._ProgressRelay(api._runtime.runs)
    relay.run_id = run_id
    queue = api._runtime.runs.register(run_id)
    try:
        relay("idea_refiner", "Starting idea_refiner...")
        row = api._runtime.store.get_generation_run(run_id)
        assert row.last_progress == {
            "stage": "idea_refiner", "message": "Starting idea_refiner..."
        }
        live = queue.get_nowait()
        assert live["event"] == "progress"
        assert "Starting idea_refiner" in live["data"]
    finally:
        api._runtime.runs.unregister(run_id, queue)
    release.set()
    await await_terminal(api, run_id)


# ── Review R3: the engine-initialization window (ticket #14) ──


def _paused_engine(api, pipeline, engine_entered: asyncio.Event, release: asyncio.Event):
    """Patch ``_get_engine`` with an engine whose initialization pauses on
    ``release`` — the reviewer's window between admission and execution."""

    async def paused_get_engine(pid, **kwargs):
        record = api._runtime.store.get_required(pid)
        engine = SimpleNamespace(run_full_pipeline=pipeline)
        ctx = api._GenerationContext(
            project_id=record.project_id,
            revision=record.revision,
            base_state_json=record.state_json,
            state=record.state,
            store=api._runtime.store,
            auto_approve=record.auto_approve,
            skill_bindings=record.skill_bindings,
        )
        engine_entered.set()
        await release.wait()
        return engine, ctx

    api._get_engine = paused_get_engine


async def test_stop_during_engine_initialization_never_runs_pipeline(client):
    """Reviewer repro (absorbed): a stop that completes while the engine is
    still being initialized must end the run for good — releasing the
    initialization afterwards can never start the pipeline, the registry
    stays clean, and a new intent on a fresh key still executes."""
    api, c = client
    p = await create_project(c, "初始化期停止故事")
    engine_entered = asyncio.Event()
    release = asyncio.Event()
    called = asyncio.Event()

    async def pipeline(**kwargs):  # pragma: no cover - must never run
        called.set()
        return state_with("初始化期停止故事", "大纲")

    _paused_engine(api, pipeline, engine_entered, release)
    submit_task = asyncio.create_task(
        c.post(f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"})
    )
    await engine_entered.wait()
    run = api._runtime.store.latest_generation_run(p["project_id"])
    assert run is not None and run.run_id

    stop = await c.post(f"/api/projects/{p['project_id']}/runs/{run.run_id}/stop")
    assert stop.status_code == 200
    assert stop.json()["stopped"] is True
    assert stop.json()["run"]["status"] == "cancelled"

    # Release the paused initialization: nothing may start afterwards.
    release.set()
    submitted = await submit_task
    assert submitted.status_code == 200
    for _ in range(50):
        if run.run_id not in api._runtime.runs._tasks:
            break
        await asyncio.sleep(0.02)
    assert run.run_id not in api._runtime.runs._tasks
    assert run.run_id not in api._runtime.runs._stop_events
    assert not called.is_set()  # the pipeline/model was never entered
    assert api._runtime.store.get_generation_run(run.run_id).status == "cancelled"

    # A new intent on a fresh key executes normally afterwards.
    release2 = asyncio.Event()

    async def pipeline2(**kwargs):
        await release2.wait()
        return state_with("初始化期停止故事", "大纲")

    scripted_engine(api, p["project_id"], pipeline2)
    fresh = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k2"}
    )
    assert fresh.status_code == 200 and fresh.json()["created"] is True
    release2.set()
    assert (await await_terminal(api, fresh.json()["run"]["run_id"]))["status"] == (
        "succeeded"
    )


async def test_engine_initialization_failure_settles_failed_without_pipeline(client):
    """Initialization failures (e.g. model not configured) are settled by
    the run's own task: admission stays durable, the row lands failed with
    the reason, the pipeline is never entered, and a resend stays
    idempotent."""
    api, c = client
    p = await create_project(c, "初始化失败故事")

    async def failing_engine(pid, **kwargs):
        raise api.HTTPException(
            400,
            detail={"code": "model_not_configured", "message": "模型不可用: No API key"},
        )

    api._get_engine = failing_engine
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    assert submitted.status_code == 200  # admission is durable; the run reports the failure
    assert submitted.json()["created"] is True
    run_id = submitted.json()["run"]["run_id"]

    outcome = await await_terminal(api, run_id)
    assert outcome["status"] == "failed"
    assert "生成无法启动" in outcome["error"]
    assert "模型不可用" in outcome["error"]
    assert not api._runtime.runs._tasks
    assert api._runtime.store.active_generation_run(p["project_id"]) is None

    # Resending the same request returns the failed run — no resurrection.
    resend = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    assert resend.status_code == 200 and resend.json()["created"] is False
    assert resend.json()["run"]["status"] == "failed"


async def test_shutdown_during_engine_initialization_records_interrupted(api_factory):
    """Shutting the service down while initialization is paused records the
    run as interrupted — and releasing the pause afterwards still never
    starts the pipeline."""
    api = api_factory()
    engine_entered = asyncio.Event()
    release = asyncio.Event()
    called = asyncio.Event()

    async def pipeline(**kwargs):  # pragma: no cover - must never run
        called.set()
        return state_with("初始化期关机故事", "大纲")

    _paused_engine(api, pipeline, engine_entered, release)
    async with api.app.router.lifespan_context(api.app):
        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            p = (await c.post("/api/projects", json={"user_input": "初始化期关机故事"})).json()
            submit_task = asyncio.create_task(
                c.post(
                    f"/api/projects/{p['project_id']}/generate",
                    json={"request_key": "k"},
                )
            )
            await engine_entered.wait()
            run = api._runtime.store.latest_generation_run(p["project_id"])
            assert run is not None and run.run_id

            await api._runtime.runs.shutdown()  # service shutdown mid-init
            release.set()
            submitted = await submit_task
            assert submitted.status_code == 200

            assert not api._runtime.runs._tasks
            assert not called.is_set()
            assert api._runtime.store.get_generation_run(run.run_id).status == (
                "interrupted"
            )


# ── Ticket #15: resuming terminal runs from their checkpoint ─────────


def _staged_state(user_input: str, upto: str) -> ProjectState:
    """A snapshot whose artifacts cover every step up to ``upto`` (incl.)."""
    from script_weaver.core.types import ArtStyle, BasicInfo, ProjectStatus, Storyboard
    from script_weaver.core.types import Outline as OutlineModel

    state = state_with(user_input, "已保存大纲")
    from script_weaver.core.pipeline import GENERATION_STEPS

    depth = list(GENERATION_STEPS).index(upto) + 1 if upto in GENERATION_STEPS else 1
    if depth >= 2:
        state.outline = OutlineModel(
            basic_info=BasicInfo(logline="已保存大纲"),
            plot_outline=[{"sequence_number": "1", "title": "起", "synopsis": "承"}],
        )
    if depth >= 3:
        from script_weaver.core.types import Character
        state.characters = [Character.model_validate({"name": "主角"})]
    if depth >= 4:
        from script_weaver.core.types import SceneDesign
        state.scenes = [SceneDesign.model_validate({"name": "车站", "location_type": "外景"})]
    if depth >= 5:
        state.art_style = ArtStyle.model_validate({"overall_style": "黑色电影"})
    if depth >= 6:
        from script_weaver.core.types import Script as ScriptModel
        state.script = ScriptModel.model_validate({"scenes": [{"blocks": [
            {"block_type": "dialogue", "content": {"character_name": "主角", "dialogue": "007"}},
        ]}]})
    if depth >= 7:
        state.storyboard = Storyboard(shots=[{
            "shot_id": "shot_1", "scene_id": "sc_1",
            "visual_description": "门口", "duration_seconds": 5,
        }])
        state.storyboard.compute_totals()
    if depth >= 8:
        state.meta.status = ProjectStatus.COMPLETE
    return state


async def _fail_run_at(c, api, project_id: str, steps: list[str], error="boom"):
    """Submit a run whose scripted pipeline commits ``steps`` then fails."""
    user_input = api._runtime.store.get_required(project_id).state.user_input

    async def pipeline(**kwargs):
        on_stage = kwargs.get("on_stage_complete")
        state = _staged_state(user_input, steps[-1] if steps else "idea_refiner")
        for step in steps:
            if on_stage is not None:
                await on_stage(step, state)
        raise RuntimeError(error)

    scripted_engine(api, project_id, pipeline)
    submitted = await c.post(
        f"/api/projects/{project_id}/generate", json={"request_key": "fail-seed"}
    )
    assert submitted.status_code == 200, submitted.text
    run_id = submitted.json()["run"]["run_id"]
    terminal = await await_terminal(api, run_id)
    assert terminal["status"] == "failed", terminal
    return run_id


async def _resumable_run(
    api, project_id: str, steps: list[str], *, upto: str | None = None,
    state: ProjectState | None = None,
) -> str:
    """Seed a failed run with a valid envelope checkpoint (no engine).

    Mirrors reality: when a run fails after committing stages, the project
    content equals the checkpoint snapshot — so the seeded project state is
    advanced to the snapshot first, exactly like stage commits would have.
    """
    from script_weaver.core.resume import (
        build_checkpoint,
        current_fingerprint,
        encode_checkpoint,
    )

    store = api._runtime.store
    record = store.get_required(project_id)
    if state is None:
        state = (
            _staged_state(record.state.user_input, upto or steps[-1])
            if (steps or upto) else None
        )
    if steps:
        store.save_state(project_id, state, record.revision,
                         source="manual", summary="seed committed stages")
        record = store.get_required(project_id)
    fingerprint = await current_fingerprint(
        skill_bindings={
            k: v.model_dump(mode="json")
            for k, v in (record.state.skill_bindings or {}).items()
        },
        auto_approve=record.auto_approve,
    )
    checkpoint = build_checkpoint(
        user_input=record.state.user_input,
        completed_steps=steps,
        state=state if steps else None,
        fingerprint=fingerprint,
        basis={"revision": record.revision},
    )
    run = store.create_generation_run(
        project_id, kind="generate",
        request_key=f"seed-{record.project_id}-{len(steps)}",
        request={"user_input": record.state.user_input, "auto_approve": True},
        base_revision=record.revision, base_state_json=record.state_json,
        checkpoint_json=encode_checkpoint(checkpoint),
        completed_steps=steps,
    )
    store.settle_generation_run(run.run_id, status="failed")
    return run.run_id


async def test_resume_creates_new_run_reusing_the_success_prefix(client):
    api, c = client
    p = await create_project(c, "恢复故事")
    failed_run = await _fail_run_at(
        c, api, p["project_id"], ["idea_refiner", "structurer"]
    )
    original = api._runtime.store.get_generation_run(failed_run)

    seen_kwargs = {}

    async def pipeline(**kwargs):
        seen_kwargs.update(kwargs)
        on_stage = kwargs.get("on_stage_complete")
        state = state_with("恢复故事", "完整大纲")
        for step in ("character_designer", "scene_designer", "art_director",
                     "scriptwriter", "storyboard_artist", "finalize"):
            if on_stage is not None:
                await on_stage(step, state)
        return state

    scripted_engine(api, p["project_id"], pipeline)

    resumed = await c.post(
        f"/api/projects/{p['project_id']}/runs/{failed_run}/resume",
        json={"request_key": "rk-1"},
    )
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["created"] is True
    new_run_id = body["run"]["run_id"]
    assert new_run_id != failed_run
    # The new run inherits the prefix from the moment it exists, and the
    # eligibility facts ride the serialized run.
    assert body["run"]["completed_steps"] == ["idea_refiner", "structurer"]
    assert body["run"]["next_step"] == "character_designer"
    assert body["run"]["content_complete"] is False

    terminal = await await_terminal(api, new_run_id)
    assert terminal["status"] == "succeeded"
    # Execution received the checkpoint basis: prefix + resume envelope.
    assert seen_kwargs["completed_steps"] == ["idea_refiner", "structurer"]
    assert seen_kwargs["resume_from"] is not None
    assert seen_kwargs["initial_state"].outline is not None

    # The original failed row is preserved untouched.
    kept = api._runtime.store.get_generation_run(failed_run)
    assert kept.status == "failed"
    assert kept.checkpoint_json == original.checkpoint_json


async def test_resume_running_run_refused_and_content_complete_refused(client):
    api, c = client
    p = await create_project(c, "状态拒绝故事")
    release = asyncio.Event()

    async def pipeline(**kwargs):
        await release.wait()
        return state_with("状态拒绝故事", "x")

    scripted_engine(api, p["project_id"], pipeline)
    first = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k1"}
    )
    active_id = first.json()["run"]["run_id"]
    refused = await c.post(
        f"/api/projects/{p['project_id']}/runs/{active_id}/resume",
        json={"request_key": "rk"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "run_active"
    release.set()
    await await_terminal(api, active_id)

    # A content-complete run (finalize committed) answers content_complete:
    # read/export instead of regenerating.
    done = await _resumable_run(api, p["project_id"], [
        "idea_refiner", "structurer", "character_designer", "scene_designer",
        "art_director", "scriptwriter", "storyboard_artist", "finalize",
    ])
    complete = await c.post(
        f"/api/projects/{p['project_id']}/runs/{done}/resume",
        json={"request_key": "rk2"},
    )
    assert complete.status_code == 409
    detail = complete.json()["detail"]
    assert detail["code"] == "content_complete"
    assert "导出" in detail["message"]


async def test_resume_old_raw_checkpoint_refused_as_missing_basis(client):
    api, c = client
    p = await create_project(c, "旧格式故事")
    store = api._runtime.store
    record = store.get_required(p["project_id"])
    run = store.create_generation_run(
        p["project_id"], kind="generate", request_key="legacy",
        request={"user_input": record.state.user_input, "auto_approve": True},
        base_revision=record.revision, base_state_json=record.state_json,
        checkpoint_json=record.state_json,  # pre-#15 raw state blob
        completed_steps=["idea_refiner"],
    )
    store.settle_generation_run(run.run_id, status="failed")
    refused = await c.post(
        f"/api/projects/{p['project_id']}/runs/{run.run_id}/resume",
        json={"request_key": "rk"},
    )
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "resume_basis_missing"
    assert "从头生成" in detail["message"]
    # The legacy row is preserved and viewable.
    kept = store.get_generation_run(run.run_id)
    assert kept.status == "failed" and kept.checkpoint_json == record.state_json


async def test_resume_refused_when_prefix_project_or_config_changed(client):
    api, c = client
    p = await create_project(c, "变化故事")
    store = api._runtime.store

    # 1) Tampered (invalid) prefix on the run row.
    tampered = await _resumable_run(api, p["project_id"], ["idea_refiner"])
    with store._write_tx() as conn:
        conn.execute(
            "UPDATE generation_runs SET completed_steps_json = ? WHERE id = ?",
            ('["structurer"]', tampered),
        )
    refused = await c.post(
        f"/api/projects/{p['project_id']}/runs/{tampered}/resume",
        json={"request_key": "rk"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "invalid_prefix"

    # 2) Project content edited after the failure.
    edited = await _resumable_run(api, p["project_id"], ["idea_refiner"])
    record = store.get_required(p["project_id"])
    user_state = record.state.model_copy(deep=True)
    user_state.refined_idea = "用户手改的概念"
    store.save_state(p["project_id"], user_state, record.revision,
                     source="manual", summary="用户编辑")
    refused = await c.post(
        f"/api/projects/{p['project_id']}/runs/{edited}/resume",
        json={"request_key": "rk"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "project_changed"

    # 3) Execution basis drifted (model settings changed since the run).
    drifted = await _resumable_run(api, p["project_id"], ["idea_refiner"])
    from script_weaver.core import config as _config
    old = _config._settings.llm_model
    _config._settings.llm_model = "another-model"
    try:
        refused = await c.post(
            f"/api/projects/{p['project_id']}/runs/{drifted}/resume",
            json={"request_key": "rk"},
        )
    finally:
        _config._settings.llm_model = old
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["code"] == "config_changed"
    assert "未调用模型" in detail["message"]


async def test_resume_rename_only_project_still_resumable(client):
    """A rename is not a content change — the resume still works."""
    api, c = client
    p = await create_project(c, "改名恢复故事")
    failed = await _resumable_run(api, p["project_id"], ["idea_refiner"])

    async def pipeline(**kwargs):
        return state_with("改名恢复故事", "完整大纲")

    scripted_engine(api, p["project_id"], pipeline)
    record = api._runtime.store.get_required(p["project_id"])
    await c.patch(
        f"/api/projects/{p['project_id']}",
        json={"title": "改名之后", "expected_revision": record.revision},
    )
    resumed = await c.post(
        f"/api/projects/{p['project_id']}/runs/{failed}/resume",
        json={"request_key": "rk"},
    )
    assert resumed.status_code == 200, resumed.text
    terminal = await await_terminal(api, resumed.json()["run"]["run_id"])
    assert terminal["status"] == "succeeded"


async def test_resume_same_key_resends_same_run_and_conflicting_target_409(client):
    api, c = client
    p = await create_project(c, "幂等恢复故事")
    # One content snapshot for both seeds: one project, two run rows that
    # both remain content-compatible with the project.
    seed_record = api._runtime.store.get_required(p["project_id"])
    shared = _staged_state(seed_record.state.user_input, "structurer")
    failed2 = await _resumable_run(
        api, p["project_id"], ["idea_refiner", "structurer"], state=shared
    )
    failed1 = await _resumable_run(
        api, p["project_id"], ["idea_refiner"], state=shared
    )

    async def pipeline(**kwargs):
        await asyncio.sleep(0)
        return state_with("幂等恢复故事", "完整大纲")

    scripted_engine(api, p["project_id"], pipeline)
    first = await c.post(
        f"/api/projects/{p['project_id']}/runs/{failed1}/resume",
        json={"request_key": "same"},
    )
    assert first.status_code == 200
    new_id = first.json()["run"]["run_id"]
    await await_terminal(api, new_id)

    # Resending the SAME key for the SAME target returns the same run.
    again = await c.post(
        f"/api/projects/{p['project_id']}/runs/{failed1}/resume",
        json={"request_key": "same"},
    )
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["run"]["run_id"] == new_id

    # The same key against a DIFFERENT resume target is a conflict.
    conflict = await c.post(
        f"/api/projects/{p['project_id']}/runs/{failed2}/resume",
        json={"request_key": "same"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "request_conflict"


async def test_concurrent_resumes_admit_exactly_one_new_run(client):
    api, c = client
    p = await create_project(c, "并发恢复故事")
    failed = await _resumable_run(api, p["project_id"], ["idea_refiner"])
    release = asyncio.Event()

    async def pipeline(**kwargs):
        await release.wait()
        return state_with("并发恢复故事", "完整大纲")

    scripted_engine(api, p["project_id"], pipeline)
    r1, r2 = await asyncio.gather(
        c.post(f"/api/projects/{p['project_id']}/runs/{failed}/resume",
               json={"request_key": "a"}),
        c.post(f"/api/projects/{p['project_id']}/runs/{failed}/resume",
               json={"request_key": "b"}),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409]
    busy = r2 if r2.status_code == 409 else r1
    assert busy.json()["detail"]["code"] == "run_active"
    release.set()
    ok = r1 if r1.status_code == 200 else r2
    await await_terminal(api, ok.json()["run"]["run_id"])
    # Exactly one new run row exists for the project besides the original.
    store = api._runtime.store
    with store._lock:
        rows = store._conn.execute(
            "SELECT COUNT(*) FROM generation_runs WHERE project_id = ?",
            (p["project_id"],),
        ).fetchone()[0]
    assert rows == 2  # the failed original + the one resumed run


async def test_resume_blocked_while_refine_in_flight_and_vice_versa(client, monkeypatch):
    api, c = client
    p = await create_project(c, "恢复互斥故事")
    failed = await _resumable_run(api, p["project_id"], ["idea_refiner"])

    from script_weaver.core.refinement import RefineExecutionError

    hold = asyncio.Event()

    async def fake_get_engine(pid, **kwargs):
        record = api._runtime.store.get_required(pid)
        from types import SimpleNamespace

        async def slow_refine(state, message):
            await hold.wait()
            raise RefineExecutionError("受控失败")

        engine = SimpleNamespace(refine=slow_refine)
        ctx = api._GenerationContext(
            project_id=record.project_id, revision=record.revision,
            base_state_json=record.state_json, state=record.state,
            store=api._runtime.store, auto_approve=record.auto_approve,
            skill_bindings=record.skill_bindings,
        )
        return engine, ctx

    api._get_engine = fake_get_engine
    refine_task = asyncio.create_task(
        c.post(f"/api/projects/{p['project_id']}/refine", json={"message": "改短"})
    )
    await asyncio.sleep(0.05)  # let the refine slot acquire
    async def pipeline(**kwargs):  # pragma: no cover - must not run
        return state_with("恢复互斥故事", "x")
    scripted_engine(api, p["project_id"], pipeline)
    refused = await c.post(
        f"/api/projects/{p['project_id']}/runs/{failed}/resume",
        json={"request_key": "rk"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "run_active"
    hold.set()
    done = await refine_task
    assert done.status_code == 502  # released; controlled failure surfaces


async def test_zero_step_resume_reruns_everything(client):
    api, c = client
    p = await create_project(c, "空恢复故事")
    failed = await _resumable_run(api, p["project_id"], [])

    seen = {}

    async def pipeline(**kwargs):
        seen["completed_steps"] = kwargs.get("completed_steps")
        seen["resume_from"] = kwargs.get("resume_from")
        return state_with("空恢复故事", "完整大纲")

    scripted_engine(api, p["project_id"], pipeline)
    resumed = await c.post(
        f"/api/projects/{p['project_id']}/runs/{failed}/resume",
        json={"request_key": "rk"},
    )
    assert resumed.status_code == 200
    terminal = await await_terminal(api, resumed.json()["run"]["run_id"])
    assert terminal["status"] == "succeeded"
    assert seen["completed_steps"] == []
    assert seen["resume_from"] is not None


async def test_growth_lifecycle_persisted_onto_checkpoint(client):
    api, c = client
    p = await create_project(c, "成长故事")

    async def pipeline(**kwargs):
        on_stage = kwargs.get("on_stage_complete")
        growth = kwargs.get("on_growth_event")
        state = state_with("成长故事", "完整大纲")
        if on_stage:
            for step in ("idea_refiner", "structurer"):
                await on_stage(step, state)
        if growth:
            await growth("running", None)
            await growth("failed", "画像磁盘已满")
        return state

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    run_id = submitted.json()["run"]["run_id"]
    terminal = await await_terminal(api, run_id)
    assert terminal["status"] == "succeeded"  # content complete despite growth
    from script_weaver.core.resume import decode_checkpoint

    run = api._runtime.store.get_generation_run(run_id)
    checkpoint = decode_checkpoint(run.checkpoint_json)
    assert checkpoint.growth_status == "failed"
    assert "画像磁盘已满" in checkpoint.growth_error
    assert checkpoint.completed_steps == ["idea_refiner", "structurer"]


async def test_stop_during_growth_converges_checkpoint_to_interrupted(client):
    api, c = client
    p = await create_project(c, "成长停止故事")
    release = asyncio.Event()

    async def pipeline(**kwargs):
        on_stage = kwargs.get("on_stage_complete")
        growth = kwargs.get("on_growth_event")
        state = state_with("成长停止故事", "完整大纲")
        if on_stage:
            await on_stage("finalize", state)
        if growth:
            await growth("running", None)
            await release.wait()  # simulates mid-growth side effects
        return state

    scripted_engine(api, p["project_id"], pipeline)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    run_id = submitted.json()["run"]["run_id"]
    await asyncio.sleep(0.05)  # let the pipeline reach the growth wait
    stopped = await c.post(f"/api/projects/{p['project_id']}/runs/{run_id}/stop", json={})
    release.set()
    assert stopped.status_code == 200
    terminal = await await_terminal(api, run_id)
    assert terminal["status"] in ("cancelled", "succeeded")
    from script_weaver.core.resume import decode_checkpoint

    run = api._runtime.store.get_generation_run(run_id)
    checkpoint = decode_checkpoint(run.checkpoint_json)
    # Growth never claimed success: it either stayed running→interrupted or
    # recorded failed — never succeeded after a stop.
    assert checkpoint.growth_status in ("interrupted", "failed")


async def test_startup_sweep_converges_midflight_growth_to_interrupted(client):
    """A run killed mid-growth is marked interrupted with the growth note
    by the restart sweep — never silently resumed nor replayed."""
    api, c = client
    p = await create_project(c, "重启成长故事")
    from script_weaver.core.resume import (
        build_checkpoint,
        decode_checkpoint,
        encode_checkpoint,
    )

    store = api._runtime.store
    record = store.get_required(p["project_id"])
    checkpoint = build_checkpoint(
        user_input=record.state.user_input,
        completed_steps=list(api.GENERATION_STEPS),  # content complete
        state=record.state,
        fingerprint={},
        growth_status="running",
        basis={"revision": record.revision},
    )
    run = store.create_generation_run(
        p["project_id"], kind="generate", request_key="killed",
        request={"user_input": record.state.user_input, "auto_approve": True},
        base_revision=record.revision, base_state_json=record.state_json,
        checkpoint_json=encode_checkpoint(checkpoint),
    )
    marked = await asyncio.to_thread(store.interrupt_stale_generation_runs)
    assert marked >= 1
    after = store.get_generation_run(run.run_id)
    decoded = decode_checkpoint(after.checkpoint_json)
    assert decoded.growth_status == "interrupted"
    assert "部分发生" in decoded.growth_error
    # Content-complete + interrupted growth: resume is refused outright.
    refused = await c.post(
        f"/api/projects/{p['project_id']}/runs/{run.run_id}/resume",
        json={"request_key": "rk"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "content_complete"
