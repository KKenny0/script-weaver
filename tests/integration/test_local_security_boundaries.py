"""PR1: local network, MCP path-encoding and request-body boundaries."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from script_weaver.daemon.server import MAX_BODY_BYTES, create_app
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
    app = create_app(database_path=db_path, media_root=work_dir / "media", token="test-token")
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
    token_file = work_dir / "runtime.token"
    token_file.write_text("mcp-token", encoding="utf-8")
    monkeypatch.setenv("SCRIPT_WEAVER_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("SCRIPT_WEAVER_DAEMON_URL", raising=False)


def test_hostile_changeset_id_cannot_reach_reject_endpoint(mcp_env, monkeypatch):
    captured: list[Any] = []
    patch_send(monkeypatch, captured)
    workbench_server.submit_changeset("id/reject?x=")
    raw_path = captured[0].url.raw_path
    assert raw_path == b"/api/changesets/id%2Freject%3Fx%3D/submit"


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
    workbench_server.append_changeset_operation(hostile, {})
    workbench_server.validate_changeset(hostile)
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
