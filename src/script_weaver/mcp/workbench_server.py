"""Read/propose-only STDIO MCP bridge to script-weaverd."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

import httpx
from mcp.server.mcpserver import MCPServer

from script_weaver.infrastructure.daemon_client import base_url, call as daemon_call

TOOL_NAMES = (
    "workbench.get_active_context", "workbench.list_tasks", "workbench.claim_task",
    "workbench.get_task_context", "workbench.fail_task", "workbench.submit_proposal",
    "workbench.get_task_snapshot",
    "workbench.get_project", "workbench.get_episode", "workbench.get_segment",
    "workbench.get_shot", "workbench.list_assets", "workbench.get_asset_version",
    "workbench.search_project", "workbench.render_shot_preview",
    "workbench.render_segment_contact_sheet", "workbench.compare_shot_versions",
    "workbench.render_asset_board", "workbench.get_deep_link",
    "workbench.prepare_generation_job", "workbench.get_generation_job",
)

server = MCPServer("script-weaver-workbench", instructions="Read project facts and submit ChangeSets. Applying changes and confirming/running generation are intentionally unavailable.")


def seg(value: Any) -> str:
    """Encode one path segment so IDs containing '/', '?', '#' or '%' stay literal."""
    return quote(str(value), safe="")


def daemon_base_url() -> httpx.URL:
    return base_url()


def _send(request: httpx.Request) -> httpx.Response:
    with httpx.Client(timeout=30) as client:
        return client.send(request)


def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    return daemon_call(method, path, body, _send)


@server.tool(name="workbench.get_active_context")
def get_active_context(session_id: str | None = None) -> dict[str, Any]:
    query = f"?{urlencode({'session_id': session_id})}" if session_id else ""
    return call("GET", f"/active-context{query}")


@server.tool(name="workbench.list_tasks")
def list_tasks(project_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    query = {key: value for key, value in {"project_id": project_id, "status": status}.items() if value}
    suffix = f"?{urlencode(query)}" if query else ""
    return call("GET", f"/tasks{suffix}")


@server.tool(name="workbench.claim_task")
def claim_task(task_id: str, worker_label: str = "codex-mcp") -> dict[str, Any]:
    return call("POST", f"/tasks/{seg(task_id)}/claim", {"worker_label": worker_label})


@server.tool(name="workbench.get_task_context")
def get_task_context(task_id: str, section: str = "all", cursor: int = 0) -> dict[str, Any]:
    return call("GET", f"/tasks/{seg(task_id)}/context?{urlencode({'section': section, 'cursor': cursor})}")


@server.tool(name="workbench.fail_task")
def fail_task(task_id: str, run_id: str, message: str) -> dict[str, Any]:
    return call("POST", f"/tasks/{seg(task_id)}/fail", {"run_id": run_id, "message": message})


@server.tool(name="workbench.submit_proposal")
def submit_proposal(task_id: str, run_id: str, summary: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    return call("POST", f"/tasks/{seg(task_id)}/changesets/submit", {"run_id": run_id, "summary": summary, "operations": operations})


@server.tool(name="workbench.get_task_snapshot")
def get_task_snapshot(task_id: str) -> dict[str, Any]: return call("GET", f"/tasks/{seg(task_id)}/snapshot")


@server.tool(name="workbench.get_project")
def get_project(project_id: str) -> dict[str, Any]: return call("GET", f"/projects/{seg(project_id)}")


@server.tool(name="workbench.get_episode")
def get_episode(episode_id: str) -> dict[str, Any]: return call("GET", f"/episodes/{seg(episode_id)}")


@server.tool(name="workbench.get_segment")
def get_segment(segment_id: str) -> dict[str, Any]: return call("GET", f"/segments/{seg(segment_id)}")


@server.tool(name="workbench.get_shot")
def get_shot(shot_id: str) -> dict[str, Any]: return call("GET", f"/shots/{seg(shot_id)}")


@server.tool(name="workbench.list_assets")
def list_assets(project_id: str) -> list[dict[str, Any]]: return call("GET", f"/projects/{seg(project_id)}/assets")


@server.tool(name="workbench.get_asset_version")
def get_asset_version(version_id: str) -> dict[str, Any]: return call("GET", f"/asset-versions/{seg(version_id)}")


@server.tool(name="workbench.search_project")
def search_project(project_id: str, query: str, limit: int = 50) -> list[dict[str, Any]]: return call("GET", f"/projects/{seg(project_id)}/search?{urlencode({'q': query, 'limit': min(limit, 100)})}")


@server.tool(name="workbench.render_shot_preview")
def render_shot_preview(shot_id: str) -> dict[str, Any]: return call("GET", f"/shots/{seg(shot_id)}/preview")


@server.tool(name="workbench.render_segment_contact_sheet")
def render_segment_contact_sheet(segment_id: str) -> dict[str, Any]: return call("GET", f"/segments/{seg(segment_id)}/contact-sheet")


@server.tool(name="workbench.compare_shot_versions")
def compare_shot_versions(shot_id: str) -> dict[str, Any]: return call("GET", f"/shots/{seg(shot_id)}/compare")


@server.tool(name="workbench.render_asset_board")
def render_asset_board(project_id: str) -> dict[str, Any]: return call("GET", f"/projects/{seg(project_id)}/asset-board")


@server.tool(name="workbench.get_deep_link")
def get_deep_link(entity_type: str, entity_id: str) -> dict[str, Any]: return call("GET", f"/deep-link/{seg(entity_type)}/{seg(entity_id)}")


@server.tool(name="workbench.prepare_generation_job")
def prepare_generation_job(project_id: str, owner_type: str, owner_id: str, prompt: str, count: int = 1, model: str = "gpt-image-2", parameters: dict[str, Any] | None = None, reference_asset_version_ids: list[str] | None = None, adapter: str = "fake", output_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    return call("POST", "/generation-jobs", {"project_id": project_id, "owner_type": owner_type, "owner_id": owner_id, "prompt": prompt, "count": count, "model": model, "parameters": parameters or {}, "reference_asset_version_ids": reference_asset_version_ids or [], "adapter": adapter, "output_spec": output_spec or {"mime": "image/png"}})


@server.tool(name="workbench.get_generation_job")
def get_generation_job(job_id: str) -> dict[str, Any]: return call("GET", f"/generation-jobs/{seg(job_id)}")


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
