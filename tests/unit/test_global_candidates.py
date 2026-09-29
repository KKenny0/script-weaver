"""Whole-artifact boundary and shared adoption transactions, without models."""
import asyncio
import pytest
from script_weaver.core.global_revision import validate_global_result
from script_weaver.core.project_store import ProjectStoreError, RevisionConflictError, hash_run_request
from test_card_candidates import store, project


def admit(store, record, key="global"):
    request = {"kind": "global", "target_id": "", "instruction": "修改", "expected_revision": record.revision}
    return store.admit_generation_run(record.project_id, kind="global_candidate", request_key=key,
        request=request, request_hash=hash_run_request(request), base_revision=record.revision,
        base_state_json=record.state_json, checkpoint_json=None)[0]


def proposed(record, artifact="characters"):
    state = record.state.model_copy(deep=True)
    if artifact == "characters": state.characters[0].personality = "新性格"
    elif artifact == "scenes": state.scenes[0].mood = "新气氛"
    elif artifact == "script": state.script.title = "新剧本名"
    else: state.storyboard.shots[0].visual_description = "新画面"
    return state


@pytest.mark.parametrize("artifact", ["characters", "scenes", "script", "storyboard"])
def test_global_whole_candidate_adoption_and_repeat(store, artifact):
    record = project(store)
    run = admit(store, record)
    candidate = store.complete_card_candidate(run.run_id, proposed(record, artifact))
    assert candidate["kind"] == "global" and candidate["target_id"] == artifact
    assert candidate["diff"] and candidate["affected_artifacts"]
    assert store.get_required(record.project_id).state_json == record.state_json
    assert len(store.list_versions(record.project_id)) == 2
    assert store.latest_generation_run(record.project_id) is None
    adopted, saved = store.decide_card_candidate(record.project_id, candidate["id"], accept=True)
    assert saved.state.model_dump(mode="json")[artifact] == candidate["proposed"][artifact]
    assert {f["artifact"] for f in saved.review["review_flags"]} == set(candidate["affected_artifacts"])
    store.rename_project(record.project_id, "later", expected_revision=saved.revision)
    again, original = store.decide_card_candidate(record.project_id, candidate["id"], accept=True)
    assert again == adopted and original.state_json == saved.state_json


@pytest.mark.parametrize("mutation", [
    lambda s: s.characters.reverse(),
    lambda s: s.characters.append(s.characters[0].model_copy(deep=True)),
    lambda s: s.characters.pop(),
    lambda s: setattr(s.characters[0], "image_reference_url", "https://new"),
    lambda s: setattr(s.characters[0], "id", "new"),
    lambda s: setattr(s.scenes[0], "mood", "another artifact"),
    lambda s: setattr(s, "user_input", "new input"),
    lambda s: setattr(s.script.scenes[0], "scene_design_id", "missing"),
])
def test_invalid_output_writes_nothing(store, mutation):
    record = project(store); run = admit(store, record)
    output = proposed(record); mutation(output)
    with pytest.raises(Exception): store.complete_card_candidate(run.run_id, output)
    assert store.get_required(record.project_id).state_json == record.state_json
    assert store.list_card_candidates(record.project_id) == []


@pytest.mark.parametrize("mutation", [
    lambda s: s.script.scenes.reverse(),
    lambda s: s.script.scenes.append(s.script.scenes[0].model_copy(deep=True)),
    lambda s: setattr(s.script.scenes[0], "characters_involved", ["new"]),
    lambda s: setattr(s.script.scenes[0], "estimated_duration_seconds", float("nan")),
    lambda s: setattr(s.storyboard.shots[0], "scene_id", "new"),
    lambda s: setattr(s.storyboard.shots[0], "reference_image_url", "https://new"),
    lambda s: s.storyboard.shots.reverse(),
])
def test_script_and_shot_structure_readonly(store, mutation):
    record = project(store); output = record.state.model_copy(deep=True)
    mutation(output)
    with pytest.raises(Exception): validate_global_result(record.state, output)


def test_global_stale_crossproject_reject_and_rollback(store, monkeypatch):
    record = project(store); candidate = store.complete_card_candidate(admit(store, record).run_id, proposed(record))
    other = project(store)
    with pytest.raises(ProjectStoreError): store.decide_card_candidate(other.project_id, candidate["id"], accept=True)
    original = store._write_new_state
    def fail(*args):
        original(*args)
        raise RuntimeError("disk")
    monkeypatch.setattr(store, "_write_new_state", fail)
    with pytest.raises(RuntimeError): store.decide_card_candidate(record.project_id, candidate["id"], accept=True)
    assert store.get_required(record.project_id).state_json == record.state_json
    assert store.list_card_candidates(record.project_id)[0]["status"] == "ready"
    monkeypatch.setattr(store, "_write_new_state", original)
    store.rename_project(record.project_id, "new", expected_revision=record.revision)
    with pytest.raises(RevisionConflictError): store.decide_card_candidate(record.project_id, candidate["id"], accept=True)
    assert store.decide_card_candidate(record.project_id, candidate["id"], accept=False)[0]["status"] == "rejected"


async def test_global_api_exports_stop_restart_and_idempotence(api_factory, monkeypatch):
    import httpx
    from types import SimpleNamespace
    api = api_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    async def refine(state, message, **kwargs):
        assert kwargs["global_candidate"]
        entered.set(); await release.wait()
        state.characters[0].personality = "全局新性格"
        return state
    async def engine(*args, **kwargs): return SimpleNamespace(refine=refine), None
    monkeypatch.setattr(api, "_get_engine", engine)
    async with api.app.router.lifespan_context(api.app):
        record = project(api._store())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as c:
            base = f"/api/projects/{record.project_id}"
            import io, zipfile, json
            def payload(response, fmt):
                assert response.status_code == 200
                if fmt == "video_gen":
                    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                        return {name: archive.read(name) for name in archive.namelist()}
                return response.content
            exports = {fmt: payload(await c.get(base + f"/export/{fmt}"), fmt) for fmt in ("json", "fountain", "video_gen")}
            body = {"message": "修改", "expected_revision": record.revision, "request_key": "global-api"}
            response = await c.post(base + "/refine", json=body)
            assert response.status_code == 200, response.text
            run_id = response.json()["run"]["run_id"]
            await entered.wait()
            assert (await c.post(base + "/refine", json=body)).json()["created"] is False
            assert (await c.get(base + "/runs/latest")).status_code == 404
            release.set(); await api._runs()._tasks[run_id]
            candidate = (await c.get(base + "/candidates")).json()["candidates"][0]
            for fmt, content in exports.items(): assert payload(await c.get(base + f"/export/{fmt}"), fmt) == content
            assert (await c.post(base + f"/runs/{run_id}/resume", json={})).status_code == 409
            response = await c.post(base + f"/candidates/{candidate['id']}/accept")
            assert response.status_code == 200
            for fmt in exports:
                exported = await c.get(base + f"/export/{fmt}")
                assert exported.status_code == 200
                assert exported.headers["X-Project-Revision"] == str(record.revision + 1)
                assert json.loads(exported.headers["X-Review-Warnings"])["review_warnings"]
                if fmt == "json": assert exported.json()["characters"][0]["personality"] == "全局新性格"
            # A stopped candidate run leaves adopted content intact and is discoverable.
            release.clear(); entered.clear()
            body.update(expected_revision=response.json()["revision"], request_key="stopped")
            stopped = (await c.post(base + "/refine", json=body)).json()["run"]["run_id"]
            await entered.wait()
            await c.post(base + f"/runs/{stopped}/stop")
            assert (await c.get(base + f"/runs/{stopped}")).json()["status"] == "cancelled"
            # Shutdown owns interruption, independently of the HTTP request.
            entered.clear(); body["request_key"] = "interrupted"
            interrupted = (await c.post(base + "/refine", json=body)).json()["run"]["run_id"]
            await entered.wait()
    async with api.app.router.lifespan_context(api.app):
        assert api._store().get_generation_run_required(interrupted).status == "interrupted"
        assert api._store().list_card_candidates(record.project_id)[0]["status"] == "accepted"
        assert api._store().get_required(record.project_id).revision == record.revision + 1


def test_complete_diff_serializes_list_additions_without_truncation(store):
    import json
    record = project(store)
    output = proposed(record)
    output.characters[0].key_props.append("新道具")
    candidate = store.complete_card_candidate(admit(store, record).run_id, output)
    json.dumps(candidate)
    change = next(c for c in candidate["diff"] if "key_props" in c["path"])
    assert change["before_exists"] is False and change["after"] == "新道具"


@pytest.mark.parametrize("agent,empty", [("structurer", False), ("art_director", False), ("scriptwriter", True), ("storyboard_artist", True)])
async def test_web_route_refuses_unsupported_or_empty_before_execution(api_factory, agent, empty):
    import httpx
    from test_project_api import real_engine, ScriptedLLM, submit_refine
    api = api_factory()
    async with api.app.router.lifespan_context(api.app):
        record = project(api._store())
        if empty:
            state = record.state.model_copy(deep=True)
            (state.script.scenes if agent == "scriptwriter" else state.storyboard.shots).clear()
            record = api._store().save_state(record.project_id, state, record.revision, source="test")
        llm = ScriptedLLM({"action": "execute_agent", "next_agent": agent, "constraint": "general"})
        real_engine(api, llm)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as c:
            outcome = await submit_refine(c, api, record.project_id, json={"message": "修改"})
            assert outcome.json()["status"] == "failed"
            assert llm.tool_submissions == 0
            assert api._store().get_required(record.project_id).state_json == record.state_json


@pytest.mark.parametrize("mode", ["unchanged", "wrong_type", "outline"])
def test_noop_wrong_type_and_unsupported_fail_without_candidate(store, mode):
    record = project(store); output = record.state.model_copy(deep=True)
    if mode == "wrong_type": output = {"script": "wrong"}
    if mode == "outline": output.outline.basic_info.logline = "new"
    with pytest.raises(Exception): store.complete_card_candidate(admit(store, record).run_id, output)
    assert store.list_card_candidates(record.project_id) == []
    assert store.get_required(record.project_id).state_json == record.state_json


def test_concurrent_accept_once_and_old_card_compatibility(store):
    from concurrent.futures import ThreadPoolExecutor
    from test_card_candidates import admit as card_admit, output
    record = project(store)
    card = store.complete_card_candidate(card_admit(store, record, key="card").run_id, output())
    candidate = store.complete_card_candidate(admit(store, record).run_id, proposed(record))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.decide_card_candidate, record.project_id, candidate["id"], accept=True) for _ in range(2)]
        results = [f.result() for f in futures]
    assert results[0][1].revision == results[1][1].revision == record.revision + 1
    assert len(store.list_versions(record.project_id)) == 3
    rows = {row["id"]: row for row in store.list_card_candidates(record.project_id)}
    assert rows[card["id"]]["kind"] == "characters" and rows[card["id"]]["status"] == "stale"
    assert rows[candidate["id"]]["status"] == "accepted"
