"""Tool definitions for agent use.

Tools are functions that agents can call during their tool-use loop.
Each tool has a schema (name, description, parameters) that's sent to the LLM.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Coroutine

from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────
# Tool Schema
# ────────────────────────────────────────────────────────


class ToolParamSchema(BaseModel):
    """Schema for a single tool parameter."""
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    enum: list[str] | None = None


class ToolDefinition(BaseModel):
    """Complete definition of a tool that can be registered with an agent."""
    name: str
    description: str
    parameters: list[ToolParamSchema] = Field(default_factory=list)

    def to_api_schema(self) -> dict[str, Any]:
        """Convert to the format expected by LLM providers (OpenAI/Anthropic)."""
        properties = {}
        required = []
        for p in self.parameters:
            prop: dict[str, Any] = {"type": p.type, "description": p.description}
            if p.enum:
                prop["enum"] = p.enum
            properties[p.name] = prop
            if p.required:
                required.append(p.name)

        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }


ToolHandler = Callable[..., Coroutine[Any, Any, str]]


# ────────────────────────────────────────────────────────
# Built-in Tools
# ────────────────────────────────────────────────────────


async def read_state_tool(project_state_json: str) -> str:
    """Read the current project state.

    Returns a summary of all existing artifacts in the project state.
    Agents use this to understand what has been produced so far.
    """
    # The agent receives the full state as JSON string
    # We return it formatted for readability
    try:
        data = json.loads(project_state_json)
        # Return a concise summary of what exists
        parts = []
        meta = data.get("meta", {})
        parts.append(f"Project: {meta.get('title', 'untitled')}")
        parts.append(f"Status: {meta.get('status', 'unknown')}")

        artifacts = []
        if data.get("refined_idea"):
            artifacts.append("✓ Refined idea")
        if data.get("outline"):
            outline = data["outline"]
            basic = outline.get("basic_info", {})
            beats = outline.get("plot_outline", [])
            artifacts.append(
                f"✓ Outline ({len(beats)} beats) — {basic.get('logline', '')[:80]}"
            )
        if data.get("characters"):
            chars = data["characters"]
            names = [c.get("name", "?") for c in chars]
            artifacts.append(f"✓ Characters ({len(chars)}): {', '.join(names)}")
        if data.get("scenes"):
            scenes = data["scenes"]
            names = [s.get("name", "?") for s in scenes]
            artifacts.append(f"✓ Scenes ({len(scenes)}): {', '.join(names)}")
        if data.get("art_style"):
            artifacts.append(f"✓ Art Style — {data['art_style'].get('overall_style', '')}")
        if data.get("script"):
            script = data["script"]
            scenes_count = len(script.get("scenes", []))
            artifacts.append(f"✓ Script ({scenes_count} scenes)")
        if data.get("storyboard"):
            sb = data["storyboard"]
            artifacts.append(f"✓ Storyboard ({sb.get('total_shot_count', 0)} shots)")
        if data.get("visual_highlights"):
            vh = data["visual_highlights"]
            artifacts.append(f"✓ Visual Highlights ({len(vh)})")

        if artifacts:
            parts.append("\nExisting Artifacts:")
            parts.extend(f"  {a}" for a in artifacts)
        else:
            parts.append("\nNo artifacts generated yet.")

        return "\n".join(parts)
    except Exception as e:
        return f"Error reading state: {e}"


async def write_artifact_tool(
    artifact_type: str,
    content: str,
    project_state_json: str,
) -> str:
    """Write or update an artifact in the project state.

    This is the primary way agents produce output. They call this tool
    with structured JSON content for the artifact they're creating.

    Args:
        artifact_type: One of: refined_idea, outline, characters, scenes,
                       art_style, script, storyboard, visual_highlights
        content: JSON string of the artifact data
        project_state_json: Current project state (for validation context)
    """
    valid_types = {
        "refined_idea", "outline", "characters", "scenes",
        "art_style", "script", "storyboard", "visual_highlights",
    }
    if artifact_type not in valid_types:
        return f"Error: Invalid artifact_type '{artifact_type}'. Must be one of: {valid_types}"

    try:
        data = json.loads(content)
        # Validate it's proper JSON at minimum
        return json.dumps({
            "status": "success",
            "artifact_type": artifact_type,
            "message": f"{artifact_type} written successfully.",
            "data": data,
        }, ensure_ascii=False, indent=2)
    except json.JSONDecodeError as e:
        return f"Error: Content must be valid JSON. Parse error: {e}"


async def request_review_tool(artifact_type: str, description: str) -> str:
    """Request human review of an artifact before proceeding.

    Use this when you want the user to review your output before moving
    to the next pipeline stage.

    Args:
        artifact_type: The type of artifact to review
        description: Summary of what was produced for user review
    """
    return json.dumps({
        "status": "review_requested",
        "artifact_type": artifact_type,
        "description": description,
        "action": "PAUSE_FOR_REVIEW",
    }, ensure_ascii=False)


async def generate_image_prompt_tool(
    subject_type: str,
    subject_description: str,
    art_style_context: str,
) -> str:
    """Generate an optimized image generation prompt for a character or scene.

    Produces a prompt suitable for image generation models (Midjourney, SDXL, etc.).

    Args:
        subject_type: "character" or "scene"
        subject_description: Detailed description of the subject
        art_style_context: Art style guidelines to incorporate
    """
    # In a real implementation, this would use an LLM call to optimize the prompt.
    # For now, return a structured response indicating what would be produced.
    return json.dumps({
        "status": "prompt_generated",
        "subject_type": subject_type,
        "image_prompt": f"[Optimized image prompt for {subject_type}] "
                        f"Based on: {subject_description[:200]}... "
                        f"Style: {art_style_context[:100]}...",
        "note": "Image prompt optimization would use an LLM sub-call here.",
    }, ensure_ascii=False)


async def read_artifact_tool(
    artifact_type: str,
    project_state_json: str,
) -> str:
    """Read a specific artifact from the project state.

    Agents use this to get detailed content of a specific artifact
    rather than the full state summary.

    Args:
        artifact_type: The artifact type to read
        project_state_json: Full project state JSON
    """
    valid_types = {
        "refined_idea", "outline", "characters", "scenes",
        "art_style", "script", "storyboard", "visual_highlights",
    }
    try:
        state = json.loads(project_state_json)
        artifact = state.get(artifact_type)
        if artifact is None:
            return f"No '{artifact_type}' artifact found in current state."
        return json.dumps(artifact, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return "Error: Could not parse project state JSON."


# ────────────────────────────────────────────────────────
# Tool Registry
# ────────────────────────────────────────────────────────

# Map of tool name → (handler function, schema)
BUILTIN_TOOLS: dict[str, tuple[ToolHandler, ToolDefinition]] = {
    "read_state": (
        read_state_tool,
        ToolDefinition(
            name="read_state",
            description=(
                "Read the current project state to see all existing artifacts "
                "(outline, characters, scenes, script, storyboard, etc.). "
                "Use this first to understand what already exists."
            ),
            parameters=[
                ToolParamSchema(
                    name="project_state_json",
                    description="The current project state as a JSON string",
                    type="string",
                ),
            ],
        ),
    ),
    "write_artifact": (
        write_artifact_tool,
        ToolDefinition(
            name="write_artifact",
            description=(
                "Write or update an artifact in the project state. "
                "This is how you produce output — call this with your completed work."
            ),
            parameters=[
                ToolParamSchema(
                    name="artifact_type",
                    description=(
                        "Type of artifact: refined_idea, outline, characters, "
                        "scenes, art_style, script, storyboard, visual_highlights"
                    ),
                    type="string",
                ),
                ToolParamSchema(
                    name="content",
                    description="JSON string containing the complete artifact data",
                    type="string",
                ),
                ToolParamSchema(
                    name="project_state_json",
                    description="Current project state as JSON string for context",
                    type="string",
                ),
            ],
        ),
    ),
    "request_review": (
        request_review_tool,
        ToolDefinition(
            name="request_review",
            description=(
                "Request human review before proceeding to the next stage. "
                "Use this after completing an important artifact."
            ),
            parameters=[
                ToolParamSchema(
                    name="artifact_type",
                    description="Type of artifact to review",
                    type="string",
                ),
                ToolParamSchema(
                    name="description",
                    description="Summary of what was produced for the reviewer",
                    type="string",
                ),
            ],
        ),
    ),
    "generate_image_prompt": (
        generate_image_prompt_tool,
        ToolDefinition(
            name="generate_image_prompt",
            description=(
                "Generate an optimized prompt for AI image generation "
                "(for character portraits or scene references)."
            ),
            parameters=[
                ToolParamSchema(
                    name="subject_type",
                    description="'character' or 'scene'",
                    type="string",
                    enum=["character", "scene"],
                ),
                ToolParamSchema(
                    name="subject_description",
                    description="Detailed description of the character or scene",
                    type="string",
                ),
                ToolParamSchema(
                    name="art_style_context",
                    description="Art style guidelines to follow",
                    type="string",
                ),
            ],
        ),
    ),
    "read_artifact": (
        read_artifact_tool,
        ToolDefinition(
            name="read_artifact",
            description=(
                "Read a specific artifact's full content from the project state. "
                "More detailed than read_state which only shows summaries."
            ),
            parameters=[
                ToolParamSchema(
                    name="artifact_type",
                    description="Artifact type to read",
                    type="string",
                ),
                ToolParamSchema(
                    name="project_state_json",
                    description="Current project state as JSON string",
                    type="string",
                ),
            ],
        ),
    ),
}


def get_builtin_tool_schemas() -> list[dict[str, Any]]:
    """Get all built-in tools as API schemas for LLM consumption."""
    return [defn.to_api_schema() for _, defn in BUILTIN_TOOLS.values()]


def get_tool_handler(tool_name: str) -> ToolHandler | None:
    """Get the handler function for a built-in tool by name."""
    entry = BUILTIN_TOOLS.get(tool_name)
    return entry[0] if entry else None


async def execute_tool_call(tool_name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool call by name with parsed arguments."""
    handler = get_tool_handler(tool_name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        result = await handler(**arguments)
        return result
    except TypeError as e:
        return json.dumps({"error": f"Tool argument error: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Tool execution error: {e}"})
