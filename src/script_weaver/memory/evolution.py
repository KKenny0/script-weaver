"""Skill Evolution — skills that learn and improve from usage.

Implements the "grows with you" concept for skills:
1. Propose new skills from project experience patterns
2. Improve existing skills based on execution feedback
3. Rank/recommend skills based on user success patterns
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from script_weaver.core.types import (
    DecisionRecord,
    Pattern,
    Skill,
    UserAction,
)
from script_weaver.memory.profile import get_profile_manager

logger = logging.getLogger(__name__)


class SkillDraft:
    """A proposed new skill extracted from user behavior patterns."""

    def __init__(
        self,
        name: str,
        stage: str,
        description: str,
        prompt_injection: str,
        source_patterns: list[Pattern],
        confidence: float = 0.3,
    ):
        self.name = name
        self.stage = stage
        self.description = description
        self.prompt_injection = prompt_injection
        self.source_patterns = source_patterns
        self.confidence = confidence
        self.created_at = datetime.now().isoformat()

    def to_skill(self, skill_id: str) -> Skill:
        """Convert draft to a proper Skill object."""
        return Skill(
            id=skill_id,
            name=self.name,
            # We'd need to import PipelineStage here properly; using string for now
            stage=None,  # Will be set by caller
            description=self.description,
            version="0.1-draft",
            source_type="custom",
            source_format="native_yaml",  # Will be exported as YAML
            file_path="",
            prompt_injection=self.prompt_injection,
            constraints=[f"Auto-generated from {len(self.source_patterns)} user patterns"],
            metadata={
                "auto_generated": True,
                "confidence": self.confidence,
                "source_patterns": [p.description for p in self.source_patterns],
                "created_at": self.created_at,
            },
        )

    def to_yaml(self) -> str:
        """Export as YAML for user review."""
        lines = [
            f"id: auto-{self.name.lower().replace(' ', '-')}",
            f'name: "{self.name}" (Draft)',
            f"stage: {self.stage}",
            f"description: {self.description}",
            'version: "0.1-draft"',
            "",
            "# Auto-generated from user behavior patterns",
            "prompt_injection: |",
        ]
        for line in self.prompt_injection.split("\n"):
            lines.append(f"  {line}")
        lines.extend([
            "",
            f"constraints:",
            f'  - "Auto-generated from {len(self.source_patterns)} user patterns"',
            f"  - Review and refine before activating",
            "",
            "metadata:",
            f"  auto_generated: true",
            f"  confidence: {self.confidence}",
        ])
        return "\n".join(lines)


class ExecutionResult:
    """Record of a skill's execution outcome."""

    def __init__(
        self,
        skill_id: str,
        stage: str,
        accepted: bool,
        modification_count: int = 0,
        user_feedback: str = "",
    ):
        self.skill_id = skill_id
        self.stage = stage
        self.accepted = accepted
        self.modification_count = modification_count
        self.user_feedback = user_feedback
        self.timestamp = datetime.now().isoformat()


class SkillEvolution:
    """Manages skill lifecycle: propose → improve → rank."""

    def __init__(self):
        self._execution_history: list[ExecutionResult] = []
        self._profile_manager = get_profile_manager()

    # ── 1. Propose New Skills from Experience ───────────

    async def propose_skill_from_project(
        self,
        project_id: str,
        decisions: list[DecisionRecord],
    ) -> list[SkillDraft]:
        """Analyze project decisions to propose new skill candidates.

        Looks for repeated modification patterns that suggest a consistent
        user preference not yet captured by an existing skill.
        """
        # Get patterns from profile manager
        patterns = self._profile_manager.extract_patterns_from_project(
            project_id, decisions
        )
        if not patterns:
            return []

        drafts: list[SkillDraft] = []

        # Group patterns by type and stage
        by_stage: dict[str, list[Pattern]] = {}
        for p in patterns:
            # Infer stage from related decisions
            stage = self._infer_stage_from_patterns(p, decisions)
            by_stage.setdefault(stage, []).append(p)

        for stage, stage_patterns in by_stage.items():
            if len(stage_patterns) >= 2:  # Need at least 2 patterns to propose
                draft = self._create_draft_from_patterns(stage, stage_patterns)
                if draft:
                    drafts.append(draft)

        logger.info(
            f"Proposed {len(drafts)} skill candidates from project {project_id}"
        )
        return drafts

    def _infer_stage_from_patterns(
        self, pattern: Pattern, decisions: list[DecisionRecord]
    ) -> str:
        """Infer which pipeline stage a pattern relates to."""
        # Find the decision that likely generated this pattern
        for d in decisions:
            if d.action == UserAction.MODIFY and d.modification:
                if pattern.description.split(":")[0].lower() in (d.modification or "").lower():
                    return d.stage
        return "scriptwriting"  # Default fallback

    def _create_draft_from_patterns(
        self, stage: str, patterns: list[Pattern]
    ) -> SkillDraft | None:
        """Create a skill draft from a group of similar patterns."""
        if not patterns:
            return None

        # Synthesize name and description from patterns
        descriptions = [p.description for p in patterns]
        avg_confidence = sum(p.confidence for p in patterns) / len(patterns)

        # Generate a name based on pattern type
        pattern_type = patterns[0].type.value
        type_names = {
            "structure_preference": "结构偏好",
            "style_preference": "风格偏好",
            "dialogue_pattern": "对话模式",
            "visual_pattern": "视觉模式",
            "workflow_optimization": "工作流优化",
        }
        base_name = type_names.get(pattern_type, "自定义")

        # Build prompt injection from patterns
        prompt_parts = [
            f"## 用户偏好：{base_name}\n",
            "基于用户在多个项目中的反复修改模式，请遵循以下偏好：\n",
        ]
        for p in patterns:
            prompt_parts.append(f"- {p.description} (置信度: {p.confidence:.0%})\n")
        prompt_parts.append("\n在执行任务时，优先考虑以上偏好。\n")

        return SkillDraft(
            name=f"用户自学习-{base_name}",
            stage=stage,
            description=f"从{len(patterns)}个用户行为模式中自动提取的{base_name}偏好",
            prompt_injection="".join(prompt_parts),
            source_patterns=patterns,
            confidence=avg_confidence,
        )

    # ── 2. Improve Existing Skills ───────────────────────

    async def improve_skill(
        self,
        skill_id: str,
        recent_results: list[ExecutionResult],
    ) -> dict[str, Any]:
        """Suggest improvements to a skill based on recent execution results.

        Returns suggested updates (not applied automatically).
        """
        if not recent_results:
            return {"status": "no_data", "message": "No execution results to analyze"}

        # Calculate metrics
        total = len(recent_results)
        accepted = sum(1 for r in recent_results if r.accepted)
        acceptance_rate = accepted / total
        avg_modifications = (
            sum(r.modification_count for r in recent_results) / total
        )

        suggestions: list[str] = []

        if acceptance_rate < 0.5:
            suggestions.append(
                f"⚠️ 低接受率 ({acceptance_rate:.0%}): "
                "考虑重新审视该 skill 的 prompt_injection 是否过于严格或与用户风格不匹配"
            )

        if avg_modifications > 2:
            suggestions.append(
                f"⚠️ 高修改频率 (平均{avg_modifications:.1f}次): "
                "用户经常需要调整此 skill 的输出，建议增加灵活性"
            )

        # Analyze feedback themes
        feedback_texts = [r.user_feedback for r in recent_results if r.user_feedback]
        if feedback_texts:
            common_themes = self._extract_feedback_themes(feedback_texts)
            if common_themes:
                suggestions.append(
                    f"💡 用户反馈主题: {', '.join(common_themes[:3])}"
                )

        return {
            "status": "analysis_complete",
            "skill_id": skill_id,
            "metrics": {
                "total_executions": total,
                "acceptance_rate": round(acceptance_rate, 3),
                "avg_modifications": round(avg_modifications, 2),
            },
            "suggestions": suggestions,
            "recommended_action": (
                "review_and_refine" if acceptance_rate < 0.6
                else "monitoring"
            ),
        }

    def _extract_feedback_themes(self, feedbacks: list[str]) -> list[str]:
        """Extract common themes from user feedback texts."""
        # Simple keyword-based extraction
        theme_keywords: dict[str, int] = {}
        for fb in feedbacks:
            fb_lower = fb.lower()
            for keyword, theme in [
                ("太长", "too_long"),
                ("太短", "too_short"),
                ("不够", "not_enough"),
                ("太多", "too_much"),
                ("不够详细", "not_detailed"),
                ("太复杂", "too_complex"),
                ("格式", "format"),
                ("风格", "style"),
            ]:
                if keyword in fb_lower:
                    theme_keywords[theme] = theme_keywords.get(theme, 0) + 1

        sorted_themes = sorted(theme_keywords.items(), key=lambda x: x[1], reverse=True)
        return [theme for theme, count in sorted_themes if count >= 1]

    # ── 3. Rank Skills ─────────────────────────────────

    async def rank_skills(
        self,
        stage: str,
        available_skills: list[Skill],
    ) -> list[tuple[Skill, float]]:
        """Rank available skills for a given stage based on user history.

        Returns (skill, score) tuples sorted by score descending.
        """
        profile = self._profile_manager.profile
        scored: list[tuple[Skill, float]] = []

        for skill in available_skills:
            score = 0.0

            # Factor 1: Has user used this structure before?
            for sp in profile.preferred_structures:
                if sp.method_name == skill.id:
                    score += sp.success_rate * 40
                    score += min(sp.usage_count * 5, 20)

            # Factor 2: Stage match bonus
            if skill.stage and skill.stage.value == stage:
                score += 20

            # Factor 3: Execution history
            skill_results = [
                r for r in self._execution_history
                if r.skill_id == skill.id and r.stage == stage
            ]
            if skill_results:
                recent_acceptance = sum(
                    1 for r in skill_results[-5:] if r.accepted
                ) / min(len(skill_results), 5)
                score += recent_acceptance * 15

            # Factor 4: Built-in skills get small default boost
            if skill.source_type == "builtin":
                score += 5

            scored.append((skill, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    # ── Execution Tracking ──────────────────────────────

    def record_execution(self, result: ExecutionResult) -> None:
        """Record a skill execution outcome."""
        self._execution_history.append(result)
        # Keep only last 200 entries
        if len(self._execution_history) > 200:
            self._execution_history = self._execution_history[-200:]
