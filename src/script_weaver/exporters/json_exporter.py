"""JSON exporter — full project state as structured JSON."""

from __future__ import annotations

from pathlib import Path

from script_weaver.core.types import ProjectState


def export_json(state: ProjectState, output_path: Path | None = None) -> str:
    """Export project state as formatted JSON.

    Args:
        state: The complete project state
        output_path: If provided, write to file. Otherwise return string.

    Returns:
        JSON string of the full project state.
    """
    json_str = state.model_dump_json(indent=2, ensure_ascii=False)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json_str, encoding="utf-8")

    return json_str
