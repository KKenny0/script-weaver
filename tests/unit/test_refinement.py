"""Refinement validation unit checks (issue #22).

Two layers, both offline:

* pure functions from ``script_weaver.core.refinement`` — the substantive
  diff (with its no-op counterexamples), target location and the strict
  shorten constraint;
* the real ``PipelineEngine.refine`` over a scripted model — proving the
  caller's state can never be polluted by a refused refine, that decisions
  are only recorded on success, and that equivalent user phrasings of the
  constrained request all flow through the deterministic check.
"""

import json

import pytest

from script_weaver.core import config, pipeline, refinement
from script_weaver.core.refinement import (
    RefineConstraintFailed,
    RefineNoMeaningfulChange,
    RefineNotExecutable,
    RefineTargetNotFound,
)
from script_weaver.core.types import (
    BasicInfo,
    Character,
    DecisionRecord,
    Outline,
    ProjectState,
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
from script_weaver.llm.providers import ChatResponse, ToolCall
from script_weaver.memory import profile


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config,
        "_settings",
        config.Settings(data_dir=tmp_path, skills_custom_dir=tmp_path / "skills"),
    )
    monkeypatch.setattr(profile, "_profile_manager", None)


# ── Shared fixture data ──────────────────────────────────

ORIGINAL_LAST = "请你一定要记住今晚的一切，无论将来发生什么事情，都绝对不要忘记我们在这里说过的话。"
SHORT_LAST = "记住今晚的一切。"


def sample_state() -> ProjectState:
    state = ProjectState(user_input="一个灯塔故事")
    state.outline = Outline(basic_info=BasicInfo(logline="灯塔守护者的告别"))
    state.refined_idea = "灯塔守护者的告别"
    state.script = Script(
        title="夜行灯塔",
        scenes=[
            ScriptScene(
                scene_id="sc_1",
                heading=ScriptSceneHeading(scene_number=1, location="天台", time_of_day="夜"),
                blocks=[
                    ScriptBlock(block_type=ScriptBlockType.ACTION,
                                content={"description": "阿芸推开铁门。"}),
                    ScriptBlock(block_type=ScriptBlockType.DIALOGUE,
                                content={"character_name": "阿芸", "dialogue": "灯不能灭。"}),
                ],
                characters_involved=["阿芸"],
            ),
            ScriptScene(
                scene_id="sc_2",
                heading=ScriptSceneHeading(scene_number=2, location="码头", time_of_day="黎明"),
                blocks=[
                    ScriptBlock(block_type=ScriptBlockType.ACTION,
                                content={"description": "老周握住阿芸的手。"}),
                    ScriptBlock(block_type=ScriptBlockType.DIALOGUE,
                                content={"character_name": "老周", "dialogue": ORIGINAL_LAST}),
                ],
                characters_involved=["老周"],
            ),
        ],
        notes="初稿备注",
    )
    state.storyboard = Storyboard(shots=[
        Shot(shot_id="shot_1", scene_id="sc_1", visual_description="天台", duration_seconds=2),
        Shot(shot_id="shot_2", scene_id="sc_2", visual_description="码头",
             dialogue=ORIGINAL_LAST, duration_seconds=3),
    ])
    state.storyboard.compute_totals()
    return state


def with_shortened(state: ProjectState) -> ProjectState:
    out = state.model_copy(deep=True)
    out.script.scenes[1].blocks[1].content["dialogue"] = SHORT_LAST
    return out


# ── Substantive diff: what counts and what never does ────


def test_dialogue_shortening_is_the_one_change():
    changes = refinement.diff_field_changes(sample_state(), with_shortened(sample_state()))
    assert [c.path for c in changes] == ["script.scenes[1].blocks[1].content.dialogue"]


def test_auto_id_regeneration_alone_is_not_substantive():
    before = sample_state()
    after = before.model_copy(deep=True)
    after.script.scenes[0].scene_id = "sc_regenerated"
    after.script.scenes[1].scene_id = "sc_also_new"
    after.storyboard.shots[0].shot_id = "shot_new"
    after.storyboard.shots[1].scene_id = "sc_also_new"
    assert refinement.diff_field_changes(before, after) == []


def test_id_regeneration_is_visible_to_the_strict_diff():
    before = sample_state()
    after = before.model_copy(deep=True)
    after.script.scenes[0].scene_id = "sc_regenerated"
    paths = {c.path for c in refinement.diff_field_changes(before, after, strict=True)}
    assert paths == {"script.scenes[0].scene_id"}


def test_notes_only_changes_are_not_substantive():
    before = sample_state()
    after = before.model_copy(deep=True)
    after.script.notes = "已按要求完成修改。"
    after.storyboard.notes = "分镜备注更新。"
    assert refinement.diff_field_changes(before, after) == []


def test_meta_memory_and_timestamps_are_never_compared():
    before = sample_state()
    after = before.model_copy(deep=True)
    after.meta.updated_at = "2099-01-01T00:00:00"
    after.meta.status = "complete"
    after.meta.title = "改名不算修改"
    after.memory.decisions.append(DecisionRecord(stage="scriptwriting", context="x"))
    assert refinement.diff_field_changes(before, after) == []


def test_nested_notes_like_key_still_counts_as_substantive():
    """The exclusion is surgical: a hand-added "notes" key inside a dialogue
    block's content is real content, not script.notes."""
    before = sample_state()
    after = before.model_copy(deep=True)
    after.script.scenes[0].blocks[1].content["notes"] = "口型备注"
    changes = refinement.diff_field_changes(before, after)
    assert [c.path for c in changes] == ["script.scenes[0].blocks[1].content.notes"]


def test_whitespace_only_differences_are_ignored_but_punctuation_is_not():
    before = sample_state()
    after = before.model_copy(deep=True)
    after.script.scenes[0].blocks[1].content["dialogue"] = "  灯不能灭。\n"
    assert refinement.diff_field_changes(before, after) == []

    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    changes = refinement.diff_field_changes(before, after)
    assert [c.path for c in changes] == ["script.scenes[0].blocks[1].content.dialogue"]


def test_list_reorder_is_a_change():
    before = sample_state()
    after = before.model_copy(deep=True)
    after.script.scenes[0], after.script.scenes[1] = (
        after.script.scenes[1], after.script.scenes[0])
    assert refinement.diff_field_changes(before, after)  # order matters


def test_reference_only_changes_are_not_substantive():
    before = sample_state()
    after = before.model_copy(deep=True)
    after.script.scenes[0].scene_design_id = "sd_new"
    assert refinement.diff_field_changes(before, after) == []


def test_build_change_summary_caps_and_previews():
    before = sample_state()
    after = before.model_copy(deep=True)
    long_text = "长" * 400
    after.script.scenes[0].blocks[1].content["dialogue"] = long_text
    after.outline.basic_info.logline = "新梗概"
    summary = refinement.build_change_summary(before, after)
    assert summary["total_changes"] == 2
    assert summary["changed_artifacts"] == ["outline", "script"]
    dialogue = next(c for c in summary["changes"] if c["path"].endswith("dialogue"))
    assert dialogue["before"] == "灯不能灭。"
    assert dialogue["after"] == "长" * 300 + "..."


# ── Target location ──────────────────────────────────────


def test_locate_last_dialogue_picks_last_non_empty_in_order():
    target = refinement.locate_last_dialogue(sample_state())
    assert (target.scene_index, target.block_index) == (1, 1)
    assert target.scene_id == "sc_2"
    assert target.original == ORIGINAL_LAST


def test_locate_last_dialogue_skips_whitespace_only_blocks():
    state = sample_state()
    state.script.scenes[1].blocks[1].content["dialogue"] = "   "
    target = refinement.locate_last_dialogue(state)
    assert (target.scene_index, target.block_index) == (0, 1)
    assert target.original == "灯不能灭。"


@pytest.mark.parametrize("remove", ["script", "dialogues"])
def test_locate_last_dialogue_without_target(remove):
    state = sample_state()
    if remove == "script":
        state.script = None
    else:
        for scene in state.script.scenes:
            scene.blocks = [b for b in scene.blocks
                            if b.block_type != ScriptBlockType.DIALOGUE]
    with pytest.raises(RefineTargetNotFound):
        refinement.locate_last_dialogue(state)


# ── Strict shorten constraint ────────────────────────────


def test_shorten_validation_accepts_the_textbook_result():
    before = sample_state()
    target = refinement.locate_last_dialogue(before)
    change = refinement.validate_shorten_result(before, with_shortened(before), target)
    assert change.after == SHORT_LAST


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d,                                        # nothing changed at all
        lambda d: _set_dialogue(d, "   "),                  # emptied
        lambda d: _set_dialogue(d, "  " + ORIGINAL_LAST),   # whitespace-only change
        lambda d: _set_dialogue(d, ORIGINAL_LAST + "嗯"),   # longer
        lambda d: {**d, "notes": "只改备注"},
        lambda d: {**d, "scenes": d["scenes"][:-1]},        # scene removed
    ],
)
def test_shorten_validation_rejects_violations(mutate):
    before = sample_state()
    target = refinement.locate_last_dialogue(before)
    after = before.model_copy(deep=True)
    after.script = Script.model_validate(mutate(before.script.model_dump()))
    with pytest.raises(RefineConstraintFailed):
        refinement.validate_shorten_result(before, after, target)


def _set_dialogue(script_dict: dict, text: str) -> dict:
    script_dict["scenes"][1]["blocks"][1]["content"]["dialogue"] = text
    return script_dict


# ── Routing shape validation ─────────────────────────────


def _routing_payload(data: dict) -> dict:
    return {"status": "success", "data": data}


def test_parse_routing_accepts_valid_decision_without_constraint():
    decision = refinement.parse_routing(
        _routing_payload({"next_agent": "scriptwriter", "action": "execute_agent"}))
    assert decision.next_agent == "scriptwriter"
    assert decision.constraint == "general"  # omitted → default


@pytest.mark.parametrize(
    "data",
    [
        {"next_agent": "scriptwriter", "action": "ask_user"},
        {"next_agent": "orchestrator", "action": "execute_agent"},
        {"next_agent": "reviewer", "action": "execute_agent"},
        {"next_agent": "scriptwriter", "action": "execute_agent", "constraint": "bogus"},
        {"next_agent": "structurer", "action": "execute_agent",
         "constraint": "shorten_last_dialogue"},
        {"next_agent": None, "action": "execute_agent"},
    ],
)
def test_parse_routing_rejects_unusable_decisions(data):
    with pytest.raises(RefineNotExecutable):
        refinement.parse_routing(_routing_payload(data))


def test_parse_routing_rejects_text_and_error_results():
    with pytest.raises(RefineNotExecutable):
        refinement.parse_routing({"status": "text_response", "data": {"text": "问用户"}})
    with pytest.raises(refinement.RefineExecutionError):
        refinement.parse_routing({"error": "LLM call failed: boom"})


def test_parse_routing_propagates_ask_user_message():
    with pytest.raises(RefineNotExecutable) as exc:
        refinement.parse_routing(_routing_payload({
            "next_agent": None, "action": "ask_user",
            "message_to_user": "要缩短哪一句对白？",
        }))
    assert "要缩短哪一句对白？" in str(exc.value)


# ── PipelineEngine.refine over a scripted model ──────────


class ScriptedLLM:
    """Routing answers are one-shot JSON; agent calls submit one artifact."""

    def __init__(self, routing: dict, artifact=None, *, artifact_type: str = "script"):
        self.routing = routing
        self.artifact = artifact
        self.artifact_type = artifact_type
        self.agent_prompts: list[str] = []
        self.tool_submissions = 0

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
        if tools is None:
            return ChatResponse(content=json.dumps(self.routing, ensure_ascii=False))
        self.agent_prompts.append(messages[-1]["content"])
        self.tool_submissions += 1
        artifact = self.artifact() if callable(self.artifact) else self.artifact
        return ChatResponse(
            stop_reason="tool_use",
            tool_calls=[ToolCall(
                id="call_scripted", name="write_artifact",
                arguments={"artifact_type": self.artifact_type,
                           "content": json.dumps(artifact, ensure_ascii=False)},
            )],
        )


ROUTE_SHORTEN = {"next_agent": "scriptwriter", "action": "execute_agent",
                 "constraint": "shorten_last_dialogue", "reason": "受限修改"}
ROUTE_GENERAL = {"next_agent": "scriptwriter", "action": "execute_agent",
                 "constraint": "general", "reason": "普通修改"}


def make_engine(llm) -> pipeline.PipelineEngine:
    return pipeline.PipelineEngine(llm_client=llm, auto_approve_gates=True)


@pytest.mark.parametrize(
    "message",
    [
        "只把结尾最后一句对白改得更简短，保持人物、地点和主要情节不变",
        "仅缩短剧本末尾那句对白，其他内容都不要动",
        "把剧本最后一句对白弄简短一点，其余保持原样",
    ],
    ids=["原始表述", "等义表述1", "等义表述2"],
)
async def test_equivalent_phrasings_flow_through_constrained_check(message):
    """Routing recognizes the constraint (semantic, model-side); the code
    then enforces the deterministic check for every equivalent phrasing."""
    state = sample_state()
    llm = ScriptedLLM(ROUTE_SHORTEN, with_shortened(state).script.model_dump(mode="json"))
    engine = make_engine(llm)

    result = await engine.refine(state, message)

    assert result.script.scenes[1].blocks[1].content["dialogue"] == SHORT_LAST
    # The agent received the position, the original text and the full script.
    prompt = llm.agent_prompts[0]
    assert "sc_2" in prompt and ORIGINAL_LAST in prompt
    assert "唯一允许修改的字段" in prompt
    assert "scene" in json.dumps(result.script.model_dump())  # sanity: script carried


async def test_refine_failure_never_pollutes_caller_state():
    state = sample_state()
    updated_at_before = state.meta.updated_at
    llm = ScriptedLLM(ROUTE_SHORTEN, state.script.model_dump(mode="json"))  # unchanged script
    engine = make_engine(llm)

    with pytest.raises(RefineConstraintFailed):
        await engine.refine(state, "只缩短最后一句对白")

    # Caller's snapshot: content, memory and timestamps all pristine.
    assert state.script.scenes[1].blocks[1].content["dialogue"] == ORIGINAL_LAST
    assert state.script.notes == "初稿备注"
    assert state.memory.decisions == []
    assert state.meta.updated_at == updated_at_before
    assert llm.tool_submissions == 1  # the agent ran; only its result was refused


async def test_decision_recorded_only_on_success():
    state = sample_state()
    llm = ScriptedLLM(ROUTE_SHORTEN, with_shortened(state).script.model_dump(mode="json"))
    engine = make_engine(llm)

    result = await engine.refine(state, "只缩短最后一句对白")

    assert len(result.memory.decisions) == 1
    decision = result.memory.decisions[0]
    assert decision.stage == "scriptwriting"
    assert decision.action.value == "modify"
    assert state.memory.decisions == []  # the caller snapshot stays pristine
    assert result.meta.updated_at  # touched on the returned copy only


async def test_general_refine_without_change_raises_no_meaningful_change():
    state = sample_state()
    llm = ScriptedLLM(ROUTE_GENERAL, state.script.model_dump(mode="json"))
    engine = make_engine(llm)

    with pytest.raises(RefineNoMeaningfulChange):
        await engine.refine(state, "重写剧本")
    assert state.memory.decisions == []


async def test_general_refine_to_structurer_changes_outline():
    state = sample_state()
    artifact = {
        "basic_info": {"logline": "全新的梗概", "genre": "剧情", "theme": "告别",
                       "tone": "克制", "episode_count": 1},
        "plot_outline": [{
            "sequence_number": 1, "title": "守灯", "synopsis": "阿芸守到黎明。",
            "emotional_arc": "克制→释放", "key_characters": ["阿芸"], "setting": "灯塔",
        }],
    }
    llm = ScriptedLLM(
        {"next_agent": "structurer", "action": "execute_agent", "constraint": "general"},
        artifact, artifact_type="outline")
    engine = make_engine(llm)

    result = await engine.refine(state, "把梗概改得更抓人")

    assert result.outline.basic_info.logline == "全新的梗概"
    assert state.outline.basic_info.logline == "灯塔守护者的告别"


async def test_refine_executes_only_one_routing_and_one_agent_flow():
    state = sample_state()

    class CountingLLM(ScriptedLLM):
        routing_calls = 0

        async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
            if tools is None:
                type(self).routing_calls += 1
            return await super().chat(messages, tools, temperature, max_tokens)

    llm = CountingLLM(ROUTE_SHORTEN, with_shortened(state).script.model_dump(mode="json"))
    engine = make_engine(llm)
    await engine.refine(state, "只缩短最后一句对白")

    assert CountingLLM.routing_calls == 1
    assert llm.tool_submissions == 1


async def test_refine_model_error_is_sanitized_execution_error():
    class ExplodingLLM(ScriptedLLM):
        async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
            if tools is None:
                raise RuntimeError("api.example.com refused (key=sk-hush)")
            return await super().chat(messages, tools, temperature, max_tokens)

    state = sample_state()
    engine = make_engine(ExplodingLLM(ROUTE_SHORTEN, {}))

    with pytest.raises(refinement.RefineExecutionError) as exc:
        await engine.refine(state, "只缩短最后一句对白")
    assert "sk-hush" not in str(exc.value)


# ── Review round 2 regressions ──────────────────────────
# Four defects found in review of 0cc162e: strict mode must compare raw
# field values, general refines must preserve reference integrity, routing
# fields must be type-checked before enum membership, and the frontend must
# not write a stale refresh-failure outcome into another project's chat
# (covered in web/tests/e2e/refinement-outcome.spec.ts).


def test_strict_diff_flags_whitespace_only_id_change():
    """Raw comparison: an ID padded with whitespace is a real difference."""
    before = sample_state()
    after = before.model_copy(deep=True)
    after.script.scenes[0].scene_id = f" {after.script.scenes[0].scene_id} "
    paths = {c.path for c in refinement.diff_field_changes(before, after, strict=True)}
    assert paths == {"script.scenes[0].scene_id"}
    # The evidence diff still treats whitespace-only changes as no-ops.
    assert refinement.diff_field_changes(before, after) == []


def test_shorten_with_whitespace_padded_other_id_is_rejected():
    before = sample_state()
    target = refinement.locate_last_dialogue(before)
    after = with_shortened(before)
    after.script.scenes[0].scene_id = " sc_1 "  # out-of-bounds raw change
    with pytest.raises(RefineConstraintFailed):
        refinement.validate_shorten_result(before, after, target)


def test_shorten_target_judgement_uses_stripped_text():
    """Only the target dialogue's non-empty/different/shorter rules use the
    stripped string; legitimate punctuation-preserving rewrites pass."""
    before = sample_state()
    target = refinement.locate_last_dialogue(before)
    after = before.model_copy(deep=True)
    # Same shortened text with harmless surrounding whitespace must pass.
    after.script.scenes[1].blocks[1].content["dialogue"] = f"  {SHORT_LAST}  "
    change = refinement.validate_shorten_result(before, after, target)
    assert change.after.strip() == SHORT_LAST


def _with_highlights(state: ProjectState) -> ProjectState:
    out = state.model_copy(deep=True)
    out.visual_highlights = [
        VisualHighlight(title="亮点1", description="码头告别",
                        related_shot_ids=["shot_1", "shot_2"]),
    ]
    return out


def test_general_rebuilt_referenced_scene_id_is_rejected():
    """Substantive dialogue edit + rebuilt scene_id: storyboard still points
    at the old ID, so the export loses the scene→character link."""
    before = _with_highlights(sample_state())
    after = before.model_copy(deep=True)
    after.script.scenes[0].scene_id = "sc_rebuilt"
    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    with pytest.raises(RefineConstraintFailed) as exc:
        refinement.validate_general_result(before, after, "scriptwriter")
    assert "storyboard.shots[0].scene_id" in str(exc.value)


def test_general_rebuilt_shot_id_breaking_highlights_is_rejected():
    before = _with_highlights(sample_state())
    after = before.model_copy(deep=True)
    after.storyboard.shots[0].shot_id = "shot_rebuilt"
    after.storyboard.shots[0].visual_description = "新的画面描述"
    with pytest.raises(RefineConstraintFailed) as exc:
        refinement.validate_general_result(before, after, "storyboard_artist")
    assert "visual_highlights[0].related_shot_ids[0]" in str(exc.value)


def test_general_rebuilt_scene_design_id_is_rejected():
    before = sample_state()
    before.scenes = [SceneDesign(id="sd_1", name="天台", environment="夜风中的天台")]
    before.script.scenes[0].scene_design_id = "sd_1"
    after = before.model_copy(deep=True)
    after.scenes = [SceneDesign(id="sd_rebuilt", name="天台", environment="夜风中的天台，加了旗杆")]
    with pytest.raises(RefineConstraintFailed) as exc:
        refinement.validate_general_result(before, after, "scene_designer")
    assert "script.scenes[0].scene_design_id" in str(exc.value)


def test_general_duplicate_referenced_scene_id_is_rejected():
    """Two scenes sharing one ID make the storyboard's reference ambiguous."""
    before = _with_highlights(sample_state())
    after = before.model_copy(deep=True)
    after.script.scenes[1].scene_id = "sc_1"  # duplicate of scene 0; shots ref both
    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    with pytest.raises(RefineConstraintFailed) as exc:
        refinement.validate_general_result(before, after, "scriptwriter")
    assert "ambiguous" in str(exc.value)


def test_general_preexisting_dangling_ref_does_not_block_unrelated_edit():
    before = _with_highlights(sample_state())
    before.storyboard.shots[1].scene_id = "sc_missing"  # historical defect
    after = before.model_copy(deep=True)
    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    changes = refinement.validate_general_result(before, after, "scriptwriter")
    assert any(c.path.endswith("dialogue") for c in changes)


def test_general_intact_references_still_pass():
    before = _with_highlights(sample_state())
    after = before.model_copy(deep=True)
    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    changes = refinement.validate_general_result(before, after, "scriptwriter")
    assert len(changes) == 1


# ── Review round 3: stable defect identity ────────────────────


def _with_designs(state: ProjectState) -> ProjectState:
    """Attach scene designs: sc_1→old_missing (historical defect), sc_2→sd_ok."""
    out = state.model_copy(deep=True)
    out.scenes = [SceneDesign(id="sd_ok", name="码头", environment="夜色中的码头")]
    out.script.scenes[0].scene_design_id = "old_missing"
    out.script.scenes[1].scene_design_id = "sd_ok"
    return out


def test_historical_design_defect_reorder_does_not_block_unrelated_edit():
    """A pre-existing dangling design ref that moves with its owner (the
    scene is reordered) is the same defect — it must not block a real edit."""
    before = _with_designs(sample_state())
    after = before.model_copy(deep=True)
    after.script.scenes.reverse()  # sc_1 (old_missing) moves to index 1
    after.script.scenes[0].blocks[1].content["dialogue"] = "请一定记住今晚。"
    changes = refinement.validate_general_result(before, after, "scriptwriter")
    assert any(c.path.endswith("dialogue") for c in changes)


def test_historical_shot_defect_reorder_does_not_block_storyboard_edit():
    """Same rule on the storyboard→script relation: shot_2's historical
    dangling scene ref stays historical when the shots are reordered."""
    before = _with_highlights(sample_state())
    before.storyboard.shots[1].scene_id = "sc_missing"
    after = before.model_copy(deep=True)
    after.storyboard.shots.reverse()  # shot_2 (defect) moves to index 0
    after.storyboard.shots[0].visual_description = "新的画面描述"
    changes = refinement.validate_general_result(before, after, "storyboard_artist")
    assert any(c.path.endswith("visual_description") for c in changes)


def test_related_shot_ids_reorder_keeps_historical_exemption():
    """A highlight's related_shot_ids list reorder is not a new reference:
    identity follows the highlight ID and the referenced value, not the
    position inside the list."""
    before = _with_highlights(sample_state())
    before.visual_highlights[0].related_shot_ids = ["shot_missing", "shot_1"]
    after = before.model_copy(deep=True)
    after.visual_highlights[0].related_shot_ids = ["shot_1", "shot_missing"]
    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    changes = refinement.validate_general_result(before, after, "scriptwriter")
    assert any(c.path.endswith("dialogue") for c in changes)


def test_repair_one_break_other_at_same_index_is_rejected():
    """The reviewed leak: fix sc_1's ref while breaking sc_2's — after a
    swap both snapshots show a dangling ref at scenes[0], but sc_2's is a
    new defect and must fail the request."""
    before = _with_designs(sample_state())
    after = before.model_copy(deep=True)
    after.script.scenes.reverse()
    after.script.scenes[0].scene_design_id = "new_missing"  # sc_2 breaks
    after.script.scenes[1].scene_design_id = "sd_ok"        # sc_1 repaired
    after.script.scenes[0].blocks[1].content["dialogue"] = "请一定记住今晚。"
    with pytest.raises(RefineConstraintFailed) as exc:
        refinement.validate_general_result(before, after, "scriptwriter")
    assert "scene_design_id" in str(exc.value)


def test_same_object_new_dangling_value_is_a_new_defect():
    """old_missing → new_missing on the same scene is a new broken
    reference, not the historical one under a different name."""
    before = _with_designs(sample_state())
    after = before.model_copy(deep=True)
    after.script.scenes[0].scene_design_id = "new_missing"
    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    with pytest.raises(RefineConstraintFailed):
        refinement.validate_general_result(before, after, "scriptwriter")


def test_duplicate_owner_id_cannot_borrow_historical_exemption():
    """A second object sharing sc_1's ID adds a second bad reference —
    local counting, not mere presence, decides the exemption."""
    before = _with_designs(sample_state())
    before.storyboard.shots[0].scene_id = ""  # isolate: no shot refs sc_1
    after = before.model_copy(deep=True)
    extra = after.script.scenes[0].model_copy(deep=True)  # same scene_id sc_1
    after.script.scenes.append(extra)
    after.script.scenes[0].blocks[1].content["dialogue"] = "请一定记住今晚。"
    with pytest.raises(RefineConstraintFailed):
        refinement.validate_general_result(before, after, "scriptwriter")


def test_new_object_cannot_borrow_historical_exemption():
    """A freshly added scene with its own dangling ref is new, even though
    an identical-looking historical defect already exists elsewhere."""
    before = _with_designs(sample_state())
    after = before.model_copy(deep=True)
    extra = after.script.scenes[1].model_copy(deep=True)
    extra.scene_id = "sc_new"
    extra.scene_design_id = "old_missing"  # same target value, new owner
    after.script.scenes.append(extra)
    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    with pytest.raises(RefineConstraintFailed):
        refinement.validate_general_result(before, after, "scriptwriter")


# ── Review round 3: character/scene-design ID rebuilds ─────────


def test_character_and_scene_design_id_rebuild_alone_is_not_substantive():
    """Character.id / SceneDesign.id regenerate when the model omits them;
    the echo is otherwise identical, so nothing substantive changed."""
    before = sample_state()
    before.characters = [Character(id="char_1", name="阿芸", personality="坚韧")]
    before.scenes = [SceneDesign(id="sd_1", name="天台", environment="夜风中的天台")]
    after = before.model_copy(deep=True)
    after.characters[0].id = "char_regenerated"
    after.scenes[0].id = "sd_regenerated"
    assert refinement.diff_field_changes(before, after) == []


def test_character_and_scene_design_id_rebuild_is_visible_to_strict_diff():
    before = sample_state()
    before.characters = [Character(id="char_1", name="阿芸", personality="坚韧")]
    before.scenes = [SceneDesign(id="sd_1", name="天台", environment="夜风中的天台")]
    after = before.model_copy(deep=True)
    after.characters[0].id = "char_regenerated"
    after.scenes[0].id = "sd_regenerated"
    paths = {c.path for c in refinement.diff_field_changes(before, after, strict=True)}
    assert paths == {"characters[0].id", "scenes[0].id"}


def test_general_character_id_only_rebuild_is_no_meaningful_change():
    before = sample_state()
    before.characters = [Character(id="char_1", name="阿芸", personality="坚韧")]
    after = before.model_copy(deep=True)
    after.characters[0].id = "char_regenerated"
    with pytest.raises(RefineNoMeaningfulChange):
        refinement.validate_general_result(before, after, "character_designer")


def test_general_scene_design_id_only_rebuild_is_no_meaningful_change():
    before = sample_state()
    before.scenes = [SceneDesign(id="sd_1", name="天台", environment="夜风中的天台")]
    after = before.model_copy(deep=True)
    after.scenes[0].id = "sd_regenerated"
    with pytest.raises(RefineNoMeaningfulChange):
        refinement.validate_general_result(before, after, "scene_designer")


def test_general_character_content_change_with_kept_id_succeeds():
    """Positive control: a real personality edit with the ID intact is a
    legitimate characters refinement — the exclusion must not hide it."""
    before = sample_state()
    before.characters = [Character(id="char_1", name="阿芸", personality="坚韧")]
    after = before.model_copy(deep=True)
    after.characters[0].personality = "外冷内热，坚韧"
    changes = refinement.validate_general_result(before, after, "character_designer")
    assert [c.path for c in changes] == ["characters[0].personality"]


def test_general_scene_design_content_change_with_reference_intact_succeeds():
    """Positive control: a real environment edit on a referenced design
    (ID kept) still passes both the diff and the integrity check."""
    before = sample_state()
    before.scenes = [SceneDesign(id="sd_1", name="天台", environment="夜风中的天台")]
    before.script.scenes[0].scene_design_id = "sd_1"
    after = before.model_copy(deep=True)
    after.scenes[0].environment = "夜风中的天台，加了旗杆"
    changes = refinement.validate_general_result(before, after, "scene_designer")
    assert [c.path for c in changes] == ["scenes[0].environment"]


# ── Review round 4: duplicate owner IDs must not transfer exemptions ──


def _design_relation_transfer():
    """The reviewed repro shape: repair A, then let B wear A's scene_id and
    reproduce the same defect — identity and count both unchanged."""
    before = _with_designs(sample_state())
    before.storyboard = None  # repro has no storyboard yet
    after = before.model_copy(deep=True)
    after.script.scenes[0].scene_design_id = "sd_ok"        # A repaired
    after.script.scenes[1].scene_id = "sc_1"                # B steals A's ID
    after.script.scenes[1].scene_design_id = "old_missing"  # defect moves to B
    after.script.scenes[1].blocks[1].content["dialogue"] = "请一定记住今晚。"
    return before, after, "scriptwriter"


def _shot_relation_transfer():
    before = sample_state()
    before.storyboard.shots[0].scene_id = "sc_missing"  # historical dangling
    after = before.model_copy(deep=True)
    after.storyboard.shots[0].scene_id = "sc_2"             # shot_1 repaired
    after.storyboard.shots[1].shot_id = "shot_1"            # shot_2 steals the ID
    after.storyboard.shots[1].scene_id = "sc_missing"       # defect transferred
    after.storyboard.shots[1].visual_description = "新的画面描述"
    return before, after, "storyboard_artist"


def _highlight_relation_transfer():
    before = sample_state()
    before.visual_highlights = [
        VisualHighlight(id="vh_1", title="坏", description="旧亮点",
                        related_shot_ids=["shot_missing"]),
        VisualHighlight(id="vh_2", title="好", description="新亮点",
                        related_shot_ids=["shot_1"]),
    ]
    after = before.model_copy(deep=True)
    after.visual_highlights[0].related_shot_ids = ["shot_1"]      # vh_1 repaired
    after.visual_highlights[1].id = "vh_1"                        # vh_2 steals the ID
    after.visual_highlights[1].related_shot_ids = ["shot_missing"]
    after.script.scenes[0].blocks[1].content["dialogue"] = "灯，不能灭。"
    return before, after, "scriptwriter"


@pytest.mark.parametrize(
    "setup",
    [_design_relation_transfer, _shot_relation_transfer, _highlight_relation_transfer],
    ids=["scene→design", "shot→scene", "highlight→shot"],
)
def test_defect_transfer_via_owner_id_duplication_is_rejected(setup):
    """A defect whose identity and count match a historical one is NOT
    historical when its owner ID is duplicated by this modification — the
    defect may simply have moved to another object wearing the ID."""
    before, after, agent = setup()
    with pytest.raises(RefineConstraintFailed) as exc:
        refinement.validate_general_result(before, after, agent)
    message = str(exc.value)
    assert "dangling" in message or "ambiguous" in message
    assert "重复" in message  # the rejection names the duplicate owner ID


def test_changed_duplicate_group_cannot_swap_defect_between_members():
    """Inside a historically duplicated group the members cannot be told
    apart by ID, so once the group's content changes the historical
    exemption no longer applies — swapping the defect between members
    (repair one, break the other) must be refused."""
    before = _with_designs(sample_state())
    before.storyboard = None
    before.script.scenes[0].scene_id = "sc_dup"  # A: old_missing (historical)
    extra = before.script.scenes[1].model_copy(deep=True)
    extra.scene_id = "sc_dup"
    extra.scene_design_id = "sd_ok"  # B: fine — group sc_dup ×2
    before.script.scenes.append(extra)
    after = before.model_copy(deep=True)
    after.script.scenes[0].scene_design_id = "sd_ok"        # A repaired
    after.script.scenes[2].scene_design_id = "old_missing"  # B now defective
    after.script.scenes[2].blocks[1].content["dialogue"] = "记住今晚。"
    with pytest.raises(RefineConstraintFailed):
        refinement.validate_general_result(before, after, "scriptwriter")


def test_unchanged_duplicate_group_does_not_block_outside_edit():
    """Positive control: a historically duplicated group left byte-identical
    keeps its exemption — unrelated edits elsewhere stay allowed."""
    before = _with_designs(sample_state())
    before.storyboard = None
    before.script.scenes[0].scene_id = "sc_dup"  # both members defective
    before.script.scenes.append(before.script.scenes[0].model_copy(deep=True))
    after = before.model_copy(deep=True)
    # sc_2 sits outside the group; only its dialogue changes.
    after.script.scenes[1].blocks[1].content["dialogue"] = "请一定记住今晚。"
    changes = refinement.validate_general_result(before, after, "scriptwriter")
    assert any(c.path.endswith("dialogue") for c in changes)


def test_shrunk_duplicate_group_with_unchanged_survivor_keeps_exemption():
    """Removing one member of a duplicate group is an improvement, not a
    transfer: the surviving (byte-identical) object's defect stays
    historical and must not block an unrelated edit."""
    before = _with_designs(sample_state())
    before.storyboard = None
    before.script.scenes[0].scene_id = "sc_dup"  # group sc_dup ×2, both defective
    before.script.scenes.append(before.script.scenes[0].model_copy(deep=True))
    after = before.model_copy(deep=True)
    del after.script.scenes[2]  # dedup improvement: only one sc_dup remains
    after.script.scenes[1].blocks[1].content["dialogue"] = "请一定记住今晚。"
    changes = refinement.validate_general_result(before, after, "scriptwriter")
    assert any(c.path.endswith("dialogue") for c in changes)


@pytest.mark.parametrize(
    "data",
    [
        {"next_agent": "scriptwriter", "action": []},
        {"next_agent": "scriptwriter", "action": {}},
        {"next_agent": "scriptwriter", "action": 1},
        {"next_agent": "scriptwriter", "action": None},
        {"next_agent": "scriptwriter", "action": "execute_agent", "constraint": []},
        {"next_agent": "scriptwriter", "action": "execute_agent", "constraint": {}},
        {"next_agent": "scriptwriter", "action": "execute_agent", "constraint": 2},
        {"next_agent": "scriptwriter", "action": "execute_agent", "constraint": None},
    ],
)
def test_parse_routing_rejects_non_string_action_or_constraint(data):
    """Type check before enum membership: unhashable values must not crash
    into a 500 — they are ordinary unusable routing decisions."""
    with pytest.raises(RefineNotExecutable):
        refinement.parse_routing(_routing_payload(data))
