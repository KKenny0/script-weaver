"""SSE lifecycle checks with a controlled pipeline, no network or model calls."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from script_weaver.core.types import ProjectState

spec = importlib.util.spec_from_file_location(
    "generation_api", Path(__file__).parents[2] / "web/api/main.py"
)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


@pytest.mark.parametrize("outcome", ["complete", "error", "disconnect"])
async def test_generation_lifecycle(outcome, monkeypatch):
    started, finish, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old_state = ProjectState(user_input="original")
    new_state = ProjectState(user_input="result")
    proj = {"state": old_state, "status": "created"}

    async def run(**kwargs):
        started.set()
        try:
            await finish.wait()
            if outcome == "error":
                raise RuntimeError("model failed")
            return new_state
        finally:
            stopped.set()

    engine = SimpleNamespace(run_full_pipeline=run)
    monkeypatch.setattr(api, "_get_engine", lambda _: (engine, proj))
    response = await api.generate("test")
    iterator = response.body_iterator
    assert (await anext(iterator))["event"] == "status"
    pending = asyncio.create_task(anext(iterator))
    await started.wait()
    if outcome == "disconnect":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        was_stopped = stopped.is_set()
        # Let buggy implementations finish so a failing test leaks no task.
        finish.set()
        await asyncio.sleep(0)
        assert was_stopped, "pipeline survived SSE disconnect"
        assert proj["status"] != "running"
        assert proj["state"] is old_state
    else:
        finish.set()
        events = [await pending] + [event async for event in iterator]
        assert stopped.is_set()
        assert events[-1]["event"] == "done"
        assert proj["status"] == outcome
        assert proj["state"] is (new_state if outcome == "complete" else old_state)
        assert any(e["event"] == ("error" if outcome == "error" else "progress") for e in events)


async def test_asgi_disconnect_waits_for_pipeline_cleanup(monkeypatch):
    started, cleaned = asyncio.Event(), asyncio.Event()
    proj = {"state": ProjectState(), "status": "created"}

    async def run(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            cleaned.set()

    monkeypatch.setattr(
        api, "_get_engine", lambda _: (SimpleNamespace(run_full_pipeline=run), proj)
    )
    response = await api.generate("test")

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    await asyncio.wait_for(
        response({"type": "http", "asgi": {"version": "3.0"}}, receive, AsyncMock()), 2
    )
    assert cleaned.is_set()
    assert proj["status"] != "running"
