from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from script_weaver.runtime import python_version_message, require_supported_python


def test_unsupported_python_message_is_actionable() -> None:
    message = python_version_message((3, 10, 12))
    assert "Python 3.10.12" in message
    assert ">=3.11" in message
    assert "uv sync --python 3.11 --extra dev" in message
    with pytest.raises(SystemExit, match="Python 3.10.12"):
        require_supported_python((3, 10, 12))


def test_supported_python_passes() -> None:
    require_supported_python((3, 11, 0))


@pytest.mark.parametrize(
    ("entrypoint", "heavy_module"),
    [
        ("cli_main", "script_weaver.cli"),
        ("daemon_main", "script_weaver.daemon.server"),
        ("mcp_main", "script_weaver.mcp.workbench_server"),
    ],
)
def test_console_bootstrap_rejects_python_310_before_heavy_import(entrypoint, heavy_module) -> None:
    code = (
        "import sys; import script_weaver.runtime as runtime; "
        "runtime.sys.version_info=(3,10,9); "
        f"\ntry: runtime.{entrypoint}()"
        "\nexcept SystemExit as error: print(error); "
        f"print('heavy_imported=' + str({heavy_module!r} in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    )
    assert "当前使用 Python 3.10.9" in result.stdout
    assert "需要 Python >=3.11" in result.stdout
    assert "heavy_imported=False" in result.stdout
    assert "Traceback" not in result.stderr


def test_all_console_scripts_use_lightweight_runtime_bootstrap() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'script-weaver = "script_weaver.runtime:cli_main"' in pyproject
    assert 'script-weaverd = "script_weaver.runtime:daemon_main"' in pyproject
    assert 'script-weaver-mcp = "script_weaver.runtime:mcp_main"' in pyproject
