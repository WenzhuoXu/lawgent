"""Document tools: write, read, inspect, edit and render Word, PDF,
spreadsheet and slide files."""

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

from anthropic import beta_tool
from pydantic import BaseModel, Field

from ..config import current_settings
from ..logging_setup import log_workflow_event
from ..documents.writers._text import disclaimer_for, resolve_lang
from ..documents import (
    copy_xlsx_worksheet as copy_xlsx_worksheet_in_package,
    edit_docx_text_document,
    edit_pptx_text as edit_pptx_text_in_package,
    edit_xlsx_workbook_checked,
    edit_xlsx_workbook,
    diff_xlsx_workbooks,
    extract_pdf_tables_document,
    inspect_docx_document,
    inspect_pdf_document,
    inspect_pptx_deck,
    inspect_xlsx_range_workbook,
    inspect_xlsx_workbook,
    merge_pdf_files,
    render_pdf_to_images,
    reshape_docx_document,
    reshape_pptx_deck,
    reshape_xlsx_workbook,
    rotate_pdf_file,
    split_pdf_file,
    write_docx_document,
    write_pdf_document,
    write_xlsx_workbook,
)


class Section(BaseModel):
    """One section of a generated legal document."""

    heading: str = Field(
        ...,
        description=(
            "Section heading, rendered as a level-1 heading (empty string for none). "
            "With layout='gbt9704' a leading 一、/（一）/1./（1） sets the GB/T 9704 level."
        ),
    )
    body_markdown: str = Field(
        ...,
        description=(
            "Section body in Markdown (GFM): headings, emphasis, lists nested by indent, "
            "pipe tables, [text](url) links, [^1] footnotes, <br>, ![alt](file under outputs/). "
            "A single newline starts a new paragraph."
        ),
    )


class XlsxChartSeries(BaseModel):
    """One series of a workbook chart, referencing values by A1 range."""

    name: str = Field(default="", description="Series label shown in the legend.")
    values_range: str = Field(..., description="A1-style range of values on this sheet, e.g. B2:B11.")


class XlsxChart(BaseModel):
    """A native openpyxl chart anchored to a sheet cell."""

    type: str = Field(..., description="Chart type: bar, line, pie, or scatter.")
    title: str = Field(default="", description="Optional chart title.")
    anchor: str = Field(default="F2", description="A1 cell where the chart top-left lands, e.g. F2.")
    categories_range: str = Field(default="", description="A1-style range of category labels, e.g. A2:A11.")
    series: list[XlsxChartSeries] = Field(default_factory=list)
    width: float | None = Field(default=None, description="Optional chart width in cm.")
    height: float | None = Field(default=None, description="Optional chart height in cm.")


class XlsxConditionalFormat(BaseModel):
    """A conditional-formatting rule applied to a range.

    Supported types: ``color_scale`` (3-color heatmap), ``cell_is``
    (operator + literal comparison), ``formula`` (custom expression), or
    ``data_bar``.
    """

    range: str = Field(..., description="A1-style range the rule applies to, e.g. B2:B20.")
    type: str = Field(..., description="Rule type: color_scale, cell_is, formula, or data_bar.")
    operator: str = Field(default="equal", description="cell_is operator: equal, notEqual, greaterThan, lessThan, between, etc.")
    formula: list[str] = Field(default_factory=list, description="cell_is/formula operands, e.g. ['\"RED\"'] for == \"RED\".")
    background: str = Field(default="", description="Hex RGB fill for matching cells (cell_is/formula).")
    foreground: str = Field(default="", description="Hex RGB font color for matching cells (cell_is/formula).")
    bold: bool = Field(default=False, description="Bold matching cells (cell_is/formula).")
    color_min: str = Field(default="F8696B", description="Heatmap min color hex (color_scale).")
    color_mid: str = Field(default="FFEB84", description="Heatmap mid color hex (color_scale).")
    color_max: str = Field(default="63BE7B", description="Heatmap max color hex (color_scale).")
    color: str = Field(default="638EC6", description="Data-bar fill hex (data_bar).")


class XlsxDataValidation(BaseModel):
    """A data-validation rule, typically used for drop-down lists or numeric ranges."""

    range: str = Field(..., description="A1-style range, e.g. B2:B20.")
    type: str = Field(default="list", description="Validation type: list, whole, decimal, date, textLength, etc.")
    operator: str = Field(default="between", description="Comparison operator (for whole/decimal/date), e.g. between, greaterThan.")
    formula1: str = Field(default="", description='Primary formula or list source, e.g. \'"Red,Yellow,Green"\' or \'=Lists!$A$1:$A$5\'.')
    formula2: str = Field(default="", description="Secondary formula for between operators.")
    prompt: str = Field(default="", description="Optional input prompt shown when the cell is selected.")
    prompt_title: str = Field(default="", description="Optional prompt title.")
    error_msg: str = Field(default="", description="Optional validation-failure message.")
    error_title: str = Field(default="", description="Optional validation-failure title.")


class XlsxNumberFormat(BaseModel):
    """An Excel number format applied to a column or range."""

    range: str = Field(
        ...,
        description="Column letter (C), column span (C:E) or A1 range (C2:C40).",
    )
    format: str = Field(
        ...,
        description=(
            "Excel number format code, e.g. '#,##0.00', '0.0%', 'yyyy-mm-dd', "
            "'¥#,##0.00', '[$-zh-CN]yyyy\"年\"m\"月\"d\"日\"'."
        ),
    )


class Sheet(BaseModel):
    """One worksheet in a generated Excel workbook.

    Rows may include formula strings (``"=SUM(B2:B9)"``). After writing, every
    formula is computed (LibreOffice, with the ``formulas`` engine as fallback)
    and its value stored in the file, so readers other than Excel see numbers
    rather than blanks; ``#REF!``/``#DIV/0!``/``#NAME?`` results are returned.
    """

    name: str = Field(default="Sheet1", description="Worksheet name, maximum 31 characters in Excel.")
    rows: list[list[str | int | float | bool | None]] = Field(
        default_factory=list,
        description=(
            "Rows of cell values. Strings beginning with '=' are written as live formulas. "
            "A string that is exactly an ISO date ('2026-10-07') or date-time "
            "('2026-10-07 14:30') becomes a real date cell; prefix ' to keep it as text."
        ),
    )
    number_formats: list[XlsxNumberFormat] = Field(
        default_factory=list,
        description="Number formats for columns or ranges (amounts, percentages, dates).",
    )
    charts: list[XlsxChart] = Field(default_factory=list, description="Native charts anchored on this sheet.")
    conditional_formats: list[XlsxConditionalFormat] = Field(
        default_factory=list,
        description="Conditional-formatting rules (heatmap, RED-for-non-compliant, data bars).",
    )
    data_validations: list[XlsxDataValidation] = Field(
        default_factory=list,
        description="Data-validation rules (drop-downs, numeric ranges).",
    )
    freeze_panes: str = Field(default="A2", description="A1 cell to freeze above and to the left of, or '' to disable.")


class CellEdit(BaseModel):
    """One scalar cell replacement in an existing Excel workbook."""

    sheet: str = Field(..., description="Worksheet name to edit.")
    cell: str = Field(..., description="A1-style cell reference, e.g. B2.")
    value: str | int | float | bool | None = Field(
        default=None,
        description="Replacement scalar value. Use null to blank the cell.",
    )


class CheckedCellEdit(CellEdit):
    """A scalar Excel edit guarded by an optional expected current value."""

    check_expected: bool = Field(
        default=False,
        description="When true, refuse the edit unless the current cell value equals expected_value.",
    )
    expected_value: str | int | float | bool | None = Field(
        default=None,
        description="Expected current value when check_expected is true. Use null to require a blank cell.",
    )


class XlsxRangeSpec(BaseModel):
    """A worksheet range in A1 notation."""

    sheet: str = Field(..., description="Worksheet name.")
    range: str = Field(..., description="A1-style range, e.g. A1:J264.")


class DocxTextReplacement(BaseModel):
    """One exact text replacement in a Word document."""

    find: str = Field(..., description="Exact text to replace.")
    replace: str = Field(..., description="Replacement text.")


class SlideTextReplacement(BaseModel):
    """One exact text replacement in an existing PowerPoint slide."""

    slide: int = Field(..., description="1-based slide number.")
    find: str = Field(..., description="Exact text to replace.")
    replace: str = Field(..., description="Replacement text.")


def _safe_filename(name: str, default: str = "document") -> str:
    # Keep Unicode letters/digits (so CJK filenames survive) plus a small set
    # of punctuation safe across Linux/macOS/Windows. Everything else collapses
    # to `_`. Without this, a Chinese filename like "制度流程治理.pptx" loses every
    # character and falls back to `document.pptx`, overwriting prior artifacts
    # and breaking download links keyed on the model-stated name.
    cleaned = "".join(ch if (ch.isalnum() or ch in "._- ") else "_" for ch in name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._- ")
    return cleaned or default


def _output_path(filename: str, extension: str) -> Path:
    settings = current_settings()
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(Path(filename).stem)
    return settings.outputs_dir / f"{stem}.{extension.lstrip('.')}"


def _disclaimer_text(mode: str, reason: str, lang: str) -> tuple[str | None, str]:
    """``(properties text or None, error)`` for the disclaimer parameters.

    The disclaimer is never part of the instrument: 88 of 104 Chinese DOCX
    opened with an English AI disclaimer, including every fresh court filing.
    It goes into the document properties in the document's language, or —
    for a filing, a letter on letterhead, anything the user will sign — it is
    left out, with the reason stated so the omission is a decision on record.
    """
    mode = (mode or "properties").strip().lower()
    if mode == "properties":
        return disclaimer_for("zh" if lang == "zh" else "en"), ""
    if mode == "none":
        if not (reason or "").strip():
            return None, (
                "ERROR: disclaimer='none' needs disclaimer_reason — say why this document "
                "carries no AI notice, e.g. 'court filing (起诉状) to be signed by counsel'."
            )
        return None, ""
    return None, f"ERROR: disclaimer must be 'properties' or 'none' (got {mode!r})."


def _write_time_lint(path: Path) -> dict[str, Any] | None:
    """The file checks that need no render, run on the writer's own output."""
    from ..documents.quality import lint_artifact

    try:
        report = lint_artifact(path)
    except Exception:  # noqa: BLE001 - a linter must never fail a write
        return None
    # Minor findings wait for the render; only a real defect changes the
    # return from a bare path.
    if any(f.severity in {"critical", "major"} for f in report.findings):
        return report.to_payload()
    return None


def _resolve_template(reference_docx: str) -> tuple[Path | None, str]:
    if not (reference_docx or "").strip():
        return None, ""
    template = _resolve_read_path(reference_docx.strip())
    if not template.is_file():
        return None, f"ERROR: reference_docx not found: {template}"
    if template.suffix.lower() not in {".docx", ".dotx"}:
        return None, f"ERROR: reference_docx must be a .docx or .dotx file: {template}"
    return template, ""


@beta_tool
def write_docx(
    filename: str,
    title: str,
    sections: list[Section],
    lang: Literal["auto", "zh", "en"] = "auto",
    layout: Literal["memo", "gbt9704"] = "memo",
    page_size: Literal["auto", "A4", "Letter"] = "auto",
    disclaimer: Literal["properties", "none"] = "properties",
    disclaimer_reason: str = "",
    reference_docx: str = "",
    subtitle: str = "",
) -> str:
    """Write a Word (.docx) document under outputs/; returns its path.

    Chinese is A4 with PRC typography and "第 X 页 共 Y 页"; English is Letter.
    The not-legal-advice notice goes in the file's properties, never the text.

    Args:
        filename: File name (.docx optional).
        title: Title on the first page.
        sections: Ordered {heading, body_markdown} sections.
        lang: auto | zh | en (auto: by share of Han characters).
        layout: memo | gbt9704 (GB/T 9704 公文 page, fonts and numbering).
        page_size: auto | A4 | Letter.
        disclaimer: properties | none (filings, 律师函, documents to be signed; give disclaimer_reason).
        disclaimer_reason: Why the notice is omitted (with disclaimer='none').
        reference_docx: Optional firm/court template (.docx/.dotx) to write into.
        subtitle: Optional line under the title.
    """
    effective_lang = "zh" if layout == "gbt9704" and lang == "auto" else resolve_lang(
        lang, title, subtitle, *(f"{s.heading}\n{s.body_markdown}" for s in sections)
    )
    note, error = _disclaimer_text(disclaimer, disclaimer_reason, effective_lang)
    if error:
        return error
    template, error = _resolve_template(reference_docx)
    if error:
        return error
    out = _output_path(filename, "docx")
    try:
        write_docx_document(
            output_path=out,
            title=title,
            sections=[(s.heading, s.body_markdown) for s in sections],
            subtitle=subtitle or None,
            disclaimer=note,
            lang=effective_lang,
            layout=layout,
            page_size=page_size,
            reference_docx=template,
            image_roots=[current_settings().outputs_dir],
        )
    except ValueError as exc:
        return f"ERROR: {exc}"
    if template is not None:
        from ..documents.lineage import record_derivation

        record_derivation(out, [template], "write_docx")
    if disclaimer == "none":
        log_workflow_event(
            "document_disclaimer_omitted",
            {"path": str(out.resolve()), "reason": disclaimer_reason.strip()[:300]},
        )
    lint = _write_time_lint(out)
    if lint:
        return json.dumps({"path": str(out.resolve()), "lint": lint}, ensure_ascii=False, indent=2)
    return str(out.resolve())


@beta_tool
def write_pdf(
    filename: str,
    title: str,
    body_markdown: str,
    lang: Literal["auto", "zh", "en"] = "auto",
    layout: Literal["memo", "gbt9704"] = "memo",
    page_size: Literal["auto", "A4", "Letter"] = "auto",
    disclaimer: Literal["properties", "none"] = "properties",
    disclaimer_reason: str = "",
    subtitle: str = "",
) -> str:
    """Write a PDF from Markdown under outputs/; returns its path.

    Same Markdown and page rules as write_docx; the not-legal-advice notice is
    the PDF's Subject metadata, never page content.

    Args:
        filename: File name (.pdf optional).
        title: Title at the top of the first page.
        body_markdown: Body in Markdown.
        lang: auto | zh | en.
        layout: memo | gbt9704.
        page_size: auto | A4 | Letter.
        disclaimer: properties | none (filings and signed letters; give disclaimer_reason).
        disclaimer_reason: Why the notice is omitted.
        subtitle: Optional line under the title.
    """
    effective_lang = "zh" if layout == "gbt9704" and lang == "auto" else resolve_lang(
        lang, title, subtitle, body_markdown
    )
    note, error = _disclaimer_text(disclaimer, disclaimer_reason, effective_lang)
    if error:
        return error
    out = _output_path(filename, "pdf")
    try:
        write_pdf_document(
            output_path=out,
            title=title,
            body_markdown=body_markdown,
            subtitle=subtitle or None,
            disclaimer=note,
            lang=effective_lang,
            layout=layout,
            page_size=page_size,
            image_roots=[current_settings().outputs_dir],
        )
    except ValueError as exc:
        return f"ERROR: {exc}"
    if disclaimer == "none":
        log_workflow_event(
            "document_disclaimer_omitted",
            {"path": str(out.resolve()), "reason": disclaimer_reason.strip()[:300]},
        )
    lint = _write_time_lint(out)
    if lint:
        return json.dumps({"path": str(out.resolve()), "lint": lint}, ensure_ascii=False, indent=2)
    return str(out.resolve())


@beta_tool
def inspect_docx(path: str, paragraph_offset: int = 0, paragraph_limit: int = 150) -> str:
    """Inspect a .docx: every body paragraph under the index edits use, plus
    headings, tables, comments, tracked changes and metadata.

    Each paragraph's ``index`` is the 0-based number ``reshape_docx`` ops
    (after/index/indices) and ``redline_docx`` (paragraph) take; headings are
    flagged in place. Auto-numbering labels (第一条, 1.1) are shown with
    ``label_is_auto_numbering`` — they are not text in the paragraph.

    Args:
        path: Absolute or project-relative path to the .docx file.
        paragraph_offset: Skip this many non-empty paragraphs (for long documents).
        paragraph_limit: Maximum paragraphs to list; 0 lists all.
    """
    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() != ".docx":
        return f"ERROR: inspect_docx only supports .docx files: {p}"
    try:
        payload = inspect_docx_document(
            p,
            paragraph_offset=max(0, int(paragraph_offset)),
            paragraph_limit=max(0, int(paragraph_limit)),
        )
    except Exception as e:
        return f"ERROR inspecting {p}: {e}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def edit_docx_text(source_path: str, filename: str, replacements: list[DocxTextReplacement]) -> str:
    """Copy an existing .docx and replace exact text everywhere it appears —
    body, tables, text boxes, headers and footers — without tracking it.

    Formatting, footnote/comment references, fields and hyperlinks around the
    text are kept. Paragraphs that carry tracked changes are refused (listed
    under ``refused``): edit those with ``redline_docx``. A find that matches
    nothing is listed under ``not_found`` with a hint. The result also reports
    anything the edit lost (``integrity``).

    Args:
        source_path: Absolute or project-relative path to the source .docx.
        filename: Desired output filename under outputs/ (with or without .docx).
        replacements: Exact find/replace operations.
    """
    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".docx":
        return f"ERROR: edit_docx_text only supports .docx files: {source}"
    out, note = _derived_output(source, filename, "docx")
    try:
        payload = edit_docx_text_document(source, out, [r.model_dump() for r in replacements])
    except Exception as e:
        return f"ERROR editing {source}: {e}"
    payload = _with_integrity(source, out, payload)
    payload = _record_edit(source, out, "edit_docx_text", payload, note)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def render_docx_pages(path: str, filename_prefix: str = "pages", dpi: int = 150) -> str:
    """Render a .docx document to page images and return them for visual QA.

    The rendered pages come back inline as images — look at them, do not just
    read the paths. Call this after any non-trivial write or edit.

    Args:
        path: Absolute or project-relative path to the .docx file.
        filename_prefix: Prefix for output files under outputs/.
        dpi: JPEG render DPI, clamped between 72 and 200.
    """
    source = _resolve_read_path(path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".docx":
        return f"ERROR: render_docx_pages only supports .docx files: {source}"
    settings = current_settings()
    safe_prefix = _safe_filename(filename_prefix, default="pages")
    out_dir = _review_dir(settings, safe_prefix, source)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        pdf = _review_pdf(source, out_dir)
        rendered = render_pdf_to_images(pdf, out_dir, safe_prefix, dpi)
    except _SofficeMissing:
        return "ERROR: render_docx_pages requires soffice on PATH"
    except subprocess.CalledProcessError as e:
        return f"ERROR rendering {source}: {e.stderr or e.stdout or e}"
    except Exception as e:
        return f"ERROR rendering {source}: {e}"
    payload: dict[str, Any] = {
        "pdf": str(pdf.resolve()),
        **{k: v for k, v in rendered.items() if k != "images"},
        "images": rendered.get("images", []),
    }
    payload.update(_font_stand_in_note(source))
    payload.update(_unrendered_floating_tables(source, pdf))
    return _visual_result(
        payload,
        rendered.get("images", []),
        out_dir,
        safe_prefix,
        label="Page",
        source=source,
        pdf=pdf,
    )


class _SofficeMissing(RuntimeError):
    pass


def _review_pdf(source: Path, out_dir: Path) -> Path:
    """Convert an Office file to PDF for review, with the GB font stand-ins.

    A private LibreOffice profile per call (``documents.soffice``) lets renders
    run side by side, and ``review_fonts.conf`` lets a 黑体 heading look like a
    sans and 仿宋 body like a serif on a host that has neither — the file
    itself still names the GB fonts.
    """
    from ..documents.review_fonts import review_env
    from ..documents.soffice import SofficeNotInstalled, convert

    try:
        return convert(source, out_dir, "pdf", timeout=120, env=review_env())
    except SofficeNotInstalled as exc:
        raise _SofficeMissing(str(exc)) from exc


def _font_stand_in_note(source: Path) -> dict[str, Any]:
    """Tell the model which faces in the render are stand-ins, not defects."""
    from ..documents.review_fonts import stand_in_note

    try:
        fonts = stand_in_note(source)
    except Exception:  # noqa: BLE001 - advisory
        return {}
    if not fonts:
        return {}
    return {
        "font_stand_ins": {
            "fonts": fonts,
            "note": (
                "This host lacks these fonts, so the pages are drawn with open stand-ins "
                "whose widths differ. The file still names the original fonts and renders "
                "with them in the reader's Office or WPS; do not change fonts to match this "
                "render, and do not read a narrow overflow (a cell showing ###, a clipped "
                "line) as proof — measure it or widen only when the content clearly needs it."
            ),
        }
    }


def _unrendered_floating_tables(source: Path, pdf: Path) -> dict[str, Any]:
    """Positioned tables whose text is in the file but not in the review render.

    LibreOffice 7.3 does not draw a table positioned with ``w:tblpPr`` against
    the page or margin — the 版记 a GB/T 9704 generator anchors to the foot of
    the last page is one — while Word and WPS do. The render then shows a
    公文 without its 版记, and an author that believes it re-runs the
    generator to "fix" a document that was never broken. Measured, not
    assumed: a table is listed only when its text is absent from the PDF.
    """
    try:
        from docx import Document
        from docx.oxml.ns import qn

        from ..documents.pdf import read_pdf_text

        floating = []
        for index, table in enumerate(Document(str(source)).tables):
            props = table._tbl.tblPr
            if props is None or props.find(qn("w:tblpPr")) is None:
                continue
            text = "".join("".join(t.text or "" for t in table._tbl.iter(qn("w:t"))).split())
            if text:
                floating.append((index, text))
        if not floating:
            return {}
        rendered = "".join(read_pdf_text(pdf, ocr=False).split())
    except Exception:  # noqa: BLE001 - advisory
        return {}
    missing = [
        {"table_index": index, "text": text[:60]}
        for index, text in floating
        if text[:12] not in rendered
    ]
    if not missing:
        return {}
    return {
        "floating_tables_not_rendered": {
            "tables": missing,
            "note": (
                "These positioned (floating) tables are in the file but this host's "
                "LibreOffice does not draw them; Word and WPS do. Their absence from the "
                "pages is a limit of the preview, not a defect: do not move or rebuild "
                "them to make them appear here. Check their text with read_document."
            ),
        }
    }


@beta_tool
def write_xlsx(filename: str, sheets: list[Sheet]) -> str:
    """Write a .xlsx workbook under outputs/.

    Beyond scalar rows the writer also accepts:

    - Live formula strings (``"=SUM(B2:B9)"``). After writing, every formula
      is computed — LibreOffice when ``soffice`` is on PATH, the pure-Python
      ``formulas`` engine otherwise and for functions LibreOffice lacks — and
      its value stored in the file, so every reader sees the number.
    - ISO date strings ('2026-10-07') stored as real dates; ``number_formats``
      per column or range ('#,##0.00', '0.0%', 'yyyy-mm-dd').
    - Column widths sized for the text, a Chinese character counting double.
    - Native charts (bar, line, pie, scatter) anchored to a cell.
    - Conditional-formatting rules (color scales, cell-is, data bars).
    - Data-validation rules (drop-downs, numeric ranges).

    Returns the absolute path on success; if a formula evaluates to an error
    (or no engine could evaluate it), returns a JSON object with ``path`` and
    ``formula_errors`` / ``unevaluated_formulas``.

    Args:
        filename: Desired filename (with or without .xlsx extension).
        sheets: Workbook sheets with rows, number formats, charts, conditional
            formats, and data validations.
    """
    from ..documents import recalc_xlsx_workbook

    out = _output_path(filename, "xlsx")
    write_xlsx_workbook(out, [s.model_dump() for s in sheets])
    status = recalc_xlsx_workbook(out)
    errors = status.get("errors") or []
    unevaluated = status.get("unevaluated") or []
    if errors or unevaluated:
        payload: dict[str, Any] = {"path": str(out.resolve()), "recalc_status": status}
        if errors:
            payload["formula_errors"] = errors
        if unevaluated:
            payload["unevaluated_formulas"] = unevaluated
        return json.dumps(payload, ensure_ascii=False, indent=2)
    return str(out.resolve())


def _visual_result(
    payload: dict[str, Any],
    images: list[str],
    out_dir: Path,
    prefix: str,
    *,
    label: str = "Page",
    authored: bool = True,
    source: Path | None = None,
    pdf: Path | None = None,
) -> str:
    """Attach a labeled contact sheet to a render result so the model SEES it.

    Tool results reach the model as content blocks, so the rendered pages come
    back inline rather than as a path list the model cannot open. Full-size
    per-page JPEGs stay on disk; ``view_image`` reads one when fine detail
    matters.

    For a file the model authored, the pages are also *measured*
    (:mod:`legal_helper.documents.quality`) and the findings travel with them:
    overflow, blank pages, overlaps and inconsistent margins are arithmetic,
    not an instruction nobody can tell was followed.
    ``pdf`` is the render the pages came from; the DOCX page checks (a table
    running onto a page without its header row) read it.
    """
    from ..documents.contact_sheet import build_contact_sheets
    from .multimodal import image_result

    paths = [Path(p) for p in images]
    try:
        sheets = build_contact_sheets(paths, out_dir, prefix, label=label)
    except Exception as exc:
        payload = {**payload, "contact_sheet_error": f"{type(exc).__name__}: {exc}"}
        sheets = []
    body: dict[str, Any] = {**payload, "contact_sheets": [str(p.resolve()) for p in sheets]}
    if authored:
        from ..documents.quality import lint_artifact

        report = lint_artifact(source or Path(), paths, render_label=label.lower(), pdf=pdf)
        body["lint"] = report.to_payload()
        if report.findings:
            body["next_step"] = "Fix every critical finding and re-render; a file with none is done."
        elif report.checked is False:
            body["next_step"] = (
                "No automatic checks exist for this file type, so nothing was measured: "
                "look at every page before calling it done."
            )
        else:
            body["next_step"] = "Nothing measured as wrong. Look once for anything the checks cannot see."
    else:
        body["reading"] = (
            f"A source document you are reading, not one you produced. Read what you need and "
            f"move on; call `view_image` for fine detail on a single {label.lower()}."
        )
    return image_result(body, sheets or paths)


def _resolve_read_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = current_settings().project_root / path
    return p


#: Review renders (page images, contact sheets, preview PDFs) live under a
#: "_" directory: the model sees the pages inline, and the chat's file list —
#: which skips "_" components — keeps only what the user asked for. A 公文
#: turn listed 20 files, most of them previews of the one .docx it delivered.
REVIEW_DIRNAME = "_renders"


def _review_dir(settings: Any, prefix: str, source: Path) -> Path:
    return settings.outputs_dir / REVIEW_DIRNAME / f"{prefix}_{source.stem}"


def _derived_output(source: Path, filename: str, extension: str) -> tuple[Path, str]:
    """The output path for an edit of ``source`` — never ``source`` itself.

    7 of 121 recorded edit calls named their own source as the output, which
    would leave nothing to diff, integrity-check or re-edit. Such an edit goes
    to the next free ``<stem>_vN`` name and the result says so.
    """
    out = _output_path(filename, extension)
    try:
        same = out.resolve() == source.resolve()
    except OSError:
        same = False
    if not same:
        return out, ""
    n = 2
    candidate = out.with_name(f"{out.stem}_v{n}{out.suffix}")
    while candidate.exists():
        n += 1
        candidate = out.with_name(f"{out.stem}_v{n}{out.suffix}")
    note = (
        f"{out.name} is the file being edited; the edit was written to {candidate.name} instead "
        "so the original stays available for comparison. Use the new path from here on."
    )
    return candidate, note


def _record_edit(source: Path, out: Path, tool: str, payload: dict[str, Any], note: str = "") -> dict[str, Any]:
    """Lineage, the overwrite note and the stale-source warning, on any edit result."""
    from ..documents.lineage import record_derivation, stale_source_warning

    # Ask before recording: the file just written is itself a newer
    # descendant of the source and must not trigger the warning.
    warning = stale_source_warning(source)
    record_derivation(out, [source], tool)
    if note:
        payload["note"] = note
    if warning:
        payload["warning"] = warning
    return payload


def _with_recalc(out: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Recompute the edited workbook's formulas and report error cells.

    openpyxl saves a formula with no cached value, so every reader but Excel
    (pandas, previews, ``read_document``) saw blanks after an edit; and an
    insert/delete that broke a reference should say so now, not when the user
    opens the file. ``recalc_xlsx_workbook`` caches values without rewriting
    the rest of the workbook, and never raises.
    """
    from ..documents.writers import recalc_xlsx_workbook

    status = recalc_xlsx_workbook(out)
    errors = status.get("errors") or []
    if errors:
        payload["formula_errors"] = errors[:50]
    # A bolded or newly formatted amount that no longer fits shows as ### in
    # Excel; say so with the edit, whether or not a render follows.
    try:
        from ..documents.quality import lint_xlsx

        narrow = [f.as_dict() for f in lint_xlsx(out)[0] if f.check == "number_wider_than_column"]
    except Exception:  # noqa: BLE001 — advisory
        narrow = []
    if narrow:
        payload["numbers_wider_than_column"] = narrow[:10]
    if status.get("recalculated"):
        payload["formulas_recalculated"] = status.get("engine") or True
    elif status.get("skipped_reason") and status.get("skipped_reason") != "no formulas":
        payload["recalc_skipped"] = status["skipped_reason"]
    return payload


def _with_integrity(source: Path, out: Path, payload: dict[str, Any], *, expected: tuple[str, ...] = ()) -> dict[str, Any]:
    """Attach what the edit lost relative to its source (comment anchors,
    tracked changes, footnote references, bookmarks, fields, images, sections)."""
    from ..documents.integrity import compare_docx_integrity

    report = compare_docx_integrity(source, out, expected_losses=expected)
    payload["integrity"] = report
    if report.get("status") == "issues":
        payload["next_step"] = (
            "The edit lost content listed under integrity. If that was not intended, redo it from the "
            "source — a narrower find, or redline_docx for text near references and revisions."
        )
    return payload


@beta_tool
def inspect_xlsx(path: str, preview_rows: int = 12) -> str:
    """Inspect an .xlsx workbook's sheets, dimensions, and preview rows.

    Args:
        path: Absolute or project-relative path to the .xlsx file.
        preview_rows: Number of leading rows to include per worksheet.
    """
    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() != ".xlsx":
        return f"ERROR: inspect_xlsx only supports .xlsx files: {p}"
    try:
        payload = inspect_xlsx_workbook(p, preview_rows=max(1, min(int(preview_rows), 50)))
    except Exception as e:
        return f"ERROR inspecting {p}: {e}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def inspect_xlsx_range(path: str, sheet: str, cell_range: str) -> str:
    """Inspect one exact .xlsx worksheet range with stable cell coordinates.

    Prefer this over `read_document` before spreadsheet edits. Markdown table
    extraction may skip blank rows or hide merged headers; this tool reports
    real workbook coordinates, formulas, number formats, merged ranges, and
    hidden rows/columns for the requested range.

    Args:
        path: Absolute or project-relative path to the .xlsx file.
        sheet: Worksheet name.
        cell_range: A1-style range, e.g. A1:J264.
    """
    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() != ".xlsx":
        return f"ERROR: inspect_xlsx_range only supports .xlsx files: {p}"
    try:
        payload = inspect_xlsx_range_workbook(p, sheet=sheet, cell_range=cell_range)
    except Exception as e:
        return f"ERROR inspecting {p}: {e}"
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


@beta_tool
def edit_xlsx_cells(source_path: str, filename: str, edits: list[CellEdit]) -> str:
    """Copy an existing .xlsx and replace specified cell values.

    Scalar value edits only. For a STRUCTURAL change — inserting, deleting or
    moving rows/columns, merging, copying a styled row, bulk styling — use
    `reshape_xlsx` instead; a string of scalar edits cannot simulate a reshape
    and will corrupt formulas and styles that reference the moved cells.
    Inspect the real workbook coordinates first: markdown table exports shift
    row numbers wherever blank rows or merged headers appear.

    Returns JSON with the output ``path`` (a new file; the source is never
    overwritten) and the number of cells written.

    Args:
        source_path: Absolute or project-relative path to the source workbook.
        filename: Desired output filename under outputs/ (with or without .xlsx).
        edits: Cell edits with worksheet name, A1 cell reference, and scalar value.
    """
    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".xlsx":
        return f"ERROR: edit_xlsx_cells only supports .xlsx files: {source}"
    out, note = _derived_output(source, filename, "xlsx")
    payload: list[dict[str, Any]] = [e.model_dump() for e in edits]
    try:
        edit_xlsx_workbook(source, out, payload)
    except Exception as e:
        return f"ERROR editing {source}: {e}"
    result = _with_recalc(out, {"path": str(out.resolve()), "cells_written": len(payload)})
    result = _record_edit(source, out, "edit_xlsx_cells", result, note)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@beta_tool
def edit_xlsx_cells_checked(
    source_path: str,
    filename: str,
    edits: list[CheckedCellEdit],
    protected_ranges: list[XlsxRangeSpec] | None = None,
) -> str:
    """Copy an .xlsx and apply guarded scalar cell edits.

    Use this for high-stakes spreadsheet edits. Each edit can include an
    expected current value so stale row/column coordinates fail loudly instead
    of silently corrupting a workbook. `protected_ranges` are verified to stay
    unchanged before the output file is saved.

    Args:
        source_path: Absolute or project-relative path to the source workbook.
        filename: Desired output filename under outputs/ (with or without .xlsx).
        edits: Cell edits with optional expected-value preconditions.
        protected_ranges: A1 ranges that must remain unchanged.
    """
    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".xlsx":
        return f"ERROR: edit_xlsx_cells_checked only supports .xlsx files: {source}"
    out, note = _derived_output(source, filename, "xlsx")
    payload = [e.model_dump() for e in edits]
    protected = [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in (protected_ranges or [])]
    try:
        result = edit_xlsx_workbook_checked(source, out, payload, protected_ranges=protected)
    except Exception as e:
        return f"ERROR editing {source}: {e}"
    result = _with_recalc(out, result)
    result = _record_edit(source, out, "edit_xlsx_cells_checked", result, note)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@beta_tool
def diff_xlsx(
    source_path: str,
    target_path: str,
    ranges: list[XlsxRangeSpec] | None = None,
    compare_styles: bool = False,
    max_diffs: int = 200,
) -> str:
    """Diff two .xlsx workbooks over explicit ranges.

    Use after edits to prove what changed and, especially, to confirm protected
    columns/ranges did not change. For large workbooks, pass explicit ranges
    rather than diffing every used cell.

    Args:
        source_path: Original workbook path.
        target_path: Edited workbook path.
        ranges: Optional list of sheet/range specs to compare.
        compare_styles: Also compare basic cell style fingerprints.
        max_diffs: Maximum diff records to return.
    """
    source = _resolve_read_path(source_path)
    target = _resolve_read_path(target_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if not target.is_file():
        return f"ERROR: File not found: {target}"
    if source.suffix.lower() != ".xlsx" or target.suffix.lower() != ".xlsx":
        return "ERROR: diff_xlsx only supports .xlsx files"
    def _dumped(r: Any) -> dict[str, Any]:
        # The Anthropic SDK validates tool inputs against the Pydantic schema
        # before dispatch, so `ranges` arrives as List[XlsxRangeSpec]. But
        # when the same tool is invoked directly from Python (tests,
        # scripts), it arrives as List[dict]. Handle both.
        if hasattr(r, "model_dump"):
            return r.model_dump()
        return dict(r)

    try:
        payload = diff_xlsx_workbooks(
            source,
            target,
            ranges=[_dumped(r) for r in ranges] if ranges else None,
            compare_styles=compare_styles,
            max_diffs=max(1, min(int(max_diffs), 1000)),
        )
    except Exception as e:
        return f"ERROR diffing workbooks: {e}"
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


@beta_tool
def inspect_pptx(path: str, include_text_runs: bool = True) -> str:
    """Inspect an existing .pptx: per slide, shapes, tables, images, text runs and notes.

    Text inside grouped shapes is included — a ppt-master deck (what
    ``create_deck`` delivers) keeps every text frame inside a group. To make a
    new deck use ``create_deck``.

    Args:
        path: Absolute or project-relative path to the .pptx file.
        include_text_runs: Whether to include extracted text and notes in the JSON result.
    """
    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() != ".pptx":
        return f"ERROR: inspect_pptx only supports .pptx files: {p}"
    try:
        payload = inspect_pptx_deck(p, include_text_runs=include_text_runs)
    except Exception as e:
        return f"ERROR inspecting {p}: {e}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def edit_pptx_text(source_path: str, filename: str, replacements: list[SlideTextReplacement]) -> str:
    """Copy an existing .pptx and replace exact text in specified slides. Returns JSON.

    Works on any deck the user has, including one ``create_deck`` delivered;
    text inside grouped shapes and text split across runs both match. The
    result gives the output ``path``, the match count per find, and lists the
    finds that matched nothing under ``not_found`` — re-anchor those, never
    report them as done.

    Args:
        source_path: Absolute or project-relative path to the source deck.
        filename: Desired output filename under outputs/ (with or without .pptx).
        replacements: Exact text replacements keyed by 1-based slide number.
    """
    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".pptx":
        return f"ERROR: edit_pptx_text only supports .pptx files: {source}"
    out, note = _derived_output(source, filename, "pptx")
    try:
        result = edit_pptx_text_in_package(source, out, [r.model_dump() for r in replacements])
    except Exception as e:
        return f"ERROR editing {source}: {e}"
    # Report the counts: a find that matched nothing must not read as a
    # successful edit.
    counts = result.get("replacements") or {}
    payload: dict[str, Any] = {"path": str(out.resolve()), "replacements": counts}
    missed = [key for key, n in counts.items() if not n]
    if missed:
        payload["not_found"] = missed
        payload["next_step"] = (
            "These finds matched nothing on their slide. Check the exact text with "
            "inspect_pptx and retry, or tell the user they were not changed."
        )
    payload = _record_edit(source, out, "edit_pptx_text", payload, note)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def render_pptx_slides(path: str, filename_prefix: str = "slides", dpi: int = 150) -> str:
    """Render a .pptx deck to slide images and return them for visual QA.

    The slides come back inline as a labeled contact sheet — look at them, do
    not just read the paths. Call it after an edit and fix what you see before
    reporting the deck as done.

    Args:
        path: Absolute or project-relative path to the .pptx file.
        filename_prefix: Prefix for output files under outputs/.
        dpi: JPEG render DPI, clamped between 72 and 200.
    """
    from ..documents.soffice import SofficeNotInstalled, convert

    source = _resolve_read_path(path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".pptx":
        return f"ERROR: render_pptx_slides only supports .pptx files: {source}"
    settings = current_settings()
    safe_prefix = _safe_filename(filename_prefix, default="slides")
    out_dir = _review_dir(settings, safe_prefix, source)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        # A private LibreOffice profile per call: on the shared default
        # profile, two renders started together fail one of them (2 of 4 in
        # every concurrent round), which is what made renders unsafe to run
        # side by side.
        pdf = convert(source, out_dir, "pdf", timeout=120)
        rendered = render_pdf_to_images(pdf, out_dir, safe_prefix, dpi)
    except SofficeNotInstalled:
        return "ERROR: render_pptx_slides requires soffice on PATH"
    except subprocess.CalledProcessError as e:
        return f"ERROR rendering {source}: {e.stderr or e.stdout or e}"
    except Exception as e:
        return f"ERROR rendering {source}: {e}"
    images = [str(Path(p).resolve()) for p in (rendered.get("images") or [])]
    return _visual_result(
        {"pdf": str(pdf.resolve()), "images": images, "count": len(images)},
        images,
        out_dir,
        safe_prefix,
        label="Slide",
        source=source,
    )


@beta_tool
def inspect_pdf(path: str, preview_chars: int = 800) -> str:
    """Inspect a PDF's pages, metadata, encryption status, and text previews.

    Args:
        path: Absolute or project-relative path to the PDF.
        preview_chars: Maximum text characters to include per page preview.
    """
    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() != ".pdf":
        return f"ERROR: inspect_pdf only supports .pdf files: {p}"
    try:
        payload = inspect_pdf_document(p, preview_chars=preview_chars)
    except Exception as e:
        return f"ERROR inspecting {p}: {e}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def extract_pdf_tables(path: str, max_pages: int = 20) -> str:
    """Extract tables from a PDF into JSON using pdfplumber.

    Args:
        path: Absolute or project-relative path to the PDF.
        max_pages: Maximum number of pages to scan, clamped to 100.
    """
    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() != ".pdf":
        return f"ERROR: extract_pdf_tables only supports .pdf files: {p}"
    try:
        payload = extract_pdf_tables_document(p, max_pages=max_pages)
    except Exception as e:
        return f"ERROR extracting tables from {p}: {e}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def render_pdf_pages(path: str, filename_prefix: str = "pages", dpi: int = 150, first_page: int | None = None, last_page: int | None = None) -> str:
    """Render a PDF to page images and return them for visual QA.

    The pages come back inline as a labeled contact sheet — look at them, do
    not just read the paths.

    Args:
        path: Absolute or project-relative path to the PDF.
        filename_prefix: Prefix for output images under outputs/.
        dpi: JPEG render DPI, clamped between 72 and 200.
        first_page: Optional 1-based first page to render.
        last_page: Optional 1-based last page to render.
    """
    source = _resolve_read_path(path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".pdf":
        return f"ERROR: render_pdf_pages only supports .pdf files: {source}"
    settings = current_settings()
    safe_prefix = _safe_filename(filename_prefix, default="pages")
    out_dir = _review_dir(settings, safe_prefix, source)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rendered = render_pdf_to_images(
            source,
            out_dir,
            safe_prefix,
            dpi,
            first_page=first_page,
            last_page=last_page,
        )
    except subprocess.CalledProcessError as e:
        return f"ERROR rendering {source}: {e.stderr or e.stdout or e}"
    except Exception as e:
        return f"ERROR rendering {source}: {e}"
    authored = _written_by_write_pdf(source)
    return _visual_result(
        {"pdf": str(source.resolve()), **rendered},
        rendered.get("images", []),
        out_dir,
        safe_prefix,
        label="Page",
        # A PDF the model is READING gets no "fix and re-render" QA loop:
        # emitting it told the model to repair a file it never wrote, and drove
        # repeat single-page renders of source PDFs. A PDF write_pdf produced
        # is the model's own deliverable, so it is measured like one.
        authored=authored,
        source=source if authored else None,
    )


def _written_by_write_pdf(path: Path) -> bool:
    """True for a PDF this harness's ``write_pdf`` produced (its HTML carries
    ``<meta name="generator" content="legal_helper write_pdf">``, which
    WeasyPrint records as the PDF's ``/Creator``)."""
    try:
        from pypdf import PdfReader

        meta = PdfReader(str(path)).metadata or {}
        creator = str(meta.get("/Creator") or "") + " " + str(meta.get("/Producer") or "")
    except Exception:  # noqa: BLE001 — unreadable metadata: treat as a source
        return False
    return "legal_helper write_pdf" in creator


@beta_tool
def merge_pdfs(paths: list[str], filename: str) -> str:
    """Merge PDFs in the given order into one output PDF under outputs/.

    Args:
        paths: Absolute or project-relative source PDF paths, in output order.
        filename: Desired output filename under outputs/ (with or without .pdf).
    """
    out = _output_path(filename, "pdf")
    try:
        resolved_paths: list[Path] = []
        for raw in paths:
            p = _resolve_read_path(raw)
            if not p.is_file() or p.suffix.lower() != ".pdf":
                return f"ERROR: merge_pdfs only supports existing PDFs: {p}"
            resolved_paths.append(p)
        payload = merge_pdf_files(resolved_paths, out)
    except Exception as e:
        return f"ERROR merging PDFs: {e}"
    from ..documents.lineage import record_derivation

    record_derivation(out, resolved_paths, "merge_pdfs")
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def split_pdf(path: str, filename_prefix: str = "split", pages: str = "all") -> str:
    """Split selected PDF pages into individual one-page PDFs under outputs/.

    Args:
        path: Absolute or project-relative source PDF path.
        filename_prefix: Prefix for output PDFs.
        pages: 1-based page spec, e.g. all, 1, 1-3, 1,3,5.
    """
    source = _resolve_read_path(path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".pdf":
        return f"ERROR: split_pdf only supports .pdf files: {source}"
    settings = current_settings()
    safe_prefix = _safe_filename(filename_prefix, default="split")
    out_dir = settings.outputs_dir / f"{safe_prefix}_{source.stem}"
    try:
        payload = split_pdf_file(source, out_dir, safe_prefix, pages=pages)
    except Exception as e:
        return f"ERROR splitting {source}: {e}"
    from ..documents.lineage import record_derivation

    for produced in payload.get("outputs") or []:
        record_derivation(produced, [source], "split_pdf")
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def rotate_pdf_pages(path: str, filename: str, pages: str = "all", degrees: int = 90) -> str:
    """Copy a PDF and rotate selected pages clockwise.

    Returns JSON with the output ``path`` (a new file; the source is never
    overwritten).

    Args:
        path: Absolute or project-relative source PDF path.
        filename: Desired output filename under outputs/ (with or without .pdf).
        pages: 1-based page spec, e.g. all, 1, 1-3, 1,3,5.
        degrees: Clockwise rotation in degrees; normalized to 0, 90, 180, 270.
    """
    source = _resolve_read_path(path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".pdf":
        return f"ERROR: rotate_pdf_pages only supports .pdf files: {source}"
    out, note = _derived_output(source, filename, "pdf")
    try:
        rotate_pdf_file(source, out, pages=pages, degrees=degrees)
    except Exception as e:
        return f"ERROR rotating {source}: {e}"
    result = _record_edit(source, out, "rotate_pdf_pages", {"path": str(out.resolve())}, note)
    return json.dumps(result, ensure_ascii=False, indent=2)


@beta_tool
def reshape_xlsx(
    source_path: str,
    filename: str,
    operations: list[dict[str, Any]],
) -> str:
    """Apply ordered structural ops to a copy of an .xlsx workbook.

    Use for any reshape that scalar `edit_xlsx_cells_checked` cannot
    express: inserting / deleting rows or columns, moving a range
    (with formula translation), copying a styled row, merging /
    unmerging cells, bulk styling (font / fill / border / alignment /
    number_format), or setting row heights / column widths.

    Each entry in `operations` is a dict shaped like
    `{"op": "<kind>", "sheet": "A类", ...op-specific fields}`. Ops apply
    in order; later ops see the state left by earlier ones. Valid ops:
    insert_rows, delete_rows, insert_cols, delete_cols (`at`, `count`),
    move_range, copy_row, merge_range, unmerge_range, set_styles,
    set_row_height, set_col_width — see `read_format_recipe("xlsx")`.

    Row/column inserts and deletes behave as in Excel: merged ranges, data
    validations, conditional formats, formula references on every sheet,
    defined names and row heights move with the cells, and each op reports
    these under `side_effects` (a reference into deleted cells becomes #REF!,
    listed under `formulas_now_ref_error`).

    Args:
        source_path: Absolute or project-relative path to the source workbook.
        filename: Output filename under outputs/ (with or without .xlsx).
        operations: Ordered list of structural ops. Each carries an `op`
            kind plus the fields that kind needs. Unknown ops fail loudly.
    """
    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".xlsx":
        return f"ERROR: reshape_xlsx only supports .xlsx files: {source}"
    out, note = _derived_output(source, filename, "xlsx")
    try:
        result = reshape_xlsx_workbook(source, out, operations)
    except Exception as e:
        return f"ERROR reshaping {source}: {e}"
    result = _with_recalc(out, result)
    result = _record_edit(source, out, "reshape_xlsx", result, note)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@beta_tool
def copy_xlsx_sheet(
    source_path: str,
    filename: str,
    sheet: str,
    new_name: str,
) -> str:
    """Duplicate one worksheet inside a workbook under `new_name`.

    Use to spin off a styled sibling sheet (e.g. `A类` → `B类`) from a
    template without losing fills, merges, validations, conditional
    formats, freeze panes or the autofilter; formulas that name the source
    sheet are pointed at the copy. Images and charts cannot be copied and
    are reported under `copied.not_copied`.

    Args:
        source_path: Absolute or project-relative path to the source workbook.
        filename: Output filename under outputs/ (with or without .xlsx).
        sheet: Existing worksheet to duplicate.
        new_name: Name for the new worksheet (max 31 chars; must not exist).
    """
    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".xlsx":
        return f"ERROR: copy_xlsx_sheet only supports .xlsx files: {source}"
    out, note = _derived_output(source, filename, "xlsx")
    try:
        result = copy_xlsx_worksheet_in_package(source, sheet, out, new_name)
    except Exception as e:
        return f"ERROR copying sheet on {source}: {e}"
    result = _with_recalc(out, result)
    result = _record_edit(source, out, "copy_xlsx_sheet", result, note)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@beta_tool
def render_xlsx_pages(path: str, filename_prefix: str = "pages", dpi: int = 150) -> str:
    """Render an .xlsx workbook to PDF + per-page JPEG images for visual QA.

    Use after a non-trivial xlsx edit (especially anything that touched
    merges, row heights, or layout) to catch gaps, broken merges,
    overflowing wrap-text, and lost styling that scalar cell inspection
    misses.

    Args:
        path: Absolute or project-relative path to the .xlsx file.
        filename_prefix: Prefix for output files under outputs/.
        dpi: JPEG render DPI, clamped between 72 and 200.
    """
    source = _resolve_read_path(path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".xlsx":
        return f"ERROR: render_xlsx_pages only supports .xlsx files: {source}"
    settings = current_settings()
    safe_prefix = _safe_filename(filename_prefix, default="pages")
    out_dir = _review_dir(settings, safe_prefix, source)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        pdf = _review_pdf(source, out_dir)
        rendered = {
            "pdf": str(pdf.resolve()),
            **render_pdf_to_images(pdf, out_dir, safe_prefix, max(72, min(int(dpi), 200))),
        }
    except _SofficeMissing:
        return "ERROR: render_xlsx_pages requires soffice on PATH"
    except subprocess.CalledProcessError as e:
        return f"ERROR rendering {source}: {e.stderr or e.stdout or e}"
    except Exception as e:
        return f"ERROR rendering {source}: {e}"
    # Column widths are measured in the workbook's own font; "###" or a
    # clipped cell in a preview drawn with a stand-in is not proof of either.
    rendered.update(_font_stand_in_note(source))
    return _visual_result(
        rendered, rendered.get("images", []), out_dir, safe_prefix, label="Page", source=source, pdf=pdf
    )


@beta_tool
def view_image(path: str, max_edge: int = 1400) -> str:
    """Look at an image file. Returns the image itself, not a description.

    Use for full-resolution inspection of one page or slide after a
    ``render_*`` contact sheet flags a problem, and for any chart PNG, scan,
    stamp (盖章), signature block, or evidence photo on disk.

    Args:
        path: Absolute or project-relative path to a .jpg/.jpeg/.png/.webp/.gif/.bmp/.tif or
            .heic/.heif (iPhone photo) file.
        max_edge: Longest edge in pixels after downscaling, clamped to 400-1600.
    """
    from .multimodal import image_result

    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif"}:
        return f"ERROR: view_image only supports raster image files: {p}"
    try:
        from PIL import Image

        if p.suffix.lower() in {".heic", ".heif"}:
            # 17 phone photos in one chat never reached the model: Pillow
            # cannot open HEIC until a HEIF plugin registers its opener.
            from ..documents.images import register_heif_opener

            if not register_heif_opener():
                return f"ERROR: reading {p.name} needs a HEIF plugin (pip install pi-heif)"
        with Image.open(p) as im:
            size = {"width": im.width, "height": im.height}
    except Exception as e:
        return f"ERROR reading {p}: {e}"
    return image_result(
        {"path": str(p.resolve()), **size},
        [p],
        max_images=1,
        max_edge=max(400, min(int(max_edge), 1600)),
    )


@beta_tool
def reshape_docx(
    source_path: str,
    filename: str,
    operations: list[dict[str, Any]],
) -> str:
    """Apply ordered structural ops to a copy of a .docx document.

    Use for inserts / deletes / style changes that `edit_docx_text`
    (text-only find/replace) cannot express: inserting or deleting
    paragraphs, restyling a paragraph (e.g. demote `Heading 1` to
    `Heading 2`), and inserting / deleting table rows or rewriting one
    cell.

    Each entry in `operations` is a dict like
    `{"op": "<kind>", ...op-specific fields}`. Valid ops: insert_paragraph
    (`after`, `text`, `style`), delete_paragraphs (`indices` | `range`),
    set_paragraph_style (`index`, `style`), replace_paragraph (`index`,
    `text`), insert_table_row, delete_table_rows, set_cell_text — see
    `read_format_recipe("docx")`. Paragraph indices are the 0-based `index`
    values `inspect_docx` lists. Ops that would rewrite or delete content
    carrying tracked changes are refused: use `redline_docx` for those. The
    result reports anything the edit lost (`integrity`).

    Args:
        source_path: Absolute or project-relative path to the source .docx.
        filename: Output filename under outputs/ (with or without .docx).
        operations: Ordered list of structural ops.
    """
    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".docx":
        return f"ERROR: reshape_docx only supports .docx files: {source}"
    out, note = _derived_output(source, filename, "docx")
    try:
        result = reshape_docx_document(source, out, operations)
    except Exception as e:
        return f"ERROR reshaping {source}: {e}"
    deleting = any(str((op or {}).get("op") or "") in {"delete_paragraphs", "delete_table_rows", "replace_paragraph", "set_cell_text"} for op in operations)
    # Deleting or rewriting a paragraph removes whatever it anchored; report
    # those losses as expected rather than as damage.
    expected = (
        ("bookmarks", "hyperlinks", "simple_fields", "complex_fields", "drawings", "vml_pictures", "tables", "styles", "text")
        if deleting
        else ()
    )
    result = _with_integrity(source, out, result, expected=expected)
    result = _record_edit(source, out, "reshape_docx", result, note)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@beta_tool
def reshape_pptx(
    source_path: str,
    filename: str,
    operations: list[dict[str, Any]],
) -> str:
    """Apply ordered structural ops to a copy of a .pptx deck.

    Use for any reshape `edit_pptx_text` (text-only find/replace) cannot
    express: duplicating, deleting, or reordering slides; rewriting one
    shape's text by index / name / placeholder idx; updating speaker
    notes.

    See the pptx format recipe (`read_format_recipe("pptx")`) for the
    full op table.

    Args:
        source_path: Absolute or project-relative path to the source .pptx.
        filename: Output filename under outputs/ (with or without .pptx).
        operations: Ordered list of structural ops.
    """
    source = _resolve_read_path(source_path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".pptx":
        return f"ERROR: reshape_pptx only supports .pptx files: {source}"
    out, note = _derived_output(source, filename, "pptx")
    try:
        result = reshape_pptx_deck(source, out, operations)
    except Exception as e:
        return f"ERROR reshaping {source}: {e}"
    result = _record_edit(source, out, "reshape_pptx", result, note)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def _read_document_window(text: str, name: str, *, offset: int, limit: int) -> str:
    """Return a line window of a document's extracted text.

    Paging exists so a tool result that spilled to a file (see
    ``tool_budget``) can be read back in pieces instead of re-entering the
    context whole — the read that recovers an over-budget result must not
    itself be over budget.
    """
    lines = text.splitlines()
    start = max(0, int(offset))
    end = len(lines) if int(limit) <= 0 else min(len(lines), start + int(limit))
    if start >= len(lines):
        return f"[No content: requested line {start} of {len(lines)} in {name}]"
    body = "\n".join(lines[start:end])
    header = f"[{name}: lines {start}-{end - 1} of {len(lines)}]"
    return f"{header}\n{body}"


def _no_section(section: str, clauses: list[Any], name: str) -> str:
    labels = [c.heading()[:40] for c in clauses if c.depth <= 1][:25]
    if labels:
        return (
            f"ERROR: no clause or section matching {section!r} in {name}. Labelled clauses: "
            + "; ".join(labels)
        )
    return (
        f"ERROR: no clause or section matching {section!r} in {name}, and the document has no labelled "
        "clauses; read it with offset/limit instead."
    )


def _read_section(p: Path, section: str, *, pages: str = "") -> str:
    """One clause/section by label (第三条 = 第3条, "3.2", "Article 5") or heading words."""
    from ..documents.clauses import clause_index_from_text, find_clauses
    from ..documents.docx_reader import read_docx
    from ..documents.intake import (
        WORD_LEGACY_SUFFIXES,
        WORD_SUFFIXES,
        effective_suffix,
        read_any,
        read_legacy_word,
    )

    suffix = effective_suffix(p)
    docx: Path | None = None
    if suffix in WORD_SUFFIXES:
        docx = p
    elif suffix in WORD_LEGACY_SUFFIXES:
        docx = read_legacy_word(p)[1]
    if docx is not None:
        reading = read_docx(docx)
        clauses = reading.clauses()
        hits = find_clauses(clauses, section)
        if not hits:
            return _no_section(section, clauses, p.name)
        clause = hits[0]
        blocks = reading.blocks[clause.start:clause.end]
        paragraphs = [b.paragraph for b in blocks if b.paragraph is not None]
        where = f"paragraphs {paragraphs[0]}–{paragraphs[-1]}" if paragraphs else "a table"
        header = [f"[{clause.heading()} — {p.name}, {where}]"]
        if len(hits) > 1:
            others = ", ".join(str(h.paragraph) for h in hits[1:6] if h.paragraph is not None)
            header.append(f"[{len(hits)} clauses match {section!r}; showing the longest. Others start at paragraph(s) {others}]")
        summary = reading.revisions.header_line()
        if summary:
            header.append(summary)
        return "\n".join(header) + "\n\n" + reading.render_blocks(blocks)
    text = read_any(p, pages=pages)
    units, clauses = clause_index_from_text(text)
    hits = find_clauses(clauses, section)
    if not hits:
        return _no_section(section, clauses, p.name)
    clause = hits[0]
    lines = text.splitlines()
    header = f"[{clause.heading()} — {p.name}, lines {clause.start}-{clause.end - 1} of {len(lines)}]"
    if len(hits) > 1:
        header += f"\n[{len(hits)} clauses match {section!r}; showing the longest]"
    return header + "\n" + "\n".join(lines[clause.start:clause.end])


@beta_tool
def read_document(
    path: str,
    offset: int = 0,
    limit: int = 0,
    pages: str = "",
    section: str = "",
    member: str = "",
) -> str:
    """Read any document as text: Word, spreadsheets, presentations, PDF and
    OFD (scans OCR'd), EPUB, images (OCR), text, and .zip archives (members
    listed). Word tracked changes read as {++ins++} / {--del--}; PDF/OFD text
    is marked [page N].

    Args:
        path: File path; ``archive.zip::member`` reads a zip member.
        offset: First line to return (0-based), to page a long file.
        limit: Lines to return; 0 = the rest.
        pages: PDF/OFD pages or slides, 1-based, e.g. "3-5,8".
        section: One clause or section by its label ("第三条", "3.2", "Article 5") or heading words.
        member: A .zip member to read.
    """
    import zipfile

    from ..documents.intake import UnsupportedFormat, read_any, resolve_member_path
    from ..documents.lineage import stale_source_warning

    raw = str(path or "")
    base, _, inner = raw.partition("::")
    p = _resolve_read_path(base)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if inner or member:
        try:
            p = resolve_member_path(Path(f"{p}::{inner}") if inner else p, member)
        except (FileNotFoundError, ValueError, zipfile.BadZipFile) as e:
            return f"ERROR: {e}"
    try:
        text = _read_section(p, section, pages=pages) if section else read_any(p, pages=pages)
    except UnsupportedFormat as e:
        return f"ERROR: {e}"
    except Exception as e:  # noqa: BLE001 — every reader failure is an ERROR string
        return f"ERROR reading {p}: {e}"
    if text.startswith("ERROR"):
        return text
    if offset or limit:
        text = _read_document_window(text, p.name, offset=offset, limit=limit)
    warning = stale_source_warning(p)
    return f"{warning}\n{text}" if warning else text
