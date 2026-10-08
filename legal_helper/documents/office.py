"""Office-file inspect / extract / edit helpers (DOCX, PPTX, XLSX).

Polished writers live in ``legal_helper.documents.writers``; this module
covers the read-side and in-place edit operations only.
"""

from __future__ import annotations

import re
import posixpath
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _qn(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


def _element_text(el: ET.Element, text_tag: str) -> str:
    return "".join(t.text or "" for t in el.iter(text_tag)).strip()


def _docx_comments(path: Path) -> list[dict[str, str]]:
    try:
        with zipfile.ZipFile(path) as zf:
            comments_xml = zf.read("word/comments.xml")
            document_xml = zf.read("word/document.xml")
    except KeyError:
        return []

    comments_root = ET.fromstring(comments_xml)
    document_root = ET.fromstring(document_xml)
    comment_tag = _qn(_W_NS, "comment")
    id_attr = _qn(_W_NS, "id")
    author_attr = _qn(_W_NS, "author")
    date_attr = _qn(_W_NS, "date")
    text_tag = _qn(_W_NS, "t")
    para_tag = _qn(_W_NS, "p")
    range_start_tag = _qn(_W_NS, "commentRangeStart")

    anchors: dict[str, str] = {}
    for para in document_root.iter(para_tag):
        ids = {
            marker.attrib.get(id_attr, "")
            for marker in para.iter(range_start_tag)
            if marker.attrib.get(id_attr)
        }
        if not ids:
            continue
        para_text = _element_text(para, text_tag)
        for comment_id in ids:
            if para_text:
                anchors.setdefault(comment_id, para_text)

    out: list[dict[str, str]] = []
    for comment in comments_root.iter(comment_tag):
        comment_id = comment.attrib.get(id_attr, "")
        text = _element_text(comment, text_tag)
        if not text:
            continue
        out.append(
            {
                "id": comment_id,
                "author": comment.attrib.get(author_attr, ""),
                "date": comment.attrib.get(date_attr, ""),
                "text": text,
                "anchor": anchors.get(comment_id, ""),
            }
        )
    return out


def _docx_text_via_python_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text:
            parts.append(p.text)
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _format_comments(comments: list[dict[str, str]]) -> list[str]:
    out = ["\n## Word comments / annotations"]
    for item in comments:
        label = f"Comment {item['id']}".strip()
        meta = ", ".join(v for v in [item.get("author"), item.get("date")] if v)
        if meta:
            label = f"{label} ({meta})"
        out.append(f"- {label}: {item['text']}")
        if item.get("anchor"):
            out.append(f"  Anchor/context: {item['anchor']}")
    return out


def extract_docx_text(path: Path, *, include_comments: bool = True) -> str:
    """Markdown for a .docx as Word shows it: the in-tree reader first.

    :mod:`.docx_reader` renders tracked changes as CriticMarkup, labels
    headers and footers, and keeps auto-numbering labels in their own form
    (第一条, not 第1条) — the three things the tiered converter lost. The
    converter and then python-docx remain as fallbacks for a package the
    reader cannot parse, and say so, because a silent fallback is how the
    losses went unnoticed in the first place.
    """
    from .docx_reader import read_docx

    try:
        return read_docx(path).render(include_comments=include_comments)
    except Exception as exc:  # noqa: BLE001 — fall back, but say so below
        reader_error = f"{type(exc).__name__}: {exc}"
    from ._convert import convert_to_markdown

    try:
        body = convert_to_markdown(path).strip()
    except Exception:
        body = _docx_text_via_python_docx(path)
    if include_comments:
        comments = _docx_comments(path)
        if comments:
            body = body + "\n" + "\n".join(_format_comments(comments))
    note = (
        f"[read with the fallback converter ({reader_error[:200]}); tracked changes, "
        "headers/footers and numbering labels may not be shown as Word shows them]"
    )
    return f"{note}\n\n{body}"


def inspect_docx_document(
    path: Path,
    *,
    paragraph_offset: int = 0,
    paragraph_limit: int = 150,
    preview_chars: int = 80,
) -> dict[str, Any]:
    """Structure of a .docx, with every body paragraph under its edit index.

    ``index`` is the 0-based position in ``Document.paragraphs`` — the index
    ``reshape_docx`` ops and ``redline_docx`` take. Headings are flagged in
    place rather than numbered separately: listing them 1-based in a list of
    their own made a model pass "paragraph 4" to an op that edited the
    paragraph after the one it meant.
    """
    from docx import Document
    from docx.styles import BabelFish

    from .docx_reader import read_docx

    doc = Document(str(path))
    reading = read_docx(path)
    body = reading.body_paragraphs()
    entries: list[dict[str, Any]] = []
    headings: list[dict[str, Any]] = []
    empty = 0
    for block in body:
        text = block.labelled_text()
        flat = " ".join(text.split())
        if not flat and not block.extras:
            empty += 1
            continue
        entry: dict[str, Any] = {"index": block.paragraph}
        if block.style_name and block.style_name.lower() not in {"normal", ""}:
            # The name reshape_docx's set_paragraph_style / insert_paragraph take
            # ("Heading 1"), not the internal one styles.xml stores ("heading 1").
            entry["style"] = BabelFish.internal2ui(block.style_name)
        if block.heading_level:
            entry["heading"] = block.heading_level
            headings.append({"index": block.paragraph, "level": block.heading_level, "text": flat[:120]})
        if block.numbering is not None:
            if block.numbering.is_bullet:
                entry["list"] = "bullet"
            elif block.numbering.label:
                entry["label"] = block.numbering.label
                entry["label_is_auto_numbering"] = True
        if block.has_revisions():
            entry["tracked_revisions"] = block.revision_marks
        if block.extras:
            entry["text_boxes"] = len(block.extras)
        limit = max(20, int(preview_chars))
        entry["text"] = flat if len(flat) <= limit else flat[:limit] + "…"
        entries.append(entry)
    start = max(0, int(paragraph_offset))
    size = int(paragraph_limit)
    window = entries[start:] if size <= 0 else entries[start:start + size]
    tables: list[dict[str, Any]] = []
    last_para = -1
    for block in reading.blocks:
        if block.kind == "paragraph" and block.paragraph is not None:
            last_para = block.paragraph
        elif block.kind == "table" and block.table is not None:
            cols = max((sum(c.span for c in row) for row in block.rows), default=0)
            tables.append({
                "table_index": block.table,
                "rows": len(block.rows),
                "cols": cols,
                "after_paragraph": last_para,
            })
    props = doc.core_properties
    payload: dict[str, Any] = {
        "filename": path.name,
        "paragraph_count": len(doc.paragraphs),
        "nonempty_paragraph_count": len(entries),
        "empty_paragraph_count": empty,
        "table_count": len(doc.tables),
        "section_count": len(doc.sections),
        "comment_count": len(reading.comments),
        "index_note": (
            "index = 0-based position in the document's body paragraphs; reshape_docx "
            "(after/index/indices) and redline_docx (paragraph) take this number. Empty "
            "paragraphs are omitted from the list but keep their index. A label marked "
            "label_is_auto_numbering is Word numbering, not text in the paragraph."
        ),
        "paragraphs": window,
        "headings": headings,
        "tables": tables,
        "core_properties": {
            "title": props.title or "",
            "author": props.author or "",
            "subject": props.subject or "",
            "keywords": props.keywords or "",
            "last_modified_by": props.last_modified_by or "",
        },
    }
    if len(window) < len(entries):
        shown_from = window[0]["index"] if window else None
        shown_to = window[-1]["index"] if window else None
        payload["paragraphs_shown"] = f"{len(window)} of {len(entries)} non-empty (indices {shown_from}–{shown_to})"
        if start + len(window) < len(entries):
            payload["next_paragraph_offset"] = start + len(window)
    revisions = reading.revisions
    if revisions.total or revisions.formatting:
        payload["tracked_changes"] = {
            "insertions": revisions.insertions,
            "deletions": revisions.deletions,
            "moves": revisions.moves,
            "formatting_changes": revisions.formatting,
            "authors": list(revisions.authors),
            "note": "Edit paragraphs that carry tracked changes with redline_docx; edit_docx_text and reshape_docx refuse them.",
        }
    if reading.protection:
        payload["protection"] = reading.protection
    return payload


# -- run-preserving text edits ---------------------------------------------
#
# Every DOCX text edit works on the paragraph's text-bearing *elements*
# (``w:t``, ``w:tab``, ``w:br``), never on whole runs. The previous edit
# collapsed a cross-run match into ``runs[0]`` and blanked the rest, which
# deleted footnote and comment references riding in those runs while
# reporting success; it only saw direct runs, so hyperlink text survived at
# the end of a "replaced" paragraph; and it walked ``row.cells``, which
# repeats a merged cell, so the cell was edited twice.

_W_URI = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC_URI = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _wt(name: str) -> str:
    return f"{{{_W_URI}}}{name}"


_TOKEN_SKIP = frozenset(
    {_wt("pPr"), _wt("del"), _wt("moveFrom"), _wt("p"), _wt("txbxContent"), _wt("tbl")}
)
_GRAPHIC_TAGS = frozenset({_wt("drawing"), _wt("pict"), _wt("object"), f"{{{_MC_URI}}}AlternateContent"})
_REVISION_ELEMENTS = (_wt("ins"), _wt("del"), _wt("moveFrom"), _wt("moveTo"))


def _text_tokens(p_el: Any) -> list[list[Any]]:
    """``[element, text, kind]`` for each text-bearing element of a paragraph,
    in order: ``w:t`` (kind ``t``), ``w:tab`` (``tab``), ``w:br``/``w:cr``
    (``br``). Deleted text and nested (text-box) paragraphs are excluded."""
    tokens: list[list[Any]] = []

    def walk(node: Any) -> None:
        for child in node:
            tag = child.tag
            if tag in _TOKEN_SKIP or tag in _GRAPHIC_TAGS:
                continue
            if tag == _wt("r"):
                for rc in child:
                    if rc.tag == _wt("t"):
                        tokens.append([rc, rc.text or "", "t"])
                    elif rc.tag in (_wt("tab"), _wt("ptab")):
                        tokens.append([rc, "\t", "tab"])
                    elif rc.tag in (_wt("br"), _wt("cr")):
                        tokens.append([rc, "\n", "br"])
                continue
            walk(child)

    walk(p_el)
    return tokens


def _paragraph_revision_count(p_el: Any) -> int:
    """Tracked revisions (ins/del/moveFrom/moveTo, incl. the paragraph mark)
    belonging to this paragraph — not to a text box nested inside it."""
    count = 0
    for el in p_el.iter(*_REVISION_ELEMENTS):
        anc = el.getparent()
        while anc is not None and anc is not p_el and anc.tag != _wt("p"):
            anc = anc.getparent()
        if anc is p_el:
            count += 1
    return count


def _revision_authors(el: Any) -> list[str]:
    authors: list[str] = []
    for rev in el.iter(*_REVISION_ELEMENTS):
        author = rev.get(_wt("author")) or "unknown author"
        if author not in authors:
            authors.append(author)
    return authors


def _set_t(el: Any, text: str) -> None:
    el.text = text
    el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _new_t_after(anchor: Any, text: str) -> Any:
    from lxml import etree

    t = etree.Element(_wt("t"))
    _set_t(t, text)
    anchor.addnext(t)
    return t


def _replace_tokens(tokens: list[list[Any]], find: str, replace: str) -> int:
    """Replace every occurrence of ``find`` across ``tokens`` in place.

    The replacement lands in the element where the match starts (so it takes
    that run's formatting); covered text in later elements is trimmed, and a
    covered tab or break is removed. Runs themselves are never removed or
    blanked, so a footnote reference, comment anchor, field or drawing that
    shares a run with matched text survives the edit.
    """
    full = "".join(t[1] for t in tokens)
    if not find or find not in full:
        return 0
    starts: list[int] = []
    pos = 0
    for token in tokens:
        starts.append(pos)
        pos += len(token[1])
    spans: list[tuple[int, int]] = []
    i = full.find(find)
    while i >= 0:
        spans.append((i, i + len(find)))
        i = full.find(find, i + len(find))
    for a, b in reversed(spans):
        covered = [
            k for k in range(len(tokens))
            if tokens[k][1] and starts[k] < b and starts[k] + len(tokens[k][1]) > a
        ]
        if not covered:
            continue
        first, last = covered[0], covered[-1]
        el, text, kind = tokens[first]
        if kind == "t":
            local_a = a - starts[first]
            tail = text[b - starts[first]:] if first == last else ""
            new_text = text[:local_a] + replace + tail
            _set_t(el, new_text)
            tokens[first][1] = new_text
        else:
            if replace:
                _new_t_after(el, replace)
            el.getparent().remove(el)
            tokens[first][1] = ""
        for k in covered[1:]:
            el_k, text_k, kind_k = tokens[k]
            if k == last and kind_k == "t":
                new_text = text_k[b - starts[k]:]
                _set_t(el_k, new_text)
                tokens[k][1] = new_text
            elif kind_k == "t":
                _set_t(el_k, "")
                tokens[k][1] = ""
            else:
                el_k.getparent().remove(el_k)
                tokens[k][1] = ""
    return len(spans)


def _replace_paragraph_content(p_el: Any, text: str) -> None:
    """Make ``text`` the paragraph's whole visible text, keeping its runs.

    Every text element is cleared — direct runs, hyperlink runs, everything
    the old run-only loop left behind — and the new text goes into the first
    direct run's ``w:t`` (so it keeps that run's formatting). Runs carrying a
    footnote/comment reference, field or drawing stay where they are.
    """
    from lxml import etree

    tokens = _text_tokens(p_el)
    t_tokens = [t for t in tokens if t[2] == "t"]
    target = next((t for t in t_tokens if t[0].getparent().getparent() is p_el), None)
    if target is None and t_tokens:
        target = t_tokens[0]
    for el, _, kind in tokens:
        if target is not None and el is target[0]:
            continue
        if kind == "t":
            _set_t(el, "")
        else:
            el.getparent().remove(el)
    if target is not None:
        _set_t(target[0], text)
        return
    if not text:
        return
    run = etree.SubElement(p_el, _wt("r"))
    first_run = p_el.find(_wt("r"))
    if first_run is not None and first_run is not run:
        rpr = first_run.find(_wt("rPr"))
        if rpr is not None:
            from copy import deepcopy

            run.append(deepcopy(rpr))
    t = etree.SubElement(run, _wt("t"))
    _set_t(t, text)


def _in_fallback(el: Any) -> bool:
    anc = el.getparent()
    while anc is not None:
        if anc.tag == f"{{{_MC_URI}}}Fallback":
            return True
        anc = anc.getparent()
    return False


def _docx_stories(doc: Any) -> list[tuple[str, Any]]:
    """The body plus every header and footer part, each once."""
    stories: list[tuple[str, Any]] = [("body", doc.element.body)]
    seen: set[int] = set()
    for rel in doc.part.rels.values():
        reltype = getattr(rel, "reltype", "") or ""
        if rel.is_external or not (reltype.endswith("/header") or reltype.endswith("/footer")):
            continue
        part = rel.target_part
        element = getattr(part, "element", None)
        if element is None or id(element) in seen:
            continue
        seen.add(id(element))
        stories.append(("header" if reltype.endswith("/header") else "footer", element))
    return stories


def docx_find_diagnosis(path: Path, find: str) -> tuple[str, str]:
    """``(reason, hint)`` for an exact ``find`` that matched nothing.

    Most misses in recorded runs were strings copied from ``read_document``
    output that are not in the XML: a numbering label Word renders (第1条 /
    第一条, "1.1"), CriticMarkup, or a tab shown as a space.
    """
    if not find:
        return "not_found", ""
    if "{++" in find or "{--" in find or "{>>" in find:
        return "criticmarkup_in_find", (
            "find contains CriticMarkup ({++…++}/{--…--}) from read_document output; those marks "
            "show tracked changes and are not text in the document"
        )
    if "\n" in find.strip():
        return "crosses_paragraphs", "find spans more than one paragraph; split it into one change per paragraph"
    unmarked = strip_markdown_emphasis(find)
    if unmarked != find:
        return "markdown_in_find", (
            f"find contains markdown emphasis (**, *, ~~) from read_document output; the document "
            f"has the plain text — use find={unmarked!r}"
        )
    try:
        from .docx_reader import read_docx

        reading = read_docx(path)
    except Exception:  # noqa: BLE001
        return "not_found", ""
    stripped = find.strip()
    for block in reading.blocks:
        label = block.label()
        if label and (stripped.startswith(label) or stripped == label):
            rest = stripped[len(label):].strip()
            return "label_is_auto_numbering", (
                f"'{label}' is Word auto-numbering rendered from numbering.xml, not text in the "
                f"paragraph; anchor with clause='{label}' and find only the words after it"
                + (f" (e.g. find='{rest[:40]}')" if rest else "")
            )
    from .clauses import detect_label

    match = detect_label(stripped)
    if match is not None:
        for block in reading.blocks:
            label = block.label()
            other = detect_label(label + " x") if label else None
            if other is not None and other.family == match.family and other.number == match.number:
                return "label_is_auto_numbering", (
                    f"'{match.label}' is shown as '{label}', which is Word auto-numbering, not text in "
                    f"the paragraph; anchor with clause='{label}' and find only the words after it"
                )
    normalized = " ".join(find.split())
    for block in reading.blocks:
        if block.kind != "paragraph":
            continue
        text = block.accepted_text()
        original = block.original_text()
        if block.has_revisions() and (find in text or find in original):
            return "inside_tracked_change", (
                "the text sits inside or across an existing tracked change; accept or reject that "
                "change first, or anchor on text outside it (redline_docx never edits a pending revision)"
            )
        if text and normalized and normalized in " ".join(text.split()) and find not in text:
            return "whitespace_mismatch", (
                "the text exists but its whitespace differs (the document has a tab or line break where "
                "find has a space)"
            )
    for label, blocks in reading.headers + reading.footers:
        for block in blocks:
            if find in block.accepted_text():
                return "in_header_or_footer", f"the text is in the {label.lower()}, which this tool does not edit"
    for block in reading.blocks:
        for extra in block.extras:
            if find in extra.accepted_text():
                return "in_text_box", "the text is inside a text box, which this tool does not edit"
    return "not_found", (
        "no exact match; copy the text exactly as it appears in read_document output, without labels or markup"
    )


def docx_find_hint(path: Path, find: str) -> str:
    return docx_find_diagnosis(path, find)[1]


# Markers glued to an ASCII letter or digit are arithmetic ("2*3*4"), not
# emphasis; next to Han characters they are emphasis, as the reader emits them.
_EMPHASIS_RE = re.compile(r"(?<![A-Za-z0-9*\\])(\*{1,3}|~~)(?=\S)(.+?)(?<=\S)\1(?![A-Za-z0-9*])")


def strip_markdown_emphasis(text: str) -> str:
    """``**Lessor**`` → ``Lessor``: the markers read_document adds for bold,
    italic and strikethrough runs are not text in the document."""
    previous = None
    while previous != text:
        previous = text
        text = _EMPHASIS_RE.sub(r"\2", text)
    return text


def edit_docx_text_document(source_path: Path, output_path: Path, replacements: list[dict[str, str]]) -> dict[str, Any]:
    """Copy a .docx replacing exact text in the body, tables, text boxes,
    headers and footers. Paragraphs with tracked revisions are refused (and
    reported) rather than rewritten: replacing text inside someone else's
    pending insertion or deletion destroys the audit trail Word shows."""
    from docx import Document

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document(str(source_path))
    # Keyed by element (the dict keeps the proxies alive, so lxml hands back
    # the same objects while iterating); ids of discarded proxies get reused.
    body_index = {p._p: i for i, p in enumerate(doc.paragraphs)}
    stories = _docx_stories(doc)
    counts: dict[str, int] = {}
    refused: list[dict[str, Any]] = []
    not_found: list[dict[str, str]] = []
    for repl in replacements:
        find = str(repl.get("find") or "")
        replace = str(repl.get("replace") or "")
        if not find:
            continue
        total = 0
        refused_here = 0
        for story, root in stories:
            for p_el in root.iter(_wt("p")):
                tokens = _text_tokens(p_el)
                if find not in "".join(t[1] for t in tokens):
                    continue
                revisions = _paragraph_revision_count(p_el)
                if revisions:
                    refused_here += 1
                    refused.append({
                        "find": find,
                        "location": story,
                        "paragraph": body_index.get(p_el),
                        "reason": (
                            f"paragraph carries {revisions} tracked revision(s) by "
                            f"{', '.join(_revision_authors(p_el))}; edit it with redline_docx so the "
                            "revision history survives"
                        ),
                    })
                    continue
                matched = _replace_tokens(tokens, find, replace)
                if not _in_fallback(p_el):
                    total += matched
        counts[find] = total
        if total == 0 and not refused_here:
            not_found.append({"find": find, "hint": docx_find_hint(source_path, find)})
    doc.save(str(output_path))
    payload: dict[str, Any] = {"output_path": str(output_path), "replacements": counts}
    if refused:
        payload["refused"] = refused
    if not_found:
        payload["not_found"] = not_found
    return payload


def _package_rels(zf: zipfile.ZipFile, rels_path: str) -> dict[str, dict[str, str]]:
    try:
        root = ET.fromstring(zf.read(rels_path))
    except KeyError:
        return {}
    out: dict[str, dict[str, str]] = {}
    for rel in root.iter(_qn(_PKG_REL_NS, "Relationship")):
        rel_id = rel.attrib.get("Id", "")
        if not rel_id:
            continue
        out[rel_id] = {
            "type": rel.attrib.get("Type", ""),
            "target": rel.attrib.get("Target", ""),
        }
    return out


def _join_part_path(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base_dir = Path(base_part).parent
    return posixpath.normpath((base_dir / target).as_posix())


def _pptx_slide_paths(zf: zipfile.ZipFile) -> list[str]:
    try:
        presentation = ET.fromstring(zf.read("ppt/presentation.xml"))
    except KeyError:
        return sorted(
            [name for name in zf.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")],
            key=lambda name: int(re.sub(r"\D", "", Path(name).stem) or "0"),
        )
    rels = _package_rels(zf, "ppt/_rels/presentation.xml.rels")
    out: list[str] = []
    for sld_id in presentation.iter(_qn(_P_NS, "sldId")):
        rel_id = sld_id.attrib.get(_qn(_R_NS, "id"), "")
        target = rels.get(rel_id, {}).get("target", "")
        if target:
            path = target.lstrip("/")
            if not path.startswith("ppt/"):
                path = f"ppt/{path}"
            out.append(path)
    return out


def _pptx_part_text(zf: zipfile.ZipFile, part_path: str) -> list[str]:
    try:
        root = ET.fromstring(zf.read(part_path))
    except KeyError:
        return []
    return [el.text or "" for el in root.iter(_qn(_A_NS, "t")) if el.text]


def _pptx_notes_path(zf: zipfile.ZipFile, slide_path: str) -> str | None:
    rels_path = f"{Path(slide_path).parent}/_rels/{Path(slide_path).name}.rels"
    for rel in _package_rels(zf, rels_path).values():
        if rel["type"].endswith("/notesSlide"):
            return _join_part_path(slide_path, rel["target"])
    return None


def _pptx_text_via_python_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
    except Exception:
        Presentation = None  # type: ignore[assignment]

    if Presentation is not None:
        prs = Presentation(str(path))
        notes_by_slide: dict[int, list[str]] = {}
        with zipfile.ZipFile(path) as zf:
            for idx, slide_path in enumerate(_pptx_slide_paths(zf), start=1):
                notes_path = _pptx_notes_path(zf, slide_path)
                notes_by_slide[idx] = _pptx_part_text(zf, notes_path) if notes_path else []

        out: list[str] = []
        for idx, slide in enumerate(prs.slides, start=1):
            out.append(f"## Slide {idx}")
            text_runs: list[str] = []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    text = shape.text.strip()
                    if text:
                        text_runs.append(text)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        values = [cell.text.strip() for cell in row.cells]
                        if any(values):
                            text_runs.append(" | ".join(values))
            out.extend(text_runs or ["[no text]"])
            notes = [n for n in notes_by_slide.get(idx, []) if n.strip()]
            if notes:
                out.append("Speaker notes:")
                out.extend(notes)
            out.append("")
        return "\n".join(out).strip()

    out: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for idx, slide_path in enumerate(_pptx_slide_paths(zf), start=1):
            out.append(f"## Slide {idx}")
            text = _pptx_part_text(zf, slide_path)
            out.extend(text or ["[no text]"])
            notes_path = _pptx_notes_path(zf, slide_path)
            if notes_path:
                notes = _pptx_part_text(zf, notes_path)
                if notes:
                    out.append("Speaker notes:")
                    out.extend(notes)
            out.append("")
    return "\n".join(out).strip()


def extract_pptx_text(path: Path) -> str:
    """Markdown via the tiered converter, falling back to python-pptx + raw XML."""
    from ._convert import convert_to_markdown

    try:
        return convert_to_markdown(path).strip()
    except Exception:
        return _pptx_text_via_python_pptx(path)


def _iter_pptx_shapes(shapes):
    """Every shape on a slide, groups opened, in z-order.

    A ppt-master deck keeps every text frame inside a group (38 groups and 112
    nested text frames on one five-slide deck), so a walk of the top level
    alone reported such a deck as having no text at all.
    """
    for shape in shapes:
        if getattr(shape, "shape_type", None) == 6:  # MSO_SHAPE_TYPE.GROUP
            yield from _iter_pptx_shapes(shape.shapes)
        else:
            yield shape


def inspect_pptx_deck(path: Path, *, include_text_runs: bool = True) -> dict[str, Any]:
    try:
        from pptx import Presentation
    except Exception:
        Presentation = None  # type: ignore[assignment]

    if Presentation is not None:
        prs = Presentation(str(path))
        notes_by_slide: dict[int, list[str]] = {}
        with zipfile.ZipFile(path) as zf:
            for idx, slide_path in enumerate(_pptx_slide_paths(zf), start=1):
                notes_path = _pptx_notes_path(zf, slide_path)
                notes_by_slide[idx] = _pptx_part_text(zf, notes_path) if notes_path else []

        slides = []
        for idx, slide in enumerate(prs.slides, start=1):
            text_runs: list[str] = []
            table_count = 0
            image_count = 0
            group_count = sum(
                1 for shape in slide.shapes if getattr(shape, "shape_type", None) == 6
            )
            for shape in _iter_pptx_shapes(slide.shapes):
                if getattr(shape, "has_text_frame", False):
                    text = shape.text.strip()
                    if text:
                        text_runs.append(text)
                if getattr(shape, "has_table", False):
                    table_count += 1
                    for row in shape.table.rows:
                        values = [cell.text.strip() for cell in row.cells]
                        if any(values):
                            text_runs.append(" | ".join(values))
                if getattr(shape, "shape_type", None) == 13:
                    image_count += 1
            notes = notes_by_slide.get(idx, [])
            payload: dict[str, Any] = {
                "slide": idx,
                "layout": slide.slide_layout.name,
                # Top-level shapes: the range ``reshape_pptx``'s shape_index addresses.
                "shape_count": len(slide.shapes),
                "group_count": group_count,
                "table_count": table_count,
                "image_count": image_count,
                "text_run_count": len(text_runs),
                "notes_run_count": len(notes),
                "title": text_runs[0] if text_runs else "",
            }
            if include_text_runs:
                payload["text_runs"] = text_runs
                payload["speaker_notes"] = notes
            slides.append(payload)
        return {"filename": path.name, "slide_count": len(slides), "slides": slides}

    with zipfile.ZipFile(path) as zf:
        slides = []
        for idx, slide_path in enumerate(_pptx_slide_paths(zf), start=1):
            text = _pptx_part_text(zf, slide_path)
            notes_path = _pptx_notes_path(zf, slide_path)
            notes = _pptx_part_text(zf, notes_path) if notes_path else []
            payload: dict[str, Any] = {
                "slide": idx,
                "path": slide_path,
                "text_run_count": len(text),
                "notes_run_count": len(notes),
                "title": text[0] if text else "",
            }
            if include_text_runs:
                payload["text_runs"] = text
                payload["speaker_notes"] = notes
            slides.append(payload)
    return {"filename": path.name, "slide_count": len(slides), "slides": slides}


def _xlsx_text_via_openpyxl(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        out: list[str] = []
        for ws in wb.worksheets:
            out.append(f"## Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                values = ["" if value is None else str(value) for value in row]
                if any(value.strip() for value in values):
                    out.append(" | ".join(values))
            out.append("")
        return "\n".join(out).strip()
    finally:
        wb.close()


def extract_xlsx_text(path: Path) -> str:
    """Markdown via the tiered converter, falling back to openpyxl pipe-delimited text."""
    from ._convert import convert_to_markdown

    try:
        return convert_to_markdown(path).strip()
    except Exception:
        return _xlsx_text_via_openpyxl(path)


def inspect_xlsx_workbook(path: Path, *, preview_rows: int = 12) -> dict[str, Any]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        sheets: list[dict[str, Any]] = []
        for ws in wb.worksheets:
            preview: list[list[str]] = []
            nonempty_rows = 0
            formula_count = 0
            for row_idx, row in enumerate(ws.iter_rows(values_only=False), start=1):
                values: list[str] = []
                has_value = False
                for cell in row:
                    value = cell.value
                    if isinstance(value, str) and value.startswith("="):
                        formula_count += 1
                    text = "" if value is None else str(value)
                    values.append(text)
                    has_value = has_value or bool(text.strip())
                if has_value:
                    nonempty_rows += 1
                if row_idx <= preview_rows:
                    preview.append(values)
            sheets.append(
                {
                    "name": ws.title,
                    "row_count": ws.max_row,
                    "nonempty_row_count": nonempty_rows,
                    "max_column_count": ws.max_column,
                    "formula_count": formula_count,
                    "preview_rows": preview,
                }
            )
        return {"filename": path.name, "sheets": sheets}
    finally:
        wb.close()


def _xlsx_cell_payload(cell) -> dict[str, Any]:
    value = cell.value
    return {
        "cell": cell.coordinate,
        "value": value,
        "formula": value if isinstance(value, str) and value.startswith("=") else None,
        "data_type": cell.data_type,
        "number_format": cell.number_format,
        "style_id": getattr(cell, "style_id", None),
    }


def inspect_xlsx_range_workbook(path: Path, *, sheet: str, cell_range: str) -> dict[str, Any]:
    """Return coordinate-stable cell payloads for one worksheet range."""
    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries

    wb = load_workbook(path, data_only=False)
    try:
        if sheet not in wb.sheetnames:
            raise ValueError(f"Unknown worksheet: {sheet}")
        ws = wb[sheet]
        min_col, min_row, max_col, max_row = range_boundaries(cell_range)
        rows: list[list[dict[str, Any]]] = []
        for row in ws.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col):
            rows.append([_xlsx_cell_payload(cell) for cell in row])

        merged = []
        for merged_range in ws.merged_cells.ranges:
            m_min_col, m_min_row, m_max_col, m_max_row = range_boundaries(str(merged_range))
            if not (m_max_row < min_row or m_min_row > max_row or m_max_col < min_col or m_min_col > max_col):
                merged.append(str(merged_range))

        from openpyxl.utils import get_column_letter

        hidden_rows = [
            idx for idx in range(min_row, max_row + 1)
            if bool(ws.row_dimensions[idx].hidden)
        ]
        hidden_columns = []
        for col_idx in range(min_col, max_col + 1):
            # ws.cell(...).column_letter raises on MergedCell. Compute the
            # letter from the column index directly.
            letter = get_column_letter(col_idx)
            if bool(ws.column_dimensions[letter].hidden):
                hidden_columns.append(letter)

        return {
            "filename": path.name,
            "sheet": sheet,
            "range": cell_range,
            "row_count": max_row - min_row + 1,
            "column_count": max_col - min_col + 1,
            "merged_ranges": merged,
            "hidden_rows": hidden_rows,
            "hidden_columns": hidden_columns,
            "rows": rows,
        }
    finally:
        wb.close()


def _xlsx_style_fingerprint(cell) -> tuple[Any, ...]:
    fill = cell.fill
    font = cell.font
    alignment = cell.alignment
    return (
        getattr(cell, "style_id", None),
        cell.number_format,
        fill.fill_type,
        getattr(fill.fgColor, "rgb", None),
        font.name,
        font.sz,
        font.bold,
        font.italic,
        getattr(font.color, "rgb", None) if font.color is not None else None,
        alignment.horizontal,
        alignment.vertical,
        alignment.wrap_text,
    )


def diff_xlsx_workbooks(
    source_path: Path,
    target_path: Path,
    *,
    ranges: list[dict[str, str]] | None = None,
    compare_styles: bool = False,
    max_diffs: int = 200,
) -> dict[str, Any]:
    """Compare values/formulas, and optionally styles, in workbook ranges."""
    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries

    source_wb = load_workbook(source_path, data_only=False)
    target_wb = load_workbook(target_path, data_only=False)
    try:
        if ranges is None:
            common = [name for name in source_wb.sheetnames if name in target_wb.sheetnames]
            ranges = [
                {"sheet": name, "range": f"A1:{source_wb[name].cell(source_wb[name].max_row, source_wb[name].max_column).coordinate}"}
                for name in common
            ]

        diffs: list[dict[str, Any]] = []
        total_diff_count = 0
        compared_cells = 0
        missing_sheets = []
        for spec in ranges:
            sheet = str(spec.get("sheet") or "")
            cell_range = str(spec.get("range") or "")
            if sheet not in source_wb.sheetnames or sheet not in target_wb.sheetnames:
                missing_sheets.append(sheet)
                continue
            source_ws = source_wb[sheet]
            target_ws = target_wb[sheet]
            min_col, min_row, max_col, max_row = range_boundaries(cell_range)
            for row in range(min_row, max_row + 1):
                for col in range(min_col, max_col + 1):
                    compared_cells += 1
                    source_cell = source_ws.cell(row, col)
                    target_cell = target_ws.cell(row, col)
                    value_changed = source_cell.value != target_cell.value
                    style_changed = compare_styles and _xlsx_style_fingerprint(source_cell) != _xlsx_style_fingerprint(target_cell)
                    if value_changed or style_changed:
                        total_diff_count += 1
                        if len(diffs) < max_diffs:
                            diffs.append(
                                {
                                    "sheet": sheet,
                                    "cell": source_cell.coordinate,
                                    "source_value": source_cell.value,
                                    "target_value": target_cell.value,
                                    "value_changed": value_changed,
                                    "style_changed": style_changed,
                                }
                            )

        return {
            "source": str(source_path),
            "target": str(target_path),
            "compared_cells": compared_cells,
            "diff_count": total_diff_count,
            "diffs_truncated": total_diff_count > len(diffs),
            "diffs": diffs,
            "missing_sheets": missing_sheets,
        }
    finally:
        source_wb.close()
        target_wb.close()


def edit_xlsx_workbook(
    source_path: Path,
    output_path: Path,
    edits: list[dict[str, Any]],
) -> None:
    """Copy an .xlsx and replace scalar cell values on existing sheets."""
    from openpyxl import load_workbook

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = load_workbook(source_path, data_only=False)
    try:
        missing = sorted({str(e.get("sheet") or "") for e in edits if str(e.get("sheet") or "") not in wb.sheetnames})
        if missing:
            raise ValueError(f"Unknown worksheet(s): {', '.join(missing)}")
        for edit in edits:
            ws = wb[str(edit["sheet"])]
            ws[str(edit["cell"]).upper()] = edit.get("value")
        wb.save(output_path)
    finally:
        wb.close()


def edit_xlsx_workbook_checked(
    source_path: Path,
    output_path: Path,
    edits: list[dict[str, Any]],
    *,
    protected_ranges: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Copy an .xlsx, apply scalar edits with preconditions, and verify ranges."""
    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = load_workbook(source_path, data_only=False)
    try:
        missing = sorted({str(e.get("sheet") or "") for e in edits if str(e.get("sheet") or "") not in wb.sheetnames})
        missing.extend(
            sorted({
                str(r.get("sheet") or "")
                for r in (protected_ranges or [])
                if str(r.get("sheet") or "") not in wb.sheetnames
            })
        )
        if missing:
            raise ValueError(f"Unknown worksheet(s): {', '.join(sorted(set(missing)))}")

        protected_snapshot: dict[tuple[str, str], Any] = {}
        for spec in protected_ranges or []:
            sheet = str(spec.get("sheet") or "")
            cell_range = str(spec.get("range") or "")
            ws = wb[sheet]
            min_col, min_row, max_col, max_row = range_boundaries(cell_range)
            for row in range(min_row, max_row + 1):
                for col in range(min_col, max_col + 1):
                    cell = ws.cell(row, col)
                    protected_snapshot[(sheet, cell.coordinate)] = cell.value

        applied: list[dict[str, Any]] = []
        precondition_failures: list[dict[str, Any]] = []
        for edit in edits:
            ws = wb[str(edit["sheet"])]
            cell_ref = str(edit["cell"]).upper()
            cell = ws[cell_ref]
            if bool(edit.get("check_expected")):
                expected = edit.get("expected_value")
                if cell.value != expected:
                    precondition_failures.append(
                        {
                            "sheet": ws.title,
                            "cell": cell.coordinate,
                            "expected_value": expected,
                            "actual_value": cell.value,
                        }
                    )
                    continue
            applied.append(
                {
                    "sheet": ws.title,
                    "cell": cell.coordinate,
                    "old_value": cell.value,
                    "new_value": edit.get("value"),
                }
            )
            cell.value = edit.get("value")

        if precondition_failures:
            raise ValueError(f"Precondition failed: {precondition_failures}")

        protected_diffs = []
        for (sheet, cell_ref), old_value in protected_snapshot.items():
            new_value = wb[sheet][cell_ref].value
            if old_value != new_value:
                protected_diffs.append(
                    {
                        "sheet": sheet,
                        "cell": cell_ref,
                        "source_value": old_value,
                        "target_value": new_value,
                    }
                )
        if protected_diffs:
            raise ValueError(f"Protected range changed: {protected_diffs[:20]}")

        wb.save(output_path)
        return {
            "path": str(output_path.resolve()),
            "applied_count": len(applied),
            "applied": applied,
            "protected_ranges_checked": protected_ranges or [],
            "protected_diff_count": 0,
        }
    finally:
        wb.close()


def _replace_in_pptx_paragraph(p_el, find: str, replace: str) -> int:
    """Run-aware replacement inside one ``a:p``: per-``a:t`` first, then a
    cross-run collapse (mirrors the DOCX ``_replace_paragraph_text`` caveat:
    a cross-run match flattens mixed run formatting in that paragraph)."""
    a_t = _qn(_A_NS, "t")
    t_els = list(p_el.iter(a_t))
    count = 0
    for t in t_els:
        text = t.text or ""
        if find in text:
            count += text.count(find)
            t.text = text.replace(find, replace)
    if count:
        return count
    full = "".join(t.text or "" for t in t_els)
    if find not in full or not t_els:
        return 0
    count = full.count(find)
    t_els[0].text = full.replace(find, replace)
    for t in t_els[1:]:
        t.text = ""
    return count


def edit_pptx_text(source_path: Path, output_path: Path, replacements: list[dict[str, Any]]) -> dict[str, Any]:
    """Copy a .pptx replacing exact text on the given slides.

    Replacement is run-aware (XML-parsed ``a:t`` nodes, never the raw part
    bytes), so finds that PowerPoint split across runs — spell-check runs,
    mixed formatting, CJK/Latin boundaries — still match. Returns per-find
    match counts keyed ``"slide {n}: {find}"`` so callers can see which
    replacements landed instead of silently writing an unchanged deck.
    """
    from lxml import etree

    output_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with zipfile.ZipFile(source_path) as zin:
        slide_paths = _pptx_slide_paths(zin)
        repl_by_slide: dict[int, list[dict[str, Any]]] = {}
        for repl in replacements:
            slide_num = int(repl.get("slide") or 0)
            if slide_num < 1 or slide_num > len(slide_paths):
                raise ValueError(f"Slide index out of range: {slide_num}")
            repl_by_slide.setdefault(slide_num, []).append(repl)
        replacement_parts: dict[str, bytes] = {}
        for slide_num, edits in repl_by_slide.items():
            path = slide_paths[slide_num - 1]
            # lxml preserves the original namespace prefixes on round-trip.
            root = etree.fromstring(zin.read(path))
            for edit in edits:
                find = str(edit.get("find") or "")
                replace = str(edit.get("replace") or "")
                total = 0
                if find:
                    for p_el in root.iter(_qn(_A_NS, "p")):
                        total += _replace_in_pptx_paragraph(p_el, find, replace)
                counts[f"slide {slide_num}: {find}"] = total
            replacement_parts[path] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = replacement_parts.get(info.filename)
                zout.writestr(info, data if data is not None else zin.read(info.filename))
    return {"output_path": str(output_path), "replacements": counts}


# ---------------------------------------------------------------------------
# XLSX structural-edit primitives
#
# Scalar `edit_xlsx_workbook(_checked)` only swap cell values. Structural
# reshape (insert / delete / move rows; merges; copy a styled row; bulk
# styling) needs openpyxl ops that preserve template fills, fonts, borders,
# alignments, merges, row heights, and column widths. Everything below is a
# thin in-place op over an existing workbook; output is a new file so the
# source is preserved for diff.
# ---------------------------------------------------------------------------


def _load_workbook(path: Path):
    from openpyxl import load_workbook

    return load_workbook(path, data_only=False)


def _save_with_dir(wb, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


def _require_sheet(wb, sheet: str):
    if sheet not in wb.sheetnames:
        raise ValueError(f"Unknown worksheet: {sheet}")
    return wb[sheet]


_RESHAPE_XLSX_OPS = (
    "insert_rows", "delete_rows", "insert_cols", "delete_cols", "move_range", "copy_row",
    "merge_range", "unmerge_range", "set_styles", "set_row_height", "set_col_width",
)


def reshape_xlsx_workbook(
    source_path: Path,
    output_path: Path,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply an ordered list of structural ops to a copy of ``source_path``.

    Supported ``op`` values, all keyed by ``sheet`` (worksheet name):

    - ``insert_rows`` / ``delete_rows``        — ``at``, ``count`` (default 1)
    - ``insert_cols`` / ``delete_cols``        — ``at``, ``count`` (default 1)
      These carry merged ranges, data validations, conditional formats,
      formula references on every sheet, defined names, tables and row
      heights with the moved cells, as Excel does, and report each in
      ``side_effects`` (a reference into deleted cells becomes ``#REF!``).
    - ``move_range``                           — ``range``, ``rows``, ``cols``,
      ``translate`` (bool, defaults True; rewrites formula refs)
    - ``copy_row``                             — ``src_row``, ``dest_row``;
      copies values (formulas translated as a copy/paste would), per-cell
      styles, row height, and overlapping merges
    - ``merge_range`` / ``unmerge_range``      — ``range``
    - ``set_styles``                           — ``range`` + any of
      ``font_name``, ``font_size``, ``font_bold``, ``font_italic``,
      ``font_color``, ``fill_color``, ``border``, ``align_h``, ``align_v``,
      ``wrap_text``, ``number_format``
    - ``set_row_height``                       — ``row``, ``height``
    - ``set_col_width``                        — ``col``, ``width``

    Operations apply in order; later ops see the state left by earlier ones.
    Returns a summary describing each applied op for audit logging.
    """
    from copy import copy as _shallow_copy
    from openpyxl.styles import Border, PatternFill, Side
    from openpyxl.styles.colors import Color
    from openpyxl.utils import get_column_letter, column_index_from_string
    from openpyxl.utils.cell import range_boundaries

    from .xlsx_shift import structural_shift, translate_copied_formula

    def _color(val: Any) -> str | None:
        if not val:
            return None
        s = str(val).lstrip("#").upper()
        if len(s) == 6:
            s = "FF" + s
        return s

    def _border_from_spec(spec: Any) -> Border | None:
        # spec is either "thin" / "medium" / "thick" (apply to all four sides)
        # or a dict like {"top": "thin", "left": "thin", ...}
        if not spec:
            return None
        sides_spec: dict[str, str] = {}
        if isinstance(spec, str):
            sides_spec = {edge: spec for edge in ("left", "right", "top", "bottom")}
        elif isinstance(spec, dict):
            sides_spec = {k: str(v) for k, v in spec.items() if v}
        if not sides_spec:
            return None
        return Border(**{edge: Side(style=style) for edge, style in sides_spec.items()})

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = _load_workbook(source_path)
    applied: list[dict[str, Any]] = []
    try:
        for idx, op_raw in enumerate(operations, start=1):
            op = dict(op_raw or {})
            kind = str(op.get("op") or "").strip()
            sheet = str(op.get("sheet") or "")
            ws = _require_sheet(wb, sheet)
            entry: dict[str, Any] = {"index": idx, "op": kind, "sheet": sheet}

            if kind in {"insert_rows", "delete_rows", "insert_cols", "delete_cols"}:
                at = int(op["at"])
                count = int(op.get("count") or 1)
                if at < 1 or count < 1:
                    raise ValueError(f"{kind} needs at >= 1 and count >= 1 (got at={at}, count={count})")
                effects = structural_shift(
                    wb,
                    ws,
                    axis="row" if kind.endswith("_rows") else "col",
                    at=at,
                    count=count,
                    delete=kind.startswith("delete"),
                )
                entry.update({"at": at, "count": count})
                if effects:
                    entry["side_effects"] = effects
            elif kind == "move_range":
                rng = str(op["range"])
                rows = int(op.get("rows") or 0)
                cols = int(op.get("cols") or 0)
                translate = bool(op.get("translate", True))
                ws.move_range(rng, rows=rows, cols=cols, translate=translate)
                entry.update({"range": rng, "rows": rows, "cols": cols, "translate": translate})
            elif kind == "copy_row":
                src_row = int(op["src_row"])
                dest_row = int(op["dest_row"])
                max_col = ws.max_column
                translated = 0
                for col in range(1, max_col + 1):
                    src = ws.cell(src_row, col)
                    dst = ws.cell(dest_row, col)
                    # Copy semantics, as Excel's copy/paste: relative refs follow.
                    dst.value = translate_copied_formula(src.value, src.coordinate, dst.coordinate)
                    if dst.value != src.value:
                        translated += 1
                    if src.has_style:
                        dst.font = _shallow_copy(src.font)
                        dst.fill = _shallow_copy(src.fill)
                        dst.border = _shallow_copy(src.border)
                        dst.alignment = _shallow_copy(src.alignment)
                        dst.number_format = src.number_format
                        dst.protection = _shallow_copy(src.protection)
                src_height = ws.row_dimensions[src_row].height
                if src_height is not None:
                    ws.row_dimensions[dest_row].height = src_height
                offset = dest_row - src_row
                added_merges = 0
                for merged in list(ws.merged_cells.ranges):
                    mn_col, mn_row, mx_col, mx_row = range_boundaries(str(merged))
                    if mn_row == src_row and mx_row == src_row:
                        new_rng = (
                            f"{get_column_letter(mn_col)}{mn_row + offset}:"
                            f"{get_column_letter(mx_col)}{mx_row + offset}"
                        )
                        ws.merge_cells(new_rng)
                        added_merges += 1
                entry.update({
                    "src_row": src_row, "dest_row": dest_row,
                    "cells_copied": max_col, "added_merges": added_merges,
                })
                if translated:
                    entry["formulas_translated"] = translated
            elif kind == "merge_range":
                rng = str(op["range"])
                ws.merge_cells(rng)
                entry.update({"range": rng})
            elif kind == "unmerge_range":
                rng = str(op["range"])
                ws.unmerge_cells(rng)
                entry.update({"range": rng})
            elif kind == "set_styles":
                rng = str(op["range"])
                min_col, min_row, max_col, max_row = range_boundaries(rng)
                font_kwargs: dict[str, Any] = {}
                for src_key, dst_key in (
                    ("font_name", "name"),
                    ("font_size", "size"),
                    ("font_bold", "bold"),
                    ("font_italic", "italic"),
                ):
                    if src_key in op:
                        font_kwargs[dst_key] = op[src_key]
                font_color = _color(op.get("font_color"))
                if font_color is not None:
                    font_kwargs["color"] = font_color
                fill_color = _color(op.get("fill_color"))
                fill = (
                    PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
                    if fill_color
                    else None
                )
                border = _border_from_spec(op.get("border"))
                align_kwargs: dict[str, Any] = {}
                if "align_h" in op and op["align_h"]:
                    align_kwargs["horizontal"] = str(op["align_h"])
                if "align_v" in op and op["align_v"]:
                    align_kwargs["vertical"] = str(op["align_v"])
                if "wrap_text" in op:
                    align_kwargs["wrap_text"] = bool(op["wrap_text"])
                number_format = op.get("number_format")
                touched = 0
                for r in range(min_row, max_row + 1):
                    for c in range(min_col, max_col + 1):
                        cell = ws.cell(r, c)
                        if font_kwargs:
                            # Start from a copy of the cell's own font. Rebuilding
                            # Font() from base.color.rgb crashed on theme, indexed
                            # and auto colours — openpyxl's default font among
                            # them — and failed 19 of 45 recorded calls; a copy
                            # also keeps underline, strike and the theme colour.
                            font = _shallow_copy(cell.font)
                            for attr, value in font_kwargs.items():
                                if attr == "color":
                                    font.color = Color(rgb=value)
                                else:
                                    setattr(font, attr, value)
                            cell.font = font
                        if fill is not None:
                            cell.fill = _shallow_copy(fill)
                        if border is not None:
                            cell.border = _shallow_copy(border)
                        if align_kwargs:
                            alignment = _shallow_copy(cell.alignment)
                            for attr, value in align_kwargs.items():
                                setattr(alignment, attr, value)
                            cell.alignment = alignment
                        if number_format is not None:
                            cell.number_format = str(number_format)
                        touched += 1
                entry.update({"range": rng, "cells_styled": touched})
            elif kind == "set_row_height":
                row = int(op["row"])
                height = float(op["height"])
                ws.row_dimensions[row].height = height
                entry.update({"row": row, "height": height})
            elif kind == "set_col_width":
                col_raw = op["col"]
                letter = (
                    col_raw if isinstance(col_raw, str)
                    else get_column_letter(int(col_raw))
                )
                # Validate via round-trip — raises if letter is malformed.
                column_index_from_string(letter)
                width = float(op["width"])
                ws.column_dimensions[letter].width = width
                entry.update({"col": letter, "width": width})
            else:
                raise ValueError(
                    f"Unknown structural op {kind!r} (operation {idx}). Valid ops: "
                    + ", ".join(_RESHAPE_XLSX_OPS)
                )

            applied.append(entry)

        _save_with_dir(wb, output_path)
        return {
            "path": str(output_path.resolve()),
            "applied_count": len(applied),
            "applied": applied,
        }
    finally:
        wb.close()


def copy_xlsx_worksheet(
    source_path: Path,
    sheet: str,
    output_path: Path,
    new_name: str,
) -> dict[str, Any]:
    """Duplicate one worksheet inside the same workbook under ``new_name``.

    Use to spin off sibling sheets (e.g. ``A类`` → ``B类``) from a styled
    template without losing fills, merges, validations, or conditional
    formats.
    """
    wb = _load_workbook(source_path)
    try:
        ws = _require_sheet(wb, sheet)
        cleaned = (new_name or "").strip()[:31] or f"{sheet}_copy"
        if cleaned in wb.sheetnames:
            raise ValueError(f"Sheet already exists: {cleaned}")
        new_ws = wb.copy_worksheet(ws)
        new_ws.title = cleaned
        # copy_worksheet copies cells, styles, merges and dimensions only; the
        # validations and conditional formats this docstring promises were
        # silently dropped.
        from .xlsx_shift import copy_sheet_extras

        extras = copy_sheet_extras(ws, new_ws)
        _save_with_dir(wb, output_path)
        payload: dict[str, Any] = {
            "path": str(output_path.resolve()),
            "source_sheet": sheet,
            "new_sheet": cleaned,
        }
        if extras:
            payload["copied"] = extras
        return payload
    finally:
        wb.close()


def _set_cell_text(cell: Any, text: str) -> None:
    """Write a cell's text without discarding its paragraph properties.

    ``cell.text = …`` replaces the cell's ``w:p`` wholesale, which drops the
    ``w:pPr`` the writer put there — including the suppressed first-line indent
    that keeps a statute title on one line inside its cell. Writing the text
    elements instead keeps the properties (and any reference-carrying runs),
    so an edit does not silently undo the layout the document was built with.
    """
    paragraph = cell.paragraphs[0]
    for extra in list(cell.paragraphs[1:]):
        extra._p.getparent().remove(extra._p)
    _replace_paragraph_content(paragraph._p, str(text))


def _refuse_revised(el: Any, what: str) -> None:
    """Raise when ``el`` carries tracked revisions: a structural edit there
    would silently accept or discard someone's pending change."""
    count = sum(1 for _ in el.iter(*_REVISION_ELEMENTS))
    if count:
        raise ValueError(
            f"{what} carries {count} tracked revision(s) by {', '.join(_revision_authors(el))}; "
            "reshape_docx would rewrite them as plain text. Make the change with redline_docx "
            "(tracked) instead, or ask the user to accept/reject the revisions first."
        )


_RESHAPE_DOCX_OPS = (
    "insert_paragraph", "delete_paragraphs", "set_paragraph_style", "replace_paragraph",
    "insert_table_row", "delete_table_rows", "set_cell_text",
)


def reshape_docx_document(
    source_path: Path,
    output_path: Path,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply an ordered list of structural ops to a copy of ``source_path``.

    Paragraph indices are the 0-based body indices ``inspect_docx`` lists.
    Supported ``op`` values:

    - ``insert_paragraph``   — ``after`` (0-based para index; -1 prepends),
      ``text``, optional ``style`` (paragraph style name like ``Heading 2``)
    - ``delete_paragraphs``  — ``indices`` (list[int]) **or** ``range``
      ([start, end_inclusive])
    - ``set_paragraph_style``— ``index``, ``style``
    - ``replace_paragraph``  — ``index``, ``text`` (keeps style and the first
      run's formatting; hyperlink and field-result text is replaced too)
    - ``insert_table_row``   — ``table_index``, ``at`` (0-based; -1 appends),
      ``cells`` (list[str])
    - ``delete_table_rows``  — ``table_index``, ``indices`` (list[int])
    - ``set_cell_text``      — ``table_index``, ``row``, ``col``, ``text``

    Ops that would rewrite or delete content carrying tracked revisions are
    refused; use ``redline_docx`` for those.
    """
    from docx import Document

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document(str(source_path))
    applied: list[dict[str, Any]] = []
    for idx, op_raw in enumerate(operations, start=1):
        op = dict(op_raw or {})
        kind = str(op.get("op") or "").strip()
        entry: dict[str, Any] = {"index": idx, "op": kind}

        if kind == "insert_paragraph":
            after = int(op.get("after", -1))
            text = str(op.get("text") or "")
            style = op.get("style")
            paras = doc.paragraphs
            new_p = _docx_make_paragraph(doc, text, style)
            if after == -1 or not paras:
                doc.element.body.insert(0, new_p)
                entry.update({"after": after, "inserted_index": 0})
                previous = None
            else:
                if after >= len(paras):
                    raise ValueError(f"insert_paragraph after={after} out of range (paragraphs={len(paras)})")
                previous = paras[after]._element
                previous.addnext(new_p)
                entry.update({"after": after, "inserted_index": after + 1})
            numbering_note = _start_or_continue_list(doc, new_p, previous)
            if numbering_note:
                entry["numbering"] = numbering_note
        elif kind == "delete_paragraphs":
            indices = op.get("indices")
            if indices is None and "range" in op:
                rng = op["range"]
                indices = list(range(int(rng[0]), int(rng[1]) + 1))
            if not indices:
                raise ValueError("delete_paragraphs requires 'indices' or 'range'")
            paras = list(doc.paragraphs)
            targets = sorted({int(x) for x in indices if 0 <= int(x) < len(paras)}, reverse=True)
            for i in targets:
                _refuse_revised(paras[i]._element, f"paragraph {i}")
            for i in targets:
                el = paras[i]._element
                el.getparent().remove(el)
            entry.update({"removed": len(targets)})
        elif kind == "set_paragraph_style":
            i = int(op["index"])
            style = str(op["style"])
            paras = doc.paragraphs
            if not (0 <= i < len(paras)):
                raise ValueError(f"set_paragraph_style index={i} out of range")
            paras[i].style = doc.styles[style]
            entry.update({"index": i, "style": style})
        elif kind == "replace_paragraph":
            i = int(op["index"])
            text = str(op.get("text") or "")
            paras = doc.paragraphs
            if not (0 <= i < len(paras)):
                raise ValueError(f"replace_paragraph index={i} out of range")
            _refuse_revised(paras[i]._p, f"paragraph {i}")
            _replace_paragraph_content(paras[i]._p, text)
            entry.update({"index": i})
        elif kind == "insert_table_row":
            t_idx = int(op["table_index"])
            cells = [str(v) for v in (op.get("cells") or [])]
            at = int(op.get("at", -1))
            tables = doc.tables
            if not (0 <= t_idx < len(tables)):
                raise ValueError(f"insert_table_row table_index={t_idx} out of range")
            table = tables[t_idx]
            new_row = table.add_row()
            if cells:
                for col, value in enumerate(cells):
                    if col < len(new_row.cells):
                        _set_cell_text(new_row.cells[col], value)
            if at != -1 and 0 <= at < len(table.rows) - 1:
                tbl = table._tbl
                tbl.remove(new_row._tr)
                anchor = table.rows[at]._tr
                anchor.addprevious(new_row._tr)
            entry.update({"table_index": t_idx, "at": at})
        elif kind == "delete_table_rows":
            t_idx = int(op["table_index"])
            indices = sorted({int(x) for x in (op.get("indices") or [])}, reverse=True)
            tables = doc.tables
            if not (0 <= t_idx < len(tables)):
                raise ValueError(f"delete_table_rows table_index={t_idx} out of range")
            table = tables[t_idx]
            targets = [i for i in indices if 0 <= i < len(table.rows)]
            for i in targets:
                _refuse_revised(table.rows[i]._tr, f"table {t_idx} row {i}")
            for i in targets:
                row = table.rows[i]
                row._tr.getparent().remove(row._tr)
            entry.update({"table_index": t_idx, "removed": len(targets)})
        elif kind == "set_cell_text":
            t_idx = int(op["table_index"])
            row = int(op["row"])
            col = int(op["col"])
            text = str(op.get("text") or "")
            tables = doc.tables
            if not (0 <= t_idx < len(tables)):
                raise ValueError(f"set_cell_text table_index={t_idx} out of range")
            table = tables[t_idx]
            if not (0 <= row < len(table.rows) and 0 <= col < len(table.rows[row].cells)):
                raise ValueError("set_cell_text row/col out of range")
            cell = table.rows[row].cells[col]
            _refuse_revised(cell._tc, f"table {t_idx} cell ({row}, {col})")
            _set_cell_text(cell, text)
            entry.update({"table_index": t_idx, "row": row, "col": col})
        else:
            raise ValueError(
                f"Unknown structural op: {kind!r}. Valid ops: {', '.join(_RESHAPE_DOCX_OPS)}"
            )
        applied.append(entry)

    doc.save(str(output_path))
    return {
        "path": str(output_path.resolve()),
        "applied_count": len(applied),
        "applied": applied,
    }


def _start_or_continue_list(doc: Any, p_el: Any, previous: Any) -> str:
    """Number an inserted ordered-list paragraph the way Word would.

    Word counts per abstract numbering definition, so a paragraph given the
    ``List Number`` style simply continued the document's last ordered list —
    an inserted list opened at 19 instead of 1, which the document lint flags
    (in a filing "诉讼请求第3项" means item 3). An inserted ordered item now
    continues the list only when the paragraph right above it belongs to the
    same list; otherwise it gets its own ``w:num`` with a ``startOverride``,
    and the items inserted after it continue that new list.
    """
    from docx.oxml.ns import qn
    from lxml import etree

    from .clauses import DocxNumbering, parse_styles

    try:
        numbering_el = doc.part.numbering_part.element
    except Exception:  # noqa: BLE001 — no numbering part: nothing to number
        return ""
    model = DocxNumbering(etree.tostring(numbering_el), parse_styles(etree.tostring(doc.styles.element)))

    def resolve(el: Any):
        if el is None or el.tag != qn("w:p"):
            return None
        ppr = el.find(qn("w:pPr"))
        style_el = ppr.find(qn("w:pStyle")) if ppr is not None else None
        style_id = style_el.get(qn("w:val")) if style_el is not None else ""
        return model.resolve(ppr, style_id or "")

    mine = resolve(p_el)
    if mine is None:
        return ""
    num_id, ilvl = mine
    abstract_raw = model.nums[num_id][0]
    abstract_id, levels = model._abstract_levels(abstract_raw)
    level = levels.get(ilvl)
    if level is None or level.fmt in {"bullet", "none"}:
        return ""
    above = resolve(previous)
    if above is not None:
        above_abstract, _ = model._abstract_levels(model.nums[above[0]][0])
        if above_abstract == abstract_id:
            _set_paragraph_numbering(p_el, above[0], ilvl)
            return "continues the list above"
    num = numbering_el.add_num(int(abstract_raw))
    num.add_lvlOverride(ilvl=ilvl).add_startOverride(level.start)
    _set_paragraph_numbering(p_el, str(num.numId), ilvl)
    return f"starts a new list at {level.start}"


def _set_paragraph_numbering(p_el: Any, num_id: str, ilvl: int) -> None:
    """Direct ``w:numPr`` on a paragraph, in schema position (after pStyle)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ppr = p_el.find(qn("w:pPr"))
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        p_el.insert(0, ppr)
    for old in ppr.findall(qn("w:numPr")):
        ppr.remove(old)
    num_pr = OxmlElement("w:numPr")
    level = OxmlElement("w:ilvl")
    level.set(qn("w:val"), str(ilvl))
    ident = OxmlElement("w:numId")
    ident.set(qn("w:val"), str(num_id))
    num_pr.append(level)
    num_pr.append(ident)
    anchor = None
    for name in ("pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl"):
        found = ppr.find(qn(f"w:{name}"))
        if found is not None:
            anchor = found
    if anchor is not None:
        anchor.addnext(num_pr)
    else:
        ppr.insert(0, num_pr)


def _docx_make_paragraph(doc, text: str, style: str | None):
    """Build a free-floating <w:p> element with optional style + text run."""
    from docx.oxml.ns import qn
    from lxml import etree

    p = etree.SubElement(doc.element.body, qn("w:p"))
    doc.element.body.remove(p)
    if style:
        pPr = etree.SubElement(p, qn("w:pPr"))
        pStyle = etree.SubElement(pPr, qn("w:pStyle"))
        pStyle.set(qn("w:val"), doc.styles[style].style_id)
    if text:
        r = etree.SubElement(p, qn("w:r"))
        t = etree.SubElement(r, qn("w:t"))
        t.text = text
        t.set(qn("xml:space"), "preserve")
    return p


# ---------------------------------------------------------------------------
# PPTX structural-edit primitives
# ---------------------------------------------------------------------------


def reshape_pptx_deck(
    source_path: Path,
    output_path: Path,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply an ordered list of structural ops to a copy of ``source_path``.

    Supported ``op`` values:

    - ``duplicate_slide``  — ``index`` (1-based), optional ``after`` (1-based;
      defaults to immediately after the source)
    - ``delete_slide``     — ``index`` (1-based)
    - ``move_slide``       — ``index``, ``to`` (both 1-based)
    - ``set_shape_text``   — ``slide`` (1-based), one of: ``shape_index``
      (0-based) or ``shape_name`` or ``placeholder_idx`` + ``text``
    - ``set_speaker_notes``— ``slide`` (1-based), ``text``

    ``shape_name`` also finds a shape inside a group, which is where a
    ppt-master deck keeps all of its text; ``shape_index`` counts top-level
    shapes only.
    """
    from pptx import Presentation
    from copy import deepcopy

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs = Presentation(str(source_path))
    applied: list[dict[str, Any]] = []

    def _xml_slides():
        return prs.slides._sldIdLst  # type: ignore[attr-defined]

    def _move_slide(src_idx_zero: int, dest_idx_zero: int) -> None:
        slides = _xml_slides()
        children = list(slides)
        if not (0 <= src_idx_zero < len(children)):
            raise ValueError(f"slide index out of range: {src_idx_zero + 1}")
        node = children[src_idx_zero]
        slides.remove(node)
        dest = max(0, min(dest_idx_zero, len(list(slides))))
        slides.insert(dest, node)

    def _duplicate_slide(src_idx_zero: int) -> int:
        source = prs.slides[src_idx_zero]
        new_slide = prs.slides.add_slide(source.slide_layout)
        # add_slide appends; copy shape XML in document order.
        for shape in source.shapes:
            new_el = deepcopy(shape._element)
            new_slide.shapes._spTree.insert_element_before(new_el, "p:extLst")
        # Copy speaker notes if present.
        if source.has_notes_slide:
            src_notes = source.notes_slide.notes_text_frame.text
            new_slide.notes_slide.notes_text_frame.text = src_notes
        return len(prs.slides) - 1  # zero-based index of the new slide

    for idx, op_raw in enumerate(operations, start=1):
        op = dict(op_raw or {})
        kind = str(op.get("op") or "").strip()
        entry: dict[str, Any] = {"index": idx, "op": kind}
        if kind == "duplicate_slide":
            src = int(op["index"]) - 1
            new_zero = _duplicate_slide(src)
            after = op.get("after")
            if after is not None:
                _move_slide(new_zero, int(after))
                entry.update({"source": src + 1, "new_index": int(after) + 1})
            else:
                _move_slide(new_zero, src + 1)
                entry.update({"source": src + 1, "new_index": src + 2})
        elif kind == "delete_slide":
            i = int(op["index"]) - 1
            slides = _xml_slides()
            children = list(slides)
            if not (0 <= i < len(children)):
                raise ValueError(f"delete_slide index out of range: {i+1}")
            slides.remove(children[i])
            entry.update({"removed_index": i + 1})
        elif kind == "move_slide":
            src = int(op["index"]) - 1
            dest = int(op["to"]) - 1
            _move_slide(src, dest)
            entry.update({"from": src + 1, "to": dest + 1})
        elif kind == "set_shape_text":
            slide_idx = int(op["slide"]) - 1
            slide = prs.slides[slide_idx]
            text = str(op.get("text") or "")
            target = None
            if "shape_index" in op:
                shape_idx = int(op["shape_index"])
                if not (0 <= shape_idx < len(slide.shapes)):
                    raise ValueError(f"shape_index {shape_idx} out of range")
                target = slide.shapes[shape_idx]
            elif "shape_name" in op:
                name = str(op["shape_name"])
                for sh in _iter_pptx_shapes(slide.shapes):
                    if sh.name == name:
                        target = sh
                        break
                if target is None:
                    raise ValueError(f"No shape named {name!r} on slide {slide_idx+1}")
            elif "placeholder_idx" in op:
                ph_idx = int(op["placeholder_idx"])
                for ph in slide.placeholders:
                    if ph.placeholder_format.idx == ph_idx:
                        target = ph
                        break
                if target is None:
                    raise ValueError(
                        f"No placeholder with idx={ph_idx} on slide {slide_idx+1}"
                    )
            else:
                raise ValueError("set_shape_text needs shape_index, shape_name, or placeholder_idx")
            if not getattr(target, "has_text_frame", False):
                raise ValueError("Target shape has no text frame")
            target.text_frame.text = text
            entry.update({"slide": slide_idx + 1, "shape": getattr(target, "name", "")})
        elif kind == "set_speaker_notes":
            slide_idx = int(op["slide"]) - 1
            slide = prs.slides[slide_idx]
            text = str(op.get("text") or "")
            slide.notes_slide.notes_text_frame.text = text
            entry.update({"slide": slide_idx + 1})
        else:
            raise ValueError(f"Unknown structural op: {kind!r}")
        applied.append(entry)

    prs.save(str(output_path))
    return {
        "path": str(output_path.resolve()),
        "applied_count": len(applied),
        "applied": applied,
    }
