"""Refinement validation shared by the pipeline and the web API (issue #22).

Pure functions only: routing-shape validation, the substantive-diff
computation between two project snapshots, the deterministic
``shorten_last_dialogue`` constraint check, and the exceptions the pipeline
raises when a refine request must not be saved. Nothing here performs I/O
or mutates a state — the pipeline runs its agents on a copy and returns
that copy only after every check passes, and the API reuses the same diff
computation to build the change summary it reports, so the interface can
never claim a modification the saved content does not contain.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from pydantic import BaseModel

from script_weaver.core.types import ProjectState, ScriptBlockType
from script_weaver.prompts.system_prompts import (
    GENERAL_MODIFICATION_PROMPT,
    SHORTEN_DIALOGUE_PROMPT,
)

# Creative artifacts a refine request may legitimately change. meta, memory
# and skill bindings are project machinery and never evidence of a change.
CREATIVE_ARTIFACTS = (
    "refined_idea", "outline", "characters", "scenes",
    "art_style", "script", "storyboard", "visual_highlights",
)

# The six agents a refine request may execute, with the artifact each owns.
REFINABLE_AGENTS = {
    "scriptwriter": "script",
    "storyboard_artist": "storyboard",
    "structurer": "outline",
    "character_designer": "characters",
    "scene_designer": "scenes",
    "art_director": "art_style",
}

ROUTING_ACTIONS = {"execute_agent", "ask_user", "pause", "complete"}
ROUTING_CONSTRAINTS = {"general", "shorten_last_dialogue"}

_ARTIFACT_LABELS = {
    "refined_idea": "精炼概念 (refined_idea)",
    "outline": "大纲 (outline)",
    "characters": "角色设计 (characters)",
    "scenes": "场景设计 (scenes)",
    "art_style": "美术风格 (art_style)",
    "script": "剧本 (script)",
    "storyboard": "分镜 (storyboard)",
    "visual_highlights": "视觉亮点 (visual_highlights)",
}


# ── Validation exceptions ─────────────────────────────────


class RefinementError(Exception):
    """A refine request was correctly refused. Maps to HTTP 422.

    The ``code`` is the stable detail.code the API reports; the message is
    user-readable Chinese and must never leak provider details.
    """

    code = "refine_not_executable"


class RefineNotExecutable(RefinementError):
    """Routing decided not to (or cannot) execute a modifiable agent."""

    code = "refine_not_executable"


class RefineTargetNotFound(RefinementError):
    """The constrained target (e.g. the last dialogue) does not exist."""

    code = "refine_target_not_found"


class RefineNoMeaningfulChange(RefinementError):
    """The agent ran but produced no substantive change."""

    code = "refine_no_meaningful_change"


class RefineConstraintFailed(RefinementError):
    """The result violated the active constraint and was discarded whole."""

    code = "refine_constraint_failed"


class RefineExecutionError(Exception):
    """The model call or its protocol failed. Maps to a sanitized non-2xx.

    Distinct from :class:`RefinementError` on purpose: a transport/model
    failure must not masquerade as a 422 no-op or a success.
    """

    code = "refine_model_failed"


# ── Data shapes ──────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class FieldChange:
    path: str
    before: Any
    after: Any


@dataclasses.dataclass(frozen=True)
class RoutingDecision:
    action: str
    next_agent: str
    constraint: str
    reason: str = ""
    message_to_user: str = ""


@dataclasses.dataclass(frozen=True)
class DialogueTarget:
    """The last non-empty dialogue, located in the pre-modification snapshot."""

    scene_index: int
    block_index: int
    scene_id: str
    scene_number: str | int
    original: str


# ── Routing validation ───────────────────────────────────


def parse_routing(result: Any) -> RoutingDecision:
    """Strictly validate an orchestrator result into a routing decision.

    Raises :class:`RefineExecutionError` when the model call itself failed
    and :class:`RefineNotExecutable` for every shape that must not be saved
    as a modification: unparseable decisions, ask_user/pause/complete,
    unknown actions, and targets outside the six modifiable agents.
    """
    if isinstance(result, dict) and result.get("error"):
        raise RefineExecutionError("编排器调用失败，修改未应用")
    if not isinstance(result, dict) or result.get("status") != "success":
        raise RefineNotExecutable("路由结果无法解析为决策 JSON，修改未应用")
    data = result.get("data")
    if not isinstance(data, dict):
        raise RefineNotExecutable("路由决策不是有效的 JSON 对象，修改未应用")

    action = data.get("action")
    if not isinstance(action, str) or action not in ROUTING_ACTIONS:
        raise RefineNotExecutable(f"路由决策包含未知 action: {action!r}，修改未应用")
    if action != "execute_agent":
        note = str(data.get("message_to_user") or data.get("reason") or action)
        raise RefineNotExecutable(f"编排器未执行修改（{action}）: {note}")

    next_agent = data.get("next_agent")
    if not isinstance(next_agent, str) or next_agent not in REFINABLE_AGENTS:
        raise RefineNotExecutable(f"路由目标不可用于修改: {next_agent!r}")

    constraint = data.get("constraint", "general")
    if not isinstance(constraint, str) or constraint not in ROUTING_CONSTRAINTS:
        raise RefineNotExecutable(f"路由决策包含未知 constraint: {constraint!r}")
    if constraint == "shorten_last_dialogue" and next_agent != "scriptwriter":
        raise RefineNotExecutable(
            "shorten_last_dialogue 约束必须路由到 scriptwriter，修改未应用",
        )
    return RoutingDecision(
        action=action,
        next_agent=next_agent,
        constraint=constraint,
        reason=str(data.get("reason") or ""),
        message_to_user=str(data.get("message_to_user") or ""),
    )


def locate_last_dialogue(state: ProjectState) -> DialogueTarget:
    """Find the last non-empty dialogue block in script order.

    Position and original text always come from this pre-modification
    snapshot; without a script or any dialogue the target does not exist
    and the execution agent must not be called.
    """
    script = state.script
    if script is None:
        raise RefineTargetNotFound("项目尚无剧本，无法定位最后一句对白")
    last: tuple[int, int, str, str | int, str] | None = None
    for scene_index, scene in enumerate(script.scenes):
        for block_index, block in enumerate(scene.blocks):
            if block.block_type != ScriptBlockType.DIALOGUE:
                continue
            text = block.content.get("dialogue")
            if isinstance(text, str) and text.strip():
                last = (
                    scene_index, block_index, scene.scene_id,
                    scene.heading.scene_number, text.strip(),
                )
    if last is None:
        raise RefineTargetNotFound("剧本中不存在非空对白，无法定位最后一句对白")
    return DialogueTarget(
        scene_index=last[0],
        block_index=last[1],
        scene_id=last[2],
        scene_number=last[3],
        original=last[4],
    )


# ── Diff computation ─────────────────────────────────────

# Leaf values that regenerate automatically or merely re-point references
# are not evidence of a substantive edit. The paths are surgical — known
# model fields only — so a "notes"-alike key anywhere else in an artifact
# still counts as a real change and cannot smuggle a no-op past validation.
_EVIDENCE_EXCLUDED_PATHS: tuple[tuple[Any, ...], ...] = (
    ("script", "notes"),
    ("storyboard", "notes"),
    ("script", "scenes", "*", "scene_id"),
    ("script", "scenes", "*", "scene_design_id"),
    ("storyboard", "shots", "*", "shot_id"),
    ("storyboard", "shots", "*", "scene_id"),
    ("visual_highlights", "*", "id"),
    ("visual_highlights", "*", "related_shot_ids"),
)

_MISSING = object()


def _normalize(value: Any) -> Any:
    """Strings compare after stripping surrounding whitespace, nothing else."""
    return value.strip() if isinstance(value, str) else value


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_dump(item) for item in value]
    return value


def _path_skipped(path: tuple[Any, ...], skips: tuple[tuple[Any, ...], ...]) -> bool:
    return any(
        len(pattern) == len(path)
        and all(p == "*" or p == s for p, s in zip(pattern, path))
        for pattern in skips
    )


def _format_path(segments: tuple[Any, ...]) -> str:
    out = ""
    for segment in segments:
        if isinstance(segment, int):
            out += f"[{segment}]"
        elif out:
            out += f".{segment}"
        else:
            out = str(segment)
    return out


def _collect(
    path: tuple[Any, ...],
    before: Any,
    after: Any,
    out: list[FieldChange],
    skips: tuple[tuple[Any, ...], ...],
    normalize: bool = True,
) -> None:
    if _path_skipped(path, skips):
        return
    b = _normalize(before) if normalize else before
    a = _normalize(after) if normalize else after
    if isinstance(b, dict) and isinstance(a, dict):
        for key in sorted(b.keys() | a.keys(), key=str):
            _collect(path + (key,), b.get(key, _MISSING), a.get(key, _MISSING),
                     out, skips, normalize)
        return
    if isinstance(b, list) and isinstance(a, list):
        for index in range(max(len(b), len(a))):
            bv = b[index] if index < len(b) else _MISSING
            av = a[index] if index < len(a) else _MISSING
            _collect(path + (index,), bv, av, out, skips, normalize)
        return
    if type(b) is not type(a) or b != a:
        out.append(FieldChange(path=_format_path(path), before=b, after=a))


def diff_field_changes(
    before: ProjectState, after: ProjectState, *, strict: bool = False
) -> list[FieldChange]:
    """Field-level diff over the creative artifacts of two snapshots.

    ``strict=True`` compares raw field values with no exclusions and no
    string normalization — used by the shorten_last_dialogue check, where
    even a whitespace-padded ID is an out-of-bounds change that must fail
    the whole result. The default (evidence) mode strips surrounding
    whitespace and excludes auto-regenerated IDs, references and notes
    fields, which alone are never a substantive modification.
    """
    skips = () if strict else _EVIDENCE_EXCLUDED_PATHS
    normalize = not strict
    changes: list[FieldChange] = []
    for field in CREATIVE_ARTIFACTS:
        _collect(
            (field,), _dump(getattr(before, field)), _dump(getattr(after, field)),
            changes, skips, normalize,
        )
    return changes


def artifact_of(path: str) -> str:
    """The top-level artifact a change path belongs to."""
    return path.split(".", 1)[0].split("[", 1)[0]


# ── Result validation ────────────────────────────────────


def validate_general_result(
    before: ProjectState, after: ProjectState, agent_name: str
) -> list[FieldChange]:
    """A general refine must at least change the routed artifact for real."""
    artifact = REFINABLE_AGENTS[agent_name]
    changes = diff_field_changes(before, after)
    if not any(artifact_of(change.path) == artifact for change in changes):
        raise RefineNoMeaningfulChange(
            f"模型未对 {artifact} 产生实质修改（仅备注、自动 ID 或元数据变化不算修改），修改未应用",
        )
    validate_reference_integrity(before, after)
    return changes


# Relations confirmed from the models and their consumers: the VideoGen
# export resolves a shot's scene via scene_id to attach character context,
# script scenes may point at a scene design, and visual highlights point at
# storyboard shots. IDs and references are exact strings — a whitespace-
# padded or rebuilt ID is a different key and breaks the lookup.


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicated: set[str] = set()
    for value in values:
        if value in seen:
            duplicated.add(value)
        seen.add(value)
    return duplicated


def _reference_violations(state: ProjectState) -> set[tuple[str, str]]:
    """(reference path, kind) pairs for dangling or ambiguous references.

    Empty optional references mean "unlinked" per the existing model
    semantics and are skipped; duplicate keys make references to them
    ambiguous.
    """
    violations: set[tuple[str, str]] = set()

    scene_ids = [s.scene_id for s in state.script.scenes] if state.script else []
    scene_id_dups = _duplicates(scene_ids)
    if state.storyboard:
        for index, shot in enumerate(state.storyboard.shots):
            if not shot.scene_id:
                continue
            path = f"storyboard.shots[{index}].scene_id"
            if shot.scene_id in scene_id_dups:
                violations.add((path, "ambiguous"))
            elif shot.scene_id not in set(scene_ids):
                violations.add((path, "dangling"))

    design_ids = [s.id for s in (state.scenes or [])]
    design_dups = _duplicates(design_ids)
    if state.script:
        for index, scene in enumerate(state.script.scenes):
            if not scene.scene_design_id:
                continue
            path = f"script.scenes[{index}].scene_design_id"
            if scene.scene_design_id in design_dups:
                violations.add((path, "ambiguous"))
            elif scene.scene_design_id not in set(design_ids):
                violations.add((path, "dangling"))

    shot_ids = [s.shot_id for s in state.storyboard.shots] if state.storyboard else []
    shot_dups = _duplicates(shot_ids)
    for index, highlight in enumerate(state.visual_highlights or []):
        for ref_index, ref in enumerate(highlight.related_shot_ids):
            if not ref:
                continue
            path = f"visual_highlights[{index}].related_shot_ids[{ref_index}]"
            if ref in shot_dups:
                violations.add((path, "ambiguous"))
            elif ref not in set(shot_ids):
                violations.add((path, "dangling"))

    return violations


def validate_reference_integrity(before: ProjectState, after: ProjectState) -> None:
    """Reject references this modification newly breaks.

    Pre-existing defects (already present in the before snapshot) do not
    block unrelated edits — only violations the modification introduces
    fail the request. The check is independent of the evidence diff's
    ID/reference exclusions, IDs are never rewritten or guessed on the
    model's behalf, and no downstream artifact is dropped to "fix" a link.
    """
    new_violations = _reference_violations(after) - _reference_violations(before)
    if new_violations:
        shown = ", ".join(f"{path}({kind})" for path, kind in sorted(new_violations)[:5])
        raise RefineConstraintFailed(f"修改破坏了引用完整性: {shown}，修改未应用")


def validate_shorten_result(
    before: ProjectState, after: ProjectState, target: DialogueTarget
) -> FieldChange:
    """Deterministic check for the constrained shorten-last-dialogue edit.

    Everything except the target dialogue is compared raw — the only
    permitted difference is the target field itself. The target's rules
    (non-empty, different, strictly fewer characters) are judged on the
    stripped string, mirroring how the target was located. Any out-of-
    bounds change fails the whole result — partial adoption from an
    over-reaching rewrite is forbidden.
    """
    allowed = (
        f"script.scenes[{target.scene_index}].blocks[{target.block_index}]"
        ".content.dialogue"
    )
    raw_changes = diff_field_changes(before, after, strict=True)
    offenders = sorted({c.path for c in raw_changes if c.path != allowed})
    if offenders:
        shown = ", ".join(offenders[:5]) + ("…" if len(offenders) > 5 else "")
        raise RefineConstraintFailed(f"修改超出允许范围（仅最后一句对白可改）: {shown}")
    target_change = next((c for c in raw_changes if c.path == allowed), None)
    if target_change is None:
        raise RefineConstraintFailed("最后一句对白未被修改，修改未应用")
    if not isinstance(target_change.after, str):
        raise RefineConstraintFailed("修改后的对白不是文本，修改未应用")
    new_text = target_change.after.strip()
    if not new_text:
        raise RefineConstraintFailed("修改后的对白为空，修改未应用")
    if new_text == target.original:
        raise RefineConstraintFailed("修改后的对白与原文相同，修改未应用")
    if len(new_text) >= len(target.original):
        raise RefineConstraintFailed(
            f"修改后的对白没有变短（{len(new_text)} 字 ≥ 原文 {len(target.original)} 字），修改未应用",
        )
    return target_change


# ── Change summary (API-facing) ──────────────────────────

MAX_SUMMARY_CHANGES = 10
PREVIEW_LIMIT = 300


def format_change_value(value: Any) -> str:
    """Render one side of a change as a bounded preview string."""
    if value is _MISSING or value is None:
        return "（不存在）"
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT] + "..."


def build_change_summary(before: ProjectState, after: ProjectState) -> dict:
    """Read-only summary of what actually changed between two snapshots."""
    changes = diff_field_changes(before, after)
    return {
        "changed_artifacts": sorted({artifact_of(c.path) for c in changes}),
        "changes": [
            {
                "path": change.path,
                "before": format_change_value(change.before),
                "after": format_change_value(change.after),
            }
            for change in changes[:MAX_SUMMARY_CHANGES]
        ],
        "total_changes": len(changes),
    }


# ── Agent instruction building ───────────────────────────


def _artifact_payload(state: ProjectState, field: str) -> str:
    value = getattr(state, field)
    if value is None:
        return "（当前为空）"
    if isinstance(value, list):
        return json.dumps([_dump(item) for item in value], ensure_ascii=False, indent=2)
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2, ensure_ascii=False)
    return str(value)


def build_agent_instruction(
    state: ProjectState,
    decision: RoutingDecision,
    user_message: str,
    target: DialogueTarget | None,
) -> str:
    """Compose the modification instruction handed to the target agent.

    The current artifact JSON, the user's request and the retention
    constraints travel together, and the constrained variant names the one
    field that may change. The agent must submit real content; a notes-only
    claim of completion will not survive validation.
    """
    artifact_field = REFINABLE_AGENTS[decision.next_agent]
    label = _ARTIFACT_LABELS[artifact_field]
    if decision.constraint == "shorten_last_dialogue" and target is not None:
        return SHORTEN_DIALOGUE_PROMPT.format(
            scene_number=target.scene_number or target.scene_index + 1,
            scene_id=target.scene_id,
            block_ordinal=target.block_index + 1,
            original=target.original,
            length=len(target.original),
            request=user_message,
            script_json=_artifact_payload(state, "script"),
        )
    return GENERAL_MODIFICATION_PROMPT.format(
        label=label,
        artifact=artifact_field,
        request=user_message,
        artifact_json=_artifact_payload(state, artifact_field),
    )
