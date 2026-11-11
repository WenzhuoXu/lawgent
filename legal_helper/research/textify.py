"""Bytes from a legal database → plain text that keeps its line structure.

Section detection reads headings at the start of a line, so every converter
here keeps block boundaries as newlines (the connector-layer helper collapses
all whitespace into one line, which is right for a snippet and wrong for a
statute). PDF, Word, OFD and legacy formats go through the one reader every
upload uses (``documents.intake.read_any``): computed numbering labels for
Word, ``[page N]`` markers and OCR for image-only PDF pages.
"""

from __future__ import annotations

import hashlib
import re
import warnings
from pathlib import Path
from typing import Optional

_BLOCK_TAGS = (
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
    "article", "blockquote", "pre", "table", "dt", "dd", "hr", "td", "th",
)
_DROP_TAGS = ("script", "style", "noscript", "header", "footer", "nav", "form", "button", "svg")
#: XML elements that run inside a line (emphasis, references, footnote marks).
_INLINE_XML = {
    "i", "e", "b", "u", "su", "fr", "sup", "sub", "emphasis", "strong", "em", "span", "a", "ref",
    "citation", "citationsubref", "abbreviation", "acronym", "term", "definition", "rubytext",
    "ruby", "rt", "inline", "date", "term", "addition", "substitution", "repeal", "commentaryref",
    "footnoteref", "noteref", "authorialnote", "ins", "del", "q", "quote",
}

_SUFFIX_BY_TYPE = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/rtf": ".rtf",
    "application/ofd": ".ofd",
    "application/vnd.oasis.opendocument.text": ".odt",
}


def raw_dir() -> Path:
    from ..config import load_settings

    root = Path(load_settings().state_dir) / "legal_docs" / "raw"
    root.mkdir(parents=True, exist_ok=True)
    return root


def normalize_lines(text: str) -> str:
    """Trim each line, collapse runs of blank lines, keep line breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in text.split("\n")]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    return "\n".join(out).strip()


def html_to_text(html: str, *, xml: bool = False, root_selector: Optional[str] = None) -> str:
    """HTML/XHTML/XML → text with one block per line.

    ``root_selector`` (a CSS selector) narrows to the element holding the
    document body, so site chrome does not end up in the statute.
    """
    from bs4 import BeautifulSoup

    try:
        from bs4 import XMLParsedAsHTMLWarning
    except ImportError:  # pragma: no cover - old bs4
        XMLParsedAsHTMLWarning = Warning  # type: ignore[assignment,misc]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml-xml" if xml else "lxml")
    if root_selector:
        node = soup.select_one(root_selector)
        if node is not None:
            soup = node  # type: ignore[assignment]
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    if xml:
        # Legal XML (eCFR DIV/HEAD/P, legislation.gov.uk CLML, e-Gov, Akoma
        # Ntoso) marks blocks with its own vocabulary: every element that is
        # not an inline run starts a line.
        for tag in soup.find_all(True):
            if tag.name.lower() not in _INLINE_XML:
                tag.insert_before("\n")
                tag.insert_after("\n")
        return normalize_lines(soup.get_text())
    # A table row becomes one line ("1 | This request for a preliminary
    # ruling…", "EUR 250 | for all flights of 1 500 km or less"): CJEU
    # judgments number their paragraphs in a layout table.
    for tr in reversed(soup.find_all("tr")):
        cell_tags = tr.find_all(["td", "th"], recursive=False)
        cells = [" ".join(c.get_text(" ").split()) for c in cell_tags]
        if len([c for c in cells if c]) < 2 or any(len(c) > 1500 for c in cells):
            # A layout table holding a whole document (Lawphil puts an act in
            # one cell): keep its blocks rather than flatten it to one line.
            continue
        line = " | ".join(c for c in cells if c)
        tr.replace_with(f"\n{line}\n" if line else "\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        if tag.name == "br":
            tag.replace_with("\n")
            continue
        if tag.name in ("td", "th"):
            tag.insert_after(" | ")
            continue
        tag.insert_before("\n")
        tag.insert_after("\n")
    return normalize_lines(soup.get_text())


def bytes_to_text(data: bytes, content_type: str = "", *, name_hint: str = "") -> str:
    """Convert a downloaded body to text by its type (header, name or magic)."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    suffix = _SUFFIX_BY_TYPE.get(ctype) or Path(name_hint.split("?")[0]).suffix.lower()
    if not suffix or suffix in (".htm", ".html", ".xhtml", ".xml", ".txt", ".json"):
        head = data[:8]
        if head.startswith(b"%PDF"):
            suffix = ".pdf"
        elif head.startswith(b"PK"):
            suffix = ".docx" if b"word/" in data[:4000] else (suffix or ".zip")
        elif head.startswith(b"\xd0\xcf\x11\xe0"):
            suffix = ".doc"
    if suffix in (".pdf", ".docx", ".doc", ".rtf", ".ofd", ".odt", ".wps"):
        return _via_reader(data, suffix)
    text = _decode(data, content_type)
    if "html" in ctype or "xhtml" in ctype or suffix in (".htm", ".html", ".xhtml") or (
        not ctype and text.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))
    ):
        return html_to_text(text)
    if "xml" in ctype or suffix == ".xml":
        return html_to_text(text, xml=True)
    return normalize_lines(text)


def _decode(data: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "gb18030", "big5", "cp1252"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _via_reader(data: bytes, suffix: str) -> str:
    from ..documents.intake import read_any

    digest = hashlib.sha256(data).hexdigest()[:32]
    path = raw_dir() / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    text = read_any(path)
    # The reader writes Markdown ("# 第四节", "**Điều 1. …**") and marks running
    # heads ("[Header] …"); a statute's own labels are the structure here, and
    # a heading must start its line to be found.
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\[(?:Header|Footer)\][^\n]*$", "", text, flags=re.M)
    text = re.sub(r"^(\s*)(\*\*|__)(.+?)\2", r"\1\3", text, flags=re.M)
    text = re.sub(r"^(\s*)(\*\*|__)", r"\1", text, flags=re.M)
    return normalize_lines(text)
