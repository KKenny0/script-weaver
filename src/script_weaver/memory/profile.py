"""UserProfile — cross-project persistent user creative profile.

Stores at ~/.scriptweaver/user_profile.json
Learns from decisions across all projects.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from script_weaver.core.config import get_settings
from script_weaver.core.types import (
    DecisionRecord,
    Pattern,
    PatternType,
    PipelineStage,
    StructurePreference,
    UserAction,
    UserProfile,
)

logger = logging.getLogger(__name__)


class ProfileManager:
    """Manage user profile lifecycle: load/save/update/query."""

    def __init__(self, profile_path: Path | None = None):
        settings = get_settings()
        self._path = profile_path or (settings.data_dir / "user_profile.json")
        self._profile: UserProfile | None = None

    @property
    def profile(self) -> UserProfile:
        """Lazy-load and return the user profile."""
        if self._profile is None:
            self._profile = self._load()
        return self._profile

    def _load(self) -> UserProfile:
        """Load profile from disk or create default."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                return UserProfile.model_validate(data)
            except Exception as e:
                logger.warning(f"Failed to load profile, creating default: {e}")
        return UserProfile()

    def save(self) -> None:
        """Persist current profile to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.profile.touch()
        self._path.write_text(
            self.profile.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug(f"Profile saved to {self._path}")

    # ── Decision Recording & Learning ──────────────────

    def record_decision(self, decision: DecisionRecord) -> None:
        """Record a decision and update learned preferences."""
        self.profile.record_decision(decision)

        # Learn from accepted decisions
        if decision.action == UserAction.ACCEPT:
            self._learn_from_acceptance(decision)
        elif decision.action == UserAction.MODIFY:
            self._learn_from_modification(decision)

        self.save()

    def _learn_from_acceptance(self, decision: DecisionRecord) -> None:
        """Extract positive patterns from an accepted proposal."""
        # If user accepted a structure method, boost its stats
        stage = decision.stage
        context = decision.context.lower()

        # Check for known structure methods in the context
        for sp in self.profile.preferred_structures:
            if sp.method_name.lower() in context:
                sp.usage_count += 1
                sp.success_rate = min(1.0, sp.success_rate + 0.05)
                sp.last_used = decision.timestamp

    def _learn_from_modification(self, decision: DecisionRecord) -> None:
        """Learn from what user changed (their actual preference)."""
        if not decision.modification:
            return

        mod = decision.modification.lower()
        stage = decision.stage

        # Detect patterns in modifications
        if "更多" in mod or "加" in mod:
            if "对白" in mod or "对话" in mod:
                self.profile.dialogue_style.monologue_frequency = "frequent"
            if "内心" in mod or "独白" in mod:
                self.profile.dialogue_style.monologue_frequency = "frequent"
            if "动作" in mod or "镜头" in mod:
                self.profile.visual_preferences.movement_preference = "dynamic"

        if "简洁" in mod or "少" in mod or "精简" in mod:
            self.profile.style_preferences.dialogue_density = "sparse"
            if "对白" in mod:
                self.profile.dialogue_style.formality_level = "stylized"

        if "情感" in mod or "情绪" in mod or "虐" in mod:
            self.profile.style_preferences.preferred_tones.append("emotional")

    # ── Pattern Extraction from Project Memory ───────────

    def extract_patterns_from_project(
        self,
        project_id: str,
        decisions: list[DecisionRecord],
    ) -> list[Pattern]:
        """Analyze project decisions to extract reusable patterns."""
        patterns: list[Pattern] = []

        # Group modifications by type
        mod_decisions = [d for d in decisions if d.action == UserAction.MODIFY]
        accept_decisions = [d for d in decisions if d.action == UserAction.ACCEPT]

        # Detect repeated modification themes
        mod_themes: dict[str, int] = {}
        for d in mod_decisions:
            if d.modification:
                theme = self._classify_modification(d.modification)
                mod_themes[theme] = mod_themes.get(theme, 0) + 1

        for theme, count in mod_themes.items():
            if count >= 2:  # Only extract if repeated
                patterns.append(Pattern(
                    type=self._theme_to_pattern_type(theme),
                    description=f"User prefers {theme} ({count} modifications)",
                    source_project=project_id,
                    confidence=min(0.9, 0.4 + count * 0.15),
                    usage_count=count,
                ))

        # Detect consistently accepted structures
        for d in accept_decisions:
            structure_match = self._detect_structure_in_context(d.context)
            if structure_match:
                patterns.append(Pattern(
                    type=PatternType.STRUCTURE_PREFERENCE,
                    description=f"Consistently accepts {structure_match} approach",
                    source_project=project_id,
                    confidence=0.7,
                    usage_count=1,
                ))

        return patterns

    def _classify_modification(self, modification: str) -> str:
        """Classify a modification into a theme category."""
        mod_lower = modification.lower()
        if any(w in mod_lower for w in ["对白", "对话", "台词"]):
            return "richer_dialogue"
        if any(w in mod_lower for w in ["情感", "情绪", "内心", "心理"]):
            return "more_emotional_depth"
        if any(w in mod_lower for w in ["节奏", "快", "慢", "拖"]):
            return "pacing_adjustment"
        if any(w in mod_lower for w in ["视觉", "画面", "镜头", "景别"]):
            return "visual_style_change"
        if any(w in mod_lower for w in ["角色", "人物", "性格"]):
            return "character_adjustment"
        return "general_refinement"

    def _theme_to_pattern_type(self, theme: str) -> PatternType:
        mapping = {
            "richer_dialogue": PatternType.DIALOGUE_PATTERN,
            "more_emotional_depth": PatternType.STYLE_PREFERENCE,
            "pacing_adjustment": PatternType.STYLE_PREFERENCE,
            "visual_style_change": PatternType.VISUAL_PATTERN,
            "character_adjustment": PatternType.STYLE_PREFERENCE,
        }
        return mapping.get(theme, PatternType.WORKFLOW_OPTIMIZATION)

    def _detect_structure_in_context(self, context: str) -> str | None:
        """Detect if a known story structure is mentioned."""
        context_lower = context.lower()
        structure_keywords = {
            "save-the-cat": ["save the cat", "救猫咪", "15节拍"],
            "story-circle": ["story circle", "故事圈", "dan harmon", "8段"],
            "three-act": ["three act", "三幕", "幕式"],
            "hero-journey": ["hero's journey", "英雄之旅", "joseph campbell"],
        }
        for name, keywords in structure_keywords.items():
            if any(kw in context_lower for kw in keywords):
                return name
        return None

    # ── Query API ───────────────────────────────────────

    def get_recommended_skills_for_stage(self, stage: str) -> list[str]:
        """Recommend skills based on historical usage success."""
        recommendations: list[tuple[str, float]] = []

        for sp in self.profile.preferred_structures:
            if sp.success_rate > 0.5 and sp.usage_count >= 1:
                recommendations.append((sp.method_name, sp.success_rate))

        # Sort by confidence descending
        recommendations.sort(key=lambda x: x[1], reverse=True)
        return [name for name, _ in recommendations]

    def get_stage_hints(self, stage: str) -> str:
        """Generate a hint string for agents about user preferences at this stage."""
        hints: list[str] = []

        if stage == PipelineStage.SCRIPTWRITING.value:
            ds = self.profile.dialogue_style
            hints.append(f"Dialogue style: {ds.formality_level.value}")
            hints.append(f"Subtext preference: {ds.subtext_preference.value}")
            hints.append(f"Monologue frequency: {ds.monologue_frequency.value}")

        elif stage == PipelineStage.STORYBOARDING.value:
            vp = self.profile.visual_preferences
            hints.append(f"Movement preference: {vp.movement_preference}")
            hints.append(f"Distribution: {json.dumps(vp.shot_size_distribution)}")

        elif stage == PipelineStage.STRUCTURING.value:
            if self.profile.preferred_structures:
                top = self.profile.preferred_structures[0]
                hints.append(
                    f"User frequently uses: {top.display_name} "
                    f"(success rate: {top.success_rate:.0%})"
                )

        return "\n".join(hints) if hints else ""


# Global singleton
_profile_manager: ProfileManager | None = None


def get_profile_manager() -> ProfileManager:
    global _profile_manager
    if _profile_manager is None:
        _profile_manager = ProfileManager()
    return _profile_manager
