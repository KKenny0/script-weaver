"""PR1: local network, MCP path-encoding and request-body boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from script_weaver.daemon.server import MAX_BODY_BYTES, create_app
from script_weaver.daemon.server import agent_token_path, runtime_token_path
from script_weaver.daemon import server as daemon_server
from script_weaver.infrastructure.sqlite import default_data_dir
from script_weaver.mcp import workbench_server


@pytest.fixture
def work_dir():
    path = Path(".test-tmp") / uuid4().hex
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def app_and_db(work_dir: Path):
    db_path = work_dir / "workbench.sqlite3"
    app = create_app(
        database_path=db_path, media_root=work_dir / "media",
        token="test-token", agent_token="agent-token",
    )
    return app, db_path


def drive_asgi(app, method: str, path: str, chunks: list[bytes], headers: list[tuple[bytes, bytes]]) -> tuple[int, bytes]:
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    start: dict[str, Any] = {}
    body = bytearray()

    async def receive():
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return start["status"], bytes(body)


def project_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    finally:
        conn.close()


def test_oversized_stream_without_content_length_returns_413_and_writes_nothing(app_and_db):
    app, db_path = app_and_db
    status, _ = drive_asgi(
        app, "POST", "/api/projects",
        [b"x" * (MAX_BODY_BYTES + 1)],
        [(b"content-type", b"application/json")],
    )
    assert status == 413
    assert project_count(db_path) == 0


def test_oversized_chunked_json_returns_413(app_and_db):
    app, db_path = app_and_db
    payload = b'{"title":"' + b"a" * (3 * 1024 * 1024) + b'"}'
    status, _ = drive_asgi(
        app, "POST", "/api/projects",
        [payload[:1024 * 1024], payload[1024 * 1024:2 * 1024 * 1024], payload[2 * 1024 * 1024:]],
        [(b"content-type", b"application/json")],
    )
    assert status == 413
    assert project_count(db_path) == 0


@pytest.mark.parametrize("header", [b"abc", b"-5"])
def test_forged_content_length_never_500_and_cannot_bypass_byte_count(app_and_db, header):
    app, db_path = app_and_db
    status, _ = drive_asgi(
        app, "POST", "/api/projects",
        [b'{"title":"ok"}'],
        [(b"content-type", b"application/json"), (b"content-length", header)],
    )
    assert status in {400, 413}
    assert project_count(db_path) == 0


def test_small_request_still_succeeds(app_and_db):
    app, db_path = app_and_db
    status, body = drive_asgi(
        app, "POST", "/api/projects",
        [b'{"title":"mist"}'],
        [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer test-token"),
            (b"content-length", b"15"),
        ],
    )
    assert status == 201, body
    assert json.loads(body)["title"] == "mist"
    assert project_count(db_path) == 1


def test_agent_token_can_read_but_creator_mutation_is_forbidden(app_and_db):
    app, db_path = app_and_db
    agent_headers = [(b"authorization", b"Bearer agent-token")]
    status, _ = drive_asgi(app, "GET", "/api/projects", [], agent_headers)
    assert status == 200
    status, body = drive_asgi(
        app, "POST", "/api/projects", [b'{"title":"forbidden"}'],
        [(b"content-type", b"application/json"), *agent_headers],
    )
    assert status == 403
    assert json.loads(body)["detail"] == "creator capability required"
    assert project_count(db_path) == 0

    status, _ = drive_asgi(
        app, "POST", "/api/projects", [b'{"title":"creator"}'],
        [(b"content-type", b"application/json"), (b"authorization", b"Bearer test-token")],
    )
    assert status == 201
    assert project_count(db_path) == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/projects/p/archive", {"expected_revision": 0}),
        ("/api/changesets/c/apply", {"fingerprint": "0" * 64}),
        ("/api/changesets/c/reject", {}),
        ("/api/documents/d/versions/v/decision", {"expected_document_revision": 0, "action": "accept"}),
        ("/api/documents/d/project", {"expected_document_revision": 0}),
        ("/api/generation-jobs/j/confirm", {"fingerprint": "0" * 64}),
        ("/api/generation-jobs/j/run", {"confirmation_token": "token"}),
    ],
)
def test_agent_token_cannot_cross_creator_capability_routes(app_and_db, path, body):
    app, _ = app_and_db
    payload = json.dumps(body).encode()
    status, _ = drive_asgi(
        app, "POST", path, [payload],
        [(b"content-type", b"application/json"), (b"authorization", b"Bearer agent-token")],
    )
    assert status == 403


def test_get_does_not_read_or_buffer_body(app_and_db):
    app, _ = app_and_db
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", b"Bearer test-token")],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    start: dict[str, Any] = {}
    reads: list[bytes] = []

    async def receive():
        reads.append(b"")

        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)

    asyncio.run(app(scope, receive, send))
    assert start["status"] == 200
    assert reads == []


class FakeResponse:
    def __init__(self) -> None:
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"ok": True}


def patch_send(monkeypatch, captured: list[httpx_RequestLike]) -> None:
    def fake_send(request):
        captured.append(request)
        return FakeResponse()

    monkeypatch.setattr(workbench_server, "_send", fake_send)


httpx_RequestLike = Any


@pytest.fixture
def mcp_env(work_dir: Path, monkeypatch):
    token_file = work_dir / "agent.token"
    token_file.write_text("mcp-token", encoding="utf-8")
    monkeypatch.setenv("SCRIPT_WEAVER_AGENT_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("SCRIPT_WEAVER_DAEMON_URL", raising=False)


def test_removed_multistep_changeset_tools_are_not_exposed(mcp_env, monkeypatch):
    captured: list[Any] = []
    patch_send(monkeypatch, captured)
    assert not hasattr(workbench_server, "submit_changeset")
    assert not hasattr(workbench_server, "append_changeset_operation")
    assert not hasattr(workbench_server, "start_task")


def test_hostile_ids_stay_encoded_in_every_path_call_site(mcp_env, monkeypatch):
    captured: list[Any] = []
    patch_send(monkeypatch, captured)
    hostile = "a/b?c#d"
    workbench_server.get_task_snapshot(hostile)
    workbench_server.get_project(hostile)
    workbench_server.get_episode(hostile)
    workbench_server.get_segment(hostile)
    workbench_server.get_shot(hostile)
    workbench_server.list_assets(hostile)
    workbench_server.get_asset_version(hostile)
    workbench_server.search_project(hostile, query="q")
    workbench_server.get_task_context(hostile)
    workbench_server.claim_task(hostile)
    workbench_server.fail_task(hostile, "run", "message")
    workbench_server.submit_proposal(hostile, "run", "summary", [])
    workbench_server.render_shot_preview(hostile)
    workbench_server.render_segment_contact_sheet(hostile)
    workbench_server.compare_shot_versions(hostile)
    workbench_server.render_asset_board(hostile)
    workbench_server.get_deep_link(hostile, hostile)
    workbench_server.get_generation_job(hostile)
    workbench_server.get_active_context(hostile)
    assert captured, "expected every tool call to issue a request"
    for request in captured:
        raw_path = request.url.raw_path
        assert b"a/b?c#d" not in raw_path, raw_path
        assert b"a%2Fb%3Fc%23d" in raw_path or b"a%2Fb%3Fc%23d" in request.url.raw_query or b"session_id=a%2Fb%3Fc%23d" in request.url.raw_query, raw_path


def test_traversal_id_cannot_escape_api_prefix(mcp_env, monkeypatch):
    captured: list[Any] = []
    patch_send(monkeypatch, captured)
    workbench_server.get_shot("../../../reject")
    raw_path = captured[0].url.raw_path
    assert raw_path == b"/api/shots/..%2F..%2F..%2Freject"


def test_non_loopback_daemon_url_is_refused(mcp_env, monkeypatch):
    monkeypatch.setenv("SCRIPT_WEAVER_DAEMON_URL", "http://192.168.1.10:8000")
    with pytest.raises(RuntimeError, match="loopback"):
        workbench_server.get_project("p1")


def test_web_scripts_bind_loopback_and_proxy_is_hardened():
    package = json.loads(Path("web/package.json").read_text(encoding="utf-8"))
    assert "--hostname 127.0.0.1" in package["scripts"]["dev"]
    assert "--hostname 127.0.0.1" in package["scripts"]["start"]
    proxy = Path("web/src/app/api/[...path]/route.ts").read_text(encoding="utf-8")
    assert "LOOPBACK_HOSTNAMES" in proxy
    assert "arrayBuffer" not in proxy
    assert 'headers.delete("authorization")' in proxy
    assert 'headers.delete("cookie")' in proxy
    assert 'redirect: "error"' in proxy


def test_data_and_token_paths_follow_explicit_localappdata_xdg_default_priority(tmp_path, monkeypatch):
    monkeypatch.delenv("SCRIPT_WEAVER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("SCRIPT_WEAVER_AGENT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert default_data_dir() == Path.home() / ".local" / "share" / "script-weaver"
    assert runtime_token_path() == default_data_dir() / "runtime.token"

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert default_data_dir() == xdg / "script-weaver"
    assert runtime_token_path() == xdg / "script-weaver" / "runtime.token"

    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert default_data_dir() == local / "script-weaver"
    assert runtime_token_path() == local / "script-weaver" / "runtime.token"

    explicit = tmp_path / "creator.token"
    explicit_agent = tmp_path / "proposal.token"
    monkeypatch.setenv("SCRIPT_WEAVER_TOKEN_FILE", str(explicit))
    monkeypatch.setenv("SCRIPT_WEAVER_AGENT_TOKEN_FILE", str(explicit_agent))
    assert runtime_token_path() == explicit
    assert agent_token_path() == explicit_agent


def test_importing_daemon_server_twice_creates_no_database_or_tokens(tmp_path):
    data_root = tmp_path / "isolated-data"
    environment = {
        **os.environ,
        "XDG_DATA_HOME": str(data_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("LOCALAPPDATA", "SCRIPT_WEAVER_TOKEN_FILE", "SCRIPT_WEAVER_AGENT_TOKEN_FILE"):
        environment.pop(name, None)
    command = [sys.executable, "-c", "import script_weaver.daemon.server"]
    for _ in range(2):
        subprocess.run(command, env=environment, check=True, capture_output=True, text=True)
        assert not (data_root / "script-weaver").exists()


def test_daemon_main_creates_one_app_and_rotates_each_token_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("SCRIPT_WEAVER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("SCRIPT_WEAVER_AGENT_TOKEN_FILE", raising=False)
    calls: list[Path] = []
    original_rotate = daemon_server.rotate_token

    def tracked_rotate(path: Path) -> str:
        calls.append(path)
        return original_rotate(path)

    captured = []
    monkeypatch.setattr(daemon_server, "rotate_token", tracked_rotate)
    monkeypatch.setattr("uvicorn.run", lambda app, **_options: captured.append(app))
    daemon_server.main()

    expected_root = tmp_path / "data" / "script-weaver"
    assert calls == [expected_root / "runtime.token", expected_root / "agent.token"]
    assert len(captured) == 1
    assert (expected_root / "workbench.sqlite3").exists()
    captured[0].state.db.close()
