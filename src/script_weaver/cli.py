"""CLI entry point for Script-Weaver.

Usage:
    script-weaver generate "你的故事想法" [--title "标题"] [--output-dir ./output]
    script-weaver generate --resume --output-dir ./output
    script-weaver skills list [--stage STAGE]
    script-weaver skills install <path_or_url>
    script-weaver skills activate <skill_id> --stage STAGE
    script-weaver skills deactivate <skill_id> --stage STAGE
    script-weaver profile show
    script-weaver profile edit
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click

from script_weaver.core.config import get_settings
from script_weaver.core.pipeline import (
    GENERATION_STEP_LABELS,
    GENERATION_STEPS,
    PipelineEngine,
)
from script_weaver.core.project_store import DataDirLock, DataDirLockError
from script_weaver.core.resume import (
    ResumeCheckpoint,
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
from script_weaver.memory.profile import get_profile_manager
from script_weaver.skills.registry import SkillRegistry


# ────────────────────────────────────────────────────────
# Shared setup
# ────────────────────────────────────────────────────────


def _create_engine(auto_approve: bool = False) -> PipelineEngine:
    """Create a pipeline engine with standard configuration."""
    llm = LLMClient()
    registry = SkillRegistry()
    return PipelineEngine(
        llm_client=llm,
        skill_registry=registry,
        auto_approve_gates=auto_approve,
        progress_callback=_cli_progress,
    )


def _cli_progress(stage: str, message: str) -> None:
    """Progress callback for CLI output."""
    click.echo(f"  [{stage}] {message}")


def _state_skill_bindings(state: ProjectState) -> dict:
    return {
        key: binding.model_dump(mode="json")
        for key, binding in (state.skill_bindings or {}).items()
    }


def _resolve_idea_text(idea: str) -> str:
    """Read the idea from a file path when one is given."""
    idea_path = Path(idea)
    if idea_path.exists():
        return idea_path.read_text(encoding="utf-8").strip()
    return idea


def _export_results(state: ProjectState, out_dir: Path, output_formats: tuple[str, ...]) -> None:
    """Export a completed state in the requested formats (format is not part
    of the generation basis — it may differ between the original run and a
    resume/re-export)."""
    click.echo("\n--- Exporting Results ---")
    if "json" in output_formats:
        json_path = out_dir / "project.json"
        export_json(state, json_path)
        click.echo(f"  ✓ JSON: {json_path}")
    if "fountain" in output_formats and state.script:
        fountain_path = out_dir / "script.fountain"
        export_fountain(state.script, fountain_path)
        click.echo(f"  ✓ Fountain: {fountain_path}")
    if "video_gen" in output_formats:
        vg_out = out_dir / "video_gen"
        vg_results = export_video_gen_prompts(state, vg_out)
        if "error" not in vg_results:
            click.echo(f"  ✓ VideoGen prompts: {vg_out}/")
        else:
            click.echo(f"  ✗ VideoGen: {vg_results['error']}")


def _print_summary(state: ProjectState) -> None:
    click.echo(f"\n{'='*60}")
    click.echo(f"  Generation Complete!")
    click.echo(f"  Title: {state.meta.title}")
    click.echo(f"  Status: {state.meta.status.value}")
    if state.outline:
        click.echo(f"  Outline: {len(state.outline.plot_outline)} beats")
    if state.characters:
        click.echo(f"  Characters: {len(state.characters)}")
    if state.script:
        click.echo(f"  Script: {len(state.script.scenes)} scenes")
    if state.storyboard:
        click.echo(
            f"  Storyboard: {state.storyboard.total_shot_count} shots, "
            f"~{state.storyboard.total_estimated_duration:.0f}s"
        )
    click.echo(f"{'='*60}\n")


def _refuse_resume(exc: ResumeRejected) -> None:
    """Report a resume refusal and exit non-zero; the file stays untouched."""
    click.echo(f"无法恢复：{exc.message}", err=True)
    click.echo("已保留原 checkpoint.json，未做任何修改。", err=True)
    sys.exit(1)


# ────────────────────────────────────────────────────────
# Main command: generate
# ────────────────────────────────────────────────────────


@click.group(invoke_without_command=True)
@click.option("--version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx: click.Context, version: bool) -> None:
    """Script-Weaver: Agent-native screenplay & storyboard generation system."""
    if version:
        from script_weaver import __version__
        click.echo(f"script-weaver v{__version__}")
        ctx.exit()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.argument("idea", required=False)
@click.option("--title", "-t", default="", help="Project title.")
@click.option(
    "--output-dir", "-o",
    default="./output",
    type=click.Path(),
    help="Output directory for generated files.",
)
@click.option(
    "--auto-approve/--no-auto-approve",
    default=True,
    help="Auto-approve human gates (non-interactive mode).",
)
@click.option(
    "--format",
    "output_formats",
    multiple=True,
    default=["json"],
    type=click.Choice(["json", "fountain", "video_gen"]),
    help="Output formats (can specify multiple).",
)
@click.option(
    "--resume", "resume_", is_flag=True, default=False,
    help="Resume from checkpoint.json in the output directory.",
)
def generate(
    idea: str | None,
    title: str,
    output_dir: str,
    auto_approve: bool,
    output_formats: tuple[str, ...],
    resume_: bool,
) -> None:
    """Generate a complete screenplay and storyboard from an idea.

    IDEA: Your story idea (text or prompt to a file path).
    If not provided, will read interactively — unless --resume continues
    from an existing checkpoint, which reuses the recorded input.

    With --resume the last complete success prefix is reused and only the
    remaining stages call the model. A finished checkpoint re-exports the
    requested formats without contacting any model or replaying growth.
    """
    if resume_ and click.get_current_context().get_parameter_source("auto_approve") == click.core.ParameterSource.DEFAULT:
        auto_approve = None
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_dir / "checkpoint.json"

    # One CLI process per output directory: the OS lock survives crashes.
    lock = DataDirLock(out_dir, lock_name="cli.lock")
    try:
        lock.acquire()
    except DataDirLockError as exc:
        click.echo(f"错误：{exc}", err=True)
        sys.exit(1)

    try:
        _generate_locked(
            idea, title, out_dir, checkpoint_path, auto_approve,
            output_formats, resume_,
        )
    finally:
        lock.release()


def _generate_locked(
    idea: str | None,
    title: str,
    out_dir: Path,
    checkpoint_path: Path,
    auto_approve: bool,
    output_formats: tuple[str, ...],
    resume_: bool,
) -> None:
    # ── Resume branch: decided BEFORE any interactive prompt ──
    if resume_:
        checkpoint = _load_resume_checkpoint(
            checkpoint_path, idea=idea, title=title, auto_approve=auto_approve
        )
        if checkpoint.content_complete:
            # Nothing to generate: only re-export, no model client, no
            # growth replay. Growth that was mid-flight at the kill is
            # converged to interrupted (already persisted by the loader).
            state = checkpoint.state_model()
            assert state is not None
            click.echo("Checkpoint 已完成：按请求格式重新导出，不调用模型、不重放成长任务。")
            _export_results(state, out_dir, output_formats)
            _print_summary(state)
            return
        auto_approve = checkpoint.fingerprint["auto_approve"]
        idea = checkpoint.user_input
        recorded_title = str(checkpoint.basis.get("title") or "")
        # The recorded title is the generation basis; a --title that only
        # fills in a previously-empty title is a cosmetic default.
        run_title = (title or recorded_title) or None
    else:
        if checkpoint_path.exists():
            state = "已完成" if _checkpoint_complete(checkpoint_path) else "未完成"
            click.echo(
                f"错误：输出目录已有{state}的 checkpoint.json，"
                "不会静默覆盖。使用 --resume 继续（或仅重新导出），"
                "或更换 --output-dir。",
                err=True,
            )
            sys.exit(1)
        if idea is None:
            idea = click.prompt("Enter your story idea")
        idea = _resolve_idea_text(idea)
        if not idea.strip():
            click.echo("Error: No idea provided.", err=True)
            sys.exit(1)
        checkpoint = None
        run_title = title or None

    click.echo(f"\n{'='*60}")
    click.echo(f"  Script-Weaver: Generating from idea...")
    if checkpoint is not None:
        done = checkpoint.completed_steps
        next_step = next(
            (s for s in GENERATION_STEPS if s not in done), None
        )
        click.echo(
            "  恢复模式：已复用阶段 "
            + ("、".join(GENERATION_STEP_LABELS.get(s, s) for s in done) or "无")
        )
        click.echo(
            "  将从「"
            + (GENERATION_STEP_LABELS.get(next_step, next_step) if next_step else "收尾")
            + "」继续"
        )
    click.echo(f"  Idea: {idea[:80]}{'...' if len(idea) > 80 else ''}")
    click.echo(f"  Output: {out_dir.resolve()}")
    click.echo(f"{'='*60}\n")

    async def _run() -> None:
        engine = _create_engine(auto_approve=auto_approve)
        holder = _CheckpointHolder(checkpoint_path, checkpoint)

        if checkpoint is None:
            # Initial checkpoint BEFORE the first model call: what runs, on
            # what basis — so a hard kill leaves a resumable record.
            fingerprint = await current_fingerprint(
                skill_bindings={}, auto_approve=auto_approve
            )
            holder.reset(
                build_checkpoint(
                    user_input=idea or "",
                    fingerprint=fingerprint,
                    basis={"title": title, "auto_approve": auto_approve},
                )
            )
            holder.save()

        state = await engine.run_full_pipeline(
            user_input=idea,
            title=run_title,
            initial_state=holder.state_model(),
            completed_steps=holder.checkpoint.completed_steps,
            resume_from=holder.checkpoint,
            on_stage_complete=holder.on_stage_complete,
            on_growth_event=holder.on_growth_event,
        )
        _export_results(state, out_dir, output_formats)
        _print_summary(state)

    asyncio.run(_run())


class _CheckpointHolder:
    """The in-memory checkpoint; every stage/growth transition is persisted
    atomically (same-dir temp file + fsync + os.replace)."""

    def __init__(self, path: Path, checkpoint: ResumeCheckpoint | None):
        self.path = path
        self.checkpoint = checkpoint

    def reset(self, checkpoint: ResumeCheckpoint) -> None:
        self.checkpoint = checkpoint

    def save(self) -> None:
        if self.checkpoint is not None:
            write_checkpoint_atomic(self.path, self.checkpoint)

    def state_model(self) -> ProjectState | None:
        return self.checkpoint.state_model() if self.checkpoint else None

    async def on_stage_complete(self, step: str, working: ProjectState) -> None:
        assert self.checkpoint is not None
        completed = [*self.checkpoint.completed_steps, step]
        self.checkpoint = build_checkpoint(
            user_input=self.checkpoint.user_input,
            completed_steps=completed,
            state=working,
            fingerprint=self.checkpoint.fingerprint,
            growth_status=self.checkpoint.growth_status,
            growth_error=self.checkpoint.growth_error,
            basis=self.checkpoint.basis,
        )
        self.save()

    async def on_growth_event(self, status: str, error: str | None) -> None:
        assert self.checkpoint is not None
        self.checkpoint = self.checkpoint.model_copy(
            update={"growth_status": status, "growth_error": error}, deep=True
        )
        self.save()


def _checkpoint_complete(path: Path) -> bool:
    try:
        return decode_checkpoint(path.read_text(encoding="utf-8")).content_complete
    except ResumeRejected:
        return False


def _load_resume_checkpoint(
    path: Path, *, idea: str | None, title: str, auto_approve: bool | None
) -> ResumeCheckpoint:
    """Load + fully validate a resume checkpoint; refuse before any model.

    Shared contract: the same ResumeRejected codes/messages the web resume
    endpoint surfaces, and a refusal never touches the stored file.
    """
    if not path.exists():
        click.echo(
            f"错误：未找到 {path}；没有可恢复的 checkpoint。", err=True
        )
        sys.exit(1)
    try:
        checkpoint = decode_checkpoint(path.read_text(encoding="utf-8"))
        validate_completed_steps(
            checkpoint.completed_steps, state=checkpoint.state_model()
        )
    except ResumeRejected as exc:
        _refuse_resume(exc)
        raise  # unreachable: _refuse_resume exits

    # Growth that was mid-flight at a hard kill: converge the record and
    # say the side effects may have partially happened (never replayed).
    if checkpoint.growth_status == "running":
        checkpoint = checkpoint.model_copy(
            update={
                "growth_status": "interrupted",
                "growth_error": "进程中断时成长任务仍在执行，"
                "其副作用可能已部分发生；不会自动重放。",
            },
            deep=True,
        )
        write_checkpoint_atomic(path, checkpoint)
        click.echo(
            "提示：上次运行在成长任务执行中被中断，已记录为 interrupted；"
            "其副作用（画像统计 / Skill 草稿）可能已部分发生，不会自动重放。",
            err=True,
        )

    # An explicitly different idea is a different run: say what differs.
    if idea is not None:
        idea_text = _resolve_idea_text(idea)
        if idea_text.strip() and idea_text != checkpoint.user_input:
            click.echo(
                "无法恢复：显式传入的 idea 与 checkpoint 记录的原始输入不同"
                "（这不是同一次生成）。如需全新生成请更换 --output-dir。",
                err=True,
            )
            sys.exit(1)
    # Execution-affecting parameters must match the recorded basis.
    recorded_title = str(checkpoint.basis.get("title") or "")
    if title and recorded_title and title != recorded_title:
        click.echo(
            f"无法恢复：--title（{title}）与 checkpoint 记录（{recorded_title}）"
            "不同；标题属于生成依据，恢复时不允许改变。",
            err=True,
        )
        sys.exit(1)
    if checkpoint.content_complete:
        return checkpoint
    recorded_auto = checkpoint.fingerprint.get("auto_approve")
    if auto_approve is None:
        auto_approve = recorded_auto
    if recorded_auto is not None and bool(recorded_auto) != auto_approve:
        click.echo(
            f"无法恢复：--auto-approve（{auto_approve}）与 checkpoint 记录"
            f"（{bool(recorded_auto)}）不同；gate 配置属于生成依据。",
            err=True,
        )
        sys.exit(1)

    # Fingerprint: current settings + actually-loaded skill contents must
    # still match the recorded basis — checked before any model call.
    async def _check_fingerprint() -> None:
        state = checkpoint.state_model()
        fingerprint = await current_fingerprint(
            skill_bindings=_state_skill_bindings(state) if state else {},
            auto_approve=auto_approve,
        )
        assert_fingerprint_match(checkpoint.fingerprint, fingerprint)

    try:
        asyncio.run(_check_fingerprint())
    except ResumeRejected as exc:
        _refuse_resume(exc)
        raise  # unreachable: _refuse_resume exits
    return checkpoint


# ────────────────────────────────────────────────────────
# Skills commands
# ────────────────────────────────────────────────────────


@main.group()
def skills() -> None:
    """Manage skills (list, install, activate, deactivate)."""
    pass


@skills.command("list")
@click.option("--stage", "-s", default=None, help="Filter by pipeline stage.")
def skills_list(stage: str | None) -> None:
    """List all available skills."""

    async def _run():
        registry = SkillRegistry()
        await registry.discover()

        available = registry.list_available(stage)
        if not available:
            click.echo("No skills found.")
            return

        click.echo(f"\nAvailable Skills ({len(available)}):\n")
        click.echo(f"{'ID':<25} {'Name':<20} {'Stage':<15} {'Format'}")
        click.echo("-" * 75)
        for s in available:
            stage_val = s.get("stage") or "-"
            click.echo(
                f"{s['id']:<25} {s['name']:<20} "
                f"{stage_val:<15} {s['format']}"
            )
        click.echo()

    asyncio.run(_run())


@skills.command("install")
@click.argument("source", required=True)
def skills_install(source: str) -> None:
    """Install a new skill from file path or URL."""

    async def _run():
        registry = SkillRegistry()
        await registry.discover()

        try:
            skill = await registry.install(source)
            click.echo(f"✓ Installed skill: {skill.name} (id: {skill.id})")
            click.echo(f"  Stage: {skill.stage.value if skill.stage else 'N/A'}")
            click.echo(f"  Format: {skill.source_format.value}")
        except Exception as e:
            click.echo(f"✗ Failed to install: {e}", err=True)
            sys.exit(1)

    asyncio.run(_run())


@skills.command("activate")
@click.argument("skill_id", required=True)
@click.option("--stage", "-s", required=True, help="Pipeline stage for this skill.")
@click.option("--priority", "-p", default=0, type=int, help="Priority (higher = applied first).")
def skills_activate(skill_id: str, stage: str, priority: int) -> None:
    """Activate a skill for a project stage."""
    # For CLI, we operate on a global config (future: per-project)
    click.echo(f"Activating '{skill_id}' for stage '{stage}' (priority={priority})")
    click.echo("(Note: In CLI mode, skills are activated for the current session.)")
    # Actual activation happens in the pipeline via bindings


@skills.command("deactivate")
@click.argument("skill_id", required=True)
@click.option("--stage", "-s", required=True, help="Pipeline stage.")
def skills_deactivate(skill_id: str, stage: str) -> None:
    """Deactivate a skill for a project stage."""
    click.echo(f"Deactivating '{skill_id}' for stage '{stage}'")


# ────────────────────────────────────────────────────────
# Profile commands
# ────────────────────────────────────────────────────────


@main.group()
def profile() -> None:
    """Manage user profile (learned preferences)."""
    pass


@profile.command("show")
def profile_show() -> None:
    """Show current user profile."""
    pm = get_profile_manager()
    p = pm.profile

    click.echo(f"\n{'='*50}")
    click.echo(f"  User Profile: {p.user_id}")
    click.echo(f"  Created: {p.created_at[:10]}")
    click.echo(f"  Updated: {p.updated_at[:10]}")
    click.echo(f"{'='*50}")

    click.echo(f"\nStyle Preferences:")
    click.echo(f"  Genres: {', '.join(p.style_preferences.preferred_genres) or '(none)'}")
    click.echo(f"  Tones: {', '.join(p.style_preferences.preferred_tones) or '(none)'}")
    click.echo(f"  Pace: {p.style_preferences.narrative_pace}")
    click.echo(f"  Dialogue density: {p.style_preferences.dialogue_density}")

    click.echo(f"\nDialogue Style:")
    ds = p.dialogue_style
    click.echo(f"  Formality: {ds.formality_level}")
    click.echo(f"  Subtext: {ds.subtext_preference}")
    click.echo(f"  Monologue: {ds.monologue_frequency}")

    click.echo(f"\nVisual Preferences:")
    vp = p.visual_preferences
    click.echo(f"  Movement: {vp.movement_preference}")
    click.echo(f"  Aspect ratios: {', '.join(vp.preferred_aspect_ratios)}")

    click.echo(f"\nPreferred Structures:")
    if p.preferred_structures:
        for sp in p.preferred_structures:
            click.echo(
                f"  - {sp.display_name}: used {sp.usage_count}x, "
                f"success {sp.success_rate:.0%}"
            )
    else:
        click.echo("  (none yet — structures will be learned as you use them)")

    click.echo(f"\nStats:")
    click.echo(f"  Projects: {p.stats.total_projects}")
    click.echo(f"  Completed: {p.stats.completed_projects}")
    click.echo(f"  Decisions recorded: {p.stats.total_decisions_recorded}")
    click.echo()


@profile.command("edit")
def profile_edit() -> None:
    """Open and edit user profile (opens in $EDITOR)."""
    pm = get_profile_manager()
    editor_path = pm._path
    click.echo(f"Opening profile: {editor_path}")

    import subprocess
    editor = os.environ.get("EDITOR", "nano")
    subprocess.call([editor, str(editor_path)])

    # Reload after editing
    pm._profile = None  # Force reload
    p = pm.profile
    click.echo(f"Profile updated. Last modified: {p.updated_at}")


if __name__ == "__main__":
    main()
