"""Safe single-card editing for characters, scenes and shots (ticket #16).

Manual card edits are the one write path where a human — not a validated
pipeline artifact — supplies the values, so this module owns the explicit
contract between the PATCH endpoint and the stored ProjectState:

- **Writable-field whitelist per kind.** Every other field (ids, shot
  ``scene_id``/``sequence_number``, reference media URLs, and anything the
  models grow later) is rejected on sight — even when submitted unchanged —
  so a stale client can never rewrite identity or ownership.
- **Canonical enum values only.** The ``_LenientStrEnum`` synonyms exist for
  LLM output compatibility and must not become a manual-edit backdoor: an
  edit is validated against the enum's canonical values exactly, without
  touching the lenient parsing used elsewhere.
- **Bounded durations.** ``duration_seconds`` must be a finite positive
  number — booleans, zero, negatives and non-finite values are refused.

The affected-artifact review flags are deliberately conservative: a
character/scene edit marks the existing script, storyboard and highlights
for review; a shot edit recomputes the storyboard totals and marks existing
highlights. No shot-reference analysis is claimed — the flag says "upstream
content changed by hand", not "these downstream artifacts are provably
stale". Missing downstream artifacts never grow flags or placeholder
objects. Ticket #17 adds the other half of the flag lifecycle here: the
confirm-along selection (validated, matched by its full identity and
removed atomically) and the compact per-artifact export warnings.

All functions here are pure with respect to storage: they mutate (and
validate) an in-memory ProjectState; the caller owns the transaction.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from script_weaver.core.project_store import ProjectStoreError
from script_weaver.core.types import (
    CameraAngle,
    CameraMovement,
    CharacterRole,
    EnvironmentType,
    ProjectState,
    ShotSize,
    TransitionType,
)

# The only card kinds the PATCH endpoint accepts.
CARD_KINDS = ("characters", "scenes", "shots")

KIND_LABELS = {"characters": "角色", "scenes": "场景", "shots": "镜头"}

# Value-type markers for the field specs below.
_STR = "str"
_STR_OR_NONE = "str_or_none"
_STR_LIST = "str_list"
_STR_DICT = "str_dict"
_POSITIVE_NUMBER = "positive_number"

# Writable fields per kind (ticket #16 contract). Anything not listed here —
# including identity fields and reference media URLs — is read-only.
CHARACTER_FIELDS: dict[str, Any] = {
    "name": _STR,
    "role": CharacterRole,
    "appearance": _STR,
    "personality": _STR,
    "costume_description": _STR_OR_NONE,
    "key_props": _STR_LIST,
    "backstory": _STR_OR_NONE,
    "motivation": _STR,
    "relationship_map": _STR_DICT,
    "image_prompt": _STR_OR_NONE,
}

SCENE_FIELDS: dict[str, Any] = {
    "name": _STR,
    "location_type": EnvironmentType,
    "environment": _STR,
    "time_of_day": _STR,
    "weather": _STR_OR_NONE,
    "mood": _STR,
    "lighting_description": _STR_OR_NONE,
    "color_palette": _STR_LIST,
    "key_elements": _STR_LIST,
    "image_prompt": _STR_OR_NONE,
}

SHOT_FIELDS: dict[str, Any] = {
    "shot_size": ShotSize,
    "camera_angle": CameraAngle,
    "camera_movement": CameraMovement,
    "movement_description": _STR_OR_NONE,
    "visual_description": _STR,
    "action_description": _STR_OR_NONE,
    "dialogue": _STR_OR_NONE,
    "voiceover": _STR_OR_NONE,
    "on_screen_text": _STR_OR_NONE,
    "sound_effects": _STR_LIST,
    "music_cue": _STR_OR_NONE,
    "music_mood": _STR_OR_NONE,
    "duration_seconds": _POSITIVE_NUMBER,
    "transition_to_next": TransitionType,
    "image_prompt": _STR_OR_NONE,
    "video_prompt": _STR_OR_NONE,
    "negative_prompt": _STR_OR_NONE,
}

WRITABLE_FIELDS: dict[str, dict[str, Any]] = {
    "characters": CHARACTER_FIELDS,
    "scenes": SCENE_FIELDS,
    "shots": SHOT_FIELDS,
}

# Identity / ownership / media-reference fields, refused even when the
# submitted value equals the stored one.
READ_ONLY_FIELDS: dict[str, tuple[str, ...]] = {
    "characters": ("id", "image_reference_url"),
    "scenes": ("id", "image_reference_url"),
    "shots": ("shot_id", "scene_id", "sequence_number", "reference_image_url"),
}

# The id attribute each kind's stable identity lives on.
_ID_ATTRIBUTE = {"characters": "id", "scenes": "id", "shots": "shot_id"}

# Reason recorded on every review flag produced by a manual upstream edit.
REVIEW_REASON = "upstream_manual_edit"

# Artifacts a manual edit can flag for review — and the only ones a
# confirmation can target (ticket #17).
CONFIRMABLE_ARTIFACTS = ("script", "storyboard", "visual_highlights")

ARTIFACT_LABELS = {
    "script": "剧本",
    "storyboard": "分镜",
    "visual_highlights": "影像亮点",
}

# The fields that identify one stored review flag for confirmation. Display
# fields (upstream_label, created_at/updated_at) are never matched: a client
# may hold a stale label, but its decision is bound to what happened, when.
SELECTION_FIELDS = (
    "artifact", "reason", "upstream_kind", "upstream_id", "since_revision",
)

# The ownership field every confirm selection must carry on top of the
# identity: two projects can hold byte-identical flags, so the five stored
# fields alone cannot bind a selection to the project it was decided in
# (review round 2). Stored flags never grow this field — it is validated
# against the target project and then dropped from the matching key.
SELECTION_OWNERSHIP_FIELD = "project_id"

# Largest float the platform can represent: beyond this a JSON integer is
# not a usable duration (and math.isfinite would overflow converting it).
_MAX_FLOAT = sys.float_info.max


class CardEditError(ProjectStoreError):
    """Base class for manual card-edit refusals."""


class CardNotFoundError(CardEditError):
    """The target card does not exist in the given project."""


class DuplicateTargetIdError(CardEditError):
    """The stored data holds several objects with the same stable id.

    Saving would silently pick one of them, so the edit is refused and the
    anomaly reported instead.
    """


class InvalidCardChangeError(CardEditError):
    """A change violates the field whitelist or a value constraint."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


@dataclass
class CardEditOutcome:
    """What :func:`apply_card_changes` actually did to the state."""

    changed: bool
    label: str
    applied_fields: list[str] = field(default_factory=list)
    # Downstream artifact keys that exist and must be flagged for review.
    affected_artifacts: list[str] = field(default_factory=list)


def _enum_values(enum_cls: type[Enum]) -> tuple[str, ...]:
    return tuple(member.value for member in enum_cls)


def _validate_value(field: str, spec: Any, value: Any) -> None:
    """Validate one submitted change value against its field spec."""
    where = f"字段「{field}」"
    if spec is _STR:
        if not isinstance(value, str):
            raise InvalidCardChangeError(f"{where}必须是字符串", field=field)
        return
    if spec is _STR_OR_NONE:
        if value is not None and not isinstance(value, str):
            raise InvalidCardChangeError(f"{where}必须是字符串或 null", field=field)
        return
    if spec is _STR_LIST:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvalidCardChangeError(f"{where}必须是字符串列表", field=field)
        return
    if spec is _STR_DICT:
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise InvalidCardChangeError(
                f"{where}必须是字符串到字符串的映射", field=field
            )
        return
    if spec is _POSITIVE_NUMBER:
        # bool is an int subclass — "true" is not a duration.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidCardChangeError(f"{where}必须是数字", field=field)
        # A JSON integer has arbitrary precision: converting an oversized one
        # to float overflows, so bounds-check it before isfinite touches it.
        if isinstance(value, int) and (
            value > _MAX_FLOAT or value < -_MAX_FLOAT
        ):
            raise InvalidCardChangeError(f"{where}数值过大", field=field)
        if not math.isfinite(value):
            raise InvalidCardChangeError(f"{where}必须是有限数字", field=field)
        if value <= 0:
            raise InvalidCardChangeError(f"{where}必须是正数", field=field)
        return
    # An enum spec: canonical member values only — the lenient synonym
    # parsing that serves model output must not silently rewrite a
    # human-entered value into a different meaning.
    if isinstance(value, str) and value in _enum_values(spec):
        return
    allowed = "、".join(repr(v) for v in _enum_values(spec))
    raise InvalidCardChangeError(
        f"{where}必须是规范枚举值之一：{allowed}", field=field
    )


def _affected_artifacts(state: ProjectState, kind: str) -> list[str]:
    """Existing downstream artifacts a card edit conservatively invalidates.

    Conservative on purpose: any character/scene edit may ripple into the
    script, the storyboard and the highlights; a shot edit recomputes the
    storyboard totals locally and only the highlights are flagged. Artifacts
    that do not exist are skipped — never fabricated.
    """
    if kind == "shots":
        candidates = ("visual_highlights",)
    else:
        candidates = ("script", "storyboard", "visual_highlights")
    return [
        artifact
        for artifact in candidates
        if _artifact_exists(state, artifact)
    ]


def _artifact_exists(state: ProjectState, artifact: str) -> bool:
    if artifact == "script":
        return state.script is not None
    if artifact == "storyboard":
        return state.storyboard is not None
    if artifact == "visual_highlights":
        return bool(state.visual_highlights)
    return False


def _coerce_value(spec: Any, value: Any) -> Any:
    """Convert a validated change value to its in-memory model type.

    Canonical enum strings become enum members — a plain ``setattr`` of the
    string would leave a raw str where the model (and downstream readers of
    this state) expect the enum, even though the serialized JSON would look
    identical. JSON integers become floats so derived computations (the
    shot-total sum) stay in float space where overflow is observable.
    """
    if isinstance(spec, type) and issubclass(spec, Enum):
        return spec(value)
    if spec is _POSITIVE_NUMBER:
        return float(value)
    return value


def apply_card_changes(
    state: ProjectState,
    kind: str,
    target_id: str,
    changes: dict[str, Any],
) -> CardEditOutcome:
    """Validate and apply manual edits to exactly one card of ``state``.

    Raises :class:`InvalidCardChangeError` for unknown/read-only fields or
    bad values (before anything is mutated), :class:`CardNotFoundError` when
    the target id matches nothing, and :class:`DuplicateTargetIdError` when
    it matches more than one object. Returns the outcome; the caller owns
    persistence and the review-flag bookkeeping.
    """
    fields = WRITABLE_FIELDS.get(kind)
    if fields is None:
        raise InvalidCardChangeError(
            f"未知的卡片类型：{kind!r}（允许：{'、'.join(CARD_KINDS)}）"
        )
    if not isinstance(changes, dict):
        raise InvalidCardChangeError("changes 必须是对象")

    unknown = [f for f in changes if f not in fields]
    if unknown:
        read_only = READ_ONLY_FIELDS.get(kind, ())
        for f in sorted(unknown):
            if f in read_only:
                raise InvalidCardChangeError(
                    f"字段「{f}」只读，不能修改（提交即拒绝，即使值未变化）",
                    field=f,
                )
        raise InvalidCardChangeError(
            f"未知字段：{'、'.join(sorted(unknown))}", field=min(unknown)
        )
    for f, value in changes.items():
        _validate_value(f, fields[f], value)

    id_attr = _ID_ATTRIBUTE[kind]
    collection = _card_collection(state, kind)
    matches = [obj for obj in collection if getattr(obj, id_attr, None) == target_id]
    if not matches:
        raise CardNotFoundError(
            f"项目内不存在{KIND_LABELS.get(kind, kind)}「{target_id}」"
        )
    if len(matches) > 1:
        raise DuplicateTargetIdError(
            f"数据异常：{len(matches)} 个{KIND_LABELS.get(kind, kind)}对象共用"
            f" ID「{target_id}」，拒绝保存以避免修改到不确定的对象。"
        )
    target = matches[0]

    applied: list[str] = []
    for f, value in changes.items():
        coerced = _coerce_value(fields[f], value)
        if getattr(target, f) != coerced:
            setattr(target, f, coerced)
            applied.append(f)

    label = _card_label(target, target_id)
    if not applied:
        # Nothing actually changed: still fully validated above, but no
        # derived-field work and no review flags belong to this save.
        return CardEditOutcome(changed=False, label=label)

    # Full-object validation after mutation: the model constraints (e.g.
    # duration > 0) re-check the merged result, not just the single value.
    target_cls = type(target)
    try:
        target_cls.model_validate(target.model_dump())
    except Exception as exc:  # pydantic ValidationError — normalize the shape
        raise InvalidCardChangeError(f"修改后的对象校验失败：{exc}") from exc

    if kind == "shots" and state.storyboard is not None:
        state.storyboard.compute_totals()
        # Each duration is finite, but their SUM may not be (1e308 + 1e308).
        # Refuse before anything is written: the caller's transaction rolls
        # back whole, leaving history and review metadata untouched.
        if not math.isfinite(state.storyboard.total_estimated_duration):
            raise InvalidCardChangeError(
                "镜头时长合计溢出，无法保存；请检查各镜头时长是否过大。"
            )

    return CardEditOutcome(
        changed=True,
        label=label,
        applied_fields=applied,
        affected_artifacts=_affected_artifacts(state, kind),
    )


def _card_collection(state: ProjectState, kind: str) -> list:
    if kind == "characters":
        return state.characters or []
    if kind == "scenes":
        return state.scenes or []
    if kind == "shots":
        return state.storyboard.shots if state.storyboard else []
    return []


def _card_label(target: Any, target_id: str) -> str:
    name = getattr(target, "name", None)
    return name if isinstance(name, str) and name else target_id


def build_review_flags(
    kind: str,
    target_id: str,
    label: str,
    since_revision: int,
    affected_artifacts: list[str],
) -> list[dict[str, Any]]:
    """Review flags for one manual edit, one per affected artifact."""
    now = datetime.now(timezone.utc).isoformat()  # noqa: UP017
    return [
        {
            "artifact": artifact,
            "reason": REVIEW_REASON,
            "upstream_kind": kind,
            "upstream_id": target_id,
            "upstream_label": label,
            "since_revision": since_revision,
            "created_at": now,
            "updated_at": now,
        }
        for artifact in affected_artifacts
    ]


def merge_review_flags(
    review: dict[str, Any] | None, new_flags: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fold new flags into the stored review metadata, preserving the rest.

    One flag lives per (artifact, reason): a re-trigger refreshes the
    existing entry (keeping its original created_at) instead of stacking
    duplicates. Flags for other artifacts or reasons — present or future —
    pass through untouched.
    """
    merged: dict[str, Any] = dict(review) if isinstance(review, dict) else {}
    flags = [f for f in merged.get("review_flags") or [] if isinstance(f, dict)]
    for new_flag in new_flags:
        for i, existing in enumerate(flags):
            if (
                existing.get("artifact") == new_flag.get("artifact")
                and existing.get("reason") == new_flag.get("reason")
            ):
                flags[i] = {**new_flag, "created_at": existing.get("created_at")}
                break
        else:
            flags.append(new_flag)
    merged["review_flags"] = flags
    return merged


# ── Confirming flags as kept-as-is (ticket #17) ───────────


class ReviewConfirmError(ProjectStoreError):
    """Base class for confirm-along refusals (ticket #17)."""


class InvalidReviewSelectionError(ReviewConfirmError):
    """A confirm request carries a malformed, unknown or duplicated item."""


class ReviewSelectionMismatchError(ReviewConfirmError):
    """A selection matches no current flag of this project.

    Raised inside the confirm transaction — the caller's basis is stale
    (flags changed, another project's flag, or the upstream re-triggered
    with a new ``since_revision``). Nothing has been written; the client
    must re-read instead of retrying blindly.
    """


def selection_key(flag: Any) -> tuple[Any, ...]:
    """The identity tuple of one review flag / one confirm selection."""
    return tuple(flag.get(f) if isinstance(flag, dict) else None
                 for f in SELECTION_FIELDS)


def validate_review_selections(
    selections: Any,
    *,
    project_id: str | None = None,
) -> list[tuple[Any, ...]]:
    """Validate request-shaped confirm selections; return their keys.

    Accepts the API model's dicts (shape already type-checked there) and
    still enforces the domain rules itself so every caller gets the same
    refusals: confirmable artifacts only, non-empty string identity fields,
    a positive integer ``since_revision``, and no duplicated selection.

    With ``project_id`` given, every selection must also carry
    ``project_id`` naming exactly that project — a missing field stays a
    missing field (never backfilled from the URL, which would mask where
    the decision came from) and a foreign owner refuses the WHOLE batch
    before anything is matched: two projects can hold byte-identical flags,
    so project A's selections must never clear project B's.
    """
    if not isinstance(selections, list) or not selections:
        raise InvalidReviewSelectionError("selections 必须是非空列表")
    keys: list[tuple[Any, ...]] = []
    for i, sel in enumerate(selections):
        where = f"第 {i + 1} 个选择"
        if not isinstance(sel, dict):
            raise InvalidReviewSelectionError(f"{where}必须是对象")
        if project_id is not None:
            owner = sel.get(SELECTION_OWNERSHIP_FIELD)
            if not isinstance(owner, str) or not owner:
                raise InvalidReviewSelectionError(
                    f"{where}缺少所属项目 {SELECTION_OWNERSHIP_FIELD}"
                )
            if owner != project_id:
                raise InvalidReviewSelectionError(
                    f"{where}属于项目「{owner}」，不能提交给项目「{project_id}」"
                )
        artifact = sel.get("artifact")
        if artifact not in CONFIRMABLE_ARTIFACTS:
            raise InvalidReviewSelectionError(
                f"{where}的产物非法：{artifact!r}"
                f"（允许：{'、'.join(CONFIRMABLE_ARTIFACTS)}）"
            )
        for f in ("reason", "upstream_kind", "upstream_id"):
            value = sel.get(f)
            if not isinstance(value, str) or not value:
                raise InvalidReviewSelectionError(
                    f"{where}的 {f} 必须是非空字符串"
                )
        since = sel.get("since_revision")
        if isinstance(since, bool) or not isinstance(since, int) or since < 1:
            raise InvalidReviewSelectionError(
                f"{where}的 since_revision 必须是正整数"
            )
        keys.append(selection_key(sel))
    if len(set(keys)) != len(keys):
        raise InvalidReviewSelectionError(
            "选择中存在重复项（产物、原因、上游与 since_revision 完全相同），"
            "每条待复核标记只能确认一次。"
        )
    return keys


def confirm_review_flags(
    review: dict[str, Any] | None,
    selections: list[dict[str, Any]],
    state: ProjectState,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Match selections against the CURRENT flags and remove exactly those.

    Identity is the full :data:`SELECTION_FIELDS` tuple — never the artifact
    name alone — so an upstream re-trigger (same artifact, new
    ``since_revision``) survives an old confirm request untouched. The
    flagged downstream artifact must still exist; a flag pointing at
    nothing is an anomaly to re-read, not to wave through. Pure: returns
    ``(new_review, confirmed_flags)`` and mutates nothing; the caller owns
    persistence and rolls the whole transaction back on any error.
    """
    new_review: dict[str, Any] = dict(review) if isinstance(review, dict) else {}
    flags = [
        f for f in new_review.get("review_flags") or [] if isinstance(f, dict)
    ]
    confirmed: list[dict[str, Any]] = []
    for sel in selections:
        key = selection_key(sel)
        index = next(
            (i for i, flag in enumerate(flags) if selection_key(flag) == key),
            None,
        )
        if index is None:
            raise ReviewSelectionMismatchError(
                "所选待复核标记已变化或不存在（可能已被确认或上游再次修改），"
                "请重新读取后重试。"
            )
        flag = flags[index]
        if not _artifact_exists(state, flag.get("artifact")):
            raise ReviewSelectionMismatchError(
                "标记对应的下游产物已不存在，请重新读取后重试。"
            )
        confirmed.append(flags.pop(index))
    new_review["review_flags"] = flags
    return new_review, confirmed


def summarize_review_warnings(
    review: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Compact, fixed-shape export warnings, one per (artifact, reason).

    The header form of the pending-review facts: artifact, stable reason
    code and the revision that triggered the flag. Labels, timestamps and
    free text stay out (headers cannot grow unboundedly) — full detail
    remains in the project GET's ``review`` metadata. Unknown or corrupt
    flag shapes are the GET's job to surface; the header only reports
    well-formed flags of the three conservative artifacts. When a duplicate
    (artifact, reason) pair somehow exists, the earliest
    ``since_revision`` wins — the longest-pending fact.
    """
    flags = (
        review.get("review_flags") if isinstance(review, dict) else None
    )
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for flag in flags or []:
        if not isinstance(flag, dict):
            continue
        artifact, reason = flag.get("artifact"), flag.get("reason")
        since = flag.get("since_revision")
        if (
            artifact not in CONFIRMABLE_ARTIFACTS
            or not isinstance(reason, str)
            or not reason
            or isinstance(since, bool)
            or not isinstance(since, int)
            or since < 1
        ):
            continue
        key = (artifact, reason)
        if key not in merged or since < merged[key]["since_revision"]:
            merged[key] = {
                "artifact": artifact,
                "reason": reason,
                "since_revision": since,
            }
    return [merged[key] for key in sorted(merged)]
