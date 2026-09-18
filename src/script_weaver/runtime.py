"""Single startup compatibility check shared by executable entrypoints."""

from __future__ import annotations

import sys
from collections.abc import Sequence


MINIMUM_PYTHON = (3, 11)


def python_version_message(version: Sequence[int] | None = None) -> str:
    current = tuple(version or sys.version_info[:3])
    rendered = ".".join(str(part) for part in current[:3])
    return (
        f"Script Weaver 当前使用 Python {rendered}；需要 Python >=3.11。"
        "请运行：uv sync --python 3.11 --extra dev"
    )


def require_supported_python(version: Sequence[int] | None = None) -> None:
    current = tuple(version or sys.version_info[:3])
    if current[:2] < MINIMUM_PYTHON:
        raise SystemExit(python_version_message(current))


def cli_main() -> None:
    require_supported_python()
    from script_weaver.cli import main

    main()


def daemon_main() -> None:
    require_supported_python()
    from script_weaver.daemon.server import main

    main()


def mcp_main() -> None:
    require_supported_python()
    from script_weaver.mcp.workbench_server import main

    main()


__all__ = [
    "MINIMUM_PYTHON",
    "cli_main",
    "daemon_main",
    "mcp_main",
    "python_version_message",
    "require_supported_python",
]
