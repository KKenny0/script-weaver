"""Shared resume contract for Web and CLI generation (ticket #15).

Both surfaces call the SAME validation for the SAME checkpoint data, so an
illegal checkpoint is refused with the same ``ResumeRejected`` code and
message everywhere, and every rejection happens before any model call.

The contract has three parts:

* :class:`ResumeCheckpoint` — the durable envelope (format version, original
  input, state snapshot + digest, completed success prefix, execution
  fingerprint, growth status, resume basis). Credentials never enter it:
  the fingerprint covers provider/model/temperature/max_tokens/endpoint
  identity, gate config and the CONTENT of the skills actually loaded.
* :func:`validate_completed_steps` — ``completed_steps`` must be a strict
  contiguous prefix of the pipeline's ``GENERATION_STEPS`` whose artifacts
  all exist in the snapshot.
* :func:`current_fingerprint` / :func:`fingerprint_diffs` — the execution
  basis is computed from the resolved settings and the DISCOVERED skill
  contents, never from configuration names, so a checkpoint recorded under
  one skill file cannot be silently resumed under another.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from script_weaver.core.config import Settings, get_settings
from script_weaver.core.types import ProjectState, ProjectStatus

CHECKPOINT_FORMAT_VERSION = 1

# Growth is recorded separately from content completion (ticket #15 §5).
GROWTH_STATUSES = ("pending", "running", "succeeded", "failed", "interrupted")

GROWTH_STATUS_MESSAGES = {
    "running": "正在执行成长任务（画像更新 / Skill 评估）…",
    "succeeded": "成长任务完成。",
    "failed": "成长任务失败，内容不受影响。",
    "interrupted": "成长任务曾被打断，其副作用可能已部分发生；不会自动重放。",
}


class ResumeRejected(ValueError):
    """A checkpoint cannot be resumed, with a stable machine-readable code.

    Subclasses :class:`ValueError` so the pipeline's legacy unknown-step
    validation keeps raising a ValueError-compatible error.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── Checkpoint envelope ─────────────────────────────────────


class ResumeCheckpoint(BaseModel):
    """The durable resume record — identical shape on Web and CLI.

    ``state`` is the ProjectState snapshot as of the last completed step
    (``None`` while no step has completed). ``state_digest`` guards it
    against corruption/tampering. ``basis`` carries surface-specific
    anchoring facts: the Web stores the revision the checkpoint was
    committed at, the CLI stores a checkpoint id and revision info.
    """

    model_config = {"extra": "forbid"}

    format_version: int = CHECKPOINT_FORMAT_VERSION
    user_input: str = ""
    completed_steps: list[str] = Field(default_factory=list)
    state: dict[str, Any] | None = None
    state_digest: str = ""
    fingerprint: dict[str, Any] = Field(default_factory=dict)
    growth_status: str = "pending"
    growth_error: str | None = None
    basis: dict[str, Any] = Field(default_factory=dict)

    @property
    def content_complete(self) -> bool:
        """Content generation finished (the finalize step committed)."""
        return "finalize" in self.completed_steps

    def state_model(self) -> ProjectState | None:
        if self.state is None:
            return None
        return ProjectState.model_validate(self.state)


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_checkpoint(
    *,
    user_input: str,
    completed_steps: Iterable[str] = (),
    state: ProjectState | None = None,
    fingerprint: dict[str, Any] | None = None,
    growth_status: str = "pending",
    growth_error: str | None = None,
    basis: dict[str, Any] | None = None,
) -> ResumeCheckpoint:
    state_dict = state.model_dump(mode="json") if state is not None else None
    return ResumeCheckpoint(
        user_input=user_input,
        completed_steps=list(completed_steps),
        state=state_dict,
        state_digest=_digest(state_dict) if state_dict is not None else "",
        fingerprint=dict(fingerprint or {}),
        growth_status=growth_status,
        growth_error=growth_error,
        basis=dict(basis or {}),
    )


def encode_checkpoint(checkpoint: ResumeCheckpoint) -> str:
    return checkpoint.model_dump_json(indent=2)


def decode_checkpoint(text: str | None) -> ResumeCheckpoint:
    """Decode and structurally verify a stored checkpoint.

    Raises :class:`ResumeRejected` with a stable code for every refusal:
    ``checkpoint_invalid`` for corrupt JSON/schema/digest, and
    ``checkpoint_unsupported`` for a future format version. A pre-#15 raw
    ProjectState blob (the old ``checkpoint_json`` column content) fails
    schema validation — exactly the "no resume basis" case.
    """
    if text is None or not text.strip():
        raise ResumeRejected("checkpoint_invalid", "checkpoint 为空。")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResumeRejected(
            "checkpoint_invalid", f"checkpoint 已损坏，无法解析：{exc}"
        ) from None
    if isinstance(data, dict) and data.get("format_version", 0) > CHECKPOINT_FORMAT_VERSION:
        raise ResumeRejected(
            "checkpoint_unsupported",
            f"checkpoint 格式版本 {data['format_version']} 超出当前程序支持"
            f"（最高 {CHECKPOINT_FORMAT_VERSION}），已保留原文件。",
        )
    try:
        checkpoint = ResumeCheckpoint.model_validate(data)
    except ValidationError:
        raise ResumeRejected(
            "checkpoint_invalid",
            "checkpoint 结构不符合恢复契约（缺少依据或为旧格式），已保留原文件。",
        ) from None
    if checkpoint.state is not None:
        if not checkpoint.state_digest:
            raise ResumeRejected(
                "checkpoint_invalid", "checkpoint 缺少状态摘要，无法校验快照。"
            )
        if _digest(checkpoint.state) != checkpoint.state_digest:
            raise ResumeRejected(
                "checkpoint_invalid", "checkpoint 状态摘要不匹配，快照可能已损坏。"
            )
    if checkpoint.growth_status not in GROWTH_STATUSES:
        raise ResumeRejected(
            "checkpoint_invalid", f"未知的成长任务状态：{checkpoint.growth_status!r}"
        )
    return checkpoint


def update_checkpoint_envelope(
    existing_text: str | None,
    *,
    state_json: str | None = None,
    completed_steps: list[str] | None = None,
    revision: int | None = None,
    growth_status: str | None = None,
    growth_error: str | None = None,
) -> str:
    """Advance a stored envelope in place, tolerating legacy/absent input.

    Used by the Web store's stage-commit and growth paths: the row's
    existing envelope (written at admission) is carried forward so the
    fingerprint and input recorded at admission survive every later
    update. A legacy raw-state blob or missing column value seeds a fresh
    envelope — such runs simply carry no fingerprint basis.
    """
    envelope: dict[str, Any] | None = None
    if existing_text:
        try:
            parsed = json.loads(existing_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and "format_version" in parsed:
            envelope = parsed
    if envelope is None:
        envelope = {
            "format_version": CHECKPOINT_FORMAT_VERSION,
            "user_input": "",
            "completed_steps": [],
            "state": None,
            "state_digest": "",
            "fingerprint": {},
            "growth_status": "pending",
            "growth_error": None,
            "basis": {},
        }
    if state_json is not None:
        state_dict = json.loads(state_json)
        envelope["state"] = state_dict
        envelope["state_digest"] = _digest(state_dict)
    if completed_steps is not None:
        envelope["completed_steps"] = list(completed_steps)
    if revision is not None:
        envelope.setdefault("basis", {})["revision"] = revision
    if growth_status is not None:
        envelope["growth_status"] = growth_status
    envelope["growth_error"] = growth_error
    return json.dumps(envelope, ensure_ascii=False)


def write_checkpoint_atomic(path: Path, checkpoint: ResumeCheckpoint) -> None:
    """Atomically persist a checkpoint: same-dir temp file, fsync, replace.

    ``os.replace`` is atomic within one filesystem, so a hard kill either
    leaves the previous checkpoint or the new one — never a half-written
    file. The temp file is removed if anything fails before the replace.
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(encode_checkpoint(checkpoint))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


# ── Completed-step prefix validation ────────────────────────


def validate_completed_steps(
    steps: Iterable[str], *, state: ProjectState | None = None
) -> None:
    """``steps`` must be a strict contiguous prefix of GENERATION_STEPS.

    Rejects unknown steps, duplicates, out-of-order entries, gaps, and
    snapshots missing the artifact a claimed step should have produced.
    Raises :class:`ResumeRejected` (a ValueError) before any model call.
    """
    from script_weaver.core.pipeline import GENERATION_STEP_LABELS, GENERATION_STEPS

    steps = list(steps)
    known = set(GENERATION_STEPS)
    unknown = [s for s in steps if s not in known]
    if unknown:
        # Exact legacy wording: the pipeline surfaced this as a ValueError
        # before ticket #15 tightened the contract.
        raise ResumeRejected(
            "invalid_prefix", f"Unknown completed steps: {sorted(set(unknown))}"
        )
    if len(set(steps)) != len(steps):
        raise ResumeRejected("invalid_prefix", f"completed steps 含重复项：{steps}")
    prefix = list(GENERATION_STEPS)[: len(steps)]
    if steps != prefix:
        raise ResumeRejected(
            "invalid_prefix",
            "completed steps 必须是生成阶段的连续前缀（不允许乱序或跳步）："
            f"记录为 {steps}，应为 {prefix}。",
        )
    if state is not None and steps:
        missing = [
            GENERATION_STEP_LABELS.get(s, s)
            for s in steps
            if not _artifact_for_step(state, s)
        ]
        if missing:
            raise ResumeRejected(
                "invalid_prefix",
                f"快照缺少已完成阶段的有效产物：{missing}；拒绝据此续跑。",
            )


def _artifact_for_step(state: ProjectState, step: str) -> bool:
    from script_weaver.core.types import Script, Storyboard

    if step == "idea_refiner":
        return bool(state.refined_idea)
    if step == "structurer":
        return state.outline is not None and bool(state.outline.plot_outline)
    if step == "character_designer":
        return bool(state.characters)
    if step == "scene_designer":
        return bool(state.scenes)
    if step == "art_director":
        return state.art_style is not None
    if step == "scriptwriter":
        return isinstance(state.script, Script) and bool(state.script.scenes)
    if step in ("storyboard_artist", "finalize"):
        ok = isinstance(state.storyboard, Storyboard) and bool(state.storyboard.shots)
        if step == "finalize":
            ok = ok and state.meta.status is ProjectStatus.COMPLETE
        return ok
    return False


# ── Execution fingerprint ───────────────────────────────────


def endpoint_identity(settings: Settings) -> str:
    """Routing identity of the request endpoint — never a credential."""
    base = getattr(settings, "openai_base_url", None)
    if settings.llm_provider in ("openai", "openai_compatible") and base:
        return f"{settings.llm_provider}:{base}"
    return settings.llm_provider


def skill_content_fingerprint(skill_registry: Any) -> dict[str, str]:
    """Fingerprint the CONTENT of every discovered skill.

    Identity is derived from what agents actually inject (prompt, schema
    override, constraints, examples, stage), so editing a skill file or
    swapping in another file under the same id invalidates a checkpoint.
    """
    fingerprints: dict[str, str] = {}
    for skill in sorted(skill_registry.list_all(), key=lambda s: s.id):
        payload = {
            "id": skill.id,
            "stage": skill.stage.value if skill.stage else None,
            "prompt_injection": skill.prompt_injection,
            "output_schema_override": skill.output_schema_override,
            "constraints": skill.constraints,
            "examples": [
                e.model_dump(mode="json") if hasattr(e, "model_dump") else e
                for e in (skill.examples or [])
            ],
        }
        fingerprints[skill.id] = _digest(payload)
    return fingerprints


async def current_fingerprint(
    *,
    skill_bindings: dict[str, Any] | None = None,
    auto_approve: bool = True,
    skill_registry: Any | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """The execution basis of the config ABOUT to run — no credentials.

    When ``skill_registry`` is omitted a fresh registry is discovered from
    disk (the caller-facing admission check). The pipeline passes its own
    discovered registry so the validated basis and the executing instance
    resolve from the same in-memory skill contents.
    """
    settings = settings or get_settings()
    if skill_registry is None:
        from script_weaver.skills.registry import SkillRegistry

        skill_registry = SkillRegistry()
        await skill_registry.discover()
    bindings = skill_bindings or {}
    return {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
        "endpoint": endpoint_identity(settings),
        "auto_approve": bool(auto_approve),
        "skill_bindings": json.loads(json.dumps(bindings, ensure_ascii=False)),
        "skills": skill_content_fingerprint(skill_registry),
    }


_FP_LABELS = {
    "provider": "模型提供商",
    "model": "模型",
    "temperature": "temperature",
    "max_tokens": "max_tokens",
    "endpoint": "接入端点",
    "auto_approve": "gate 配置",
    "skill_bindings": "Skill 绑定",
}


def fingerprint_diffs(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Human-readable differences between two execution fingerprints."""
    diffs: list[str] = []
    for key, label in _FP_LABELS.items():
        if key not in expected or key not in actual:
            continue
        if expected.get(key) != actual.get(key):
            if key == "skill_bindings":
                diffs.append("Skill 绑定与恢复依据不一致。")
            else:
                diffs.append(
                    f"{label}已变化：恢复依据为 {expected.get(key)!r}，"
                    f"当前为 {actual.get(key)!r}。"
                )
    expected_skills = expected.get("skills") or {}
    actual_skills = actual.get("skills") or {}
    changed = sorted(
        sid
        for sid in set(expected_skills) | set(actual_skills)
        if expected_skills.get(sid) != actual_skills.get(sid)
    )
    if changed:
        diffs.append(f"Skill 实际载入内容与恢复依据不一致：{changed}。")
    return diffs


def assert_fingerprint_match(
    expected: dict[str, Any], actual: dict[str, Any]
) -> None:
    """Refuse to resume when the execution basis drifted (zero model calls)."""
    required = {
        "provider": str, "model": str, "temperature": (int, float),
        "max_tokens": int, "endpoint": str, "auto_approve": bool,
        "skill_bindings": dict, "skills": dict,
    }
    for fingerprint in (expected, actual):
        if any(
            key not in fingerprint or not isinstance(fingerprint[key], kind)
            or (key in ("temperature", "max_tokens") and isinstance(fingerprint[key], bool))
            for key, kind in required.items()
        ) or any(not isinstance(k, str) or not isinstance(v, str)
                 for k, v in fingerprint.get("skills", {}).items()):
            raise ResumeRejected("resume_basis_missing", "checkpoint 缺少有效的执行依据，拒绝恢复（未调用模型）。")
    diffs = fingerprint_diffs(expected, actual)
    if diffs:
        raise ResumeRejected(
            "config_changed",
            "执行依据与 checkpoint 不一致，已拒绝恢复（未调用模型）：\n- "
            + "\n- ".join(diffs),
        )
