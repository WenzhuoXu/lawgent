"""Read OFD (GB/T 33190, 版式文档) as text with ``[page N]`` markers.

OFD is the national fixed-layout format: courts, tax e-invoices (数电票) and
government portals issue it. It is a zip of XML: ``OFD.xml`` points at each
document's ``Document.xml``, which lists pages (and template pages); a page's
``Content.xml`` holds ``TextObject``s whose ``TextCode`` children carry the
text and its position in millimetres.

Surveyed first (2026-10-07): DocVortex (MIT) reads OFD but pulls 26
dependencies — one an upgrade of pypdfium2 the shared env cannot take — and is
a month old; easyofd (Apache-2.0, 441★) depends on PyMuPDF, which is AGPL and
cannot ship in this MIT tree; ofdrw is a JVM library. Reading text needs only
the zip and the positions, so it is done here: text objects from the page and
its templates, grouped into lines by baseline, each line read left to right.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any, Optional

_CJK = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(el: Any, name: str) -> list[Any]:
    return [c for c in el if _local(c.tag) == name]


def _find(el: Any, *path: str) -> Optional[Any]:
    node = el
    for name in path:
        found = None
        for child in node:
            if _local(child.tag) == name:
                found = child
                break
        if found is None:
            return None
        node = found
    return node


def _resolve(base_dir: str, loc: str) -> str:
    loc = (loc or "").strip()
    if loc.startswith("/"):
        return loc.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, loc))


def _floats(value: Optional[str]) -> list[float]:
    out = []
    for token in (value or "").replace(",", " ").split():
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def _text_items(root: Any) -> list[tuple[float, float, float, str]]:
    """(baseline y, x, font size, text) for every TextCode under ``root``."""
    items = []
    for obj in root.iter():
        if _local(obj.tag) != "TextObject":
            continue
        box = _floats(obj.get("Boundary"))
        bx, by = (box[0], box[1]) if len(box) >= 2 else (0.0, 0.0)
        ctm = _floats(obj.get("CTM"))
        if len(ctm) == 6:
            bx, by = bx + ctm[4], by + ctm[5]
        size = (_floats(obj.get("Size")) or [3.5])[0]
        for code in obj:
            if _local(code.tag) != "TextCode":
                continue
            text = code.text or ""
            if not text.strip():
                continue
            x = bx + ((_floats(code.get("X")) or [0.0])[0])
            y = by + ((_floats(code.get("Y")) or [0.0])[0])
            items.append((y, x, size, text))
    return items


def _lines(items: list[tuple[float, float, float, str]]) -> list[str]:
    items = sorted(items, key=lambda it: (it[0], it[1]))
    lines: list[list[tuple[float, float, float, str]]] = []
    for item in items:
        if lines:
            current = lines[-1]
            tolerance = 0.6 * min(item[2], min(i[2] for i in current))
            if abs(item[0] - current[0][0]) <= max(tolerance, 0.8):
                current.append(item)
                continue
        lines.append([item])
    out = []
    for line in lines:
        line.sort(key=lambda it: it[1])
        text = ""
        for _, _, _, piece in line:
            piece = piece.strip()
            if text and not (_CJK.match(text[-1]) and _CJK.match(piece[0])):
                text += " "
            text += piece
        out.append(text)
    return out


def read_ofd_text(path: Path) -> str:
    """Text of every page of every document in an .ofd package."""
    from lxml import etree

    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())

        def parse(name: str) -> Optional[Any]:
            if name not in names:
                return None
            try:
                return etree.fromstring(zf.read(name))
            except etree.XMLSyntaxError:
                return None

        root = parse("OFD.xml")
        if root is None:
            raise ValueError(f"{path.name} is not an OFD package (no OFD.xml)")
        out: list[str] = []
        page_no = 0
        title = ""
        for body in _children(root, "DocBody"):
            info = _find(body, "DocInfo", "Title")
            if info is not None and (info.text or "").strip() and not title:
                title = info.text.strip()
            doc_root = _find(body, "DocRoot")
            if doc_root is None or not (doc_root.text or "").strip():
                continue
            doc_path = _resolve("", doc_root.text)
            document = parse(doc_path)
            if document is None:
                continue
            base = posixpath.dirname(doc_path)
            templates: dict[str, str] = {}
            common = _find(document, "CommonData")
            if common is not None:
                for tpl in _children(common, "TemplatePage"):
                    templates[tpl.get("ID", "")] = _resolve(base, tpl.get("BaseLoc", ""))
            pages = _find(document, "Pages")
            for page in _children(pages, "Page") if pages is not None else []:
                page_no += 1
                content = parse(_resolve(base, page.get("BaseLoc", "")))
                items: list[tuple[float, float, float, str]] = []
                if content is not None:
                    for tpl_ref in _children(content, "Template"):
                        tpl = parse(templates.get(tpl_ref.get("TemplateID", ""), ""))
                        if tpl is not None:
                            items.extend(_text_items(tpl))
                    items.extend(_text_items(content))
                lines = _lines(items)
                out.append(f"[page {page_no}]\n" + ("\n".join(lines) if lines else "[no text on this page]"))
    header = f"[OFD document: {title}]" if title else "[OFD document]"
    if not page_no:
        return header + "\n[warning: no pages found in the package]"
    return header + "\n" + "\n\n".join(out)


__all__ = ["read_ofd_text"]
