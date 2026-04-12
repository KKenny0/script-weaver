"""Unit tests for core Pydantic data models."""

import pytest
from script_weaver.core.types import (
    ArtStyle,
    Character,
    Outline,
    ProjectState,
    Script,
    SceneDesign,
    Shot,
    Skill,
    Storyboard,
    VisualHighlight,
)


class TestBasicModels:
    """Test basic model creation and validation."""

    def test_project_state_default(self):
        state = ProjectState()
        assert state.meta.status == "idea_input"
        assert state.user_input == ""
        assert state.memory.decisions == []
        assert state.skill_bindings == {}

    def test_project_state_with_input(self):
        state = ProjectState(user_input="一个穿越时空的故事")
        assert state.user_input == "一个穿越时空的故事"
        assert state.current_stage_status().value == "refining"

    def test_character_creation(self):
        char = Character(
            name="林清泉",
            role="protagonist",
            appearance="白衣胜雪，眉目如画，气质清冷",
            personality="外冷内热，内心善良",
        )
        assert char.id.startswith("char_")
        assert char.role.value == "protagonist"
        assert "白衣" in char.appearance

    def test_scene_design_creation(self):
        scene = SceneDesign(
            name="九王洗血后院",
            location_type="exterior",
            environment="古色古香的庭院，月光如水",
            mood="阴森肃杀",
        )
        assert scene.id.startswith("scene_")

    def test_art_style_creation(self):
        style = ArtStyle(
            overall_style="真人古风",
            color_palette_primary=["#1a1a2e", "#16213e"],
            lighting_style="冷暖对比强烈",
        )
        assert style.overall_style == "真人古风"
        assert len(style.color_palette_primary) == 2


class TestScriptModels:
    """Test script-related models."""

    def test_outline_creation(self):
        outline = Outline()
        outline.basic_info.logline = "一个穿越者改变命运的故事"
        outline.basic_info.genre = "古装玄幻"
        outline.basic_info.episode_count = 1
        assert outline.basic_info.logline is not None

    def test_shot_creation(self):
        shot = Shot(
            shot_id="shot_test_001",  # Explicit ID for deterministic testing
            scene_id="sc_001",
            sequence_number=1,
            visual_description="月光下的庭院，白衣女子独立",
            duration_seconds=4.0,
        )
        assert shot.shot_id == "shot_test_001"
        assert shot.shot_size.value == "medium_shot"
        assert shot.camera_movement.value == "static"
        assert shot.duration_seconds == 4.0

    def test_shot_auto_id(self):
        """Test that Shot can be created without explicit ID (uses default)."""
        shot = Shot(
            scene_id="sc_002",
            sequence_number=2,
            visual_description="测试镜头",
            duration_seconds=3.0,
        )
        # Shot should create successfully with default ID
        assert shot.scene_id == "sc_002"
        assert shot.duration_seconds == 3.0

    def test_storyboard_compute_totals(self):
        sb = Storyboard(
            shots=[
                Shot(duration_seconds=3.0),
                Shot(duration_seconds=5.0),
                Shot(duration_seconds=2.0),
            ]
        )
        sb.compute_totals()
        assert sb.total_shot_count == 3
        assert sb.total_estimated_duration == 10.0


class TestSkillModel:
    """Test skill model."""

    def test_skill_creation(self):
        skill = Skill(
            id="test-skill",
            name="Test Skill",
            description="A test skill",
            prompt_injection="Always do X",
        )
        assert skill.source_format.value == "native_yaml"
        assert skill.version == "1.0"

    def test_skill_extra_fields_forbidden(self):
        """Skill model should not allow extra fields."""
        with pytest.raises(Exception):  # ValidationError
            Skill(
                id="test",
                name="Test",
                unknown_field="should_fail",
            )


class TestProjectStateLifecycle:
    """Test project state status progression."""

    def test_empty_state(self):
        state = ProjectState()
        assert state.current_stage_status().value == "idea_input"

    def test_with_refined_idea(self):
        state = ProjectState(refined_idea="精炼后的概念")
        status = state.current_stage_status().value
        assert status == "structured"

    def test_with_outline(self):
        state = ProjectState(outline=Outline())
        status = state.current_stage_status().value
        assert status == "designing"

    def test_with_script(self):
        state = ProjectState(script=Script())
        status = state.current_stage_status().value
        assert status == "storyboarding"

    def test_complete_state(self):
        from script_weaver.core.types import Storyboard
        state = ProjectState(
            storyboard=Storyboard(),
            visual_highlights=[VisualHighlight()],
        )
        status = state.current_stage_status().value
        assert status == "complete"

    def test_touch_updates_timestamp(self):
        import time
        state = ProjectState()
        old_time = state.meta.updated_at
        time.sleep(0.01)  # Small delay
        state.touch()
        assert state.meta.updated_at != old_time


class TestMemoryAndGrowth:
    """Test project memory and decision recording."""

    def test_record_decision(self):
        from script_weaver.core.types import DecisionRecord, UserAction
        state = ProjectState()
        decision = DecisionRecord(
            stage="structuring",
            context="Proposed Save the Cat structure",
            action=UserAction.ACCEPT,
        )
        state.memory.record_decision(decision)
        assert len(state.memory.decisions) == 1
        assert state.memory.decisions[0].action == UserAction.ACCEPT

    def test_add_pattern(self):
        from script_weaver.core.types import Pattern as PatternModel, PatternType
        state = ProjectState()
        pat = PatternModel(
            type=PatternType.STYLE_PREFERENCE,
            description="User prefers emotional dialogue",
            source_project="test-1",
        )
        state.memory.add_pattern(pat)
        assert len(state.memory.extracted_patterns) == 1
        assert state.memory.extracted_patterns[0].description == "User prefers emotional dialogue"

    def test_pattern_boost_confidence(self):
        from script_weaver.core.types import Pattern as PatternModel
        p = PatternModel(confidence=0.5)
        p.boost_confidence(0.1)
        assert p.confidence == 0.6
        assert p.usage_count == 1

    def test_pattern_boost_multiple(self):
        from script_weaver.core.types import Pattern as PatternModel
        p = PatternModel(confidence=0.3)
        p.boost_confidence(0.1)  # 0.4
        p.boost_confidence(0.1)  # 0.5
        p.boost_confidence(0.2)  # 0.7 (capped at 1.0 would be 0.9, but 0.7 < 1.0)
        assert p.confidence == 0.7
        assert p.usage_count == 3
