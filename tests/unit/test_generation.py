"""Offline regression checks for PR #12's generation and export contract."""

import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from script_weaver.agents.impl import IdeaRefiner, StoryboardArtist
from script_weaver.core import config, pipeline
from script_weaver.core.types import (
    CameraMovement,
    ProjectState,
    ProjectStatus,
    ScriptBlockType,
    ShotSize,
)
from script_weaver.exporters.fountain_exporter import export_fountain
from script_weaver.exporters.json_exporter import export_json
from script_weaver.exporters.video_gen_exporter import export_video_gen_prompts
from script_weaver.llm.client import LLMClient
from script_weaver.llm.providers import AnthropicProvider, ChatResponse, OpenAIProvider, ToolCall
from script_weaver.memory import profile


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config,
        "_settings",
        config.Settings(data_dir=tmp_path, skills_custom_dir=tmp_path / "skills"),
    )
    monkeypatch.setattr(profile, "_profile_manager", None)


def write(kind, data, **overrides):
    args = {"artifact_type": kind, "content": json.dumps(data), "project_state_json": "{}"}
    args.update(overrides)
    return ChatResponse(tool_calls=[ToolCall(id="write", name="write_artifact", arguments=args)])


def client(*responses):
    return AsyncMock(chat=AsyncMock(side_effect=responses))


async def test_numeric_text_survives_integration_and_export():
    engine = object.__new__(pipeline.PipelineEngine)
    state = ProjectState()
    await engine._integrate_artifact(
        state,
        "storyboard_artist",
        {"shots": [{"on_screen_text": "2026", "sequence_number": "1", "duration_seconds": "3.5"}]},
    )
    assert state.storyboard.shots[0].on_screen_text == "2026"
    assert state.storyboard.shots[0].duration_seconds == 3.5
    await engine._integrate_artifact(
        state,
        "scriptwriter",
        {
            "scenes": [
                {
                    "blocks": [
                        {
                            "block_type": "dialogue",
                            "content": {"character_name": "Bond", "dialogue": "007"},
                        }
                    ]
                }
            ]
        },
    )
    assert "007" in export_fountain(state.script)


@pytest.mark.parametrize(
    "enum,value,expected",
    [
        (ShotSize, "CLOSE_UP", "close_up"),
        (CameraMovement, "PUSH-IN", "push_in"),
        (ScriptBlockType, "DIALOGUE", "dialogue"),
        (ShotSize, "特写", "close_up"),
    ],
)
def test_enum_normalization(enum, value, expected):
    assert enum(value).value == expected
    with pytest.raises(ValueError):
        enum("not_a_real_value")


@pytest.mark.parametrize(
    "bad",
    [
        write("not_valid", {"logline": "bad"}),
        write("outline", {"logline": "wrong artifact"}),
        ChatResponse(
            tool_calls=[ToolCall(id="write", name="write_artifact", arguments={"content": "{}"})]
        ),
        write("refined_idea", {}),
        write("refined_idea", "scalar"),
    ],
)
async def test_invalid_write_is_returned_to_model_for_correction(bad):
    llm = client(bad, write("refined_idea", {"logline": "A real story"}))
    result = await IdeaRefiner(llm_client=llm).execute(ProjectState())
    assert result["data"]["logline"] == "A real story"
    assert llm.chat.await_count == 2
    messages = llm.chat.call_args.kwargs["messages"]
    assert any(m["role"] == "tool" and "error" in m["content"].lower() for m in messages)


async def test_invalid_schema_and_final_json_get_corrected():
    llm = client(
        write("storyboard", {"shots": [{"shot_size": "nonsense"}]}),
        ChatResponse(content='{"shots": []}'),
        write("storyboard", {"shots": [{"visual_description": "A doorway"}]}),
    )
    result = await StoryboardArtist(llm_client=llm).execute(ProjectState())
    assert result["data"]["shots"][0]["visual_description"] == "A doorway"
    assert llm.chat.await_count == 3


async def test_exhausted_agent_stops_pipeline_before_growth():
    config._settings.agent_max_tool_iterations = 2
    llm = client(ChatResponse(content="not an artifact"), ChatResponse(content="still not JSON"))
    engine = pipeline.PipelineEngine(llm_client=llm, auto_approve_gates=True)
    engine._run_growth_loop = AsyncMock()
    with pytest.raises(RuntimeError):
        await engine.run_full_pipeline("story")
    engine._run_growth_loop.assert_not_awaited()
    assert llm.chat.await_count == 2


@pytest.mark.parametrize("provider_class", [AnthropicProvider, OpenAIProvider])
async def test_provider_tool_round_trip(provider_class, monkeypatch):
    requests = []

    async def post(_self, url, *, json, headers):
        requests.append(copy.deepcopy(json))
        if len(requests) == 1:
            if provider_class is AnthropicProvider:
                data = {
                    "content": [
                        {"type": "text", "text": "Reading context"},
                        {
                            "type": "tool_use",
                            "id": "r1",
                            "name": "read_state",
                            "input": {"project_state_json": "{}"},
                        },
                        {
                            "type": "tool_use",
                            "id": "r2",
                            "name": "read_state",
                            "input": {"project_state_json": "{}"},
                        },
                    ]
                }
            else:
                data = {
                    "choices": [
                        {
                            "message": {
                                "content": "Reading context",
                                "tool_calls": [
                                    {
                                        "id": i,
                                        "type": "function",
                                        "function": {
                                            "name": "read_state",
                                            "arguments": '{"project_state_json":"{}"}',
                                        },
                                    }
                                    for i in ["r1", "r2"]
                                ],
                            }
                        }
                    ]
                }
        elif provider_class is AnthropicProvider:
            data = {"content": [{"type": "text", "text": '{"logline":"A story"}'}]}
        else:
            data = {"choices": [{"message": {"content": '{"logline":"A story"}'}}]}
        return httpx.Response(200, json=data, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    llm = LLMClient(provider_class(api_key="fake", model="fake"))
    result = await IdeaRefiner(llm_client=llm).execute(ProjectState())
    assert result["status"] == "success"
    history = requests[1]["messages"]
    if provider_class is AnthropicProvider:
        assert all(m["role"] in ("user", "assistant") for m in history)
        assert history[-2]["content"][0] == {"type": "text", "text": "Reading context"}
        assert [b["id"] for b in history[-2]["content"] if b["type"] == "tool_use"] == ["r1", "r2"]
        assert [b["tool_use_id"] for b in history[-1]["content"]] == ["r1", "r2"]
    else:
        assert history[-3]["content"] == "Reading context"
        assert [m["tool_call_id"] for m in history[-2:]] == ["r1", "r2"]
        assert history[-3]["tool_calls"][0]["function"]["name"] == "read_state"


async def test_full_pipeline_produces_exportable_artifacts(tmp_path):
    script = {
        "title": "007",
        "scenes": [
            {
                "blocks": [
                    {
                        "block_type": "DIALOGUE",
                        "content": {"character_name": "Bond", "dialogue": "007"},
                    }
                ]
            }
        ],
    }
    llm = client(
        write("refined_idea", {"logline": "A spy returns"}),
        write(
            "outline",
            {
                "plot_outline": [
                    {"sequence_number": "1", "title": "Return", "synopsis": "A spy returns"}
                ]
            },
        ),
        write("characters", [{"name": "Bond"}]),
        write("scenes", [{"name": "Station", "location_type": "外景"}]),
        write("art_style", {"overall_style": "Film noir"}),
        write("script", script),
        write(
            "storyboard",
            {
                "shots": [
                    {
                        "visual_description": "A spy waits",
                        "on_screen_text": "2026",
                        "shot_size": "CLOSE_UP",
                        "image_prompt": "Spy at station",
                        "video_prompt": "Spy turns",
                        "duration_seconds": "5",
                    }
                ]
            },
        ),
    )
    engine = pipeline.PipelineEngine(llm_client=llm, auto_approve_gates=True)
    state = await engine.run_full_pipeline("A spy returns")
    assert state.meta.status is ProjectStatus.COMPLETE
    assert (
        state.refined_idea
        and state.outline.plot_outline
        and state.characters
        and state.scenes
        and state.art_style.overall_style
    )
    assert state.script.scenes and state.storyboard.total_shot_count == 1
    assert "007" in export_fountain(state.script)
    assert json.loads(export_json(state))["storyboard"]["shots"][0]["on_screen_text"] == "2026"
    exported = export_video_gen_prompts(state, tmp_path / "exports")
    assert "error" not in exported
    assert list((tmp_path / "exports").rglob("*.csv"))
    assert profile.get_profile_manager().profile.stats.completed_projects == 1


@pytest.mark.parametrize(
    "bad", [ChatResponse(content='```json\n{"shots":'), write("storyboard", None)]
)
async def test_truncated_or_null_artifact_gets_corrected(bad):
    llm = client(bad, write("storyboard", {"shots": [{"visual_description": "A doorway"}]}))
    result = await StoryboardArtist(llm_client=llm).execute(ProjectState())
    assert result["status"] == "success"
    assert llm.chat.await_count == 2


async def test_storyboard_model_receives_valid_values_and_reads_real_state():
    """The provider must get the contract and state tools must use trusted state."""
    from script_weaver.core.types import Script, ScriptScene

    state = ProjectState(
        script=Script(scenes=[ScriptScene(scene_id="actual-scene")]),
    )
    calls = 0

    async def chat(*, messages, tools):
        nonlocal calls
        calls += 1
        write_tool = next(t for t in tools if t["name"] == "write_artifact")
        assert write_tool["parameters"]["properties"]["artifact_type"]["enum"] == ["storyboard"]
        contract = json.loads(
            write_tool["parameters"]["properties"]["content"]["description"].split("\n", 1)[1]
        )
        assert contract["$defs"]["ShotSize"]["enum"] == [m.value for m in ShotSize]
        for tool in tools:
            if tool["name"] in ("read_state", "read_artifact", "write_artifact"):
                assert "project_state_json" not in tool["parameters"]["properties"]
                assert "project_state_json" not in tool["parameters"]["required"]
        if calls == 1:
            return ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="read",
                        name="read_artifact",
                        arguments={
                            "artifact_type": "script",
                            # Stale/hallucinated context must never replace the real project.
                            "project_state_json": '{"script":{"scenes":[{"scene_id":"fake-scene"}]}}',
                        },
                    )
                ]
            )
        artifact = json.loads(messages[-1]["content"])
        assert artifact["scenes"][0]["scene_id"] == "actual-scene"
        return write(
            "storyboard",
            {
                "shots": [
                    {
                        "scene_id": artifact["scenes"][0]["scene_id"],
                        "shot_size": contract["$defs"]["ShotSize"]["enum"][0],
                        "visual_description": "A doorway",
                    }
                ]
            },
        )

    result = await StoryboardArtist(llm_client=AsyncMock(chat=chat)).execute(state)
    assert calls == 2
    assert result["status"] == "success"
    assert result["data"]["shots"][0]["scene_id"] == "actual-scene"


async def test_write_supplies_runtime_state_without_model_echo():
    response = write("refined_idea", {"logline": "A story"})
    del response.tool_calls[0].arguments["project_state_json"]
    result = await IdeaRefiner(llm_client=client(response), max_iterations=1).execute(
        ProjectState()
    )
    assert result["status"] == "success"


async def test_iteration_limit_preserves_actual_validation_error():
    llm = client(
        write("storyboard", {"shots": [{"shot_size": "invented-shot"}]}),
        ChatResponse(content="The enum values need fixing. Let me produce the storyboard."),
    )
    result = await StoryboardArtist(llm_client=llm, max_iterations=2).execute(ProjectState())
    assert result["error"] == "max_iterations_exceeded"
    assert "shot_size" in result["last_error"]
    assert "extreme_long_shot" in result["last_error"]


async def test_truncated_generation_is_identified_as_token_limit():
    llm = client(ChatResponse(content='{"shots":[', stop_reason="max_tokens"))
    result = await StoryboardArtist(llm_client=llm, max_iterations=1).execute(ProjectState())
    assert result["error"] == "output_token_limit"
    assert "max_tokens" in result["last_error"]


async def test_empty_transport_error_is_identifiable_and_recorded(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "verify_real_model", Path(__file__).parents[2] / "scripts/verify_real_model.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    llm = client(httpx.ReadTimeout(""))
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        await runner.verify(tmp_path, "A spy returns", llm_client=llm)
    record = json.loads((tmp_path / "calls.jsonl").read_text())
    assert record["error_type"] == "ReadTimeout"
    assert record["agent"] == "idea_refiner"
    assert record["elapsed_seconds"] >= 0
    assert llm.chat.await_count == 1


async def test_original_brief_reaches_downstream_agent_even_with_expanded_outline():
    from script_weaver.agents.impl import ScriptWriter, Structurer

    brief = '60秒悬疑短片，两人一景。门牌为007，屏幕显示2026，结尾反转。'
    state = ProjectState(user_input=brief, refined_idea='最后60秒发生反转的酒店故事')
    for agent_type in (Structurer, ScriptWriter):
        llm = client(RuntimeError('stop after capturing request'))
        await agent_type(llm_client=llm).execute(state)
        messages = llm.chat.call_args.kwargs['messages']
        assert brief in messages[1]['content']
        assert '原始创作要求优先' in messages[1]['content']
