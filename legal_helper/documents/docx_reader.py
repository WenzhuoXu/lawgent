"""Read a .docx the way Word shows it, for a model that has to act on it.

The tiered converter (anydoc → markitdown) handled the common case but lost
three things a lawyer reads first, all measured on real uploads:

- **Tracked changes read as accepted.** 5 of 52 uploads carried revisions;
  7,954 deleted characters were hidden and 8,112 inserted characters were shown
  as agreed text, so a counterparty's markup read as the agreed contract.
- **Headers and footers were dropped** — "机密", "DRAFT — subject to contract",
  the document number.
- **Auto-numbering changed form.** 第一条 came back as ``- 第1条`` (anydoc
  renders ``chineseCounting`` as Arabic and the level as a list item).

So the body is rendered here from ``document.xml`` directly: revisions as
CriticMarkup (``{++inserted++}`` / ``{--deleted--}``) under a one-line summary,
labels computed by :mod:`.clauses` the way Word computes them, headers and
footers labelled, footnotes as ``[^n]``, comments with the text they anchor.
Every body paragraph keeps the 0-based index python-docx (and therefore
``reshape_docx``) uses, so what ``inspect_docx`` lists is what an edit targets.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .clauses import (
    Clause,
    ClauseUnit,
    DocxNumbering,
    ParagraphNumbering,
    StyleInfo,
    build_clause_index,
    heading_level,
    parse_styles,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
V_NS = "urn:schemas-microsoft-com:vml"


def w(name: str) -> str:
    return f"{{{W_NS}}}{name}"


_T, _DELTEXT, _R, _P, _TBL, _TR, _TC = w("t"), w("delText"), w("r"), w("p"), w("tbl"), w("tr"), w("tc")
_INS, _DEL, _MOVE_FROM, _MOVE_TO = w("ins"), w("del"), w("moveFrom"), w("moveTo")
_REVISION_TAGS = frozenset({_INS, _DEL, _MOVE_FROM, _MOVE_TO})
_FORMAT_CHANGE_TAGS = frozenset(
    w(n) for n in ("rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "trPrChange", "tcPrChange", "tblGridChange", "numberingChange")
)
_PASS_THROUGH = frozenset(
    w(n) for n in ("fldSimple", "smartTag", "customXml", "bdo", "dir")
)
_TEXTBOX_CONTENT = w("txbxContent")
_MC_ALTERNATE = f"{{{MC_NS}}}AlternateContent"
_MC_CHOICE = f"{{{MC_NS}}}Choice"
_MC_FALLBACK = f"{{{MC_NS}}}Fallback"

_REL_STYLES = "/styles"
_REL_NUMBERING = "/numbering"
_REL_FOOTNOTES = "/footnotes"
_REL_ENDNOTES = "/endnotes"
_REL_COMMENTS = "/comments"
_REL_HEADER = "/header"
_REL_FOOTER = "/footer"
_REL_HYPERLINK = "/hyperlink"
_REL_SETTINGS = "/settings"


@dataclass
class Segment:
    text: str
    kind: str = "normal"  # normal | ins | del
    author: str = ""
    date: str = ""
    fmt: str = ""  # direct run formatting: any of "b", "i", "s" (strike)


@dataclass
class Block:
    kind: str  # paragraph | table
    location: str  # body | table | textbox | sdt | header | footer | note
    paragraph: Optional[int] = None  # python-docx body paragraph index
    table: Optional[int] = None  # python-docx body table index
    style_id: str = ""
    style_name: str = ""
    heading_level: Optional[int] = None
    numbering: Optional[ParagraphNumbering] = None
    segments: list[Segment] = field(default_factory=list)
    rows: list[list["Cell"]] = field(default_factory=list)
    extras: list["Block"] = field(default_factory=list)
    revision_marks: int = 0
    element: Any = None

    # -- text views -------------------------------------------------------
    def accepted_text(self) -> str:
        if self.kind == "table":
            return "\n".join(
                " | ".join(cell.accepted_text() for cell in row) for row in self.rows
            )
        return "".join(s.text for s in self.segments if s.kind != "del")

    def original_text(self) -> str:
        if self.kind == "table":
            return "\n".join(
                " | ".join(cell.original_text() for cell in row) for row in self.rows
            )
        return "".join(s.text for s in self.segments if s.kind != "ins")

    def label(self) -> str:
        if self.numbering is None or self.numbering.is_bullet:
            return ""
        return self.numbering.label

    def labelled_text(self) -> str:
        """Accepted text with its rendered numbering label, as Word shows it."""
        label = self.label()
        text = self.accepted_text()
        if not label:
            return text
        sep = "" if self.numbering and self.numbering.suffix == "nothing" else " "
        return f"{label}{sep}{text}"

    def has_revisions(self) -> bool:
        return self.revision_marks > 0


@dataclass
class Cell:
    blocks: list[Block]
    span: int = 1
    merged_continue: bool = False

    def accepted_text(self) -> str:
        return " ".join(b.labelled_text() for b in self.blocks if b.labelled_text()).strip()

    def original_text(self) -> str:
        return " ".join(b.original_text() for b in self.blocks if b.original_text()).strip()


@dataclass
class RevisionSummary:
    insertions: int = 0
    deletions: int = 0
    moves: int = 0
    formatting: int = 0
    paragraph_marks: int = 0
    authors: dict[str, int] = field(default_factory=dict)
    dates: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.insertions + self.deletions + self.moves

    def note(self, kind: str, author: str, date: str) -> None:
        if kind == "ins":
            self.insertions += 1
        elif kind == "del":
            self.deletions += 1
        elif kind == "move":
            self.moves += 1
        author = author or "unknown author"
        self.authors[author] = self.authors.get(author, 0) + 1
        if date:
            self.dates.append(date[:10])

    def header_line(self) -> str:
        if not self.total and not self.formatting and not self.paragraph_marks:
            return ""
        parts = []
        if self.insertions:
            parts.append(f"{self.insertions} tracked insertion{'s' if self.insertions != 1 else ''}")
        if self.deletions:
            parts.append(f"{self.deletions} deletion{'s' if self.deletions != 1 else ''}")
        if self.moves:
            parts.append(f"{self.moves} move{'s' if self.moves != 1 else ''}")
        if self.formatting:
            parts.append(f"{self.formatting} formatting change{'s' if self.formatting != 1 else ''}")
        if not parts and self.paragraph_marks:
            parts.append(f"{self.paragraph_marks} tracked paragraph change{'s' if self.paragraph_marks != 1 else ''}")
        what = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        authors = ", ".join(self.authors) or "an unnamed author"
        dates = sorted(set(self.dates))
        when = ""
        if dates:
            when = f", {dates[0]}" if len(dates) == 1 else f", {dates[0]} – {dates[-1]}"
        return (
            f"[Tracked changes: this document carries {what} by {authors}{when}, not yet "
            "accepted. Inserted text is shown as {++…++} and deleted text as {--…--}; "
            "neither is agreed text until accepted.]"
        )


@dataclass
class Comment:
    id: str
    author: str
    date: str
    text: str
    anchor: str = ""
    paragraph_text: str = ""


_CONVENTIONAL_PARTS = {
    "/styles": "word/styles.xml",
    "/numbering": "word/numbering.xml",
    "/footnotes": "word/footnotes.xml",
    "/endnotes": "word/endnotes.xml",
    "/comments": "word/comments.xml",
    "/settings": "word/settings.xml",
}


class _Package:
    """The parts of a .docx a reader needs, resolved through relationships."""

    def __init__(self, path: Path):
        from lxml import etree

        self.path = Path(path)
        with zipfile.ZipFile(self.path) as zf:
            self.names = set(zf.namelist())
            main = "word/document.xml"
            try:
                root_rels = etree.fromstring(zf.read("_rels/.rels"))
                for rel in root_rels.iter(f"{{{PKG_REL_NS}}}Relationship"):
                    if rel.get("Type", "").endswith("/officeDocument"):
                        main = rel.get("Target", main).lstrip("/")
                        break
            except (KeyError, etree.XMLSyntaxError):
                pass
            self.main = main
            self.document = zf.read(main)
            self.rels: dict[str, tuple[str, str, str]] = {}
            rels_name = posixpath.join(posixpath.dirname(main), "_rels", posixpath.basename(main) + ".rels")
            if rels_name in self.names:
                try:
                    for rel in etree.fromstring(zf.read(rels_name)).iter(f"{{{PKG_REL_NS}}}Relationship"):
                        target = rel.get("Target", "")
                        mode = rel.get("TargetMode", "")
                        resolved = target if mode == "External" else _join(main, target)
                        self.rels[rel.get("Id", "")] = (rel.get("Type", ""), resolved, mode)
                except etree.XMLSyntaxError:
                    pass
            self.parts: dict[str, bytes] = {}
            for _, target, mode in self.rels.values():
                if mode != "External" and target in self.names and target.endswith(".xml"):
                    self.parts[target] = zf.read(target)
            # Packages assembled by hand (or by tools that skip the rels) still
            # put these parts at their conventional names; read them there too.
            for name in _CONVENTIONAL_PARTS.values():
                if name in self.names and name not in self.parts:
                    self.parts[name] = zf.read(name)

    def part_of_type(self, suffix: str) -> Optional[bytes]:
        for rtype, target, mode in self.rels.values():
            if rtype.endswith(suffix) and mode != "External" and target in self.parts:
                return self.parts.get(target)
        conventional = _CONVENTIONAL_PARTS.get(suffix)
        return self.parts.get(conventional) if conventional else None

    def target(self, rid: str) -> tuple[str, str]:
        rtype, target, _ = self.rels.get(rid, ("", "", ""))
        return rtype, target


def _join(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target))


class DocxReading:
    """A parsed, rendered .docx. Build with :func:`read_docx`."""

    def __init__(self, path: Path, *, document_root: Any = None):
        from lxml import etree

        self.package = _Package(path)
        self.styles: dict[str, StyleInfo] = parse_styles(self.package.part_of_type(_REL_STYLES))
        numbering_xml = self.package.part_of_type(_REL_NUMBERING)
        self.numbering = DocxNumbering(numbering_xml, self.styles)
        self._story_numbering = DocxNumbering(numbering_xml, self.styles)
        self.root = document_root if document_root is not None else etree.fromstring(self.package.document)
        self.revisions = RevisionSummary()
        self._footnote_seq: dict[str, int] = {}
        self._endnote_seq: dict[str, int] = {}
        self._open_comments: dict[str, list[str]] = {}
        self._comment_anchor: dict[str, str] = {}
        self._comment_paragraph: dict[str, str] = {}
        self.blocks: list[Block] = []
        self.headers: list[tuple[str, list[Block]]] = []
        self.footers: list[tuple[str, list[Block]]] = []
        self.footnotes: list[tuple[int, list[Block]]] = []
        self.endnotes: list[tuple[int, list[Block]]] = []
        self.comments: list[Comment] = []
        self.protection: Optional[str] = None
        self.track_revisions = False

        body = self.root.find(w("body"))
        if body is not None:
            self.blocks = list(self._container(body, "body", top=True))
        self._read_stories()
        self._read_notes()
        self._read_comments()
        self._read_settings()
        self.revisions.formatting += sum(
            1 for el in self.root.iter() if el.tag in _FORMAT_CHANGE_TAGS
        )

    # -- containers ---------------------------------------------------------
    def _container(self, container: Any, location: str, *, top: bool = False, numbering: Optional[DocxNumbering] = None) -> Iterable[Block]:
        numbering = numbering or self.numbering
        para_index = 0
        table_index = 0
        for child in container:
            tag = child.tag
            if tag == _P:
                yield self._paragraph(child, location, para_index if top else None, numbering)
                if top:
                    para_index += 1
            elif tag == _TBL:
                yield self._table(child, location, table_index if top else None, numbering)
                if top:
                    table_index += 1
            elif tag == w("sdt"):
                content = child.find(w("sdtContent"))
                if content is not None:
                    yield from self._container(content, "sdt" if location == "body" else location, numbering=numbering)
            elif tag == w("customXml"):
                yield from self._container(child, location, numbering=numbering)
            elif tag == _MC_ALTERNATE:
                choice = child.find(_MC_CHOICE)
                if choice is not None:
                    yield from self._container(choice, location, numbering=numbering)

    def _paragraph(self, p_el: Any, location: str, index: Optional[int], numbering: DocxNumbering) -> Block:
        ppr = p_el.find(w("pPr"))
        style_id = ""
        outline = None
        if ppr is not None:
            style_el = ppr.find(w("pStyle"))
            if style_el is not None:
                style_id = style_el.get(w("val")) or ""
            outline_el = ppr.find(w("outlineLvl"))
            if outline_el is not None:
                try:
                    outline = int(outline_el.get(w("val")) or "9")
                except ValueError:
                    outline = None
        mark_revised = False
        if ppr is not None:
            mark = ppr.find(w("rPr"))
            if mark is not None and (mark.find(_INS) is not None or mark.find(_DEL) is not None):
                self.revisions.paragraph_marks += 1
                mark_revised = True
        block = Block(kind="paragraph", location=location, paragraph=index, element=p_el)
        block.style_id = style_id
        style = self.styles.get(style_id)
        block.style_name = style.name if style is not None else style_id
        block.heading_level = heading_level(self.styles, style_id, outline) if location in {"body", "sdt"} else None
        block.numbering = numbering.label_for(ppr, style_id)
        segments: list[Segment] = []
        extras: list[Block] = []
        before = self.revisions.total
        self._inline(p_el, ("normal", "", ""), segments, extras, block)
        block.revision_marks = self.revisions.total - before + (1 if mark_revised else 0)
        block.segments = _merge(segments)
        block.extras = extras
        for cid in list(self._open_comments):
            self._comment_paragraph.setdefault(cid, block.accepted_text())
        return block

    def _table(self, tbl: Any, location: str, index: Optional[int], numbering: DocxNumbering) -> Block:
        block = Block(kind="table", location=location, table=index, element=tbl)
        before = self.revisions.total
        for tr in _iter_rows(tbl):
            trpr = tr.find(w("trPr"))
            if trpr is not None:
                for tag, kind in ((_INS, "ins"), (_DEL, "del")):
                    mark = trpr.find(tag)
                    if mark is not None:
                        self.revisions.note(kind, mark.get(w("author")) or "", mark.get(w("date")) or "")
            row: list[Cell] = []
            for tc in _iter_cells(tr):
                tcpr = tc.find(w("tcPr"))
                span = 1
                merged = False
                if tcpr is not None:
                    grid = tcpr.find(w("gridSpan"))
                    if grid is not None:
                        try:
                            span = max(1, int(grid.get(w("val")) or "1"))
                        except ValueError:
                            span = 1
                    vmerge = tcpr.find(w("vMerge"))
                    if vmerge is not None and (vmerge.get(w("val")) or "continue") == "continue":
                        merged = True
                cell_blocks: list[Block] = []
                for child_block in self._container(tc, "table", numbering=numbering):
                    if child_block.kind == "table":
                        # A nested table flattens into its cell as text.
                        text = "; ".join(
                            " | ".join(c.accepted_text() for c in r) for r in child_block.rows
                        )
                        cell_blocks.append(Block(kind="paragraph", location="table", segments=[Segment(text)]))
                    else:
                        cell_blocks.append(child_block)
                row.append(Cell(blocks=cell_blocks, span=span, merged_continue=merged))
            block.rows.append(row)
        block.revision_marks = self.revisions.total - before
        return block

    # -- inline content -----------------------------------------------------
    def _inline(self, el: Any, ctx: tuple[str, str, str], out: list[Segment], extras: list[Block], block: Block) -> None:
        for child in el:
            tag = child.tag
            if tag == _R:
                self._run(child, ctx, out, extras, block)
            elif tag in _REVISION_TAGS:
                kind = "ins" if tag in (_INS, _MOVE_TO) else "del"
                author = child.get(w("author")) or ""
                date = child.get(w("date")) or ""
                self.revisions.note("move" if tag in (_MOVE_FROM, _MOVE_TO) else kind, author, date)
                self._inline(child, (kind, author, date), out, extras, block)
            elif tag == w("hyperlink"):
                inner: list[Segment] = []
                self._inline(child, ctx, inner, extras, block)
                rid = child.get(f"{{{R_NS}}}id")
                url = ""
                if rid:
                    rtype, target = self.package.target(rid)
                    if rtype.endswith(_REL_HYPERLINK):
                        url = target
                text = "".join(s.text for s in inner)
                if url and text and all(s.kind == ctx[0] for s in inner) and not text.startswith("http"):
                    # Not _emit: the inner runs already fed any open comment anchor.
                    out.append(Segment(f"[{text}]({url})", ctx[0], ctx[1], ctx[2]))
                else:
                    out.extend(inner)
            elif tag in _PASS_THROUGH:
                self._inline(child, ctx, out, extras, block)
            elif tag == w("sdt"):
                content = child.find(w("sdtContent"))
                if content is not None:
                    self._inline(content, ctx, out, extras, block)
            elif tag == _MC_ALTERNATE:
                choice = child.find(_MC_CHOICE)
                if choice is not None:
                    self._inline(choice, ctx, out, extras, block)
            elif tag in (f"{{{M_NS}}}oMath", f"{{{M_NS}}}oMathPara"):
                text = "".join(t.text or "" for t in child.iter(f"{{{M_NS}}}t"))
                if text:
                    self._emit(out, Segment(text, *ctx))
            elif tag == w("commentRangeStart"):
                cid = child.get(w("id")) or ""
                if cid:
                    self._open_comments.setdefault(cid, [])
            elif tag == w("commentRangeEnd"):
                cid = child.get(w("id")) or ""
                parts = self._open_comments.pop(cid, None)
                if parts is not None:
                    self._comment_anchor.setdefault(cid, "".join(parts))

    def _run(self, r: Any, ctx: tuple[str, str, str], out: list[Segment], extras: list[Block], block: Block) -> None:
        kind, author, date = ctx
        fmt = _run_format(r.find(w("rPr")))
        for child in r:
            tag = child.tag
            text = ""
            if tag == _T:
                text = child.text or ""
            elif tag == _DELTEXT:
                text = child.text or ""
                if kind == "normal":
                    self._emit(out, Segment(text, "del", author, date, fmt))
                    continue
            elif tag in (w("tab"), w("ptab")):
                text = "\t"
            elif tag in (w("br"), w("cr")):
                text = "\n"
            elif tag == w("noBreakHyphen"):
                text = "-"
            elif tag == w("footnoteReference"):
                fid = child.get(w("id")) or ""
                seq = self._footnote_seq.setdefault(fid, len(self._footnote_seq) + 1)
                text = f"[^{seq}]"
            elif tag == w("endnoteReference"):
                eid = child.get(w("id")) or ""
                seq = self._endnote_seq.setdefault(eid, len(self._endnote_seq) + 1)
                text = f"[^e{seq}]"
            elif tag in (w("drawing"), w("pict"), _MC_ALTERNATE, w("object")):
                text = self._graphic(child, extras)
            if text:
                styled = fmt if tag in (_T, _DELTEXT) else ""
                self._emit(out, Segment(text, kind, author, date, styled))

    def _graphic(self, el: Any, extras: list[Block]) -> str:
        """Text boxes become extra blocks; a picture leaves an ``[image]`` marker."""
        target = el
        if el.tag == _MC_ALTERNATE:
            choice = el.find(_MC_CHOICE)
            target = choice if choice is not None else el.find(_MC_FALLBACK)
            if target is None:
                return ""
        boxes = [tb for tb in target.iter(_TEXTBOX_CONTENT)]
        if boxes:
            for box in boxes:
                for b in self._container(box, "textbox"):
                    if b.accepted_text().strip() or b.kind == "table":
                        extras.append(b)
            return ""
        if el.tag == w("object"):
            return "[embedded object]"
        descr = ""
        for doc_pr in target.iter(f"{{{WP_NS}}}docPr"):
            descr = (doc_pr.get("descr") or doc_pr.get("title") or "").strip()
            if descr:
                break
        has_picture = any(True for _ in target.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip")) or any(
            True for _ in target.iter(f"{{{V_NS}}}imagedata")
        )
        if not has_picture and target.tag != w("drawing"):
            return ""
        return f"[image: {descr}]" if descr else "[image]"

    def _emit(self, out: list[Segment], segment: Segment) -> None:
        out.append(segment)
        if segment.kind != "del":
            for parts in self._open_comments.values():
                parts.append(segment.text)

    # -- other stories --------------------------------------------------------
    def _story_blocks(self, xml: bytes, location: str) -> list[Block]:
        from lxml import etree

        try:
            root = etree.fromstring(xml)
        except etree.XMLSyntaxError:
            return []
        return list(self._container(root, location, numbering=self._story_numbering))

    def _read_stories(self) -> None:
        seen: set[str] = set()
        texts: set[tuple[str, str]] = set()
        for sect in self.root.iter(w("sectPr")):
            for ref_tag, bucket, name in (
                (w("headerReference"), self.headers, "Header"),
                (w("footerReference"), self.footers, "Footer"),
            ):
                for ref in sect.findall(ref_tag):
                    rid = ref.get(f"{{{R_NS}}}id") or ""
                    _, target = self.package.target(rid)
                    if not target or target in seen:
                        continue
                    seen.add(target)
                    xml = self.package.parts.get(target)
                    if not xml:
                        continue
                    blocks = self._story_blocks(xml, name.lower())
                    text = " / ".join(
                        _render_inline(b) for b in blocks if _render_inline(b).strip()
                    ).strip()
                    if not text or (name, text) in texts:
                        continue
                    texts.add((name, text))
                    variant = ref.get(w("type")) or "default"
                    label = name if variant == "default" else f"{name}, {'first page' if variant == 'first' else 'even pages'}"
                    bucket.append((label, blocks))

    def _read_notes(self) -> None:
        from lxml import etree

        for suffix, tag, order, bucket in (
            (_REL_FOOTNOTES, w("footnote"), self._footnote_seq, self.footnotes),
            (_REL_ENDNOTES, w("endnote"), self._endnote_seq, self.endnotes),
        ):
            xml = self.package.part_of_type(suffix)
            if not xml or not order:
                continue
            try:
                root = etree.fromstring(xml)
            except etree.XMLSyntaxError:
                continue
            by_id = {}
            for note in root.iter(tag):
                if note.get(w("type")) in {"separator", "continuationSeparator", "continuationNotice"}:
                    continue
                by_id[note.get(w("id")) or ""] = note
            for nid, seq in sorted(order.items(), key=lambda kv: kv[1]):
                note = by_id.get(nid)
                if note is None:
                    continue
                bucket.append((seq, list(self._container(note, "note", numbering=self._story_numbering))))

    def _read_comments(self) -> None:
        from lxml import etree

        xml = self.package.part_of_type(_REL_COMMENTS)
        if not xml:
            return
        try:
            root = etree.fromstring(xml)
        except etree.XMLSyntaxError:
            return
        for comment in root.iter(w("comment")):
            cid = comment.get(w("id")) or ""
            text = " ".join(
                "".join(t.text or "" for t in p.iter(_T)).strip() for p in comment.iter(_P)
            ).strip()
            if not text:
                continue
            self.comments.append(
                Comment(
                    id=cid,
                    author=comment.get(w("author")) or "",
                    date=comment.get(w("date")) or "",
                    text=text,
                    anchor=self._comment_anchor.get(cid, "").strip(),
                    paragraph_text=self._comment_paragraph.get(cid, "").strip(),
                )
            )

    def _read_settings(self) -> None:
        from lxml import etree

        xml = self.package.part_of_type(_REL_SETTINGS)
        if not xml:
            return
        try:
            root = etree.fromstring(xml)
        except etree.XMLSyntaxError:
            return
        prot = root.find(w("documentProtection"))
        if prot is not None:
            enforced = (prot.get(w("enforcement")) or "0").lower() in {"1", "true", "on"}
            edit = prot.get(w("edit")) or "readOnly"
            if enforced and edit != "none":
                self.protection = edit
        track = root.find(w("trackRevisions"))
        if track is not None and (track.get(w("val")) or "1").lower() not in {"0", "false", "off"}:
            self.track_revisions = True

    # -- views ----------------------------------------------------------------
    @property
    def multi_author(self) -> bool:
        return len(self.revisions.authors) > 1

    def body_paragraphs(self) -> list[Block]:
        return [b for b in self.blocks if b.kind == "paragraph" and b.paragraph is not None]

    def clause_units(self) -> list[ClauseUnit]:
        units: list[ClauseUnit] = []
        for block in self.blocks:
            if block.kind == "table":
                units.append(ClauseUnit(text="", is_table=True))
                continue
            units.append(
                ClauseUnit(
                    text=block.labelled_text(),
                    paragraph=block.paragraph,
                    heading_level=block.heading_level,
                    numbering_ilvl=block.numbering.ilvl if block.numbering else None,
                )
            )
        return units

    def clauses(self) -> list[Clause]:
        return build_clause_index(self.clause_units())

    def render_blocks(self, blocks: Iterable[Block]) -> str:
        """Blocks as markdown, a blank line between blocks — but consecutive
        list items (bullets, numbered body paragraphs) on consecutive lines."""
        out: list[str] = []
        previous_list = False
        for block in blocks:
            line = render_block(block, multi_author=self.multi_author)
            if not line.strip():
                continue
            is_list = (
                block.kind == "paragraph"
                and block.numbering is not None
                and not block.heading_level
                and not block.extras
            )
            if out:
                out.append("\n" if (is_list and previous_list) else "\n\n")
            out.append(line)
            previous_list = is_list
        return "".join(out)

    def render(self, *, include_comments: bool = True) -> str:
        out: list[str] = []
        header = self.revisions.header_line()
        if header:
            out.append(header)
        for label, blocks in self.headers:
            text = " / ".join(_render_inline(b, multi_author=self.multi_author) for b in blocks if b.accepted_text().strip() or b.has_revisions())
            if text.strip():
                out.append(f"[{label}] {text.strip()}")
        body = self.render_blocks(self.blocks)
        if body:
            out.append(body)
        for label, blocks in self.footers:
            text = " / ".join(_render_inline(b, multi_author=self.multi_author) for b in blocks if b.accepted_text().strip() or b.has_revisions())
            if text.strip():
                out.append(f"[{label}] {text.strip()}")
        notes = []
        for seq, blocks in self.footnotes:
            notes.append(f"[^{seq}]: " + " ".join(_render_inline(b, multi_author=self.multi_author) for b in blocks).strip())
        for seq, blocks in self.endnotes:
            notes.append(f"[^e{seq}]: " + " ".join(_render_inline(b, multi_author=self.multi_author) for b in blocks).strip())
        if notes:
            out.append("\n".join(notes))
        if include_comments and self.comments:
            lines = ["## Word comments / annotations"]
            for c in self.comments:
                label = f"Comment {c.id}".strip()
                meta = ", ".join(v for v in (c.author, c.date) if v)
                if meta:
                    label = f"{label} ({meta})"
                lines.append(f"- {label}: {c.text}")
                anchor = c.anchor or c.paragraph_text
                if anchor:
                    lines.append(f"  Anchor/context: {anchor[:300]}")
            out.append("\n".join(lines))
        return "\n\n".join(out).strip()


def _on(el: Any) -> bool:
    if el is None:
        return False
    return (el.get(w("val")) or "true").lower() not in {"0", "false", "off", "none"}


def _run_format(rpr: Any) -> str:
    """Direct bold / italic / strikethrough of a run, as ``"bis"`` flags.

    Bold carries defined terms and party names; a strikethrough that is not a
    tracked change is how many counterparties mark a deletion by hand, so it
    must not read as live text either.
    """
    if rpr is None:
        return ""
    fmt = ""
    if _on(rpr.find(w("b"))):
        fmt += "b"
    if _on(rpr.find(w("i"))):
        fmt += "i"
    if _on(rpr.find(w("strike"))) or _on(rpr.find(w("dstrike"))):
        fmt += "s"
    return fmt


def _emphasize(text: str, fmt: str) -> str:
    """Markdown emphasis around the non-blank core of ``text``."""
    if not fmt or not text.strip():
        return text
    core = text.strip()
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    if "b" in fmt and "i" in fmt:
        core = f"***{core}***"
    elif "b" in fmt:
        core = f"**{core}**"
    elif "i" in fmt:
        core = f"*{core}*"
    if "s" in fmt:
        core = f"~~{core}~~"
    return f"{lead}{core}{trail}"


def _merge(segments: list[Segment]) -> list[Segment]:
    merged: list[Segment] = []
    for seg in segments:
        if not seg.text:
            continue
        last = merged[-1] if merged else None
        if last is not None and (last.kind, last.author, last.date, last.fmt) == (seg.kind, seg.author, seg.date, seg.fmt):
            merged[-1] = Segment(last.text + seg.text, seg.kind, seg.author, seg.date, seg.fmt)
        else:
            merged.append(seg)
    return merged


def _iter_rows(tbl: Any) -> Iterable[Any]:
    for child in tbl:
        if child.tag == _TR:
            yield child
        elif child.tag in (w("sdt"), w("customXml")):
            content = child.find(w("sdtContent")) if child.tag == w("sdt") else child
            if content is not None:
                yield from _iter_rows(content)


def _iter_cells(tr: Any) -> Iterable[Any]:
    for child in tr:
        if child.tag == _TC:
            yield child
        elif child.tag in (w("sdt"), w("customXml")):
            content = child.find(w("sdtContent")) if child.tag == w("sdt") else child
            if content is not None:
                yield from _iter_cells(content)


def markup_text(segments: list[Segment], *, multi_author: bool) -> str:
    """CriticMarkup for a paragraph's segments; attribution follows each run of
    changes when more than one author revised the document."""
    out: list[str] = []
    group: list[Segment] = []

    def flush() -> None:
        if not group:
            return
        for seg in group:
            body = _emphasize(seg.text, seg.fmt)
            out.append(("{++" if seg.kind == "ins" else "{--") + body + ("++}" if seg.kind == "ins" else "--}"))
        if multi_author:
            who = []
            for seg in group:
                tag = " ".join(x for x in (seg.author or "unknown author", seg.date[:10]) if x)
                if tag not in who:
                    who.append(tag)
            out.append("{>>" + "; ".join(who) + "<<}")
        group.clear()

    for seg in segments:
        if seg.kind == "normal":
            flush()
            out.append(_emphasize(seg.text, seg.fmt))
        else:
            group.append(seg)
    flush()
    return "".join(out)


def _render_inline(block: Block, *, multi_author: bool = False) -> str:
    if block.kind == "table":
        # Header/footer tables (logo | title | number) read best as one line.
        return "; ".join(
            " | ".join(_cell_markdown(cell, multi_author) for cell in row if not cell.merged_continue)
            for row in block.rows
        )
    text = markup_text(block.segments, multi_author=multi_author)
    label = block.label()
    if label:
        sep = "" if block.numbering and block.numbering.suffix == "nothing" else " "
        text = f"{label}{sep}{text}"
    return text


def _cell_markdown(cell: Cell, multi_author: bool) -> str:
    parts = [_render_inline(b, multi_author=multi_author) for b in cell.blocks]
    text = "<br>".join(p.strip() for p in parts if p.strip())
    return text.replace("|", "\\|").replace("\n", "<br>")


def render_block(block: Block, *, multi_author: bool = False) -> str:
    """Markdown for one block (paragraph or table), including its text boxes."""
    if block.kind == "table":
        grid: list[list[str]] = []
        for row in block.rows:
            line: list[str] = []
            for cell in row:
                text = "" if cell.merged_continue else _cell_markdown(cell, multi_author)
                line.append(text)
                line.extend([""] * (cell.span - 1))
            grid.append(line)
        if not grid:
            return ""
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid]
        if not any(c.strip() for r in grid for c in r):
            return ""
        lines = ["| " + " | ".join(grid[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        lines.extend("| " + " | ".join(r) + " |" for r in grid[1:])
        return "\n".join(lines)
    text = markup_text(block.segments, multi_author=multi_author)
    numbering = block.numbering
    prefix = ""
    if numbering is not None and numbering.is_bullet:
        prefix = "  " * numbering.ilvl + "- "
    elif numbering is not None and numbering.label:
        sep = "" if numbering.suffix == "nothing" else " "
        prefix = numbering.label + sep
    if block.heading_level and (text.strip() or prefix.strip()):
        prefix = "#" * block.heading_level + " " + prefix
    line = (prefix + text) if (text.strip() or prefix.strip()) else ""
    extras = [render_block(b, multi_author=multi_author) for b in block.extras]
    extras = [f"[text box] {e}" for e in extras if e.strip()]
    return "\n\n".join(x for x in [line.rstrip(), *extras] if x.strip())


def read_docx(path: Path, *, document_root: Any = None) -> DocxReading:
    return DocxReading(Path(path), document_root=document_root)


def docx_to_markdown(path: Path, *, include_comments: bool = True) -> str:
    return read_docx(path).render(include_comments=include_comments)


__all__ = [
    "Block",
    "Cell",
    "Comment",
    "DocxReading",
    "RevisionSummary",
    "Segment",
    "docx_to_markdown",
    "markup_text",
    "read_docx",
    "render_block",
]
