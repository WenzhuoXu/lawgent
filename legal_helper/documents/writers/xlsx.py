"""XLSX writer.

Beyond plain scalar rows the writer also accepts:

- Formula strings (``"=SUM(B2:B9)"``), written as live formulas.
  ``recalc_xlsx_workbook`` (run by the ``write_xlsx`` tool after this writer)
  computes them and stores each result as the cell's cached value.
- ``number_formats``: an Excel number format per column or range
  (``{"range": "C", "format": "#,##0.00"}``). Measured over the harness's
  workbooks, 0 of 351 numeric cells carried one.
- ISO dates: a cell that is exactly ``2026-10-07`` (or ``2026-10-07 14:30``)
  becomes a real date cell with a date format, so it sorts, filters and
  subtracts; 45 such dates had been stored as text. Prefix ``'`` to keep one
  as text.
- Column widths measured in display width, a Han character counting double:
  288 of 578 columns were too narrow for their Chinese text. Text longer than
  the widest column wraps instead of running under its neighbour.
- Native charts (bar, line, pie, scatter) via openpyxl.chart.
- Conditional formatting rules: color scales, cell-is, formula-based,
  and data bars.
- Data-validation drop-downs and numeric/date ranges.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any

from ._text import display_width

#: Excel column-width units are roughly the width of "0" in the default font;
#: a Han glyph is two of them.
_MIN_COL_WIDTH = 8
_MAX_COL_WIDTH = 60
_FORMULA_COL_WIDTH = 12

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$")
DATE_FORMAT = "yyyy-mm-dd"
DATETIME_FORMAT = "yyyy-mm-dd hh:mm"


def coerce_cell(value: Any) -> tuple[Any, str | None]:
    """``(value, number_format)`` for one authored cell.

    An exact ISO date or date-time string becomes a ``date``/``datetime``; a
    string starting with ``'`` is kept as text without the apostrophe (the
    Excel convention for "this is not a number or date"). Anything else passes
    through unchanged.
    """
    if not isinstance(value, str):
        return value, None
    if value.startswith("'"):
        return value[1:], None
    stripped = value.strip()
    match = _ISO_DATE_RE.match(stripped)
    if match:
        try:
            return _dt.date(*(int(g) for g in match.groups())), DATE_FORMAT
        except ValueError:
            return value, None
    match = _ISO_DATETIME_RE.match(stripped)
    if match:
        parts = [int(g) if g is not None else 0 for g in match.groups()]
        try:
            return _dt.datetime(*parts), DATETIME_FORMAT
        except ValueError:
            return value, None
    return value, None


def _display_text(value: Any, number_format: str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        return "2026-10-07 14:30"
    if isinstance(value, _dt.date):
        return "2026-10-07"
    if isinstance(value, str) and value.startswith("="):
        return "x" * _FORMULA_COL_WIDTH
    if isinstance(value, float):
        return f"{value:,.2f}" if number_format and "," in number_format else repr(value)
    if isinstance(value, int) and not isinstance(value, bool) and number_format and "," in number_format:
        return f"{value:,}"
    return str(value)


def _cells_for(ws, spec: str):
    """Cells of ``spec``: a column (``C``), columns (``C:E``) or an A1 range."""
    from openpyxl.utils.cell import column_index_from_string, range_boundaries

    spec = spec.strip().upper().replace("$", "")
    max_row = max(ws.max_row, 1)
    if re.fullmatch(r"[A-Z]{1,3}", spec):
        col = column_index_from_string(spec)
        return [ws.cell(row=r, column=col) for r in range(1, max_row + 1)]
    if re.fullmatch(r"[A-Z]{1,3}:[A-Z]{1,3}", spec):
        first, last = (column_index_from_string(p) for p in spec.split(":"))
        return [
            ws.cell(row=r, column=c)
            for c in range(min(first, last), max(first, last) + 1)
            for r in range(1, max_row + 1)
        ]
    min_col, min_row, max_col, max_row_ = range_boundaries(spec)
    return [
        ws.cell(row=r, column=c)
        for r in range(min_row, max_row_ + 1)
        for c in range(min_col, max_col + 1)
    ]


_CHART_CLASSES = {
    "bar": "BarChart",
    "line": "LineChart",
    "pie": "PieChart",
    "scatter": "ScatterChart",
}


def _build_chart(ws, spec: dict[str, Any]):
    from openpyxl.chart import BarChart, LineChart, PieChart, ScatterChart, Reference
    from openpyxl.utils.cell import range_boundaries

    kind = str(spec.get("type", "")).lower()
    cls_name = _CHART_CLASSES.get(kind)
    if not cls_name:
        return None
    chart_cls = {"BarChart": BarChart, "LineChart": LineChart, "PieChart": PieChart, "ScatterChart": ScatterChart}[cls_name]
    chart = chart_cls()
    title = spec.get("title") or ""
    if title:
        chart.title = title
    chart.style = 10
    series = spec.get("series") or []
    for s in series:
        rng = str(s.get("values_range") or "").strip()
        if not rng:
            continue
        min_col, min_row, max_col, max_row = range_boundaries(rng)
        ref = Reference(ws, min_col=min_col, min_row=min_row, max_col=max_col, max_row=max_row)
        chart.add_data(ref, titles_from_data=False)
        name = str(s.get("name") or "")
        if name and chart.series:
            from openpyxl.chart.series import SeriesLabel
            chart.series[-1].tx = SeriesLabel(v=name)
    cats_range = str(spec.get("categories_range") or "").strip()
    if cats_range and hasattr(chart, "set_categories"):
        min_col, min_row, max_col, max_row = range_boundaries(cats_range)
        cats = Reference(ws, min_col=min_col, min_row=min_row, max_col=max_col, max_row=max_row)
        chart.set_categories(cats)
    width = spec.get("width")
    height = spec.get("height")
    if isinstance(width, (int, float)):
        chart.width = float(width)
    if isinstance(height, (int, float)):
        chart.height = float(height)
    return chart


def _apply_conditional_format(ws, spec: dict[str, Any]) -> None:
    from openpyxl.formatting.rule import (
        ColorScaleRule,
        CellIsRule,
        FormulaRule,
        DataBarRule,
    )
    from openpyxl.styles import Font, PatternFill

    rng = str(spec.get("range") or "").strip()
    if not rng:
        return
    kind = str(spec.get("type", "")).lower()

    def _fill(color: str) -> PatternFill | None:
        color = color.lstrip("#").upper()
        if not color:
            return None
        if len(color) == 6:
            color = "FF" + color
        return PatternFill(start_color=color, end_color=color, fill_type="solid")

    def _font() -> Font | None:
        fg = str(spec.get("foreground") or "").lstrip("#").upper()
        bold = bool(spec.get("bold"))
        if not fg and not bold:
            return None
        if fg and len(fg) == 6:
            fg = "FF" + fg
        return Font(color=fg or None, bold=bold)

    if kind == "color_scale":
        rule = ColorScaleRule(
            start_type="min",
            start_color=(spec.get("color_min") or "F8696B").lstrip("#"),
            mid_type="percentile",
            mid_value=50,
            mid_color=(spec.get("color_mid") or "FFEB84").lstrip("#"),
            end_type="max",
            end_color=(spec.get("color_max") or "63BE7B").lstrip("#"),
        )
    elif kind == "data_bar":
        rule = DataBarRule(
            start_type="min",
            end_type="max",
            color=(spec.get("color") or "638EC6").lstrip("#"),
            showValue=True,
        )
    elif kind == "cell_is":
        operator = str(spec.get("operator") or "equal")
        formula = [str(f) for f in (spec.get("formula") or [])] or [""]
        rule = CellIsRule(
            operator=operator,
            formula=formula,
            fill=_fill(str(spec.get("background") or "")),
            font=_font(),
        )
    elif kind == "formula":
        formula = [str(f) for f in (spec.get("formula") or [])] or [""]
        rule = FormulaRule(
            formula=formula,
            fill=_fill(str(spec.get("background") or "")),
            font=_font(),
        )
    else:
        return
    ws.conditional_formatting.add(rng, rule)


def _apply_data_validation(ws, spec: dict[str, Any]) -> None:
    from openpyxl.worksheet.datavalidation import DataValidation

    rng = str(spec.get("range") or "").strip()
    if not rng:
        return
    dv = DataValidation(
        type=str(spec.get("type") or "list"),
        operator=str(spec.get("operator") or "between") or None,
        formula1=str(spec.get("formula1") or "") or None,
        formula2=str(spec.get("formula2") or "") or None,
        allow_blank=True,
        showErrorMessage=bool(spec.get("error_msg")),
        errorTitle=spec.get("error_title") or None,
        error=spec.get("error_msg") or None,
        showInputMessage=bool(spec.get("prompt")),
        promptTitle=spec.get("prompt_title") or None,
        prompt=spec.get("prompt") or None,
    )
    dv.add(rng)
    ws.add_data_validation(dv)


def write_xlsx_workbook(
    path: Path,
    sheets: list[
        tuple[str, list[list[Any]]]
        | dict[str, Any]
    ],
) -> None:
    """Write a workbook from ``sheets``.

    Each entry is either a legacy ``(name, rows)`` tuple or a dict with keys
    ``name`` / ``rows`` / ``charts`` / ``conditional_formats`` /
    ``data_validations`` / ``freeze_panes`` / ``number_formats``. Formula
    strings in ``rows`` are written verbatim; ``recalc_xlsx_workbook`` then
    computes and caches their values. ISO date strings become date cells.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    path.parent.mkdir(parents=True, exist_ok=True)
    safe_sheets = sheets or [("Sheet1", [])]
    wb = Workbook()
    wb.remove(wb.active)
    used_names: set[str] = set()
    for idx, sheet in enumerate(safe_sheets, start=1):
        if isinstance(sheet, tuple):
            name, rows = sheet
            charts: list[dict[str, Any]] = []
            cond_formats: list[dict[str, Any]] = []
            validations: list[dict[str, Any]] = []
            number_formats: list[dict[str, Any]] = []
            freeze = "A2"
        else:
            name = sheet.get("name") or f"Sheet{idx}"
            rows = sheet.get("rows") or []
            charts = sheet.get("charts") or []
            cond_formats = sheet.get("conditional_formats") or []
            validations = sheet.get("data_validations") or []
            number_formats = sheet.get("number_formats") or []
            freeze = sheet.get("freeze_panes", "A2")

        title = (name or f"Sheet{idx}")[:31]
        while title in used_names:
            suffix = f"_{idx}"
            title = f"{title[:31 - len(suffix)]}{suffix}"
        used_names.add(title)
        ws = wb.create_sheet(title=title)

        for row in rows:
            coerced = [coerce_cell(value) for value in row]
            ws.append([value for value, _ in coerced])
            for column, (_, fmt) in enumerate(coerced, start=1):
                if fmt:
                    ws.cell(row=ws.max_row, column=column).number_format = fmt
        for spec in number_formats:
            target = str(spec.get("range") or "").strip()
            fmt = str(spec.get("format") or "").strip()
            if not target or not fmt or len(fmt) > 255:
                continue
            for cell in _cells_for(ws, target):
                if cell.value is not None:
                    cell.number_format = fmt
        if rows:
            for cell in ws[1]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill(fill_type="solid", fgColor="EAF2F8")
            if freeze:
                ws.freeze_panes = freeze
            for column in ws.columns:
                widest = 0.0
                for cell in column:
                    text = _display_text(cell.value, cell.number_format)
                    # Two Excel width units per em: a Han glyph is two "0"s.
                    width = max((display_width(line) * 2 for line in text.splitlines() or [""]), default=0)
                    widest = max(widest, width)
                    if width + 2 > _MAX_COL_WIDTH and isinstance(cell.value, str) and not cell.value.startswith("="):
                        cell.alignment = Alignment(wrap_text=True, vertical="top")
                ws.column_dimensions[column[0].column_letter].width = min(
                    max(widest + 2, _MIN_COL_WIDTH), _MAX_COL_WIDTH
                )

        for cf in cond_formats:
            _apply_conditional_format(ws, cf)
        for dv in validations:
            _apply_data_validation(ws, dv)
        for chart_spec in charts:
            chart = _build_chart(ws, chart_spec)
            if chart is not None:
                anchor = str(chart_spec.get("anchor") or "F2")
                ws.add_chart(chart, anchor)

    wb.save(path)
    wb.close()
