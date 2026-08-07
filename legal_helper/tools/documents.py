"""Document tools: write_docx, write_pdf, read_document."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from anthropic import beta_tool
from pydantic import BaseModel, Field

from ..config import current_settings
from ..documents import (
    copy_xlsx_worksheet as copy_xlsx_worksheet_in_package,
    edit_docx_text_document,
    edit_pptx_text as edit_pptx_text_in_package,
    edit_xlsx_workbook_checked,
    edit_xlsx_workbook,
    diff_xlsx_workbooks,
    extract_pdf_tables_document,
    extract_docx_text,
    extract_pptx_text,
    extract_xlsx_text,
    inspect_docx_document,
    inspect_pdf_document,
    inspect_pptx_deck,
    inspect_xlsx_range_workbook,
    inspect_xlsx_workbook,
    merge_pdf_files,
    read_pdf_text,
    render_pdf_to_images,
    render_xlsx_to_images,
    reshape_docx_document,
    reshape_pptx_deck,
    reshape_xlsx_workbook,
    rotate_pdf_file,
    split_pdf_file,
    write_docx_document,
    write_pdf_document,
    write_pptx_deck,
    write_xlsx_workbook,
)


_DISCLAIMER = (
    "Legal AI helper — not legal advice. All output must be reviewed by "
    "qualified counsel in the relevant jurisdiction before reliance."
)


class Section(BaseModel):
    """One section of a generated legal document."""

    heading: str = Field(..., description="Section heading (rendered as Heading 2).")
    body_markdown: str = Field(
        ...,
        description="Section body, written in markdown.  Tables, bullet lists, "
                    "bold/italic, and headings are rendered correctly.",
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


class Sheet(BaseModel):
    """One worksheet in a generated Excel workbook.

    Rows may include formula strings (``"=SUM(B2:B9)"``). When soffice is on
    PATH the writer recalculates the workbook on save so the cached values
    are present and any ``#REF!``/``#DIV/0!``/``#NAME?`` errors surface to
    the caller.
    """

    name: str = Field(default="Sheet1", description="Worksheet name, maximum 31 characters in Excel.")
    rows: list[list[str | int | float | bool | None]] = Field(
        default_factory=list,
        description="Rows of cell values. Strings beginning with '=' are written as live formulas.",
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


class ChartSeries(BaseModel):
    """One data series on a slide chart."""

    name: str = Field(..., description="Series label shown in the legend.")
    values: list[float] = Field(..., description="Numeric values aligned with the chart's categories list.")


class SlideChart(BaseModel):
    """A chart embedded on a slide via pptxgenjs.

    Supported chart types map directly to pptxgenjs.ChartType: bar, line, pie,
    doughnut, scatter, bubble, radar.
    """

    type: str = Field(..., description="Chart type: bar, line, pie, doughnut, scatter, bubble, or radar.")
    title: str = Field(default="", description="Optional chart title shown above the plot area.")
    categories: list[str] = Field(default_factory=list, description="Category (x-axis) labels.")
    series: list[ChartSeries] = Field(default_factory=list, description="One or more data series.")
    x: float | None = Field(default=None, description="Optional override: left position in inches.")
    y: float | None = Field(default=None, description="Optional override: top position in inches.")
    w: float | None = Field(default=None, description="Optional override: width in inches.")
    h: float | None = Field(default=None, description="Optional override: height in inches.")


class SlideImage(BaseModel):
    """An image overlaid on a slide."""

    path: str = Field(default="", description="Local filesystem path or http(s) URL of the image.")
    data: str = Field(default="", description="Base64 data URI, used when path is empty (e.g. 'image/png;base64,...').")
    x: float = Field(default=0.85, description="Left position in inches.")
    y: float = Field(default=1.55, description="Top position in inches.")
    w: float = Field(default=5.0, description="Width in inches.")
    h: float = Field(default=3.0, description="Height in inches.")
    sizing: str = Field(default="contain", description="Sizing mode: contain, cover, crop, or stretch.")


class SlideMaster(BaseModel):
    """A master-slide spec consumed by pptxgenjs.defineSlideMaster.

    Slides reference a master by setting their ``master`` field to this name.
    """

    name: str = Field(..., description="Master identifier used by Slide.master.")
    background_color: str = Field(default="", description="Optional hex RGB background, e.g. FFFFFF.")
    accent_color: str = Field(default="", description="Optional hex RGB accent, e.g. 1F4E79.")
    footer: str = Field(default="", description="Optional footer text shown on every slide using this master.")
    show_slide_number: bool = Field(default=True, description="Whether to show the slide-number placeholder.")


class FlowNode(BaseModel):
    """One node in a slide flowchart."""

    id: str = Field(..., description="Author-chosen node identifier referenced by edges.")
    kind: Literal["process", "decision", "terminator", "io", "data", "subprocess"] = Field(
        default="process",
        description=(
            "Shape kind. Maps to pptxgenjs flowChart* shapes: process=rectangle, "
            "decision=diamond, terminator=oval (start/end), io=parallelogram, "
            "data=manual-input, subprocess=predefined-process."
        ),
    )
    text: str = Field(default="", description="Label drawn inside the shape.")
    x: float | None = Field(default=None, description="Left position in inches; required when layout='manual'.")
    y: float | None = Field(default=None, description="Top position in inches; required when layout='manual'.")
    w: float | None = Field(default=None, description="Width in inches; defaults to SlideFlowchart.node_w.")
    h: float | None = Field(default=None, description="Height in inches; defaults to SlideFlowchart.node_h.")
    fill: str = Field(default="", description="Hex RGB fill color (no leading #); empty = theme accent tint.")
    stroke: str = Field(default="", description="Hex RGB outline color (no leading #); empty = theme ink.")


class FlowEdge(BaseModel):
    """One directed edge between two flowchart nodes."""

    from_id: str = Field(..., description="Source FlowNode.id.")
    to_id: str = Field(..., description="Target FlowNode.id.")
    label: str = Field(default="", description="Optional edge label drawn near the midpoint.")
    style: Literal["solid", "dashed"] = Field(default="solid", description="Line style.")
    arrow: Literal["end", "none", "both"] = Field(default="end", description="Arrowhead placement.")


class SlideFlowchart(BaseModel):
    """A flowchart block rendered as editable pptxgenjs shapes.

    Provide EITHER ``mermaid`` (LLM authors a small Mermaid string and the
    auto-layout helper extracts nodes/edges + positions) OR ``nodes`` +
    ``edges`` directly. When both are given, ``nodes``/``edges`` win.
    """

    mermaid: str = Field(
        default="",
        description=(
            "Mermaid flowchart source (e.g. 'flowchart TB\\n  A[Step 1] --> B{OK?}'). "
            "When set, the helper renders to SVG via mmdc and extracts node/edge "
            "positions automatically."
        ),
    )
    nodes: list[FlowNode] = Field(
        default_factory=list,
        description="Explicit node list; takes precedence over the mermaid string when both are set.",
    )
    edges: list[FlowEdge] = Field(
        default_factory=list,
        description="Edges referencing FlowNode.id values.",
    )
    layout: Literal["manual", "auto"] = Field(
        default="auto",
        description=(
            "manual = use the x/y/w/h fields on each FlowNode as-is. "
            "auto = run mermaid+dagre layout and overwrite the coordinates."
        ),
    )
    direction: Literal["TB", "BT", "LR", "RL"] = Field(
        default="TB",
        description="Auto-layout flow direction: TB=top-to-bottom, LR=left-to-right, etc.",
    )
    x: float = Field(default=0.55, description="Bounding-box left in inches for auto layout.")
    y: float = Field(default=1.25, description="Bounding-box top in inches for auto layout.")
    w: float = Field(default=12.2, description="Bounding-box width in inches for auto layout.")
    h: float = Field(default=5.6, description="Bounding-box height in inches for auto layout.")
    node_w: float = Field(default=2.2, description="Default node width in inches when not specified per-node.")
    node_h: float = Field(default=0.9, description="Default node height in inches when not specified per-node.")


class Slide(BaseModel):
    """One slide in a generated PowerPoint deck."""

    title: str = Field(..., description="Slide title.")
    subtitle: str = Field(default="", description="Optional subtitle or emphasis text.")
    layout: str = Field(
        default="bullets",
        description=(
            "Slide layout: title, section, bullets, two_column, comparison, "
            "table, quote, stat, timeline, or chart."
        ),
    )
    bullets: list[str] = Field(default_factory=list, description="Body bullet lines.")
    left_title: str = Field(default="", description="Optional left-column heading.")
    right_title: str = Field(default="", description="Optional right-column heading.")
    left: list[str] = Field(default_factory=list, description="Left-column bullet lines.")
    right: list[str] = Field(default_factory=list, description="Right-column bullet lines.")
    table_headers: list[str] = Field(default_factory=list, description="Table header row for table layout.")
    table_rows: list[list[str | int | float | bool | None]] = Field(default_factory=list, description="Table rows.")
    stats: list[dict[str, str | int | float]] = Field(default_factory=list, description="Stat callouts with label/value.")
    speaker_notes: str = Field(default="", description="Optional speaker notes.")
    footer: str = Field(default="", description="Optional slide footer or source note.")
    background_color: str = Field(default="", description="Optional hex RGB background color, e.g. FFFFFF.")
    accent_color: str = Field(default="", description="Optional hex RGB accent color, e.g. 1F4E79.")
    chart: SlideChart | None = Field(
        default=None,
        description=(
            "Optional chart. When layout='chart' the chart fills the body; on "
            "other layouts it docks on the right half as an overlay."
        ),
    )
    images: list[SlideImage] = Field(
        default_factory=list,
        description="Optional images overlaid on the slide, in z-order.",
    )
    flowchart: SlideFlowchart | None = Field(
        default=None,
        description=(
            "Optional flowchart block rendered as editable pptxgenjs flowChart* "
            "shapes + line connectors. Author a small Mermaid string (preferred) "
            "or supply explicit nodes/edges. See skills/flowchart for guidance."
        ),
    )
    master: str = Field(default="", description="Optional master-slide name; must match a SlideMaster defined on the deck.")


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


@beta_tool
def write_docx(filename: str, title: str, sections: list[Section]) -> str:
    """Write a .docx legal document under outputs/. Returns the absolute path.

    Args:
        filename: Desired filename (with or without .docx extension).
        title: Document title rendered on the first page.
        sections: Ordered list of {heading, body_markdown} sections.
    """
    out = _output_path(filename, "docx")
    write_docx_document(
        output_path=out,
        title=title,
        sections=[(s.heading, s.body_markdown) for s in sections],
        disclaimer=_DISCLAIMER,
    )
    return str(out.resolve())


@beta_tool
def write_pdf(filename: str, title: str, body_markdown: str) -> str:
    """Render markdown into a .pdf legal document under outputs/. Returns the
    absolute path.

    Args:
        filename: Desired filename (with or without .pdf extension).
        title: Document title rendered at the top of the first page.
        body_markdown: Document body in markdown (headings, lists, tables).
    """
    out = _output_path(filename, "pdf")
    write_pdf_document(
        output_path=out,
        title=title,
        body_markdown=body_markdown,
        disclaimer=_DISCLAIMER,
    )
    return str(out.resolve())


@beta_tool
def inspect_docx(path: str) -> str:
    """Inspect a .docx file's structure, headings, tables, comments, and metadata.

    Args:
        path: Absolute or project-relative path to the .docx file.
    """
    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() != ".docx":
        return f"ERROR: inspect_docx only supports .docx files: {p}"
    try:
        payload = inspect_docx_document(p)
    except Exception as e:
        return f"ERROR inspecting {p}: {e}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def edit_docx_text(source_path: str, filename: str, replacements: list[DocxTextReplacement]) -> str:
    """Copy an existing .docx and replace exact text in paragraphs and tables.

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
    out = _output_path(filename, "docx")
    try:
        payload = edit_docx_text_document(source, out, [r.model_dump() for r in replacements])
    except Exception as e:
        return f"ERROR editing {source}: {e}"
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
    soffice = shutil.which("soffice")
    if not soffice:
        return "ERROR: render_docx_pages requires soffice on PATH"
    settings = current_settings()
    safe_prefix = _safe_filename(filename_prefix, default="pages")
    out_dir = settings.outputs_dir / f"{safe_prefix}_{source.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(source)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        pdf = out_dir / f"{source.stem}.pdf"
        if not pdf.is_file():
            candidates = sorted(out_dir.glob("*.pdf"))
            if not candidates:
                return f"ERROR: soffice did not produce a PDF in {out_dir}"
            pdf = candidates[0]
        rendered = render_pdf_to_images(pdf, out_dir, safe_prefix, dpi)
    except subprocess.CalledProcessError as e:
        return f"ERROR rendering {source}: {e.stderr or e.stdout or e}"
    except Exception as e:
        return f"ERROR rendering {source}: {e}"
    return _visual_result(
        {"pdf": str(pdf.resolve()), **{k: v for k, v in rendered.items() if k != "images"},
         "images": rendered.get("images", [])},
        rendered.get("images", []),
        out_dir,
        safe_prefix,
        label="Page",
    )


@beta_tool
def write_xlsx(filename: str, sheets: list[Sheet]) -> str:
    """Write a .xlsx workbook under outputs/.

    Beyond scalar rows the writer also accepts:

    - Live formula strings (``"=SUM(B2:B9)"``) — these are evaluated by an
      automatic LibreOffice recalc pass on save when ``soffice`` is on PATH.
    - Native charts (bar, line, pie, scatter) anchored to a cell.
    - Conditional-formatting rules (color scales, cell-is, data bars).
    - Data-validation rules (drop-downs, numeric ranges).

    Returns the absolute path on success; if formula errors are detected
    after recalc, returns a JSON object with ``path`` and ``formula_errors``.

    Args:
        filename: Desired filename (with or without .xlsx extension).
        sheets: Workbook sheets with rows, charts, conditional formats, and
            data validations.
    """
    from ..documents import recalc_xlsx_workbook

    out = _output_path(filename, "xlsx")
    write_xlsx_workbook(out, [s.model_dump() for s in sheets])
    status = recalc_xlsx_workbook(out)
    errors = status.get("errors") or []
    if errors:
        return json.dumps(
            {"path": str(out.resolve()), "formula_errors": errors, "recalc_status": status},
            ensure_ascii=False,
            indent=2,
        )
    return str(out.resolve())


def _visual_result(
    payload: dict[str, Any],
    images: list[str],
    out_dir: Path,
    prefix: str,
    *,
    label: str = "Page",
) -> str:
    """Attach a labeled contact sheet to a render result so the model SEES it.

    Tool results reach the model as content blocks, so the rendered pages come
    back inline rather than as a path list the model cannot open. Full-size
    per-page JPEGs stay on disk; ``view_image`` reads one when fine detail
    matters.
    """
    from ..documents.contact_sheet import build_contact_sheets
    from .multimodal import image_result

    paths = [Path(p) for p in images]
    try:
        sheets = build_contact_sheets(paths, out_dir, prefix, label=label)
    except Exception as exc:
        payload = {**payload, "contact_sheet_error": f"{type(exc).__name__}: {exc}"}
        sheets = []
    body = {
        **payload,
        "contact_sheets": [str(p.resolve()) for p in sheets],
        "visual_qa": (
            f"The images below are the rendered {label.lower()}s. Check each for "
            "text overflowing its box, blank or near-empty pages, overlapping "
            "elements, clipped tables, and inconsistent margins. Fix and re-render "
            "before reporting the file as done."
        ),
    }
    return image_result(body, sheets or paths)


def _resolve_read_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = current_settings().project_root / path
    return p


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
    out = _output_path(filename, "xlsx")
    payload: list[dict[str, Any]] = [e.model_dump() for e in edits]
    try:
        edit_xlsx_workbook(source, out, payload)
    except Exception as e:
        return f"ERROR editing {source}: {e}"
    return str(out.resolve())


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
    out = _output_path(filename, "xlsx")
    payload = [e.model_dump() for e in edits]
    protected = [r.model_dump() for r in (protected_ranges or [])]
    try:
        result = edit_xlsx_workbook_checked(source, out, payload, protected_ranges=protected)
    except Exception as e:
        return f"ERROR editing {source}: {e}"
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
def write_pptx(
    filename: str,
    title: str,
    slides: list[Slide],
    masters: list[SlideMaster] | None = None,
) -> str:
    """Write a .pptx slide deck under outputs/. Returns the absolute path.

    Each slide can carry an embedded chart, overlay images, a flowchart
    block (real shapes + arrows, not text boxes), and reference a named
    master. Define masters with the ``masters`` argument; refer to them
    from a slide via ``master``.

    For flowcharts/process maps prefer the ``flowchart`` field on a slide
    over a vertical bullet list. Author a Mermaid string (e.g. ``flowchart
    TB\\n  A[Step 1] --> B{OK?}``); the helper renders, lays out, and
    embeds real shapes. See ``skills/flowchart`` for examples.

    Args:
        filename: Desired filename (with or without .pptx extension).
        title: Deck title metadata.
        slides: Slides with title, bullets, optional chart, flowchart, images, master ref, etc.
        masters: Optional master-slide definitions for consistent branding.
    """
    from ..documents.diagrams import prepare_flowchart_slides

    out = _output_path(filename, "pptx")
    payload = [s.model_dump() for s in slides]
    prepare_flowchart_slides(payload)
    result = write_pptx_deck(
        out,
        payload,
        title=title,
        masters=[m.model_dump() for m in (masters or [])],
    )
    # The minimal OOXML fallback silently drops tables, charts, flowcharts,
    # stats, images, and masters, and flattens every layout to bullets. Never
    # report that as a plain success — the caller must know the deck it just
    # "wrote" is not the deck it specified.
    # Stay on this module's string contract: a successful write returns the
    # path, a failure returns an "ERROR:"-prefixed string. Returning a JSON
    # object here instead would break every caller that chains the result
    # straight into render_pptx_slides / inspect_pptx as a path.
    if result.get("renderer") != "pptxgenjs":
        dropped = ", ".join(result.get("dropped_features", [])) or "none recorded"
        return (
            f"ERROR: deck written to {out.resolve()} but DEGRADED — the pptxgenjs "
            f"renderer was unavailable, so the minimal OOXML fallback wrote it and "
            f"these features were dropped: {dropped}. "
            f"Renderer error: {result.get('renderer_error', '')}. "
            "Install it with `npm i pptxgenjs` in the repo root and write the deck "
            "again. Do not present this file to the user as finished."
        )
    return str(out.resolve())


@beta_tool
def read_deck_stylesheet() -> str:
    """Read the built-in deck stylesheet for ``write_pptx_from_html``.

    Returns the CSS design system (palette variables, .slide geometry, card /
    grid / kpi / step / table components) to inline into the ``<style>`` block.
    Read this before authoring HTML slides, then override the custom
    properties for the client's palette.
    """
    from ..documents.writers.html_pptx import deck_stylesheet

    css = deck_stylesheet()
    return css or "ERROR: deck.css not found"


@beta_tool
def write_pptx_from_html(
    filename: str,
    html: str,
    title: str = "Presentation",
    raster_selectors: list[str] | None = None,
) -> str:
    """Write a .pptx by laying slides out in HTML/CSS. Returns a JSON report.

    Use this instead of ``write_pptx`` whenever the deck needs a layout the
    fixed enum does not cover — card grids, sidebars, KPI bands, numbered
    process strips, overlapping heroes, anything with real visual structure.
    Chromium renders the HTML and the computed geometry is mapped onto native
    PowerPoint objects, so text stays editable text, boxes stay shapes, and
    ``<table>`` stays a real table.

    Contract: every slide is one element with ``class="slide"``, exactly
    1280x720 px (13.333in x 7.5in at 96px/in). Call ``read_deck_stylesheet``
    first and inline it in a ``<style>`` block. Keep content inside a ~52px
    margin. Mark SVG/gradient/chart blocks with ``data-raster`` so they are
    captured as pictures instead of being rebuilt as shapes.

    Always call ``render_pptx_slides`` afterwards and look at the result.

    Args:
        filename: Desired filename (with or without .pptx extension).
        html: Full HTML document containing one or more .slide elements.
        title: Deck title metadata.
        raster_selectors: Extra CSS selectors to rasterise, e.g. [".chart", ".logo"].
    """
    from ..documents.writers.html_pptx import write_pptx_from_html as _render

    out = _output_path(filename, "pptx")
    try:
        report = _render(
            out,
            html,
            title=title,
            lang="zh-CN" if re.search(r"[一-鿿]", html) else "en-US",
            raster_selectors=raster_selectors or [],
        )
    except Exception as e:
        return (
            f"ERROR: html2pptx failed: {e}\n\n"
            "Fall back to write_pptx with the fixed layouts, or fix the HTML "
            "and retry. Every slide must be an element with class=\"slide\"."
        )
    report["path"] = str(out.resolve())
    report["next_step"] = "Call render_pptx_slides on this path and inspect the slides before reporting done."
    return json.dumps(report, ensure_ascii=False, indent=2)


@beta_tool
def inspect_pptx(path: str, include_text_runs: bool = True) -> str:
    """Inspect a .pptx deck's slides, extracted text runs, and speaker notes.

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
    """Copy an existing .pptx and replace exact text in specified slides.

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
    out = _output_path(filename, "pptx")
    try:
        edit_pptx_text_in_package(source, out, [r.model_dump() for r in replacements])
    except Exception as e:
        return f"ERROR editing {source}: {e}"
    return str(out.resolve())


@beta_tool
def render_pptx_slides(path: str, filename_prefix: str = "slides", dpi: int = 150) -> str:
    """Render a .pptx deck to slide images and return them for visual QA.

    The slides come back inline as a labeled contact sheet — look at them, do
    not just read the paths. Call this after every write_pptx / edit and fix
    what you see before reporting the deck as done.

    Args:
        path: Absolute or project-relative path to the .pptx file.
        filename_prefix: Prefix for output files under outputs/.
        dpi: JPEG render DPI, clamped between 72 and 200.
    """
    source = _resolve_read_path(path)
    if not source.is_file():
        return f"ERROR: File not found: {source}"
    if source.suffix.lower() != ".pptx":
        return f"ERROR: render_pptx_slides only supports .pptx files: {source}"
    soffice = shutil.which("soffice")
    if not soffice:
        return "ERROR: render_pptx_slides requires soffice on PATH"
    settings = current_settings()
    safe_prefix = _safe_filename(filename_prefix, default="slides")
    out_dir = settings.outputs_dir / f"{safe_prefix}_{source.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(source)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        pdf = out_dir / f"{source.stem}.pdf"
        if not pdf.is_file():
            candidates = sorted(out_dir.glob("*.pdf"))
            if not candidates:
                return f"ERROR: soffice did not produce a PDF in {out_dir}"
            pdf = candidates[0]
        render_pdf_to_images(pdf, out_dir, safe_prefix, dpi)
    except subprocess.CalledProcessError as e:
        return f"ERROR rendering {source}: {e.stderr or e.stdout or e}"
    except Exception as e:
        return f"ERROR rendering {source}: {e}"
    images = sorted(str(p.resolve()) for p in out_dir.glob(f"{safe_prefix}-*.jpg"))
    return _visual_result(
        {"pdf": str(pdf.resolve()), "images": images, "count": len(images)},
        images,
        out_dir,
        safe_prefix,
        label="Slide",
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
    out_dir = settings.outputs_dir / f"{safe_prefix}_{source.stem}"
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
    return _visual_result(
        {"pdf": str(source.resolve()), **rendered},
        rendered.get("images", []),
        out_dir,
        safe_prefix,
        label="Page",
    )


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
    return json.dumps(payload, ensure_ascii=False, indent=2)


@beta_tool
def rotate_pdf_pages(path: str, filename: str, pages: str = "all", degrees: int = 90) -> str:
    """Copy a PDF and rotate selected pages clockwise.

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
    out = _output_path(filename, "pdf")
    try:
        rotate_pdf_file(source, out, pages=pages, degrees=degrees)
    except Exception as e:
        return f"ERROR rotating {source}: {e}"
    return str(out.resolve())


def _read_pdf(path: Path) -> str:
    return read_pdf_text(path)


def _read_docx(path: Path) -> str:
    return extract_docx_text(path, include_comments=True)


def _read_xlsx(path: Path) -> str:
    return extract_xlsx_text(path)


def _read_pptx(path: Path) -> str:
    return extract_pptx_text(path)


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
    in order; later ops see the state left by earlier ones. See the xlsx
    format recipe (`read_format_recipe("xlsx")`) for the full op table.

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
    out = _output_path(filename, "xlsx")
    try:
        result = reshape_xlsx_workbook(source, out, operations)
    except Exception as e:
        return f"ERROR reshaping {source}: {e}"
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
    template without losing fills, merges, validations, or conditional
    formats.

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
    out = _output_path(filename, "xlsx")
    try:
        result = copy_xlsx_worksheet_in_package(source, sheet, out, new_name)
    except Exception as e:
        return f"ERROR copying sheet on {source}: {e}"
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
    out_dir = settings.outputs_dir / f"{safe_prefix}_{source.stem}"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rendered = render_xlsx_to_images(source, out_dir, safe_prefix, max(72, min(int(dpi), 200)))
    except subprocess.CalledProcessError as e:
        return f"ERROR rendering {source}: {e.stderr or e.stdout or e}"
    except Exception as e:
        return f"ERROR rendering {source}: {e}"
    return _visual_result(rendered, rendered.get("images", []), out_dir, safe_prefix, label="Page")


@beta_tool
def view_image(path: str, max_edge: int = 1400) -> str:
    """Look at an image file. Returns the image itself, not a description.

    Use for full-resolution inspection of one page or slide after a
    ``render_*`` contact sheet flags a problem, and for any chart PNG, scan,
    stamp (盖章), signature block, or evidence photo on disk.

    Args:
        path: Absolute or project-relative path to a .jpg/.jpeg/.png/.webp/.gif/.bmp file.
        max_edge: Longest edge in pixels after downscaling, clamped to 400-1600.
    """
    from .multimodal import image_result

    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return f"ERROR: view_image only supports raster image files: {p}"
    try:
        from PIL import Image

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
    `{"op": "<kind>", ...op-specific fields}`. See the docx format
    recipe (`read_format_recipe("docx")`) for the full op table.

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
    out = _output_path(filename, "docx")
    try:
        result = reshape_docx_document(source, out, operations)
    except Exception as e:
        return f"ERROR reshaping {source}: {e}"
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
    out = _output_path(filename, "pptx")
    try:
        result = reshape_pptx_deck(source, out, operations)
    except Exception as e:
        return f"ERROR reshaping {source}: {e}"
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@beta_tool
def read_document(path: str) -> str:
    """Read .txt, .md, .pdf, .docx, .xlsx, .pptx, or .csv and return text content.

    Args:
        path: Absolute or project-relative path to the file.
    """
    p = _resolve_read_path(path)
    if not p.is_file():
        return f"ERROR: File not found: {p}"
    suffix = p.suffix.lower()
    try:
        if suffix in {".txt", ".md"}:
            return p.read_text(encoding="utf-8", errors="replace")
        if suffix == ".pdf":
            return _read_pdf(p)
        if suffix == ".docx":
            return _read_docx(p)
        if suffix == ".xlsx":
            return _read_xlsx(p)
        if suffix == ".pptx":
            return _read_pptx(p)
        if suffix == ".csv":
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR reading {p}: {e}"
    # Fallback: attempt text read
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"ERROR: Unsupported file type {suffix}: {e}"
