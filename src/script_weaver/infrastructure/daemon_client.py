"""Small authenticated loopback client shared by CLI and MCP."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import httpx

from .sqlite import default_data_dir

LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def base_url() -> httpx.URL:
    parsed = httpx.URL(os.getenv("SCRIPT_WEAVER_DAEMON_URL", "http://127.0.0.1:8000"))
    if (parsed.host or "").strip("[]") not in LOOPBACK_HOSTNAMES:
        raise RuntimeError("SCRIPT_WEAVER_DAEMON_URL must point at a loopback address")
    return parsed


def call(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    sender: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Any:
    token_path = Path(
        os.getenv(
            "SCRIPT_WEAVER_AGENT_TOKEN_FILE",
            str(default_data_dir() / "agent.token"),
        )
    )
    token = token_path.read_text(encoding="utf-8").strip()
    request = httpx.Request(
        method,
        base_url().join(httpx.URL(f"/api{path}")),
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    if sender is None:
        with httpx.Client(timeout=30) as client:
            response = client.send(request)
    else:
        response = sender(request)
    response.raise_for_status()
    return response.json()
