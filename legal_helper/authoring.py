"""The system prompt of whoever writes the answer the user reads.

Two agents author answers: the single-pass author (205 of 219 legal answers
since August) and the integrator of a multi-specialist run. Neither saw the
playbook's method before — only specialists had it inlined — and the
single-pass path dropped the skill the planner chose, so `litigation-analysis`
was routed five times and its method loaded on none of them.

The prompt built here is static for a given (mode, skill, active packs), so it
caches; everything that changes per turn (date, language, length, what the
answer must cover) goes in the request tail — see ``answer_contract.author_tail``.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from .answer_contract import author_rules
from .config import Settings

AUTHOR_MODES = ("single_pass", "integration")

_ROLE = {
    "single_pass": """You are a legal AI helper writing the answer to a practitioner's question.

You have hosted web search and fetch, legal source connectors and document
tools. There are no sub-agents this turn: research the question yourself with
your tools, then write the answer. Do not emit `<tool_call>` or any pseudo
function-call envelope as text.""",
    "integration": """You are a legal AI helper writing the answer to a practitioner's question.

Specialists have already researched it; their bundles are attached to the
request. A bundle is raw research — findings with pinpoints, per-issue element
work, artefacts, gaps and sources — not text to paste: you are the only author,
and the reader never sees which specialist found what. You have the same
research and document tools as the specialists: use them to close a gap a
bundle names, to resolve a conflict between bundles, or to produce a requested
file. Do not emit `<tool_call>` or any pseudo function-call envelope as text.""",
}

_CONVERSATION = """This turn may sit anywhere in an ongoing conversation. The conversation context
supplied with the request is what you have already established with this user;
build on it rather than re-deriving it."""

_INTEGRATION_RULES = """# Integrating the bundles
- Walk the issues in the plan, in order. For each: the controlling rule, the
  elements against the facts, the strongest counter-reading, the conclusion.
- Conflicting findings: reconcile them from the sources (higher authority, the
  version in force at the relevant date, the more specific rule — name which
  rule decides). If they cannot be reconciled, keep both and say how they sit
  together and what would decide between them. Never silently drop one.
- Open gaps (a bundle's `Out of scope`, a failed or empty specialist): research
  them yourself if your tools can close them; otherwise say plainly which
  angles this answer does not cover and must be re-run before reliance.
- Commit to what a bundle verified with a pinpoint; hedge only what no bundle
  and no tool call could confirm. Never invent findings for failed coverage."""


def research_conduct() -> str:
    """How to find the law. Shared by every author and every specialist.

    Domain-specific source routing is not here: an active pack contributes its
    own (``DomainPack.research_sources``), so a generic prompt never names one
    domain's regulators or instruments.
    """
    return """# Research conduct
- Scope the applicable law before you research (playbook §0): which bodies of
  law control each issue, at which level, in the version in force on the
  operative date. Then fetch the controlling primary sources and pin them;
  secondary commentary alone does not carry a legal proposition.
- A treaty or other international instrument applies only through its
  domestic hook: name the statute clause, implementing law or
  direct-application rule that makes it bind in the forum, and each party's
  status and reservations. A sourced "this does not apply here / this party is
  not bound" is a finding, not a gap.
- State what you verified as established, with its pinpoint inline. Hedge only
  what you could not confirm, and say what would settle it. A list of things
  "to be verified" is not an answer.
- Source routing is by where the law lives. Jurisdictions with a structured
  connector — PRC (PKULaw tools when present; `flk_npc_search` is a
  status/metadata fallback without statute bodies), US (eCFR, Federal
  Register, GovInfo, CourtListener), EU (EUR-Lex) — use the connector first,
  then hosted web search for gaps and current events. Every other jurisdiction:
  hosted `web_search` / `web_fetch` of official sources (government gazette,
  regulator, court) is the primary path, and a thin catalogue or scan connector
  is supplementary. A connector result that comes back garbled, empty or off
  topic is not a source: search for the authoritative text instead of citing it
  or reporting the pinpoint as unavailable.
- Cite only sources you read."""


def playbook_method() -> str:
    """The general playbook, inlined for every legal author."""
    from .skills import load_playbook

    try:
        playbook = load_playbook().strip()
    except Exception:  # noqa: BLE001 — a missing playbook never breaks a run
        return ""
    return f"# General legal playbook\n\n{playbook}" if playbook else ""


_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")

#: SKILL.md sections that describe the specialist bundle rather than method.
#: The author writes the final answer, so they would contradict its contract.
_BUNDLE_SECTION_PREFIXES = ("output contract",)


def strip_bundle_sections(body: str) -> str:
    """A SKILL.md body without its bundle-contract sections.

    Split on level-2 headings outside fenced blocks — the contract sections
    themselves contain fenced `## Findings` examples.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    fence: Optional[str] = None
    for line in body.split("\n"):
        marker = _FENCE_RE.match(line)
        if marker:
            token = marker.group(1)[0] * 3
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
        heading = None if fence else _H2_RE.match(line)
        if heading:
            sections.append((heading.group(1).strip().lower(), [line]))
        else:
            sections[-1][1].append(line)
    kept = [
        "\n".join(lines)
        for title, lines in sections
        if not title.startswith(_BUNDLE_SECTION_PREFIXES)
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def skill_method(skill_name: Optional[str], active_packs: Iterable[str] = ()) -> str:
    """The chosen skill's method for the author, without its bundle contract."""
    if not skill_name:
        return ""
    from .agent import _strip_inactive_pack_pointers
    from .skills import internal_skill_names, load_skill_body

    if skill_name not in internal_skill_names():
        return ""
    try:
        body = load_skill_body(skill_name)
    except Exception:  # noqa: BLE001
        return ""
    body = _strip_inactive_pack_pointers(strip_bundle_sections(body), set(active_packs))
    if not body:
        return ""
    return (
        f"# Method for this request (`{skill_name}`)\n\n"
        "The planner routed this request to the method below. Follow its steps "
        "and produce the artefacts it calls for (tables, matrices, timelines) in "
        "the answer itself where they help the reader. Its `references/` and "
        "`resources/` files open with `read_skill_reference`.\n\n"
        f"{body}"
    )


def author_system_prompt(
    settings: Settings,
    *,
    mode: str = "single_pass",
    skill_name: Optional[str] = None,
) -> str:
    """System prompt for the author of a legal answer."""
    from .agent import _build_parent_addendum

    if mode not in AUTHOR_MODES:
        raise ValueError(f"unknown author mode: {mode}")
    blocks = [
        _ROLE[mode],
        _CONVERSATION,
        research_conduct(),
    ]
    if mode == "integration":
        blocks.append(_INTEGRATION_RULES)
    blocks.append(author_rules())
    method = playbook_method()
    if method:
        blocks.append(method)
    chosen = skill_method(skill_name, settings.active_domain_packs)
    if chosen:
        blocks.append(chosen)
    return "\n\n".join(blocks) + _build_parent_addendum(settings, skill_name=skill_name)


__all__ = [
    "AUTHOR_MODES",
    "author_system_prompt",
    "playbook_method",
    "research_conduct",
    "skill_method",
    "strip_bundle_sections",
]
