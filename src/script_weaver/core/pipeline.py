"""Pipeline orchestration engine.

Manages the end-to-end flow:
Input → IdeaRefiner → [Gate] → Structurer → [Gate] → [Designers |||] → [Gate]
→ ScriptWriter → [Gate] → StoryboardArtist → [Growth Loop] → Export
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Coroutine

from script_weaver.agents.impl import (
    ArtDirector,
    CharacterDesigner,
    IdeaRefiner,
    Orchestrator,
    Reviewer,
    SceneDesigner,
    ScriptWriter,
    StoryboardArtist,
    Structurer,
    create_agent,
)
from script_weaver.core.types import (
    DecisionRecord,
    ProjectMeta,
    ProjectState,
    ProjectStatus,
    UserAction,
    VisualHighlight,
)
from script_weaver.llm.client import LLMClient
from script_weaver.memory.evolution import SkillEvolution
from script_weaver.memory.profile import get_profile_manager
from script_weaver.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

# Type alias for async functions
AsyncFunc = Callable[..., Coroutine[Any, Any, Any]]


def _coerce_llm_types(data: Any) -> Any:
    """Recursively coerce LLM output: pure-numeric strings → int/float.

    LLMs frequently emit numbers as strings (e.g. "1" instead of 1), which
    fails strict Pydantic validation and discards an entire artifact. This
    normalizes the common cases so one type mismatch doesn't lose everything.
    """
    if isinstance(data, dict):
        return {k: _coerce_llm_types(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_coerce_llm_types(v) for v in data]
    if isinstance(data, str):
        s = data.strip()
        if s:
            try:
                return int(s)
            except ValueError:
                try:
                    return float(s)
                except ValueError:
                    return data
    return data


class HumanGateResult:
    """Result of a human-in-the-loop gate."""

    def __init__(self, approved: bool, feedback: str = ""):
        self.approved = approved
        self.feedback = feedback


# Callback types for human interaction (CLI/UI can inject these)
GateCallback = Callable[[str, str], Coroutine[Any, Any, HumanGateResult]]
ProgressCallback = Callable[[str, str], None]


class PipelineEngine:
    """Orchestrates the full agent pipeline with state management and growth loop."""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        skill_registry: SkillRegistry | None = None,
        gate_callback: GateCallback | None = None,
        progress_callback: ProgressCallback | None = None,
        auto_approve_gates: bool = False,  # For non-interactive mode
    ):
        self._llm = llm_client or LLMClient()
        self._skill_registry = skill_registry or SkillRegistry()
        self._gate_callback = gate_callback
        self._progress = progress_callback
        self._auto_approve = auto_approve_gates
        self._evolution = SkillEvolution()
        self._profile_manager = get_profile_manager()

    def _notify(self, stage: str, message: str) -> None:
        """Send progress notification."""
        if self._progress:
            self._progress(stage, message)
        logger.info(f"[Pipeline:{stage}] {message}")

    async def _gate(self, stage: str, artifact_summary: str) -> bool:
        """Human-in-the-loop gate. Returns True if approved."""
        if self._auto_approve:
            return True

        if self._gate_callback:
            result = await self._gate_callback(stage, artifact_summary)
            # Record decision to memory
            action = UserAction.ACCEPT if result.approved else UserAction.MODIFY
            # Note: we don't have state here to record to; caller should handle
            return result.approved

        # Default: auto-approve in CLI mode without callback
        return True

    async def _run_agent(
        self,
        agent_name: str,
        state: ProjectState,
        user_message: str = "",
    ) -> dict[str, Any]:
        """Run a single agent and integrate results into state."""
        self._notify(agent_name, f"Starting {agent_name}...")
        start = time.time()

        try:
            agent = create_agent(
                agent_name,
                llm_client=self._llm,
                skill_registry=self._skill_registry,
            )
            result = await agent.execute(state, user_message)

            elapsed = time.time() - start

            # Integrate result into state
            if isinstance(result, dict):
                status = result.get("status", "")
                data = result.get("data", result)

                if status == "success" and isinstance(data, (dict, list)):
                    await self._integrate_artifact(state, agent_name, data)
                elif status == "text_response":
                    # Agent returned text directly — try to parse as artifact
                    text_data = data.get("text", "")
                    self._notify(
                        agent_name,
                        f"Completed in {elapsed:.1f}s (text response)",
                    )
                    return result
                else:
                    self._notify(
                        agent_name,
                        f"Completed in {elapsed:.1f}s (status: {status})",
                    )
            else:
                self._notify(
                    agent_name,
                    f"Completed in {elapsed:.1f}s (raw result)",
                )

            return result if isinstance(result, dict) else {"data": result}

        except Exception as e:
            logger.error(f"[{agent_name}] Failed: {e}", exc_info=True)
            self._notify(agent_name, f"FAILED: {e}")
            return {"error": str(e)}

    async def _integrate_artifact(
        self,
        state: ProjectState,
        agent_name: str,
        data: dict[str, Any],
    ) -> None:
        """Parse agent output and update project state."""
        from script_weaver.core.types import (  # Local import to avoid circular
            ArtStyle, Character, Outline, Script, SceneDesign, Storyboard,
        )

        data = _coerce_llm_types(data)

        try:
            if agent_name == "idea_refiner":
                state.refined_idea = data.get("logline", "")
                if not state.refined_idea:
                    state.refined_idea = data.get("title", "") + ": " + data.get(
                        "core_theme", ""
                    )
                state.meta.status = ProjectStatus.REFINING

            elif agent_name == "structurer":
                outline = Outline.model_validate(data) if data else Outline()
                state.outline = outline
                state.meta.status = ProjectStatus.STRUCTURED

            elif agent_name == "character_designer":
                if isinstance(data, list):
                    characters = [Character.model_validate(c) for c in data]
                else:
                    characters = [Character.model_validate(data)]
                state.characters = characters
                state.meta.status = ProjectStatus.DESIGNING

            elif agent_name == "scene_designer":
                if isinstance(data, list):
                    scenes = [SceneDesign.model_validate(s) for s in data]
                else:
                    scenes = [SceneDesign.model_validate(data)]
                state.scenes = scenes

            elif agent_name == "art_director":
                state.art_style = ArtStyle.model_validate(data)

            elif agent_name == "scriptwriter":
                state.script = Script.model_validate(data)
                state.meta.status = ProjectStatus.SCRIPTING

            elif agent_name == "storyboard_artist":
                sb = Storyboard.model_validate(data)
                sb.compute_totals()
                state.storyboard = sb
                state.meta.status = ProjectStatus.STORYBOARDING

            state.touch()
        except Exception as e:
            logger.warning(f"Failed to integrate artifact from {agent_name}: {e}")

    # ── Main Pipeline Methods ───────────────────────────

    async def run_full_pipeline(
        self,
        user_input: str,
        title: str | None = None,
    ) -> ProjectState:
        """Run the complete pipeline from idea to storyboard.

        This is the primary entry point for end-to-end generation.
        """
        self._notify("pipeline", "=== Starting Full Pipeline ===")

        # Initialize state
        state = ProjectState()
        state.user_input = user_input
        if title:
            state.meta.title = title
        else:
            state.meta.title = user_input[:50] + ("..." if len(user_input) > 50 else "")

        # Discover skills
        await self._skill_registry.discover()
        self._notify(
            "pipeline",
            f"Skills discovered: {len(self._skill_registry.list_all())}",
        )

        # ── Stage 1: Idea Refinement ────────────────────
        self._notify("pipeline", "--- Stage 1: Idea Refinement ---")
        await self._run_agent("idea_refiner", state, user_input)

        if not await self._await_gate("ideation", state):
            return state

        # ── Stage 2: Structuring ───────────────────────
        self._notify("pipeline", "--- Stage 2: Story Structuring ---")
        await self._run_agent("structurer", state)

        if not await self._await_gate("structuring", state):
            return state

        # ── Stage 3: Design (Parallel) ────────────────
        self._notify("pipeline", "--- Stage 3: Design (Characters/Scenes/Art) ---")
        await self._run_agent("character_designer", state)
        await self._run_agent("scene_designer", state)
        await self._run_agent("art_director", state)

        if not await self._await_gate("designing", state):
            return state

        # ── Stage 4: Script Writing ────────────────────
        self._notify("pipeline", "--- Stage 4: Script Writing ---")
        await self._run_agent("scriptwriter", state)

        if not await self._await_gate("scriptwriting", state):
            return state

        # ── Stage 5: Storyboarding ────────────────────
        self._notify("pipeline", "--- Stage 5: Storyboard Generation ---")
        await self._run_agent("storyboard_artist", state)

        # ── Stage 6: Visual Highlights ─────────────────
        self._notify("pipeline", "--- Stage 6: Visual Highlights ---")
        await self._generate_visual_highlights(state)

        # ── Final State ───────────────────────────────
        state.meta.status = ProjectStatus.COMPLETE
        state.touch()

        # ── Growth Loop ──────────────────────────────
        await self._run_growth_loop(state)

        self._notify("pipeline", "=== Pipeline Complete ===")
        return state

    async def _await_gate(self, stage: str, state: ProjectState) -> bool:
        """Wait at a human gate. Returns True if approved."""
        summary = self._build_gate_summary(stage, state)
        approved = await self._gate(stage, summary)

        if not approved:
            # Record rejection
            decision = DecisionRecord(
                stage=stage,
                context=summary,
                action=UserAction.REJECT,
            )
            state.memory.record_decision(decision)

        return approved

    def _build_gate_summary(self, stage: str, state: ProjectState) -> str:
        """Build a human-readable summary for the gate."""
        summaries = {
            "ideation": f"精炼概念: {(state.refined_idea or '')[:200]}",
            "structuring": (
                f"大纲: {state.outline.basic_info.logline if state.outline else 'N/A'}"
                f" ({len(state.outline.plot_outline)} 节拍)" if state.outline else "N/A"
            ),
            "designing": (
                f"角色: {len(state.characters or [])}个, "
                f"场景: {len(state.scenes or [])}个, "
                f"风格: {state.art_style.overall_style if state.art_style else 'N/A'}"
            ),
            "scriptwriting": (
                f"剧本: {len(state.script.scenes) if state.script else 0} 场"
            ),
            "storyboarding": (
                f"分镜: {state.storyboard.total_shot_count if state.storyboard else 0} 镜头, "
                f"~{state.storyboard.total_estimated_duration:.0f}s"
                if state.storyboard else "N/A"
            ),
        }
        return summaries.get(stage, f"Stage: {stage}")

    async def _generate_visual_highlights(self, state: ProjectState) -> None:
        """Extract key visual highlights from the storyboard."""
        if not state.storyboard or not state.script:
            return

        # Use a simple heuristic-based approach for MVP
        # In production, this would use an LLM call
        highlights: list[VisualHighlight] = []

        # Pick shots that are likely highlights (long duration, extreme close-ups, etc.)
        for i, shot in enumerate(state.storyboard.shots[:10]):
            if shot.duration_seconds >= 5 or shot.shot_size.value in (
                "extreme_close_up", "close_up"
            ):
                highlights.append(VisualHighlight(
                    title=f"亮点{i+1}: {shot.visual_description[:40]}...",
                    description=shot.visual_description,
                    related_shot_ids=[shot.shot_id],
                    visual_technique=(
                        f"{shot.shot_size.value} + "
                        f"{shot.camera_movement.value}"
                    ),
                ))

        if highlights:
            state.visual_highlights = highlights

    async def _run_growth_loop(self, state: ProjectState) -> None:
        """Execute the Grows With User growth cycle."""
        self._notify("growth", "--- Running Growth Loop ---")

        try:
            # 1. Extract patterns from decisions
            patterns = self._profile_manager.extract_patterns_from_project(
                state.meta.id, state.memory.decisions
            )
            for p in patterns:
                state.memory.add_pattern(p)

            # 2. Propose new skills from experience
            drafts = await self._evolution.propose_skill_from_project(
                state.meta.id, state.memory.decisions
            )
            if drafts:
                self._notify(
                    "growth",
                    f"Proposed {len(drafts)} new skill candidates from experience",
                )
                for draft in drafts:
                    self._notify(
                        "growth",
                        f"  - {draft.name} (confidence: {draft.confidence:.0%})",
                    )

            # 3. Update user profile
            self._profile_manager.profile.stats.total_projects += 1
            self._profile_manager.profile.record_project_completed()
            self._profile_manager.save()

            # 4. Record decisions to profile
            for decision in state.memory.decisions:
                self._profile_manager.record_decision(decision)

            self._notify("growth", "Growth loop complete.")

        except Exception as e:
            logger.warning(f"Growth loop error (non-fatal): {e}")

    # ── Iterative Refinement ───────────────────────────

    async def refine(
        self,
        state: ProjectState,
        user_message: str,
    ) -> ProjectState:
        """Handle an iterative refinement request.

        Routes the user's modification request to the appropriate agent(s).
        """
        self._notify("refine", f"Processing refinement: {user_message[:80]}...")

        # Use orchestrator to decide routing
        orchestrator = create_agent("orchestrator", llm_client=self._llm)
        routing_result = await orchestrator.execute(state, user_message)

        # Parse routing decision
        next_agent = "orchestrator"
        if isinstance(routing_result, dict):
            data = routing_result.get("data", routing_result)
            if isinstance(data, dict):
                next_agent = data.get("next_agent", "orchestrator")

        # Execute the routed agent
        if next_agent and next_agent in [
            "scriptwriter", "storyboard_artist", "structurer",
            "character_designer", "scene_designer", "art_director",
        ]:
            await self._run_agent(next_agent, state, user_message)

            # Record modification decision
            decision = DecisionRecord(
                stage=getattr(
                    create_agent(next_agent), 'stage', 'unknown'
                ),
                context=user_message,
                action=UserAction.MODIFY,
                modification=user_message,
            )
            state.memory.record_decision(decision)

        state.touch()
        return state
