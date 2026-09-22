"""All 9 agent implementations.

Each agent extends BaseAgent (tool-use loop) or SimpleAgent (one-shot),
with its own system prompt from system_prompts.py and skill injection support.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from script_weaver.agents.base import BaseAgent, SimpleAgent
from script_weaver.core.types import (
    ArtStyle,
    Character,
    DecisionRecord,
    Outline,
    ProjectState,
    Script,
    SceneDesign,
    Storyboard,
    UserAction,
)
from script_weaver.llm.client import LLMClient
from script_weaver.memory.profile import get_profile_manager
from script_weaver.prompts.system_prompts import (
    ART_DIRECTOR_PROMPT,
    CHARACTER_DESIGNER_PROMPT,
    IDEA_REFINER_PROMPT,
    ORCHESTRATOR_PROMPT,
    REVIEWER_PROMPT,
    SCENE_DESIGNER_PROMPT,
    SCRIPT_WRITER_PROMPT,
    STORYBOARD_ARTIST_PROMPT,
    STRUCTURER_PROMPT,
)
from script_weaver.skills.registry import AgentSkillContext, SkillRegistry

logger = logging.getLogger(__name__)


def _inject_skills(
    base_prompt: str,
    stage: str,
    skill_registry: SkillRegistry,
    bindings: dict[str, dict],
) -> str:
    """Build full system prompt with skill injections."""
    ctx: AgentSkillContext | None = None
    if skill_registry and bindings:
        try:
            ctx = skill_registry.build_agent_context(stage, bindings)
        except Exception as e:
            logger.warning(f"Skill context build failed: {e}")

    if ctx and ctx.system_additions:
        return base_prompt + "\n\n## Active Skills\n" + ctx.system_additions
    return base_prompt


def _add_user_profile_hints(base_prompt: str, stage: str) -> str:
    """Append user preference hints to system prompt."""
    try:
        pm = get_profile_manager()
        hints = pm.get_stage_hints(stage)
        if hints:
            return base_prompt + f"\n\n## User Preferences (Learned)\n{hints}\n"
    except Exception:
        pass
    return base_prompt


# ────────────────────────────────────────────────────────
# 1. IdeaRefiner — SimpleAgent (one-shot)
# ────────────────────────────────────────────────────────


class IdeaRefiner(BaseAgent):
    """Takes raw user idea → produces refined story concept."""

    name = "idea_refiner"
    stage = "ideation"
    system_prompt = IDEA_REFINER_PROMPT
    output_artifact_type = "refined_idea"

    def _build_system_prompt(self, state: ProjectState) -> str:
        prompt = self.system_prompt
        # Inject skills
        if hasattr(state, 'skill_bindings') and state.skill_bindings:
            from script_weaver.skills.registry import SkillRegistry
            # Lazy import to avoid circular issues; registry is set at runtime
            pass
        return prompt

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        if user_message:
            return (
                f"请将以下创意想法精炼为完整的故事概念：\n\n{user_message}"
            )
        return (
            f"请将以下创意想法精炼为完整的故事概念：\n\n{state.user_input}"
        )


# ────────────────────────────────────────────────────────
# 2. Structurer — BaseAgent (tool-use loop)
# ────────────────────────────────────────────────────────


class Structurer(BaseAgent):
    """Takes refined idea → produces structured outline."""

    name = "structurer"
    stage = "structuring"
    system_prompt = STRUCTURER_PROMPT
    output_artifact_type = "outline"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._skill_registry: SkillRegistry | None = None

    def set_skill_registry(self, registry: SkillRegistry) -> None:
        self._skill_registry = registry

    def _build_system_prompt(self, state: ProjectState) -> str:
        prompt = _inject_skills(
            self.system_prompt, self.stage,
            self._skill_registry, state.skill_bindings,
        )
        return _add_user_profile_hints(prompt, self.stage)

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        refined = state.refined_idea or state.user_input
        base = (
            f"请基于以下精炼后的故事概念，构建完整的故事结构大纲。\n\n"
            f"=== 故事概念 ===\n{refined}\n"
        )
        if user_message:
            base += f"\n\n用户额外指示: {user_message}"
        return base


# ────────────────────────────────────────────────────────
# 3. CharacterDesigner
# ────────────────────────────────────────────────────────


class CharacterDesigner(BaseAgent):
    """Takes outline → produces character designs."""

    name = "character_designer"
    stage = "character_design"
    system_prompt = CHARACTER_DESIGNER_PROMPT
    output_artifact_type = "characters"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._skill_registry: SkillRegistry | None = None

    def set_skill_registry(self, registry: SkillRegistry) -> None:
        self._skill_registry = registry

    def _build_system_prompt(self, state: ProjectState) -> str:
        prompt = _inject_skills(
            self.system_prompt, self.stage,
            self._skill_registry, state.skill_bindings,
        )
        return _add_user_profile_hints(prompt, self.stage)

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        outline_json = ""
        if state.outline:
            outline_json = state.outline.model_dump_json(
                indent=2, ensure_ascii=False
            )
        return (
            f"请基于以下故事大纲，设计详细的角色档案。\n\n"
            f"=== 故事大纲 ===\n{outline_json}\n"
            + (f"\n用户额外指示: {user_message}" if user_message else "")
        )


# ────────────────────────────────────────────────────────
# 4. SceneDesigner
# ────────────────────────────────────────────────────────


class SceneDesigner(BaseAgent):
    """Takes outline + characters → produces scene designs."""

    name = "scene_designer"
    stage = "scene_design"
    system_prompt = SCENE_DESIGNER_PROMPT
    output_artifact_type = "scenes"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._skill_registry: SkillRegistry | None = None

    def set_skill_registry(self, registry: SkillRegistry) -> None:
        self._skill_registry = registry

    def _build_system_prompt(self, state: ProjectState) -> str:
        prompt = _inject_skills(
            self.system_prompt, self.stage,
            self._skill_registry, state.skill_bindings,
        )
        return _add_user_profile_hints(prompt, self.stage)

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        parts = ["请基于以下故事信息，设计详细的场景美术方案。"]
        if state.outline:
            parts.append(f"\n=== 大纲 ===\n{state.outline.model_dump_json(indent=2, ensure_ascii=False)}")
        if state.characters:
            char_names = [c.name for c in state.characters]
            parts.append(f"\n=== 已有角色 ===\n{', '.join(char_names)}")
        if user_message:
            parts.append(f"\n用户额外指示: {user_message}")
        return "\n".join(parts)


# ────────────────────────────────────────────────────────
# 5. ArtDirector
# ────────────────────────────────────────────────────────


class ArtDirector(BaseAgent):
    """Takes outline + characters + scenes → produces art style guide."""

    name = "art_director"
    stage = "art_direction"
    system_prompt = ART_DIRECTOR_PROMPT
    output_artifact_type = "art_style"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._skill_registry: SkillRegistry | None = None

    def set_skill_registry(self, registry: SkillRegistry) -> None:
        self._skill_registry = registry

    def _build_system_prompt(self, state: ProjectState) -> str:
        prompt = _inject_skills(
            self.system_prompt, self.stage,
            self._skill_registry, state.skill_bindings,
        )
        return _add_user_profile_hints(prompt, self.stage)

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        parts = ["请基于以下项目信息，制定全局美术风格指南。"]
        if state.outline:
            parts.append(
                f"\n=== 大纲概要 ===\n"
                f"类型: {state.outline.basic_info.genre}\n"
                f"基调: {state.outline.basic_info.tone}\n"
                f"主题: {state.outline.basic_info.theme}"
            )
        if state.characters:
            parts.append(
                f"\n=== 角色 ({len(state.characters)}个) ===\n"
                + "\n".join(f"- {c.name}: {c.appearance[:60]}..."
                           for c in state.characters[:5])
            )
        if state.scenes:
            parts.append(
                f"\n=== 场景 ({len(state.scenes)}个) ===\n"
                + "\n".join(f"- {s.name}: {s.mood}" for s in state.scenes[:5])
            )
        if user_message:
            parts.append(f"\n用户额外指示: {user_message}")
        return "\n".join(parts)


# ────────────────────────────────────────────────────────
# 6. ScriptWriter
# ────────────────────────────────────────────────────────


class ScriptWriter(BaseAgent):
    """Takes outline + designs → produces full screenplay."""

    name = "scriptwriter"
    stage = "scriptwriting"
    system_prompt = SCRIPT_WRITER_PROMPT
    output_artifact_type = "script"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._skill_registry: SkillRegistry | None = None

    def set_skill_registry(self, registry: SkillRegistry) -> None:
        self._skill_registry = registry

    def _build_system_prompt(self, state: ProjectState) -> str:
        prompt = _inject_skills(
            self.system_prompt, self.stage,
            self._skill_registry, state.skill_bindings,
        )
        return _add_user_profile_hints(prompt, self.stage)

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        parts = [
            "请基于以下完整的项目资料，撰写专业影视剧本。",
            "\n=== 项目上下文 ===",
        ]
        if state.meta.title:
            parts.append(f"标题: {state.meta.title}")
        if state.outline:
            bi = state.outline.basic_info
            parts.append(
                f"类型: {bi.genre} | 基调: {bi.tone} | "
                f"主题: {bi.theme} | 集数: {bi.episode_count}"
            )
            parts.append(f"\n--- 情节节拍 ({len(state.outline.plot_outline)}个) ---")
            for beat in state.outline.plot_outline:
                parts.append(
                    f"{beat.sequence_number}. [{beat.title}] {beat.synopsis[:120]}"
                )
        if state.characters:
            parts.append(
                f"\n--- 角色 ({len(state.characters)}个) ---"
            )
            for c in state.characters[:8]:
                parts.append(
                    f"- {c.name} ({c.role.value}): "
                    f"{c.personality[:80]}... | 动机: {c.motivation[:60]}"
                )
        if state.art_style:
            parts.append(
                f"\n--- 美术风格 ---\n{state.art_style.overall_style}"
            )
        if user_message:
            parts.append(f"\n用户额外指示: {user_message}")
        return "\n".join(parts)


# ────────────────────────────────────────────────────────
# 7. StoryboardArtist — 最复杂的 Agent
# ────────────────────────────────────────────────────────


class StoryboardArtist(BaseAgent):
    """Takes script + designs → produces shot-level storyboard.

    This is the most complex agent. It needs to:
    - Read the full script (potentially very long)
    - Read all design artifacts for visual consistency
    - Produce per-shot breakdowns with camera language
    - Generate image/video prompts for downstream video generation
    """

    name = "storyboard_artist"
    stage = "storyboarding"
    system_prompt = STORYBOARD_ARTIST_PROMPT
    output_artifact_type = "storyboard"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._skill_registry: SkillRegistry | None = None

    def set_skill_registry(self, registry: SkillRegistry) -> None:
        self._skill_registry = registry

    def _build_system_prompt(self, state: ProjectState) -> str:
        prompt = _inject_skills(
            self.system_prompt, self.stage,
            self._skill_registry, state.skill_bindings,
        )
        return _add_user_profile_hints(prompt, self.stage)

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        parts = [
            "请基于以下完整剧本和设计资料，进行精确的分镜拆解。",
            "这是最关键的环节——每个镜头都需要完整的摄影指令。",
            "\n=== 剧本 ===",
        ]
        if state.script:
            for i, scene in enumerate(state.script.scenes):
                parts.append(f"\n--- 场景 {scene.heading.scene_number}: "
                            f"{scene.heading.location} ({scene.heading.int_ext.value} "
                            f"{scene.heading.time_of_day}) ---")
                for block in scene.blocks[:20]:  # Limit per scene to avoid overflow
                    if block.block_type == "action":
                        parts.append(f"  [动作] {block.content.get('description', '')[:200]}")
                    elif block.block_type == "dialogue":
                        dc = block.content
                        parts.append(
                            f"  {dc.get('character_name', '?')}: "
                            f"{dc.get('dialogue', '')[:150]}"
                        )
                    elif block.block_type == "scene_heading":
                        parts.append(f"  >> {block.content}")
                if len(scene.blocks) > 20:
                    parts.append(f"  ... (+{len(scene.blocks)-20} more blocks)")

        if state.scenes:
            parts.append("\n=== 场景设计 ===")
            for s in state.scenes[:5]:
                parts.append(
                    f"- {s.name}: {s.environment[:100]}... | "
                    f"光影: {s.lighting_description or 'N/A'}"
                )

        if state.art_style:
            parts.append(
                f"\n=== 美术风格 ===\n{state.art_style.model_dump_json(ensure_ascii=False)}"
            )

        if state.characters:
            parts.append("\n=== 角色视觉参考 ===")
            for c in state.characters[:5]:
                parts.append(f"- {c.name}: {c.appearance[:100]}...")

        if user_message:
            parts.append(f"\n用户额外指示: {user_message}")

        return "\n".join(parts)


# ────────────────────────────────────────────────────────
# 8. Reviewer
# ────────────────────────────────────────────────────────


class Reviewer(SimpleAgent):
    """Reviews all artifacts for quality and consistency."""

    name = "reviewer"
    stage = "review"
    system_prompt = REVIEWER_PROMPT
    output_artifact_type = "review_report"

    def _build_system_prompt(self, state: ProjectState) -> str:
        return _add_user_profile_hints(self.system_prompt, self.stage)

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        parts = [
            "请对当前项目的所有产物进行全面质量审查。",
            "\n=== 项目状态摘要 ===",
            f"标题: {state.meta.title}",
            f"状态: {state.meta.status.value}",
        ]
        artifact_summary = []
        if state.refined_idea:
            artifact_summary.append(f"✓ 精炼概念 ({len(state.refined_idea)}字)")
        if state.outline:
            artifact_summary.append(
                f"✓ 大纲 ({len(state.outline.plot_outline)}个节拍)"
            )
        if state.characters:
            artifact_summary.append(f"✓ 角色 ({len(state.characters)}个)")
        if state.scenes:
            artifact_summary.append(f"✓ 场景 ({len(state.scenes)}个)")
        if state.art_style:
            artifact_summary.append("✓ 美术风格")
        if state.script:
            artifact_summary.append(
                f"✓ 剧本 ({len(state.script.scenes)}场)"
            )
        if state.storyboard:
            artifact_summary.append(
                f"✓ 分镜 ({state.storyboard.total_shot_count}个镜头)"
            )

        if artifact_summary:
            parts.append("已有产物:\n" + "\n".join(f"  {a}" for a in artifact_summary))
        else:
            parts.append("暂无产物可供审查。")

        if user_message:
            parts.append(f"\n用户额外指示: {user_message}")

        return "\n".join(parts)


# ────────────────────────────────────────────────────────
# 9. Orchestrator
# ────────────────────────────────────────────────────────


class Orchestrator(SimpleAgent):
    """Routes user requests to appropriate agents."""

    name = "orchestrator"
    stage = "routing"
    system_prompt = ORCHESTRATOR_PROMPT
    output_artifact_type = "routing_decision"

    def _build_system_prompt(self, state: ProjectState) -> str:
        return self.system_prompt

    def _build_user_prompt(self, state: ProjectState, user_message: str) -> str:
        status = state.current_stage_status().value
        existing = []
        if state.refined_idea:
            existing.append("refined_idea")
        if state.outline:
            existing.append("outline")
        if state.characters:
            existing.append("characters")
        if state.scenes:
            existing.append("scenes")
        if state.art_style:
            existing.append("art_style")
        if state.script:
            existing.append("script")
        if state.storyboard:
            existing.append("storyboard")

        return (
            f"当前项目状态: {status}\n"
            f"已有产物: {', '.join(existing) or '(无)'}\n"
            f"项目标题: {state.meta.title or '未命名'}\n\n"
            f"用户消息: {user_message}\n\n"
            f"请决定下一步操作。"
        )


# ────────────────────────────────────────────────────────
# Agent Factory
# ────────────────────────────────────────────────────────

AGENT_MAP: dict[str, type[BaseAgent | SimpleAgent]] = {
    "idea_refiner": IdeaRefiner,
    "structurer": Structurer,
    "character_designer": CharacterDesigner,
    "scene_designer": SceneDesigner,
    "art_director": ArtDirector,
    "scriptwriter": ScriptWriter,
    "storyboard_artist": StoryboardArtist,
    "reviewer": Reviewer,
    "orchestrator": Orchestrator,
}

# Agents that support skill injection (need set_skill_registry called)
SKILL_AWARE_AGENTS = {
    "structurer", "character_designer", "scene_designer",
    "art_director", "scriptwriter", "storyboard_artist",
}


def create_agent(
    name: str,
    llm_client: LLMClient | None = None,
    skill_registry: SkillRegistry | None = None,
) -> BaseAgent | SimpleAgent:
    """Factory: create an agent by name with optional dependencies."""
    agent_class = AGENT_MAP.get(name)
    if agent_class is None:
        raise ValueError(
            f"Unknown agent: '{name}'. Available: {list(AGENT_MAP.keys())}"
        )

    agent = agent_class(llm_client=llm_client)

    # Wire up skill registry for agents that support it
    if name in SKILL_AWARE_AGENTS and skill_registry:
        if hasattr(agent, 'set_skill_registry'):
            agent.set_skill_registry(skill_registry)

    return agent
