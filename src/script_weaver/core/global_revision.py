"""Whole-artifact candidate boundary for the Web refinement entry point."""
import math

from script_weaver.core.card_edit import apply_card_changes, InvalidCardChangeError
from script_weaver.core.refinement import CREATIVE_ARTIFACTS, validate_reference_integrity
from script_weaver.core.types import ProjectState

SUPPORTED = ("characters", "scenes", "script", "storyboard")
DOWNSTREAM = {
    "characters": ("script", "storyboard", "visual_highlights"),
    "scenes": ("script", "storyboard", "visual_highlights"),
    "script": ("storyboard", "visual_highlights"),
    "storyboard": ("visual_highlights",),
}


def has_content(state, artifact):
    value = getattr(state, artifact)
    return bool(value and (value.scenes if artifact == "script" else value.shots if artifact == "storyboard" else value))


def impacts(state, artifact):
    return [name for name in DOWNSTREAM[artifact] if getattr(state, name)]


def validate_global_result(base: ProjectState, result: ProjectState):
    before, after = base.model_dump(mode="json"), result.model_dump(mode="json")
    changed = [name for name in CREATIVE_ARTIFACTS if before[name] != after[name]]
    if len(changed) != 1:
        raise InvalidCardChangeError("全局候选必须只修改一个已有产物，且包含实际内容变化。")
    artifact = changed[0]
    if artifact not in SUPPORTED:
        raise InvalidCardChangeError("阶段 1 全局候选仅支持角色、场景、剧本和分镜；暂不支持大纲或美术风格。")
    if not has_content(base, artifact):
        raise InvalidCardChangeError("目标产物不存在，请先完成生成。")
    # Pipeline bookkeeping is intentionally discarded. All other project
    # inputs and creative artifacts stay identical to the frozen baseline.
    for key in before.keys() - {artifact, "meta", "memory"}:
        if before[key] != after[key]:
            raise InvalidCardChangeError("全局候选越过了单产物边界。")
    state = base.model_copy(deep=True)
    if artifact in ("characters", "scenes", "storyboard"):
        kind = "shots" if artifact == "storyboard" else artifact
        old = before[artifact]["shots"] if kind == "shots" else before[artifact]
        new = after[artifact]["shots"] if kind == "shots" else after[artifact]
        identity = "shot_id" if kind == "shots" else "id"
        if [x[identity] for x in old] != [x[identity] for x in new]:
            raise InvalidCardChangeError("阶段 1 不支持增删或重排，请保留 ID 和顺序。")
        for left, right in zip(old, new):
            changes = {key: value for key, value in right.items() if left.get(key) != value}
            if changes:
                apply_card_changes(state, kind, left[identity], changes)
        if kind == "shots":
            # Totals are derived; board-level settings are outside card editing.
            for key in ("aspect_ratio", "fps", "notes"):
                if before[artifact][key] != after[artifact][key]:
                    raise InvalidCardChangeError("分镜设置为只读；本阶段只修订镜头内容。")
            expected = state.storyboard.model_dump(mode="json")
            if expected != after[artifact]:
                raise InvalidCardChangeError("分镜合计与镜头内容不一致。")
    else:
        left, right = before[artifact]["scenes"], after[artifact]["scenes"]
        if [s["scene_id"] for s in left] != [s["scene_id"] for s in right]:
            raise InvalidCardChangeError("阶段 1 不支持剧本场次增删或重排。")
        for old, new in zip(left, right):
            for key in ("scene_design_id", "characters_involved"):
                if old[key] != new[key]:
                    raise InvalidCardChangeError("剧本场次归属和人物引用必须保持不变。")
            if old["heading"]["scene_number"] != new["heading"]["scene_number"]:
                raise InvalidCardChangeError("剧本场次编号必须保持不变。")
            if len(old["blocks"]) != len(new["blocks"]):
                raise InvalidCardChangeError("阶段 1 不支持剧本块增删。")
            for a, b in zip(old["blocks"], new["blocks"]):
                if a["block_type"] != b["block_type"] or a["content"].get("character_name") != b["content"].get("character_name"):
                    raise InvalidCardChangeError("剧本块类型、顺序和对白人物必须保持不变。")
        state.script = result.script.model_copy(deep=True)
    validate_reference_integrity(base, state)
    if artifact == "script":
        for value in [state.script.total_estimated_duration, *(s.estimated_duration_seconds for s in state.script.scenes)]:
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value <= 0):
                raise InvalidCardChangeError("剧本时长必须为有限正数。")
    # Validate actual values again, including model instances mutated after construction.
    ProjectState.model_validate(state.model_dump())
    return artifact, state
