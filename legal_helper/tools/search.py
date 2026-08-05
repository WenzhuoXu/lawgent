"""Hosted-tool builders for provider-native web search/fetch and file search."""

from __future__ import annotations

from typing import Any

from ..config import Settings


def anthropic_hosted_search(settings: Settings) -> list[dict[str, Any]]:
    """Anthropic server tools: web_search + web_fetch."""
    out: list[dict[str, Any]] = []
    if settings.enable_web_search:
        out.append({"type": "web_search_20250305", "name": "web_search"})
    if settings.enable_web_fetch:
        out.append({"type": "web_fetch_20250910", "name": "web_fetch"})
    return out


def openai_hosted_search(settings: Settings) -> list[dict[str, Any]]:
    """OpenAI Responses hosted tools: web_search + optional file_search."""
    out: list[dict[str, Any]] = []
    if settings.enable_web_search:
        out.append({"type": "web_search"})
    if settings.openai_enable_file_search and settings.openai_file_search_vector_store_ids:
        out.append(
            {
                "type": "file_search",
                "vector_store_ids": settings.openai_file_search_vector_store_ids,
            }
        )
    return out


def hosted_search_tools_for_provider(settings: Settings) -> list[dict[str, Any]]:
    if settings.provider == "openai":
        return openai_hosted_search(settings)
    return anthropic_hosted_search(settings)
