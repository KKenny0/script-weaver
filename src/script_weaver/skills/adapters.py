"""Skill format adapters — convert various file formats to unified Skill model.

Supports:
- Native YAML (.yaml/.yml) — script-weaver's own format
- Native JSON (.json) — script-weaver's own format
- Claude Code Markdown (.md) — Claude Code skill format with frontmatter

Extensible: implement BaseSkillAdapter for custom formats.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml
import frontmatter

from script_weaver.core.types import (
    Example,
    PipelineStage,
    Skill,
    SkillSourceFormat,
)


class BaseSkillAdapter(ABC):
    """Abstract base class for skill format adapters.

    Each adapter can parse a specific file format into the unified Skill model.
    """

    @abstractmethod
    def can_parse(self, file_path: Path, content: str | None = None) -> bool:
        """Check if this adapter can handle the given file."""

    @abstractmethod
    async def parse(self, file_path: Path) -> Skill:
        """Parse a skill file into the unified Skill model."""

    @abstractmethod
    async def export(self, skill: Skill, target_path: Path) -> None:
        """Export a unified Skill model back to this format."""


def _infer_stage_from_text(text: str) -> PipelineStage | None:
    """Heuristic: infer pipeline stage from text content (TRIGGER/keywords)."""
    text_lower = text.lower()
    stage_keywords = {
        PipelineStage.IDEATION: ["idea", "创意", "concept", "概念", "ideation"],
        PipelineStage.STRUCTURING: [
            "outline", "大纲", "structure", "结构", "beat", "节拍",
            "plot", "情节", "story structure",
        ],
        PipelineStage.CHARACTER_DESIGN: [
            "character", "角色", "人物", "persona", "protagonist",
            "antagonist",
        ],
        PipelineStage.SCENE_DESIGN: [
            "scene", "场景", "setting", "environment", "location",
            "world-building",
        ],
        PipelineStage.ART_DIRECTION: [
            "art", "美术", "visual style", "视觉风格", "cinematography",
            "aesthetic", "director",
        ],
        PipelineStage.SCRIPTWRITING: [
            "script", "剧本", "screenplay", "dialogue", "对白",
            "writing", "write", "编剧",
        ],
        PipelineStage.STORYBOARDING: [
            "storyboard", "分镜", "shot", "镜头", "camera",
            "frame", "visual", "film",
        ],
        PipelineStage.REVIEW: [
            "review", "审查", "quality", "check", "validate",
            "critique",
        ],
    }

    best_stage = None
    best_count = 0
    for stage, keywords in stage_keywords.items():
        count = sum(1 for kw in keywords if kw in text_lower)
        if count > best_count:
            best_count = count
            best_stage = stage

    return best_stage


def _extract_constraints(text: str) -> list[str]:
    """Extract constraint-like patterns from text."""
    constraints = []
    lines = text.split("\n")
    in_constraints_section = False
    for line in lines:
        stripped = line.strip()
        if any(kw in stripped.lower() for kw in [
            "约束", "constraint", "要求", "requirement", "必须", "must",
            "规则", "rule", "注意", "note", "禁止", "do not",
        ]):
            if stripped.startswith(("-", "*", "#")) or (
                stripped[0].isdigit() and "." in stripped[:3]
            ):
                constraints.append(stripped.lstrip("-*# 0123456789.). "))
    return constraints


def _extract_examples(text: str) -> list[Example]:
    """Extract few-shot examples from code blocks or structured sections."""
    examples = []
    # Look for example blocks
    import re
    pattern = r'(?:example|示例|例子|input|输入)[:\s]*\n(.*?)(?=(?:example|示例|output|输出)|$)'
    matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
    for match in matches[:5]:  # Limit to prevent abuse
        clean = match.strip()
        if len(clean) > 10 and len(clean) < 2000:
            examples.append(Example(input=clean, output=""))
    return examples


# ────────────────────────────────────────────────────────
# Native YAML Adapter
# ────────────────────────────────────────────────────────


class NativeYamlAdapter(BaseSkillAdapter):
    """Parse script-weaver native YAML skill files."""

    EXTENSIONS = {".yaml", ".yml"}

    def can_parse(self, file_path: Path, content: str | None = None) -> bool:
        return file_path.suffix in self.EXTENSIONS

    async def parse(self, file_path: Path) -> Skill:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"Invalid YAML skill: expected dict, got {type(raw)}")

        # Parse examples
        examples = []
        for ex in raw.get("examples", []):
            if isinstance(ex, dict):
                examples.append(Example(
                    input=ex.get("input", ""),
                    output=ex.get("output", ""),
                ))

        # Parse stage
        stage_str = raw.get("stage", "")
        try:
            stage = PipelineStage(stage_str) if stage_str else None
        except ValueError:
            stage = _infer_stage_from_text(raw.get("prompt_injection", ""))

        return Skill(
            id=raw.get("id", file_path.stem),
            name=raw.get("name", file_path.stem),
            stage=stage,
            description=raw.get("description", ""),
            version=raw.get("version", "1.0"),
            source_type="custom" if "custom" in str(file_path) else "builtin",
            source_format=SkillSourceFormat.NATIVE_YAML,
            file_path=str(file_path),
            prompt_injection=raw.get("prompt_injection", ""),
            output_schema_override=raw.get("output_schema_override"),
            constraints=raw.get("constraints", []),
            examples=examples,
            metadata={k: v for k, v in raw.items() if k not in {
                "id", "name", "stage", "description", "version",
                "source_type", "prompt_injection", "output_schema_override",
                "constraints", "examples",
            }},
        )

    async def export(self, skill: Skill, target_path: Path) -> None:
        data = {
            "id": skill.id,
            "name": skill.name,
            "stage": skill.stage.value if skill.stage else None,
            "description": skill.description,
            "version": skill.version,
            "prompt_injection": skill.prompt_injection,
            "output_schema_override": skill.output_schema_override,
            "constraints": skill.constraints,
            "examples": [{"input": e.input, "output": e.output} for e in skill.examples],
            **skill.metadata,
        }
        with open(target_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# ────────────────────────────────────────────────────────
# Native JSON Adapter
# ────────────────────────────────────────────────────────


class NativeJsonAdapter(BaseSkillAdapter):
    """Parse script-weaver native JSON skill files."""

    EXTENSIONS = {".json"}

    def can_parse(self, file_path: Path, content: str | None = None) -> bool:
        return file_path.suffix in self.EXTENSIONS

    async def parse(self, file_path: Path) -> Skill:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"Invalid JSON skill: expected dict, got {type(raw)}")

        examples = []
        for ex in raw.get("examples", []):
            if isinstance(ex, dict):
                examples.append(Example(input=ex.get("input", ""), output=ex.get("output", "")))

        stage_str = raw.get("stage", "")
        try:
            stage = PipelineStage(stage_str) if stage_str else None
        except ValueError:
            stage = _infer_stage_from_text(raw.get("prompt_injection", ""))

        return Skill(
            id=raw.get("id", file_path.stem),
            name=raw.get("name", file_path.stem),
            stage=stage,
            description=raw.get("description", ""),
            version=raw.get("version", "1.0"),
            source_type="custom" if "custom" in str(file_path) else "builtin",
            source_format=SkillSourceFormat.NATIVE_JSON,
            file_path=str(file_path),
            prompt_injection=raw.get("prompt_injection", ""),
            output_schema_override=raw.get("output_schema_override"),
            constraints=raw.get("constraints", []),
            examples=examples,
            metadata={k: v for k, v in raw.items() if k not in {
                "id", "name", "stage", "description", "version",
                "source_type", "prompt_injection", "output_schema_override",
                "constraints", "examples",
            }},
        )

    async def export(self, skill: Skill, target_path: Path) -> None:
        data = {
            "id": skill.id,
            "name": skill.name,
            "stage": skill.stage.value if skill.stage else None,
            "description": skill.description,
            "version": skill.version,
            "prompt_injection": skill.prompt_injection,
            "output_schema_override": skill.output_schema_override,
            "constraints": skill.constraints,
            "examples": [{"input": e.input, "output": e.output} for e in skill.examples],
            **skill.metadata,
        }
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ────────────────────────────────────────────────────────
# Claude Code Markdown Adapter
# ────────────────────────────────────────────────────────


class ClaudeCodeMdAdapter(BaseSkillAdapter):
    """Parse Claude Code native .md skill files.

    Claude Code skills use YAML frontmatter + markdown body.
    Frontmatter contains metadata (name, description, TRIGGER when, etc.)
    Body contains the actual instructions/prompt.

    Mapping:
    - frontmatter.name → Skill.name
    - frontmatter.description → Skill.description
    - frontmatter.TRIGGER when / trigger → used to infer stage
    - body (after frontmatter) → Skill.prompt_injection
    - "输出要求"/"要求" sections → constraints
    - code block examples → examples
    - All original frontmatter keys → metadata (preserved)
    """

    EXTENSIONS = {".md"}

    def can_parse(self, file_path: Path, content: str | None = None) -> bool:
        if file_path.suffix not in self.EXTENSIONS:
            return False
        # Check it looks like a Claude Code skill (has frontmatter)
        if content is None:
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                return False
        return content.startswith("---")

    async def parse(self, file_path: Path) -> Skill:
        post = frontmatter.load(str(file_path))

        # Extract metadata from frontmatter
        fm = post.metadata
        name = fm.get("name", file_path.stem)
        description = fm.get("description", fm.get("desc", ""))
        body = post.content.strip()

        # Infer stage from TRIGGER/trigger field or body content
        trigger_text = fm.get("trigger", fm.get("TRIGGER when", ""))
        combined_for_inference = f"{trigger_text} {body}"
        stage = _infer_stage_from_text(combined_for_inference)

        # Extract constraints from body
        constraints = _extract_constraints(body)

        # Extract examples from body
        examples = _extract_examples(body)

        # Preserve all original frontmatter as metadata
        metadata = {}
        for k, v in fm.items():
            if k not in ("name", "description", "desc"):
                metadata[k] = v
        metadata["original_frontmatter_keys"] = list(fm.keys())

        return Skill(
            id=self._make_id(name),
            name=name,
            stage=stage,
            description=description,
            version=fm.get("version", "1.0"),
            source_type="custom",
            source_format=SkillSourceFormat.CLAUDE_CODE_MD,
            file_path=str(file_path),
            prompt_injection=body,
            output_schema_override=None,  # MD format doesn't have schema overrides
            constraints=constraints,
            examples=examples,
            metadata=metadata,
        )

    async def export(self, skill: Skill, target_path: Path) -> None:
        """Export back to Claude Code MD format."""
        fm = dict(skill.metadata)
        fm["name"] = skill.name
        fm["description"] = skill.description
        if skill.stage:
            fm["stage"] = skill.stage.value

        body = skill.prompt_injection
        if skill.constraints:
            body += "\n\n## Constraints\n"
            for c in skill.constraints:
                body += f"- {c}\n"

        post = frontmatter.Post(body, **fm)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

    @staticmethod
    def _make_id(name: str) -> str:
        """Convert name to slug-style ID."""
        return name.lower().replace(" ", "-").replace("_", "-").strip("-")


# ────────────────────────────────────────────────────────
# Adapter Registry
# ────────────────────────────────────────────────────────

DEFAULT_ADAPTERS: list[BaseSkillAdapter] = [
    NativeYamlAdapter(),
    NativeJsonAdapter(),
    ClaudeCodeMdAdapter(),
]


class AdapterRegistry:
    """Registry of format adapters with auto-detection."""

    def __init__(self, adapters: list[BaseSkillAdapter] | None = None):
        self._adapters = adapters or list(DEFAULT_ADAPTERS)

    def register(self, adapter: BaseSkillAdapter) -> None:
        """Register a custom adapter."""
        self._adapters.append(adapter)

    def get_adapter(self, file_path: Path, content: str | None = None) -> BaseSkillAdapter:
        """Find the right adapter for a given file."""
        for adapter in self._adapters:
            if adapter.can_parse(file_path, content):
                return adapter
        raise ValueError(
            f"No adapter found for '{file_path}'. "
            f"Supported formats: YAML (.yaml), JSON (.json), Markdown (.md)"
        )

    async def load(self, file_path: Path) -> Skill:
        """Auto-detect format and load a skill file."""
        adapter = self.get_adapter(file_path)
        return await adapter.parse(file_path)

    async def load_all(self, directory: Path) -> list[Skill]:
        """Load all skill files from a directory."""
        skills = []
        if not directory.exists():
            return skills
        for file_path in sorted(directory.iterdir()):
            if file_path.is_file():
                try:
                    adapter = self.get_adapter(file_path)
                    skill = await adapter.parse(file_path)
                    skills.append(skill)
                except (ValueError, Exception) as e:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Failed to load skill '{file_path}': {e}"
                    )
        return skills
