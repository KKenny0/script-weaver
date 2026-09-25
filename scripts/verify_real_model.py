"""Run the real pipeline with stage checkpoints and content-free call diagnostics.

Run from the repository root using .venv/bin/python scripts/verify_real_model.py.
Uses the same SCRIPTWEAVER_* settings as the CLI. Never records credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from script_weaver.core.config import get_settings
from script_weaver.core.pipeline import PipelineEngine
from script_weaver.core.types import ProjectState, ProjectStatus
from script_weaver.exporters.fountain_exporter import export_fountain
from script_weaver.exporters.json_exporter import export_json
from script_weaver.exporters.video_gen_exporter import export_video_gen_prompts

STAGES = [
    "idea_refiner",
    "structurer",
    "character_designer",
    "scene_designer",
    "art_director",
    "scriptwriter",
    "storyboard_artist",
]


async def verify(output_dir: Path, idea: str | None, resume: bool = False, llm_client=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "checkpoint.json"
    completed = []
    saved = None
    if resume:
        data = json.loads(checkpoint.read_text(encoding="utf-8"))
        completed = data["completed_agents"]
        if not completed or completed != STAGES[: len(completed)]:
            raise ValueError("Checkpoint stages are not a valid pipeline prefix")
        saved = ProjectState.model_validate(data["state"])
        if saved.meta.status == ProjectStatus.COMPLETE:
            raise ValueError("This run already completed; use a new output directory")
        idea = saved.user_input
    elif checkpoint.exists():
        raise ValueError("Checkpoint exists; use --resume or a new output directory")
    if not idea:
        raise ValueError("Provide --idea for a new run")

    settings = get_settings()
    engine = PipelineEngine(
        llm_client=llm_client,
        auto_approve_gates=True,
        progress_callback=lambda stage, message: print(f"[{stage}] {message}", flush=True),
    )
    run_agent = engine._run_agent
    chat = engine._llm.chat
    current_agent = ""
    restored = False

    def save(state):
        data = {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "max_tokens": settings.llm_max_tokens,
            "completed_agents": completed,
            "state": state.model_dump(mode="json"),
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_dir, delete=False
        ) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        Path(f.name).replace(checkpoint)

    async def checkpoint_agent(name, state, user_message=""):
        nonlocal current_agent, restored
        current_agent = name
        if saved is not None and not restored:
            for field in ProjectState.model_fields:
                setattr(state, field, getattr(saved, field))
            restored = True
        if name in completed:
            print(f"[resume] Reusing {name}", flush=True)
            return {"status": "success"}
        result = await run_agent(name, state, user_message)
        completed.append(name)
        save(state)
        return result

    async def traced_chat(*args, **kwargs):
        started = time.monotonic()
        try:
            response = await chat(*args, **kwargs)
        except Exception as exc:
            # Record the type, not exception text that may contain request data.
            record = {
                "agent": current_agent,
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                "error_type": type(exc).__name__,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            with (output_dir / "calls.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            raise
        record = {
            "agent": current_agent,
            "stop_reason": response.stop_reason,
            "provider": settings.llm_provider,
            "model": response.model or settings.llm_model,
            "usage": response.usage.model_dump(),
            "text_characters": len(response.content or ""),
            "tools": [call.name for call in response.tool_calls],
        }
        with (output_dir / "calls.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(
            f"[llm] {current_agent}: {response.stop_reason}, output_tokens={response.usage.completion_tokens}",
            flush=True,
        )
        return response

    engine._run_agent = checkpoint_agent
    engine._llm.chat = traced_chat
    try:
        state = await engine.run_full_pipeline(
            idea, title=saved.meta.title if saved else "PR12真实模型验证"
        )
    finally:
        engine._llm.chat = chat
    export_json(state, output_dir / "project.json")
    export_fountain(state.script, output_dir / "script.fountain")
    exported = export_video_gen_prompts(state, output_dir / "video_gen")
    if "error" in exported:
        raise ValueError(exported["error"])
    save(state)
    print(f"PASS: {output_dir}", flush=True)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--idea")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    asyncio.run(verify(args.output_dir, args.idea, args.resume))
