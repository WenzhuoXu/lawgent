"""The system prompt of the author: the chat's model, answering every turn.

There is one author and one prompt. The chat's model reads every message and
decides what it needs: which method to load (`load_skill`), which domain pack
(`load_pack`), whether to research, whether to delegate to specialists
(`run_workflow`), or whether to just answer.

The prompt is static for a given set of pinned packs, so it caches; everything
that changes per turn (date, language, length, a method the user picked) goes
in the request tail — see ``answer_contract.author_tail``.
"""

from __future__ import annotations

import re
from typing import Optional

from .answer_contract import author_rules, deliverable_rules
from .config import Settings

_ROLE = """You are a legal AI helper. You answer every message in this chat yourself, with the tools you hold: hosted web search and fetch, legal source connectors, the local legal corpus, document tools, the legal methods listed below, and a multi-agent workflow.

Legal work follows the research conduct and the general playbook below. Anything else — a document or spreadsheet task, a translation, a reasoning or exam question, a general question, a greeting — gets a direct, practical answer, without legal framing the user did not ask for. Use hosted web search when an answer depends on current facts. Do not emit `<tool_call>` or any pseudo function-call envelope as text."""

_CONVERSATION = """This turn may sit anywhere in an ongoing conversation. The conversation context
supplied with the request is what you have already established with this user;
build on it rather than re-deriving it."""

_CONDUCT = """# Conduct
- Work a question through before you answer it. When the user disputes an
  answer or proposes a different one, work the question again from the source
  material before you reply: change your answer only when that work supports
  the change, and say what changed; otherwise keep it and show why.
- Ask a clarifying question only when a fact that changes the answer is
  missing and neither a stated assumption nor a short answer for each reading
  will do. Ask only for that fact.
- Files: pick the tool its description says fits. For a workbook, inspect the
  real cell coordinates before editing (`inspect_xlsx_range`), never row
  numbers read off a markdown export. `read_format_recipe` holds the deeper
  recipe for a format when you want it; it is not a required first step."""

_INTEGRATION_RULES = """## Integrating a workflow's bundles
- Walk the request's issues in the user's order. For each: the controlling
  rule, the elements against the facts, the strongest counter-reading, the
  conclusion.
- Conflicting findings: reconcile them from the sources (higher authority, the
  version in force at the relevant date, the more specific rule — name which
  rule decides). If they cannot be reconciled, keep both and say how they sit
  together and what would decide between them. Never silently drop one.
- Open gaps (a bundle's `Out of scope`, a failed or empty specialist): research
  them yourself if your tools can close them; otherwise say plainly which
  angles this answer does not cover and must be re-run before reliance.
- Commit to what a bundle verified with a pinpoint; hedge only what no bundle
  and no tool call could confirm. Never invent findings for failed coverage."""


def workflow_guidance() -> str:
    """When to delegate, and how to integrate what comes back."""
    return (
        "# Multi-agent workflow\n"
        "`run_workflow` hands parts of a request to specialist sub-agents that "
        "research in parallel and return bundles to you; its description says "
        "when it pays. When the user asks for a workflow or for several agents — "
        "in any language, in any phrasing — calling it is mandatory, sized to the "
        "task. Otherwise most turns need no workflow: answer directly, with your "
        "own tools.\n\n" + _INTEGRATION_RULES
    )


def methods_block() -> str:
    """The method list: name and one line each; `load_skill` returns the body."""
    from .skills import internal_skill_names, skill_manifest

    lines = []
    for name in internal_skill_names():
        if name == "cite-check":
            continue
        try:
            description = str(skill_manifest(name).get("description") or "")
        except Exception:  # noqa: BLE001
            description = ""
        first = re.split(r"(?<=[.。])\s", description.strip(), maxsplit=1)[0]
        lines.append(f"- `{name}` — {first[:240]}")
    if not lines:
        return ""
    return (
        "# Legal methods\n"
        "When a legal request matches a method below, load it with "
        "`load_skill` before you work the request and follow its steps; its "
        "references open with `read_skill_file`. A request may need none, or "
        "more than one. Never guess a name.\n"
        + "\n".join(lines)
    )


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


def author_system_prompt(settings: Settings) -> str:
    """System prompt of the author — the same on every turn of a chat."""
    from .agent import _build_parent_addendum

    blocks = [
        _ROLE,
        _CONVERSATION,
        _CONDUCT,
        research_conduct(),
        author_rules(),
        deliverable_rules(),
    ]
    method = playbook_method()
    if method:
        blocks.append(method)
    methods = methods_block()
    if methods:
        blocks.append(methods)
    from .tools.pack_tools import pack_catalog_block

    packs = pack_catalog_block(settings)
    if packs:
        blocks.append(packs)
    from .tool_exposure import on_demand_block

    blocks.append(on_demand_block())
    blocks.append(workflow_guidance())
    return "\n\n".join(blocks) + _build_parent_addendum(settings)


__all__ = [
    "author_system_prompt",
    "methods_block",
    "playbook_method",
    "research_conduct",
    "strip_bundle_sections",
    "workflow_guidance",
]
