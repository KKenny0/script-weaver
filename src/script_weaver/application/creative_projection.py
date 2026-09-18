"""Strict, dependency-free Markdown contracts for phase 3 projections."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_MAP_BLOCK = re.compile(
    r"```script-weaver-episode-map[ \t]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)
_EPISODE_HEADING = re.compile(r"^# EP(?P<number>\d{3}) (?P<title>[^\n]+)$")
_SCENE_HEADING = re.compile(
    r"^## EP(?P<episode>\d{3})-SC(?P<scene>\d{3}) "
    r"(?P<interior>内|外|内外) · (?P<location>[^·\n]+) · (?P<time>[^\n]+)$"
)
_DIALOGUE = re.compile(r"^(?P<speaker>[^：\n]{1,30})(?:（(?P<direction>[^）\n]+)）)?：(?P<text>.+)$")


class DevelopmentMapError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _invalid_line(line_number: int, reason: str) -> DevelopmentMapError:
    return DevelopmentMapError(
        "development_episode_map_jsonl_invalid",
        f"分集地图第 {line_number} 行格式无效：{reason}。每一行必须是一个 JSON 对象。",
    )


def parse_episode_map(content: str) -> list[dict[str, Any]]:
    matches = list(_MAP_BLOCK.finditer(content))
    if not matches:
        raise DevelopmentMapError(
            "development_episode_map_missing",
            "开发稿缺少分集地图。请插入一个 script-weaver-episode-map 代码块。",
        )
    if len(matches) > 1:
        raise DevelopmentMapError(
            "development_episode_map_multiple",
            "开发稿包含多个分集地图。请只保留一个 script-weaver-episode-map 代码块。",
        )
    entries: list[dict[str, Any]] = []
    for line_number, raw in enumerate(matches[0].group("body").splitlines(), 1):
        if not raw.strip():
            raise _invalid_line(line_number, "不能是空行")
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as error:
            raise _invalid_line(line_number, "不是有效 JSON") from error
        if not isinstance(item, dict) or set(item) - {"episode", "title", "story", "source_span"}:
            raise _invalid_line(line_number, "包含不支持的字段")
        if type(item.get("episode")) is not int or item["episode"] < 1:  # bool is not an episode number
            raise _invalid_line(line_number, "episode 必须是正整数")
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise _invalid_line(line_number, "title 不能为空")
        if not isinstance(item.get("story"), str) or not item["story"].strip():
            raise _invalid_line(line_number, "story 不能为空")
        item["title"] = item["title"].strip()
        item["story"] = item["story"].strip()
        item["fingerprint"] = hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        entries.append(item)
    if not entries or [entry["episode"] for entry in entries] != list(range(1, len(entries) + 1)):
        raise DevelopmentMapError(
            "development_episode_map_noncontinuous",
            "分集编号必须从 1 开始、连续且不重复。请按 1、2、3… 顺序填写。",
        )
    return entries


def verify_episode_spans(
    entries: list[dict[str, Any]], source_content: str
) -> list[dict[str, Any]]:
    source = source_content.encode("utf-8")
    previous_end = 0
    verified = []
    for entry in entries:
        span = entry.get("source_span")
        if not isinstance(span, dict) or set(span) != {"start", "end", "sha256"}:
            raise ValueError(f"EP{entry['episode']:03d} requires start/end/sha256 source_span")
        start, end, expected = span["start"], span["end"], span["sha256"]
        if type(start) is not int or type(end) is not int or start < previous_end or end <= start:
            raise ValueError("source byte spans must be ordered, non-overlapping, and non-empty")
        if end > len(source) or not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("source byte span is outside the source or has an invalid hash")
        chunk = source[start:end]
        if hashlib.sha256(chunk).hexdigest() != expected.lower():
            raise ValueError(f"EP{entry['episode']:03d} source span hash mismatch")
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("source byte span must align to UTF-8 boundaries") from error
        verified.append({**entry, "start": start, "end": end, "sha256": expected.lower(), "content": text})
        previous_end = end
    return verified


def parse_screenplay(content: str, expected_episode: int) -> dict[str, Any]:
    lines = content.splitlines()
    if not lines:
        raise ValueError("screenplay is empty")
    heading = _EPISODE_HEADING.fullmatch(lines[0])
    if not heading or int(heading["number"]) != expected_episode:
        raise ValueError(f"screenplay must start with '# EP{expected_episode:03d} title'")
    scene_starts = [index for index, line in enumerate(lines) if line.startswith("## ")]
    if not scene_starts:
        raise ValueError("screenplay requires at least one scene")
    if any(line.strip() for line in lines[1:scene_starts[0]]):
        raise ValueError("screenplay cannot contain content before the first scene")
    scenes: list[dict[str, Any]] = []
    for ordinal, start in enumerate(scene_starts, 1):
        match = _SCENE_HEADING.fullmatch(lines[start])
        if not match or int(match["episode"]) != expected_episode or int(match["scene"]) != ordinal:
            raise ValueError("scene headings must match the episode and be consecutive from SC001")
        end = scene_starts[ordinal] if ordinal < len(scene_starts) else len(lines)
        blocks = []
        for raw in lines[start + 1:end]:
            text = raw.strip()
            if not text:
                continue
            dialogue = _DIALOGUE.fullmatch(text)
            if dialogue:
                blocks.append({"type": "dialogue", **{key: value for key, value in dialogue.groupdict().items() if value}})
            else:
                blocks.append({"type": "action", "text": text})
        if not blocks:
            raise ValueError(f"SC{ordinal:03d} has no action or dialogue")
        scenes.append({
            "scene_number": f"EP{expected_episode:03d}-SC{ordinal:03d}",
            "heading": {
                "interior": match["interior"],
                "location": match["location"].strip(),
                "time": match["time"].strip(),
            },
            "blocks": blocks,
        })
    return {"episode": expected_episode, "title": heading["title"].strip(), "scenes": scenes}


__all__ = ["parse_episode_map", "parse_screenplay", "verify_episode_spans"]
