"""Chat title and rolling-memory helpers.

The rolling summary used to be maintained by **rewrite**: hand the model the
previous summary plus the recent transcript and keep whatever came back. That
is the pattern ACE (Agentic Context Engineering, ICLR 2026) names *context
collapse* — each rewrite silently erodes detail, so a long chat's early facts
decay pass by pass even though the summary never looks wrong.

This module maintains it by **delta** instead. The model is asked only for what
changed, as ``+ SECTION | text`` / ``- SECTION | text`` lines against a
sectioned document; a deterministic merge applies them. Bullets accumulate and
are dropped only when the model says so or a section hits its cap — so loss is
bounded and explicit rather than emergent. If a delta will not parse, the old
rewrite path still runs, so a bad model turn degrades rather than breaks.
"""

from __future__ import annotations

import re
from typing import Iterable

from .chat_models import ChatMessage
from .config import Settings
from .providers import build_provider


# Stable section keys. The rolling summary is internal context, never shown to
# the user, so the headers stay ASCII while bullet text keeps whatever language
# the conversation uses (CLAUDE.md language rule).
SUMMARY_SECTIONS: tuple[str, ...] = (
    "Goals",
    "Facts",
    "Conclusions",
    "Artifacts",
    "Open",
)
_DEFAULT_SECTION = "Facts"
_MAX_BULLETS_PER_SECTION = 40
_MAX_SUMMARY_CHARS = 12_000

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*\S)\s*$")
_DELTA_RE = re.compile(r"^\s*([+-])\s*([A-Za-z]+)\s*\|\s*(.+\S)\s*$")


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _canonical_section(name: str) -> str:
    target = name.strip().casefold()
    for section in SUMMARY_SECTIONS:
        if section.casefold() == target:
            return section
    return _DEFAULT_SECTION


def parse_summary(text: str) -> dict[str, list[str]]:
    """Parse a rendered summary back into ``{section: [bullets]}``.

    Free-form text that predates the sectioned format (or came from the
    fallback path) lands in ``Facts`` so nothing is lost on the first upgrade.
    """
    sections: dict[str, list[str]] = {s: [] for s in SUMMARY_SECTIONS}
    current: str | None = None
    loose: list[str] = []
    for line in (text or "").splitlines():
        header = _SECTION_RE.match(line)
        if header:
            current = _canonical_section(header.group(1))
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            (sections[current] if current else loose).append(bullet.group(1))
            continue
        stripped = line.strip()
        if stripped and current is None:
            loose.append(stripped)
    for item in loose:
        if item not in sections[_DEFAULT_SECTION]:
            sections[_DEFAULT_SECTION].append(item)
    return sections


def render_summary(sections: dict[str, list[str]]) -> str:
    """Render ``{section: [bullets]}`` back to markdown, empty sections omitted."""
    parts: list[str] = []
    for name in SUMMARY_SECTIONS:
        bullets = sections.get(name) or []
        if not bullets:
            continue
        parts.append(f"## {name}")
        parts.extend(f"- {b}" for b in bullets)
    return "\n".join(parts)[:_MAX_SUMMARY_CHARS]


def parse_delta(raw: str) -> list[tuple[str, str, str]]:
    """Parse ``+ Section | text`` / ``- Section | text`` lines into operations."""
    ops: list[tuple[str, str, str]] = []
    for line in (raw or "").splitlines():
        m = _DELTA_RE.match(line)
        if not m:
            continue
        op, section, body = m.group(1), _canonical_section(m.group(2)), m.group(3).strip()
        if body:
            ops.append((op, section, body))
    return ops


def apply_delta(sections: dict[str, list[str]], ops: Iterable[tuple[str, str, str]]) -> dict[str, list[str]]:
    """Apply delta operations to a parsed summary.

    ``+`` appends unless a near-duplicate already exists; ``-`` removes bullets
    whose text starts with the given prefix. Per-section caps evict oldest-first
    — the only implicit loss, and it is bounded and visible here rather than
    delegated to whatever the model chose to retain.
    """
    out = {name: list(sections.get(name) or []) for name in SUMMARY_SECTIONS}
    for op, section, body in ops:
        bucket = out.setdefault(section, [])
        if op == "+":
            if not any(_norm(b) == _norm(body) for b in bucket):
                bucket.append(body)
        else:
            prefix = _norm(body)
            out[section] = [b for b in bucket if not _norm(b).startswith(prefix)]
    for name, bullets in out.items():
        if len(bullets) > _MAX_BULLETS_PER_SECTION:
            out[name] = bullets[-_MAX_BULLETS_PER_SECTION:]
    return out


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


_DELTA_SYSTEM = (
    "You maintain a legal assistant's rolling memory as an append-only playbook. "
    "You emit only deltas — never a rewritten summary."
)


def _delta_prompt(previous: str, transcript: str) -> str:
    sections = ", ".join(SUMMARY_SECTIONS)
    return (
        "Below is a legal chat's memory playbook and the newest part of the "
        "transcript. Emit ONLY the changes, one per line, in exactly this form:\n"
        "  + Section | new fact to remember\n"
        "  - Section | start of an existing bullet that is now wrong or resolved\n"
        f"Valid sections: {sections}. Goals = what the user wants; Facts = case "
        "facts, parties, dates, jurisdictions; Conclusions = legal conclusions "
        "already reached; Artifacts = documents requested or produced; Open = "
        "unresolved follow-ups.\n"
        "Rules: do not restate bullets that are already correct — they are kept "
        "automatically. Do not add new legal analysis. Write bullet text in "
        "whichever language best preserves fidelity (follow the transcript), and "
        "keep Chinese legal terms and citations exactly as written. If nothing "
        "changed, output nothing.\n\n"
        f"Current playbook:\n{previous or '(empty)'}\n\nRecent transcript:\n{transcript}"
    )


def summarize_with_fast_model(settings: Settings, messages: list[ChatMessage], previous: str = "") -> str:
    """Roll the memory forward by delta; fall back locally on any provider issue.

    Returns the merged playbook. The previous summary is *always* the base, so a
    model turn that produces nothing usable leaves prior detail intact instead of
    replacing it with a lossier paraphrase.
    """
    sections = parse_summary(previous)
    try:
        provider = build_provider(settings, fast=True)
        transcript = "\n\n".join(f"{m.role}: {m.content[:2000]}" for m in messages[-12:])
        result = provider.run(
            system=_DELTA_SYSTEM,
            messages=[{"role": "user", "content": _delta_prompt(previous, transcript)}],
            tools=[],
            max_iterations=1,
        )
        ops = parse_delta(result.text)
        if ops:
            return render_summary(apply_delta(sections, ops))
        # No parseable delta. If the playbook already holds content, keeping it
        # verbatim beats replacing it with an unparsed blob.
        if any(sections.values()):
            return render_summary(sections)
        if result.text.strip():
            return result.text.strip()[:6000]
    except Exception:
        pass
    return fallback_summary(messages, previous)
