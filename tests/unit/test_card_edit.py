"""Manual single-card editing (ticket #16): API contract + store transaction.

Uses the real FastAPI app over an isolated data dir; no model keys and no
model calls exist anywhere on these paths — a card save is pure storage.
"""

import httpx
import pytest
from pydantic import ValidationError

from script_weaver.core.config import get_settings
from script_weaver.core.project_store import ProjectStore, RevisionConflictError
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
    VisualHighlight,
)


@pytest.fixture
async def client(api_factory):
    api = api_factory()
    async with api.app.router.lifespan_context(api.app):
        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield api, c


async def create_project(c: httpx.AsyncClient, user_input: str, title: str) -> dict:
    r = await c.post("/api/projects", json={"user_input": user_input, "title": title})
    assert r.status_code == 200, r.text
    return r.json()


# ── Seeding ────────────────────────────────────────────────


def full_card_state(user_input: str, *, shots: int = 2) -> ProjectState:
    """Characters + scenes + script + storyboard + highlights, all linked."""
    state = ProjectState(user_input=user_input)
    state.outline = Outline(basic_info=BasicInfo(logline="卡片编辑验收"))
    state.refined_idea = "卡片编辑验收"
    state.meta.status = ProjectStatus.COMPLETE
    state.characters = [
        Character(
            id="char_a", name="阿芸", role="protagonist",
            appearance="二十岁出头的守灯人",
            personality="倔强",
            costume_description="深蓝色粗布衣",
            key_props=["马灯", "钥匙"],
            backstory="从小在灯塔长大",
            motivation="让灯不灭",
            relationship_map={"陈叔": "师徒"},
            image_prompt="portrait of a lighthouse keeper",
        ),
        Character(id="char_b", name="陈叔", role="supporting", motivation="帮阿芸"),
    ]
    state.scenes = [
        SceneDesign(
            id="scene_a", name="灯塔顶层", location_type="interior",
            environment="狭窄的圆形灯室", time_of_day="深夜",
            weather="暴雨", mood="紧张", lighting_description="只有一盏马灯",
            color_palette=["#0B1020", "#E0AE57"],
            key_elements=["旋转灯组", "木梯"],
            image_prompt="interior of a lighthouse lamp room",
        ),
        SceneDesign(id="scene_b", name="礁石滩", location_type="exterior"),
    ]
    state.script = Script(
        title="夜行灯塔",
        scenes=[ScriptScene(
            scene_id="sc_1",
            heading=ScriptSceneHeading(location="灯塔顶层", time_of_day="夜"),
            blocks=[ScriptBlock(
                block_type=ScriptBlockType.ACTION,
                content={"description": "阿芸握紧马灯。"},
            )],
        )],
    )
    state.storyboard = Storyboard(shots=[
        Shot(
            shot_id=f"shot_{i:02d}", scene_id="sc_1", sequence_number=i,
            shot_size="medium_shot", camera_angle="eye_level",
            camera_movement="static", visual_description=f"镜头{i}",
            duration_seconds=2, transition_to_next="cut",
        )
        for i in range(1, shots + 1)
    ])
    state.storyboard.compute_totals()
    state.visual_highlights = [
        VisualHighlight(id="vh_1", title="灯不灭", related_shot_ids=["shot_01"]),
    ]
    return state


async def seed_card_project(c: httpx.AsyncClient, api, **kwargs) -> dict:
    p = await create_project(c, "卡片编辑验收", "卡片编辑")
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], full_card_state("卡片编辑验收", **kwargs),
        record.revision, source="manual", summary="卡片编辑种子",
    )
    return p


def patch_url(project_id: str, kind: str, target_id: str) -> str:
    return f"/api/projects/{project_id}/artifacts/{kind}/{target_id}"


async def patch_card(
    c: httpx.AsyncClient, project_id: str, kind: str, target_id: str,
    changes: dict, expected_revision: int,
) -> httpx.Response:
    return await c.patch(
        patch_url(project_id, kind, target_id),
        json={"changes": changes, "expected_revision": expected_revision},
    )


async def write_facts(api, project_id: str) -> tuple[int, list[int], dict]:
    """Everything a refused save must leave untouched."""
    record = api._runtime.store.get_required(project_id)
    versions = api._runtime.store.list_versions(project_id)
    return record.revision, [v.revision for v in versions], record.review


def restart_store(api) -> ProjectStore:
    """A fresh store over the same database — the restart read."""
    return ProjectStore(get_settings().data_dir / "main-web" / "projects.sqlite3")


# ── Happy path: three kinds, persistence, history ──────────


async def test_character_edit_roundtrip_persists_and_versions(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]

    r = await patch_card(c, pid, "characters", "char_a",
                         {"name": "阿云", "key_props": ["马灯", "铜哨"]}, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["revision"] == 3
    updated = next(ch for ch in body["characters"] if ch["id"] == "char_a")
    assert updated["name"] == "阿云"
    assert updated["key_props"] == ["马灯", "铜哨"]

    # Re-read through the API: current content reflects the save.
    after = (await c.get(f"/api/projects/{pid}")).json()
    assert next(ch for ch in after["characters"] if ch["id"] == "char_a")["name"] == "阿云"
    # Restart read: a fresh store over the same database sees the same bytes.
    restarted = restart_store(api).get_required(pid)
    assert next(ch for ch in restarted.state.characters if ch.id == "char_a").name == "阿云"
    assert restarted.state_json == api._runtime.store.get_required(pid).state_json

    # Previous version still viewable, unchanged.
    before = (await c.get(f"/api/projects/{pid}/versions/2")).json()
    old = next(ch for ch in before["characters"] if ch["id"] == "char_a")
    assert old["name"] == "阿芸"
    versions = (await c.get(f"/api/projects/{pid}/versions")).json()["versions"]
    latest = versions[0]
    assert latest["revision"] == 3 and latest["source"] == "manual"
    assert "编辑角色" in latest["summary"] and "阿云" in latest["summary"]


async def test_scene_edit_roundtrip_and_selective_change(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]

    r = await patch_card(c, pid, "scenes", "scene_a",
                         {"location_type": "exterior", "weather": None}, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 3
    scenes = body["scenes"]
    assert scenes[0]["location_type"] == "exterior"
    assert scenes[0]["weather"] is None
    # The untouched sibling keeps its exact content and position.
    assert scenes[1]["id"] == "scene_b" and scenes[1]["name"] == "礁石滩"
    assert [s["id"] for s in scenes] == ["scene_a", "scene_b"]
    assert [ch["id"] for ch in body["characters"]] == ["char_a", "char_b"]

    after = (await c.get(f"/api/projects/{pid}")).json()
    assert after["scenes"][0]["location_type"] == "exterior"
    restarted = restart_store(api).get_required(pid)
    assert restarted.state.scenes[0].weather is None


async def test_shot_edit_recomputes_totals(client):
    api, c = client
    p = await seed_card_project(c, api, shots=3)
    pid = p["project_id"]
    assert api._runtime.store.get_required(pid).state.storyboard.total_estimated_duration == 6

    r = await patch_card(c, pid, "shots", "shot_02", {"duration_seconds": 4.5}, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    sb = body["storyboard"]
    assert sb["total_estimated_duration"] == 8.5
    assert sb["total_shot_count"] == 3
    shot = next(s for s in sb["shots"] if s["shot_id"] == "shot_02")
    assert shot["duration_seconds"] == 4.5

    restarted = restart_store(api).get_required(pid)
    assert restarted.state.storyboard.total_estimated_duration == 8.5


# ── Refusals: zero-write guarantees ────────────────────────


async def assert_zero_writes(api, c, pid, baseline):
    revision, versions, review = await write_facts(api, pid)
    assert (revision, versions, review) == baseline


async def test_non_canonical_enum_values_rejected_without_write(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    baseline = await write_facts(api, pid)

    # A lenient-parsing synonym ("主角") and an unknown value both refuse.
    for bad in ("主角", "boss", "Protagonist"):
        r = await patch_card(c, pid, "characters", "char_a", {"role": bad}, 2)
        assert r.status_code == 422, (bad, r.text)
        detail = r.json()["detail"]
        assert detail["code"] == "invalid_card_change"
        assert detail["field"] == "role"
    r = await patch_card(c, pid, "scenes", "scene_a",
                         {"location_type": "INT."}, 2)
    assert r.status_code == 422  # a SceneLocationType value, not EnvironmentType
    await assert_zero_writes(api, c, pid, baseline)

    # The canonical value still saves.
    r = await patch_card(c, pid, "characters", "char_a", {"role": "antagonist"}, 2)
    assert r.status_code == 200 and r.json()["changed"] is True


async def test_bad_durations_rejected_without_write(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    baseline = await write_facts(api, pid)

    for bad in (0, -1, -0.5, "3", True, False, None):
        r = await patch_card(c, pid, "shots", "shot_01",
                             {"duration_seconds": bad}, 2)
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["detail"]["code"] == "invalid_card_change"
    await assert_zero_writes(api, c, pid, baseline)

    # A valid positive duration (int or float) still saves.
    r = await patch_card(c, pid, "shots", "shot_01", {"duration_seconds": 1.5}, 2)
    assert r.status_code == 200 and r.json()["changed"] is True


async def test_unknown_and_read_only_fields_rejected_even_unchanged(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    baseline = await write_facts(api, pid)

    r = await patch_card(c, pid, "characters", "char_a",
                         {"name": "阿芸", "weapon": "马灯"}, 2)
    assert r.status_code == 422
    assert r.json()["detail"]["field"] == "weapon"

    # Read-only identity/media fields: rejected even when value unchanged.
    read_only_cases = [
        ("characters", "char_a", {"id": "char_a"}),
        ("characters", "char_a", {"image_reference_url": "https://x/y.png"}),
        ("scenes", "scene_a", {"id": "scene_a"}),
        ("shots", "shot_01", {"shot_id": "shot_01"}),
        ("shots", "shot_01", {"scene_id": "sc_1"}),
        ("shots", "shot_01", {"sequence_number": 1}),
        ("shots", "shot_01", {"reference_image_url": None}),
    ]
    for kind, tid, changes in read_only_cases:
        r = await patch_card(c, pid, kind, tid, changes, 2)
        assert r.status_code == 422, (kind, changes, r.text)
        assert "只读" in r.json()["detail"]["message"]
    await assert_zero_writes(api, c, pid, baseline)


async def test_unknown_kind_is_validation_error(client):
    api, c = client
    p = await seed_card_project(c, api)
    r = await c.patch(
        f"/api/projects/{p['project_id']}/artifacts/outlines/xyz",
        json={"changes": {}, "expected_revision": 2},
    )
    assert r.status_code == 422


async def test_wrong_project_target_answers_404_without_write(client):
    api, c = client
    pa = await seed_card_project(c, api)
    pb = await create_project(c, "另一个项目", "项目B")
    record_b = api._runtime.store.get_required(pb["project_id"])
    record_b.state.characters = [Character(id="char_only_in_b", name="乙")]
    api._runtime.store.save_state(pb["project_id"], record_b.state, record_b.revision,
                                  source="manual", summary="B 的角色")
    baseline = await write_facts(api, pa["project_id"])

    # char_only_in_b exists, but in project B — not in project A.
    r = await patch_card(c, pa["project_id"], "characters", "char_only_in_b",
                         {"name": "x"}, 2)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "card_not_found"
    await assert_zero_writes(api, c, pa["project_id"], baseline)


async def test_duplicate_target_id_refused_as_data_anomaly(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    # Corrupt the stored data with a duplicated stable id.
    record = api._runtime.store.get_required(pid)
    record.state.characters.append(
        Character(id="char_a", name="冒名顶替者", role="extra")
    )
    api._runtime.store.save_state(pid, record.state, record.revision,
                                  source="manual", summary="注入重复 ID")
    baseline = await write_facts(api, pid)

    r = await patch_card(c, pid, "characters", "char_a", {"name": "改动"}, 3)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "duplicate_target_id"
    assert "数据异常" in detail["message"]
    await assert_zero_writes(api, c, pid, baseline)


# ── Revision CAS, atomicity, unchanged saves ───────────────


async def test_two_saves_with_same_stale_revision_only_one_wins(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]

    first = await patch_card(c, pid, "characters", "char_a", {"name": "第一份"}, 2)
    second = await patch_card(c, pid, "scenes", "scene_a", {"mood": "第二份"}, 2)
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["current_revision"] == 3
    # The loser wrote nothing: neither content nor history moved.
    versions = [v.revision for v in api._runtime.store.list_versions(pid)]
    assert versions == [3, 2, 1]


async def test_failed_save_rolls_back_content_history_and_review(client, monkeypatch):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    baseline = await write_facts(api, pid)

    store = api._runtime.store
    original = store._write_new_state

    def exploding_write(*args, **kwargs):
        raise RuntimeError("模拟提交前的存储故障")

    monkeypatch.setattr(store, "_write_new_state", exploding_write)
    r = await patch_card(c, pid, "characters", "char_a", {"name": "不该存活"}, 2)
    monkeypatch.setattr(store, "_write_new_state", original)

    assert r.status_code == 500  # storage failure surfaces as a server error
    await assert_zero_writes(api, c, pid, baseline)
    restarted = restart_store(api).get_required(pid)
    assert next(ch for ch in restarted.state.characters if ch.id == "char_a").name == "阿芸"


async def test_unchanged_save_reports_changed_false_without_new_version(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    baseline = await write_facts(api, pid)

    r = await patch_card(c, pid, "characters", "char_a",
                         {"name": "阿芸", "role": "protagonist",
                          "key_props": ["马灯", "钥匙"],
                          "relationship_map": {"陈叔": "师徒"}}, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is False
    assert body["revision"] == 2  # no revision bump
    assert body["review"].get("review_flags", []) == []  # no flags from a no-op
    await assert_zero_writes(api, c, pid, baseline)

    # An unchanged save is still validated on its merits.
    r = await patch_card(c, pid, "characters", "char_a", {"role": "主角"}, 2)
    assert r.status_code == 422
    r = await patch_card(c, pid, "characters", "char_a", {"name": "阿芸"}, 99)
    assert r.status_code == 409


# ── Review flags ───────────────────────────────────────────


def flags_for(review: dict, artifact: str) -> list[dict]:
    return [f for f in review.get("review_flags", [])
            if f.get("artifact") == artifact]


async def test_review_flags_cover_existing_downstream_only(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]

    r = await patch_card(c, pid, "characters", "char_a", {"name": "阿云"}, 2)
    review = r.json()["review"]
    for artifact in ("script", "storyboard", "visual_highlights"):
        flags = flags_for(review, artifact)
        assert len(flags) == 1, artifact
        flag = flags[0]
        assert flag["reason"] == "upstream_manual_edit"
        assert flag["upstream_kind"] == "characters"
        assert flag["upstream_id"] == "char_a"
        assert flag["upstream_label"] == "阿云"
        assert flag["since_revision"] == 3

    # A no-op save must not add flags.
    r = await patch_card(c, pid, "characters", "char_a", {"name": "阿云"}, 3)
    assert len(r.json()["review"]["review_flags"]) == 3

    # A shot edit flags only the highlights (totals are recomputed locally).
    r = await patch_card(c, pid, "shots", "shot_01", {"duration_seconds": 9}, 3)
    review = r.json()["review"]
    assert flags_for(review, "script") and len(flags_for(review, "script")) == 1
    shot_flag = flags_for(review, "visual_highlights")
    assert len(shot_flag) == 1
    assert shot_flag[0]["upstream_kind"] == "shots"


async def test_shot_edit_without_highlights_creates_no_flag(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    record = api._runtime.store.get_required(pid)
    record.state.visual_highlights = None
    api._runtime.store.save_state(pid, record.state, record.revision,
                                  source="manual", summary="移除亮点")

    r = await patch_card(c, pid, "shots", "shot_01", {"duration_seconds": 5}, 3)
    assert r.status_code == 200 and r.json()["changed"] is True
    # Missing downstream: no flags, no fabricated objects.
    assert r.json()["review"].get("review_flags", []) == []


async def test_character_edit_without_downstream_creates_no_flag(client):
    api, c = client
    p = await create_project(c, "只有角色", "裸项目")
    pid = p["project_id"]
    record = api._runtime.store.get_required(pid)
    record.state.characters = [Character(id="char_a", name="阿芸")]
    api._runtime.store.save_state(pid, record.state, record.revision,
                                  source="manual", summary="只放一个角色")

    r = await patch_card(c, pid, "characters", "char_a", {"name": "阿云"}, 2)
    assert r.status_code == 200
    assert r.json()["review"].get("review_flags", []) == []
    assert r.json()["revision"] == 3


async def test_review_merge_preserves_other_flags_and_refreshes_retriggered(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    store = api._runtime.store
    record = store.get_required(pid)
    record.state.visual_highlights = None  # keep highlights out of the way
    store.save_state(pid, record.state, record.revision,
                     source="manual", summary="简化下游")
    # A pre-existing unrelated flag (as a future ticket could leave behind).
    seeded_review = {
        "review_flags": [{
            "artifact": "script", "reason": "candidate_rejected",
            "detail": "人工注入的既有标记",
        }],
    }
    record = store.get_required(pid)
    store.save_state(pid, record.state, record.revision, source="manual",
                     summary="注入既有标记",
                     review=seeded_review)

    r = await patch_card(c, pid, "scenes", "scene_a", {"mood": "更紧张"}, 4)
    assert r.status_code == 200
    flags = r.json()["review"]["review_flags"]
    by_key = {(f["artifact"], f.get("reason")): f for f in flags}
    assert by_key[("script", "candidate_rejected")]["detail"] == "人工注入的既有标记"
    new_flag = by_key[("script", "upstream_manual_edit")]
    assert new_flag["upstream_kind"] == "scenes"

    # Re-triggering the same artifact+reason refreshes instead of stacking.
    r = await patch_card(c, pid, "characters", "char_a", {"name": "阿云"}, 5)
    flags = r.json()["review"]["review_flags"]
    manual_script = [
        f for f in flags
        if f.get("reason") == "upstream_manual_edit" and f.get("artifact") == "script"
    ]
    assert len(manual_script) == 1
    assert manual_script[0]["upstream_kind"] == "characters"
    assert manual_script[0]["since_revision"] == 6
    # The original flag's created_at is preserved on refresh.
    first_created = by_key[("script", "upstream_manual_edit")]["created_at"]
    assert manual_script[0]["created_at"] == first_created


# ── Coexistence with generation runs (#14/#15 behaviour) ───


async def test_manual_save_wins_over_late_generation_result(client):
    api, c = client
    p = await seed_card_project(c, api)
    pid = p["project_id"]
    store = api._runtime.store
    record = store.get_required(pid)

    # An active generation run anchored at the current revision.
    run = store.create_generation_run(
        pid, kind="generate", request_key="race-1",
        request={"user_input": "卡片编辑验收"}, base_revision=record.revision,
        base_state_json=record.state_json, checkpoint_json=None,
    )

    # Manual editing is never blocked by a running generation.
    r = await patch_card(c, pid, "characters", "char_a", {"name": "手工优先"}, 2)
    assert r.status_code == 200, r.text
    saved_revision = r.json()["revision"]

    # The run's late stage result is anchored at the pre-edit content: the
    # existing CAS refuses it, so the manual save is never overwritten.
    late_state = full_card_state("卡片编辑验收")
    late_state.characters[0].name = "生成结果覆盖"
    with pytest.raises(RevisionConflictError):
        store.commit_generation_stage(
            run.run_id, state=late_state, base_revision=record.revision,
            base_state_json=record.state_json, step="character_design",
            summary="迟到的阶段结果",
            progress={"stage": "character_design", "message": "late"},
        )
    final = store.get_required(pid)
    assert final.revision == saved_revision
    assert final.state.characters[0].name == "手工优先"


# ── Direct apply-level contract (fast checks) ──────────────


def test_apply_rejects_changes_that_are_not_a_mapping():
    from script_weaver.core.card_edit import InvalidCardChangeError, apply_card_changes

    state = full_card_state("x")
    with pytest.raises(InvalidCardChangeError):
        apply_card_changes(state, "characters", "char_a", ["not", "a", "dict"])


def test_apply_empty_changes_is_a_valid_no_op():
    from script_weaver.core.card_edit import apply_card_changes

    state = full_card_state("x")
    outcome = apply_card_changes(state, "characters", "char_a", {})
    assert outcome.changed is False


def test_request_model_requires_expected_revision(api_factory):
    api = api_factory()
    with pytest.raises(ValidationError):
        api.CardEditRequest.model_validate({"changes": {}})


async def test_unknown_top_level_body_fields_are_rejected(client):
    api, c = client
    p = await seed_card_project(c, api)
    baseline = await write_facts(api, p["project_id"])

    r = await c.patch(
        patch_url(p["project_id"], "characters", "char_a"),
        json={"changes": {}, "expected_revision": 2, "force": True},
    )
    assert r.status_code == 422
    await assert_zero_writes(api, c, p["project_id"], baseline)
