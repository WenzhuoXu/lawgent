"""Pytest fixtures: dummy API keys + temp state/outputs/logs for isolation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Make the project root importable when pytest is invoked from anywhere.
_ROOT = Path(__file__).resolve().parent.parent
import sys

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """Redirect state/, outputs/, and logs/ to a tmp dir; force dummy API keys for mock tests."""
    monkeypatch.setenv("LEGAL_HELPER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LEGAL_HELPER_OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("LEGAL_HELPER_LOGS_DIR", str(tmp_path / "logs"))
    # Default-on dummy keys; live tests override before importing the provider.
    monkeypatch.setenv("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY", "test-anthropic-key"))
    monkeypatch.setenv("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", "test-openai-key"))
    # Default model for the whole test suite is gpt-6-sol (OpenAI). Tests that
    # need a specific provider/model still override these explicitly; a real env
    # var wins. (There is no bare "gpt-6" alias — always name the variant.)
    monkeypatch.setenv("MODEL_PROVIDER", os.getenv("MODEL_PROVIDER", "openai"))
    monkeypatch.setenv("OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-6-sol"))
    # Refresh the settings cache between tests.
    from legal_helper import config as cfg_mod

    cfg_mod._CACHED = None
    yield
    cfg_mod._CACHED = None
