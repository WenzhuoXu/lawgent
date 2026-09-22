"""A retired model must not survive in a persisted chat selection.

A chat freezes its model at creation (``ChatStore.default_settings``) and
re-applies it on every later run, so dropping a model from
``OPENAI_HIGH_EFFORT_MODELS`` only ever affected *new* chats. gpt-5.5 kept
serving turns on pre-retirement chats for 18 requests after its 2026-09-11
retirement, at 2.5x the price of the model the user had actually selected.
These tests pin the three places the live allowlist is now enforced: the
resolver, the store (write path + one-time migration), and the run path.
"""

from __future__ import annotations

from legal_helper.chat_models import ChatSettings
from legal_helper.chat_store import ChatStore
from legal_helper.config import current_settings


def test_retired_model_resolves_to_the_configured_default():
    settings = current_settings()
    assert settings.openai_model == "gpt-6-sol"
    for retired in ("gpt-5.5", "gpt-5.6-terra", "gpt-5.6-sol"):
        assert settings.resolve_model_for_provider(retired, "openai") == "gpt-6-sol"


def test_every_retired_model_is_explained_and_no_longer_offered():
    from legal_helper.config import RETIRED_MODELS

    settings = current_settings()
    offered = {
        *settings.offered_models_for_provider("openai"),
        *settings.offered_models_for_provider("anthropic"),
    }
    assert not offered & set(RETIRED_MODELS)


def test_offered_models_are_left_alone():
    """An offered model is the user's selection and must survive untouched."""
    settings = current_settings()
    for model in settings.offered_models_for_provider("openai"):
        assert settings.resolve_model_for_provider(model, "openai") == model


def test_fast_tier_is_not_promoted_to_the_high_effort_default():
    """The cheap tier is a deliberate choice, not a stale value to be fixed."""
    settings = current_settings()
    assert settings.resolve_model_for_provider("gpt-6-luna", "openai") == "gpt-6-luna"
    assert settings.resolve_model_for_provider("gpt-5.6-luna", "openai") == "gpt-6-sol"


def test_pinned_config_model_counts_as_offered(monkeypatch):
    """An explicit config pin is by definition the user's selection."""
    from legal_helper import config as cfg

    cfg._CACHED = None
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6-astra")
    settings = cfg.current_settings()
    assert settings.resolve_model_for_provider("gpt-6-astra", "openai") == "gpt-6-astra"
    cfg._CACHED = None


def test_cross_provider_and_empty_selections_fall_back():
    settings = current_settings()
    assert settings.resolve_model_for_provider("claude-opus-5-5", "openai") == "gpt-6-sol"
    assert settings.resolve_model_for_provider("", "openai") == "gpt-6-sol"
    assert settings.resolve_model_for_provider(None, "openai") == "gpt-6-sol"


def test_anthropic_selections_resolve_against_their_own_tier():
    settings = current_settings()
    assert settings.anthropic_model == "claude-opus-5-5"
    assert settings.anthropic_fast_model == "claude-sonnet-5"
    assert (
        settings.resolve_model_for_provider("claude-sonnet-5", "anthropic") == "claude-sonnet-5"
    )
    for retired in ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-haiku-4-5"):
        assert settings.resolve_model_for_provider(retired, "anthropic") == "claude-opus-5-5"
    assert settings.resolve_model_for_provider("gpt-5.5", "anthropic") == settings.anthropic_model


def test_store_normalizes_an_incoming_retired_selection(tmp_path):
    store = ChatStore(db_path=tmp_path / "chats.sqlite3")
    chat = store.create_chat(
        title="stale client",
        settings=ChatSettings(provider="openai", model="gpt-5.5"),
    )
    assert chat.settings.model == "gpt-6-sol"

    # A client that cached the old value and PATCHes it back must not re-persist it.
    patched = store.update_chat(
        chat.id, settings=ChatSettings(provider="openai", model="gpt-5.5")
    )
    assert patched.settings.model == "gpt-6-sol"
    assert store.get_chat(chat.id).settings.model == "gpt-6-sol"


def test_existing_rows_are_migrated_when_the_store_opens(tmp_path):
    """The rows that predate the retirement are rewritten once, at open."""
    import json
    import sqlite3

    db = tmp_path / "chats.sqlite3"
    store = ChatStore(db_path=db)
    keep = store.create_chat(settings=ChatSettings(provider="openai", model="gpt-6-luna"))
    stale = store.create_chat(settings=ChatSettings(provider="openai", model="gpt-6-sol"))

    # Write a retired model straight past the normalizer, as a pre-retirement
    # release would have left it on disk.
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE chats SET settings_json = ? WHERE id = ?",
        (json.dumps({"provider": "openai", "model": "gpt-5.6-terra"}), stale.id),
    )
    con.commit()
    con.close()

    reopened = ChatStore(db_path=db)
    assert reopened.get_chat(stale.id).settings.model == "gpt-6-sol"
    assert reopened.get_chat(keep.id).settings.model == "gpt-6-luna"


def test_run_settings_resolve_a_retired_chat_selection():
    """Last line of defence: the model that serves the turn, not the stored one."""
    from legal_helper.server import _settings_for_chat

    run_settings = _settings_for_chat(ChatSettings(provider="openai", model="gpt-5.6-terra"))
    assert run_settings.openai_model == "gpt-6-sol"
    assert run_settings.model_for_provider() == "gpt-6-sol"

    run_settings = _settings_for_chat(ChatSettings(provider="anthropic", model="claude-opus-5"))
    assert run_settings.anthropic_model == "claude-opus-5-5"
