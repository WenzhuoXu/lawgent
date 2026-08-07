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
    """Markdown-style text via the tiered converter or python-docx (fallback).

    Word comments are not surfaced by either converter; we always overlay them
    from ``_docx_comments`` when ``include_comments`` is True.
    """
    from ._convert import convert_to_markdown

    try:
        body = convert_to_markdown(path).strip()
    except Exception:
        body = _docx_text_via_python_docx(path)
    if include_comments:
        comments = _docx_comments(path)
        if comments:
            body = body + "\n" + "\n".join(_format_comments(comments))
    return body


def inspect_docx_document(path: Path) -> dict[str, Any]:
    from docx import Document

    doc = Document(str(path))
    headings: list[dict[str, Any]] = []
    for idx, para in enumerate(doc.paragraphs, start=1):
        style = para.style.name if para.style is not None else ""
        if style.lower().startswith("heading") and para.text.strip():
            headings.append({"paragraph": idx, "style": style, "text": para.text.strip()})
    comments = _docx_comments(path)
    props = doc.core_properties
    return {
        "filename": path.name,
        "paragraph_count": len(doc.paragraphs),
        "table_count": len(doc.tables),
        "section_count": len(doc.sections),
        "comment_count": len(comments),
        "headings": headings,
        "core_properties": {
            "title": props.title or "",
            "author": props.author or "",
            "subject": props.subject or "",
            "keywords": props.keywords or "",
            "last_modified_by": props.last_modified_by or "",
        },
    }


def _replace_paragraph_text(para: Any, find: str, replace: str) -> int:
    count = 0
    for run in para.runs:
        if find in run.text:
            count += run.text.count(find)
            run.text = run.text.replace(find, replace)
    if count or find not in para.text:
        return count
    # Replacement spanning run boundaries. This preserves paragraph style but
    # necessarily collapses mixed run formatting in that paragraph.
    new_text = para.text.replace(find, replace)
    count = para.text.count(find)
    if para.runs:
        para.runs[0].text = new_text
        for run in para.runs[1:]:
            run.text = ""
    else:
        para.add_run(new_text)
    return count


def edit_docx_text_document(source_path: Path, output_path: Path, replacements: list[dict[str, str]]) -> dict[str, Any]:
    from docx import Document

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document(str(source_path))
    counts: dict[str, int] = {}
    for repl in replacements:
        find = str(repl.get("find") or "")
        replace = str(repl.get("replace") or "")
        if not find:
            continue
        total = 0
        for para in doc.paragraphs:
            total += _replace_paragraph_text(para, find, replace)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        total += _replace_paragraph_text(para, find, replace)
        counts[find] = total
    doc.save(str(output_path))
    return {"output_path": str(output_path), "replacements": counts}


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


_FLOWCHART_KINDS: dict[str, str] = {
    # MSO_SHAPE enum name -> our FlowNode.kind
    "FLOWCHART_PROCESS": "process",
    "FLOWCHART_DECISION": "decision",
    "FLOWCHART_TERMINATOR": "terminator",
    "FLOWCHART_DATA": "io",
    "FLOWCHART_PREDEFINED_PROCESS": "subprocess",
    "FLOWCHART_MANUAL_INPUT": "data",
    "FLOWCHART_DOCUMENT": "io",
}


def _emu_to_inches(emu: int | None) -> float | None:
    if emu is None:
        return None
    return round(float(emu) / 914400.0, 3)


def _extract_flowchart_payload(slide) -> dict[str, Any]:
    """Group flowchart node-shapes and line connectors on one slide.

    Returns ``{nodes: [...], edges: [...]}`` with best-effort edge pairing
    by matching each line shape's endpoints to the nearest node bounding
    box. ``confidence`` on each edge is the inverse distance heuristic,
    so the LLM can tell mis-pairings from solid ones.
    """
    try:
        from pptx.enum.shapes import MSO_SHAPE
    except Exception:  # pragma: no cover - python-pptx absent
        MSO_SHAPE = None

    nodes: list[dict[str, Any]] = []
    raw_lines: list[dict[str, Any]] = []

    for idx, shape in enumerate(slide.shapes):
        try:
            ast = shape.auto_shape_type
        except Exception:
            ast = None
        name = getattr(ast, "name", None) or ""
        if name in _FLOWCHART_KINDS:
            text = ""
            if getattr(shape, "has_text_frame", False):
                text = shape.text_frame.text.strip()
            nodes.append({
                "shape_index": idx,
                "kind": _FLOWCHART_KINDS[name],
                "text": text,
                "x": _emu_to_inches(shape.left),
                "y": _emu_to_inches(shape.top),
                "w": _emu_to_inches(shape.width),
                "h": _emu_to_inches(shape.height),
                "shape_name": getattr(shape, "name", ""),
            })
        elif name == "LINE_INVERSE" or name == "LINE" or (
            # OOXML line shapes show up as auto_shape_type=None but with
            # prstGeom prst="line". Detect from the underlying element.
            ast is None and _shape_is_line(shape)
        ):
            x = _emu_to_inches(shape.left)
            y = _emu_to_inches(shape.top)
            w = _emu_to_inches(shape.width)
            h = _emu_to_inches(shape.height)
            if x is None or y is None or w is None or h is None:
                continue
            raw_lines.append({
                "shape_index": idx,
                "x1": x, "y1": y,
                "x2": x + w, "y2": y + h,
            })

    edges: list[dict[str, Any]] = []
    for line in raw_lines:
        from_idx, from_d = _nearest_node(nodes, line["x1"], line["y1"])
        to_idx, to_d = _nearest_node(nodes, line["x2"], line["y2"])
        if from_idx is None or to_idx is None:
            continue
        # Confidence ~= 1 / (1 + total_distance_in_inches). > 0.5 means both
        # endpoints landed within ~1 inch of a node center.
        confidence = round(1.0 / (1.0 + from_d + to_d), 3)
        edges.append({
            "shape_index": line["shape_index"],
            "from_node": from_idx,
            "to_node": to_idx,
            "confidence": confidence,
        })
    return {"nodes": nodes, "edges": edges}


def _shape_is_line(shape) -> bool:
    try:
        el = shape._element
        ns = {"a": _A_NS}
        prst = el.find(".//a:prstGeom", ns)
        return prst is not None and prst.get("prst") == "line"
    except Exception:
        return False


def _nearest_node(
    nodes: list[dict[str, Any]], px: float, py: float
) -> tuple[int | None, float]:
    """Return (shape_index, distance_inches) for the node nearest (px, py).

    Distance is measured from the line endpoint to the node center. Returns
    (None, inf) when ``nodes`` is empty.
    """
    best_idx: int | None = None
    best_d = float("inf")
    for n in nodes:
        if n.get("x") is None:
            continue
        cx = n["x"] + (n.get("w") or 0) / 2.0
        cy = n["y"] + (n.get("h") or 0) / 2.0
        d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
        if d < best_d:
            best_d = d
            best_idx = int(n["shape_index"])
    return best_idx, best_d


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
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    text = shape.text.strip()
                    if text:
                        text_runs.append(text)
                if getattr(shape, "has_table", False):
                    table_count += 1
                if getattr(shape, "shape_type", None) == 13:
                    image_count += 1
            notes = notes_by_slide.get(idx, [])
            fc_payload = _extract_flowchart_payload(slide)
            payload: dict[str, Any] = {
                "slide": idx,
                "layout": slide.slide_layout.name,
                "shape_count": len(slide.shapes),
                "table_count": table_count,
                "image_count": image_count,
                "text_run_count": len(text_runs),
                "notes_run_count": len(notes),
                "title": text_runs[0] if text_runs else "",
                "flowchart_node_count": len(fc_payload["nodes"]),
                "flowchart_edge_count": len(fc_payload["edges"]),
            }
            if fc_payload["nodes"]:
                payload["flowchart"] = fc_payload
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


def reshape_xlsx_workbook(
    source_path: Path,
    output_path: Path,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply an ordered list of structural ops to a copy of ``source_path``.

    Supported ``op`` values, all keyed by ``sheet`` (worksheet name):

    - ``insert_rows`` / ``delete_rows``        — ``at``, ``count`` (default 1)
    - ``insert_cols`` / ``delete_cols``        — ``at``, ``count`` (default 1)
    - ``move_range``                           — ``range``, ``rows``, ``cols``,
      ``translate`` (bool, defaults True; rewrites formula refs)
    - ``copy_row``                             — ``src_row``, ``dest_row``;
      copies values, per-cell styles, row height, and overlapping merges
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
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter, column_index_from_string
    from openpyxl.utils.cell import range_boundaries

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

            if kind == "insert_rows":
                at = int(op["at"])
                count = int(op.get("count") or 1)
                ws.insert_rows(at, amount=count)
                entry.update({"at": at, "count": count})
            elif kind == "delete_rows":
                at = int(op["at"])
                count = int(op.get("count") or 1)
                ws.delete_rows(at, amount=count)
                entry.update({"at": at, "count": count})
            elif kind == "insert_cols":
                at = int(op["at"])
                count = int(op.get("count") or 1)
                ws.insert_cols(at, amount=count)
                entry.update({"at": at, "count": count})
            elif kind == "delete_cols":
                at = int(op["at"])
                count = int(op.get("count") or 1)
                ws.delete_cols(at, amount=count)
                entry.update({"at": at, "count": count})
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
                for col in range(1, max_col + 1):
                    src = ws.cell(src_row, col)
                    dst = ws.cell(dest_row, col)
                    dst.value = src.value
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
                            base = cell.font
                            cell.font = Font(
                                name=font_kwargs.get("name", base.name),
                                size=font_kwargs.get("size", base.size),
                                bold=font_kwargs.get("bold", base.bold),
                                italic=font_kwargs.get("italic", base.italic),
                                color=font_kwargs.get("color", getattr(base.color, "rgb", None) if base.color else None),
                            )
                        if fill is not None:
                            cell.fill = _shallow_copy(fill)
                        if border is not None:
                            cell.border = _shallow_copy(border)
                        if align_kwargs:
                            base = cell.alignment
                            cell.alignment = Alignment(
                                horizontal=align_kwargs.get("horizontal", base.horizontal),
                                vertical=align_kwargs.get("vertical", base.vertical),
                                wrap_text=align_kwargs.get("wrap_text", base.wrap_text),
                            )
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
                raise ValueError(f"Unknown structural op: {kind!r}")

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
        _save_with_dir(wb, output_path)
        return {
            "path": str(output_path.resolve()),
            "source_sheet": sheet,
            "new_sheet": cleaned,
        }
    finally:
        wb.close()


def render_xlsx_to_images(
    source_path: Path,
    out_dir: Path,
    filename_prefix: str,
    dpi: int,
) -> dict[str, Any]:
    """Convert an .xlsx to PDF via LibreOffice, then to per-page JPGs.

    Mirrors the docx/pptx visual-QA path. Requires ``soffice`` on PATH.
    """
    import shutil
    import subprocess

    from .pdf import render_pdf_to_images

    soffice = shutil.which("soffice")
    if not soffice:
        raise RuntimeError("render_xlsx_to_images requires soffice on PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(source_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    pdf = out_dir / f"{source_path.stem}.pdf"
    if not pdf.is_file():
        candidates = sorted(out_dir.glob("*.pdf"))
        if not candidates:
            raise RuntimeError(f"soffice did not produce a PDF in {out_dir}")
        pdf = candidates[0]
    rendered = render_pdf_to_images(pdf, out_dir, filename_prefix, dpi)
    return {"pdf": str(pdf.resolve()), **rendered}


# ---------------------------------------------------------------------------
# DOCX structural-edit primitives
# ---------------------------------------------------------------------------


def reshape_docx_document(
    source_path: Path,
    output_path: Path,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply an ordered list of structural ops to a copy of ``source_path``.

    Supported ``op`` values:

    - ``insert_paragraph``   — ``after`` (0-based para index; -1 prepends),
      ``text``, optional ``style`` (paragraph style name like ``Heading 2``)
    - ``delete_paragraphs``  — ``indices`` (list[int]) **or** ``range``
      ([start, end_inclusive])
    - ``set_paragraph_style``— ``index``, ``style``
    - ``replace_paragraph``  — ``index``, ``text`` (keeps style; collapses runs)
    - ``insert_table_row``   — ``table_index``, ``at`` (0-based; -1 appends),
      ``cells`` (list[str])
    - ``delete_table_rows``  — ``table_index``, ``indices`` (list[int])
    - ``set_cell_text``      — ``table_index``, ``row``, ``col``, ``text``
    """
    from docx import Document
    from docx.oxml.ns import qn

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
            if after == -1 or not paras:
                # Prepend: insert before the first body element.
                body = doc.element.body
                new_para = doc.add_paragraph(text=text)
                if style:
                    new_para.style = doc.styles[str(style)]
                body.insert(0, new_para._element)
                # add_paragraph already appended it to body — remove the
                # duplicate trailing one.
                body.remove(new_para._element) if False else None  # noqa: E501
                entry.update({"after": after, "inserted_index": 0})
            else:
                if after >= len(paras):
                    raise ValueError(f"insert_paragraph after={after} out of range (paragraphs={len(paras)})")
                anchor = paras[after]
                new_p = anchor._element.addnext(
                    _docx_make_paragraph(doc, text, style)
                )
                _ = new_p
                entry.update({"after": after, "inserted_index": after + 1})
        elif kind == "delete_paragraphs":
            indices = op.get("indices")
            if indices is None and "range" in op:
                rng = op["range"]
                indices = list(range(int(rng[0]), int(rng[1]) + 1))
            if not indices:
                raise ValueError("delete_paragraphs requires 'indices' or 'range'")
            paras = list(doc.paragraphs)
            removed = 0
            for i in sorted({int(x) for x in indices}, reverse=True):
                if 0 <= i < len(paras):
                    el = paras[i]._element
                    el.getparent().remove(el)
                    removed += 1
            entry.update({"removed": removed})
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
            para = paras[i]
            for run in para.runs:
                run.text = ""
            if para.runs:
                para.runs[0].text = text
            else:
                para.add_run(text)
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
                        new_row.cells[col].text = value
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
            removed = 0
            for i in indices:
                if 0 <= i < len(table.rows):
                    row = table.rows[i]
                    row._tr.getparent().remove(row._tr)
                    removed += 1
            entry.update({"table_index": t_idx, "removed": removed})
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
                raise ValueError(f"set_cell_text row/col out of range")
            table.rows[row].cells[col].text = text
            entry.update({"table_index": t_idx, "row": row, "col": col})
        else:
            raise ValueError(f"Unknown structural op: {kind!r}")
        applied.append(entry)
        _ = qn  # silence linter; we keep it imported for callers that need ns

    doc.save(str(output_path))
    return {
        "path": str(output_path.resolve()),
        "applied_count": len(applied),
        "applied": applied,
    }


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


def _nth_flowchart_shape(slide, n: int):
    """Return the n-th (0-based) shape on ``slide`` whose auto_shape_type
    maps to one of our recognized flowchart kinds."""
    seen = 0
    for shape in slide.shapes:
        try:
            ast = shape.auto_shape_type
        except Exception:
            ast = None
        name = getattr(ast, "name", None) or ""
        if name in _FLOWCHART_KINDS:
            if seen == n:
                return shape
            seen += 1
    return None


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
    - ``set_flow_node_text``— ``slide`` (1-based), ``node_index`` (0-based
      among flowchart shapes on the slide, as reported by ``inspect_pptx``)
      + ``text``
    - ``move_flow_node``   — ``slide`` (1-based), ``node_index``
      (0-based), ``x``, ``y`` in inches; optional ``w``, ``h``
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
                for sh in slide.shapes:
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
        elif kind in ("set_flow_node_text", "move_flow_node"):
            slide_idx = int(op["slide"]) - 1
            slide = prs.slides[slide_idx]
            node_idx = int(op.get("node_index", 0))
            target = _nth_flowchart_shape(slide, node_idx)
            if target is None:
                raise ValueError(
                    f"flowchart node #{node_idx} not found on slide {slide_idx+1}"
                )
            if kind == "set_flow_node_text":
                if not getattr(target, "has_text_frame", False):
                    raise ValueError("Target flowchart node has no text frame")
                target.text_frame.text = str(op.get("text") or "")
                entry.update({"slide": slide_idx + 1, "node_index": node_idx})
            else:
                x_in = float(op["x"])
                y_in = float(op["y"])
                target.left = int(x_in * 914400)
                target.top = int(y_in * 914400)
                if "w" in op:
                    target.width = int(float(op["w"]) * 914400)
                if "h" in op:
                    target.height = int(float(op["h"]) * 914400)
                entry.update({"slide": slide_idx + 1, "node_index": node_idx,
                              "x": x_in, "y": y_in})
        else:
            raise ValueError(f"Unknown structural op: {kind!r}")
        applied.append(entry)

    prs.save(str(output_path))
    return {
        "path": str(output_path.resolve()),
        "applied_count": len(applied),
        "applied": applied,
    }
