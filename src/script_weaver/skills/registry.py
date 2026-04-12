"""Skill Registry — unified management entry for the skill plugin system.

This is the central point that agents and future UI/API code use to:
- Discover available skills
- Load skill content
- Activate/deactivate skills per project
- Build prompt context for agent execution
- Merge schema overrides

Design: CLI/Web API share this same registry instance.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from script_weaver.core.config import get_settings
from script_weaver.core.types import (
    Example,
    PipelineStage,
    Skill,
    SkillBinding,
)
from script_weaver.skills.adapters import AdapterRegistry, DEFAULT_ADAPTERS

logger = logging.getLogger(__name__)


class AgentSkillContext:
    """Pre-computed context for an agent's skill injection.

    Built by SkillRegistry.build_agent_context() and consumed by BaseAgent.
    """

    def __init__(
        self,
        system_additions: str = "",
        schema_patches: dict[str, Any] | None = None,
        constraints: list[str] | None = None,
        examples: list[Example] | None = None,
        active_skill_names: list[str] | None = None,
    ):
        self.system_additions = system_additions
        self.schema_patches = schema_patches or {}
        self.constraints = constraints or []
        self.examples = examples or []
        self.active_skill_names = active_skill_names or []


class SkillRegistry:
    """Central skill lifecycle manager.

    Usage:
        registry = SkillRegistry()
        await registry.discover()  # Load all skills from disk

        # Agent execution context
        ctx = registry.build_agent_context("structuring", project.skill_bindings)

        # Management operations
        await registry.install(path_or_url)
        await registry.activate(project_id, "save-the-cat")
        await registry.deactivate(project_id, "save-the-cat")
    """

    def __init__(self, adapter_registry: AdapterRegistry | None = None):
        settings = get_settings()
        self._adapters = adapter_registry or AdapterRegistry(DEFAULT_ADAPTERS)
        self._skills: dict[str, Skill] = {}           # skill_id → Skill
        self._builtin_dir = settings.skills_builtin_dir
        self._custom_dir = settings.skills_custom_dir

    # ── Discovery ────────────────────────────────────────

    async def discover(self, *extra_dirs: Path) -> list[Skill]:
        """Discover and load all skills from standard + extra directories."""
        all_dirs = [self._builtin_dir, self._custom_dir] + list(extra_dirs)
        all_skills: list[Skill] = []

        for directory in all_dirs:
            skills = await self._adapters.load_all(directory)
            all_skills.extend(skills)

        # Index by ID (later wins on collision)
        for skill in all_skills:
            self._skills[skill.id] = skill
            logger.debug(f"Discovered skill: {skill.id} ({skill.name}) @ stage={skill.stage}")

        return all_skills

    async def refresh(self) -> None:
        """Re-discover all skills from disk."""
        self._skills.clear()
        await self.discover()

    # ── Lookup ──────────────────────────────────────────

    def get(self, skill_id: str) -> Skill | None:
        """Get a skill by ID."""
        return self._skills.get(skill_id)

    def get_for_stage(self, stage: str) -> list[Skill]:
        """Get all skills applicable to a given pipeline stage."""
        return [
            s for s in self._skills.values()
            if s.stage is not None and s.stage.value == stage
        ]

    def list_all(self) -> list[Skill]:
        """List all discovered skills."""
        return list(self._skills.values())

    def list_by_stage(self) -> dict[str, list[Skill]]:
        """List skills grouped by stage."""
        result: dict[str, list[Skill]] = {}
        for skill in self._skills.values():
            stage_key = skill.stage.value if skill.stage else "_unassigned"
            result.setdefault(stage_key, []).append(skill)
        return result

    # ── Installation / Removal ───────────────────────────

    async def install(self, source: str | Path) -> Skill:
        """Install a new skill from file path or URL."""
        if isinstance(source, str):
            if source.startswith(("http://", "https://")):
                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(source)
                    resp.raise_for_status()
                    content = resp.text
                # Determine format from URL or content
                dest = self._custom_dir / source.rsplit("/", 1)[-1]
                if not dest.suffix:
                    dest = dest.with_suffix(".yaml")
                dest.write_text(content, encoding="utf-8")
                source = dest
            else:
                source = Path(source)

        skill = await self._adapters.load(Path(source))
        self._skills[skill.id] = skill
        logger.info(f"Installed skill: {skill.id} from {source}")
        return skill

    async def uninstall(self, skill_id: str) -> None:
        """Uninstall a custom skill."""
        skill = self._skills.get(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")
        if skill.source_type == "builtin":
            raise ValueError(f"Cannot uninstall built-in skill '{skill_id}'")

        file_path = Path(skill.file_path)
        if file_path.exists():
            file_path.unlink()
        del self._skills[skill_id]
        logger.info(f"Uninstalled skill: {skill_id}")

    # ── Activation (project-level) ───────────────────────

    def activate(
        self,
        bindings: dict[str, dict[str, SkillBinding]],
        stage: str,
        skill_id: str,
        priority: int = 0,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Activate a skill for a specific stage in a project."""
        if skill_id not in self._skills:
            raise ValueError(f"Unknown skill: {skill_id}")

        stage_bindings = bindings.setdefault(stage, {})
        stage_bindings[skill_id] = SkillBinding(
            skill_id=skill_id,
            active=True,
            priority=priority,
            params=params or {},
            activated_at=datetime.now().isoformat(),
        )

    def deactivate(
        self,
        bindings: dict[str, dict[str, SkillBinding]],
        stage: str,
        skill_id: str,
    ) -> None:
        """Deactivate a skill for a specific stage."""
        stage_bindings = bindings.get(stage, {})
        binding = stage_bindings.get(skill_id)
        if binding:
            binding.active = False

    def reorder(
        self,
        bindings: dict[str, dict[str, SkillBinding]],
        stage: str,
        ordered_ids: list[str],
    ) -> None:
        """Reorder active skills by priority (first = highest priority)."""
        stage_bindings = bindings.get(stage, {})
        for i, skill_id in enumerate(ordered_ids):
            if skill_id in stage_bindings:
                stage_bindings[skill_id].priority = len(ordered_ids) - i

    def update_params(
        self,
        bindings: dict[str, dict[str, SkillBinding]],
        stage: str,
        skill_id: str,
        params: dict[str, Any],
    ) -> None:
        """Update user-defined parameters for a skill binding."""
        stage_bindings = bindings.get(stage, {})
        binding = stage_bindings.get(skill_id)
        if binding:
            binding.params.update(params)

    # ── Agent Context Building ───────────────────────────

    def build_agent_context(
        self,
        stage: str,
        bindings: dict[str, dict[str, SkillBinding]],
    ) -> AgentSkillContext:
        """Build all skill context for an agent at a given stage.

        This is called by BaseAgent before executing its tool-use loop.
        It aggregates all active skills' prompt injections, constraints,
        schema overrides, and examples into a single context object.
        """
        stage_bindings = bindings.get(stage, {})

        # Filter to only active bindings, sort by priority (desc)
        active_bindings = sorted(
            [(sid, b) for sid, b in stage_bindings.items() if b.active],
            key=lambda x: x[1].priority,
            reverse=True,
        )

        if not active_bindings:
            return AgentSkillContext()

        # Aggregate content from each active skill
        system_parts: list[str] = []
        all_constraints: list[str] = []
        all_examples: list[Example] = []
        schema_patches: dict[str, Any] = {}
        active_names: list[str] = []

        for skill_id, binding in active_bindings:
            skill = self._skills.get(skill_id)
            if skill is None:
                logger.warning(f"Active skill '{skill_id}' not found in registry")
                continue

            active_names.append(skill.name)

            # 1. Prompt injection (with param substitution)
            prompt = skill.prompt_injection
            if binding.params:
                for key, value in binding.params.items():
                    prompt = prompt.replace("{" + key + "}", str(value))
            if prompt.strip():
                system_parts.append(
                    f"\n## Active Skill: {skill.name}\n{prompt}\n"
                )

            # 2. Constraints
            all_constraints.extend(skill.constraints)

            # 3. Examples
            all_examples.extend(skill.examples)

            # 4. Schema override
            if skill.output_schema_override:
                schema_patches = deep_merge(schema_patches, skill.output_schema_override)

        return AgentSkillContext(
            system_additions="\n".join(system_parts),
            schema_patches=schema_patches if schema_patches else None,
            constraints=all_constraints if all_constraints else None,
            examples=all_examples if all_examples else None,
            active_skill_names=active_names,
        )

    # ── Listing APIs (for CLI/UI) ────────────────────────

    def list_available(self, stage: str | None = None) -> list[dict[str, Any]]:
        """List available skills with details (for CLI/UI display)."""
        skills = self.list_all()
        if stage:
            skills = [s for s in skills if s.stage is not None and s.stage.value == stage]

        return [
            {
                "id": s.id,
                "name": s.name,
                "stage": s.stage.value if s.stage else None,
                "description": s.description[:100],
                "version": s.version,
                "source": s.source_type,
                "format": s.source_format.value if hasattr(s.source_format, "value") else str(s.source_format),
            }
            for s in skills
        ]

    def list_active(
        self,
        bindings: dict[str, dict[str, SkillBinding]],
    ) -> dict[str, list[dict[str, Any]]]:
        """List currently active skills per stage."""
        result: dict[str, list[dict[str, Any]]] = {}
        for stage, stage_bindings in bindings.items():
            active = []
            for skill_id, binding in stage_bindings.items():
                if binding.active:
                    skill = self._skills.get(skill_id)
                    if skill:
                        active.append({
                            "id": skill_id,
                            "name": skill.name,
                            "priority": binding.priority,
                            "params": binding.params,
                            "notes": binding.notes,
                        })
            if active:
                result[stage] = active
        return result


def deep_merge(base: dict, update: dict) -> dict:
    """Deep merge two dicts (update takes precedence)."""
    result = base.copy()
    for k, v in update.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result
