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
from typing import Any, Callable, Coroutine, Iterable

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
from script_weaver.core import refinement
from script_weaver.core.refinement import RefineExecutionError
from script_weaver.core.resume import (
    ResumeCheckpoint,
    ResumeRejected,
    assert_fingerprint_match,
    current_fingerprint,
    validate_completed_steps,
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

# The checkpointable steps of a full generation, in execution order. Each
# entry is a model-driven agent stage (or the derived finalize step); a step
# only counts as completed once its artifact validated and integrated.
GENERATION_STEPS = (
    "idea_refiner",
    "structurer",
    "character_designer",
    "scene_designer",
    "art_director",
    "scriptwriter",
    "storyboard_artist",
    "finalize",
)

GENERATION_STEP_LABELS = {
    "idea_refiner": "概念精炼",
    "structurer": "故事大纲",
    "character_designer": "角色设计",
    "scene_designer": "场景设计",
    "art_director": "美术风格",
    "scriptwriter": "剧本",
    "storyboard_artist": "分镜",
    "finalize": "视觉亮点与收尾",
}

StageCompleteCallback = Callable[[str, ProjectState], Coroutine[Any, Any, None]]
# Growth lifecycle notifications (ticket #15 §5): status is one of
# pending/running/succeeded/failed/interrupted; "running" is reported BEFORE
# any growth side effect starts so the caller can persist it first.
GrowthEventCallback = Callable[[str, str | None], Coroutine[Any, Any, None]]


class PipelineStopped(Exception):
    """The pipeline was asked to stop between stages (e.g. user stop).

    Raised by the stage-completion callback (or awaited inside it) to abort
    before the next stage starts; everything already checkpointed stays
    saved. Distinct from a failure: the run is user-stopped, not broken.
    """


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

            if not isinstance(result, dict) or result.get("status") != "success":
                raise ValueError(f"Agent did not produce a valid artifact: {result}")
            await self._integrate_artifact(state, agent_name, result["data"])
            self._notify(agent_name, f"Completed in {elapsed:.1f}s")
            return result

        except Exception as e:
            logger.error(f"[{agent_name}] Failed: {e}", exc_info=True)
            self._notify(agent_name, f"FAILED: {e}")
            raise RuntimeError(f"{agent_name} failed: {e}") from e

    async def _integrate_artifact(
        self,
        state: ProjectState,
        agent_name: str,
        data: dict[str, Any] | list[Any],
    ) -> None:
        """Parse agent output and update project state."""
        from script_weaver.core.types import (  # Local import to avoid circular
            ArtStyle, Character, Outline, Script, SceneDesign, Storyboard,
        )

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
            raise ValueError(f"Failed to integrate artifact from {agent_name}: {e}") from e

    # ── Main Pipeline Methods ───────────────────────────

    async def run_full_pipeline(
        self,
        user_input: str | None = None,
        title: str | None = None,
        *,
        initial_state: ProjectState | None = None,
        completed_steps: Iterable[str] = (),
        on_stage_complete: StageCompleteCallback | None = None,
        resume_from: ResumeCheckpoint | None = None,
        on_growth_event: GrowthEventCallback | None = None,
    ) -> ProjectState:
        """Run the complete pipeline from idea to storyboard.

        The keyword arguments are the ticket-#14/#15 contracts:
        ``initial_state`` resumes from an explicit snapshot instead of a
        fresh state, ``completed_steps`` names steps that already succeeded
        (they and their gates are skipped), ``on_stage_complete`` is awaited
        after every step's artifact validated and integrated — the caller
        persists its checkpoint there and may raise (e.g.
        :class:`PipelineStopped`) to abort before the next stage.
        ``resume_from`` additionally validates the checkpoint BEFORE any
        model call: ``completed_steps`` must be a strict contiguous prefix
        of :data:`GENERATION_STEPS` with its artifacts present, and the
        execution fingerprint must match the engine's own resolved
        settings/skills (a drift raises :class:`ResumeRejected`). A
        checkpoint whose content already completed skips every stage AND
        the growth loop — completed content is never regenerated and
        growth is never replayed. ``on_growth_event`` receives the growth
        lifecycle transitions. Legacy callers pass only
        ``user_input``/``title`` and keep today's behavior.
        """
        self._notify("pipeline", "=== Starting Full Pipeline ===")

        resumed = resume_from is not None
        if resumed:
            # The checkpoint is the single source of truth for the success
            # prefix: callers pass the same list, but conflicting values
            # can never widen what is skipped.
            completed_steps = list(resume_from.completed_steps)
            validate_completed_steps(
                resume_from.completed_steps, state=resume_from.state_model()
            )
            if initial_state is None and resume_from.state is not None:
                initial_state = resume_from.state_model()

        # Initialize state
        if initial_state is not None:
            state = initial_state.model_copy(deep=True)
            if user_input is not None:
                state.user_input = user_input
            if title:
                state.meta.title = title
        else:
            state = ProjectState()
            state.user_input = user_input or ""
            if title:
                state.meta.title = title
            else:
                idea = state.user_input
                state.meta.title = idea[:50] + ("..." if len(idea) > 50 else "")

        done: set[str] = set(completed_steps)
        validate_completed_steps(completed_steps, state=state if resumed else None)

        async def _checkpoint(step: str) -> None:
            if on_stage_complete is not None:
                await on_stage_complete(step, state)

        # Discover skills
        await self._skill_registry.discover()
        self._notify(
            "pipeline",
            f"Skills discovered: {len(self._skill_registry.list_all())}",
        )

        # Resume basis: the fingerprint of the config/skills this engine
        # just resolved — BEFORE the first model call, so a drifted basis
        # can never spend one.
        if resumed:
            actual = await current_fingerprint(
                skill_registry=self._skill_registry,
                skill_bindings={
                    k: v.model_dump(mode="json")
                    for k, v in (state.skill_bindings or {}).items()
                },
                auto_approve=self._auto_approve,
            )
            assert_fingerprint_match(resume_from.fingerprint, actual)

        # ── Stage 1: Idea Refinement ────────────────────
        if "idea_refiner" not in done:
            self._notify("pipeline", "--- Stage 1: Idea Refinement ---")
            await self._run_agent("idea_refiner", state, state.user_input)
            if not await self._await_gate("ideation", state):
                return state
            await _checkpoint("idea_refiner")

        # ── Stage 2: Structuring ───────────────────────
        if "structurer" not in done:
            self._notify("pipeline", "--- Stage 2: Story Structuring ---")
            await self._run_agent("structurer", state)
            if not await self._await_gate("structuring", state):
                return state
            await _checkpoint("structurer")

        # ── Stage 3: Design (Parallel) ────────────────
        self._notify("pipeline", "--- Stage 3: Design (Characters/Scenes/Art) ---")
        for designer in ("character_designer", "scene_designer", "art_director"):
            if designer not in done:
                await self._run_agent(designer, state)
                await _checkpoint(designer)
        designers_done = {"character_designer", "scene_designer", "art_director"} <= done
        # A checkpoint commits each designer BEFORE the designing gate, so a
        # resumed run with all designers saved but scriptwriter not yet run
        # must re-ask that gate: a saved artifact is not a confirmed gate.
        gate_needed = not designers_done or (resumed and "scriptwriter" not in done)
        if gate_needed and not await self._await_gate("designing", state):
            return state

        # ── Stage 4: Script Writing ────────────────────
        if "scriptwriter" not in done:
            self._notify("pipeline", "--- Stage 4: Script Writing ---")
            await self._run_agent("scriptwriter", state)
            if not await self._await_gate("scriptwriting", state):
                return state
            await _checkpoint("scriptwriter")

        # ── Stage 5: Storyboarding ────────────────────
        if "storyboard_artist" not in done:
            self._notify("pipeline", "--- Stage 5: Storyboard Generation ---")
            await self._run_agent("storyboard_artist", state)
            await _checkpoint("storyboard_artist")

        # ── Stage 6: Visual Highlights + completion ────
        if "finalize" not in done:
            self._notify("pipeline", "--- Stage 6: Visual Highlights ---")
            await self._generate_visual_highlights(state)
            state.meta.status = ProjectStatus.COMPLETE
            state.touch()
            await _checkpoint("finalize")

        # ── Growth Loop ──────────────────────────────
        # Growth runs only when THIS run completed the content; a resumed
        # already-complete checkpoint neither regenerates content nor
        # replays growth (highlights are deterministic and were kept).
        content_pre_complete = resumed and resume_from.content_complete
        if not content_pre_complete:
            growth_status = await self._run_growth_loop(
                state, on_event=on_growth_event
            )
            if growth_status == "failed":
                self._notify("growth", "内容已完成；成长任务失败已记录，不影响内容。")

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

    async def _run_growth_loop(
        self,
        state: ProjectState,
        *,
        on_event: GrowthEventCallback | None = None,
    ) -> str:
        """Execute the Grows With User growth cycle; report its outcome.

        Returns ``"succeeded"`` or ``"failed"`` and notifies ``on_event``
        with the lifecycle transitions. ``running`` is reported BEFORE the
        first side effect — the caller persists it first, so a crash later
        leaves a durable record that side effects may have partially
        happened (a restart converges it to ``interrupted``). A failure is
        reported as data, never swallowed: the content stays complete.
        """
        self._notify("growth", "--- Running Growth Loop ---")

        if on_event is not None:
            await on_event("running", None)

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
            if on_event is not None:
                await on_event("succeeded", None)
            return "succeeded"

        except Exception as e:
            logger.warning(f"Growth loop error (non-fatal): {e}")
            if on_event is not None:
                await on_event("failed", str(e))
            return "failed"

    # ── Iterative Refinement ───────────────────────────

    async def refine(
        self,
        state: ProjectState,
        user_message: str,
    ) -> ProjectState:
        """Handle an iterative refinement request.

        Routes the user's modification request to one agent and returns the
        modified state — but only after validation proves a substantive,
        constraint-respecting change. Every refusal raises a
        :class:`~script_weaver.core.refinement.RefinementError` (or a
        sanitized :class:`RefineExecutionError` for model failures) and the
        caller's state stays untouched: agents run on a deep copy, and the
        decision record and touch are only added once validation passes.
        """
        self._notify("refine", f"Processing refinement: {user_message[:80]}...")

        # Route on the incoming snapshot (read-only for the orchestrator).
        orchestrator = create_agent("orchestrator", llm_client=self._llm)
        try:
            routing_result = await orchestrator.execute(state, user_message)
        except Exception as e:
            logger.exception("[refine] orchestrator failed")
            raise RefineExecutionError("编排器调用失败，修改未应用") from e
        decision = refinement.parse_routing(routing_result)

        # Locate the constrained target from the pre-modification snapshot;
        # without it the execution agent must not even be called.
        target = (
            refinement.locate_last_dialogue(state)
            if decision.constraint == "shorten_last_dialogue"
            else None
        )

        # Execute on a deep copy so a failed validation can never leak
        # partial writes into the caller's state.
        working = state.model_copy(deep=True)
        instruction = refinement.build_agent_instruction(
            state, decision, user_message, target
        )
        try:
            await self._run_agent(decision.next_agent, working, instruction)
        except Exception as e:
            logger.exception("[refine] agent %s failed", decision.next_agent)
            raise RefineExecutionError(
                f"{decision.next_agent} 执行失败，修改未应用"
            ) from e

        # Validate the returned content against the snapshot, never the
        # model's own claims about what it did.
        if target is not None:
            refinement.validate_shorten_result(state, working, target)
        else:
            refinement.validate_general_result(state, working, decision.next_agent)

        # Validation passed: only now record the modification and touch —
        # in the returned copy only, so the caller's snapshot stays pristine.
        stage = create_agent(decision.next_agent, llm_client=self._llm).stage
        working.memory.record_decision(DecisionRecord(
            stage=stage,
            context=user_message,
            action=UserAction.MODIFY,
            modification=user_message,
        ))
        working.touch()
        return working
