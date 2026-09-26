"""Run the real pipeline with stage checkpoints and content-free call diagnostics.

Run from the repository root using .venv/bin/python scripts/verify_real_model.py.
Uses the same SCRIPTWEAVER_* settings as the CLI. Never records credentials.

Ticket #15 rewrite: no private-method monkeypatching. Progress rides the
public ``progress_callback``, checkpoints ride the public
``on_stage_complete``/``on_growth_event`` callbacks plus the shared resume
contract (``--resume`` validates prefix, fingerprint and input before any
model call), and call diagnostics wrap the LLM client through its public
constructor parameter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from script_weaver.core.config import get_settings
from script_weaver.core.pipeline import PipelineEngine
from script_weaver.core.resume import (
    ResumeRejected,
    assert_fingerprint_match,
    build_checkpoint,
    current_fingerprint,
    decode_checkpoint,
    validate_completed_steps,
    write_checkpoint_atomic,
)
from script_weaver.core.types import ProjectState
from script_weaver.exporters.fountain_exporter import export_fountain
from script_weaver.exporters.json_exporter import export_json
from script_weaver.exporters.video_gen_exporter import export_video_gen_prompts
from script_weaver.llm.client import LLMClient


class _TracedLLM:
    """Public-interface wrapper: attributes every call to the running agent.

    Records the stop reason/usage/error type (never request content or
    credentials) to ``calls.jsonl`` and stdout.
    """

    def __init__(self, inner, output_dir: Path, agent_tracker):
        self._inner = inner
        self._output_dir = output_dir
        self._agent_tracker = agent_tracker
        self.chat = self._traced_chat

    async def _traced_chat(self, *args, **kwargs):
        settings = get_settings()
        agent = self._agent_tracker["current"]
        started = time.monotonic()
        try:
            response = await self._inner.chat(*args, **kwargs)
        except Exception as exc:
            # Record the type, not exception text that may contain request data.
            record = {
                "agent": agent,
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                "error_type": type(exc).__name__,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            self._append(record)
            raise
        record = {
            "agent": agent,
            "stop_reason": response.stop_reason,
            "provider": settings.llm_provider,
            "model": response.model or settings.llm_model,
            "usage": response.usage.model_dump(),
            "text_characters": len(response.content or ""),
            "tools": [call.name for call in response.tool_calls],
        }
        self._append(record)
        print(
            f"[llm] {agent}: {response.stop_reason}, "
            f"output_tokens={response.usage.completion_tokens}",
            flush=True,
        )
        return response

    def _append(self, record: dict) -> None:
        with (self._output_dir / "calls.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def verify(output_dir: Path, idea: str | None, resume: bool = False, llm_client=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "checkpoint.json"
    checkpoint = None
    title = "PR12真实模型验证"

    if resume:
        if not checkpoint_path.exists():
            raise ValueError(f"未找到 {checkpoint_path}，没有可恢复的 checkpoint")
        checkpoint = decode_checkpoint(checkpoint_path.read_text(encoding="utf-8"))
        validate_completed_steps(
            checkpoint.completed_steps, state=checkpoint.state_model()
        )
        state = checkpoint.state_model()
        fingerprint = await current_fingerprint(
            skill_bindings=(
                {
                    k: v.model_dump(mode="json")
                    for k, v in (state.skill_bindings or {}).items()
                }
                if state
                else {}
            ),
            auto_approve=True,
        )
        assert_fingerprint_match(checkpoint.fingerprint, fingerprint)
        if checkpoint.content_complete:
            raise ValueError("This run already completed; use a new output directory")
        if idea is not None and idea != checkpoint.user_input:
            raise ValueError("传入的 idea 与 checkpoint 记录的原始输入不同")
        idea = checkpoint.user_input
        title = str(checkpoint.basis.get("title") or title)
        done = list(checkpoint.completed_steps)
        print(f"[resume] Reusing stages: {done}", flush=True)
    else:
        if checkpoint_path.exists():
            raise ValueError("Checkpoint exists; use --resume or a new output directory")
        if not idea:
            raise ValueError("Provide --idea for a new run")
        done = []

    # The execution-basis fingerprint: recorded on the initial checkpoint
    # BEFORE the first model call, and the comparison basis for --resume.
    # The pipeline re-validates it against its own resolved instance.
    fingerprint_value = (
        checkpoint.fingerprint if checkpoint and checkpoint.fingerprint
        else await current_fingerprint(skill_bindings={}, auto_approve=True)
    )
    fingerprint_cache = {"value": fingerprint_value}
    if not resume:
        checkpoint = build_checkpoint(
            user_input=idea, fingerprint=fingerprint_value, basis={"title": title}
        )
        write_checkpoint_atomic(checkpoint_path, checkpoint)

    agent_tracker = {"current": ""}

    def progress(stage: str, message: str) -> None:
        # Agent stages announce themselves through the public callback; the
        # tracker attributes each model call to the announcing agent.
        if stage and not message.startswith(("FAILED", "Completed")):
            agent_tracker["current"] = stage
        print(f"[{stage}] {message}", flush=True)

    traced = _TracedLLM(llm_client or LLMClient(), output_dir, agent_tracker)
    engine = PipelineEngine(
        llm_client=traced,
        auto_approve_gates=True,
        progress_callback=progress,
    )

    async def on_stage_complete(step: str, working: ProjectState) -> None:
        done.append(step)
        write_checkpoint_atomic(
            checkpoint_path,
            build_checkpoint(
                user_input=idea or "",
                completed_steps=done,
                state=working,
                fingerprint=fingerprint_cache["value"],
                growth_status="pending",
                basis={"title": title},
            ),
        )

    async def on_growth_event(status: str, error: str | None) -> None:
        raw = decode_checkpoint(checkpoint_path.read_text(encoding="utf-8"))
        write_checkpoint_atomic(
            checkpoint_path,
            raw.model_copy(
                update={"growth_status": status, "growth_error": error}, deep=True
            ),
        )

    # The fingerprint recorded on the FIRST run is the basis every --resume
    # compares against; compute it once here (a fresh registry discover —
    # the pipeline re-validates against its own resolved instance).
    # (Set above, before the initial checkpoint write.)

    state = await engine.run_full_pipeline(
        user_input=idea,
        title=title,
        initial_state=checkpoint.state_model(),
        completed_steps=list(checkpoint.completed_steps),
        resume_from=checkpoint,
        on_stage_complete=on_stage_complete,
        on_growth_event=on_growth_event,
    )
    export_json(state, output_dir / "project.json")
    export_fountain(state.script, output_dir / "script.fountain")
    exported = export_video_gen_prompts(state, output_dir / "video_gen")
    if "error" in exported:
        raise ValueError(exported["error"])
    final = decode_checkpoint(checkpoint_path.read_text(encoding="utf-8"))
    write_checkpoint_atomic(
        checkpoint_path,
        build_checkpoint(
            user_input=final.user_input,
            completed_steps=done,
            state=state,
            fingerprint=final.fingerprint,
            growth_status=final.growth_status,
            growth_error=final.growth_error,
            basis=final.basis,
        ),
    )
    print(f"PASS: {output_dir}", flush=True)
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--idea")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    async def _main():
        try:
            await verify(args.output_dir, args.idea, args.resume)
        except ResumeRejected as exc:
            raise SystemExit(f"无法恢复：{exc.message}")

    asyncio.run(_main())
