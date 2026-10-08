"""Heuristic clause extractor used by review-contract / triage-nda.

Generic commercial categories live here. Domain-pack-specific category
patterns (e.g. aviation IDERA / Cape Town / AD-SB) are pulled in only when
the corresponding pack is active, via ``_active_category_tags()``.
"""

import json
import re

from anthropic import beta_tool


# Generic commercial clause categories — apply across jurisdictions and
# domains. Aviation-specific tags (hull insurance, IDERA, AD/SB, etc.) are
# kept in ``domains/aviation/contract_tags.py`` and merged in only when the
# aviation pack is active.
_CATEGORY_TAGS: list[tuple[str, re.Pattern[str]]] = [
    ("limitation_of_liability", re.compile(r"\b(limitation of liability|cap on liability|liability cap)\b", re.I)),
    ("indemnification", re.compile(r"\b(indemnif(y|ication)|hold harmless)\b", re.I)),
    ("insurance", re.compile(r"\b(insurance|insured|insurer)\b", re.I)),
    ("export_control", re.compile(r"\b(ITAR|EAR|export control|denied party|deemed export)\b", re.I)),
    ("governing_law", re.compile(r"\b(governing law|jurisdiction|venue|arbitration)\b", re.I)),
    ("term_and_termination", re.compile(r"\b(term|termination|renewal|expiry|expiration)\b", re.I)),
    ("assignment_novation", re.compile(r"\b(assignment|novation|change of control)\b", re.I)),
    ("data_protection", re.compile(r"\b(data protection|GDPR|DPA|personal data)\b", re.I)),
    ("confidentiality", re.compile(r"\b(confidential(?:ity)?|non-disclosure)\b", re.I)),
    ("force_majeure", re.compile(r"\b(force majeure)\b", re.I)),
    ("payment_terms", re.compile(r"\b(payment terms|invoice|late fee|interest)\b", re.I)),
    ("dispute_resolution", re.compile(r"\b(dispute resolution|arbitration|mediation)\b", re.I)),
    ("warranties", re.compile(r"\b(warrant(?:y|ies)|representations)\b", re.I)),
]


# The same categories in Chinese contract language. ``\b`` never fires between
# two Han characters (both are word characters), so these carry no boundaries.
_CATEGORY_TAGS_ZH: list[tuple[str, re.Pattern[str]]] = [
    ("limitation_of_liability", re.compile(r"责任限制|责任上限|赔偿限额|赔偿总额不超过")),
    ("indemnification", re.compile(r"赔偿|补偿|免受损害|使其免责")),
    ("insurance", re.compile(r"保险")),
    ("export_control", re.compile(r"出口管制|两用物项|制裁")),
    ("governing_law", re.compile(r"适用法律|法律适用|管辖法院|管辖权|准据法")),
    ("term_and_termination", re.compile(r"期限|终止|解除|续期|续约|届满")),
    ("assignment_novation", re.compile(r"转让|让与|控制权变更")),
    ("data_protection", re.compile(r"个人信息|数据保护|数据安全|数据出境")),
    ("confidentiality", re.compile(r"保密|商业秘密")),
    ("force_majeure", re.compile(r"不可抗力")),
    ("payment_terms", re.compile(r"付款|支付|价款|租金|发票|违约金|滞纳金|利息")),
    ("dispute_resolution", re.compile(r"争议解决|仲裁|诉讼|调解|协商解决")),
    ("warranties", re.compile(r"陈述与保证|陈述和保证|保证|承诺")),
]


def _active_category_tags() -> list[tuple[str, re.Pattern[str]]]:
    """Return generic categories plus any pack-contributed categories for
    the currently active domain packs."""
    tags = list(_CATEGORY_TAGS) + list(_CATEGORY_TAGS_ZH)
    try:
        from ..config import current_settings

        active = list(current_settings().active_domain_packs)
    except Exception:
        active = []
    if "aviation" in active:
        try:
            from ..domains.aviation.contract_tags import AVIATION_CATEGORY_TAGS

            tags.extend(AVIATION_CATEGORY_TAGS)
        except Exception:
            pass
    return tags


def _categorize(heading: str, body: str) -> list[str]:
    text = f"{heading}\n{body}"
    hits: list[str] = []
    for name, pat in _active_category_tags():
        if name not in hits and pat.search(text):
            hits.append(name)
    return hits


def _clause_record(clause, body: str, by_index: list) -> dict:
    heading = clause.heading() if clause.label else clause.title
    record = {
        "label": clause.label,
        "heading": heading,
        "title": clause.title,
        "level": clause.depth,
        "parent": by_index[clause.parent].heading() if clause.parent is not None else None,
        "body": body.strip(),
        "categories": _categorize(heading, body),
    }
    if clause.paragraph is not None:
        record["paragraph"] = clause.paragraph
    return record


def _split_clauses(text: str) -> list[dict]:
    """Clauses of plain text / markdown / PDF text, flat in document order.

    Each clause's ``body`` runs to the next clause of any level, so no text is
    repeated; ``level`` and ``parent`` carry the hierarchy. Text before the
    first clause is the PREAMBLE.
    """
    from ..documents.clauses import clause_index_from_text

    units, clauses = clause_index_from_text(text)
    lines = (text or "").splitlines()
    out: list[dict] = []
    first = clauses[0].start if clauses else len(lines)
    preamble = "\n".join(lines[:first]).strip()
    if preamble:
        out.append({"label": "", "heading": "PREAMBLE", "title": "", "level": 0, "parent": None,
                    "body": preamble, "categories": _categorize("PREAMBLE", preamble)})
    for clause in clauses:
        body = "\n".join(lines[clause.start + 1:clause.own_end])
        out.append(_clause_record(clause, body, clauses))
    for clause, record in zip(clauses, out[1 if preamble else 0:]):
        record["line"] = clause.start
    return out


def _split_docx_clauses(path) -> list[dict]:
    """Clauses of a Word document from its own numbering (第一条 as Word shows
    it), with each clause's body paragraph index for redline_docx anchors."""
    from ..documents.docx_reader import read_docx

    reading = read_docx(path)
    clauses = reading.clauses()
    out: list[dict] = []
    first = clauses[0].start if clauses else len(reading.blocks)
    preamble = reading.render_blocks(reading.blocks[:first]).strip()
    if preamble:
        out.append({"label": "", "heading": "PREAMBLE", "title": "", "level": 0, "parent": None,
                    "body": preamble, "categories": _categorize("PREAMBLE", preamble)})
    for clause in clauses:
        body = reading.render_blocks(reading.blocks[clause.start + 1:clause.own_end])
        out.append(_clause_record(clause, body, clauses))
    return out


@beta_tool
def extract_clauses(text: str = "", path: str = "") -> str:
    """Split a contract into its clauses and tag generic commercial categories
    (governing law, indemnification, payment terms, force majeure, etc.).
    When a domain pack is active, pack-specific tags are surfaced too.

    Pass ``path`` for a file: a .docx (or .doc/.rtf/.odt/.wps) is indexed
    from Word's own numbering, so labels come out exactly as the document
    shows them (第一条, 1.1, Article 5) with each clause's paragraph index for
    redline_docx; other formats are read as text. Or pass ``text`` already
    in hand. Labels recognised in text: 第X编/章/节/条/款, 一、, （一）, 1. / 1.1
    / 1.1.1, (1), ①, Article / Section / Clause N, Schedule / Exhibit / Annex.

    Returns a JSON list in document order: ``[{label, heading, title, level,
    parent, paragraph?, line?, body, categories}]``; ``body`` runs to the next
    clause of any level, and text before the first clause is PREAMBLE.

    Args:
        text: Contract text (already extracted). Ignored when ``path`` is given.
        path: Absolute or project-relative path to the contract file.
    """
    if path:
        from pathlib import Path

        from ..config import current_settings
        from ..documents.intake import (
            WORD_LEGACY_SUFFIXES,
            WORD_SUFFIXES,
            UnsupportedFormat,
            effective_suffix,
            read_any,
            read_legacy_word,
        )

        p = Path(path)
        if not p.is_absolute():
            p = current_settings().project_root / path
        if not p.is_file():
            return json.dumps({"error": f"File not found: {p}"}, ensure_ascii=False)
        try:
            suffix = effective_suffix(p)
            docx = p if suffix in WORD_SUFFIXES else (read_legacy_word(p)[1] if suffix in WORD_LEGACY_SUFFIXES else None)
            if docx is not None:
                return json.dumps(_split_docx_clauses(docx), ensure_ascii=False)
            text = read_any(p)
        except UnsupportedFormat as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"could not read {p.name}: {exc}"}, ensure_ascii=False)
    clauses = _split_clauses(text or "")
    return json.dumps(clauses, ensure_ascii=False)
