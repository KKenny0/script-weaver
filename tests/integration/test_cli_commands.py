from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from click.testing import CliRunner

from script_weaver import cli


def test_readme_uses_installed_console_script_not_missing_module_entrypoint():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert not re.search(r"(?:uv run )?python\s+-m\s+script_weaver(?:\s|$)", readme)
    assert "uv run script-weaver generate" in readme
    assert "uv run script-weaver workbench doctor" in readme


def test_workbench_cli_routes_and_json_contract(monkeypatch, tmp_path):
    calls = []

    def fake_call(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/projects/project-1") and path == "/projects/project-1":
            return {"id": "project-1", "documents": [{"kind": "development", "versions": [], "draft": {"content": "draft"}}]}
        return {"ok": True}

    monkeypatch.setattr(cli, "_call", fake_call)
    runner = CliRunner()
    commands = [
        ["workbench", "doctor", "--json"],
        ["task", "list", "--status", "QUEUED"],
        ["task", "claim", "--task-id", "task-1", "--worker-label", "codex"],
        ["task", "context", "task-1", "--section", "source"],
        ["task", "fail", "--task-id", "task-1", "--run-id", "run-1", "--message", "failed"],
        ["search", "project-1", "--query", "future"],
    ]
    for command in commands:
        result = runner.invoke(cli.main, command)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["ok"] is True

    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"summary": "develop", "operations": [{"op": "document.version.create"}]}), encoding="utf-8")
    submitted = runner.invoke(cli.main, ["changeset", "submit", "task-1", "--run-id", "run-1", "--file", str(proposal)])
    assert submitted.exit_code == 0
    assert calls[-1][2]["run_id"] == "run-1"

    output = tmp_path / "export"
    exported = runner.invoke(cli.main, ["export", "project-1", "--output", str(output)])
    assert exported.exit_code == 0
    assert (output / "project.json").is_file()
    assert (output / "documents" / "01-development.md").read_text() == "draft"

    repeated = runner.invoke(cli.main, ["export", "project-1", "--output", str(output)])
    assert repeated.exit_code == 2
    assert (output / "project.json").is_file()


def test_export_cleans_partial_sibling_directory(monkeypatch, tmp_path):
    output = tmp_path / "export"
    project = {"id": "p", "documents": [{"kind": "development", "versions": [], "draft": {"content": "x"}}]}
    original = Path.write_text

    def fail_document(path, *args, **kwargs):
        if path.suffix == ".md":
            raise OSError("disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_document)
    with pytest.raises(OSError):
        cli._write_export(project, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".export-*"))


def test_non_json_http_error_has_stable_exit_code(monkeypatch):
    response = httpx.Response(403, text="forbidden", request=httpx.Request("POST", "http://localhost"))

    def fail(*_args, **_kwargs):
        raise httpx.HTTPStatusError("bad", request=response.request, response=response)

    monkeypatch.setattr(cli, "daemon_call", fail)
    result = CliRunner().invoke(cli.main, ["task", "list"])
    assert result.exit_code == 5
    assert json.loads(result.stderr)["error"] == "forbidden"
