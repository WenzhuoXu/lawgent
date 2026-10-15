"""Compute an authored workbook's formulas, cache the results, report errors.

openpyxl writes a formula with an empty cached value (``<f>SUM(B2:B9)</f><v/>``).
Excel recalculates on open, but every other reader — pandas, a mail preview,
the file browser's quick look, this harness's own ``read_document`` — sees a
blank: 48 of 48 formulas in the harness's workbooks had no value. Three
docstrings said a "LibreOffice recalc pass" fixed that; none ran one.

What happens now, in :func:`recalc_xlsx_workbook`:

1. **LibreOffice computes.** A *copy* of the workbook goes through
   ``soffice --convert-to xlsx`` (headless, per-call profile), which loads it,
   recalculates every formula and saves the results.
2. **The values come back, the file does not.** The computed values are read
   out of LibreOffice's copy and written into the original workbook as each
   formula cell's cached ``<v>``. LibreOffice's re-export is thrown away, so
   the styles, charts, validations and conditional formats openpyxl wrote are
   untouched byte for byte.
3. **A second opinion for ``#NAME?``.** LibreOffice 7.3 does not know newer
   Excel functions (``XLOOKUP``, ``LET``…), and reports them as ``#NAME?``. Those
   cells are re-evaluated with the pure-Python ``formulas`` engine, which is
   also the whole engine when ``soffice`` is absent.

Error values (``#REF!``, ``#DIV/0!``, ``#NAME?``, ``#VALUE!``, ``#N/A``,
``#NUM!``, ``#NULL!``) are cached as the errors they are and returned, so the
model sees them before it calls the workbook finished. The workbook keeps
``fullCalcOnLoad``, so Excel still recalculates everything when it opens it.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

ERROR_TOKENS = ("#REF!", "#DIV/0!", "#NAME?", "#VALUE!", "#NULL!", "#N/A", "#NUM!")

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _coerce_value(node: Any) -> Any:
    """Unwrap the formulas-library cell wrapper to a Python scalar.

    Error cells come back as ``formulas.tokens.operand.XlError`` objects whose
    ``str()`` is one of :data:`ERROR_TOKENS`; coerce them so the caller can
    string-compare.
    """
    try:
        import numpy as np
    except Exception:
        np = None  # type: ignore[assignment]
    val = node
    if hasattr(val, "value"):
        val = val.value
    if np is not None and isinstance(val, np.ndarray):
        flat = val.flatten()
        val = flat[0] if flat.size else None
    if hasattr(val, "item"):  # numpy scalars
        try:
            val = val.item()
        except Exception:
            pass
    if type(val).__name__ == "XlError":
        val = str(val)
    return val


def formula_cells(path: Path) -> list[tuple[str, str]]:
    """``(sheet, A1)`` of every formula cell, in workbook order."""
    from openpyxl import load_workbook

    wb = load_workbook(str(path), data_only=False)
    try:
        out: list[tuple[str, str]] = []
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if cell.data_type == "f" or (isinstance(cell.value, str) and cell.value.startswith("=")):
                        out.append((ws.title, cell.coordinate))
        return out
    finally:
        wb.close()


def _libreoffice_values(path: Path) -> dict[tuple[str, str], Any]:
    """Values LibreOffice computes for ``path``, read from a converted copy."""
    from openpyxl import load_workbook

    from ..soffice import convert

    with tempfile.TemporaryDirectory(prefix="lh-recalc-") as tmp:
        tmp_dir = Path(tmp)
        source = tmp_dir / "in" / "book.xlsx"
        source.parent.mkdir()
        shutil.copyfile(path, source)
        produced = convert(source, tmp_dir / "out", 'xlsx:Calc MS Excel 2007 XML', timeout=180)
        wb = load_workbook(str(produced), data_only=True)
        try:
            return {
                (ws.title, cell.coordinate): cell.value
                for ws in wb.worksheets
                for row in ws.iter_rows()
                for cell in row
            }
        finally:
            wb.close()


_FORMULAS_KEY_RE = re.compile(r"^'?\[[^\]]+\](.+?)'?!([A-Z]+\d+)$")


def _formulas_values(path: Path) -> dict[tuple[str, str], Any]:
    """Values from the pure-Python ``formulas`` engine (sheet names upper-cased)."""
    import formulas

    solution = formulas.ExcelModel().loads(str(path)).finish().calculate()
    out: dict[tuple[str, str], Any] = {}
    for key, node in solution.items():
        match = _FORMULAS_KEY_RE.match(str(key))
        if match:
            out[(match.group(1).upper(), match.group(2))] = _coerce_value(node)
    return out


def _sheet_parts(zf: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name → its part name inside the package."""
    from lxml import etree

    workbook = etree.fromstring(zf.read("xl/workbook.xml"))
    rels = etree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.get("Id"): rel.get("Target")
        for rel in rels.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    parts: dict[str, str] = {}
    for sheet in workbook.iter(f"{{{_MAIN_NS}}}sheet"):
        target = targets.get(sheet.get(f"{{{_REL_NS}}}id"), "")
        if not target:
            continue
        part = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        parts[sheet.get("name")] = part
    return parts


def _excel_number(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        from openpyxl.utils.datetime import to_excel

        return repr(float(to_excel(value)))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return None


def _write_cached_values(path: Path, values: dict[tuple[str, str], Any]) -> int:
    """Put ``values`` into the formula cells' ``<v>`` and return how many."""
    from lxml import etree

    by_sheet: dict[str, dict[str, Any]] = {}
    for (sheet, coord), value in values.items():
        by_sheet.setdefault(sheet, {})[coord] = value
    written = 0
    tmp = path.with_name(f".{path.name}.recalc")
    with zipfile.ZipFile(path) as src:
        parts = _sheet_parts(src)
        replacements: dict[str, bytes] = {}
        for sheet, cells in by_sheet.items():
            part = parts.get(sheet)
            if part is None:
                continue
            root = etree.fromstring(src.read(part))
            for cell in root.iter(f"{{{_MAIN_NS}}}c"):
                coord = cell.get("r")
                if coord not in cells or cell.find(f"{{{_MAIN_NS}}}f") is None:
                    continue
                value = cells[coord]
                v = cell.find(f"{{{_MAIN_NS}}}v")
                if value is None:
                    if v is not None:
                        cell.remove(v)
                    continue
                if v is None:
                    v = etree.SubElement(cell, f"{{{_MAIN_NS}}}v")
                number = _excel_number(value)
                if isinstance(value, bool):
                    cell.set("t", "b")
                    v.text = "1" if value else "0"
                elif number is not None:
                    if "t" in cell.attrib:
                        del cell.attrib["t"]
                    v.text = number
                elif isinstance(value, str) and value in ERROR_TOKENS:
                    cell.set("t", "e")
                    v.text = value
                else:
                    cell.set("t", "str")
                    v.text = str(value)
                written += 1
            replacements[part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for item in src.infolist():
                data = replacements.get(item.filename)
                out.writestr(item, data if data is not None else src.read(item.filename))
    tmp.replace(path)
    return written


def recalc_xlsx_workbook(path: Path, *, use_libreoffice: bool = True) -> dict[str, Any]:
    """Compute every formula in ``path``, cache the values, report errors.

    Returns ``{"recalculated", "engine", "cached", "errors", "unevaluated",
    "skipped_reason"}``. Never raises — this is post-processing, and a write
    that succeeded must not be reported as failed because a value could not be
    computed.
    """
    path = Path(path)
    status: dict[str, Any] = {
        "recalculated": False, "engine": None, "cached": 0, "errors": [],
        "unevaluated": [], "skipped_reason": "",
    }
    if not path.is_file():
        status["skipped_reason"] = "file not found"
        return status
    try:
        cells = formula_cells(path)
    except Exception as exc:  # noqa: BLE001
        status["skipped_reason"] = f"could not read workbook: {exc}"
        return status
    if not cells:
        status["skipped_reason"] = "no formulas"
        return status

    values: dict[tuple[str, str], Any] = {}
    engines: list[str] = []
    lo_error = ""
    if use_libreoffice:
        try:
            computed = _libreoffice_values(path)
            values = {key: computed.get(key) for key in cells}
            engines.append("libreoffice")
        except Exception as exc:  # noqa: BLE001 - soffice absent or failed; fall back
            lo_error = f"{type(exc).__name__}: {exc}"

    second = [key for key in cells if key not in values or values[key] in (None, "#NAME?")]
    if second:
        try:
            fallback = _formulas_values(path)
            used = False
            for sheet, coord in second:
                alt = fallback.get((sheet.upper(), coord))
                if alt is not None and alt != "#NAME?":
                    values[(sheet, coord)] = alt
                    used = True
                elif (sheet, coord) not in values:
                    values[(sheet, coord)] = alt
            if used or not engines:
                engines.append("formulas")
        except Exception as exc:  # noqa: BLE001
            if not engines:
                status["skipped_reason"] = (
                    f"no engine could evaluate the workbook ({lo_error or 'soffice unavailable'}; "
                    f"formulas: {exc})"
                )
                return status

    try:
        status["cached"] = _write_cached_values(path, values)
    except Exception as exc:  # noqa: BLE001
        status["skipped_reason"] = f"computed but could not write cached values: {exc}"
    status["recalculated"] = True
    status["engine"] = "+".join(engines) or None
    for (sheet, coord) in cells:
        value = values.get((sheet, coord))
        if isinstance(value, str) and value in ERROR_TOKENS:
            status["errors"].append({"sheet": sheet, "cell": coord, "error": value})
        elif value is None:
            status["unevaluated"].append({"sheet": sheet, "cell": coord})
    return status


__all__ = ["ERROR_TOKENS", "formula_cells", "recalc_xlsx_workbook"]
