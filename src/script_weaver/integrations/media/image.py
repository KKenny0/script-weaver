"""Image adapters write only daemon staging files."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

_ONE_PIXEL_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class FakeImageAdapter:
    def generate(self, spec: dict, staging: Path) -> list[Path]:
        paths = []
        for index in range(spec["count"]):
            path = staging / f"{index}.png"
            path.write_bytes(_ONE_PIXEL_PNG)
            paths.append(path)
        return paths


class GptImage2Adapter:
    def generate(self, spec: dict, staging: Path) -> list[Path]:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        parameters = spec.get("parameters", {})
        body = {
            "model": spec["model"], "prompt": spec["prompt"], "n": spec["count"],
            "size": parameters.get("size", "1024x1024"),
            "quality": parameters.get("quality", "auto"),
            "output_format": "png",
        }
        response = httpx.post("https://api.openai.com/v1/images/generations", headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=300)
        response.raise_for_status()
        paths = []
        for index, image in enumerate(response.json()["data"]):
            path = staging / f"{index}.png"
            path.write_bytes(base64.b64decode(image["b64_json"], validate=True))
            paths.append(path)
        return paths
