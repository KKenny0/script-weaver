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
    assert stop.json()["run"]["status"] == "stopping"

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
    original = api._runtime.store.replace_state
    calls = {"n": 0}

    def flaky_replace(*args, **kwargs):
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
    monkeypatch.setattr(api._runtime.store, "replace_state", flaky_replace)
    submitted = await c.post(
        f"/api/projects/{p['project_id']}/generate", json={"request_key": "k"}
    )
    terminal = await await_terminal(api, submitted.json()["run"]["run_id"])
    assert terminal["status"] == "failed"
    assert "保存" in terminal["error"]
    assert stages_seen == ["idea_refiner"]  # aborted before any new stage


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
