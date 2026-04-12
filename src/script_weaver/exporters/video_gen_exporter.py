"""Video Generation exporter — per-shot prompts for downstream video tools.

Exports storyboard shots in a format optimized for AI video generation platforms
like Seiko (商汤), Runway Gen-3, Kling, Pika, etc.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from script_weaver.core.types import ProjectState, Shot, Storyboard


def export_video_gen_prompts(
    state: ProjectState,
    output_dir: Path | None = None,
) -> dict[str, str]:
    """Export per-shot prompts optimized for video generation tools.

    Produces three outputs:
    1. JSON array of VideoGenShot objects
    2. CSV with shot-level data (for spreadsheet workflows)
    3. Per-shot text files (one per shot, for batch processing)

    Args:
        state: Complete project state with storyboard
        output_dir: Directory to write files into

    Returns:
        Dict with keys: "json", "csv", "shots_dir"
    """
    if not state.storyboard or not state.storyboard.shots:
        return {"error": "No storyboard available in project state"}

    sb = state.storyboard
    shots_data = []

    for i, shot in enumerate(sb.shots):
        shot_entry = _shot_to_video_gen(shot, i, state)
        shots_data.append(shot_entry)

    results: dict[str, str] = {}

    # JSON output
    json_str = json.dumps(shots_data, indent=2, ensure_ascii=False)
    results["json"] = json_str

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

        # Write JSON
        json_path = output_dir / "video_gen_shots.json"
        json_path.write_text(json_str, encoding="utf-8")

        # Write CSV
        csv_path = output_dir / "video_gen_shots.csv"
        _write_csv(shots_data, csv_path)

        # Write per-shot prompt files
        shots_dir = output_dir / "shots"
        shots_dir.mkdir(exist_ok=True)
        for entry in shots_data:
            shot_file = shots_dir / f"{entry['shot_id']}.txt"
            content = _format_shot_text(entry)
            shot_file.write_text(content, encoding="utf-8")

        results["csv"] = str(csv_path)
        results["shots_dir"] = str(shots_dir)

    return results


def _shot_to_video_gen(shot: Shot, index: int, state: ProjectState) -> dict:
    """Convert a Storyboard Shot to a VideoGen-ready dict."""
    # Get character context for this shot's scene
    char_context = ""
    if state.characters and shot.scene_id:
        for scene in (state.script.scenes if state.script else []):
            if scene.scene_id == shot.scene_id:
                char_names = scene.characters_involved
                if char_names:
                    chars = [c for c in (state.characters or []) if c.name in char_names]
                    char_context = "; ".join(
                        f"{c.name}: {c.appearance[:60]}" for c in chars[:3]
                    )
                break

    art_context = ""
    if state.art_style:
        art_context = (
            f"Style: {state.art_style.overall_style}; "
            f"Colors: {', '.join(state.art_style.color_palette_primary[:3])}; "
            f"Lighting: {state.art_style.lighting_style}"
        )

    return {
        "shot_id": shot.shot_id,
        "sequence_order": index + 1,
        "scene_id": shot.scene_id,

        # Image generation (first frame / reference)
        "image_prompt": shot.image_prompt or (
            f"{shot.visual_description}. "
            f"{art_context}. "
            f"Cinematic, {shot.camera_angle.value}, "
            f"{shot.shot_size.value.replace('_', ' ')}, "
            f"{state.storyboard.aspect_ratio or '16:9'}"
        ),
        "image_negative_prompt": shot.negative_prompt or (
            "low quality, blurry, distorted, deformed, ugly, "
            "bad anatomy, watermark, text, logo"
        ),
        "image_aspect_ratio": state.storyboard.aspect_ratio or "16:9",

        # Video generation
        "video_prompt": shot.video_prompt or (
            f"{shot.action_description or shot.visual_description}. "
            f"Camera: {shot.camera_movement.value.replace('_', ' ')}. "
            f"{char_context}"
        ),
        "video_negative_prompt": shot.negative_prompt or (
            "static image, no motion, low quality, blurry, distorted"
        ),

        # Timing
        "duration_seconds": shot.duration_seconds,
        "fps": state.storyboard.fps or 24,

        # Audio (separate track info)
        "dialogue_text": shot.dialogue or "",
        "voiceover_text": shot.voiceover or "",
        "music_description": shot.music_cue or "",
        "sfx_descriptions": "|".join(shot.sound_effects) if shot.sound_effects else "",

        # Editing
        "transition_in": _prev_transition(index, state),
        "transition_out": shot.transition_to_next.value,

        # Metadata linking back
        "source_shot_id": shot.shot_id,
        "shot_size": shot.shot_size.value,
        "camera_angle": shot.camera_angle.value,
        "camera_movement": shot.camera_movement.value,
    }


def _prev_transition(index: int, state: ProjectState) -> str:
    """Get transition INTO this shot from previous."""
    if index == 0:
        return "fade_in"
    prev_shot = state.storyboard.shots[index - 1] if state.storyboard else None
    if prev_shot:
        return prev_shot.transition_to_next.value
    return "cut"


def _write_csv(shots_data: list[dict], path: Path) -> None:
    """Write shots data as CSV."""
    if not shots_data:
        return

    fieldnames = [
        "sequence_order", "shot_id", "scene_id", "image_prompt",
        "video_prompt", "duration_seconds", "transition_in", "transition_out",
        "shot_size", "camera_angle", "camera_movement",
        "dialogue_text", "voiceover_text",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for shot in shots_data:
            # Truncate long fields for CSV readability
            row = {}
            for key in fieldnames:
                val = shot.get(key, "")
                if isinstance(val, str) and len(val) > 200:
                    val = val[:200] + "..."
                row[key] = val
            writer.writerow(row)


def _format_shot_text(entry: dict) -> str:
    """Format a single shot as a human-readable text file."""
    lines = [
        f"Shot: {entry['shot_id']}",
        f"Sequence: {entry['sequence_order']}",
        f"Scene: {entry['scene_id']}",
        "",
        f"Shot Size: {entry.get('shot_size', 'N/A')}",
        f"Camera Angle: {entry.get('camera_angle', 'N/A')}",
        f"Camera Movement: {entry.get('camera_movement', 'N/A')}",
        f"Duration: {entry.get('duration_seconds', 0)}s",
        f"Transition In: {entry.get('transition_in', 'N/A')}",
        f"Transition Out: {entry.get('transition_out', 'N/A')}",
        "",
        "=== IMAGE PROMPT ===",
        entry.get("image_prompt", ""),
        "",
        "=== VIDEO PROMPT ===",
        entry.get("video_prompt", ""),
        "",
    ]
    if entry.get("dialogue_text"):
        lines.extend([
            "=== DIALOGUE ===",
            entry["dialogue_text"],
            "",
        ])
    if entry.get("voiceover_text"):
        lines.extend([
            "=== VOICEOVER ===",
            entry["voiceover_text"],
            "",
        ])
    if entry.get("music_description"):
        lines.extend([
            "=== MUSIC ===",
            entry["music_description"],
            "",
        ])
    return "\n".join(lines)
