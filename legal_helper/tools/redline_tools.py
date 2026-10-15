"""Tracked-changes tools: author a redline, or compare two drafts into one.

They expose the redline engine (``documents/redline.py``) and LibreOffice's
document compare (``documents/compare.py``) to the model: without a tool, a
chat that asked for 修订模式 output got a clean rewrite.
"""

from __future__ import annotations

import json
from typing import Literal, Optional

from anthropic import beta_tool
from pydantic import BaseModel, Field

from .documents import _derived_output, _record_edit, _resolve_read_path, _with_integrity

DEFAULT_REDLINE_AUTHOR = "Legal AI Helper"


class RedlineChange(BaseModel):
    """One tracked change (or margin comment) in a Word document."""

    op: Literal["replace", "delete", "insert_after", "insert_before", "comment"] = Field(
        default="replace",
        description=(
            "replace = delete `find` and insert `replace`; delete = delete `find` (or the whole "
            "paragraph/clause when find is empty); insert_after / insert_before = insert `text` next to "
            "`find`, or as new paragraph(s) after/before the paragraph/clause when find is empty; "
            "comment = margin note on `find` (or the paragraph/clause)."
        ),
    )
    find: str = Field(
        default="",
        description=(
            "Exact text inside one paragraph, as read_document shows it but without CriticMarkup or "
            "numbering labels (第一条 / 1.1 are Word auto-numbering, not text — scope with `clause`). "
            "Empty = act on the whole paragraph/clause."
        ),
    )
    replace: str = Field(default="", description="Replacement text for op=replace.")
    text: str = Field(
        default="",
        description=(
            "Inserted text (insert_after / insert_before; a newline starts another paragraph when "
            "inserting whole paragraphs) or the comment body (op=comment)."
        ),
    )
    clause: str = Field(
        default="",
        description=(
            "Limit the change to one clause: its label as the document shows it (第三条 = 第3条, '3.2', "
            "'Article 5', '（二）') or words of its heading."
        ),
    )
    paragraph: Optional[int] = Field(
        default=None,
        description="Limit the change to one body paragraph: the 0-based index inspect_docx lists.",
    )
    occurrence: int = Field(default=0, description="1-based: change only the n-th match in scope; 0 changes every match.")
    comment: str = Field(default="", description="Margin comment giving the reason for this change.")
    category: str = Field(
        default="",
        description=(
            "Optional category such as 'substantive' or 'drafting'. The change is attributed to "
            "category_authors[category] (or '<author> (<category>)'), so a reviewer can show or hide "
            "each kind separately in Word."
        ),
    )
    style: str = Field(default="", description="Paragraph style for inserted paragraphs; default is the anchor's style.")


@beta_tool
def redline_docx(
    source_path: str,
    filename: str,
    changes: list[RedlineChange],
    author: str = DEFAULT_REDLINE_AUTHOR,
    category_authors: Optional[dict[str, str]] = None,
) -> str:
    """Copy a .docx and apply changes as native Word tracked changes (修订模式):
    every insertion and deletion is an attributed, accept/reject-able
    revision, with optional margin comments. Use this — not edit_docx_text —
    whenever the deliverable is a markup a counterparty or partner reviews.

    Anchor each change by exact text (`find`), and scope it by `clause`
    (label as shown, e.g. 第三条) or `paragraph` (inspect_docx index) when
    the text recurs or is a numbering label. Every change that cannot be
    anchored comes back under `not_found` with a reason and a hint — re-anchor
    those, never report them as done. Track Changes is switched on in the
    output, an enforced editing restriction (other than tracked-changes-only)
    is refused, and the result checks that nothing else was lost.

    Args:
        source_path: Absolute or project-relative path to the source .docx.
        filename: Output filename under outputs/ (with or without .docx).
        changes: Ordered tracked changes / comments to apply.
        author: Reviewer name the revisions are attributed to.
        category_authors: Optional map of change category to author name,
            e.g. {"substantive": "Counsel (substantive)", "drafting": "Counsel (drafting)"}.
    """
    from ..documents.redline import RedlineRefused, redline_docx_document

    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".docx":
        return (
            f"ERROR: redline_docx needs a .docx (got {source.suffix or 'no extension'}). For .doc/.rtf/.odt/.wps, "
            "read_document converts it; ask for or produce a .docx first."
        )
    out, note = _derived_output(source, filename, "docx")
    edits = [c.model_dump() if hasattr(c, "model_dump") else dict(c) for c in changes]
    try:
        payload = redline_docx_document(
            source,
            out,
            edits,
            author=(author or DEFAULT_REDLINE_AUTHOR).strip() or DEFAULT_REDLINE_AUTHOR,
            category_authors=category_authors or {},
        )
    except RedlineRefused as e:
        return f"ERROR: redline refused — {e}"
    except Exception as e:
        return f"ERROR redlining {source}: {e}"
    payload["output_path"] = str(out.resolve())
    payload = _with_integrity(source, out, payload)
    if payload.get("not_found"):
        payload["next_step"] = (
            f"{len(payload['not_found'])} change(s) were not applied (see not_found). Re-anchor them — the "
            "hint says how — and run redline_docx again on this output; do not report them as made."
        )
    payload = _record_edit(source, out, "redline_docx", payload, note)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def compare_docx(
    original_path: str,
    revised_path: str,
    filename: str,
    author: str = DEFAULT_REDLINE_AUTHOR,
) -> str:
    """Compare two versions of a Word document and write a .docx in which
    every difference is a native tracked change (比对稿 / blackline), via
    LibreOffice's own compare. Han text is compared character by character.

    The result is checked both ways: accepting every change must give the
    revised text and rejecting every change the original
    (`round_trip`). Reports insertion/deletion counts.

    Args:
        original_path: The earlier version (.docx, .doc, .odt or .rtf).
        revised_path: The later version.
        filename: Output filename under outputs/ (with or without .docx).
        author: Name the differences are attributed to.
    """
    from ..documents.compare import CompareUnavailable, compare_docx_documents
    from ..documents.lineage import record_derivation, stale_source_warning

    original = _resolve_read_path(original_path)
    revised = _resolve_read_path(revised_path)
    for p in (original, revised):
        if not p.is_file():
            return f"ERROR: File not found: {p}"
        if p.suffix.lower() not in {".docx", ".doc", ".odt", ".rtf", ".wps", ".docm"}:
            return f"ERROR: compare_docx compares Word documents; got {p.name}"
    out, note = _derived_output(revised, filename, "docx")
    if out.resolve() == original.resolve():
        out, note = _derived_output(original, filename, "docx")
    try:
        payload = compare_docx_documents(original, revised, out, author=(author or DEFAULT_REDLINE_AUTHOR).strip())
    except CompareUnavailable as e:
        return f"ERROR: compare_docx is unavailable here: {e}"
    except Exception as e:
        return f"ERROR comparing {original.name} with {revised.name}: {e}"
    for p in (original, revised):
        warning = stale_source_warning(p)
        if warning:
            payload.setdefault("warnings", []).append(warning)
    record_derivation(out, [original, revised], "compare_docx")
    if note:
        payload["note"] = note
    trip = payload.get("round_trip") or {}
    if trip and not (trip.get("accept_all_equals_revised") and trip.get("reject_all_equals_original")):
        payload["next_step"] = (
            "The compare output does not round-trip exactly (see round_trip). Check the mismatching "
            "lines with read_document before sending it; a redline_docx pass from the original is the fallback."
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = ["RedlineChange", "compare_docx", "redline_docx"]
