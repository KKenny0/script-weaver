"""SSE lifecycle checks with a controlled pipeline, no network or model calls.

Ticket #13 keeps the "disconnect cancels the pipeline" contract from PR #12
while the project store provides persistence; the engine/context seam replaces
the old in-memory project dict.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from script_weaver.core.types import ProjectState

spec = importlib.util.spec_from_file_location(
    "generation_api", Path(__file__).parents[2] / "web/api/main.py"
)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api  # dataclass decorators need the module registered
spec.loader.exec_module(api)


@pytest.mark.parametrize("outcome", ["complete", "error", "disconnect"])
async def test_generation_lifecycle(outcome, monkeypatch):
    started, finish, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old_state = ProjectState(user_input="original")
    new_state = ProjectState(user_input="result")
    committed = []

    class FakeCtx:
        project_id = "test"
        revision = 1
        base_state_json = "{}"
        state = old_state

        @staticmethod
        def commit(new, source, summary):
            committed.append((new, source))
            return SimpleNamespace(revision=2)

    async def run(**kwargs):
        started.set()
        try:
            await finish.wait()
            if outcome == "error":
                raise RuntimeError("model failed")
            return new_state
        finally:
            stopped.set()

    engine = SimpleNamespace(run_full_pipeline=run, _progress=None)

    async def fake_get_engine(project_id):
        return engine, FakeCtx()

    monkeypatch.setattr(api, "_get_engine", fake_get_engine)

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
        assert committed == [], "disconnected run must not persist a result"
    else:
        finish.set()
        events = [await pending] + [event async for event in iterator]
        assert stopped.is_set()
        assert events[-1]["event"] == "done"
        if outcome == "complete":
            assert committed and committed[0][0] is new_state
            assert committed[0][1] == "pipeline"
        else:
            assert committed == [], "failed run must not persist a result"
    assert "test" not in api._active_generations
