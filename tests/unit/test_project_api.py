"""API-level checks for persistent projects (ticket #13).

Uses the real FastAPI app with an isolated tmp data dir; pipeline engines are
controlled fakes so no network or model calls happen.
"""

import asyncio
import copy
import io
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from script_weaver.core.types import (
    BasicInfo,
    Character,
    Outline,
    ProjectState,
    ProjectStatus,
    SceneDesign,
    Script,
    ScriptBlock,
    ScriptBlockType,
    ScriptScene,
    ScriptSceneHeading,
    Shot,
    Storyboard,
)


@pytest.fixture
async def client(api_factory):
    """App + httpx client with the lifespan running on an isolated data dir.

    raise_app_exceptions=False mirrors a real server, which converts handler
    exceptions into 500 responses instead of propagating them.
    """
    api = api_factory()
    async with api.app.router.lifespan_context(api.app):
        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield api, c


# ── Helpers ────────────────────────────────────────────────


async def create_project(c: httpx.AsyncClient, user_input: str, title: str | None = None) -> dict:
    r = await c.post("/api/projects", json={"user_input": user_input, "title": title})
    assert r.status_code == 200, r.text
    return r.json()


def outlined_state(user_input: str, logline: str) -> ProjectState:
    state = ProjectState(user_input=user_input)
    state.outline = Outline(basic_info=BasicInfo(logline=logline))
    state.refined_idea = logline
    state.meta.status = ProjectStatus.STRUCTURED
    return state


def stub_engine(api, run_full_pipeline=None, refine=None):
    """Patch api._get_engine with a controlled engine over the real store."""
    async def fake_get_engine(project_id):
        record = api._runtime.store.get_required(project_id)
        engine = SimpleNamespace(
            run_full_pipeline=run_full_pipeline,
            refine=refine,
            _progress=None,
        )
        ctx = api._GenerationContext(
            project_id=record.project_id,
            revision=record.revision,
            base_state_json=record.state_json,
            state=record.state,
            store=api._runtime.store,
        )
        return engine, ctx

    api._get_engine = fake_get_engine


async def consume_sse(c: httpx.AsyncClient, path: str) -> list[tuple[str, str]]:
    """Collect (event, data) pairs from an SSE endpoint."""
    events = []
    async with c.stream("GET", path) as resp:
        assert resp.status_code == 200, resp.text
        current_event = None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current_event:
                events.append((current_event, line.split(":", 1)[1].strip()))
    return events


# ── CRUD, isolation, listing ───────────────────────────────


async def test_two_projects_isolated_and_listed_by_update_time(client):
    api, c = client
    pa = await create_project(c, "A 的想法", "项目A")
    pb = await create_project(c, "B 的想法", "项目B")

    # Simulate persisted content for A only.
    record = api._runtime.store.get_required(pa["project_id"])
    api._runtime.store.save_state(
        pa["project_id"], outlined_state("A 的想法", "A 的大纲"),
        record.revision, source="manual", summary="x",
    )

    ra = await c.get(f"/api/projects/{pa['project_id']}")
    rb = await c.get(f"/api/projects/{pb['project_id']}")
    assert ra.status_code == rb.status_code == 200
    a, b = ra.json(), rb.json()
    assert a["has_outline"] is True and a["outline"]["basic_info"]["logline"] == "A 的大纲"
    assert b["has_outline"] is False
    assert a["meta"]["id"] == pa["project_id"]  # service ID == meta.id
    # Preserved read fields from the original contract:
    for field in ("refined_idea", "outline", "characters", "scenes", "art_style",
                  "script", "storyboard", "visual_highlights", "memory_decisions",
                  "has_refined_idea", "characters_count", "scenes_count",
                  "script_scenes_count", "storyboard_shots_count", "meta"):
        assert field in a, field

    listed = (await c.get("/api/projects")).json()
    assert [p["project_id"] for p in listed] == [pa["project_id"], pb["project_id"]]
    assert listed[0]["updated_at"] >= listed[1]["updated_at"]


async def test_get_unknown_project_404(client):
    _, c = client
    r = await c.get("/api/projects/doesnotexist")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "project_not_found"


# ── Rename ─────────────────────────────────────────────────


async def test_rename_flow_and_conflicts(client):
    _, c = client
    p = await create_project(c, "想改名的项目", "旧名字")

    ok = await c.patch(f"/api/projects/{p['project_id']}",
                       json={"title": "新名字", "expected_revision": p["revision"]})
    assert ok.status_code == 200
    assert ok.json()["revision"] == p["revision"] + 1
    assert ok.json()["title"] == "新名字"

    # Stale revision → 409 with a stable code, no partial write.
    stale = await c.patch(f"/api/projects/{p['project_id']}",
                          json={"title": "更旧的名字", "expected_revision": p["revision"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "revision_conflict"
    assert stale.json()["detail"]["current_revision"] == p["revision"] + 1

    current = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert current["meta"]["title"] == "新名字"

    unknown = await c.patch("/api/projects/none", json={"title": "x", "expected_revision": 1})
    assert unknown.status_code == 404

    blank = await c.patch(f"/api/projects/{p['project_id']}",
                          json={"title": "   ", "expected_revision": 2})
    assert blank.status_code == 422


# ── Versions ───────────────────────────────────────────────


async def test_version_history_readonly_and_cross_project_404(client):
    api, c = client
    pa = await create_project(c, "有历史的项目", "A")
    pb = await create_project(c, "另一个项目", "B")

    record = api._runtime.store.get_required(pa["project_id"])
    api._runtime.store.save_state(
        pa["project_id"], outlined_state("有历史的项目", "第二版大纲"),
        record.revision, source="manual", summary="内容修改",
    )

    versions = (await c.get(f"/api/projects/{pa['project_id']}/versions")).json()
    assert versions["current_revision"] == 2
    assert [v["revision"] for v in versions["versions"]] == [2, 1]
    assert versions["versions"][0]["summary"] == "内容修改"

    v1 = (await c.get(f"/api/projects/{pa['project_id']}/versions/1")).json()
    assert v1["has_outline"] is False  # previous version still viewable
    v2 = (await c.get(f"/api/projects/{pa['project_id']}/versions/2")).json()
    assert v2["outline"]["basic_info"]["logline"] == "第二版大纲"

    # Cross-project: revision 2 exists for A but not for B.
    cross = await c.get(f"/api/projects/{pb['project_id']}/versions/2")
    assert cross.status_code == 404
    # And versions of an unknown project are 404, not 500.
    assert (await c.get("/api/projects/none/versions")).status_code == 404


# ── Generation persists; survives restart; meta.id pinned ──


async def test_generate_persists_result_and_survives_restart(api_factory):
    """Content generated before a restart is openable and exportable after."""
    api = api_factory()
    async with api.app.router.lifespan_context(api.app):
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/projects", json={"user_input": "一个测试故事"})
            p = r.json()

            async def fake_pipeline(**kwargs):
                assert kwargs["user_input"] == "一个测试故事"
                return outlined_state("一个测试故事", "生成的大纲")

            stub_engine(api, run_full_pipeline=fake_pipeline)

            events = await consume_sse(c, f"/api/projects/{p['project_id']}/generate")
            assert events[-1][0] == "done"
            assert '"error"' not in events[-1][1]

            after = (await c.get(f"/api/projects/{p['project_id']}")).json()
            assert after["has_outline"] is True
            assert after["revision"] == 2
            assert after["meta"]["id"] == p["project_id"]  # export meta.id == project id

            versions = (await c.get(f"/api/projects/{p['project_id']}/versions")).json()
            assert versions["versions"][0]["source"] == "pipeline"

    # Simulate a backend restart: the first lifespan exited (lock released),
    # a new module + lifespan opens on the same data directory.
    api2 = api_factory()
    async with api2.app.router.lifespan_context(api2.app):
        transport = httpx.ASGITransport(app=api2.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c2:
            reopened = (await c2.get(f"/api/projects/{p['project_id']}")).json()
            assert reopened["has_outline"] is True
            assert reopened["outline"]["basic_info"]["logline"] == "生成的大纲"
            assert reopened["revision"] == 2

            export_res = await c2.get(f"/api/projects/{p['project_id']}/export/json")
            assert export_res.status_code == 200
            assert json.loads(export_res.text)["meta"]["id"] == p["project_id"]


async def test_generate_rejects_concurrent_edit_without_overwrite(client):
    api, c = client
    p = await create_project(c, "并发编辑测试")

    started, finish = asyncio.Event(), asyncio.Event()

    async def fake_pipeline(**kwargs):
        started.set()
        await finish.wait()
        return outlined_state("并发编辑测试", "生成的大纲")

    stub_engine(api, run_full_pipeline=fake_pipeline)

    async def run_stream():
        return await consume_sse(c, f"/api/projects/{p['project_id']}/generate")

    stream_task = asyncio.create_task(run_stream())
    await asyncio.wait_for(started.wait(), timeout=5)

    # A concurrent manual edit lands while the pipeline is running.
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], outlined_state("并发编辑测试", "用户手工改的大纲"),
        record.revision, source="manual", summary="并发编辑",
    )
    finish.set()
    events = await stream_task

    assert events[-1][0] == "done"
    assert "revision_conflict" in events[-1][1]
    error_events = [data for name, data in events if name == "error"]
    assert error_events, "expected an SSE error event for the conflict"

    final = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert final["outline"]["basic_info"]["logline"] == "用户手工改的大纲"
    assert final["revision"] == 2  # manual save only


async def test_generate_missing_model_returns_clear_error(client, monkeypatch):
    api, c = client
    p = await create_project(c, "没有模型密钥的故事")

    def broken_llm():
        raise ValueError("No API key set for provider 'anthropic'.")

    monkeypatch.setattr(api, "LLMClient", broken_llm)

    r = await c.get(f"/api/projects/{p['project_id']}/generate")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "model_not_configured"
    # The project itself remains openable without any model key.
    got = await c.get(f"/api/projects/{p['project_id']}")
    assert got.status_code == 200


# ── Refine persists ────────────────────────────────────────


async def test_refine_persists_via_store(client):
    api, c = client
    p = await create_project(c, "要修改的故事", "标题")

    async def fake_refine(state, message):
        state = outlined_state("要修改的故事", f"按指令修改: {message}")
        state.meta.id = "must-be-repinned"
        return state

    stub_engine(api, refine=fake_refine)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "让大纲更悬疑"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2

    after = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert after["outline"]["basic_info"]["logline"] == "按指令修改: 让大纲更悬疑"
    # The service project ID always wins over any state-carried meta.id.
    assert after["meta"]["id"] == p["project_id"]


# ── Export serves real, tool-readable files ────────────────


# >200 chars so the CSV summary rule (truncate to 200 + "...") is exercised
# while JSON and per-shot TXT must keep the full text.
LONG_PROMPT = "夜色中的灯塔守望者" + "，海风呼啸而过" * 30


def exportable_state(logline: str) -> ProjectState:
    """A persisted-ready state with Chinese script + 2-shot storyboard."""
    state = outlined_state("可导出的故事", logline)
    state.script = Script(
        title="夜行灯塔",
        scenes=[
            ScriptScene(
                heading=ScriptSceneHeading(location="灯塔顶层", time_of_day="夜"),
                blocks=[
                    ScriptBlock(
                        block_type=ScriptBlockType.ACTION,
                        content={"description": "阿芸推开锈蚀的铁门，寒风灌入。"},
                    ),
                    ScriptBlock(
                        block_type=ScriptBlockType.DIALOGUE,
                        content={"character_name": "阿芸", "dialogue": "灯不能灭。"},
                    ),
                ],
                characters_involved=["阿芸"],
            ),
        ],
        total_estimated_duration=10,
    )
    state.storyboard = Storyboard(shots=[
        Shot(
            shot_id="shot_lighthouse_01", scene_id="sc_1",
            visual_description="灯塔外景，巨浪拍岸",
            image_prompt=LONG_PROMPT, video_prompt=LONG_PROMPT,
            dialogue="灯不能灭。", duration_seconds=2,
        ),
        Shot(
            shot_id="shot_lighthouse_02", scene_id="sc_1",
            visual_description="阿芸特写，眼神坚定",
            image_prompt="近景：阿芸握紧灯芯", video_prompt="镜头缓缓推近",
            duration_seconds=3,
        ),
    ])
    state.storyboard.compute_totals()
    return state


async def seed_exportable(client, api, logline="导出用大纲") -> dict:
    """Create a project whose persisted state has script + storyboard."""
    p = await create_project(client, "可导出的故事", "导出测试")
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], exportable_state(logline), record.revision,
        source="manual", summary="可导出内容",
    )
    return p


async def test_export_json_is_raw_project_state_file(client):
    api, c = client
    p = await seed_exportable(c, api)

    r = await c.get(f"/api/projects/{p['project_id']}/export/json")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"] == (
        f'attachment; filename="{p["project_id"]}.json"'
    )

    # The body IS the ProjectState object: top-level meta/script, no
    # {"content": ...} envelope, no second layer of string encoding.
    body = r.text
    parsed = json.loads(body)
    assert set(parsed) >= {"meta", "user_input", "script", "storyboard"}
    assert "content" not in parsed
    assert parsed["meta"]["id"] == p["project_id"]
    reparsed = ProjectState.model_validate_json(body)
    assert reparsed.script.title == "夜行灯塔"
    assert reparsed.storyboard.shots[0].dialogue == "灯不能灭。"


async def test_export_fountain_is_plain_text_file(client):
    api, c = client
    p = await seed_exportable(c, api)

    r = await c.get(f"/api/projects/{p['project_id']}/export/fountain")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["content-disposition"] == (
        f'attachment; filename="{p["project_id"]}.fountain"'
    )

    text = r.text
    assert not text.lstrip().startswith("{"), "fountain must not be JSON-wrapped"
    assert "Title: 夜行灯塔" in text
    assert "INT 灯塔顶层 - 夜" in text
    assert "阿芸推开锈蚀的铁门" in text
    assert "灯不能灭。" in text


async def test_export_video_gen_is_real_zip(client):
    api, c = client
    p = await seed_exportable(c, api)

    r = await c.get(f"/api/projects/{p['project_id']}/export/video_gen")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/zip")
    assert r.headers["content-disposition"] == (
        f'attachment; filename="{p["project_id"]}_video_gen.zip"'
    )

    payload = io.BytesIO(r.content)
    assert zipfile.is_zipfile(payload), "video_gen download must be a real ZIP"
    with zipfile.ZipFile(payload) as zf:
        assert zf.testzip() is None, "ZIP members must decompress cleanly"
        assert set(zf.namelist()) == {
            "video_gen_shots.json", "video_gen_shots.csv",
            "shots/shot_lighthouse_01.txt", "shots/shot_lighthouse_02.txt",
        }

        shots_json = json.loads(zf.read("video_gen_shots.json").decode("utf-8"))
        assert [s["shot_id"] for s in shots_json] == [
            "shot_lighthouse_01", "shot_lighthouse_02",
        ]
        assert shots_json[0]["dialogue_text"] == "灯不能灭。"

        # Full long prompts survive in JSON and per-shot TXT; CSV keeps its
        # 200-char summary rule ("..." suffix), i.e. it is NOT lossless.
        assert shots_json[0]["image_prompt"] == LONG_PROMPT
        txt = zf.read("shots/shot_lighthouse_01.txt").decode("utf-8")
        assert LONG_PROMPT in txt
        assert "Shot: shot_lighthouse_01" in txt
        csv_text = zf.read("video_gen_shots.csv").decode("utf-8")
        assert LONG_PROMPT[:200] + "..." in csv_text
        assert LONG_PROMPT not in csv_text


async def test_export_rejects_unsafe_shot_ids(client):
    """Shot ids become filenames inside the ZIP; risky ones must be refused."""
    api, c = client

    bad_ids = ["../../evil", "a/b", "a\\b", "", ".."]
    for bad in bad_ids:
        p = await create_project(c, f"危险镜头 {bad!r}")
        state = exportable_state("危险镜头")
        state.storyboard.shots[0].shot_id = bad
        record = api._runtime.store.get_required(p["project_id"])
        api._runtime.store.save_state(
            p["project_id"], state, record.revision, source="manual", summary="x",
        )
        r = await c.get(f"/api/projects/{p['project_id']}/export/video_gen")
        assert r.status_code == 422, (bad, r.status_code, r.text)
        assert "detail" in r.json()

    # Duplicate ids would silently overwrite files inside the ZIP.
    p = await create_project(c, "重复镜头")
    state = exportable_state("重复")
    state.storyboard.shots[1].shot_id = state.storyboard.shots[0].shot_id
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], state, record.revision, source="manual", summary="x",
    )
    r = await c.get(f"/api/projects/{p['project_id']}/export/video_gen")
    assert r.status_code == 422, r.text
    assert "detail" in r.json()


async def test_export_error_semantics(client):
    api, c = client
    p = await seed_exportable(c, api)
    fresh = await create_project(c, "空项目")

    # Unknown project → 404 for every format.
    for fmt in ("json", "fountain", "video_gen"):
        r = await c.get(f"/api/projects/none/export/{fmt}")
        assert r.status_code == 404, (fmt, r.status_code)
        assert "detail" in r.json()

    # Unknown format → 400.
    r = await c.get(f"/api/projects/{p['project_id']}/export/nope")
    assert r.status_code == 400
    assert "detail" in r.json()

    # Missing required artifacts → 400 with a reason, no fake file.
    for fmt in ("fountain", "video_gen"):
        r = await c.get(f"/api/projects/{fresh['project_id']}/export/{fmt}")
        assert r.status_code == 400, (fmt, r.status_code)
        assert "detail" in r.json()


async def test_export_never_mutates_project_history(client):
    api, c = client
    p = await seed_exportable(c, api)

    before = (await c.get(f"/api/projects/{p['project_id']}")).json()
    versions_before = (
        await c.get(f"/api/projects/{p['project_id']}/versions")
    ).json()["versions"]

    for fmt in ("json", "fountain", "video_gen"):
        assert (await c.get(f"/api/projects/{p['project_id']}/export/{fmt}")).status_code == 200

    after = (await c.get(f"/api/projects/{p['project_id']}")).json()
    versions_after = (
        await c.get(f"/api/projects/{p['project_id']}/versions")
    ).json()["versions"]

    assert after["revision"] == before["revision"]
    assert versions_after == versions_before


# ── Storage failure handling ───────────────────────────────


async def test_store_failure_is_500_without_partial_write(client, monkeypatch):
    api, c = client
    p = await create_project(c, "存储会失败的项目", "原标题")

    def broken_rename(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(api._runtime.store, "rename_project", broken_rename)
    r = await c.patch(f"/api/projects/{p['project_id']}",
                      json={"title": "不该出现", "expected_revision": p["revision"]})
    assert r.status_code == 500

    after = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert after["meta"]["title"] == "原标题"
    assert after["revision"] == p["revision"]


# ── Single-instance guarantee (real uvicorn processes) ─────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_api(port: int, data_dir: Path) -> subprocess.Popen:
    env = {**os.environ, "SCRIPTWEAVER_DATA_DIR": str(data_dir)}
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "web.api.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
            "--log-level", "warning",
        ],
        cwd=Path(__file__).parents[2],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/projects", timeout=2.0)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.3)
    return False


def test_second_api_instance_rejected_and_restart_allowed(tmp_path):
    """Second instance on the same data dir refuses to start; after the first
    exits cleanly the directory is lockable again."""
    port1, port2, port3 = _free_port(), _free_port(), _free_port()
    first = _spawn_api(port1, tmp_path)
    try:
        assert _wait_ready(port1), "first instance did not become ready"
        httpx.post(
            f"http://127.0.0.1:{port1}/api/projects",
            json={"user_input": "跨进程持久化"}, timeout=5.0,
        )

        second = _spawn_api(port2, tmp_path)
        out, _ = second.communicate(timeout=30)
        assert second.returncode != 0, "second instance should refuse to start"
        assert "锁定" in out or "lock" in out.lower()

        # First instance still serves normally.
        r = httpx.get(f"http://127.0.0.1:{port1}/api/projects", timeout=5.0)
        assert r.status_code == 200
    finally:
        first.terminate()
        first.wait(timeout=15)

    # After a clean exit the next instance may start on the same directory.
    third = _spawn_api(port3, tmp_path)
    try:
        assert _wait_ready(port3), "restart after clean exit did not become ready"
        listed = httpx.get(f"http://127.0.0.1:{port3}/api/projects", timeout=5.0).json()
        assert any(p["title"] == "跨进程持久化" for p in listed)
    finally:
        third.terminate()
        third.wait(timeout=15)


# ── CLI never writes the web database ─────────────────────


# ── Refine outcome: substantive change or honest failure (#22) ───────


LAST_DIALOGUE = "请你一定要记住今晚的一切，无论将来发生什么事情，都绝对不要忘记我们在这里说过的话。"
SHORTENED_DIALOGUE = "记住今晚的一切。"


def scripted_state() -> ProjectState:
    """A persisted-ready project whose last dialogue is the shorten target."""
    state = outlined_state("修改目标故事", "修改目标故事的一句话梗概")
    state.script = Script(
        title="夜行灯塔",
        scenes=[
            ScriptScene(
                scene_id="sc_first",
                heading=ScriptSceneHeading(scene_number=1, location="天台", time_of_day="夜"),
                blocks=[
                    ScriptBlock(
                        block_type=ScriptBlockType.ACTION,
                        content={"description": "阿芸推开锈蚀的铁门。"},
                    ),
                    ScriptBlock(
                        block_type=ScriptBlockType.DIALOGUE,
                        content={"character_name": "阿芸", "dialogue": "灯不能灭。"},
                    ),
                ],
                characters_involved=["阿芸"],
            ),
            ScriptScene(
                scene_id="sc_last",
                heading=ScriptSceneHeading(scene_number=2, location="码头", time_of_day="黎明"),
                blocks=[
                    ScriptBlock(
                        block_type=ScriptBlockType.ACTION,
                        content={"description": "老周握住阿芸的手。"},
                    ),
                    ScriptBlock(
                        block_type=ScriptBlockType.DIALOGUE,
                        content={"character_name": "老周", "dialogue": LAST_DIALOGUE},
                    ),
                ],
                characters_involved=["老周"],
            ),
        ],
        notes="初稿备注",
        total_estimated_duration=20,
    )
    state.storyboard = Storyboard(shots=[
        Shot(shot_id="shot_1", scene_id="sc_first",
             visual_description="天台外景", duration_seconds=2),
        Shot(shot_id="shot_2", scene_id="sc_last",
             visual_description="码头告别", dialogue=LAST_DIALOGUE, duration_seconds=3),
    ])
    state.storyboard.compute_totals()
    return state


def scripted_script_dict(state: ProjectState) -> dict:
    """The seeded script as the model would receive/echo it (JSON shape)."""
    return state.script.model_dump(mode="json")


class ScriptedLLM:
    """Deterministic model stand-in driving the real refine flow.

    Calls without tools are the Orchestrator's one-shot routing call; calls
    with tools get a single ``write_artifact`` submission of the scripted
    artifact. ``artifact`` may be a callable run per submission so tests can
    simulate concurrent writes from inside the "model call".
    """

    def __init__(self, routing: dict, artifact=None, *, artifact_type: str = "script",
                 routing_error: Exception | None = None):
        self.routing = routing
        self.artifact = artifact
        self.artifact_type = artifact_type
        self.routing_error = routing_error
        self.tool_submissions = 0

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
        from script_weaver.llm.providers import ChatResponse, ToolCall

        if tools is None:
            if self.routing_error is not None:
                raise self.routing_error
            return ChatResponse(content=json.dumps(self.routing, ensure_ascii=False))
        artifact = self.artifact() if callable(self.artifact) else self.artifact
        self.tool_submissions += 1
        return ChatResponse(
            stop_reason="tool_use",
            tool_calls=[ToolCall(
                id="call_scripted",
                name="write_artifact",
                arguments={
                    "artifact_type": self.artifact_type,
                    "content": json.dumps(artifact, ensure_ascii=False),
                },
            )],
        )


def real_engine(api, llm):
    """Patch ``_get_engine`` with the real PipelineEngine over a scripted model.

    Unlike ``stub_engine``, everything except the LLM is production code, so
    routing validation, the diff check and the CAS save all run for real.
    """
    from script_weaver.core.pipeline import PipelineEngine
    from script_weaver.memory import profile as profile_m
    from script_weaver.skills.registry import SkillRegistry

    async def fake_get_engine(project_id):
        record = api._runtime.store.get_required(project_id)
        profile_m._profile_manager = None  # keep profile IO inside this tmp dir
        engine = PipelineEngine(
            llm_client=llm,
            skill_registry=SkillRegistry(),
            auto_approve_gates=record.auto_approve,
        )
        ctx = api._GenerationContext(
            project_id=record.project_id,
            revision=record.revision,
            base_state_json=record.state_json,
            state=record.state,
            store=api._runtime.store,
        )
        return engine, ctx

    api._get_engine = fake_get_engine


async def seed_scripted_project(c: httpx.AsyncClient, api, title: str) -> dict:
    p = await create_project(c, "修改目标故事", title)
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], scripted_state(), record.revision,
        source="manual", summary="seed",
    )
    return p


ROUTE_SHORTEN = {
    "next_agent": "scriptwriter",
    "action": "execute_agent",
    "constraint": "shorten_last_dialogue",
    "reason": "受限修改：只缩短最后一句对白",
    "message_to_user": "",
}

ROUTE_GENERAL_SCRIPT = {
    "next_agent": "scriptwriter",
    "action": "execute_agent",
    "constraint": "general",
    "reason": "普通剧本修改",
    "message_to_user": "",
}


def with_notes_claim(script: dict, claim: str = "已按用户要求缩短最后一句对白。") -> dict:
    """The defect shape: notes claim completion, dialogue untouched."""
    out = copy.deepcopy(script)
    out["notes"] = claim
    return out


def with_shortened_last(script: dict, new_text: str = SHORTENED_DIALOGUE) -> dict:
    out = copy.deepcopy(script)
    out["scenes"][1]["blocks"][1]["content"]["dialogue"] = new_text
    return out


async def assert_project_untouched(c: httpx.AsyncClient, api, p: dict) -> None:
    """Nothing was written: same revision, history, memory and content."""
    current = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert current["revision"] == 2, "rejected refine must not create a revision"
    versions = (await c.get(f"/api/projects/{p['project_id']}/versions")).json()["versions"]
    assert len(versions) == 2, "rejected refine must not create history"
    assert current["script"]["notes"] == "初稿备注"
    assert current["script"]["scenes"][1]["blocks"][1]["content"]["dialogue"] == LAST_DIALOGUE
    assert current["memory_decisions"] == [], "rejected refine must not record decisions"


async def test_refine_notes_only_claim_rejected_no_write(client):
    """Defect regression (#22): the model only claims completion in
    script.notes while the dialogue stays identical. Old behaviour saved a
    new revision and answered 200; the fix must answer 422 and write
    nothing."""
    api, c = client
    p = await seed_scripted_project(c, api, "修改目标A")
    base = scripted_script_dict(scripted_state())

    llm = ScriptedLLM(ROUTE_SHORTEN, with_notes_claim(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine", json={
        "message": "只把结尾最后一句对白改得更简短，保持人物、地点和主要情节不变",
    })
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "refine_constraint_failed"
    assert detail["message"]
    await assert_project_untouched(c, api, p)


async def test_refine_general_notes_only_change_is_no_meaningful_change(client):
    api, c = client
    p = await seed_scripted_project(c, api, "修改目标B")
    base = scripted_script_dict(scripted_state())

    llm = ScriptedLLM(ROUTE_GENERAL_SCRIPT, with_notes_claim(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "把剧本对白整体调整得更简洁"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "refine_no_meaningful_change"
    await assert_project_untouched(c, api, p)


async def test_refine_shorten_last_dialogue_success(client):
    api, c = client
    p = await seed_scripted_project(c, api, "缩短成功")
    base = scripted_script_dict(scripted_state())
    b = await create_project(c, "B 的想法", "项目B")

    llm = ScriptedLLM(ROUTE_SHORTEN, with_shortened_last(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine", json={
        "message": "只把结尾最后一句对白改得更简短，其他内容保持不变",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 3
    assert body["changed_artifacts"] == ["script"]
    assert body["total_changes"] == 1
    [change] = body["changes"]
    assert change["path"] == "script.scenes[1].blocks[1].content.dialogue"
    assert change["before"] == LAST_DIALOGUE
    assert change["after"] == SHORTENED_DIALOGUE
    assert "分镜" in body["notice"] and "未自动同步" in body["notice"]

    current = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert current["revision"] == 3
    assert current["script"]["scenes"][1]["blocks"][1]["content"]["dialogue"] == SHORTENED_DIALOGUE
    assert current["script"]["notes"] == "初稿备注"  # untouched by the constrained edit
    assert len(current["memory_decisions"]) == 1

    versions = (await c.get(f"/api/projects/{p['project_id']}/versions")).json()["versions"]
    assert [v["revision"] for v in versions] == [3, 2, 1]  # newest first
    new_version = (await c.get(f"/api/projects/{p['project_id']}/versions/3")).json()
    assert new_version["script"]["scenes"][1]["blocks"][1]["content"]["dialogue"] == SHORTENED_DIALOGUE
    old_version = (await c.get(f"/api/projects/{p['project_id']}/versions/2")).json()
    assert old_version["script"]["scenes"][1]["blocks"][1]["content"]["dialogue"] == LAST_DIALOGUE

    # Project B is untouched by A's refine.
    b_current = (await c.get(f"/api/projects/{b['project_id']}")).json()
    assert b_current["revision"] == 1 and b_current["script"] is None


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda s: s, "对白完全未修改"),
        (lambda s: with_shortened_last(s, "   "), "对白清空/仅空白"),
        (lambda s: with_shortened_last(s, LAST_DIALOGUE + "，就这样。"), "对白变更长"),
        (lambda s: _set(s, ["notes"], "只改了备注"), "只改备注"),
        (lambda s: _set(s, ["scenes", 0, "blocks", 1, "content", "dialogue"], "灯，不能灭。"),
         "改错句子（第一句）"),
        (lambda s: _set(s, ["scenes", 1, "blocks", 1, "content", "character_name"], "阿芸"),
         "改了角色名"),
        (lambda s: _set(s, ["scenes", 1, "blocks", 0, "content", "description"], "老周松开手。"),
         "改了动作描述"),
        (lambda s: _set(s, ["scenes", 1, "scene_id"], "sc_regenerated"), "ID 被改写"),
        (lambda s: _swap(s), "场次顺序调换"),
        (lambda s: with_shortened_last(s), "正常缩短（阳性对照）"),
    ],
)
async def test_refine_shorten_counterexamples(client, mutate, reason):
    """Every out-of-bounds shape must be rejected whole — no partial adoption."""
    api, c = client
    p = await seed_scripted_project(c, api, "缩短反例")
    base = scripted_script_dict(scripted_state())

    llm = ScriptedLLM(ROUTE_SHORTEN, mutate(copy.deepcopy(base)))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "只缩短最后一句对白"})
    if reason == "正常缩短（阳性对照）":
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 422, f"{reason}: {r.text}"
        assert r.json()["detail"]["code"] == "refine_constraint_failed"
        await assert_project_untouched(c, api, p)


def _set(script: dict, path: list, value) -> dict:
    """Set a nested value in the scripted script dict (in place)."""
    node = script
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return script


def _swap(script: dict) -> dict:
    script["scenes"][0], script["scenes"][1] = script["scenes"][1], script["scenes"][0]
    return script


@pytest.mark.parametrize(
    "routing,reason",
    [
        ({"next_agent": "scriptwriter", "action": "ask_user",
          "message_to_user": "要缩短哪一句？", "constraint": "general"}, "ask_user"),
        ({"next_agent": None, "action": "complete", "constraint": "general"}, "complete"),
        ({"next_agent": "reviewer", "action": "execute_agent", "constraint": "general"},
         "不可修改的 Agent"),
        ({"next_agent": "unknown_agent", "action": "execute_agent", "constraint": "general"},
         "未知 Agent"),
        ({"next_agent": "scriptwriter", "action": "execute", "constraint": "general"},
         "未知 action"),
        ({"next_agent": "scriptwriter", "action": "execute_agent", "constraint": "shorten"},
         "未知 constraint"),
        ({"next_agent": "storyboard_artist", "action": "execute_agent",
          "constraint": "shorten_last_dialogue"}, "受限约束必须路由 scriptwriter"),
        # Review round 2: non-string routing fields must be refused as 422,
        # not crash the handler with a TypeError (500).
        ({"next_agent": "scriptwriter", "action": []}, "action 是数组"),
        ({"next_agent": "scriptwriter", "action": {}}, "action 是对象"),
        ({"next_agent": "scriptwriter", "action": 1}, "action 是数字"),
        ({"next_agent": "scriptwriter", "action": "execute_agent", "constraint": {}},
         "constraint 是对象"),
        ({"next_agent": "scriptwriter", "action": "execute_agent", "constraint": []},
         "constraint 是数组"),
        ({"next_agent": "scriptwriter", "action": "execute_agent", "constraint": 3},
         "constraint 是数字"),
    ],
)
async def test_refine_unusable_routing_is_not_executable(client, routing, reason):
    api, c = client
    p = await seed_scripted_project(c, api, "路由反例")
    base = scripted_script_dict(scripted_state())

    llm = ScriptedLLM(routing, with_shortened_last(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "随便改点什么"})
    assert r.status_code == 422, f"{reason}: {r.text}"
    assert r.json()["detail"]["code"] == "refine_not_executable"
    assert llm.tool_submissions == 0, f"{reason}: 不可执行路由不应调用目标 Agent"
    await assert_project_untouched(c, api, p)


async def test_refine_routing_text_response_is_not_executable(client):
    api, c = client
    p = await seed_scripted_project(c, api, "路由文本")

    class TextLLM(ScriptedLLM):
        async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
            from script_weaver.llm.providers import ChatResponse

            if tools is None:
                return ChatResponse(content="我觉得应该先问问用户。")
            return await super().chat(messages, tools, temperature, max_tokens)

    real_engine(api, TextLLM(ROUTE_GENERAL_SCRIPT, with_shortened_last(
        scripted_script_dict(scripted_state()))))

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "改一下"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "refine_not_executable"
    await assert_project_untouched(c, api, p)


@pytest.mark.parametrize("strip_script", [True, False], ids=["无剧本", "无对白"])
async def test_refine_shorten_without_target_is_target_not_found(client, strip_script):
    api, c = client
    p = await create_project(c, "修改目标故事", "目标缺失")
    state = scripted_state()
    if strip_script:
        state.script = None
        state.storyboard = None
    else:
        for scene in state.script.scenes:
            scene.blocks = [b for b in scene.blocks
                            if b.block_type != ScriptBlockType.DIALOGUE]
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], state, record.revision, source="manual", summary="seed")

    llm = ScriptedLLM(ROUTE_SHORTEN, {"scenes": []})
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "只缩短最后一句对白"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "refine_target_not_found"
    assert llm.tool_submissions == 0, "目标不存在时不应调用执行 Agent"
    current = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert current["revision"] == 2
    versions = (await c.get(f"/api/projects/{p['project_id']}/versions")).json()["versions"]
    assert len(versions) == 2
    assert current["memory_decisions"] == []


async def test_refine_model_failure_is_sanitized_502(client):
    api, c = client
    p = await seed_scripted_project(c, api, "模型失败")

    llm = ScriptedLLM(
        ROUTE_GENERAL_SCRIPT, None,
        routing_error=RuntimeError("connection refused to api.deepseek.com (key=sk-secret)"),
    )
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "改一下"})
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "refine_model_failed"
    assert "sk-secret" not in json.dumps(detail)  # sanitized
    assert "deepseek" not in json.dumps(detail).lower()
    await assert_project_untouched(c, api, p)


async def test_refine_invalid_artifact_is_model_failure_no_write(client):
    api, c = client
    p = await seed_scripted_project(c, api, "非法产物")
    llm = ScriptedLLM(ROUTE_GENERAL_SCRIPT, {"scenes": []})  # empty script
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "改一下"})
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["code"] == "refine_model_failed"
    await assert_project_untouched(c, api, p)


async def test_refine_general_real_change_succeeds_with_true_diff(client):
    api, c = client
    p = await seed_scripted_project(c, api, "普通修改成功")
    base = scripted_script_dict(scripted_state())

    def rewrite_first_dialogue(script: dict) -> dict:
        out = copy.deepcopy(script)
        out["scenes"][0]["blocks"][1]["content"]["dialogue"] = "灯，不能灭。"
        return out

    llm = ScriptedLLM(ROUTE_GENERAL_SCRIPT, rewrite_first_dialogue(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "第一句对白加个顿号"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed_artifacts"] == ["script"]
    assert body["total_changes"] == 1
    assert body["changes"][0]["before"] == "灯不能灭。"
    assert body["changes"][0]["after"] == "灯，不能灭。"


async def test_refine_auto_id_shuffle_is_no_meaningful_change(client):
    """Regenerated IDs alone must not masquerade as a substantive edit."""
    api, c = client
    p = await seed_scripted_project(c, api, "ID 重生成")
    base = scripted_script_dict(scripted_state())

    def scene_ids_only(script: dict) -> dict:
        out = copy.deepcopy(script)
        out["scenes"][0]["scene_id"] = "sc_new_1"
        out["scenes"][1]["scene_id"] = "sc_new_2"
        return out

    llm = ScriptedLLM(ROUTE_GENERAL_SCRIPT, scene_ids_only(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "重写剧本"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "refine_no_meaningful_change"
    await assert_project_untouched(c, api, p)


async def test_refine_conflict_when_project_changes_during_model_call(client):
    api, c = client
    p = await seed_scripted_project(c, api, "并发修改")
    base = scripted_script_dict(scripted_state())

    class ConcurrentWriterLLM(ScriptedLLM):
        async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
            if tools is not None and self.tool_submissions == 0:
                record = api._runtime.store.get_required(p["project_id"])
                api._runtime.store.rename_project(
                    p["project_id"], "并发期间改名", record.revision)
            return await super().chat(messages, tools, temperature, max_tokens)

    real_engine(api, ConcurrentWriterLLM(ROUTE_SHORTEN, with_shortened_last(base)))

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "只缩短最后一句对白"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "revision_conflict"
    assert detail["current_revision"] == 3
    # The concurrent rename survives; the refine result was not applied.
    current = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert current["meta"]["title"] == "并发期间改名"
    assert current["script"]["scenes"][1]["blocks"][1]["content"]["dialogue"] == LAST_DIALOGUE


async def test_refine_store_failure_is_500_without_partial_write(client, monkeypatch):
    import sqlite3

    api, c = client
    p = await seed_scripted_project(c, api, "存储失败")
    base = scripted_script_dict(scripted_state())
    real_engine(api, ScriptedLLM(ROUTE_SHORTEN, with_shortened_last(base)))

    original_save = api._runtime.store.save_state

    def broken_save(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(api._runtime.store, "save_state", broken_save)
    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "只缩短最后一句对白"})
    assert r.status_code == 500, r.text
    monkeypatch.setattr(api._runtime.store, "save_state", original_save)
    await assert_project_untouched(c, api, p)


async def test_refine_summary_truncates_to_ten_previews_300(client):
    api, c = client
    p = await create_project(c, "修改目标故事", "摘要截断")
    state = outlined_state("修改目标故事", "摘要截断的故事梗概")
    long_text = "很长很长的对白" * 60  # >300 chars
    blocks = [
        ScriptBlock(
            block_type=ScriptBlockType.ACTION,
            content={"description": "群像场景。"},
        ),
    ]
    blocks += [
        ScriptBlock(
            block_type=ScriptBlockType.DIALOGUE,
            content={"character_name": f"角色{i}", "dialogue": long_text if i == 0 else f"第{i}句原对白"},
        )
        for i in range(12)
    ]
    state.script = Script(
        title="群像",
        scenes=[ScriptScene(scene_id="sc_only", heading=ScriptSceneHeading(
            scene_number=1, location="广场", time_of_day="夜"), blocks=blocks)],
    )
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], state, record.revision, source="manual", summary="seed")

    def rewrite_all(script: dict) -> dict:
        out = copy.deepcopy(script)
        for block in out["scenes"][0]["blocks"]:
            if block["block_type"] == "dialogue":
                block["content"]["dialogue"] += "改"
        return out

    llm = ScriptedLLM(ROUTE_GENERAL_SCRIPT, rewrite_all(state.script.model_dump(mode="json")))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "整体改写"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_changes"] == 12
    assert len(body["changes"]) == 10  # capped for display; full diff lives in history
    for change in body["changes"]:
        assert len(change["before"]) <= 303  # 300 + "..."
        assert len(change["after"]) <= 303


# ── Review round 2: raw strict compare + reference integrity ────────


async def test_refine_shorten_whitespace_padded_other_id_rejected(client):
    """The constrained check must compare raw values: padding another
    scene's ID with whitespace is an out-of-bounds change, not a no-op."""
    api, c = client
    p = await seed_scripted_project(c, api, "空白ID越界")
    base = scripted_script_dict(scripted_state())

    def pad_other_scene_id(script: dict) -> dict:
        out = with_shortened_last(script)
        out["scenes"][0]["scene_id"] = f" {out['scenes'][0]['scene_id']} "
        return out

    llm = ScriptedLLM(ROUTE_SHORTEN, pad_other_scene_id(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "只缩短最后一句对白"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "refine_constraint_failed"
    assert "scene_id" in r.json()["detail"]["message"]
    await assert_project_untouched(c, api, p)


async def test_refine_general_rebuilt_referenced_id_rejected_no_write(client):
    """Real Pipeline → API → Store chain: a substantive dialogue edit that
    also rebuilds a storyboard-referenced scene_id must be refused whole —
    saving it would leave the export's scene→character lookup dangling."""
    api, c = client
    p = await seed_scripted_project(c, api, "引用重建")
    base = scripted_script_dict(scripted_state())

    def shorten_first_and_rebuild_id(script: dict) -> dict:
        out = copy.deepcopy(script)
        out["scenes"][0]["blocks"][1]["content"]["dialogue"] = "灯，不能灭。"
        out["scenes"][0]["scene_id"] = "sc_new"  # storyboard shot_1 refs sc_first
        return out

    llm = ScriptedLLM(ROUTE_GENERAL_SCRIPT, shorten_first_and_rebuild_id(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "第一句对白加顿号"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "refine_constraint_failed"
    assert "storyboard.shots[0].scene_id" in r.json()["detail"]["message"]
    await assert_project_untouched(c, api, p)


async def test_refine_general_intact_references_succeed(client):
    api, c = client
    p = await seed_scripted_project(c, api, "引用完整修改")
    base = scripted_script_dict(scripted_state())

    def keep_ids_change_dialogue(script: dict) -> dict:
        out = copy.deepcopy(script)
        out["scenes"][0]["blocks"][1]["content"]["dialogue"] = "灯，不能灭。"
        return out

    llm = ScriptedLLM(ROUTE_GENERAL_SCRIPT, keep_ids_change_dialogue(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "第一句对白加顿号"})
    assert r.status_code == 200, r.text
    assert r.json()["changed_artifacts"] == ["script"]


# ── Review round 3: stable defect identity + auto-ID exclusions ────

ROUTE_GENERAL_CHARACTERS = {
    "next_agent": "character_designer",
    "action": "execute_agent",
    "constraint": "general",
    "reason": "普通角色修改",
    "message_to_user": "",
}

ROUTE_GENERAL_SCENES = {
    "next_agent": "scene_designer",
    "action": "execute_agent",
    "constraint": "general",
    "reason": "普通场景设计修改",
    "message_to_user": "",
}


def designed_scripted_state() -> ProjectState:
    """scripted_state plus a historical defect: sc_first→old_missing
    (dangling), sc_last→sd_ok (valid)."""
    state = scripted_state()
    state.scenes = [SceneDesign(id="sd_ok", name="码头", environment="夜色中的码头")]
    state.script.scenes[0].scene_design_id = "old_missing"
    state.script.scenes[1].scene_design_id = "sd_ok"
    return state


async def seed_designed_project(c: httpx.AsyncClient, api, title: str) -> dict:
    p = await create_project(c, "修改目标故事", title)
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], designed_scripted_state(), record.revision,
        source="manual", summary="seed",
    )
    return p


async def test_refine_repair_one_break_other_same_index_rejected_no_write(client):
    """Real Pipeline → API → Store leak regression: repairing sc_first's
    design ref while breaking sc_last's — after a swap both snapshots show
    the defect at scenes[0], so the index-keyed comparison waved it through
    and saved a new revision. Identity-keyed comparison must refuse whole."""
    api, c = client
    p = await seed_designed_project(c, api, "同位换缺陷")
    base = scripted_script_dict(designed_scripted_state())

    def swap_repair_and_break(script: dict) -> dict:
        out = copy.deepcopy(script)
        out["scenes"].reverse()                                # sc_last first now
        out["scenes"][0]["scene_design_id"] = "new_missing"    # sc_last breaks
        out["scenes"][1]["scene_design_id"] = "sd_ok"          # sc_first repaired
        out["scenes"][0]["blocks"][1]["content"]["dialogue"] = "请一定记住今晚。"
        return out

    llm = ScriptedLLM(ROUTE_GENERAL_SCRIPT, swap_repair_and_break(base))
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "交换两场并修好第一场的场景引用"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "refine_constraint_failed"
    assert "scene_design_id" in r.json()["detail"]["message"]
    await assert_project_untouched(c, api, p)
    current = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert current["script"]["scenes"][0]["scene_design_id"] == "old_missing"
    assert current["script"]["scenes"][1]["scene_design_id"] == "sd_ok"


async def test_refine_character_id_rebuild_only_is_no_meaningful_change(client):
    """Real Pipeline → API → Store: the model echoes the character minus
    its id; Pydantic regenerates char_xxx and the echo used to count as a
    substantive edit (200 + new revision). Only real content may."""
    api, c = client
    p = await create_project(c, "修改目标故事", "角色ID重建")
    state = scripted_state()
    state.characters = [Character(id="char_ayun", name="阿芸", personality="坚韧")]
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], state, record.revision, source="manual", summary="seed")

    # The model echoes the identical content but omits id — Pydantic's
    # default_factory mints a fresh char_xxx during integration.
    echoed_without_id = [{"name": "阿芸", "personality": "坚韧"}]
    llm = ScriptedLLM(ROUTE_GENERAL_CHARACTERS, echoed_without_id,
                      artifact_type="characters")
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "把主角性格写得更鲜明"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "refine_no_meaningful_change"
    await assert_project_untouched(c, api, p)
    current = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert current["characters"][0]["id"] == "char_ayun"


async def test_refine_scene_design_id_rebuild_only_is_no_meaningful_change(client):
    """Same defect shape on an unreferenced SceneDesign: id regeneration
    alone must not create a version."""
    api, c = client
    p = await create_project(c, "修改目标故事", "场景ID重建")
    state = scripted_state()
    state.scenes = [SceneDesign(id="sd_roof", name="天台", environment="夜风中的天台")]
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], state, record.revision, source="manual", summary="seed")

    # Identical content, id omitted — a fresh scene_xxx is minted on save.
    echoed_without_id = [{"name": "天台", "environment": "夜风中的天台"}]
    llm = ScriptedLLM(ROUTE_GENERAL_SCENES, echoed_without_id,
                      artifact_type="scenes")
    real_engine(api, llm)

    r = await c.post(f"/api/projects/{p['project_id']}/refine",
                     json={"message": "把天台环境写得更具体"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "refine_no_meaningful_change"
    await assert_project_untouched(c, api, p)
    current = (await c.get(f"/api/projects/{p['project_id']}")).json()
    assert current["scenes"][0]["id"] == "sd_roof"


def test_cli_generate_does_not_touch_web_database(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from script_weaver import cli
    from script_weaver.core import config
    from script_weaver.memory import profile

    data_dir = tmp_path / "data"
    out_dir = tmp_path / "out"
    monkeypatch.setattr(
        config, "_settings",
        config.Settings(data_dir=data_dir, skills_custom_dir=data_dir / "skills"),
    )
    monkeypatch.setattr(profile, "_profile_manager", None)

    class StubEngine:
        async def run_full_pipeline(self, **kwargs):
            return outlined_state(kwargs.get("user_input", ""), "CLI 生成结果")

    monkeypatch.setattr(cli, "_create_engine", lambda auto_approve: StubEngine())

    result = CliRunner().invoke(
        cli.main,
        ["generate", "CLI 测试想法", "--auto-approve", "--output-dir", str(out_dir)],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "project.json").exists()
    # The CLI writes only to its output dir; the web namespace stays absent.
    assert not (data_dir / "main-web").exists()
