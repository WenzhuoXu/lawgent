"""What a DOCX edit lost, compared against its source, without rendering.

Edits used to report success on a file that had silently lost things a
reviewer relies on: a cross-run replacement deleted the runs carrying footnote
and comment references, and the comment survived in ``comments.xml`` anchored
to nothing — the reviewer's note was invisible in Word and nothing said so.
Every DOCX edit tool now compares its output with its input and returns the
losses.

Two layers:

- **ooxml-integrity** (MIT, Dmitry-Kov/ooxml-integrity) when installed: its
  ``compare`` counts constructs and matches comment / footnote bodies by text
  (so "one comment removed, another added" is still caught), and its ``check``
  finds broken references inside the edited file (an orphaned comment, a
  footnote nobody references). Only findings that are new relative to the
  source are reported, so a pre-existing defect is not blamed on the edit.
- **In-tree counts** for what that package does not count — bookmarks,
  fields, section breaks, endnote references, VML pictures — and, when it is
  not installed, for everything: comment anchors, tracked changes, footnote
  references, hyperlinks, images, tables.

Losses only: an edit that *adds* tracked changes or comments is doing its job.
"""

from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# (key, label, xml tag, attribute to identify an item or None, severity,
#  covered by ooxml-integrity's own compare)
_COUNTED: tuple[tuple[str, str, str, Optional[str], str, bool], ...] = (
    ("comment_anchors", "comment anchors", f"{_W}commentReference", f"{_W}id", "error", True),
    ("comment_ranges", "comment ranges", f"{_W}commentRangeStart", f"{_W}id", "error", False),
    ("footnote_refs", "footnote references", f"{_W}footnoteReference", f"{_W}id", "error", True),
    ("endnote_refs", "endnote references", f"{_W}endnoteReference", f"{_W}id", "error", False),
    ("insertions", "tracked insertions", f"{_W}ins", f"{_W}id", "error", True),
    ("deletions", "tracked deletions", f"{_W}del", f"{_W}id", "error", True),
    ("moves", "tracked moves", f"{_W}moveFrom", f"{_W}id", "error", False),
    ("bookmarks", "bookmarks", f"{_W}bookmarkStart", f"{_W}name", "warning", False),
    ("hyperlinks", "hyperlinks", f"{_W}hyperlink", None, "warning", True),
    ("simple_fields", "fields (simple)", f"{_W}fldSimple", f"{_W}instr", "warning", False),
    ("complex_fields", "fields", f"{_W}fldChar", None, "warning", False),
    ("drawings", "images and charts", f"{_W}drawing", None, "error", True),
    ("vml_pictures", "VML pictures", f"{_W}pict", None, "error", False),
    ("section_breaks", "section breaks", f"{_W}sectPr", None, "error", False),
    ("tables", "tables", f"{_W}tbl", None, "error", True),
    ("content_controls", "content controls", f"{_W}sdt", None, "error", True),
)

_STORY_PREFIXES = ("word/document", "word/header", "word/footer", "word/footnotes", "word/endnotes")


def _story_roots(path: Path) -> list[Any]:
    from lxml import etree

    roots = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith(".xml") and name.startswith(_STORY_PREFIXES) and "/_rels/" not in name:
                try:
                    roots.append(etree.fromstring(zf.read(name)))
                except etree.XMLSyntaxError:
                    continue
    return roots


def _inventory(path: Path) -> dict[str, Counter]:
    roots = _story_roots(path)
    out: dict[str, Counter] = {}
    for key, _label, tag, attr, _sev, _covered in _COUNTED:
        counter: Counter = Counter()
        for root in roots:
            for el in root.iter(tag):
                if tag == f"{_W}fldChar" and el.get(f"{_W}fldCharType") != "begin":
                    continue
                if tag == f"{_W}sectPr" and el.getparent() is not None and el.getparent().tag.endswith("sectPrChange"):
                    continue
                if tag == f"{_W}bookmarkStart" and (el.get(attr) or "").startswith("_"):
                    continue  # _GoBack, _Toc… — Word's own transient bookmarks
                ident = (el.get(attr) or "") if attr else ""
                counter[ident] += 1
        out[key] = counter
    return out


# In-tree expectation keys -> the ooxml-integrity element tags (FID001) they
# cover, so a loss the caller asked for is reported as expected there too.
_EXPECTED_TAGS = {
    "hyperlinks": {"hyperlink"},
    "drawings": {"drawing"},
    "tables": {"tbl", "tblHeader"},
    "comment_anchors": {"commentReference"},
    "footnote_refs": {"footnoteReference"},
    "content_controls": {"sdt"},
    "styles": {"pStyle", "rStyle", "numPr"},
}


def _external_findings(source: Path, output: Path, expected: set[str]) -> Optional[list[dict[str, Any]]]:
    try:
        import ooxml_integrity as oi
    except Exception:  # noqa: BLE001 — optional dependency; in-tree counts cover the rest
        return None
    expected_tags = set().union(*(_EXPECTED_TAGS.get(key, set()) for key in expected)) if expected else set()
    findings: list[dict[str, Any]] = []
    try:
        for f in oi.compare(source, output):
            sev = str(getattr(f.severity, "value", f.severity))
            if sev not in {"error", "warn", "warning"}:
                continue
            extra = getattr(f, "extra", None) or {}
            is_expected = (f.code == "FID001" and extra.get("tag") in expected_tags) or (
                f.code == "FID003" and "text" in expected
            )
            finding = {
                "check": f.code,
                "severity": "info" if is_expected else ("error" if sev == "error" else "warning"),
                "message": f.message,
            }
            if is_expected:
                finding["expected"] = True
            findings.append(finding)
        before = {(f.code, f.message) for f in oi.check(source)}
        for f in oi.check(output):
            sev = str(getattr(f.severity, "value", f.severity))
            if sev not in {"error", "warn", "warning"} or (f.code, f.message) in before:
                continue
            findings.append({
                "check": f.code,
                "severity": "error" if sev == "error" else "warning",
                "message": f.message,
                **({"where": f.where} if getattr(f, "where", None) else {}),
            })
    except Exception as exc:  # noqa: BLE001 — a checker fault must not fail the edit
        return [{"check": "ooxml-integrity", "severity": "warning", "message": f"checker failed: {type(exc).__name__}: {exc}"}]
    return findings


def compare_docx_integrity(source: Path, output: Path, *, expected_losses: Iterable[str] = ()) -> dict[str, Any]:
    """Losses in ``output`` relative to ``source``.

    ``expected_losses`` names what the caller asked to remove — in-tree keys
    such as ``"bookmarks"``, plus ``"styles"`` and ``"text"`` for the
    checker's style-reference and text-volume findings — so a paragraph
    delete that removes a bookmark is reported as expected, not as damage.
    """
    expected = set(expected_losses)
    try:
        before = _inventory(Path(source))
        after = _inventory(Path(output))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unchecked", "error": f"{type(exc).__name__}: {exc}"}
    external = _external_findings(Path(source), Path(output), expected)
    findings: list[dict[str, Any]] = []
    for key, label, _tag, attr, severity, covered in _COUNTED:
        if external is not None and covered:
            continue
        b, a = before[key], after[key]
        lost_total = sum(b.values()) - sum(a.values())
        missing = [ident for ident in (b - a) if ident] if attr else []
        if lost_total <= 0 and not missing:
            continue
        finding: dict[str, Any] = {
            "check": key,
            "severity": "info" if key in expected else severity,
            "message": f"{label}: {sum(b.values())} -> {sum(a.values())}",
        }
        if missing:
            finding["lost"] = sorted(missing)[:20]
        if key in expected:
            finding["expected"] = True
        findings.append(finding)
    if external:
        findings.extend(external)
    damaging = [f for f in findings if f["severity"] in {"error", "warning"}]
    return {
        "status": "issues" if damaging else "ok",
        "checker": "ooxml-integrity + in-tree counts" if external is not None else "in-tree counts",
        "findings": findings,
    }


__all__ = ["compare_docx_integrity"]
