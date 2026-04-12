"""Fountain format exporter — compatible with Final Draft, Trelby, etc.

Fountain is a plain-text markup language for screenplays.
See: https://fountain.io/syntax
"""

from __future__ import annotations

from pathlib import Path

from script_weaver.core.types import (
    SceneLocationType,
    Script,
    ScriptBlock,
    TransitionType,
)


def export_fountain(script: Script, output_path: Path | None = None) -> str:
    """Export a Script object to Fountain markup format.

    Args:
        script: The Script model to export
        output_path: Optional file path to write to

    Returns:
        Fountain-formatted string
    """
    lines: list[str] = []

    # Title section
    if script.title:
        lines.append(f"Title: {script.title}")
        lines.append("")
        if script.notes:
            lines.append(f"Credit: Notes")
            lines.append(f"    {script.notes}")
            lines.append("")

    # Scenes
    for scene in script.scenes:
        # Scene heading
        heading = scene.heading
        int_ext = _map_int_ext(heading.int_ext)
        lines.append(f"{int_ext} {heading.location} - {heading.time_of_day}")

        # Blocks
        for block in scene.blocks:
            if block.block_type == "action":
                desc = block.content.get("description", "")
                lines.append(desc)
            elif block.block_type == "dialogue":
                dc = block.content
                name = dc.get("character_name", "")
                dialogue = dc.get("dialogue", "")
                parenthetical = dc.get("parenthetical")

                lines.append(name.upper())
                if parenthetical:
                    lines.append(f"({parenthetical})")
                lines.append(dialogue)
                lines.append("")  # Blank line after dialogue
            elif block.block_type == "transition":
                trans = block.content.get("type", "cut")
                trans_text = _map_transition(trans)
                lines.append(f"{trans_text}")

        lines.append("")  # Blank line between scenes

    result = "\n".join(lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result, encoding="utf-8")

    return result


def _map_int_ext(value: SceneLocationType) -> str:
    mapping = {
        SceneLocationType.INT: "INT",
        SceneLocationType.EXT: "EXT",
        SceneLocationType.INT_EXT: "INT./EXT.",
    }
    return mapping.get(value, "INT")


def _map_transition(value: str) -> str:
    mapping = {
        "cut_to": "CUT TO:",
        "fade_in": "FADE IN:",
        "fade_out": "FADE OUT.",
        "dissolve": "DISSOLVE TO:",
        "smash_cut": "SMASH CUT TO:",
        "match_cut": "MATCH CUT TO:",
        "jump_cut": "JUMP CUT TO:",
        "cross_dissolve": "CROSS DISSOLVE TO:",
        "hard_cut": "HARD CUT TO:",
        "wipe": "WIPE TO:",
    }
    return mapping.get(value, "CUT TO:")
