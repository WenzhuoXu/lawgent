"""Chat title and rolling-memory helpers."""

from __future__ import annotations

from typing import Iterable

from .chat_models import ChatMessage
from .config import Settings
from .providers import build_provider


def fallback_title(text: str) -> str:
    clean = " ".join(text.strip().split())
    if not clean:
        return "New chat"
    return clean[:48] + ("..." if len(clean) > 48 else "")


def _cheap_title_settings(settings: Settings) -> Settings:
    updates = {"max_tokens": min(settings.max_tokens, 256)}
    if settings.provider == "openai":
        updates["openai_reasoning_effort"] = "none"
    return settings.model_copy(update=updates)


def _clean_title(raw: str) -> str:
    title = " ".join(raw.strip().split())
    title = title.strip("\"'`“”‘’")
    for prefix in ("Title:", "Chat title:"):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix) :].strip()
    title = title.splitlines()[0].strip("\"'`“”‘’ ")
    if not title:
        return "New chat"
    return title[:60].rstrip(" .,-:;")


def summarize_title_with_fast_model(settings: Settings, messages: list[ChatMessage]) -> str:
    """Best-effort cheap title generation; keeps the title untouched on provider failure."""
    try:
        provider = build_provider(_cheap_title_settings(settings), fast=True)
        transcript = "\n\n".join(f"{m.role}: {m.content[:1200]}" for m in messages[-6:] if m.content.strip())
        if not transcript.strip():
            return "New chat"
        prompt = (
            "Create a concise, editable chat title for this conversation. "
            "Use 3 to 8 words, preserve the user's language when practical, "
            "and return only the title without quotes or punctuation.\n\n"
            f"Conversation:\n{transcript}"
        )
        result = provider.run(
            system="You write short chat titles. Return only one title.",
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_iterations=1,
        )
        return _clean_title(result.text)
    except Exception:
        return "New chat"


def fallback_summary(messages: Iterable[ChatMessage], previous: str = "") -> str:
    facts: list[str] = []
    if previous.strip():
        facts.append(previous.strip())
    for msg in list(messages)[-8:]:
        content = " ".join(msg.content.split())
        if not content:
            continue
        facts.append(f"{msg.role}: {content[:500]}")
    joined = "\n".join(facts)
    return joined[-4000:]


def summarize_with_fast_model(settings: Settings, messages: list[ChatMessage], previous: str = "") -> str:
    """Best-effort cheap summarization; falls back locally on any provider issue."""
    try:
        provider = build_provider(settings, fast=True)
        transcript = "\n\n".join(f"{m.role}: {m.content[:2000]}" for m in messages[-12:])
        prompt = (
            "Update this concise legal-chat memory summary. Preserve user goals, "
            "facts, prior conclusions, artifacts requested/created, and unresolved follow-ups. "
            "Do not add new legal analysis. Write the memory summary in whichever language "
            "best preserves fidelity — follow the dominant language of the transcript, and "
            "keep Chinese legal terms and citations as written rather than translating them.\n\n"
            f"Existing summary:\n{previous or '(none)'}\n\nRecent transcript:\n{transcript}"
        )
        result = provider.run(
            system="You write concise memory summaries for a legal AI assistant, in whichever language best fits the conversation.",
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_iterations=1,
        )
        if result.text.strip():
            return result.text.strip()[:6000]
    except Exception:
        pass
    return fallback_summary(messages, previous)
