"""Core Pydantic data models for Script-Weaver.

All artifacts in the pipeline are defined here as validated Pydantic models.
This is the single source of truth for data shapes — every other module imports from here.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────
# Constants & Enums
# ────────────────────────────────────────────────────────


class PipelineStage(str, Enum):
    """All pipeline stages that agents operate in."""
    IDEATION = "ideation"
    STRUCTURING = "structuring"
    CHARACTER_DESIGN = "character_design"
    SCENE_DESIGN = "scene_design"
    ART_DIRECTION = "art_direction"
    SCRIPTWRITING = "scriptwriting"
    STORYBOARDING = "storyboarding"
    REVIEW = "review"


class ProjectStatus(str, Enum):
    """Lifecycle status of a project."""
    IDEA_INPUT = "idea_input"
    REFINING = "refining"
    STRUCTURED = "structured"
    DESIGNING = "designing"
    SCRIPTING = "scripting"
    STORYBOARDING = "storyboarding"
    REVIEWING = "reviewing"
    COMPLETE = "complete"


class FormatType(str, Enum):
    """Content format types."""
    SHORT_FILM = "short_film"
    FEATURE = "feature"
    EPISODE = "episode"
    COMMERCIAL = "commercial"


# ────────────────────────────────────────────────────────
# Script-specific Enums
# ────────────────────────────────────────────────────────


class ShotSize(str, Enum):
    """Shot size / framing (景别)."""
    EXTREME_LONG_SHOT = "extreme_long_shot"       # 大远景 ELS
    LONG_SHOT = "long_shot"                         # 远景 LS
    FULL_SHOT = "full_shot"                         # 全景 FS
    MEDIUM_LONG_SHOT = "medium_long_shot"           # 中远景 MLS
    MEDIUM_SHOT = "medium_shot"                     # 中景 MS
    MEDIUM_CLOSE_UP = "medium_close_up"             # 中近景 MCU
    CLOSE_UP = "close_up"                           # 特写 CU
    EXTREME_CLOSE_UP = "extreme_close_up"           # 大特写 ECU


class CameraMovement(str, Enum):
    """Camera movement types (机位运动)."""
    STATIC = "static"                               # 固定机位
    PUSH_IN = "push_in"                             # 推镜头
    PULL_OUT = "pull_out"                           # 拉镜头
    PAN_LEFT = "pan_left"                           # 左摇
    PAN_RIGHT = "pan_right"                         # 右摇
    TILT_UP = "tilt_up"                             # 上仰
    TILT_DOWN = "tilt_down"                         # 下俯
    DOLLY = "dolly"                                 # 移动跟拍
    TRACKING = "tracking"                           # 跟踪镜头
    ARC = "arc"                                     # 弧形运动
    CRANE_UP = "crane_up"                           # 升降（升）
    CRANE_DOWN = "crane_down"                       # 升降（降）
    HANDHELD = "handheld"                           # 手持
    STEADICAM = "steadicam"                         # 斯坦尼康
    AERIAL = "aerial"                               # 航拍
    ZOOM_IN = "zoom_in"                             # 变焦推进
    ZOOM_OUT = "zoom_out"                           # 变焦拉远


class CameraAngle(str, Enum):
    """Camera angle types (拍摄角度)."""
    EYE_LEVEL = "eye_level"                         # 平视
    LOW_ANGLE = "low_angle"                         # 仰拍
    HIGH_ANGLE = "high_angle"                       # 俯拍
    DUTCH_ANGLE = "dutch_angle"                     # 倾斜角/荷兰角
    BIRD_EYE = "bird_eye"                           # 鸟瞰
    OVER_SHOULDER = "over_shoulder"                 # 过肩镜头
    POINT_OF_VIEW = "point_of_view"                 # 主观视角 POV
    TWO_SHOT = "two_shot"                           # 双人镜头


class SceneLocationType(str, Enum):
    INT = "INT."
    EXT = "EXT."
    INT_EXT = "INT./EXT."


class TransitionType(str, Enum):
    CUT = "cut"
    FADE_IN = "fade_in"
    FADE_OUT = "fade_out"
    DISSOLVE = "dissolve"
    SMASH_CUT = "smash_cut"
    MATCH_CUT = "match_cut"
    JUMP_CUT = "jump_cut"
    CROSS_DISSOLVE = "cross_dissolve"
    HARD_CUT = "hard_cut"
    WIPE = "wipe"


class CharacterRole(str, Enum):
    PROTAGONIST = "protagonist"
    ANTAGONIST = "antagonist"
    SUPPORTING = "supporting"
    EXTRA = "extra"


class UserAction(str, Enum):
    ACCEPT = "accept"
    MODIFY = "modify"
    REJECT = "reject"
    REFINE = "refine"


class SkillSourceFormat(str, Enum):
    NATIVE_YAML = "native_yaml"
    NATIVE_JSON = "native_json"
    CLAUDE_CODE_MD = "claude_code_md"
    UNKNOWN = "unknown"


class PatternType(str, Enum):
    STRUCTURE_PREFERENCE = "structure_preference"
    STYLE_PREFERENCE = "style_preference"
    DIALOGUE_PATTERN = "dialogue_pattern"
    VISUAL_PATTERN = "visual_pattern"
    WORKFLOW_OPTIMIZATION = "workflow_optimization"


# ────────────────────────────────────────────────────────
# Project Metadata & State
# ────────────────────────────────────────────────────────


class ProjectMeta(BaseModel):
    """Project-level metadata."""
    id: str = Field(default_factory=lambda: uuid4().hex)
    title: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    status: ProjectStatus = ProjectStatus.IDEA_INPUT
    genre: str | None = None
    format_type: FormatType = FormatType.SHORT_FILM
    target_duration_seconds: int | None = None
    language: Literal["zh-CN"] = "zh-CN"

    def touch(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now().isoformat()


# ────────────────────────────────────────────────────────
# Outline / Structure Models
# ────────────────────────────────────────────────────────


class BasicInfo(BaseModel):
    """Basic story information from the outline stage."""
    logline: str = ""                              # 一句话梗概
    genre: str = ""                                # e.g., "古装玄幻"
    theme: str = ""                                # Core theme
    tone: str = ""                                 # e.g., "虐心", "热血"
    target_audience: str | None = None
    episode_count: int = 1
    estimated_total_duration: str = ""             # e.g., "约30分钟"


class OutlineCharacterSummary(BaseModel):
    """Brief character summary within the outline."""
    name: str = ""
    role: str = ""                                 # e.g., "主角", "反派", "配角"
    core_motivation: str = ""
    character_arc: str | None = None


class PlotBeat(BaseModel):
    """A single plot beat / story beat."""
    sequence_number: int = 0
    title: str = ""
    synopsis: str = ""                              # Paragraph-level summary
    emotional_arc: str | None = None
    key_characters: list[str] = Field(default_factory=list)
    setting: str | None = None


class Outline(BaseModel):
    """Structured story outline produced by the Structurer agent."""
    basic_info: BasicInfo = Field(default_factory=BasicInfo)
    main_characters: list[OutlineCharacterSummary] = Field(
        default_factory=list
    )
    plot_outline: list[PlotBeat] = Field(default_factory=list)


# ────────────────────────────────────────────────────────
# Character Design Model
# ────────────────────────────────────────────────────────


class Character(BaseModel):
    """Detailed character design produced by CharacterDesigner agent."""
    id: str = Field(default_factory=lambda: f"char_{uuid4().hex[:8]}")
    name: str = ""
    role: CharacterRole = CharacterRole.SUPPORTING

    # Textual design
    appearance: str = ""                            # Detailed visual description
    personality: str = ""
    costume_description: str | None = None
    key_props: list[str] = Field(default_factory=list)

    # Narrative role
    backstory: str | None = None
    motivation: str = ""
    relationship_map: dict[str, str] = Field(     # {name: relationship}
        default_factory=dict
    )

    # For downstream image generation
    image_prompt: str | None = None
    image_reference_url: str | None = None


# ────────────────────────────────────────────────────────
# Scene Design Model
# ────────────────────────────────────────────────────────


class SceneDesign(BaseModel):
    """Scene/environment design produced by SceneDesigner agent."""
    id: str = Field(default_factory=lambda: f"scene_{uuid4().hex[:8]}")
    name: str = ""

    location_type: Literal["interior", "exterior", "mixed"] = "interior"
    environment: str = ""                          # Detailed environmental description
    time_of_day: str = ""                          # e.g., "深夜", "黄昏"
    weather: str | None = None

    mood: str = ""                                 # e.g., "阴森", "温暖"
    lighting_description: str | None = None
    color_palette: list[str] = Field(default_factory=list)

    key_elements: list[str] = Field(default_factory=list)

    image_prompt: str | None = None
    image_reference_url: str | None = None


# ────────────────────────────────────────────────────────
# Art Style Model
# ────────────────────────────────────────────────────────


class ArtStyle(BaseModel):
    """Visual art style guide produced by ArtDirector agent."""
    overall_style: str = ""                        # e.g., "真人古风", "赛博朋克"
    color_palette_primary: list[str] = Field(default_factory=list)
    color_palette_secondary: list[str] = Field(default_factory=list)
    color_palette_accent: list[str] | None = None
    lighting_style: str = ""                        # e.g., "冷暖对比强烈"
    atmosphere: str = ""
    reference_aesthetics: list[str] = Field(default_factory=list)
    texture_notes: str | None = None
    composition_principles: list[str] = Field(default_factory=list)


# ────────────────────────────────────────────────────────
# Script Models (ScreenJSON-inspired)
# ────────────────────────────────────────────────────────


class DialogueBlock(BaseModel):
    """A dialogue block in a script."""
    character_name: str = ""
    parenthetical: str | None = None               # Stage direction in parentheses
    dialogue: str = ""
    emotion: str | None = None                     # Intended delivery emotion


class ActionBlock(BaseModel):
    """An action/description block in a script."""
    description: str = ""
    focus: str | None = None                       # What to emphasize visually


class TransitionBlock(BaseModel):
    type: TransitionType = TransitionType.CUT
    description: str | None = None


class ScriptBlock(BaseModel):
    """Discriminated union of all script block types."""
    block_type: Literal["scene_heading", "action", "dialogue", "transition"]
    # Content varies by block_type; stored as raw dict for flexibility
    content: dict[str, Any] = Field(default_factory=dict)


class ScriptSceneHeading(BaseModel):
    scene_number: str = ""
    int_ext: SceneLocationType = SceneLocationType.INT
    location: str = ""
    time_of_day: str = ""


class ScriptScene(BaseModel):
    """A single scene in the screenplay."""
    scene_id: str = Field(default_factory=lambda: f"sc_{uuid4().hex[:8]}")
    heading: ScriptSceneHeading = Field(default_factory=ScriptSceneHeading)
    blocks: list[ScriptBlock] = Field(default_factory=list)
    characters_involved: list[str] = Field(default_factory=list)
    scene_design_id: str | None = None              # Link to SceneDesign
    estimated_duration_seconds: float | None = None


class Script(BaseModel):
    """Complete screenplay produced by ScriptWriter agent."""
    title: str = ""
    scenes: list[ScriptScene] = Field(default_factory=list)
    total_estimated_duration: float | None = None
    notes: str | None = None


# ────────────────────────────────────────────────────────
# Storyboard / Shot Models
# ────────────────────────────────────────────────────────


class Shot(BaseModel):
    """A single shot in the storyboard — the core unit for downstream video generation."""
    shot_id: str = Field(default_factory=lambda: f"shot_{__import__('uuid').uuid4().hex[:8]}")
    scene_id: str = ""                              # Parent script scene reference
    sequence_number: int = 0                        # Order within scene

    # Visual composition
    shot_size: ShotSize = ShotSize.MEDIUM_SHOT
    camera_angle: CameraAngle = CameraAngle.EYE_LEVEL
    camera_movement: CameraMovement = CameraMovement.STATIC
    movement_description: str | None = None          # Natural language detail

    # Content
    visual_description: str = ""                    # What's visible in frame (detailed)
    action_description: str | None = None
    dialogue: str | None = None                     # Overlaid/subtitled dialogue
    voiceover: str | None = None                    # VO narration
    on_screen_text: str | None = None               # Title cards, subtitles

    # Audio
    sound_effects: list[str] = Field(default_factory=list)
    music_cue: str | None = None
    music_mood: str | None = None

    # Timing
    duration_seconds: float = Field(default=3.0, gt=0)
    transition_to_next: TransitionType = TransitionType.CUT

    # Downstream video generation
    image_prompt: str | None = None                 # First-frame image gen prompt
    video_prompt: str | None = None                 # Video generation prompt
    negative_prompt: str | None = None
    reference_image_url: str | None = None


class Storyboard(BaseModel):
    """Complete storyboard produced by StoryboardArtist agent."""
    shots: list[Shot] = Field(default_factory=list)
    total_shot_count: int = 0
    total_estimated_duration: float = 0.0
    aspect_ratio: Literal["16:9", "9:16", "1:1", "21:9"] = "16:9"
    fps: int = 24
    notes: str | None = None

    def compute_totals(self) -> None:
        """Recalculate derived fields from shots list."""
        self.total_shot_count = len(self.shots)
        self.total_estimated_duration = round(
            sum(s.duration_seconds for s in self.shots), 2
        )


# ────────────────────────────────────────────────────────
# Visual Highlights
# ────────────────────────────────────────────────────────


class VisualHighlight(BaseModel):
    """A key cinematic moment identified for emphasis."""
    id: str = Field(default_factory=lambda: f"vh_{uuid4().hex[:8]}")
    title: str = ""
    description: str = ""
    related_shot_ids: list[str] = Field(default_factory=list)
    emotional_impact: str | None = None
    visual_technique: str | None = None


# ────────────────────────────────────────────────────────
# Skill Models
# ────────────────────────────────────────────────────────


class Example(BaseModel):
    """A few-shot example for skill prompt engineering."""
    input: str = ""
    output: str = ""


class Skill(BaseModel):
    """Unified internal representation of a skill (format-agnostic)."""
    id: str = ""                                    # Unique ID, e.g., "save-the-cat"
    name: str = ""                                  # Display name
    stage: PipelineStage | None = None              # Primary applicable stage
    description: str = ""
    version: str = "1.0"
    source_type: Literal["builtin", "custom", "url"] = "builtin"
    source_format: SkillSourceFormat = SkillSourceFormat.NATIVE_YAML
    file_path: str = ""

    # Core content
    prompt_injection: str = ""                      # Injected into agent system prompt
    output_schema_override: dict[str, Any] | None = None
    constraints: list[str] = Field(default_factory=list)
    examples: list[Example] = Field(default_factory=list)

    # Extra metadata preserved from original format
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class SkillBinding(BaseModel):
    """Per-project activation configuration for a skill."""
    skill_id: str = ""
    active: bool = True
    priority: int = 0                               # Higher = applied first
    params: dict[str, Any] = Field(default_factory=dict)
    activated_at: str | None = None
    notes: str = ""


# ────────────────────────────────────────────────────────
# Grows With User: Memory & Profile Models
# ────────────────────────────────────────────────────────


class DecisionRecord(BaseModel):
    """Record of a user decision at a specific pipeline stage."""
    stage: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    context: str = ""                               # What the AI proposed
    action: UserAction = UserAction.ACCEPT
    modification: str | None = None                  # How user changed it
    reasoning: str | None = None                     # User's explanation (if given)


class FeedbackRecord(BaseModel):
    """User feedback on an artifact."""
    artifact_type: str = ""                         # e.g., "outline", "script"
    artifact_id: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    feedback: str = ""
    sentiment: Literal["positive", "negative", "neutral"] = "neutral"


class Pattern(BaseModel):
    """A reusable pattern extracted from project experience."""
    id: str = Field(default_factory=lambda: f"pat_{uuid4().hex[:8]}")
    type: PatternType = PatternType.STYLE_PREFERENCE
    description: str = ""
    source_project: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    usage_count: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    def boost_confidence(self, amount: float = 0.1) -> None:
        """Increase confidence with each successful use."""
        self.confidence = min(1.0, self.confidence + amount)
        self.usage_count += 1


class ProjectMemory(BaseModel):
    """Experience memory for a single project."""
    project_id: str = ""
    decisions: list[DecisionRecord] = Field(default_factory=list)
    feedback_records: list[FeedbackRecord] = Field(default_factory=list)
    extracted_patterns: list[Pattern] = Field(default_factory=list)

    def record_decision(self, decision: DecisionRecord) -> None:
        """Add a decision record."""
        self.decisions.append(decision)

    def record_feedback(self, feedback: FeedbackRecord) -> None:
        """Add a feedback record."""
        self.feedback_records.append(feedback)

    def add_pattern(self, pattern: Pattern) -> None:
        """Add an extracted pattern."""
        self.extracted_patterns.append(pattern)


# ────────────────────────────────────────────────────────
# User Profile (cross-project persistence)
# ────────────────────────────────────────────────────────


class StylePreferences(BaseModel):
    """User's creative style preferences (auto-learned + editable)."""
    preferred_genres: list[str] = Field(default_factory=list)
    preferred_tones: list[str] = Field(default_factory=list)
    narrative_pace: Literal["slow", "medium", "fast", "varied"] = "medium"
    dialogue_density: Literal["sparse", "balanced", "dense"] = "balanced"
    visual_style_preference: str = ""                # e.g., "cinematic", "naturalistic"
    custom_notes: str = ""


class StructurePreference(BaseModel):
    """A preferred story structure method with usage stats."""
    method_name: str = ""                           # e.g., "save-the-cat", "three-act"
    display_name: str = ""
    usage_count: int = 0
    last_used: str | None = None
    success_rate: float = Field(default=0.5, ge=0.0, le=1.0)


class CharacterHabits(BaseModel):
    """Learned patterns about how the user designs characters."""
    naming_convention: str | None = None            # e.g., "meaningful Chinese names"
    typical_cast_size_range: tuple[int, int] = (2, 6)
    prefers_complex_arcs: bool = True
    common_archetypes: list[str] = Field(default_factory=list)


class DialogueStyleProfile(BaseModel):
    """User's dialogue style preferences."""
    formality_level: Literal["formal", "colloquial", "mixed", "stylized"] = "mixed"
    subtext_preference: Literal["explicit", "implicit", "mixed"] = "mixed"
    humor_level: Literal["none", "light", "moderate", "heavy"] = "light"
    monologue_frequency: Literal["rare", "occasional", "frequent"] = "occasional"
    dialect_notes: str = ""


class VisualPreferences(BaseModel):
    """User's visual/art preferences."""
    preferred_aspect_ratios: list[str] = Field(
        default_factory=lambda: ["16:9"]
    )
    shot_size_distribution: dict[str, float] = Field(   # Preference weights
        default_factory=lambda: {
            "wide": 0.15, "medium": 0.4, "close_up": 0.35, "extreme": 0.1
        }
    )
    movement_preference: Literal["static", "dynamic", "mixed"] = "mixed"
    color_temperature_hint: str | None = None         # e.g., "warm", "cool", "high contrast"
    lighting_preference: str = ""                     # e.g., "naturalistic", "dramatic"


class UserStats(BaseModel):
    """Aggregate statistics across all projects."""
    total_projects: int = 0
    completed_projects: int = 0
    total_decisions_recorded: int = 0
    total_feedback_given: int = 0
    most_used_structure: str | None = None
    favorite_genre: str | None = None


class UserProfile(BaseModel):
    """Cross-project user creative profile. Persisted at ~/.scriptweaver/user_profile.json."""
    user_id: str = "default"
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    style_preferences: StylePreferences = Field(default_factory=StylePreferences)
    preferred_structures: list[StructurePreference] = Field(default_factory=list)
    character_habits: CharacterHabits = Field(default_factory=CharacterHabits)
    dialogue_style: DialogueStyleProfile = Field(default_factory=DialogueStyleProfile)
    visual_preferences: VisualPreferences = Field(default_factory=VisualPreferences)
    stats: UserStats = Field(default_factory=UserStats)

    def touch(self) -> None:
        """Update timestamp."""
        self.updated_at = datetime.now().isoformat()

    def record_decision(self, decision: DecisionRecord) -> None:
        """Record a decision and update stats."""
        self.stats.total_decisions_recorded += 1
        self.touch()

    def record_project_completed(self) -> None:
        """Mark a project as completed."""
        self.stats.completed_projects += 1
        self.touch()

    def get_recommended_skills_for_stage(self, stage: str) -> list[str]:
        """Recommend skills based on historical preferences."""
        recommendations = []
        for sp in self.preferred_structures:
            if sp.success_rate > 0.6 and sp.usage_count >= 2:
                recommendations.append(sp.method_name)
        return recommendations

    def confidence_for_pattern(self, pattern_type: str, value: str) -> float:
        """Return confidence score for a specific pattern preference."""
        # Simple implementation — can be enhanced with pattern matching
        if pattern_type == "structure":
            for sp in self.preferred_structures:
                if sp.method_name == value:
                    return sp.success_rate
        return 0.5


# ────────────────────────────────────────────────────────
# Review Models
# ────────────────────────────────────────────────────────


class ReviewIssue(BaseModel):
    """A single issue found during review."""
    severity: Literal["error", "warning", "suggestion"] = "suggestion"
    category: str = ""                              # e.g., "consistency", "pacing"
    description: str = ""
    affected_artifact: str = ""                      # e.g., "script.scenes[2]"
    suggestion: str | None = None


class ReviewReport(BaseModel):
    """Quality review report produced by Reviewer agent."""
    reviewer: str = "auto-reviewer"
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    overall_score: float = Field(default=0.0, ge=0.0, le=1.0)
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = ""
    passed: bool = False

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")


# ────────────────────────────────────────────────────────
# Top-Level Project State
# ────────────────────────────────────────────────────────


class ProjectState(BaseModel):
    """The canonical project state container — everything flows through this."""
    meta: ProjectMeta = Field(default_factory=ProjectMeta)

    # Input
    user_input: str = ""

    # Refined idea
    refined_idea: str | None = None

    # Structural artifacts
    outline: Outline | None = None

    # Design artifacts
    characters: list[Character] | None = None
    scenes: list[SceneDesign] | None = None
    art_style: ArtStyle | None = None

    # Content artifacts
    script: Script | None = None
    storyboard: Storyboard | None = None
    visual_highlights: list[VisualHighlight] | None = None

    # Skill configuration (per-project)
    skill_bindings: dict[str, SkillBinding] = Field(default_factory=dict)
    # {"structuring": {"save-the-cat": SkillBinding(...)}, ...}

    # Memory & growth
    memory: ProjectMemory = Field(default_factory=ProjectMemory)

    # Review history
    review_history: list[ReviewReport] = Field(default_factory=list)

    def touch(self) -> None:
        """Update metadata timestamp."""
        self.meta.touch()

    def current_stage_status(self) -> ProjectStatus:
        """Determine current status based on which artifacts exist."""
        if self.storyboard and self.visual_highlights:
            return ProjectStatus.COMPLETE
        if self.script:
            return ProjectStatus.STORYBOARDING
        if self.characters or self.scenes or self.art_style:
            return ProjectStatus.SCRIPTWRITING
        if self.outline:
            return ProjectStatus.DESIGNING
        if self.refined_idea:
            return ProjectStatus.STRUCTURED
        if self.user_input:
            return ProjectStatus.REFINING
        return ProjectStatus.IDEA_INPUT
