"""Deterministic short-drama validation services adapted from drama-skills."""

from __future__ import annotations

from typing import Any, Literal


def finding(code: str, message: str, path: str = "") -> dict[str, str]:
    return {"code": code, "severity": "error", "message": message, "path": path}


class AssetValidationService:
    def validate(self, assets: list[dict[str, Any]]) -> list[dict[str, str]]:
        results = []
        seen = set()
        for index, asset in enumerate(assets):
            name = str(asset.get("name", "")).strip()
            if not name:
                results.append(finding("ASSET_NAME_REQUIRED", "资产名称不能为空", str(index)))
            if name in seen:
                results.append(finding("ASSET_NAME_DUPLICATE", "资产名称重复", str(index)))
            seen.add(name)
        return results


class StoryboardValidationService:
    def validate(self, shots: list[dict[str, Any]]) -> list[dict[str, str]]:
        results = []
        orders = [shot.get("order_index") for shot in shots]
        if orders != sorted(orders):
            results.append(finding("SHOT_ORDER_INVALID", "镜头顺序必须递增"))
        for index, shot in enumerate(shots):
            if float(shot.get("duration_seconds", 0)) <= 0:
                results.append(finding("SHOT_DURATION_INVALID", "镜头时长必须大于零", str(index)))
            if not shot.get("start_boundary") and not shot.get("start_boundary_json"):
                results.append(finding("SHOT_START_MISSING", "镜头缺少起始边界", str(index)))
        return results


class PromptValidationService:
    def __init__(self, kind: Literal["image", "video"]):
        self.kind = kind

    def validate(self, prompts: list[dict[str, Any]]) -> list[dict[str, str]]:
        results = []
        for index, prompt in enumerate(prompts):
            content = str(prompt.get("content", "")).strip()
            if not content:
                results.append(finding("PROMPT_EMPTY", f"{self.kind} prompt 不能为空", str(index)))
            if self.kind == "video" and not any(word in content.lower() for word in ("camera", "move", "static", "track", "push", "pan", "tilt")):
                results.append(finding("MOTION_CAMERA_MISSING", "视频提示词缺少运镜或静态约束", str(index)))
        return results


class ReviewValidationService:
    def validate(self, review: dict[str, Any]) -> list[dict[str, str]]:
        results = []
        if review.get("verdict") not in {"PASS", "REVISE", "BLOCK"}:
            results.append(finding("REVIEW_VERDICT_INVALID", "审查结论必须是 PASS、REVISE 或 BLOCK"))
        for index, item in enumerate(review.get("findings", [])):
            if not item.get("evidence") or not item.get("required_result"):
                results.append(finding("REVIEW_FINDING_INCOMPLETE", "审查问题必须包含证据和修订结果", str(index)))
        return results
