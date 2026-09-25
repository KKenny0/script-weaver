"""Shared fixtures: load the web API module against an isolated data dir."""

import importlib.util
import sys
from pathlib import Path

import pytest

from script_weaver.core import config


def load_api_module():
    """Load a fresh instance of web/api/main.py (not through sys.modules)."""
    path = Path(__file__).parents[1] / "web" / "api" / "main.py"
    spec = importlib.util.spec_from_file_location("web_api_under_test", path)
    module = importlib.util.module_from_spec(spec)
    # dataclass decorators resolve their owning module via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api_factory(tmp_path, monkeypatch):
    """Return a loader that pins Settings to this test's tmp data dir."""
    monkeypatch.setattr(
        config,
        "_settings",
        config.Settings(data_dir=tmp_path, skills_custom_dir=tmp_path / "skills"),
    )
    return load_api_module
