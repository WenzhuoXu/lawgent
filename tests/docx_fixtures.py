"""Programmatic DOCX fixtures for the intake / editing tests.

Real uploads carry constructs python-docx cannot author — tracked changes,
footnote references, auto-numbering in Chinese counting formats, fields,
headers and footers. Each review defect was reproduced on fixtures like these,
so the regression tests build them the same way: start from python-docx's
default package (styles, settings, a section) and splice raw OOXML in.

Not a test module (no ``test_`` prefix); test files import it by name, which
works because pytest puts ``tests/`` on ``sys.path`` for rootdir-less suites.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Mapping

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_REL_TYPES = {
    "numbering": f"{R_NS}/numbering",
    "footnotes": f"{R_NS}/footnotes",
    "endnotes": f"{R_NS}/endnotes",
    "comments": f"{R_NS}/comments",
    "header": f"{R_NS}/header",
    "footer": f"{R_NS}/footer",
    "hyperlink": f"{R_NS}/hyperlink",
    "image": f"{R_NS}/image",
}
_CONTENT_TYPES = {
    "numbering": "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
    "footnotes": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    "endnotes": "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
    "comments": "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
    "header": "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
    "footer": "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
}

NS_DECL = (
    f'xmlns:w="{W_NS}" xmlns:r="{R_NS}" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)


def p(text: str = "", *, style: str = "", num: tuple[int, int] | None = None, runs: str = "") -> str:
    """One ``w:p``. ``runs`` is raw run XML appended after ``text``."""
    ppr = ""
    parts = []
    if style:
        parts.append(f'<w:pStyle w:val="{style}"/>')
    if num is not None:
        num_id, ilvl = num
        parts.append(f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/></w:numPr>')
    if parts:
        ppr = "<w:pPr>" + "".join(parts) + "</w:pPr>"
    body = r(text) if text else ""
    return f"<w:p>{ppr}{body}{runs}</w:p>"


def r(text: str, *, bold: bool = False) -> str:
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>'


def ins(text: str, *, author: str = "Counterparty", rid: int = 901, date: str = "2026-09-01T10:00:00Z") -> str:
    return (
        f'<w:ins w:id="{rid}" w:author="{author}" w:date="{date}">'
        f'<w:r><w:t xml:space="preserve">{_esc(text)}</w:t></w:r></w:ins>'
    )


def dele(text: str, *, author: str = "Counterparty", rid: int = 902, date: str = "2026-09-01T10:00:00Z") -> str:
    return (
        f'<w:del w:id="{rid}" w:author="{author}" w:date="{date}">'
        f'<w:r><w:delText xml:space="preserve">{_esc(text)}</w:delText></w:r></w:del>'
    )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def numbering_xml(abstracts: Mapping[int, list[dict]], nums: Mapping[int, int], overrides: Mapping[int, Mapping[int, int]] | None = None) -> str:
    """``abstracts``: abstractNumId -> [{fmt, text, start, indent?}] per level.
    ``nums``: numId -> abstractNumId. ``overrides``: numId -> {ilvl: start}."""
    out = [f'<w:numbering {NS_DECL}>']
    for aid, levels in abstracts.items():
        out.append(f'<w:abstractNum w:abstractNumId="{aid}">')
        for ilvl, lvl in enumerate(levels):
            restart = ""
            if "restart" in lvl:
                restart = f'<w:lvlRestart w:val="{lvl["restart"]}"/>'
            lgl = "<w:isLgl/>" if lvl.get("lgl") else ""
            out.append(
                f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="{lvl.get("start", 1)}"/>'
                f'<w:numFmt w:val="{lvl["fmt"]}"/>{restart}{lgl}<w:lvlText w:val="{lvl["text"]}"/>'
                f'<w:lvlJc w:val="left"/></w:lvl>'
            )
        out.append("</w:abstractNum>")
    for num_id, aid in nums.items():
        inner = f'<w:abstractNumId w:val="{aid}"/>'
        for ilvl, start in ((overrides or {}).get(num_id) or {}).items():
            inner += f'<w:lvlOverride w:ilvl="{ilvl}"><w:startOverride w:val="{start}"/></w:lvlOverride>'
        out.append(f'<w:num w:numId="{num_id}">{inner}</w:num>')
    out.append("</w:numbering>")
    return "".join(out)


def build_docx(
    path: Path,
    body: str,
    *,
    numbering: str = "",
    footnotes: str = "",
    endnotes: str = "",
    comments: str = "",
    header: str = "",
    footer: str = "",
    hyperlinks: Mapping[str, str] | None = None,
    settings_extra: str = "",
    extra_styles: str = "",
) -> Path:
    """Write a .docx whose body is ``body`` (raw ``w:p`` / ``w:tbl`` XML).

    ``header`` / ``footer`` are inner XML (paragraphs) of a default header or
    footer; ``hyperlinks`` maps relationship id -> external URL.
    """
    from docx import Document

    buf = io.BytesIO()
    Document().save(buf)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zin:
        files = {name: zin.read(name) for name in zin.namelist()}

    doc_xml = files["word/document.xml"].decode("utf-8")
    sect = re.search(r"<w:sectPr\b.*?</w:sectPr>", doc_xml, re.S)
    sect_xml = sect.group(0) if sect else ""
    rels = files["word/_rels/document.xml.rels"].decode("utf-8")
    ctypes = files["[Content_Types].xml"].decode("utf-8")

    def add_part(kind: str, name: str, xml: str, rid: str) -> None:
        nonlocal rels, ctypes
        files[f"word/{name}"] = xml.encode("utf-8")
        rels = rels.replace(
            "</Relationships>",
            f'<Relationship Id="{rid}" Type="{_REL_TYPES[kind]}" Target="{name}"/></Relationships>',
        )
        if f'PartName="/word/{name}"' not in ctypes:
            ctypes = ctypes.replace(
                "</Types>",
                f'<Override PartName="/word/{name}" ContentType="{_CONTENT_TYPES[kind]}"/></Types>',
            )

    if numbering:
        # The default template already has a numbering part; replace its body.
        if "word/numbering.xml" in files:
            files["word/numbering.xml"] = numbering.encode("utf-8")
        else:
            add_part("numbering", "numbering.xml", numbering, "rIdNum1")
    if footnotes:
        add_part(
            "footnotes",
            "footnotes.xml",
            f'<w:footnotes {NS_DECL}>'
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
            f"{footnotes}</w:footnotes>",
            "rIdFn1",
        )
    if endnotes:
        add_part(
            "endnotes",
            "endnotes.xml",
            f'<w:endnotes {NS_DECL}>'
            '<w:endnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:endnote>'
            '<w:endnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:endnote>'
            f"{endnotes}</w:endnotes>",
            "rIdEn1",
        )
    if comments:
        add_part("comments", "comments.xml", f"<w:comments {NS_DECL}>{comments}</w:comments>", "rIdCm1")
    if header:
        add_part("header", "header1.xml", f"<w:hdr {NS_DECL}>{header}</w:hdr>", "rIdHdr1")
        sect_xml = sect_xml.replace(
            "<w:sectPr>", '<w:sectPr><w:headerReference w:type="default" r:id="rIdHdr1"/>', 1
        ) if "<w:sectPr>" in sect_xml else re.sub(
            r"(<w:sectPr\b[^>]*>)", r'\1<w:headerReference w:type="default" r:id="rIdHdr1"/>', sect_xml, count=1
        )
    if footer:
        add_part("footer", "footer1.xml", f"<w:ftr {NS_DECL}>{footer}</w:ftr>", "rIdFtr1")
        sect_xml = re.sub(
            r"(<w:sectPr\b[^>]*>(?:<w:headerReference[^>]*/>)?)",
            r'\1<w:footerReference w:type="default" r:id="rIdFtr1"/>',
            sect_xml,
            count=1,
        )
    for rid, url in (hyperlinks or {}).items():
        rels = rels.replace(
            "</Relationships>",
            f'<Relationship Id="{rid}" Type="{_REL_TYPES["hyperlink"]}" Target="{url}" '
            'TargetMode="External"/></Relationships>',
        )
    if settings_extra:
        settings = files["word/settings.xml"].decode("utf-8")
        settings = re.sub(r"(<w:settings\b[^>]*>)", lambda m: m.group(1) + settings_extra, settings, count=1)
        files["word/settings.xml"] = settings.encode("utf-8")
    if extra_styles:
        styles = files["word/styles.xml"].decode("utf-8")
        styles = styles.replace("</w:styles>", extra_styles + "</w:styles>")
        files["word/styles.xml"] = styles.encode("utf-8")

    files["word/document.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document {NS_DECL}><w:body>{body}{sect_xml}</w:body></w:document>'
    ).encode("utf-8")
    files["word/_rels/document.xml.rels"] = rels.encode("utf-8")
    files["[Content_Types].xml"] = ctypes.encode("utf-8")

    path = Path(path)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in files.items():
            zout.writestr(name, data)
    return path


def document_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def part_xml(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode("utf-8")


# A PRC-style contract: 第%1条 in chineseCounting, sub-clauses as （%2）.
PRC_CONTRACT_NUMBERING = numbering_xml(
    {
        10: [
            {"fmt": "chineseCounting", "text": "第%1条"},
            {"fmt": "chineseCounting", "text": "（%2）"},
        ],
        11: [
            {"fmt": "decimal", "text": "%1."},
            {"fmt": "decimal", "text": "%1.%2"},
        ],
    },
    {1: 10, 2: 11},
)


__all__ = [
    "PRC_CONTRACT_NUMBERING",
    "build_docx",
    "dele",
    "document_xml",
    "ins",
    "numbering_xml",
    "p",
    "part_xml",
    "r",
]
