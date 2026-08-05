"""python-docx writer with a clean legal-style template.

CJK-native typography: when the content is Chinese (auto-detected, or forced
via ``lang="zh-CN"``), styles get ``w:eastAsia`` fonts (黑体-class headings,
宋体/仿宋-class body), black headings, a 2-character first-line indent, and
CJK line-break/punctuation rules. ``layout="gbt9704"`` applies a GB/T
9704-style official-document layout (仿宋 三号 body, GB margins, fixed line
pitch). Word / LibreOffice substitute Noto CJK SC when the named font is not
installed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Cm, Inches, Pt, RGBColor


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

# Han ideographs, CJK punctuation, fullwidth forms, compatibility ideographs.
_CJK_RE = re.compile(
    "[\u2e80-\u2eff\u3000-\u303f\u31c0-\u31ef\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]"
)

_EA_BODY_FONT = "宋体"          # SimSun-class default body
_EA_GBT_BODY_FONT = "仿宋"      # FangSong-class GB/T 9704 body
_EA_HEADING_FONT = "黑体"       # SimHei-class headings
_EA_GBT_H2_FONT = "楷体"        # KaiTi-class GB/T second-level headings
_EA_TITLE_FONT = "宋体"
_EA_SUBTITLE_FONT = "楷体"      # CJK convention instead of faux-italic


def _has_cjk(text: str | None) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _detect_cjk(title: str, sections: list[tuple[str, str]], subtitle: str | None) -> bool:
    probe = [title, subtitle or ""]
    probe.extend(f"{heading}\n{body}" for heading, body in sections)
    return _has_cjk("\n".join(probe))


def _rpr_of(style):
    return style.element.get_or_add_rPr()


def _set_east_asia_font(style, font_name: str, lang: str = "zh-CN") -> None:
    """Set ``w:rFonts/@w:eastAsia`` + ``w:lang/@w:eastAsia`` on a style."""
    rpr = _rpr_of(style)
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), font_name)
    lang_el = rpr.find(qn("w:lang"))
    if lang_el is None:
        lang_el = OxmlElement("w:lang")
        rpr.append(lang_el)  # w:lang sorts near the end of CT_RPr
    lang_el.set(qn("w:eastAsia"), lang)


def _set_run_east_asia_font(run, font_name: str) -> None:
    rpr = run._r.get_or_add_rPr()
    rpr.get_or_add_rFonts().set(qn("w:eastAsia"), font_name)


# pPr children that must follow the CJK layout toggles (schema order).
_CJK_TOGGLE_SUCCESSORS = (
    "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:jc", "w:textAlignment", "w:outlineLvl", "w:rPr",
)


def _enable_cjk_paragraph_rules(style) -> None:
    """kinsoku line breaking, hanging punctuation, CJK/Latin auto-spacing."""
    ppr = style.element.get_or_add_pPr()
    for tag in ("w:kinsoku", "w:overflowPunct", "w:autoSpaceDE", "w:autoSpaceDN"):
        if ppr.find(qn(tag)) is None:
            ppr.insert_element_before(OxmlElement(tag), *_CJK_TOGGLE_SUCCESSORS)


def _set_first_line_chars(style, chars: int, char_size_pt: float) -> None:
    """首行缩进 in characters (``w:firstLineChars``), twips as fallback."""
    ppr = style.element.get_or_add_pPr()
    ind = ppr.get_or_add_ind()
    ind.set(qn("w:firstLineChars"), str(chars * 100))
    ind.set(qn("w:firstLine"), str(int(chars * char_size_pt * 20)))


def _iter_heading_styles(doc: Document):
    for level in range(1, 5):
        yield doc.styles[f"Heading {level}"]


def _apply_cjk_typography(doc: Document, *, gbt9704: bool) -> None:
    body_font = _EA_GBT_BODY_FONT if gbt9704 else _EA_BODY_FONT
    normal = doc.styles["Normal"]
    body_size = Pt(16) if gbt9704 else Pt(12)  # 三号 for GB/T, 小四 otherwise
    normal.font.size = body_size
    normal.font.color.rgb = RGBColor(0, 0, 0)
    _set_east_asia_font(normal, body_font)
    _enable_cjk_paragraph_rules(normal)
    _set_first_line_chars(normal, 2, body_size.pt)
    if gbt9704:
        pf = normal.paragraph_format
        pf.line_spacing = Pt(28)  # GB/T 9704-style fixed line pitch
        pf.space_after = Pt(0)

    for idx, style in enumerate(_iter_heading_styles(doc), start=1):
        if gbt9704:
            # GB/T 9704 heading scheme: 一级黑体、二级楷体、三级以下仿宋(加粗).
            if idx == 1:
                ea = _EA_HEADING_FONT
            elif idx == 2:
                ea = _EA_GBT_H2_FONT
            else:
                ea = _EA_GBT_BODY_FONT
            style.font.size = Pt(16)
            style.font.bold = idx > 2
        else:
            ea = _EA_HEADING_FONT
        _set_east_asia_font(style, ea)
        style.font.color.rgb = RGBColor(0, 0, 0)

    title_style = doc.styles["Title"]
    _set_east_asia_font(title_style, _EA_TITLE_FONT)
    title_style.font.color.rgb = RGBColor(0, 0, 0)
    title_style.font.bold = True
    if gbt9704:
        title_style.font.size = Pt(22)  # 二号
    # Centered titles must not inherit the body first-line indent.
    ind = title_style.element.get_or_add_pPr().get_or_add_ind()
    ind.set(qn("w:firstLineChars"), "0")
    ind.set(qn("w:firstLine"), "0")


def _set_default_style(doc: Document) -> None:
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(11)
    pf = style.paragraph_format
    pf.line_spacing = 1.15
    pf.space_after = Pt(6)


def _set_margins(doc: Document, *, gbt9704: bool = False) -> None:
    for section in doc.sections:
        if gbt9704:
            # GB/T 9704-style page: 天头 3.7cm, 地脚 3.5cm, 订口 2.8cm, 翻口 2.6cm.
            section.top_margin = Cm(3.7)
            section.bottom_margin = Cm(3.5)
            section.left_margin = Cm(2.8)
            section.right_margin = Cm(2.6)
        else:
            section.left_margin = Inches(1)
            section.right_margin = Inches(1)
            section.top_margin = Inches(1)
            section.bottom_margin = Inches(1)


def _apply_watermark(doc: Document, text: str, *, cjk: bool) -> None:
    """Stamp a repeating watermark banner into every section's page header.

    A true rotated diagonal VML watermark renders inconsistently across
    Word/LibreOffice; a bold, centered, light-red header banner repeats on
    every page in both and reads unambiguously as ``DO NOT FILE``. Also drop a
    prominent banner as the document's first body paragraph so the verdict is
    impossible to miss on page one.
    """
    banner_color = RGBColor(0xC9, 0x44, 0x42)  # terracotta, matches UI --dnf/accent
    for section in doc.sections:
        header = section.header
        header.is_linked_to_previous = False
        hp = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
        hp.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        # Clear any inherited runs, then add the watermark run.
        hp.text = ""
        run = hp.add_run(text)
        run.bold = True
        run.font.size = Pt(12)
        run.font.color.rgb = banner_color
        if cjk:
            _set_run_east_asia_font(run, _EA_HEADING_FONT)


def _add_runs_with_markdown_inline(paragraph, text: str) -> None:
    """Apply **bold**, *italic*, and `code` styling within a paragraph."""
    pos = 0
    pattern = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")
    for m in pattern.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        token = m.group(0)
        if token.startswith("**") and token.endswith("**"):
            r = paragraph.add_run(token[2:-2])
            r.bold = True
        elif token.startswith("*") and token.endswith("*"):
            r = paragraph.add_run(token[1:-1])
            r.italic = True
        elif token.startswith("`") and token.endswith("`"):
            r = paragraph.add_run(token[1:-1])
            r.font.name = "Courier New"
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _parse_table(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    """Parse a markdown table starting at lines[start]; return (rows, next_idx)."""
    rows: list[list[str]] = []
    i = start
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Skip the header separator row (|---|---|).
        if all(re.match(r"^:?-+:?$", c) for c in cells if c):
            i += 1
            continue
        rows.append(cells)
        i += 1
    return rows, i


def _render_body_markdown(doc: Document, body: str, *, cjk: bool = False) -> None:
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip():
            i += 1
            continue
        # Tables
        if line.lstrip().startswith("|") and "|" in line[1:]:
            rows, next_i = _parse_table(lines, i)
            if rows:
                table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                # Plain black grid for CJK docs (GB-style), accent grid otherwise.
                table.style = "Table Grid" if cjk else "Light Grid Accent 1"
                for r_idx, row in enumerate(rows):
                    for c_idx, cell_text in enumerate(row):
                        if c_idx < len(table.rows[r_idx].cells):
                            cell = table.rows[r_idx].cells[c_idx]
                            cell.text = ""
                            p = cell.paragraphs[0]
                            _add_runs_with_markdown_inline(p, cell_text)
                            if r_idx == 0:
                                for run in p.runs:
                                    run.bold = True
                i = next_i
                continue
        # Headings
        if line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        # Bulleted list
        elif line.lstrip().startswith(("- ", "* ")):
            text = line.lstrip()[2:]
            p = doc.add_paragraph(style="List Bullet")
            _add_runs_with_markdown_inline(p, text)
        # Numbered list
        elif re.match(r"^\s*\d+\.\s+", line):
            text = re.sub(r"^\s*\d+\.\s+", "", line)
            p = doc.add_paragraph(style="List Number")
            _add_runs_with_markdown_inline(p, text)
        # Blockquote
        elif line.startswith("> "):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.4)
            r = p.add_run(line[2:])
            r.italic = True
        # Horizontal rule
        elif re.match(r"^\s*-{3,}\s*$", line):
            p = doc.add_paragraph()
            p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            p.add_run("* * *")
        # Regular paragraph
        else:
            p = doc.add_paragraph()
            _add_runs_with_markdown_inline(p, line)
        i += 1


def write_docx_document(
    *,
    output_path: Path | str,
    title: str,
    sections: Iterable[tuple[str, str]],
    subtitle: str | None = None,
    disclaimer: str | None = None,
    lang: str | None = None,
    layout: str | None = None,
    watermark: str | None = None,
) -> Path:
    """Render a legal-style .docx document.

    Each section is a (heading, body_markdown) pair.

    ``lang`` forces the typography track: ``"zh-CN"`` (or any ``zh*``) for
    CJK-native styling, anything else for the Latin default. When ``None``,
    CJK is auto-detected from the title/subtitle/sections. ``layout``
    accepts ``"gbt9704"`` for a GB/T 9704-style official-document layout
    (implies CJK).

    ``watermark`` stamps a repeating watermark banner on every page (e.g.
    ``"DO NOT FILE · 禁止对外提交"``) — used when the citation audit returns a
    DO-NOT-FILE verdict so an exported draft cannot be mistaken for a filed
    document.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if layout not in (None, "gbt9704"):
        raise ValueError(f"Unknown layout: {layout!r} (expected None or 'gbt9704')")
    section_pairs = [(heading, body) for heading, body in sections]
    gbt9704 = layout == "gbt9704"
    if lang is None:
        cjk = gbt9704 or _detect_cjk(title, section_pairs, subtitle)
    else:
        cjk = lang.lower().startswith("zh")

    doc = Document()
    _set_default_style(doc)
    _set_margins(doc, gbt9704=gbt9704)
    if cjk:
        _apply_cjk_typography(doc, gbt9704=gbt9704)

    if watermark:
        # Repeat the verdict on every page header, and add a page-one banner.
        _apply_watermark(doc, watermark, cjk=cjk)
        banner = doc.add_paragraph()
        banner.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        br = banner.add_run(watermark)
        br.bold = True
        br.font.size = Pt(16)
        br.font.color.rgb = RGBColor(0xC9, 0x44, 0x42)
        if cjk:
            _set_run_east_asia_font(br, _EA_HEADING_FONT)

    title_p = doc.add_heading(title, level=0)
    title_p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    if subtitle:
        sub = doc.add_paragraph()
        sub.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
        r = sub.add_run(subtitle)
        if cjk:
            _set_run_east_asia_font(r, _EA_SUBTITLE_FONT)
        else:
            r.italic = True
    if disclaimer:
        p = doc.add_paragraph()
        r = p.add_run(disclaimer)
        if cjk:
            _set_run_east_asia_font(r, _EA_SUBTITLE_FONT)
        else:
            r.italic = True
        r.font.size = Pt(9)

    for heading, body in section_pairs:
        if heading:
            doc.add_heading(heading, level=2)
        _render_body_markdown(doc, body, cjk=cjk)

    doc.save(str(output_path))
    return output_path
