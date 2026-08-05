"""Severity-tiered verification-report renderer.

Output format adapted (with attribution) from
sboghossian/master-claude-for-legal ``skills/citation-verifier.md`` (MIT)
and the coverage-line + provenance-tag discipline in
anthropics/claude-for-legal ``litigation-legal/CLAUDE.md``.

The renderer produces a markdown ``verification-report.md`` that is the
audit trail — it is intentionally **not** capped at the playbook's
≤2000-char ceiling, which applies to legal *answers*, not to audit
output. The report stands alongside (not inside) the answer bundle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


Severity = Literal["critical", "nuanced", "model_only"]
Verdict = Literal[
    "confirmed",
    "flagged_miscited",          # cite malformed or unknown reporter (Mode 1)
    "fabricated",                # cite not found in any reporter / DB (Mode 1)
    "misgrounded",               # cite exists, source doesn't support proposition (Mode 2)
    "paraphrase_as_quote",       # quote not verbatim in source (Mode 2)
    "could_not_check",           # connector/source unavailable
    "untagged",                  # cite has no provenance tag
    "out_of_date",               # cite to a repealed/superseded/abrogated authority
]


@dataclass
class ReportItem:
    location: str            # "p. 3, ¶ 2" or "char 1245" or "Finding #4"
    issue_type: Verdict
    severity: Severity
    claimed: str             # what the draft says
    source_says: str         # what the primary source actually says, or "(not retrieved)"
    fix: str                 # recommended action
    jurisdiction: str = ""
    provenance_tag: str = ""


@dataclass
class CoverageStats:
    total_cites: int = 0
    confirmed: int = 0
    could_not_check: int = 0
    miscited: int = 0
    misgrounded: int = 0

    @property
    def checked(self) -> int:
        return self.confirmed + self.could_not_check + self.miscited + self.misgrounded


# Escalation thresholds (sboghossian/citation-verifier):
#   - 5+ fabricated cites OR any paraphrase-as-quote on a holding citation
DEFAULT_FAB_THRESHOLD = 5


@dataclass
class VerificationReport:
    document: str
    stakes: Literal["filed", "internal"] = "internal"
    coverage: CoverageStats = field(default_factory=CoverageStats)
    items: list[ReportItem] = field(default_factory=list)
    sources_consulted: list[str] = field(default_factory=list)
    do_not_file: bool = False
    do_not_file_reason: str = ""
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    )

    def add(self, item: ReportItem) -> None:
        self.items.append(item)

    def coverage_line(self, lang: str = "en") -> str:
        cs = self.coverage
        if lang == "zh":
            return (
                f"已核 {cs.checked} 条 / 共 {cs.total_cites} 条引证："
                f"{cs.confirmed} 条确认；"
                f"{cs.could_not_check} 条无法核验；"
                f"{cs.miscited} 条疑似错引；"
                f"{cs.misgrounded} 条疑似 misgrounded（引用真实，但来源未支持论点）。"
            )
        return (
            f"Checked {cs.checked} of {cs.total_cites} citations. "
            f"{cs.confirmed} confirmed; "
            f"{cs.could_not_check} could not be retrieved; "
            f"{cs.miscited} flagged as potential miscitations; "
            f"{cs.misgrounded} flagged as misgrounded (cite exists but doesn't "
            f"support the proposition)."
        )

    def evaluate_do_not_file(
        self, fabricated_threshold: int = DEFAULT_FAB_THRESHOLD
    ) -> None:
        crits = [i for i in self.items if i.severity == "critical"]
        fabs = [
            i for i in crits
            if i.issue_type in ("fabricated", "flagged_miscited", "misgrounded")
        ]
        paraphrase_on_holding = any(
            i.issue_type == "paraphrase_as_quote"
            and ("hold" in i.claimed.lower() or "认定" in i.claimed or "判决" in i.claimed)
            for i in crits
        )
        if self.stakes == "filed" and len(fabs) >= fabricated_threshold:
            self.do_not_file = True
            self.do_not_file_reason = (
                f"{len(fabs)} fabricated/miscited/misgrounded citations exceed the "
                f"{fabricated_threshold}-cite escalation threshold for filed work."
            )
        elif self.stakes == "filed" and paraphrase_on_holding:
            self.do_not_file = True
            self.do_not_file_reason = (
                "Paraphrase-as-quote on a holding citation — pre-filing escalation required."
            )

    # ----- rendering ---------------------------------------------------------

    def _render_table(self, sev: Severity, lang: str = "en") -> str:
        rows = [i for i in self.items if i.severity == sev]
        if not rows:
            return ("_(none)_" if lang == "en" else "_（无）_") + "\n"
        if lang == "zh":
            header = "| 位置 | 问题 | 主张 | 来源实际 | 建议 |\n|---|---|---|---|---|\n"
        else:
            header = (
                "| Location | Issue | Claimed | Source says | Fix |\n"
                "|---|---|---|---|---|\n"
            )
        lines = [header]
        for r in rows:
            lines.append(
                f"| {_md(r.location)} | {_md(r.issue_type)} | "
                f"{_md(_clip(r.claimed, 200))} | "
                f"{_md(_clip(r.source_says, 200))} | "
                f"{_md(_clip(r.fix, 200))} |\n"
            )
        return "".join(lines)

    def render_markdown(self, lang: str = "en") -> str:
        L = _STRINGS[lang]
        body: list[str] = []
        body.append(f"# {L['title']}\n\n")
        body.append(f"**{L['document']}:** {self.document}\n")
        body.append(f"**{L['stakes']}:** {self.stakes}\n")
        body.append(f"**{L['generated']}:** {self.generated_at}\n")
        body.append(
            f"**{L['sources']}:** "
            + (", ".join(self.sources_consulted) if self.sources_consulted else L["none"])
            + "\n\n"
        )
        body.append(f"## {L['summary']}\n\n")
        body.append(self.coverage_line(lang) + "\n\n")
        if self.do_not_file:
            body.append(f"> **⚠️ {L['do_not_file']}** — {self.do_not_file_reason}\n\n")
        body.append(f"## {L['critical']}\n\n")
        body.append(L["critical_blurb"] + "\n\n")
        body.append(self._render_table("critical", lang) + "\n")
        body.append(f"## {L['nuanced']}\n\n")
        body.append(L["nuanced_blurb"] + "\n\n")
        body.append(self._render_table("nuanced", lang) + "\n")
        body.append(f"## {L['model_only']}\n\n")
        body.append(L["model_only_blurb"] + "\n\n")
        body.append(self._render_table("model_only", lang) + "\n")
        body.append(
            "---\n\n"
            + L["disclaimer"]
            + "\n"
        )
        return "".join(body)


# ----- citation trust tiers --------------------------------------------------
#
# Two-tier convention (anthropics/claude-for-legal): a Sources entry whose
# authority was actually retrieved through a connector this session carries a
# connector-verified tag; everything else carries the "[verify]" flag so the
# reader knows it rests on model memory alone.

TrustTier = Literal["connector_verified", "model_only"]

_CONNECTOR_LABELS = {
    "pkulaw_fatiao": "PKULaw",
    "pkulaw_law_search": "PKULaw",
    "pkulaw_case_search": "PKULaw",
    "pkulaw_case_list": "PKULaw",
    "pkulaw_nl_search": "PKULaw",
    "flk_npc_search": "flk.npc.gov.cn",
    "web": "web fetch",
}


@dataclass(frozen=True)
class TrustTag:
    ref: str                       # citation text or URL the tag applies to
    tier: TrustTier
    connector: str = ""            # fetcher that verified it (connector_verified only)

    def render(self) -> str:
        if self.tier == "connector_verified":
            label = _CONNECTOR_LABELS.get(self.connector, self.connector or "connector")
            return f"[connector-verified: {label}]"
        return "[verify]"


def assign_trust_tiers(
    refs: list[str], verified_by: dict[str, str]
) -> list[TrustTag]:
    """Map each citation ref to its trust tier.

    ``verified_by`` maps ref → connector name for refs whose source body was
    actually retrieved this run. Anything absent stays model-only "[verify]" —
    never promote a cite the pipeline did not round-trip.
    """
    tags: list[TrustTag] = []
    for ref in refs:
        connector = verified_by.get(ref)
        if connector:
            tags.append(TrustTag(ref, "connector_verified", connector))
        else:
            tags.append(TrustTag(ref, "model_only"))
    return tags


def annotate_sources_with_trust(text: str, tags: list[TrustTag]) -> str:
    """Append each entry's trust tag inside the ``## Sources`` / ``## 资料来源``
    section. Lines already carrying a provenance tag are left untouched.
    """
    from .provenance import TAG_RE, is_provenance_tag

    idx = max(text.rfind("Sources"), text.rfind("资料来源"), text.rfind("来源"))
    if idx < 0 or not tags:
        return text
    head, block = text[:idx], text[idx:]
    out_lines: list[str] = []
    for line in block.splitlines(keepends=True):
        stripped = line.strip()
        is_entry = stripped.startswith(("-", "*")) or re.match(r"^\d+[.)]", stripped)
        if is_entry and not any(is_provenance_tag(t) for t in TAG_RE.findall(line)):
            tag = next((t for t in tags if t.ref and t.ref in line), None)
            if tag is not None:
                newline = "\n" if line.endswith("\n") else ""
                line = line.rstrip("\n") + " " + tag.render() + newline
        out_lines.append(line)
    return head + "".join(out_lines)


def _clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else (s[: n - 1] + "…")


def _md(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


_STRINGS = {
    "en": {
        "title": "Citation Verification Report",
        "document": "Document",
        "stakes": "Stakes",
        "generated": "Generated",
        "sources": "Sources consulted",
        "none": "_(none — flag every cite tagged `[model knowledge — verify]`)_",
        "summary": "Summary",
        "do_not_file": "DO NOT FILE in current form",
        "critical": "CRITICAL — must address before filing",
        "critical_blurb": (
            "Fabricated cites, misgrounded propositions (cite exists but source "
            "doesn't support the claim), and paraphrases passed off as quotes."
        ),
        "nuanced": "NUANCED — review for accuracy",
        "nuanced_blurb": (
            "Cite supports part of the proposition but not the whole; framing "
            "may overstate. The Stanford RegLab \"partial-support\" failure mode."
        ),
        "model_only": "MODEL-ONLY — confirm or add citation",
        "model_only_blurb": (
            "Uncited propositions and cites missing a provenance tag. Default tag "
            "for un-retrieved citations is `[model knowledge — verify]`."
        ),
        "disclaimer": (
            "*Not legal advice. This is an AI-assisted pre-review pass; counsel must "
            "spot-check every flagged item against a primary source before filing or "
            "external delivery.*"
        ),
    },
    "zh": {
        "title": "引证核验报告",
        "document": "文档",
        "stakes": "提交属性",
        "generated": "生成时间",
        "sources": "已检索来源",
        "none": "_（无 — 所有未带溯源标签的引证默认按 `[model knowledge — verify]` 处理）_",
        "summary": "汇总",
        "do_not_file": "在当前状态下请勿提交",
        "critical": "CRITICAL — 提交前必须修正",
        "critical_blurb": (
            "包含：伪造引证；misgrounded（引证真实，但来源未支持论点）；"
            "以转述伪装为直接引语。"
        ),
        "nuanced": "NUANCED — 请人工复核",
        "nuanced_blurb": "引证仅部分支持论点；表述可能夸大。",
        "model_only": "MODEL-ONLY — 请确认或补充引证",
        "model_only_blurb": (
            "缺少引证的论断及未带溯源标签的引证。默认未检索引证标签为 "
            "`[model knowledge — verify]`。"
        ),
        "disclaimer": (
            "*非法律意见。本报告为 AI 辅助预审，提交前请由执业律师对每一条标记项核对原文。*"
        ),
    },
}
