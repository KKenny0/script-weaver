"""CLI entry point for Script-Weaver.

Usage:
    script-weaver generate "你的故事想法" [--title "标题"] [--output-dir ./output]
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
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlencode

import click
import httpx

from script_weaver.core.config import get_settings
from script_weaver.core.pipeline import PipelineEngine
from script_weaver.exporters.fountain_exporter import export_fountain
from script_weaver.exporters.json_exporter import export_json
from script_weaver.exporters.video_gen_exporter import export_video_gen_prompts
from script_weaver.llm.client import LLMClient
from script_weaver.memory.profile import get_profile_manager
from script_weaver.skills.registry import SkillRegistry
from script_weaver.infrastructure.daemon_client import call as daemon_call


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


# ────────────────────────────────────────────────────────
# Main command: generate
# ────────────────────────────────────────────────────────


@click.group(invoke_without_command=True)
@click.option("--version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx: click.Context, version: bool) -> None:
    """Script-Weaver: Agent-native screenplay & storyboard generation system."""
    from script_weaver.runtime import require_supported_python
    require_supported_python()
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
def generate(
    idea: str | None,
    title: str,
    output_dir: str,
    auto_approve: bool,
    output_formats: tuple[str, ...],
) -> None:
    """Generate a complete screenplay and storyboard from an idea.

    IDEA: Your story idea (text or prompt to a file path).
    If not provided, will read interactively.
    """
    if idea is None:
        idea = click.prompt("Enter your story idea")

    # Check if it's a file path
    idea_path = Path(idea)
    if idea_path.exists():
        idea = idea_path.read_text(encoding="utf-8").strip()

    if not idea.strip():
        click.echo("Error: No idea provided.", err=True)
        sys.exit(1)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"\n{'='*60}")
    click.echo(f"  Script-Weaver: Generating from idea...")
    click.echo(f"  Idea: {idea[:80]}{'...' if len(idea) > 80 else ''}")
    click.echo(f"  Output: {out_dir.resolve()}")
    click.echo(f"{'='*60}\n")

    async def _run():
        engine = _create_engine(auto_approve=auto_approve)
        state = await engine.run_full_pipeline(user_input=idea, title=title or None)

        # Export results
        click.echo(f"\n--- Exporting Results ---")
        results = {"project_state": str(out_dir / "project.json")}

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
                click.echo(f"    - JSON ({len(state.storyboard.shots)} shots)")
                click.echo(f"    - CSV")
                click.echo(f"    - Per-shot text files")
            else:
                click.echo(f"  ✗ VideoGen: {vg_results['error']}")

        # Summary
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

    asyncio.run(_run())


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
    click.echo(f"  Pace: {p.style_preferences.narrative_pace.value}")
    click.echo(f"  Dialogue density: {p.style_preferences.dialogue_density.value}")

    click.echo(f"\nDialogue Style:")
    ds = p.dialogue_style
    click.echo(f"  Formality: {ds.formality_level.value}")
    click.echo(f"  Subtext: {ds.subtext_preference.value}")
    click.echo(f"  Monologue: {ds.monologue_frequency.value}")

    click.echo(f"\nVisual Preferences:")
    vp = p.visual_preferences
    click.echo(f"  Movement: {vp.movement_preference.value}")
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


# ────────────────────────────────────────────────────────
# Persistent workbench collaboration commands
# ────────────────────────────────────────────────────────


def _seg(value: str) -> str:
    return quote(value, safe="")


def _json(value) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _call(method: str, path: str, body: dict | None = None):
    try:
        return daemon_call(method, path, body)
    except (FileNotFoundError, httpx.ConnectError, httpx.TimeoutException) as error:
        click.echo(json.dumps({"error": f"daemon unavailable: {error}"}), err=True)
        raise click.exceptions.Exit(3) from error
    except httpx.HTTPStatusError as error:
        try:
            payload = error.response.json()
            detail = payload.get("detail", str(error)) if isinstance(payload, dict) else str(error)
        except (ValueError, json.JSONDecodeError):
            detail = error.response.text.strip() or str(error)
        code = 4 if error.response.status_code == 409 else 2 if error.response.status_code in {400, 404, 422} else 5
        click.echo(json.dumps({"error": detail}, ensure_ascii=False), err=True)
        raise click.exceptions.Exit(code) from error


def _depth(value, current=0):
    if current > 32:
        raise click.UsageError("proposal JSON exceeds maximum depth 32")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, current + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, current + 1)


@main.group()
def workbench() -> None:
    """Inspect the local persistent workbench."""


@workbench.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON document.")
def workbench_doctor(as_json: bool) -> None:
    """Check daemon, schema, and pinned product skills."""
    result = _call("GET", "/workbench/doctor")
    if as_json:
        _json(result)
    else:
        missing = [skill["name"] for skill in result["skills"] if not skill["available"]]
        click.echo(f"daemon: ok · schema: {max(result['schema_versions'])}")
        click.echo("skills: " + ("ok" if not missing else "missing " + ", ".join(missing)))
        h3 = result.get("h3", {})
        if not h3.get("configured"):
            click.echo("h3: optional · not configured")
        else:
            identity = h3.get("identity", "unavailable")
            readiness = "ok" if h3.get("ready") else "failed"
            click.echo(
                f"h3: {readiness} · {identity} · requested {h3.get('requested_task')} · "
                f"requested {h3.get('requested_short_edge')} short-edge"
            )


@main.group()
def task() -> None:
    """Claim and inspect durable workbench tasks."""


@task.command("list")
@click.option("--project-id")
@click.option("--status")
def task_list(project_id: str | None, status: str | None) -> None:
    query = {key: value for key, value in {"project_id": project_id, "status": status}.items() if value}
    _json(_call("GET", f"/tasks?{urlencode(query)}"))


@task.command("claim")
@click.option("--task-id", required=True)
@click.option("--worker-label", required=True)
def task_claim(task_id: str, worker_label: str) -> None:
    _json(_call("POST", f"/tasks/{_seg(task_id)}/claim", {"worker_label": worker_label}))


@task.command("context")
@click.argument("task_id")
@click.option("--section", type=click.Choice(["all", "source", "target"]), default="all")
@click.option("--cursor", type=click.IntRange(min=0), default=0)
def task_context(task_id: str, section: str, cursor: int) -> None:
    query = urlencode({"section": section, "cursor": cursor})
    _json(_call("GET", f"/tasks/{_seg(task_id)}/context?{query}"))


@task.command("fail")
@click.option("--task-id", required=True)
@click.option("--run-id", required=True)
@click.option("--message", required=True)
def task_fail(task_id: str, run_id: str, message: str) -> None:
    _json(_call("POST", f"/tasks/{_seg(task_id)}/fail", {"run_id": run_id, "message": message}))


@main.group()
def changeset() -> None:
    """Submit typed proposals; applying remains creator-only in the UI."""


@changeset.command("submit")
@click.argument("task_id")
@click.option("--run-id", required=True)
@click.option("--file", "proposal_file", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
def changeset_submit(task_id: str, run_id: str, proposal_file: Path) -> None:
    if proposal_file.stat().st_size > 2 * 1024 * 1024:
        raise click.UsageError("proposal file exceeds 2 MiB")
    try:
        proposal = json.loads(proposal_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise click.UsageError(f"invalid proposal file: {error}") from error
    if not isinstance(proposal, dict):
        raise click.UsageError("proposal must be a JSON object")
    _depth(proposal)
    proposal["run_id"] = run_id
    _json(_call("POST", f"/tasks/{_seg(task_id)}/changesets/submit", proposal))


@main.command("search")
@click.argument("project_id")
@click.option("--query", required=True)
@click.option("--limit", type=click.IntRange(1, 100), default=50)
def search_command(project_id: str, query: str, limit: int) -> None:
    _json(_call("GET", f"/projects/{_seg(project_id)}/search?{urlencode({'q': query, 'limit': limit})}"))


@main.command("export")
@click.argument("project_id")
@click.option("--output", required=True, type=click.Path(file_okay=False, path_type=Path))
def export_command(project_id: str, output: Path) -> None:
    project = _call("GET", f"/projects/{_seg(project_id)}")
    _write_export(project, output)
    _json({"project_id": project_id, "output": str(output), "documents": len(project.get("documents", []))})


def _write_export(project: dict, output: Path) -> None:
    """Build a complete sibling directory, then publish it with one rename."""
    if output.exists():
        raise click.UsageError("export output must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        (temporary / "project.json").write_text(
            json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        documents = temporary / "documents"
        documents.mkdir()
        for index, document in enumerate(project.get("documents", []), 1):
            version = next(
                (item for item in document["versions"] if item["status"] == "ACCEPTED"),
                None,
            )
            content = version["content"] if version else (document.get("draft") or {}).get("content", "")
            (documents / f"{index:02d}-{document['kind']}.md").write_text(
                content, encoding="utf-8"
            )
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


import os


if __name__ == "__main__":
    main()
