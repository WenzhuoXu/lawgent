"""One output contract for every author and specialist.

The contract used to live in six places that disagreed: the single-pass system
prompt, the integration prompt, the orchestrator prompt, the specialist
contract, the repair prompt and the heuristic plan — plus a flat "≤ 2000
characters" in twelve SKILL.md files against "~700 per coverage token" here,
three different label sets, and English status words (`pinpoint unavailable`)
hard-coded into answers written in Chinese (95% of September's Chinese answers
carried the English token). Everything an answer must look like is rendered
from this module, and ``tests/test_answer_contract.py`` fails when a prompt
grows its own copy.

Two contracts:

- **Author** — the single-pass author and the integrator write the answer the
  user reads. One sources list, one line per source, status words in the
  answer's language; no claim column (it restated the body: 28.5% of an
  answer's length); no disclaimer (the interface and exports show it once).
- **Specialist** — research sub-agents write internal bundles the author reads.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

# --- language -----------------------------------------------------------------

_HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_URL_RE = re.compile(r"https?://\S+|\S+@\S+|`[^`]*`")


def detect_language(text: str, default: str = "Chinese") -> str:
    """The answer language for ``text``, from its script share.

    The planner used to supply this and the parser defaulted to English when it
    did not, so a 241-character Chinese request got a 10,309-character English
    memo. URLs, e-mail addresses and code spans are ignored; a Latin word
    counts like one Han character, since a Chinese request routinely names
    English instruments.
    """
    cleaned = _URL_RE.sub(" ", text or "")
    han = len(_HAN_RE.findall(cleaned))
    latin_words = len(re.findall(r"[A-Za-z]+", cleaned))
    if not han and not latin_words:
        return default
    return "Chinese" if han >= latin_words else "English"


def language_code(user_language: Optional[str]) -> str:
    """``zh`` or ``en`` for the contract's per-language tokens."""
    lang = (user_language or "").strip().lower()
    if lang.startswith(("zh", "chinese", "中")):
        return "zh"
    return "en"


# --- per-language tokens --------------------------------------------------------

SOURCES_HEADING = {"zh": "资料来源", "en": "Sources"}

#: Status words for a sources line. ``user`` marks the user's own files: an
#: online check does not apply to them, and 271 of 408 "unverified" rows had
#: cited exactly those, showing amber for no reason.
STATUS = {
    "zh": {
        "verified": "已在线核验",
        "unverified": "未核验",
        "user": "用户提供",
        "no_pinpoint": "无法获得具体条款",
    },
    "en": {
        "verified": "verified online",
        "unverified": "unverified",
        "user": "user-provided",
        "no_pinpoint": "pinpoint unavailable",
    },
}

# --- length tiers ---------------------------------------------------------------

LENGTH_TIERS = ("brief", "focused", "full")

_TIER_FOR_COMPLEXITY = {"simple": "brief", "standard": "focused", "complex": "full"}

#: OpenAI ``text.verbosity`` per tier. Only the brief tier moves off the
#: default: simple questions were answered at a median of 4,457 characters and
#: a quarter of them ran longer than the 8.5k gold memo.
VERBOSITY = {"brief": "low", "focused": "medium", "full": "medium"}

_LENGTH_SENTENCE = {
    "brief": (
        "Length: brief — give the conclusion and the controlling rule with its "
        "pinpoint, then only the reasoning needed to support it."
    ),
    "focused": (
        "Length: focused — answer each question asked, in order, with its rule, "
        "application and conclusion; leave out background the reader did not ask for."
    ),
    "full": (
        "Length: a full memo — cover every part of the request in the order "
        "asked, at the depth each issue needs."
    ),
}


def length_tier(complexity: Optional[str], override: Optional[str] = None) -> str:
    """The answer's length tier: a pinned chat setting wins, else complexity."""
    if override in LENGTH_TIERS:
        return override  # type: ignore[return-value]
    return _TIER_FOR_COMPLEXITY.get((complexity or "").strip().lower(), "focused")


def length_instruction(tier: str) -> str:
    """One sentence. An explicit length or deliverable request always wins."""
    return (
        _LENGTH_SENTENCE.get(tier, _LENGTH_SENTENCE["focused"])
        + " If the user asked for a particular length or deliverable, that request "
        "wins. Mention adjacent issues worth a follow-up in one line each at the "
        "end instead of analysing them."
    )


# --- the volatile per-turn tail -------------------------------------------------

_CST = timezone(timedelta(hours=8))


def today_line(now: Optional[datetime] = None) -> str:
    """Today's date for the per-turn tail (never the cached system prompt).

    No request carried the date before, so period and deadline arithmetic ran
    against whatever the model assumed today was.
    """
    current = (now or datetime.now(_CST)).astimezone(_CST)
    return f"Today's date: {current.date().isoformat()} (Asia/Shanghai)."


# --- author contract ------------------------------------------------------------

def _sources_rule() -> str:
    zh, en = STATUS["zh"], STATUS["en"]
    return (
        f"- End with exactly one sources section: `## {SOURCES_HEADING['zh']}` in "
        f"a Chinese answer, `## {SOURCES_HEADING['en']}` in an English one. One "
        "numbered line per source the answer relies on — no table, no claim "
        "column (the body already states the claims):\n"
        "  `1. Source name, pinpoint — status`\n"
        "  then the source's link, if it has one: its web-search citation, or the "
        "URL you hold as text. The status is exactly one of — Chinese answers: "
        f"{zh['verified']} / {zh['unverified']} / {zh['user']} / {zh['no_pinpoint']}; "
        f"English answers: {en['verified']} / {en['unverified']} / {en['user']} / "
        f"{en['no_pinpoint']}. Use {zh['user']} / {en['user']} for the user's own "
        "files and documents. Do not write any other verification table or log."
    )


def author_rules() -> str:
    """How the final answer is written. Static, so it caches with the system prompt."""
    return "\n".join(
        [
            "# Writing the answer",
            "- Write in the answer language the request tail names. Quote primary "
            "sources in their original language, with a short gloss when it "
            "differs from the answer's.",
            "- Begin with the answer: the conclusion first, then the analysis. Do "
            "not narrate your process or restate the question.",
            "- Answer what was asked, at the scope intended, at the length the "
            "request tail sets. Pick one organising axis (the user's own outline "
            "when there is one) and state each fact once.",
            "- Cite at the point of use: name the source and its pinpoint "
            "(art./§/clause) in the sentence, and keep the citation web search "
            "attaches. A URL you hold as text (a page you fetched, a connector "
            "result) may be a markdown link `[Source, pinpoint](URL)`. Never write a "
            "link without a real URL or file path, and never put a citation inside "
            "a link. A pinpoint you could not confirm is marked as such, never "
            "invented.",
            "- When the facts call for action — a deadline, a filing, evidence to "
            "obtain, a decision to take — end the analysis with those steps, "
            "concretely and in order. Leave this out when there is nothing to do.",
            _sources_rule(),
            "- Do not add a not-legal-advice line; the interface and every export "
            "already carry one.",
            "- When the user asks for a file (Word, PDF, spreadsheet, deck), make "
            "it with the matching tool and give its path; do not paste the "
            "document body into the chat instead.",
        ]
    )


def author_tail(
    *,
    user_language: str,
    tier: str,
    must_cover: str = "",
    fact_conflicts: Iterable[str] = (),
    assumptions: str = "",
) -> str:
    """The volatile part of an author's request: everything that changes per turn."""
    parts = [today_line(), f"Answer language: {user_language}.", length_instruction(tier)]
    if must_cover.strip():
        parts.append("What the answer must cover:\n" + must_cover.strip())
    conflicts = [c.strip() for c in fact_conflicts if c and c.strip()]
    if conflicts:
        parts.append(
            "The facts as given contradict each other — do not compute through "
            "this. Say which reading you adopt and how the answer changes on the "
            "other:\n" + "\n".join(f"- {c}" for c in conflicts)
        )
    if assumptions.strip():
        parts.append(assumptions.strip())
    return "\n\n".join(parts)


# --- specialist contract --------------------------------------------------------

#: Labels a Findings bullet may carry. One set, here — the playbook had `case`
#: and `guidance`, the specialist prompt had `ICAO`.
FINDING_LABELS = (
    "treaty",
    "statute",
    "regulation",
    "judicial-interpretation",
    "case",
    "guidance",
    "local-law",
    "contract",
    "best-practice",
    "risk",
    "drafting",
)


def specialist_contract() -> str:
    """The research-bundle contract every in-tree specialist emits."""
    labels = " | ".join(FINDING_LABELS)
    return f"""# Specialist bundle contract

Your output is internal research for the author of the final answer, not the
answer. Emit these sections and nothing else. Keep the headings verbatim in
English (the harness parses them); write the content in whichever language fits
the material.

```
## Findings
- [one self-contained conclusion, ONE sentence, with its inline pinpoint `[Source, art./§/p.](URL or local path)`] (label: {labels})

## Issues
### <issue id or title>
- elements → the facts and evidence for each, or `unproven`
- strongest counter-reading / adverse authority
- considered and excluded: <authority> — <why it does not control>

## Artefacts
<tables your method produces — risk matrix, element table, review grid, draft clauses — complete, as markdown>

## Out of scope
- [angles your task named that you did not cover]

## Sources
- [source name] — [URL or local path] — supports #X, #Y — online-checked: yes/no — pinpoint: confirmed/unavailable
```

- `## Issues` only when your task assigns issues; `## Artefacts` only when your
  method produces a table or draft. Everything else is required.
- For a case, add its weight to the label (binding / 应当参照 / 参考 /
  persuasive, as the playbook defines them).
- No bullet restates another; merge propositions that share an authority.
- A pinpoint you could not confirm is written `pinpoint unavailable` and the
  claim is softened; never invent article numbers, dates, agencies or forms.
- Length: about 700 characters of Findings per `coverage:` token in your task
  (floor ~1,800, ceiling ~6,000). Every coverage token gets at least one
  pinpointed bullet. Issues and Artefacts are outside this budget.
- Do not write an introduction, summary, recap, scenario walkthrough or a
  separate verification log; the author owns presentation.
"""


# --- repair ---------------------------------------------------------------------

def repair_rules(user_language: str) -> str:
    lang = language_code(user_language)
    no_pin = STATUS[lang]["no_pinpoint"]
    heading = SOURCES_HEADING[lang]
    return "\n".join(
        [
            "## How to repair",
            "- Fix only what the audit flagged. Leave every other sentence, "
            "heading, table and citation exactly as it is.",
            "- Re-verify each flagged citation with the tools you have. If the "
            "source supports the claim, correct the pinpoint and keep the claim.",
            "- If the source does not support the claim, change the claim to what "
            "the source does support, or remove it and say in the analysis that "
            "the point could not be verified.",
            "- Never invent a replacement citation, and never keep a pinpoint you "
            f"could not confirm: mark that source `{no_pin}`.",
            f"- Return the complete repaired answer in {user_language}, with its "
            f"`## {heading}` section intact. Return the answer only — no "
            "changelog, no commentary on the repair.",
        ]
    )


__all__ = [
    "FINDING_LABELS",
    "LENGTH_TIERS",
    "SOURCES_HEADING",
    "STATUS",
    "VERBOSITY",
    "author_rules",
    "author_tail",
    "detect_language",
    "language_code",
    "length_instruction",
    "length_tier",
    "repair_rules",
    "specialist_contract",
    "today_line",
]
