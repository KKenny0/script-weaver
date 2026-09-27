"""Candidate runs use a fake LLM; no provider or network is constructed."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from script_weaver.agents.card_revision import CardRevisionAgent
from script_weaver.core.project_store import ProjectStore, RevisionConflictError, hash_run_request
from script_weaver.llm.providers import ChatResponse, ToolCall
from test_card_edit import full_card_state


@pytest.fixture(autouse=True)
def forbid_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real model construction forbidden")
    monkeypatch.setattr("script_weaver.agents.base.LLMClient", forbidden)


@pytest.fixture
def store(tmp_path):
    store = ProjectStore(tmp_path / "projects.sqlite3")
    yield store
    store.close()


def project(store):
    record = store.create_project(user_input="灯塔")
    return store.save_state(record.project_id, full_card_state("灯塔"), expected_revision=record.revision, source="test")


def admit(store, record, kind="characters", target="char_a", key="one"):
    request = {"kind": kind, "target_id": target, "instruction": "改好", "expected_revision": record.revision}
    return store.admit_generation_run(record.project_id, kind="card_candidate", request_key=key,
        request=request, request_hash=hash_run_request(request), base_revision=record.revision,
        base_state_json=record.state_json, checkpoint_json=None)[0]


def output(kind="characters", target="char_a", changes=None):
    return {"kind": kind, "target_id": target, "changes": changes or {"name": "新名字"}}


@pytest.mark.parametrize("kind,target,changes", [("characters", "char_a", {"name": "新名字"}), ("scenes", "scene_a", {"mood": "舒缓"}), ("shots", "shot_1", {"duration_seconds": 8})])
def test_complete_accept_history_and_idempotence(store, kind, target, changes):
    r = project(store)
    if kind == "shots": target = r.state.storyboard.shots[0].shot_id
    run = admit(store, r, kind, target)
    candidate = store.complete_card_candidate(run.run_id, output(kind, target, changes))
    assert store.get_required(r.project_id).state_json == r.state_json
    assert store.get_generation_run_required(run.run_id).status == "succeeded"
    assert store.latest_generation_run(r.project_id) is None
    adopted, record = store.decide_card_candidate(r.project_id, candidate["id"], accept=True)
    assert adopted["accepted_revision"] == r.revision + 1
    assert record.review["review_flags"]
    assert len(store.list_versions(r.project_id)) == 3
    store.rename_project(r.project_id, "后来", expected_revision=record.revision)
    repeated, original = store.decide_card_candidate(r.project_id, candidate["id"], accept=True)
    assert repeated == adopted
    assert original.revision == record.revision
    assert original.state_json == record.state_json
    assert original.review == record.review
    assert len(store.list_versions(r.project_id)) == 4


def test_concurrent_edits_stale_reject_ownership_and_restart(store):
    r = project(store); run = admit(store, r)
    edited, _ = store.save_card_edit(r.project_id, kind="scenes", target_id="scene_a", changes={"name": "新地点"}, expected_revision=r.revision)
    candidate = store.complete_card_candidate(run.run_id, output())
    assert candidate["status"] == "stale"
    with pytest.raises(RevisionConflictError):
        store.decide_card_candidate(r.project_id, candidate["id"], accept=True)
    other = project(store)
    with pytest.raises(Exception, match="此项目"):
        store.decide_card_candidate(other.project_id, candidate["id"], accept=True)
    for _ in range(2):
        assert store.decide_card_candidate(r.project_id, candidate["id"], accept=False)[0]["status"] == "rejected"
    assert store.get_required(r.project_id).state_json == edited.state_json
    assert store.list_card_candidates(r.project_id)[0]["original"]["name"] == "阿芸"
    candidate2 = store.complete_card_candidate(admit(store, edited, key="two").run_id, output())
    store.rename_project(r.project_id, "标题", expected_revision=edited.revision)
    assert store.list_card_candidates(r.project_id)[0]["status"] == "stale"
    with pytest.raises(RevisionConflictError):
        admit(store, edited, key="three")


def test_adoption_rollback(store, monkeypatch):
    r = project(store); candidate = store.complete_card_candidate(admit(store, r).run_id, output())
    write = store._write_new_state
    def fail(*args):
        write(*args)
        raise RuntimeError("disk failure")
    monkeypatch.setattr(store, "_write_new_state", fail)
    with pytest.raises(RuntimeError): store.decide_card_candidate(r.project_id, candidate["id"], accept=True)
    assert store.get_required(r.project_id).state_json == r.state_json
    assert store.list_card_candidates(r.project_id)[0]["status"] == "ready"
    assert len(store.list_versions(r.project_id)) == 2


@pytest.mark.parametrize("bad", [[], {}, {"kind":"characters","target_id":"wrong","changes":{"name":"X"}}, {"kind":"characters","target_id":"char_a","changes":{"id":"char_a"}}, {"kind":"characters","target_id":"char_a","changes":{"name":"阿芸"}}, {"kind":"characters","target_id":"char_a","changes":{"role":"hero"}}, {"kind":"characters","target_id":"char_a","changes":{"name":False}}, {"kind":"characters","target_id":"char_a","changes":{}, "extra": []}])
def test_invalid_output_never_succeeds(store, bad):
    r = project(store); run = admit(store, r)
    with pytest.raises(Exception): store.complete_card_candidate(run.run_id, bad)
    assert store.list_card_candidates(r.project_id) == []
    assert store.get_required(r.project_id).state_json == r.state_json


class FakeLLM:
    def __init__(self, response): self.response = response
    async def chat(self, **kwargs): return self.response


@pytest.mark.parametrize("mode", ["json", "tool", "multi", "truncated", "bad", "empty"])
async def test_agent_protocol(mode):
    payload = output()
    call = ToolCall(name="write_artifact", arguments={"artifact_type": "card_candidate", "content": json.dumps(payload)})
    response = ChatResponse(content=json.dumps(payload))
    if mode == "tool": response.tool_calls = [call]
    if mode == "multi": response.tool_calls = [call, call]
    if mode == "truncated": response.stop_reason = "max_tokens"
    if mode == "bad": response.content = json.dumps([payload, payload])
    if mode == "empty": response.content = ""
    agent = CardRevisionAgent("characters", "char_a", llm_client=FakeLLM(response))
    state = full_card_state("灯塔")
    before = state.model_dump_json()
    if mode in ("multi", "bad"):
        with pytest.raises(Exception): await agent.execute(state, "修改")
    else:
        result = await agent.execute(state, "修改")
        assert (result.get("status") == "success") == (mode in ("json", "tool"))
    assert state.model_dump_json() == before


async def test_run_api_lifecycle(api_factory, monkeypatch):
    api = api_factory()
    entered, release = asyncio.Event(), asyncio.Event()
    async def chat(self, **kwargs):
        entered.set(); await release.wait()
        return ChatResponse(content=json.dumps(output()))
    monkeypatch.setattr("script_weaver.agents.base.LLMClient", lambda: FakeLLM(None))
    monkeypatch.setattr(FakeLLM, "chat", chat)
    async with api.app.router.lifespan_context(api.app):
        r = project(api._store())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as c:
            base = f"/api/projects/{r.project_id}"
            exported = await c.get(base + "/export/json")
            assert exported.status_code == 200
            response = await c.post(base + "/artifacts/characters/char_a/candidates", json={"instruction":"修改", "expected_revision":r.revision, "request_key":"one"})
            assert response.status_code == 200, response.text
            run_id = response.json()["run"]["run_id"]
            await entered.wait()
            assert (await c.get(base + "/export/json")).content == exported.content
            assert (await c.get(base + "/runs/latest")).status_code == 404
            release.set(); await api._runs()._tasks[run_id]
            listing = (await c.get(base + "/candidates")).json()
            assert listing["runs"][0]["status"] == "succeeded"
            assert listing["runs"][0]["next_step"] is None
            candidate = listing["candidates"][0]
            assert (await c.get(base + "/export/json")).content == exported.content
            adopted = await c.post(base + f"/candidates/{candidate['id']}/accept")
            assert adopted.status_code == 200, adopted.text
            assert adopted.json()["revision"] == r.revision + 1
            assert (await c.post(base + f"/runs/{run_id}/resume", json={})).status_code == 409


@pytest.mark.parametrize("failure", ["stop", "timeout", "model", "missing", "restart", "truncated", "invalid", "empty"])
async def test_terminal_failures_preserve_project(api_factory, monkeypatch, failure):
    api = api_factory(); entered = asyncio.Event()
    async def chat(self, **kwargs):
        entered.set()
        if failure == "model": raise RuntimeError("provider failed")
        if failure == "truncated": return ChatResponse(content=json.dumps(output()), stop_reason="max_tokens")
        if failure == "invalid": return ChatResponse(content=json.dumps(output(target="other")))
        if failure == "empty": return ChatResponse(content="")
        await asyncio.Event().wait()
    monkeypatch.setattr(FakeLLM, "chat", chat)
    if failure != "missing": monkeypatch.setattr("script_weaver.agents.base.LLMClient", lambda: FakeLLM(None))
    if failure == "timeout": monkeypatch.setattr(api.get_settings(), "agent_timeout_seconds", .02)
    async with api.app.router.lifespan_context(api.app):
        r = project(api._store())
        run, _ = await api._runs().submit_candidate(r.project_id, "characters", "char_a", api.CandidateRequest(instruction="改", request_key="one", expected_revision=r.revision))
        task = api._runs()._tasks[run.run_id]
        if failure not in ("missing",): await entered.wait()
        if failure == "stop": await api._runs().request_stop(run.run_id)
        if failure == "restart": await api._runs().shutdown()
        await task
        run = api._store().get_generation_run_required(run.run_id)
        assert run.status == {"stop":"cancelled", "restart":"interrupted"}.get(failure, "failed")
        assert api._store().get_required(r.project_id).state_json == r.state_json
        assert api._store().active_generation_run(r.project_id) is None
        assert api._store().list_card_candidates(r.project_id) == []


def test_v2_migration_and_persistence(tmp_path):
    path = tmp_path / "old.db"
    store = ProjectStore(path); r = project(store)
    history = store.list_versions(r.project_id)
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE card_candidates"); conn.execute("PRAGMA user_version=2"); conn.close()
    store = ProjectStore(path)
    assert store.get_required(r.project_id).state_json == r.state_json
    assert store.list_versions(r.project_id) == history
    candidate = store.complete_card_candidate(admit(store, r).run_id, output())
    live = admit(store, r, key="live")
    store.close(); store = ProjectStore(path)
    assert store.interrupt_stale_generation_runs() == 1
    assert store.get_generation_run_required(live.run_id).status == "interrupted"
    assert store.list_card_candidates(r.project_id) == [candidate]
    store.close()


def test_adopt_and_manual_save_compete_at_revision(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    r = project(store)
    candidate = store.complete_card_candidate(admit(store, r).run_id, output())
    barrier = Barrier(2)
    def adopt():
        barrier.wait()
        return store.decide_card_candidate(r.project_id, candidate["id"], accept=True)
    def edit():
        barrier.wait()
        return store.save_card_edit(r.project_id, kind="characters", target_id="char_a", changes={"name": "手工"}, expected_revision=r.revision)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(adopt), pool.submit(edit)]
        results = []
        for future in futures:
            try: results.append(future.result())
            except RevisionConflictError: results.append(None)
    assert sum(result is not None for result in results) == 1
    assert store.get_required(r.project_id).revision == r.revision + 1
    assert len(store.list_versions(r.project_id)) == 3
