"""Confirm-along review flags + warned exports (ticket #17).

Store transactions, pure card_edit contracts and the API surface: a confirm
never rewrites content, removes exactly the selections it matched (full
identity — never the artifact name alone), and every refusal is a zero
write. Exports report the review facts of the very snapshot they serve via
response headers. No model keys and no model calls exist on any of these
paths.
"""

import io
import json
import sqlite3
import zipfile

import httpx
import pytest

from script_weaver.core.card_edit import (
    InvalidReviewSelectionError,
    ReviewSelectionMismatchError,
    confirm_review_flags,
    summarize_review_warnings,
    validate_review_selections,
)
from script_weaver.core.project_store import (
    ProjectNotFoundError,
    ProjectStore,
    RevisionConflictError,
    content_signature,
)
from script_weaver.core.types import (
    BasicInfo,
    Character,
    Outline,
    ProjectState,
    ProjectStatus,
    Script,
    ScriptBlock,
    ScriptBlockType,
    ScriptScene,
    ScriptSceneHeading,
    Shot,
    Storyboard,
    VisualHighlight,
)

# ── Seeding ────────────────────────────────────────────────


def reviewable_state(user_input: str) -> ProjectState:
    """Script + storyboard + highlights: every artifact a flag can target."""
    state = ProjectState(user_input=user_input)
    state.outline = Outline(basic_info=BasicInfo(logline="复核验收"))
    state.meta.status = ProjectStatus.COMPLETE
    state.characters = [
        Character(id="char_a", name="阿芸", role="protagonist",
                  appearance="二十岁出头的守灯人", personality="倔强",
                  motivation="让灯不灭", relationship_map={"陈叔": "师徒"}),
        Character(id="char_b", name="陈叔", role="supporting", motivation="帮阿芸"),
    ]
    state.script = Script(
        title="夜行灯塔",
        scenes=[ScriptScene(
            heading=ScriptSceneHeading(location="灯塔顶层", time_of_day="夜"),
            blocks=[ScriptBlock(block_type=ScriptBlockType.ACTION,
                                content={"description": "阿芸握紧马灯。"})],
        )],
        total_estimated_duration=4,
    )
    state.storyboard = Storyboard(shots=[
        Shot(shot_id="shot_a", scene_id="sc_1", sequence_number=1,
             shot_size="medium_shot", camera_angle="eye_level",
             camera_movement="static", visual_description="灯塔外景",
             duration_seconds=2, transition_to_next="cut",
             video_prompt="镜头缓推"),
        Shot(shot_id="shot_b", scene_id="sc_1", sequence_number=2,
             shot_size="close_up", camera_angle="eye_level",
             camera_movement="static", visual_description="阿芸特写",
             duration_seconds=2, transition_to_next="cut"),
    ])
    state.storyboard.compute_totals()
    state.visual_highlights = [
        VisualHighlight(id="vh_a", title="灯不灭", related_shot_ids=["shot_a"]),
    ]
    return state


def make_selection(flag: dict) -> dict:
    """The request shape for confirming one stored flag."""
    return {
        "artifact": flag["artifact"],
        "reason": flag["reason"],
        "upstream_kind": flag["upstream_kind"],
        "upstream_id": flag["upstream_id"],
        "since_revision": flag["since_revision"],
    }


def seeded_project_with_flags(store: ProjectStore, *, review_extra: dict | None = None):
    """A project whose manual character edit flagged all three artifacts."""
    p = store.create_project(user_input="复核验收", title="复核项目")
    record = store.get_required(p.project_id)
    state = reviewable_state("复核验收")
    review = {"review_flags": [], **(review_extra or {})}
    store.save_state(p.project_id, state, record.revision,
                     source="manual", summary="复核种子", review=review)
    saved, changed = store.save_card_edit(
        p.project_id, kind="characters", target_id="char_a",
        changes={"name": "阿云"}, expected_revision=record.revision + 1,
    )
    assert changed and len(saved.review["review_flags"]) == 3
    return p, saved


# ── Store transaction ──────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = ProjectStore(tmp_path / "projects.sqlite3")
    yield s
    s.close()


def test_confirm_removes_only_selected_flags_and_keeps_content(store):
    p, saved = seeded_project_with_flags(
        store, review_extra={"last_seen_revision": 2})
    before_signature = content_signature(saved.state_json)
    script_flag = next(f for f in saved.review["review_flags"]
                       if f["artifact"] == "script")

    record, confirmed = store.confirm_review(
        p.project_id, expected_revision=saved.revision,
        selections=[make_selection(script_flag)],
    )

    # Only the selected flag is gone; siblings and foreign metadata stay.
    assert [f["artifact"] for f in confirmed] == ["script"]
    remaining = [f["artifact"] for f in record.review["review_flags"]]
    assert remaining == ["storyboard", "visual_highlights"]
    assert record.review["last_seen_revision"] == 2
    assert record.revision == saved.revision + 1
    # The artifacts themselves are untouched — only meta timestamps may move.
    assert content_signature(record.state_json) == before_signature
    # One new version records the confirmation.
    versions = store.list_versions(p.project_id)
    assert [v.revision for v in versions] == [record.revision, saved.revision, 2, 1]
    assert "确认沿用" in versions[0].summary
    assert "剧本" in versions[0].summary


def test_confirm_rejects_stale_revision_with_zero_writes(store):
    p, saved = seeded_project_with_flags(store)
    flag = saved.review["review_flags"][0]

    with pytest.raises(RevisionConflictError) as exc_info:
        store.confirm_review(
            p.project_id, expected_revision=saved.revision - 1,
            selections=[make_selection(flag)],
        )
    assert exc_info.value.current_revision == saved.revision

    current = store.get_required(p.project_id)
    assert current.revision == saved.revision
    assert current.review == saved.review
    assert [v.revision for v in store.list_versions(p.project_id)] == [saved.revision, 2, 1]


def test_confirm_rejects_selections_that_match_no_current_flag(store):
    p, saved = seeded_project_with_flags(store)
    baseline_revision = saved.revision
    baseline_review = saved.review

    script_flag = next(f for f in saved.review["review_flags"]
                       if f["artifact"] == "script")
    wrong_upstream = make_selection(script_flag) | {"upstream_id": "char_nobody"}
    wrong_since = make_selection(script_flag) | {"since_revision": 99}
    foreign = {"artifact": "script", "reason": "upstream_manual_edit",
               "upstream_kind": "scenes", "upstream_id": "scene_x",
               "since_revision": 2}

    for bad in (wrong_upstream, wrong_since, foreign):
        with pytest.raises(ReviewSelectionMismatchError):
            store.confirm_review(
                p.project_id, expected_revision=baseline_revision,
                selections=[bad],
            )

    current = store.get_required(p.project_id)
    assert current.revision == baseline_revision
    assert current.review == baseline_review


def test_upstream_retrigger_invalidates_the_old_confirmation(store):
    """A re-trigger refreshes the flag's since_revision; the old basis fails."""
    p, saved = seeded_project_with_flags(store)
    old_flag = next(f for f in saved.review["review_flags"]
                    if f["artifact"] == "script")
    assert old_flag["since_revision"] == saved.revision

    # char_b edits again: same (artifact, reason) flag, new since_revision.
    retriggered, _ = store.save_card_edit(
        p.project_id, kind="characters", target_id="char_b",
        changes={"name": "陈叔改"}, expected_revision=saved.revision,
    )
    fresh_flag = next(f for f in retriggered.review["review_flags"]
                      if f["artifact"] == "script")
    assert fresh_flag["since_revision"] == retriggered.revision

    # The stale confirm (pre-retrigger basis) cannot clear the NEW flag.
    with pytest.raises(ReviewSelectionMismatchError):
        store.confirm_review(
            p.project_id, expected_revision=retriggered.revision,
            selections=[make_selection(old_flag)],
        )
    current = store.get_required(p.project_id)
    assert current.revision == retriggered.revision
    assert len(current.review["review_flags"]) == 3

    # The current basis confirms cleanly.
    record, confirmed = store.confirm_review(
        p.project_id, expected_revision=retriggered.revision,
        selections=[make_selection(fresh_flag)],
    )
    assert [f["artifact"] for f in confirmed] == ["script"]
    assert [f["artifact"] for f in record.review["review_flags"]] == [
        "storyboard", "visual_highlights"]


def test_confirmed_flags_cannot_be_confirmed_again(store):
    """A repeated old request answers mismatch and never births a version."""
    p, saved = seeded_project_with_flags(store)
    selections = [make_selection(f) for f in saved.review["review_flags"]]

    record, confirmed = store.confirm_review(
        p.project_id, expected_revision=saved.revision, selections=selections,
    )
    assert record.review["review_flags"] == [] and len(confirmed) == 3
    settled_revision = record.revision

    with pytest.raises(ReviewSelectionMismatchError):
        store.confirm_review(
            p.project_id, expected_revision=settled_revision,
            selections=selections,
        )
    current = store.get_required(p.project_id)
    assert current.revision == settled_revision
    assert current.review["review_flags"] == []
    assert len(store.list_versions(p.project_id)) == 4


def test_duplicate_store_selections_refuse_and_roll_back(store):
    """One selection consumes the flag; the identical second one mismatches,
    and the whole transaction rolls back instead of half-clearing."""
    p, saved = seeded_project_with_flags(store)
    flag = make_selection(
        next(f for f in saved.review["review_flags"]
             if f["artifact"] == "script"))

    with pytest.raises(ReviewSelectionMismatchError):
        store.confirm_review(
            p.project_id, expected_revision=saved.revision,
            selections=[flag, dict(flag)],
        )
    current = store.get_required(p.project_id)
    assert current.revision == saved.revision
    assert len(current.review["review_flags"]) == 3


def test_failed_commit_rolls_back_confirmation(tmp_path, monkeypatch):
    """A rejected COMMIT leaves flags, content and history untouched."""
    db = tmp_path / "projects.sqlite3"
    store = ProjectStore(db)
    p, saved = seeded_project_with_flags(store)
    selections = [make_selection(f) for f in saved.review["review_flags"][:1]]

    class _CommitFailProxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("simulated COMMIT failure")
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "_conn", _CommitFailProxy(store._conn))
    with pytest.raises(sqlite3.OperationalError, match="COMMIT"):
        store.confirm_review(
            p.project_id, expected_revision=saved.revision, selections=selections,
        )
    monkeypatch.undo()

    assert not store._conn.in_transaction
    current = store.get_required(p.project_id)
    assert current.revision == saved.revision
    assert len(current.review["review_flags"]) == 3
    assert [v.revision for v in store.list_versions(p.project_id)] == [saved.revision, 2, 1]

    # Usable again once the fault clears.
    record, confirmed = store.confirm_review(
        p.project_id, expected_revision=saved.revision, selections=selections,
    )
    assert len(confirmed) == 1 and record.revision == saved.revision + 1
    store.close()


def test_confirmation_facts_survive_reopen(tmp_path):
    """Refresh / restart reads the same review facts (cleared flags stay
    cleared; the confirming version stays in history)."""
    db = tmp_path / "projects.sqlite3"
    store = ProjectStore(db)
    p, saved = seeded_project_with_flags(store)
    selections = [make_selection(f) for f in saved.review["review_flags"]]
    record, _ = store.confirm_review(
        p.project_id, expected_revision=saved.revision, selections=selections,
    )
    settled = (record.revision, record.review)
    store.close()

    reopened = ProjectStore(db)
    current = reopened.get_required(p.project_id)
    assert (current.revision, current.review) == settled
    assert [v.revision for v in reopened.list_versions(p.project_id)] == [4, 3, 2, 1]
    assert "确认沿用" in reopened.list_versions(p.project_id)[0].summary
    reopened.close()


def test_confirm_unknown_project_404(store):
    with pytest.raises(ProjectNotFoundError):
        store.confirm_review(
            "nope", expected_revision=1,
            selections=[{"artifact": "script", "reason": "upstream_manual_edit",
                         "upstream_kind": "characters", "upstream_id": "x",
                         "since_revision": 1}],
        )


# ── Pure contracts (card_edit) ─────────────────────────────


def test_validate_review_selections_accepts_and_rejects():
    good = [{"artifact": "script", "reason": "upstream_manual_edit",
             "upstream_kind": "characters", "upstream_id": "char_a",
             "since_revision": 3}]
    keys = validate_review_selections(good)
    assert keys and all(isinstance(k, tuple) for k in keys)

    dup = good + [dict(good[0])]
    with pytest.raises(InvalidReviewSelectionError, match="重复"):
        validate_review_selections(dup)

    with pytest.raises(InvalidReviewSelectionError, match="产物非法"):
        validate_review_selections([good[0] | {"artifact": "characters"}])
    with pytest.raises(InvalidReviewSelectionError):
        validate_review_selections([])
    with pytest.raises(InvalidReviewSelectionError):
        validate_review_selections([good[0] | {"since_revision": 0}])
    # bool is not a revision either.
    with pytest.raises(InvalidReviewSelectionError):
        validate_review_selections([good[0] | {"since_revision": True}])
    with pytest.raises(InvalidReviewSelectionError):
        validate_review_selections([good[0] | {"upstream_id": ""}])


def test_confirm_review_flags_is_exact_and_preserving():
    from script_weaver.core.card_edit import merge_review_flags
    from script_weaver.core.project_store import serialize_state

    state = reviewable_state("x")
    review = merge_review_flags(
        {"review_flags": [], "note": "keep me"},
        [{"artifact": "script", "reason": "upstream_manual_edit",
          "upstream_kind": "characters", "upstream_id": "char_a",
          "upstream_label": "阿芸", "since_revision": 3,
          "created_at": "t1", "updated_at": "t1"},
         {"artifact": "storyboard", "reason": "upstream_manual_edit",
          "upstream_kind": "characters", "upstream_id": "char_a",
          "upstream_label": "阿芸", "since_revision": 3,
          "created_at": "t1", "updated_at": "t1"}],
    )
    flags = review["review_flags"]
    state_json_before = serialize_state(state)
    new_review, confirmed = confirm_review_flags(
        review, [make_selection(flags[0])], state)
    assert [f["artifact"] for f in confirmed] == ["script"]
    assert confirmed[0]["upstream_label"] == "阿芸"  # the stored flag, not the selection
    assert [f["artifact"] for f in new_review["review_flags"]] == ["storyboard"]
    assert new_review["note"] == "keep me"
    # Pure: the input dict's flag list is untouched...
    assert len(review["review_flags"]) == 2
    # ...and the state was never mutated.
    assert serialize_state(state) == state_json_before


def test_confirm_review_flags_missing_downstream_is_a_mismatch():
    state = reviewable_state("x")
    state.script = None  # the flagged artifact vanished
    review = {"review_flags": [{
        "artifact": "script", "reason": "upstream_manual_edit",
        "upstream_kind": "characters", "upstream_id": "char_a",
        "upstream_label": "阿芸", "since_revision": 3,
        "created_at": "t1", "updated_at": "t1",
    }]}
    with pytest.raises(ReviewSelectionMismatchError, match="不存在"):
        confirm_review_flags(review, [make_selection(review["review_flags"][0])], state)


def test_summarize_review_warnings_shape_and_merging():
    assert summarize_review_warnings(None) == []
    assert summarize_review_warnings({}) == []
    assert summarize_review_warnings({"review_flags": []}) == []

    review = {"review_flags": [
        {"artifact": "storyboard", "reason": "upstream_manual_edit",
         "since_revision": 5, "upstream_label": "陈叔"},
        {"artifact": "script", "reason": "upstream_manual_edit",
         "since_revision": 3, "upstream_label": "阿芸"},
        # Corrupt / unknown shapes never leak into the header.
        "not-a-dict",
        {"artifact": "characters", "reason": "upstream_manual_edit",
         "since_revision": 2},
        {"artifact": "script", "reason": "", "since_revision": 1},
        {"artifact": "script", "reason": "upstream_manual_edit",
         "since_revision": "3"},
    ]}
    warnings = summarize_review_warnings(review)
    assert warnings == [
        {"artifact": "script", "reason": "upstream_manual_edit", "since_revision": 3},
        {"artifact": "storyboard", "reason": "upstream_manual_edit", "since_revision": 5},
    ]
    # Labels and free text stay out of the compact form.
    assert all("阿芸" not in json.dumps(w, ensure_ascii=True) for w in warnings)


# ── API surface ────────────────────────────────────────────


@pytest.fixture
async def client(api_factory):
    api = api_factory()
    async with api.app.router.lifespan_context(api.app):
        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield api, c


async def _seed_flagged(c: httpx.AsyncClient, api, title="复核接口"):
    p = (await c.post("/api/projects", json={
        "user_input": "复核验收", "title": title})).json()
    record = api._runtime.store.get_required(p["project_id"])
    api._runtime.store.save_state(
        p["project_id"], reviewable_state("复核验收"), record.revision,
        source="manual", summary="复核种子",
    )
    r = await c.patch(
        f"/api/projects/{p['project_id']}/artifacts/characters/char_a",
        json={"changes": {"name": "阿云"}, "expected_revision": record.revision + 1},
    )
    assert r.status_code == 200, r.text
    return p, r.json()


async def test_confirm_endpoint_roundtrip(client):
    api, c = client
    p, saved = await _seed_flagged(c, api)
    pid = p["project_id"]
    script_flag = next(f for f in saved["review"]["review_flags"]
                       if f["artifact"] == "script")

    r = await c.post(f"/api/projects/{pid}/review/confirm", json={
        "expected_revision": saved["revision"],
        "selections": [make_selection(script_flag)],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == pid and body["revision"] == saved["revision"] + 1
    assert [f["artifact"] for f in body["confirmed"]] == ["script"]
    assert [f["artifact"] for f in body["review"]["review_flags"]] == [
        "storyboard", "visual_highlights"]
    # The snapshot is complete (the frontend adopts it wholesale).
    for field in ("characters", "script", "storyboard", "visual_highlights", "meta"):
        assert field in body

    after = (await c.get(f"/api/projects/{pid}")).json()
    assert after["review"] == body["review"] and after["revision"] == body["revision"]
    versions = (await c.get(f"/api/projects/{pid}/versions")).json()["versions"]
    assert "确认沿用" in versions[0]["summary"]


async def test_confirm_endpoint_validation_refusals(client):
    api, c = client
    p, saved = await _seed_flagged(c, api)
    pid = p["project_id"]
    url = f"/api/projects/{pid}/review/confirm"
    baseline_revision = (await c.get(f"/api/projects/{pid}")).json()["revision"]
    flag = make_selection(saved["review"]["review_flags"][0])

    # Empty selection / unknown field / illegal artifact / duplicates / bad
    # revision: all 422, none of them writes anything.
    r = await c.post(url, json={"expected_revision": saved["revision"], "selections": []})
    assert r.status_code == 422
    r = await c.post(url, json={
        "expected_revision": saved["revision"], "selections": [flag],
        "review": {"review_flags": []},
    })
    assert r.status_code == 422
    r = await c.post(url, json={
        "expected_revision": saved["revision"],
        "selections": [flag | {"artifact": "characters"}],
    })
    assert r.status_code == 422
    r = await c.post(url, json={
        "expected_revision": saved["revision"],
        "selections": [flag, dict(flag)],
    })
    assert r.status_code == 422
    assert "重复" in r.json()["detail"]["message"]
    r = await c.post(url, json={
        "expected_revision": saved["revision"],
        "selections": [flag | {"since_revision": 0}],
    })
    assert r.status_code == 422

    after = (await c.get(f"/api/projects/{pid}")).json()
    assert after["revision"] == baseline_revision
    assert len(after["review"]["review_flags"]) == 3


async def test_confirm_endpoint_conflict_semantics(client):
    api, c = client
    p, saved = await _seed_flagged(c, api)
    pid = p["project_id"]
    url = f"/api/projects/{pid}/review/confirm"
    flag = make_selection(saved["review"]["review_flags"][0])

    # Stale revision → 409 with the current revision, nothing written.
    r = await c.post(url, json={
        "expected_revision": saved["revision"] - 1, "selections": [flag]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "revision_conflict"
    assert detail["current_revision"] == saved["revision"]

    # A flag of another project (never existed here) → 409 review_changed.
    r = await c.post(url, json={
        "expected_revision": saved["revision"],
        "selections": [{"artifact": "script", "reason": "upstream_manual_edit",
                        "upstream_kind": "scenes", "upstream_id": "scene_elsewhere",
                        "since_revision": 2}]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "review_changed"

    # Unknown project → 404.
    r = await c.post("/api/projects/nope/review/confirm", json={
        "expected_revision": 1, "selections": [flag]})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "project_not_found"

    after = (await c.get(f"/api/projects/{pid}")).json()
    assert after["revision"] == saved["revision"]
    assert len(after["review"]["review_flags"]) == 3


# ── Export warnings headers ────────────────────────────────


def _parse_warnings_header(raw: str) -> dict:
    assert raw.isascii(), f"header must stay ASCII-safe: {raw!r}"
    parsed = json.loads(raw)
    assert set(parsed) == {"review_warnings"}
    for w in parsed["review_warnings"]:
        assert set(w) == {"artifact", "reason", "since_revision"}
    return parsed


async def test_export_headers_report_the_same_snapshot(client):
    api, c = client
    p, saved = await _seed_flagged(c, api)
    pid = p["project_id"]

    for fmt in ("json", "fountain", "video_gen"):
        r = await c.get(f"/api/projects/{pid}/export/{fmt}")
        assert r.status_code == 200, (fmt, r.text)
        assert r.headers["x-project-revision"] == str(saved["revision"])
        warnings = _parse_warnings_header(r.headers["x-review-warnings"])
        assert len(warnings["review_warnings"]) == 3

    # The original file contracts are intact alongside the new headers.
    json_res = await c.get(f"/api/projects/{pid}/export/json")
    from script_weaver.core.types import ProjectState as PS
    state = PS.model_validate_json(json_res.text)
    assert state.script.title == "夜行灯塔"

    fountain = await c.get(f"/api/projects/{pid}/export/fountain")
    assert "阿芸握紧马灯" in fountain.text

    zip_res = await c.get(f"/api/projects/{pid}/export/video_gen")
    with zipfile.ZipFile(io.BytesIO(zip_res.content)) as zf:
        assert zf.testzip() is None
        assert set(zf.namelist()) == {
            "video_gen_shots.json", "video_gen_shots.csv",
            "shots/shot_a.txt", "shots/shot_b.txt",
        }


async def test_export_warnings_track_confirmations(client):
    api, c = client
    p, saved = await _seed_flagged(c, api)
    pid = p["project_id"]
    url = f"/api/projects/{pid}/review/confirm"

    # Project-level and format-independent: all three formats report the
    # same unhandled scope.
    for fmt in ("json", "fountain", "video_gen"):
        r = await c.get(f"/api/projects/{pid}/export/{fmt}")
        parsed = _parse_warnings_header(r.headers["x-review-warnings"])
        assert [w["artifact"] for w in parsed["review_warnings"]] == [
            "script", "storyboard", "visual_highlights"]
        assert all(w["reason"] == "upstream_manual_edit"
                   for w in parsed["review_warnings"])

    flags = saved["review"]["review_flags"]
    partial = [f for f in flags if f["artifact"] in ("script", "storyboard")]
    r = await c.post(url, json={
        "expected_revision": saved["revision"],
        "selections": [make_selection(f) for f in partial]})
    assert r.status_code == 200, r.text

    # Only the unconfirmed scope remains warned, with its triggering revision.
    r = await c.get(f"/api/projects/{pid}/export/json")
    parsed = _parse_warnings_header(r.headers["x-review-warnings"])
    assert parsed["review_warnings"] == [
        {"artifact": "visual_highlights", "reason": "upstream_manual_edit",
         "since_revision": flags[2]["since_revision"]}]

    # Flagless projects answer an empty array — present, never fabricated.
    fresh = (await c.post("/api/projects", json={
        "user_input": "干净项目", "title": "无标记"})).json()
    r = await c.get(f"/api/projects/{fresh['project_id']}/export/json")
    assert r.status_code == 200
    assert _parse_warnings_header(r.headers["x-review-warnings"]) == {"review_warnings": []}


async def test_export_headers_and_file_come_from_one_snapshot(client, monkeypatch):
    """A mid-export modification must never pair old bytes with new warnings
    (or the reverse): both are built from the single ProjectRecord read."""
    api, c = client
    p, saved = await _seed_flagged(c, api)
    pid = p["project_id"]
    real_build = api._build_video_gen_zip

    def build_after_editing(state):
        # Land a real edit WHILE the export is being built.
        record = api._runtime.store.get_required(pid)
        api._runtime.store.save_card_edit(
            pid, kind="characters", target_id="char_b",
            changes={"name": "并发修改"}, expected_revision=record.revision,
        )
        return real_build(state)

    monkeypatch.setattr(api, "_build_video_gen_zip", build_after_editing)
    r = await c.get(f"/api/projects/{pid}/export/video_gen")
    monkeypatch.undo()

    assert r.status_code == 200, r.text
    # The headers describe the snapshot the ZIP was built from — not the
    # revision the concurrent edit produced.
    assert r.headers["x-project-revision"] == str(saved["revision"])
    parsed = _parse_warnings_header(r.headers["x-review-warnings"])
    assert len(parsed["review_warnings"]) == 3
    assert all(w["since_revision"] == saved["revision"]
               for w in parsed["review_warnings"])
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        shots = json.loads(zf.read("video_gen_shots.json").decode("utf-8"))
    assert shots, "ZIP content must be the export-time snapshot's shots"

    current = (await c.get(f"/api/projects/{pid}")).json()
    assert current["revision"] == saved["revision"] + 1


async def test_cors_exposes_the_review_headers(client):
    """Direct cross-origin API consumers must be able to read the facts."""
    api, _ = client
    middleware = {m.cls.__name__: m for m in api.app.user_middleware}
    assert "CORSMiddleware" in middleware
    exposed = middleware["CORSMiddleware"].kwargs["expose_headers"]
    for header in ("Content-Disposition", "X-Project-Revision", "X-Review-Warnings"):
        assert header in exposed
