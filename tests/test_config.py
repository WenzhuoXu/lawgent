"""Configuration loading behavior."""

from __future__ import annotations

from pathlib import Path


def test_api_key_file_supplies_provider_keys(tmp_path, monkeypatch):
    from legal_helper import config as cfg_mod

    root = tmp_path
    key_dir = root / "legal_helper"
    key_dir.mkdir()
    (root / "config.yaml").write_text("provider: openai\n", encoding="utf-8")
    (key_dir / "api_key").write_text(
        'export OPENAI_API_KEY="file-openai-key"\n'
        'ANTHROPIC_API_KEY="file-anthropic-key"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(cfg_mod, "_PROJECT_ROOT", root)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg_mod._CACHED = None

    settings = cfg_mod.load_settings(refresh=True)

    assert settings.provider == "openai"
    assert settings.openai_api_key == "file-openai-key"
    assert settings.anthropic_api_key == "file-anthropic-key"
    assert settings.api_key_path == root / "legal_helper" / "api_key"


def test_process_env_overrides_api_key_file(tmp_path, monkeypatch):
    from legal_helper import config as cfg_mod

    root = tmp_path
    key_dir = root / "legal_helper"
    key_dir.mkdir()
    (root / "config.yaml").write_text("provider: openai\n", encoding="utf-8")
    (key_dir / "api_key").write_text(
        'OPENAI_API_KEY="file-openai-key"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(cfg_mod, "_PROJECT_ROOT", root)
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    cfg_mod._CACHED = None

    settings = cfg_mod.load_settings(refresh=True)

    assert settings.openai_api_key == "env-openai-key"


def test_primary_and_internal_fast_models_are_routed_by_provider(monkeypatch):
    from legal_helper.config import load_settings

    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("ANTHROPIC_FAST_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("OPENAI_FAST_MODEL", "gpt-5.4-mini")

    anthropic = load_settings(refresh=True).model_copy(update={"provider": "anthropic"})
    assert anthropic.model_for_provider() == "claude-sonnet-4-6"
    assert anthropic.model_for_provider(fast=True) == "claude-haiku-4-5"
    assert anthropic.high_effort_models_for_provider("anthropic") == ["claude-opus-5-5"]

    openai = anthropic.model_copy(update={"provider": "openai"})
    assert openai.model_for_provider() == "gpt-5.4"
    assert openai.model_for_provider(fast=True) == "gpt-5.4-mini"
    assert openai.high_effort_models_for_provider("openai") == ["gpt-6-sol"]


def test_max_concurrent_agents_is_configurable_and_clamped(monkeypatch):
    from legal_helper.config import load_settings

    monkeypatch.setenv("LEGAL_HELPER_MAX_CONCURRENT_AGENTS", "auto")
    anthropic = load_settings(refresh=True).model_copy(update={"provider": "anthropic"})
    openai = anthropic.model_copy(update={"provider": "openai"})
    assert anthropic.max_concurrent_agents == "auto"
    assert anthropic.effective_max_concurrent_agents() == 2
    assert openai.effective_max_concurrent_agents() == 4

    monkeypatch.setenv("LEGAL_HELPER_MAX_CONCURRENT_AGENTS", "3")
    settings = load_settings(refresh=True)
    assert settings.max_concurrent_agents == 3
    assert settings.effective_max_concurrent_agents() == 3

    monkeypatch.setenv("LEGAL_HELPER_MAX_CONCURRENT_AGENTS", "0")
    settings = load_settings(refresh=True)
    assert settings.max_concurrent_agents == 1
    assert settings.effective_max_concurrent_agents() == 1
