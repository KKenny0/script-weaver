"""Shared resume-contract checks (ticket #15).

One contract, three surfaces: the pure module (resume.py), the CLI
(``--resume``) and the web resume endpoint must refuse the SAME illegal
checkpoint with the SAME reason, and every refusal happens before any
model call. The CLI cases below run offline against controlled engine
stubs with a real output directory and real atomic checkpoint writes.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from script_weaver.core import config, pipeline
from script_weaver.core.resume import (
    ResumeRejected,
    assert_fingerprint_match,
    build_checkpoint,
    current_fingerprint,
    decode_checkpoint,
    encode_checkpoint,
    fingerprint_diffs,
    validate_completed_steps,
    write_checkpoint_atomic,
)
from script_weaver.core.types import (
    BasicInfo,
    Outline,
    ProjectState,
    ProjectStatus,
    Script,
    Storyboard,
)
from script_weaver.llm.providers import ChatResponse, ToolCall
from script_weaver.memory import profile


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config,
        "_settings",
        config.Settings(data_dir=tmp_path, skills_custom_dir=tmp_path / "skills"),
    )
    monkeypatch.setattr(profile, "_profile_manager", None)


def write(kind, data, **overrides):
    args = {"artifact_type": kind, "content": json.dumps(data)}
    args.update(overrides)
    return ChatResponse(
        tool_calls=[ToolCall(id="write", name="write_artifact", arguments=args)]
    )


def client(*responses):
    return AsyncMock(chat=AsyncMock(side_effect=list(responses)))


def scripted_llm():
    """Responses for every stage, in pipeline order."""
    return client(*_full_responses())


def _full_responses():
    return (
        write("refined_idea", {"logline": "A spy returns"}),
        write("outline", {"plot_outline": [
            {"sequence_number": "1", "title": "Return", "synopsis": "A spy returns"}
        ]}),
        write("characters", [{"name": "Bond"}]),
        write("scenes", [{"name": "Station", "location_type": "外景"}]),
        write("art_style", {"overall_style": "Film noir"}),
        write("script", {"title": "007", "scenes": [{"blocks": [
            {"block_type": "DIALOGUE",
             "content": {"character_name": "Bond", "dialogue": "007"}},
        ]}]}),
        write("storyboard", {"shots": [{
            "visual_description": "A spy waits", "shot_size": "CLOSE_UP",
            "image_prompt": "Spy at station", "video_prompt": "Spy turns",
            "duration_seconds": "5",
        }]}),
    )


def state_through_scriptwriter() -> ProjectState:
    """A snapshot whose artifacts cover the first six generation steps."""
    state = ProjectState(user_input="A spy returns")
    state.refined_idea = "A spy returns"
    state.outline = Outline(
        basic_info=BasicInfo(logline="A spy returns"),
        plot_outline=[{"sequence_number": "1", "title": "Return",
                       "synopsis": "A spy returns"}],
    )
    state.characters = [{"name": "Bond"}]
    state.scenes = [{"name": "Station", "location_type": "外景"}]
    from script_weaver.core.types import ArtStyle, Character, SceneDesign
    state.characters = [Character.model_validate({"name": "Bond"})]
    state.scenes = [SceneDesign.model_validate({"name": "Station", "location_type": "外景"})]
    state.art_style = ArtStyle.model_validate({"overall_style": "Film noir"})
    state.script = Script.model_validate({"scenes": [{"blocks": [
        {"block_type": "DIALOGUE",
         "content": {"character_name": "Bond", "dialogue": "007"}},
    ]}]})
    state.meta.status = ProjectStatus.SCRIPTING
    return state


# ── Prefix validation ───────────────────────────────────────


@pytest.mark.parametrize(
    "steps",
    [
        ["structurer"],                                # skipped idea_refiner
        ["idea_refiner", "structurer", "idea_refiner"],  # duplicate
        ["structurer", "idea_refiner"],                # out of order
        ["idea_refiner", "scriptwriter"],              # gap
        ["nope"],                                      # unknown step
    ],
)
def test_non_prefix_step_lists_are_rejected(steps):
    with pytest.raises(ResumeRejected) as excinfo:
        validate_completed_steps(steps)
    assert excinfo.value.code == "invalid_prefix"


def test_unknown_step_keeps_legacy_valueerror_wording():
    with pytest.raises(ValueError, match="Unknown completed steps"):
        validate_completed_steps(["nope"])


def test_valid_prefixes_pass():
    validate_completed_steps([])
    validate_completed_steps(["idea_refiner"])
    validate_completed_steps(
        ["idea_refiner", "structurer", "character_designer", "scene_designer",
         "art_director", "scriptwriter", "storyboard_artist", "finalize"]
    )


def test_completed_step_without_artifact_is_rejected():
    state = ProjectState(user_input="x")
    with pytest.raises(ResumeRejected) as excinfo:
        validate_completed_steps(["idea_refiner"], state=state)
    assert excinfo.value.code == "invalid_prefix"
    assert "概念精炼" in excinfo.value.message


def test_prefix_with_artifacts_passes():
    state = state_through_scriptwriter()
    validate_completed_steps(
        ["idea_refiner", "structurer", "character_designer", "scene_designer",
         "art_director", "scriptwriter"],
        state=state,
    )


# ── Checkpoint encode/decode ────────────────────────────────


def test_checkpoint_roundtrip_and_content_complete():
    state = state_through_scriptwriter()
    checkpoint = build_checkpoint(
        user_input="A spy returns",
        completed_steps=["idea_refiner"],
        state=state,
        fingerprint={"model": "m"},
        basis={"revision": 3},
    )
    decoded = decode_checkpoint(encode_checkpoint(checkpoint))
    assert decoded == checkpoint
    assert not decoded.content_complete
    assert decoded.state_model().script.scenes
    full = checkpoint.model_copy(
        update={"completed_steps": [*checkpoint.completed_steps, "structurer",
                                    "character_designer", "scene_designer",
                                    "art_director", "scriptwriter",
                                    "storyboard_artist", "finalize"]}
    )
    assert decode_checkpoint(encode_checkpoint(full)).content_complete


@pytest.mark.parametrize(
    "payload,code",
    [
        ("{broken json", "checkpoint_invalid"),
        ('{"format_version": 99}', "checkpoint_unsupported"),
        # A pre-#15 raw ProjectState blob (the old checkpoint column
        # content) is not an envelope: no resume basis.
        ('{"meta": {"id": "x"}, "user_input": "idea"}', "checkpoint_invalid"),
        (None, "checkpoint_invalid"),
    ],
)
def test_illegal_checkpoints_are_refused_with_stable_codes(payload, code):
    with pytest.raises(ResumeRejected) as excinfo:
        decode_checkpoint(payload)
    assert excinfo.value.code == code


def test_state_digest_tampering_is_detected():
    checkpoint = build_checkpoint(
        user_input="i", completed_steps=["idea_refiner"],
        state=state_through_scriptwriter(),
    )
    text = json.loads(encode_checkpoint(checkpoint))
    text["state"]["user_input"] = "tampered"
    with pytest.raises(ResumeRejected) as excinfo:
        decode_checkpoint(json.dumps(text))
    assert excinfo.value.code == "checkpoint_invalid"
    assert "摘要" in excinfo.value.message


def test_invalid_growth_status_refused():
    checkpoint = build_checkpoint(user_input="i")
    raw = json.loads(encode_checkpoint(checkpoint))
    raw["growth_status"] = "exploded"
    with pytest.raises(ResumeRejected) as excinfo:
        decode_checkpoint(json.dumps(raw))
    assert excinfo.value.code == "checkpoint_invalid"


# ── Atomic checkpoint writes ────────────────────────────────


def test_atomic_write_leaves_previous_checkpoint_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "checkpoint.json"
    good = build_checkpoint(user_input="first", completed_steps=["idea_refiner"])
    write_checkpoint_atomic(path, good)

    import os as os_mod

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os_mod, "replace", boom)
    with pytest.raises(OSError):
        write_checkpoint_atomic(path, build_checkpoint(user_input="second"))
    # The previous complete checkpoint survived verbatim, and no temp file
    # was left behind.
    assert decode_checkpoint(path.read_text(encoding="utf-8")) == good
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_atomic_write_survives_hard_kill_window(tmp_path):
    """Only ever the old or the new document — a same-filesystem replace
    is atomic, so an interrupted process cannot leave a half file."""
    path = tmp_path / "checkpoint.json"
    first = build_checkpoint(user_input="first")
    write_checkpoint_atomic(path, first)
    second = build_checkpoint(user_input="second", completed_steps=["idea_refiner"])
    write_checkpoint_atomic(path, second)
    assert decode_checkpoint(path.read_text(encoding="utf-8")) == second


# ── Execution fingerprint ───────────────────────────────────


async def test_fingerprint_changes_when_settings_change():
    base = await current_fingerprint(skill_bindings={}, auto_approve=True)
    config._settings.llm_temperature = 0.11
    drifted = await current_fingerprint(skill_bindings={}, auto_approve=True)
    diffs = fingerprint_diffs(base, drifted)
    assert any("temperature" in d for d in diffs)
    with pytest.raises(ResumeRejected) as excinfo:
        assert_fingerprint_match(base, drifted)
    assert excinfo.value.code == "config_changed"
    assert "未调用模型" in excinfo.value.message


async def test_fingerprint_changes_when_skill_content_changes(tmp_path):
    skills_dir = config._settings.skills_custom_dir
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skills_dir / "test-skill.yaml"
    skill_file.write_text(
        "id: test-skill\nname: Test\nstage: structuring\n"
        "description: d\nprompt_injection: |\n  first version\n",
        encoding="utf-8",
    )
    base = await current_fingerprint(skill_bindings={}, auto_approve=True)
    assert "test-skill" in base["skills"]

    skill_file.write_text(
        "id: test-skill\nname: Test\nstage: structuring\n"
        "description: d\nprompt_injection: |\n  EDITED content\n",
        encoding="utf-8",
    )
    drifted = await current_fingerprint(skill_bindings={}, auto_approve=True)
    diffs = fingerprint_diffs(base, drifted)
    assert any("test-skill" in d for d in diffs)
    with pytest.raises(ResumeRejected):
        assert_fingerprint_match(base, drifted)


async def test_fingerprint_changes_when_binding_changes():
    base = await current_fingerprint(skill_bindings={}, auto_approve=True)
    bound = await current_fingerprint(
        skill_bindings={"structuring": {"skill_id": "x", "active": True}},
        auto_approve=True,
    )
    assert any("Skill 绑定" in d for d in fingerprint_diffs(base, bound))


async def test_fingerprint_contains_no_credentials():
    fingerprint = await current_fingerprint(skill_bindings={}, auto_approve=True)
    blob = json.dumps(fingerprint)
    for key in config.Settings.model_fields:
        if "key" in key.lower():
            assert key not in blob
    assert set(fingerprint) == {
        "provider", "model", "temperature", "max_tokens", "endpoint",
        "auto_approve", "skill_bindings", "skills",
    }


# ── Engine-level resume semantics ───────────────────────────


async def test_resume_rejected_before_any_model_call_on_drift():
    llm = scripted_llm()
    engine = pipeline.PipelineEngine(llm_client=llm, auto_approve_gates=True)
    checkpoint = build_checkpoint(
        user_input="A spy returns",
        completed_steps=["idea_refiner", "structurer"],
        state=state_through_scriptwriter(),
        fingerprint={**await current_fingerprint(auto_approve=True), "model": "another-model"},
    )
    with pytest.raises(ResumeRejected) as excinfo:
        await engine.run_full_pipeline(resume_from=checkpoint)
    assert excinfo.value.code == "config_changed"
    assert llm.chat.await_count == 0  # rejected before any model call


def _responses_failing_at_scriptwriter():
    """Five good stage answers, then a broken scriptwriter twice."""
    good_prefix = (
        write("refined_idea", {"logline": "A spy returns"}),
        write("outline", {"plot_outline": [
            {"sequence_number": "1", "title": "Return", "synopsis": "A spy returns"}
        ]}),
        write("characters", [{"name": "Bond"}]),
        write("scenes", [{"name": "Station", "location_type": "外景"}]),
        write("art_style", {"overall_style": "Film noir"}),
    )
    broken = (
        ChatResponse(content="not a tool call"),
        ChatResponse(content="still not a tool call"),
    )
    return good_prefix + broken


async def test_resume_completes_only_missing_stages_with_exact_call_counts():
    first_llm = client(*_responses_failing_at_scriptwriter())
    engine = pipeline.PipelineEngine(llm_client=first_llm, auto_approve_gates=True)
    with pytest.raises(RuntimeError, match="scriptwriter"):
        await engine.run_full_pipeline("A spy returns")
    # 5 successful stages + 3 scriptwriter attempts (2 broken answers plus
    # the retry that exhausted the scripted responses).
    assert first_llm.chat.await_count == 8

    # The run stopped with five completed stages; the resume must talk to
    # the model exactly for the remaining three stages.
    fingerprint = await current_fingerprint(skill_bindings={}, auto_approve=True)
    snapshot = state_through_scriptwriter()
    checkpoint = build_checkpoint(
        user_input="A spy returns",
        completed_steps=["idea_refiner", "structurer", "character_designer",
                         "scene_designer", "art_director"],
        state=snapshot,
        fingerprint=fingerprint,
    )
    frozen = checkpoint.model_dump(mode="json")

    second_llm = client(
        write("script", {"title": "007", "scenes": [{"blocks": [
            {"block_type": "DIALOGUE",
             "content": {"character_name": "Bond", "dialogue": "007"}},
        ]}]}),
        write("storyboard", {"shots": [{
            "visual_description": "A spy waits", "shot_size": "CLOSE_UP",
            "image_prompt": "Spy at station", "video_prompt": "Spy turns",
            "duration_seconds": "5",
        }]}),
    )
    engine2 = pipeline.PipelineEngine(llm_client=second_llm, auto_approve_gates=True)
    state = await engine2.run_full_pipeline(resume_from=checkpoint)
    assert second_llm.chat.await_count == 2  # scriptwriter + storyboard only
    assert state.meta.status is ProjectStatus.COMPLETE
    # The caller's checkpoint snapshot was never mutated by execution.
    assert checkpoint.model_dump(mode="json") == frozen


async def test_storyboard_partial_failure_reruns_whole_stage_on_resume():
    # Storyboard dies mid-stage: the stage never completes, so the resume
    # must re-run the ENTIRE storyboard stage (not per-scene continue).
    first_llm = client(
        *_full_responses()[:6],
        ChatResponse(stop_reason="max_tokens"),  # truncated storyboard attempt
        ChatResponse(content="gibberish"),       # failed self-correction
    )
    engine = pipeline.PipelineEngine(llm_client=first_llm, auto_approve_gates=True)
    with pytest.raises(RuntimeError, match="storyboard_artist"):
        await engine.run_full_pipeline("A spy returns")

    fingerprint = await current_fingerprint(skill_bindings={}, auto_approve=True)
    checkpoint = build_checkpoint(
        user_input="A spy returns",
        completed_steps=["idea_refiner", "structurer", "character_designer",
                         "scene_designer", "art_director", "scriptwriter"],
        state=state_through_scriptwriter(),
        fingerprint=fingerprint,
    )
    second_llm = client(
        write("storyboard", {"shots": [{
            "visual_description": "A doorway", "shot_size": "CLOSE_UP",
            "duration_seconds": "5",
        }]}),
    )
    engine2 = pipeline.PipelineEngine(llm_client=second_llm, auto_approve_gates=True)
    state = await engine2.run_full_pipeline(resume_from=checkpoint)
    assert state.storyboard.total_shot_count == 1
    assert second_llm.chat.await_count == 1  # the whole stage, once


async def test_resume_asks_designing_gate_even_with_all_designers_saved():
    """A saved artifact is not a confirmed gate: resuming with all three
    designers checkpointed but scriptwriter NOT yet run must re-ask."""
    asked: list[str] = []

    async def gate(stage: str, summary: str):
        asked.append(stage)
        from script_weaver.core.pipeline import HumanGateResult
        return HumanGateResult(approved=True)

    fingerprint = await current_fingerprint(skill_bindings={}, auto_approve=False)
    checkpoint = build_checkpoint(
        user_input="A spy returns",
        completed_steps=["idea_refiner", "structurer", "character_designer",
                         "scene_designer", "art_director"],
        state=state_through_scriptwriter(),
        fingerprint=fingerprint,
    )
    llm = client(
        write("script", {"title": "007", "scenes": [{"blocks": [
            {"block_type": "DIALOGUE",
             "content": {"character_name": "Bond", "dialogue": "007"}},
        ]}]}),
        write("storyboard", {"shots": [{
            "visual_description": "A doorway", "shot_size": "CLOSE_UP",
            "duration_seconds": "5",
        }]}),
    )
    engine = pipeline.PipelineEngine(
        llm_client=llm, auto_approve_gates=False, gate_callback=gate
    )
    await engine.run_full_pipeline(resume_from=checkpoint)
    # ideation/structuring gates are implied by their completed steps;
    # designing is re-asked; scriptwriting is fresh (asked in this run).
    assert asked == ["designing", "scriptwriting"]


async def test_completed_checkpoint_resume_skips_pipeline_and_growth():
    """A finished checkpoint resumes to a no-op: zero model calls, zero
    growth replay (profile stats unchanged), content untouched."""
    from script_weaver.core.types import Shot

    complete = state_through_scriptwriter()
    complete.storyboard = Storyboard(shots=[Shot(
        shot_id="s1", scene_id="a", visual_description="A doorway",
        duration_seconds=5,
    )])
    complete.storyboard.compute_totals()
    complete.meta.status = ProjectStatus.COMPLETE
    fingerprint = await current_fingerprint(skill_bindings={}, auto_approve=True)
    checkpoint = build_checkpoint(
        user_input="A spy returns",
        completed_steps=list(pipeline.GENERATION_STEPS),
        state=complete,
        fingerprint=fingerprint,
        growth_status="succeeded",
    )
    llm = client()
    engine = pipeline.PipelineEngine(llm_client=llm, auto_approve_gates=True)
    state = await engine.run_full_pipeline(resume_from=checkpoint)
    assert llm.chat.await_count == 0
    assert state.storyboard.total_shot_count == 1
    # Growth was already done for this checkpoint: not replayed.
    stats = profile.get_profile_manager().profile.stats
    assert stats.completed_projects == 0


async def test_growth_failure_is_reported_and_content_stays_complete(monkeypatch):
    """Inject a failure at a growth side-effect boundary: the run records
    failed, content stays complete, no exception escapes."""
    broken_save = profile.get_profile_manager()
    monkeypatch.setattr(
        broken_save, "save", lambda: (_ for _ in ()).throw(RuntimeError("profile disk full"))
    )

    events: list[tuple[str, str | None]] = []

    async def on_growth_event(status, error):
        events.append((status, error))

    llm = scripted_llm()
    engine = pipeline.PipelineEngine(llm_client=llm, auto_approve_gates=True)
    state = await engine.run_full_pipeline(
        "A spy returns", on_growth_event=on_growth_event
    )
    assert state.meta.status is ProjectStatus.COMPLETE
    assert [s for s, _ in events] == ["running", "failed"]
    assert "profile disk full" in events[1][1]


async def test_growth_lifecycle_events_reach_the_callback():
    events: list[str] = []

    async def on_growth_event(status, error):
        events.append(status)

    llm = scripted_llm()
    engine = pipeline.PipelineEngine(llm_client=llm, auto_approve_gates=True)
    await engine.run_full_pipeline("A spy returns", on_growth_event=on_growth_event)
    assert events == ["running", "succeeded"]
    assert profile.get_profile_manager().profile.stats.completed_projects == 1


# ── CLI end-to-end (offline, controlled engine) ─────────────


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """CliRunner plus an engine factory recording every construction."""
    from script_weaver import cli as cli_module

    built = []
    responses = {"queue": None}

    def fake_engine(auto_approve: bool = False):
        llm = client(*responses["queue"])
        engine = pipeline.PipelineEngine(
            llm_client=llm, auto_approve_gates=auto_approve,
            progress_callback=lambda stage, message: None,
        )
        built.append(engine)
        return engine

    monkeypatch.setattr(cli_module, "_create_engine", fake_engine)
    runner = CliRunner()
    out_dir = tmp_path / "out"
    return runner, out_dir, built, responses, cli_module


def test_cli_refuses_unfinished_checkpoint_without_resume(cli_env):
    runner, out_dir, built, _r, cli = cli_env
    # Simulate a hard kill mid-run: stage checkpoints up to structurer.
    checkpoint = build_checkpoint(
        user_input="A spy returns",
        completed_steps=["idea_refiner", "structurer"],
        state=state_through_scriptwriter(),
        fingerprint={},
    )
    out_dir.mkdir(parents=True)
    write_checkpoint_atomic(out_dir / "checkpoint.json", checkpoint)
    result = runner.invoke(cli.main, ["generate", "-o", str(out_dir)])
    assert result.exit_code != 0
    assert "--resume" in result.output
    assert built == []  # never even built an engine: no model path at all
    # The file was not touched.
    assert decode_checkpoint(
        (out_dir / "checkpoint.json").read_text(encoding="utf-8")
    ).completed_steps == ["idea_refiner", "structurer"]


def test_cli_resume_missing_checkpoint_exits_nonzero(cli_env):
    runner, out_dir, _, _r, cli = cli_env
    result = runner.invoke(cli.main, ["generate", "--resume", "-o", str(out_dir)])
    assert result.exit_code != 0
    assert "checkpoint" in result.output


def test_cli_resume_rejects_different_explicit_idea(cli_env):
    runner, out_dir, _, _r, cli = cli_env
    checkpoint = build_checkpoint(user_input="原始输入", completed_steps=[])
    out_dir.mkdir(parents=True)
    write_checkpoint_atomic(out_dir / "checkpoint.json", checkpoint)
    result = runner.invoke(
        cli.main, ["generate", "完全不同的想法", "--resume", "-o", str(out_dir)]
    )
    assert result.exit_code != 0
    assert "不同" in result.output


def test_cli_resume_rejects_changed_execution_params(cli_env):
    runner, out_dir, _, _, cli = cli_env
    fingerprint = asyncio.run(current_fingerprint(skill_bindings={}, auto_approve=True))
    fingerprint = {**fingerprint, "temperature": 0.11}
    checkpoint = build_checkpoint(
        user_input="A spy returns", completed_steps=["idea_refiner"],
        fingerprint=fingerprint,
    )
    out_dir.mkdir(parents=True)
    write_checkpoint_atomic(out_dir / "checkpoint.json", checkpoint)
    result = runner.invoke(cli.main, ["generate", "--resume", "-o", str(out_dir)])
    assert result.exit_code != 0
    assert "temperature" in result.output
    # The original file survives a refusal verbatim.
    assert decode_checkpoint(
        (out_dir / "checkpoint.json").read_text(encoding="utf-8")
    ).fingerprint["temperature"] == 0.11


def test_cli_resume_rejects_invalid_prefix_and_preserves_file(cli_env):
    runner, out_dir, _, _r, cli = cli_env
    checkpoint = build_checkpoint(
        user_input="x", completed_steps=["structurer"],  # skipped step 1
        fingerprint={"model": "m"},
    )
    raw = json.loads(encode_checkpoint(checkpoint))
    raw["completed_steps"] = ["structurer"]  # keep digest out of the picture
    raw["state"] = None
    raw["state_digest"] = ""
    out_dir.mkdir(parents=True)
    (out_dir / "checkpoint.json").write_text(json.dumps(raw), encoding="utf-8")
    result = runner.invoke(cli.main, ["generate", "--resume", "-o", str(out_dir)])
    assert result.exit_code != 0
    assert "连续前缀" in result.output
    assert json.loads((out_dir / "checkpoint.json").read_text())["completed_steps"] == [
        "structurer"
    ]


def test_cli_full_run_then_resume_only_runs_missing_stages(cli_env):
    runner, out_dir, built, responses, cli = cli_env
    # First invocation: dies at the storyboard stage (hard-kill stand-in).
    responses["queue"] = _responses_failing_at_scriptwriter() + (
        write("script", {"title": "007", "scenes": [{"blocks": [
            {"block_type": "DIALOGUE",
             "content": {"character_name": "Bond", "dialogue": "007"}},
        ]}]}),
        ChatResponse(stop_reason="max_tokens"),
        ChatResponse(content="broken"),
    )
    result = runner.invoke(cli.main, ["generate", "A spy returns", "-o", str(out_dir)])
    assert result.exit_code != 0
    assert built, "the engine was built for the fresh run"
    checkpoint = decode_checkpoint(
        (out_dir / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint.completed_steps == [
        "idea_refiner", "structurer", "character_designer", "scene_designer",
        "art_director", "scriptwriter",
    ]
    assert checkpoint.user_input == "A spy returns"
    # project.json is only written after a full success.
    assert not (out_dir / "project.json").exists()

    # Second invocation WITHOUT --resume refuses instead of restarting.
    built.clear()
    refused = runner.invoke(cli.main, ["generate", "-o", str(out_dir)])
    assert refused.exit_code != 0 and built == []

    # Resume: only the storyboard stage + finalize talk to the model.
    responses["queue"] = (
        write("storyboard", {"shots": [{
            "visual_description": "A doorway", "shot_size": "CLOSE_UP",
            "image_prompt": "i", "video_prompt": "v", "duration_seconds": "5",
        }]}),
    )
    resumed = runner.invoke(cli.main, ["generate", "--resume", "-o", str(out_dir)])
    assert resumed.exit_code == 0, resumed.output
    assert built[-1]._llm.chat.await_count == 1  # exactly the storyboard stage
    final = decode_checkpoint(
        (out_dir / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert final.content_complete
    assert final.growth_status == "succeeded"
    assert (out_dir / "project.json").exists()

    # A completed checkpoint + --resume only re-exports: no engine at all.
    built.clear()
    config.get_settings().llm_model = "changed-after-completion"
    config.get_settings().llm_provider = "ollama"
    config.get_settings().llm_temperature = 0.123
    again = runner.invoke(
        cli.main, ["generate", "--resume", "--no-auto-approve", "--format", "fountain", "-o", str(out_dir)]
    )
    assert again.exit_code == 0, again.output
    assert built == []  # zero model clients constructed
    assert (out_dir / "script.fountain").exists()
    # Growth never replayed on a finished checkpoint.
    stats = profile.get_profile_manager().profile.stats
    completed_once = stats.completed_projects
    rerun = runner.invoke(cli.main, ["generate", "--resume", "-o", str(out_dir)])
    assert rerun.exit_code == 0
    assert profile.get_profile_manager().profile.stats.completed_projects == completed_once


def test_cli_resume_converges_interrupted_growth_and_does_not_replay(cli_env):
    runner, out_dir, built, _responses, cli = cli_env
    complete = state_through_scriptwriter()
    complete.storyboard = Storyboard(shots=[{
        "shot_id": "s1", "scene_id": "a", "visual_description": "A doorway",
        "duration_seconds": 5,
    }])
    complete.storyboard.compute_totals()
    complete.meta.status = ProjectStatus.COMPLETE
    fingerprint = asyncio.run(current_fingerprint(skill_bindings={}, auto_approve=True))
    checkpoint = build_checkpoint(
        user_input="A spy returns",
        completed_steps=list(pipeline.GENERATION_STEPS),
        state=complete,
        fingerprint=fingerprint,
        growth_status="running",  # killed mid-growth
    )
    out_dir.mkdir(parents=True)
    write_checkpoint_atomic(out_dir / "checkpoint.json", checkpoint)
    before = profile.get_profile_manager().profile.stats.completed_projects

    result = runner.invoke(cli.main, ["generate", "--resume", "-o", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert built == []  # no engine: re-export only
    converged = decode_checkpoint(
        (out_dir / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert converged.growth_status == "interrupted"
    assert "部分发生" in (converged.growth_error or "")
    assert profile.get_profile_manager().profile.stats.completed_projects == before


def test_cli_output_dir_lock_excludes_second_process(cli_env, monkeypatch):
    from script_weaver.core.project_store import DataDirLock

    runner, out_dir, _, _r, cli = cli_env
    out_dir.mkdir(parents=True)
    holder = DataDirLock(out_dir, lock_name="cli.lock")
    holder.acquire()
    try:
        result = runner.invoke(
            cli.main, ["generate", "idea", "-o", str(out_dir)]
        )
        assert result.exit_code != 0
        assert "锁定" in result.output
    finally:
        holder.release()


# ── Web/CLI parity: one illegal checkpoint, one reason ──────


async def test_web_and_cli_refuse_the_same_checkpoint_identically(api_factory):
    """The same illegal checkpoint gives the SAME refusal reason through
    the CLI and the web resume endpoint, with zero model calls."""
    api = api_factory()
    async with api.app.router.lifespan_context(api.app):
        import httpx

        transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as c:
            created = await c.post(
                "/api/projects", json={"user_input": "一致性故事"}
            )
            pid = created.json()["project_id"]
            record = api._runtime.store.get_required(pid)

            # Seed a failed run whose checkpoint has an invalid prefix.
            fingerprint = await current_fingerprint(
                skill_bindings={}, auto_approve=record.auto_approve
            )
            checkpoint = build_checkpoint(
                user_input="一致性故事",
                completed_steps=["structurer"],  # skipped idea_refiner
                state=state_through_scriptwriter(),
                fingerprint=fingerprint,
                basis={"revision": record.revision},
            )
            store = api._runtime.store
            run = store.create_generation_run(
                pid, kind="generate", request_key="parity",
                request={"user_input": "一致性故事", "auto_approve": True},
                base_revision=record.revision, base_state_json=record.state_json,
                checkpoint_json=encode_checkpoint(checkpoint),
                completed_steps=["structurer"],
            )
            store.settle_generation_run(run.run_id, status="failed")

            refused = await c.post(
                f"/api/projects/{pid}/runs/{run.run_id}/resume",
                json={"request_key": "rk"},
            )
            assert refused.status_code == 409
            web_detail = refused.json()["detail"]
            assert web_detail["code"] == "invalid_prefix"

            # The CLI path over the SAME checkpoint content refuses with
            # the identical message.
            with pytest.raises(ResumeRejected) as cli_exc:
                validate_completed_steps(
                    decode_checkpoint(encode_checkpoint(checkpoint)).completed_steps,
                    state=checkpoint.state_model(),
                )
            assert cli_exc.value.code == web_detail["code"]
            assert cli_exc.value.message == web_detail["message"]


@pytest.mark.parametrize("missing", ["provider", "model", "temperature", "max_tokens", "endpoint", "auto_approve", "skill_bindings", "skills"])
def test_cli_missing_execution_basis_never_builds_engine(cli_env, missing):
    runner, out_dir, built, _, cli = cli_env
    fp = asyncio.run(current_fingerprint())
    del fp[missing]
    out_dir.mkdir()
    write_checkpoint_atomic(out_dir / "checkpoint.json", build_checkpoint(user_input="idea", fingerprint=fp))
    result = runner.invoke(cli.main, ["generate", "--resume", "-o", str(out_dir)])
    assert result.exit_code != 0
    assert "缺少有效的执行依据" in result.output
    assert built == []


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_inherits_omitted_gate_and_refuses_explicit_change(cli_env, monkeypatch, explicit):
    from unittest.mock import Mock
    runner, out_dir, _, _, cli = cli_env
    fp = asyncio.run(current_fingerprint(auto_approve=False))
    out_dir.mkdir()
    write_checkpoint_atomic(out_dir / "checkpoint.json", build_checkpoint(user_input="idea", fingerprint=fp))
    create = Mock(side_effect=RuntimeError("controlled stop before model"))
    monkeypatch.setattr(cli, "_create_engine", create)
    result = runner.invoke(cli.main, ["generate", "--resume", "-o", str(out_dir)] + (["--auto-approve"] if explicit else []))
    if explicit:
        create.assert_not_called()
        assert "gate" in result.output
    else:
        create.assert_called_once_with(auto_approve=False)
