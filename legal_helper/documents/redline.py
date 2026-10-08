"""Native Word tracked changes (``w:ins`` / ``w:del``) and margin comments.

``edit_docx_text`` rewrites silently; this module produces the document a
lawyer actually forwards: real revision marks Word renders in the Review pane
(accept/reject-able), plus comments anchored with ``commentRangeStart`` /
``commentRangeEnd``. Comment authoring uses python-docx >= 1.2; revision marks
are authored at the oxml level because python-docx has no tracked-change API.

What the first version got wrong, and what this one does instead:

- **Run splits dropped tabs and breaks.** A boundary run was collapsed to
  ``rPr`` + one ``w:t``, so "Term:\\tthree years" redlined to five read
  "Term:five years" once accepted. Runs are now split element by element
  (``w:tab``/``w:br`` are one-character atoms), and zero-width children —
  footnote and comment references, field characters, drawings — are kept out
  of a deletion rather than deleted with the text around them.
- **Anchors the model could not supply.** 5 of 8 finds on a fixture came back
  ``not_found``, three of them strings copied from ``read_document`` output:
  numbering labels such as 第1条 that are not in the XML. A change can now be
  scoped by clause label (the clause index computes 第一条 from
  ``numbering.xml``) or by body paragraph index, and every miss carries a
  reason and a hint.
- **No tracking setting, no protection check.** ``w:trackRevisions`` is set so
  the counterparty's further edits are tracked too, and an enforced
  ``w:documentProtection`` other than tracked-changes is refused.

Scope notes: one ``find`` stays within one paragraph; text inside an existing
revision (someone else's pending insertion or deletion) is never re-matched,
so revisions never nest.
"""

from __future__ import annotations

import copy
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from xml.etree import ElementTree as ET

DEFAULT_AUTHOR = "Legal AI Helper"

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_LOCKED = ""  # stands in for an existing revision: no match may cross it


def _wq(name: str) -> str:
    return f"{{{_W_NS}}}{name}"


_R, _T, _P, _PPR, _RPR = _wq("r"), _wq("t"), _wq("p"), _wq("pPr"), _wq("rPr")
_INS, _DEL, _MOVE_FROM, _MOVE_TO = _wq("ins"), _wq("del"), _wq("moveFrom"), _wq("moveTo")
_REVISIONS = (_INS, _DEL, _MOVE_FROM, _MOVE_TO)
_ONE_CHAR = {_wq("tab"): "\t", _wq("ptab"): "\t", _wq("br"): "\n", _wq("cr"): "\n", _wq("noBreakHyphen"): "-"}
_WALK_INTO = frozenset({_wq("hyperlink"), _wq("smartTag"), _wq("customXml"), _wq("fldSimple"), _wq("bdo"), _wq("dir")})
# Zero-width run children a text edit must not take down with it.
_KEEP_OUT = frozenset(
    {
        _wq("footnoteReference"), _wq("endnoteReference"), _wq("commentReference"),
        _wq("fldChar"), _wq("instrText"), _wq("drawing"), _wq("pict"), _wq("object"),
        f"{{{_MC_NS}}}AlternateContent", _wq("annotationRef"), _wq("footnoteRef"), _wq("endnoteRef"),
    }
)
_REVISION_ID_TAGS = tuple(
    _wq(n) for n in (
        "ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange", "sectPrChange",
        "tblPrChange", "trPrChange", "tcPrChange", "numberingChange",
        "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
    )
)
# CT_Settings children that must follow w:trackRevisions (schema order).
_AFTER_TRACK_REVISIONS = frozenset(
    _wq(n) for n in (
        "doNotTrackMoves", "doNotTrackFormatting", "documentProtection", "autoFormatOverride",
        "styleLockTheme", "styleLockQFSet", "defaultTabStop", "autoHyphenation",
        "consecutiveHyphenLimit", "hyphenationZone", "doNotHyphenateCaps", "showEnvelope",
        "summaryLength", "clickAndTypeStyle", "defaultTableStyle", "evenAndOddHeaders",
        "bookFoldRevPrinting", "bookFoldPrinting", "bookFoldPrintingSheets",
        "drawingGridHorizontalSpacing", "drawingGridVerticalSpacing",
        "displayHorizontalDrawingGridEvery", "displayVerticalDrawingGridEvery",
        "doNotUseMarginsForDrawingGridOrigin", "drawingGridHorizontalOrigin",
        "drawingGridVerticalOrigin", "doNotShadeFormData", "noPunctuationKerning",
        "characterSpacingControl", "printTwoOnOne", "strictFirstAndLastChars",
        "noLineBreaksAfter", "noLineBreaksBefore", "savePreviewPicture",
        "doNotValidateAgainstSchema", "saveInvalidXml", "ignoreMixedContent",
        "alwaysShowPlaceholderText", "doNotDemarcateInvalidXml", "saveXmlDataOnly",
        "useXSLTWhenSaving", "saveThroughXslt", "showXMLTags", "alwaysMergeEmptyNamespace",
        "updateFields", "hdrShapeDefaults", "footnotePr", "endnotePr", "compat", "docVars",
        "rsids", "mathPr", "attachedSchema", "themeFontLang", "clrSchemeMapping",
        "doNotIncludeSubdocsInStats", "doNotAutoCompressPictures", "forceUpgrade", "captions",
        "readModeInkLockDown", "smartTagType", "schemaLibrary", "shapeDefaults",
        "doNotEmbedSmartTags", "decimalSymbol", "listSeparator",
    )
)


class RedlineRefused(ValueError):
    """The document must not be redlined (e.g. it is protected)."""


def _utc_stamp(timestamp: str | None) -> str:
    if timestamp:
        return timestamp
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _oxml(tag: str) -> Any:
    from docx.oxml import OxmlElement

    return OxmlElement(tag)


def _set_text(el: Any, text: str) -> None:
    el.text = text
    el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


# ---------------------------------------------------------------------------
# Runs as sequences of atoms
# ---------------------------------------------------------------------------


def _child_text(child: Any) -> Optional[str]:
    if child.tag == _T:
        return child.text or ""
    return _ONE_CHAR.get(child.tag)


def _run_text(r_el: Any) -> str:
    return "".join(_child_text(c) or "" for c in r_el if c.tag != _RPR)


def _new_run_like(r_el: Any) -> Any:
    run = _oxml("w:r")
    for key, value in r_el.attrib.items():
        run.set(key, value)
    rpr = r_el.find(_RPR)
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    return run


def _split_run(r_el: Any, offset: int, *, zero_width_left: bool) -> tuple[Any, Any]:
    """Split a run at ``offset`` (in run-text coordinates), element by element.

    Returns ``(left, right)``; ``left`` is a new run inserted before ``r_el``,
    ``right`` is ``r_el``. Tabs and breaks move whole; a ``w:t`` straddling the
    offset is cut in two; zero-width children exactly at the offset go left
    when ``zero_width_left`` else right — the caller passes whichever keeps
    them outside the span being isolated.
    """
    left = _new_run_like(r_el)
    pos = 0
    for child in list(r_el):
        if child.tag == _RPR:
            continue
        text = _child_text(child)
        length = len(text) if text is not None else 0
        if length == 0:
            if pos < offset or (pos == offset and zero_width_left):
                left.append(child)
            continue
        if pos + length <= offset:
            left.append(child)
        elif pos < offset:
            cut = offset - pos
            piece = _oxml("w:t")
            _set_text(piece, text[:cut])
            left.append(piece)
            _set_text(child, text[cut:])
        pos += length
    r_el.addprevious(left)
    return left, r_el


def _partition_keep_out(r_el: Any) -> list[tuple[Any, bool]]:
    """Split a run so each reference/field/drawing child sits in a run of its
    own; returns ``[(run, holds_text)]`` in order, replacing ``r_el``."""
    if not any(c.tag in _KEEP_OUT for c in r_el):
        return [(r_el, True)]
    pieces: list[tuple[Any, bool]] = []
    current = None
    current_is_text = None
    for child in list(r_el):
        if child.tag == _RPR:
            continue
        is_text = child.tag not in _KEEP_OUT
        if current is None or current_is_text != is_text:
            current = _new_run_like(r_el)
            current_is_text = is_text
            pieces.append((current, is_text))
        current.append(child)
    for piece, _ in pieces:
        r_el.addprevious(piece)
    r_el.getparent().remove(r_el)
    return pieces


def _entries(p_el: Any) -> list[tuple[str, Any, str]]:
    """``(kind, element, text)`` for a paragraph's content in order: matchable
    runs (``run``) and existing revisions (``locked``, one sentinel char)."""
    out: list[tuple[str, Any, str]] = []

    def walk(node: Any) -> None:
        for child in node:
            tag = child.tag
            if tag == _R:
                out.append(("run", child, _run_text(child)))
            elif tag in _REVISIONS:
                out.append(("locked", child, _LOCKED))
            elif tag in _WALK_INTO:
                walk(child)
            elif tag == _wq("sdt"):
                content = child.find(_wq("sdtContent"))
                if content is not None:
                    walk(content)

    walk(p_el)
    return out


def _paragraph_text(p_el: Any) -> str:
    return "".join(text for _, _, text in _entries(p_el))


def _isolate(p_el: Any, start: int, end: int) -> list[Any]:
    """Split boundary runs so ``[start, end)`` is covered by whole runs, and
    keep reference/field/drawing children out of the covered runs."""
    covered: list[Any] = []
    pos = 0
    for kind, el, text in _entries(p_el):
        r_start, r_end = pos, pos + len(text)
        pos = r_end
        if kind != "run" or r_end <= start or r_start >= end or r_start == r_end:
            continue
        run = el
        local_start = max(start - r_start, 0)
        local_end = min(end - r_start, len(text))
        if local_start > 0:
            _, run = _split_run(run, local_start, zero_width_left=True)
            local_end -= local_start
        if local_end < len(_run_text(run)):
            run, _ = _split_run(run, local_end, zero_width_left=False)
        for piece, holds_text in _partition_keep_out(run):
            if holds_text and _run_text(piece):
                covered.append(piece)
    return covered


def _groups(runs: list[Any]) -> list[list[Any]]:
    """Consecutive sibling runs, so one wrapper never spans two parents."""
    groups: list[list[Any]] = []
    for run in runs:
        if groups and groups[-1][-1].getnext() is run:
            groups[-1].append(run)
        else:
            groups.append([run])
    return groups


def _make_tracked(tag: str, rev_id: int, author: str, date: str) -> Any:
    from docx.oxml.ns import qn

    el = _oxml(tag)
    el.set(qn("w:id"), str(rev_id))
    el.set(qn("w:author"), author)
    el.set(qn("w:date"), date)
    return el


class _Revs:
    """Revision-id allocator and counters for one redline call."""

    def __init__(self, document_el: Any):
        from docx.oxml.ns import qn

        max_id = 0
        for el in document_el.iter(*_REVISION_ID_TAGS):
            try:
                max_id = max(max_id, int(el.get(qn("w:id"), "0")))
            except ValueError:
                continue
        self.next_id = max_id + 1
        self.count = 0

    def take(self) -> int:
        rid = self.next_id
        self.next_id += 1
        self.count += 1
        return rid


def _wrap_del(runs: list[Any], revs: _Revs, author: str, date: str) -> list[Any]:
    """Wrap ``runs`` in ``w:del`` (one per sibling group), converting text to
    ``w:delText`` — Word rejects ``w:t`` inside ``w:del``."""
    wrappers = []
    for group in _groups(runs):
        del_el = _make_tracked("w:del", revs.take(), author, date)
        group[0].addprevious(del_el)
        for run in group:
            del_el.append(run)
            for t in run.findall(_T):
                t.tag = _wq("delText")
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            for instr in run.findall(_wq("instrText")):
                instr.tag = _wq("delInstrText")
        wrappers.append(del_el)
    return wrappers


def _text_run(text: str, rpr_source: Any) -> Any:
    """One run carrying ``text``, with ``\\t`` / ``\\n`` as real tab/break elements."""
    run = _oxml("w:r")
    if rpr_source is not None:
        rpr = rpr_source.find(_RPR)
        if rpr is not None:
            run.append(copy.deepcopy(rpr))
    for piece in re.split(r"(\t|\n)", text):
        if piece == "\t":
            run.append(_oxml("w:tab"))
        elif piece == "\n":
            run.append(_oxml("w:br"))
        elif piece:
            t = _oxml("w:t")
            _set_text(t, piece)
            run.append(t)
    return run


def _make_ins(text: str, rpr_source: Any, revs: _Revs, author: str, date: str) -> Any:
    ins = _make_tracked("w:ins", revs.take(), author, date)
    ins.append(_text_run(text, rpr_source))
    return ins


def _mark_paragraph(p_el: Any, tag: str, revs: _Revs, author: str, date: str) -> None:
    """Track the paragraph mark itself (``w:pPr/w:rPr/w:ins|w:del``)."""
    ppr = p_el.find(_PPR)
    if ppr is None:
        ppr = _oxml("w:pPr")
        p_el.insert(0, ppr)
    rpr = ppr.find(_RPR)
    if rpr is None:
        rpr = _oxml("w:rPr")
        # rPr comes after the other pPr children except sectPr / pPrChange.
        tail = ppr.find(_wq("sectPr"))
        if tail is None:
            tail = ppr.find(_wq("pPrChange"))
        if tail is not None:
            tail.addprevious(rpr)
        else:
            ppr.append(rpr)
    rpr.insert(0, _make_tracked(tag, revs.take(), author, date))


def _ensure_comment_reference_style(doc: Any) -> None:
    """Define the ``CommentReference`` character style the reference run uses.

    python-docx's ``add_comment`` (and this module's anchor run) point at the
    style without defining it when the document lacks one — an undefined
    ``rStyle`` the integrity check flags and Word silently ignores.
    """
    from docx.oxml import parse_xml

    styles = doc.styles.element
    for style in styles.iter(_wq("style")):
        if style.get(_wq("styleId")) == "CommentReference":
            return
    based_on = ""
    if any(st.get(_wq("styleId")) == "DefaultParagraphFont" for st in styles.iter(_wq("style"))):
        based_on = '<w:basedOn w:val="DefaultParagraphFont"/>'
    styles.append(parse_xml(
        f'<w:style xmlns:w="{_W_NS}" w:type="character" w:styleId="CommentReference">'
        f'<w:name w:val="annotation reference"/>{based_on}<w:uiPriority w:val="99"/>'
        '<w:semiHidden/><w:unhideWhenUsed/><w:rPr><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr></w:style>'
    ))


def _mark_comment_range(first_el: Any, last_el: Any, comment_id: int) -> None:
    """Anchor comment ``comment_id`` on ``[first_el, last_el]``; markers are
    siblings of the runs/revisions they annotate, as Word writes them."""
    from docx.oxml.ns import qn

    start = _oxml("w:commentRangeStart")
    start.set(qn("w:id"), str(comment_id))
    first_el.addprevious(start)
    end = _oxml("w:commentRangeEnd")
    end.set(qn("w:id"), str(comment_id))
    last_el.addnext(end)
    ref_run = _oxml("w:r")
    rpr = _oxml("w:rPr")
    rstyle = _oxml("w:rStyle")
    rstyle.set(qn("w:val"), "CommentReference")
    rpr.append(rstyle)
    ref_run.append(rpr)
    ref = _oxml("w:commentReference")
    ref.set(qn("w:id"), str(comment_id))
    ref_run.append(ref)
    end.addnext(ref_run)


# ---------------------------------------------------------------------------
# Settings: tracking on, protection respected
# ---------------------------------------------------------------------------


def _protection(doc: Any) -> Optional[str]:
    """The enforced protection mode that forbids this edit, or ``None``.

    ``trackedChanges`` protection is the one mode a redline satisfies — it
    exists to force exactly the tracked revisions this module writes — so it
    is allowed; read-only, comments-only and forms-only are refused.
    """
    from docx.oxml.ns import qn

    prot = doc.settings.element.find(qn("w:documentProtection"))
    if prot is None:
        return None
    enforced = (prot.get(qn("w:enforcement")) or "0").lower() in {"1", "true", "on"}
    edit = prot.get(qn("w:edit")) or "readOnly"
    if not enforced or edit in {"none", "trackedChanges"}:
        return None
    return edit


def _enable_track_revisions(doc: Any) -> bool:
    """Turn on Track Changes so the counterparty's next edits are tracked too."""
    settings = doc.settings.element
    if settings.find(_wq("trackRevisions")) is not None:
        return False
    track = _oxml("w:trackRevisions")
    for child in settings:
        if child.tag in _AFTER_TRACK_REVISIONS:
            child.addprevious(track)
            break
    else:
        settings.append(track)
    return True


# ---------------------------------------------------------------------------
# Scope and anchoring
# ---------------------------------------------------------------------------


def _in_textbox(el: Any) -> bool:
    anc = el.getparent()
    while anc is not None:
        if anc.tag in (_wq("txbxContent"), f"{{{_MC_NS}}}AlternateContent"):
            return True
        anc = anc.getparent()
    return False


def _body_paragraphs(doc: Any) -> list[Any]:
    """Every paragraph of the body in order (tables included, text boxes not)."""
    return [p for p in doc.element.body.iter(_P) if not _in_textbox(p)]


def _clause_scope(doc: Any, source_path: Path, query: str) -> tuple[Optional[list[Any]], str, list[Any]]:
    """(paragraph elements, resolved label, blocks) for a clause label, or
    ``(None, hint, [])`` when nothing matches."""
    from .docx_reader import read_docx

    reading = read_docx(source_path, document_root=doc.element)
    clauses = reading.clauses()
    from .clauses import find_clauses

    hits = find_clauses(clauses, query)
    if not hits:
        labels = [c.heading()[:30] for c in clauses if c.depth <= 1][:12]
        hint = "available clauses: " + "; ".join(labels) if labels else "the document has no labelled clauses"
        return None, hint, []
    clause = hits[0]
    blocks = reading.blocks[clause.start:clause.end]
    paragraphs: list[Any] = []
    for block in blocks:
        if block.kind == "table":
            paragraphs.extend(p for p in block.element.iter(_P) if not _in_textbox(p))
        elif block.element is not None:
            paragraphs.append(block.element)
    note = clause.heading()
    if len(hits) > 1:
        note += f" (first of {len(hits)} clauses with this label)"
    return paragraphs, note, blocks


def _find_spans(text: str, find: str) -> tuple[list[tuple[int, int]], bool]:
    """Exact, non-overlapping spans; failing that, a match where each run of
    whitespace in ``find`` stands for any whitespace (a tab in the document
    reads as a space in most renderings)."""
    spans: list[tuple[int, int]] = []
    i = text.find(find)
    while i >= 0 and find:
        spans.append((i, i + len(find)))
        i = text.find(find, i + len(find))
    if spans or not find.strip():
        return spans, False
    parts = find.split()
    if len(parts) < 2 and find == find.strip():
        return [], False
    pattern = r"\s+".join(re.escape(p) for p in parts)
    if find[:1].isspace():
        pattern = r"\s+" + pattern
    if find[-1:].isspace():
        pattern = pattern + r"\s+"
    return [m.span() for m in re.finditer(pattern, text)], True


def _strip_label(text: str, label: str) -> str:
    """``text`` without a leading copy of ``label`` (as written or in another
    form of the same number: 第1条 for 第一条)."""
    from .clauses import detect_label

    stripped = text.lstrip()
    if stripped.startswith(label):
        return stripped[len(label):].lstrip()
    own = detect_label(stripped)
    target = detect_label(label + " x")
    if own is not None and target is not None and (own.family, own.number) == (target.family, target.number):
        return stripped[len(own.label):].lstrip()
    return text


def _anchor_past_label(doc: Any, source_path: Path, find: str, scope: list[Any]):
    """``(label, paragraph, rest, spans)`` when ``find`` = an auto-numbering
    label + text of exactly one paragraph in scope that carries that label."""
    from .clauses import detect_label
    from .docx_reader import read_docx

    wanted = detect_label(find.strip())
    if wanted is None:
        return None
    try:
        reading = read_docx(source_path, document_root=doc.element)
    except Exception:  # noqa: BLE001
        return None
    in_scope = set(scope)
    hits = []
    for block in reading.blocks:
        label = block.label()
        if block.kind != "paragraph" or not label or block.element not in in_scope:
            continue
        own = detect_label(label + " x")
        if own is None or (own.family, own.number) != (wanted.family, wanted.number):
            continue
        rest = _strip_label(find, label)
        if not rest:
            continue
        spans, _ = _find_spans(_paragraph_text(block.element), rest)
        if spans:
            hits.append((label, block.element, rest, spans))
    return hits[0] if len(hits) == 1 else None


def _author_for(change: dict[str, Any], author: str, category_authors: dict[str, str]) -> str:
    if change.get("author"):
        return str(change["author"])
    category = str(change.get("category") or "").strip()
    if category:
        return category_authors.get(category) or f"{author} ({category})"
    return author


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_OPS = ("replace", "delete", "insert_after", "insert_before", "comment")


def redline_docx_document(
    source_path: Path,
    output_path: Path,
    edits: list[dict[str, Any]],
    *,
    author: str = DEFAULT_AUTHOR,
    initials: str = "AI",
    timestamp: str | None = None,
    category_authors: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Copy a .docx applying ``edits`` as native tracked changes + comments.

    Each edit is a dict:

    - ``op`` — ``replace`` (default) | ``delete`` | ``insert_after`` |
      ``insert_before`` | ``comment``
    - ``find`` — exact text within one paragraph. Omit it to act on a whole
      paragraph or clause: delete it, insert new paragraph(s) after/before
      it, replace a paragraph's text, or comment on it.
    - ``replace`` — replacement text (``replace``; empty means delete)
    - ``text`` — inserted text (``insert_*``; ``\\n`` starts a new paragraph
      when inserting whole paragraphs) or the comment body (``comment``)
    - ``clause`` — scope to a clause by label as Word shows it (第三条 = 第3条,
      "3.2", "Article 5") or by heading words
    - ``paragraph`` — scope to one body paragraph by its 0-based index from
      ``inspect_docx`` (indices refer to the document before this call)
    - ``occurrence`` — 1-based: change only the n-th match in scope
    - ``comment`` — margin note attached to the change
    - ``category`` / ``author`` — attribution: ``category_authors`` maps a
      category ("substantive", "drafting") to an author name

    Returns per-edit counts, revision/comment totals, and ``not_found``
    entries carrying a ``reason`` and a ``hint`` — never a silent miss.
    Raises :class:`RedlineRefused` for a protected document.
    """
    from docx import Document

    from .office import docx_find_diagnosis, strip_markdown_emphasis

    source_path = Path(source_path)
    output_path = Path(output_path)
    doc = Document(str(source_path))
    protection = _protection(doc)
    if protection:
        raise RedlineRefused(
            f"the document is protected (w:documentProtection edit={protection}, enforced): its author "
            "restricted editing, and writing revisions into it would bypass that restriction. Ask the "
            "user for an unprotected copy, or for the password-free version they can edit."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    date = _utc_stamp(timestamp)
    revs = _Revs(doc.element)
    category_authors = {str(k): str(v) for k, v in (category_authors or {}).items()}
    body_paragraphs = list(doc.paragraphs)
    body_index = {p._p: i for i, p in enumerate(body_paragraphs)}
    all_paragraphs = _body_paragraphs(doc)

    applied: list[dict[str, Any]] = []
    not_found: list[dict[str, Any]] = []
    comment_count = 0

    def add_comment(first: Any, last: Any, note: str, who: str) -> None:
        nonlocal comment_count
        if not comment_count:
            _ensure_comment_reference_style(doc)
        comment = doc.comments.add_comment(text=note, author=who, initials=initials)
        _mark_comment_range(first, last, comment.comment_id)
        comment_count += 1

    for number, edit in enumerate(edits, start=1):
        op = str(edit.get("op") or "replace").strip()
        if op not in _OPS:
            raise ValueError(f"Unknown redline op {op!r} (edit {number}); valid ops: {', '.join(_OPS)}")
        find = str(edit.get("find") or "")
        replace = str(edit.get("replace") or "")
        text = str(edit.get("text") or "")
        margin_note = str(edit.get("comment") or "")
        if op == "comment":
            margin_note = margin_note or text
            if not margin_note:
                raise ValueError(f"comment edit {number} needs 'text' (the comment body)")
        if op == "replace" and not replace and (find or not text):
            op = "delete"
        who = _author_for(edit, author, category_authors)
        clause_query = str(edit.get("clause") or "").strip()
        para_raw = edit.get("paragraph")
        occurrence = int(edit.get("occurrence") or 0)
        entry: dict[str, Any] = {"edit": number, "op": op, "author": who}
        if find:
            entry["find"] = find

        # -- resolve scope --------------------------------------------------
        scope: list[Any]
        scope_blocks: list[Any] = []
        if para_raw is not None and str(para_raw).strip() != "":
            idx = int(para_raw)
            if not 0 <= idx < len(body_paragraphs):
                not_found.append({**entry, "reason": "paragraph_out_of_range",
                                  "hint": f"paragraph must be 0..{len(body_paragraphs) - 1} (inspect_docx lists them)"})
                continue
            scope = [body_paragraphs[idx]._p]
            entry["scope"] = f"paragraph {idx}"
        elif clause_query:
            resolved, note, scope_blocks = _clause_scope(doc, source_path, clause_query)
            if resolved is None:
                not_found.append({**entry, "clause": clause_query, "reason": "clause_not_found", "hint": note})
                continue
            scope = resolved
            entry["scope"] = f"clause {note}"
        else:
            if not find:
                raise ValueError(f"edit {number} needs 'find', or a 'paragraph'/'clause' to act on whole")
            scope = all_paragraphs

        # -- whole-paragraph / whole-clause operations ----------------------
        if not find:
            targets = [p for p in scope if not _in_textbox(p)]
            if not targets:
                not_found.append({**entry, "reason": "empty_scope", "hint": "the paragraph/clause holds no editable paragraph"})
                continue
            if op == "delete":
                for p_el in targets:
                    runs = [el for kind, el, t in _entries(p_el) if kind == "run"]
                    if runs:
                        _wrap_del(runs, revs, who, date)
                    # The mark goes too, so accepting removes the paragraph rather
                    # than leaving it empty. Never the last mark of the body (Word
                    # needs it) and never inside a table cell (a cell keeps one).
                    nxt = p_el.getnext()
                    if body_index.get(p_el) is not None and nxt is not None and nxt.tag in (_P, _wq("tbl"), _wq("sdt")):
                        _mark_paragraph(p_el, "w:del", revs, who, date)
                for block in scope_blocks:
                    if block.kind == "table":
                        for tr in block.element.findall(_wq("tr")):
                            trpr = tr.find(_wq("trPr"))
                            if trpr is None:
                                trpr = _oxml("w:trPr")
                                prefix = tr.find(_wq("tblPrEx"))
                                if prefix is not None:
                                    prefix.addnext(trpr)
                                else:
                                    tr.insert(0, trpr)
                            trpr.append(_make_tracked("w:del", revs.take(), who, date))
                entry["count"] = len(targets)
                if margin_note and targets:
                    first_runs = [el for kind, el, _ in _entries(targets[0]) if kind in {"run", "locked"}]
                    if first_runs:
                        add_comment(first_runs[0], first_runs[-1], margin_note, who)
            elif op in {"insert_after", "insert_before"}:
                if not text:
                    raise ValueError(f"edit {number} ({op}) needs 'text'")
                anchor = targets[-1] if op == "insert_after" else targets[0]
                if scope_blocks and op == "insert_after" and scope_blocks[-1].kind == "table":
                    anchor = scope_blocks[-1].element
                inserted = _insert_paragraphs(anchor, text, after=(op == "insert_after"), revs=revs, author=who, date=date,
                                              style=str(edit.get("style") or ""), doc=doc)
                entry["count"] = len(inserted)
                if margin_note and inserted:
                    ins_els = [el for el in inserted[0] if el.tag == _INS]
                    if ins_els:
                        add_comment(ins_els[0], ins_els[-1], margin_note, who)
            elif op == "replace":
                if len(targets) != 1:
                    raise ValueError(
                        f"edit {number}: replacing a whole clause needs 'find' (the words to change) or "
                        "'paragraph' (one paragraph to rewrite)"
                    )
                p_el = targets[0]
                runs = [el for kind, el, t in _entries(p_el) if kind == "run" and t]
                first_rpr = runs[0] if runs else None
                text_runs: list[Any] = []
                for run in runs:
                    text_runs.extend(piece for piece, holds in _partition_keep_out(run) if holds and _run_text(piece))
                wrappers = _wrap_del(text_runs, revs, who, date) if text_runs else []
                ins = _make_ins(replace or text, first_rpr, revs, who, date)
                if wrappers:
                    wrappers[-1].addnext(ins)
                else:
                    ppr = p_el.find(_PPR)
                    (ppr.addnext if ppr is not None else (lambda x: p_el.insert(0, x)))(ins)
                entry["count"] = 1
                if margin_note:
                    add_comment(wrappers[0] if wrappers else ins, ins, margin_note, who)
            else:  # comment on the whole paragraph / clause heading
                p_el = targets[0]
                content = [el for kind, el, _ in _entries(p_el)]
                if not content:
                    not_found.append({**entry, "reason": "empty_paragraph", "hint": "the paragraph has no text to anchor a comment on"})
                    continue
                add_comment(content[0], content[-1], margin_note, who)
                entry["count"] = 1
            applied.append(entry)
            continue

        # -- text-anchored operations ---------------------------------------
        matches: list[tuple[Any, tuple[int, int]]] = []
        normalized = False
        texts = [(p_el, _paragraph_text(p_el)) for p_el in scope]
        for p_el, para_text in texts:
            spans, loose = _find_spans(para_text, find)
            normalized = normalized or (loose and bool(spans))
            matches.extend((p_el, span) for span in spans)
        unmarked = strip_markdown_emphasis(find)
        if not matches and unmarked != find:
            # The model copied read_document's **bold** markers into find.
            for p_el, para_text in texts:
                spans, loose = _find_spans(para_text, unmarked)
                matches.extend((p_el, span) for span in spans)
            if matches:
                entry["matched_without_markdown"] = unmarked
        if not matches:
            # The commonest miss: find opens with a numbering label (第一条, 1.1)
            # that Word renders but the XML does not hold. When exactly one
            # paragraph in scope carries that label and the rest of find, anchor
            # there on the rest; the label stays Word's.
            relabelled = _anchor_past_label(doc, source_path, unmarked, scope)
            if relabelled is not None:
                label, p_el, rest, spans = relabelled
                matches = [(p_el, span) for span in spans]
                entry["anchored_after_label"] = label
                if op == "replace":
                    replace = _strip_label(replace, label)
        if occurrence > 0:
            matches = matches[occurrence - 1:occurrence]
        if not matches:
            reason, hint = docx_find_diagnosis(source_path, find)
            if occurrence > 0 and reason == "not_found":
                hint = f"there are fewer than {occurrence} matches in scope"
            elif reason == "not_found" and (clause_query or para_raw is not None):
                hint = f"no exact match inside {entry.get('scope', 'the scope')}; " + hint
            not_found.append({**entry, "reason": reason, "hint": hint})
            entry["count"] = 0
            applied.append(entry)
            continue
        if normalized:
            entry["matched_with_whitespace_normalized"] = True
        by_paragraph: dict[int, list[tuple[int, int]]] = {}
        order: list[Any] = []
        for p_el, span in matches:
            key = id(p_el)
            if key not in by_paragraph:
                by_paragraph[key] = []
                order.append(p_el)
            by_paragraph[key].append(span)
        count = 0
        touched: list[int] = []
        for p_el in order:
            for start, end in sorted(by_paragraph[id(p_el)], reverse=True):
                covered = _isolate(p_el, start, end)
                if not covered:
                    continue
                first = last = None
                if op in {"replace", "delete"}:
                    wrappers = _wrap_del(covered, revs, who, date)
                    first, last = wrappers[0], wrappers[-1]
                    if op == "replace":
                        ins = _make_ins(replace, covered[0], revs, who, date)
                        wrappers[-1].addnext(ins)
                        last = ins
                elif op == "insert_after":
                    ins = _make_ins(text, covered[-1], revs, who, date)
                    covered[-1].addnext(ins)
                    first = last = ins
                elif op == "insert_before":
                    ins = _make_ins(text, covered[0], revs, who, date)
                    covered[0].addprevious(ins)
                    first = last = ins
                else:
                    first, last = covered[0], covered[-1]
                if margin_note:
                    add_comment(first, last, margin_note, who)
                count += 1
            if body_index.get(p_el) is not None:
                touched.append(body_index[p_el])
        entry["count"] = count
        if touched:
            entry["paragraphs"] = sorted(set(touched))
        applied.append(entry)

    tracking_enabled = _enable_track_revisions(doc)
    doc.save(str(output_path))
    return {
        "output_path": str(output_path),
        "author": author,
        "date": date,
        "applied": applied,
        "revision_count": revs.count,
        "comment_count": comment_count,
        "not_found": not_found,
        "track_changes_turned_on": tracking_enabled,
    }


def _insert_paragraphs(anchor: Any, text: str, *, after: bool, revs: _Revs, author: str, date: str, style: str, doc: Any) -> list[Any]:
    """Insert one tracked paragraph per line of ``text`` next to ``anchor``.

    Each new paragraph copies the anchor's paragraph properties *without* its
    numbering (an inserted body paragraph must not renumber the clauses after
    it), and both its text and its paragraph mark are tracked insertions, so
    rejecting removes it cleanly.
    """
    from docx.oxml.ns import qn

    source_ppr = anchor.find(_PPR) if anchor.tag == _P else None
    rpr_source = None
    if anchor.tag == _P:
        rpr_source = next((el for kind, el, t in _entries(anchor) if kind == "run" and t), None)
    created: list[Any] = []
    lines = text.split("\n")
    point = anchor
    for line in lines:
        p_el = _oxml("w:p")
        if source_ppr is not None:
            ppr = copy.deepcopy(source_ppr)
            for removable in (_wq("numPr"), _wq("sectPr"), _wq("pPrChange"), _RPR):
                for child in ppr.findall(removable):
                    ppr.remove(child)
            p_el.append(ppr)
        if style:
            ppr = p_el.find(_PPR)
            if ppr is None:
                ppr = _oxml("w:pPr")
                p_el.insert(0, ppr)
            for child in ppr.findall(_wq("pStyle")):
                ppr.remove(child)
            pstyle = _oxml("w:pStyle")
            try:
                pstyle.set(qn("w:val"), doc.styles[style].style_id)
            except KeyError:
                pstyle.set(qn("w:val"), style)
            ppr.insert(0, pstyle)
        _mark_paragraph(p_el, "w:ins", revs, author, date)
        if line:
            p_el.append(_make_ins(line, rpr_source, revs, author, date))
        if after:
            point.addnext(p_el)
            point = p_el
        else:
            anchor.addprevious(p_el)
        created.append(p_el)
    return created


def extract_docx_revisions(path: Path) -> list[dict[str, Any]]:
    """List tracked changes in a .docx: type, author, date, text, paragraph.

    ``paragraph`` is the 0-based body paragraph index (``inspect_docx``'s
    numbering) when the change sits in a body paragraph, else ``None`` (table
    cell, text box). Works on any .docx with the stdlib.
    """
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body = root.find(_wq("body"))
    if body is None:
        return []
    body_index = {id(p): i for i, p in enumerate(c for c in body if c.tag == _P)}
    parents = {child: parent for parent in root.iter() for child in parent}

    def paragraph_of(el: Any) -> Optional[Any]:
        node = parents.get(el)
        while node is not None and node.tag != _P:
            node = parents.get(node)
        return node

    out: list[dict[str, Any]] = []
    for el in root.iter():
        if el.tag not in _REVISIONS:
            continue
        parent = parents.get(el)
        if parent is not None and parent.tag == _RPR:
            kind = "paragraph_insertion" if el.tag == _INS else "paragraph_deletion"
            text = ""
        elif el.tag in (_INS, _MOVE_TO):
            kind = "insertion" if el.tag == _INS else "move_to"
            text = "".join(_child_text(t) or "" for t in el.iter() if t.tag in (_T, *_ONE_CHAR))
        else:
            kind = "deletion" if el.tag == _DEL else "move_from"
            text = "".join(
                (t.text or "") if t.tag == _wq("delText") else (_ONE_CHAR.get(t.tag) or "")
                for t in el.iter() if t.tag == _wq("delText") or t.tag in _ONE_CHAR
            )
        p = paragraph_of(el)
        out.append(
            {
                "type": kind,
                "id": el.get(_wq("id"), ""),
                "author": el.get(_wq("author"), ""),
                "date": el.get(_wq("date"), ""),
                "text": text,
                "paragraph": body_index.get(id(p)) if p is not None else None,
            }
        )
    return out


def _visible_text(el: Any, mode: str, in_ins: bool = False, in_del: bool = False) -> str:
    parts: list[str] = []
    for child in el:
        tag = child.tag
        if tag in (_P, _wq("txbxContent"), _wq("drawing"), _wq("pict"), f"{{{_MC_NS}}}AlternateContent", _PPR, _RPR):
            continue
        if tag in (_INS, _MOVE_TO):
            parts.append(_visible_text(child, mode, True, in_del))
            continue
        if tag in (_DEL, _MOVE_FROM):
            parts.append(_visible_text(child, mode, in_ins, True))
            continue
        visible = (not in_ins and not in_del) or (in_ins and not in_del and mode == "accept") or (in_del and mode == "reject")
        if tag == _T:
            if visible:
                parts.append(child.text or "")
        elif tag == _wq("delText"):
            if in_del and mode == "reject":
                parts.append(child.text or "")
        elif tag in _ONE_CHAR:
            if visible:
                parts.append(_ONE_CHAR[tag])
        else:
            parts.append(_visible_text(child, mode, in_ins, in_del))
    return "".join(parts)


def _ordered_paragraphs(container: Any) -> Iterable[Any]:
    for child in container:
        if child.tag == _P:
            yield child
        elif child.tag == _wq("tbl"):
            for tr in child.findall(_wq("tr")):
                for tc in tr.findall(_wq("tc")):
                    yield from _ordered_paragraphs(tc)
        elif child.tag in (_wq("sdt"),):
            content = child.find(_wq("sdtContent"))
            if content is not None:
                yield from _ordered_paragraphs(content)
        elif child.tag == _wq("customXml"):
            yield from _ordered_paragraphs(child)


def preview_docx_revisions(path: Path, mode: str = "accept") -> str:
    """Document text as if every revision were accepted (``accept``) or
    rejected (``reject``), one line per paragraph, tabs as ``\\t``.

    Paragraph marks count: accepting a deleted mark (or rejecting an inserted
    one) joins that paragraph to the next, which is how a tracked paragraph
    insertion disappears on reject. This is the round-trip contract every
    redline is tested against — accept-all must equal the intended revision,
    reject-all the original.
    """
    if mode not in ("accept", "reject"):
        raise ValueError(f"mode must be 'accept' or 'reject', got {mode!r}")
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body = root.find(_wq("body"))
    if body is None:
        return ""
    lines: list[str] = []
    carry = ""
    for p in _ordered_paragraphs(body):
        text = carry + _visible_text(p, mode)
        ppr = p.find(_PPR)
        mark = ppr.find(_RPR) if ppr is not None else None
        mark_ins = mark is not None and mark.find(_INS) is not None
        mark_del = mark is not None and mark.find(_DEL) is not None
        if (mode == "accept" and mark_del) or (mode == "reject" and mark_ins):
            carry = text
            continue
        lines.append(text)
        carry = ""
    if carry:
        lines.append(carry)
    return "\n".join(lines)


__all__ = [
    "DEFAULT_AUTHOR",
    "RedlineRefused",
    "extract_docx_revisions",
    "preview_docx_revisions",
    "redline_docx_document",
]
