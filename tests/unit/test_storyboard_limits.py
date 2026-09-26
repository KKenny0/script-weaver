"""Regression coverage for full-storyboard truncation during real-model runs."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from script_weaver.agents.impl import StoryboardArtist
from script_weaver.core import config
from script_weaver.core.types import ProjectState, Script, ScriptScene, Storyboard
from script_weaver.llm.client import LLMClient
from script_weaver.llm.providers import ChatResponse, DeepSeekProvider, TokenUsage, ToolCall


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config,
        "_settings",
        config.Settings(data_dir=tmp_path, skills_custom_dir=tmp_path / "skills"),
    )


def response(scene_id):
    return ChatResponse(
        tool_calls=[
            ToolCall(
                id="write",
                name="write_artifact",
                arguments={
                    "artifact_type": "storyboard",
                    "content": json.dumps(
                        {
                            "shots": [
                                {
                                    "shot_id": "local-1",
                                    "scene_id": scene_id,
                                    "visual_description": "A doorway",
                                    "duration_seconds": 3,
                                }
                            ]
                        }
                    ),
                },
            )
        ]
    )


async def test_storyboard_requests_are_scoped_and_merged_without_losing_scenes():
    state = ProjectState(
        script=Script(scenes=[ScriptScene(scene_id=f"scene-{i}") for i in range(13)])
    )
    original = state.model_dump_json()
    llm = AsyncMock()
    reads = 0

    async def chat(*, messages, tools):
        nonlocal reads
        if messages[-1]["role"] != "tool":
            return ChatResponse(
                tool_calls=[
                    ToolCall(id="read", name="read_artifact", arguments={"artifact_type": "script"})
                ]
            )
        script = json.loads(messages[-1]["content"])
        assert len(script["scenes"]) == 1, "full script still sent for every completion"
        scene_id = script["scenes"][0]["scene_id"]
        assert scene_id == f"scene-{reads}"
        reads += 1
        return response(scene_id)

    llm.chat.side_effect = chat
    result = await StoryboardArtist(llm_client=llm).execute(state)
    assert result.get("status") == "success", result
    storyboard = Storyboard.model_validate(result["data"])
    assert [shot.scene_id for shot in storyboard.shots] == [f"scene-{i}" for i in range(13)]
    assert len({shot.shot_id for shot in storyboard.shots}) == 13
    assert storyboard.total_estimated_duration == 39
    assert storyboard.total_shot_count == 13
    assert state.model_dump_json() == original


async def test_truncation_stops_without_spending_fifteen_identical_attempts():
    truncated = ChatResponse(stop_reason="max_tokens", usage=TokenUsage(completion_tokens=8192))
    llm = AsyncMock(chat=AsyncMock(return_value=truncated))
    result = await StoryboardArtist(llm_client=llm).execute(ProjectState())
    assert llm.chat.await_count == 1
    assert result["error"] == "output_token_limit"
    assert "8192" in result["last_error"]


async def test_failed_scene_never_returns_partial_storyboard_as_success():
    state = ProjectState(
        script=Script(scenes=[ScriptScene(scene_id="a"), ScriptScene(scene_id="b")])
    )
    llm = AsyncMock(
        chat=AsyncMock(side_effect=[response("a"), ChatResponse(stop_reason="max_tokens")])
    )
    result = await StoryboardArtist(llm_client=llm).execute(state)
    assert result.get("error") == "output_token_limit"
    assert result["failed_scene_id"] == "b"
    assert result.get("status") != "success"
    assert state.storyboard is None


async def test_deepseek_request_explicitly_uses_supported_non_thinking_mode(monkeypatch):
    requests = []

    async def post(_self, url, *, json, headers):
        requests.append(json)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"logline":"story"}'}}
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await LLMClient(DeepSeekProvider(api_key="fake", model="deepseek-flash")).chat(
        [{"role": "user", "content": "story"}]
    )
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["max_tokens"] == 8192


async def test_checkpoint_resume_skips_successful_agents_and_exports(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    from script_weaver.memory import profile

    monkeypatch.setattr(profile, "_profile_manager", None)
    spec = importlib.util.spec_from_file_location(
        "real_model_verifier", Path(__file__).parents[2] / "scripts/verify_real_model.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    def artifact(kind, data):
        return ChatResponse(
            tool_calls=[
                ToolCall(
                    id="write",
                    name="write_artifact",
                    arguments={
                        "artifact_type": kind,
                        "content": json.dumps(data),
                    },
                )
            ]
        )

    first = AsyncMock(
        chat=AsyncMock(
            side_effect=[
                artifact("refined_idea", {"logline": "A spy returns"}),
                artifact("outline", {"plot_outline": [{"synopsis": "A spy returns"}]}),
                artifact("characters", [{"name": "Bond"}]),
                artifact("scenes", [{"name": "Station"}]),
                artifact("art_style", {"overall_style": "Film noir"}),
                artifact(
                    "script",
                    {
                        "scenes": [
                            {
                                "scene_id": "a",
                                "blocks": [
                                    {
                                        "block_type": "dialogue",
                                        "content": {"character_name": "Bond", "dialogue": "007"},
                                    }
                                ],
                            }
                        ]
                    },
                ),
                ChatResponse(stop_reason="max_tokens", usage=TokenUsage(completion_tokens=8192)),
            ]
        )
    )
    with pytest.raises(RuntimeError, match="output_token_limit"):
        await runner.verify(tmp_path, "A spy returns", llm_client=first)
    snapshot = json.loads((tmp_path / "checkpoint.json").read_text())
    # Ticket #15 shared contract: the envelope records the completed
    # success prefix (everything but the storyboard stage) plus the input
    # and format version a resume validates against.
    from script_weaver.core.pipeline import GENERATION_STEPS

    assert snapshot["format_version"] == 1
    assert snapshot["user_input"] == "A spy returns"
    assert snapshot["completed_steps"] == list(GENERATION_STEPS)[:-2]
    assert snapshot["state"]["script"]["scenes"][0]["scene_id"] == "a"
    assert not (tmp_path / "project.json").exists()
    assert profile.get_profile_manager().profile.stats.completed_projects == 0
    second = AsyncMock(chat=AsyncMock(return_value=response("a")))
    state = await runner.verify(tmp_path, None, resume=True, llm_client=second)
    assert second.chat.await_count == 1
    assert state.meta.status.value == "complete"
    assert (tmp_path / "project.json").exists()
    assert "007" in (tmp_path / "script.fountain").read_text()
    assert (tmp_path / "video_gen/video_gen_shots.csv").exists()
    assert profile.get_profile_manager().profile.stats.completed_projects == 1
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert calls[-2]["stop_reason"] == "max_tokens"
    assert calls[-2]["usage"]["completion_tokens"] == 8192
    assert calls[-1]["agent"] == "storyboard_artist"
    with pytest.raises(ValueError, match="already completed"):
        await runner.verify(tmp_path, None, resume=True, llm_client=second)
