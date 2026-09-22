"""PPTX writer.

Prefers a Node.js + pptxgenjs renderer (handles layouts, tables, stats,
two-column, timeline, theme) and falls back to a minimal OOXML zip when
Node is unavailable.

Both paths draw on the same 13.333 x 7.5in canvas, to the EMU. The fallback
can serve only the title-and-bullets layout, so it serves that one as a
designed plain slide rather than as a downgrade that looks like a bug.
"""

from __future__ import annotations

import json
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ..graph_layout import wrap_label
from ._node import find_node, node_diagnostic


_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CJK_RE = re.compile(
    "[\u2e80-\u2eff\u3000-\u303f\u31c0-\u31ef\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]"
)
_DEFAULT_EAST_ASIA_FONT = "微软雅黑"  # Microsoft YaHei-class deck font

_EMU_PER_IN = 914400

#: The wide canvas, in the exact EMU ``scripts/pptxgen.js`` writes for
#: LAYOUT_WIDE (13.333 x 7.5in). Every default on the flowchart model is sized
#: for it — a 12.2in content column, which a 10in canvas cannot hold at all.
_CANVAS_W_IN = 12192000 / _EMU_PER_IN
_CANVAS_H_IN = 6858000 / _EMU_PER_IN

#: PowerPoint's portrait notes page. The notes box is derived from it rather
#: than from the slide, which is how an 8.5in-wide notes box came to hang off
#: a 7.5in page.
_NOTES_W_IN = 6858000 / _EMU_PER_IN
_NOTES_H_IN = 9144000 / _EMU_PER_IN

#: Furniture as shares of the canvas, so moving the canvas moves the layout
#: with it instead of stranding boxes at coordinates for the old one.
#: ``_MARGIN_IN`` lands on the 0.55in margin pptxgen.js uses.
_MARGIN_IN = _CANVAS_W_IN / 24
_GUTTER_IN = _MARGIN_IN / 2
_CONTENT_W_IN = _CANVAS_W_IN - 2 * _MARGIN_IN
_TITLE_TOP_IN = _CANVAS_H_IN / 18
_RULE_H_IN = _CANVAS_H_IN / 160

#: Point sizes are absolute: type is read from the back of a room, not scaled
#: to the canvas. A flat 18pt gave the title no authority over the body and
#: was small body copy for a 13.33in canvas.
_TITLE_PT = 36.0
_TITLE_PT_LONG = 28.0
_BODY_PT_LADDER = (28.0, 26.0, 24.0, 22.0, 20.0, 18.0, 16.0, 14.0)
_FOOTER_PT = 10.0
_NOTES_PT = 12.0

#: The line-height multiplier of ``quality.LINE_HEIGHT``: the fit below and
#: the linter's overflow check have to agree on how tall a line is.
_LINE_H = 1.16

#: Air below the last line of a block, in ems of its own type. Without it the
#: descenders touch whatever the box sits above — the accent rule, the footer.
_BLOCK_PAD_EM = 0.44
_BULLET_INDENT_IN = 0.28
_BULLET_GAP_MIN_EM = 0.4
_BULLET_GAP_MAX_EM = 3.0

_INK = "17202A"
_MUTED = "5D6D7E"
_ACCENT = "1F4E79"  # accent1 of the theme written below, and pptxgen.js's default

# Slide-spec fields the minimal OOXML fallback cannot render.
_FALLBACK_DROPPED_KEYS = (
    "chart",
    "images",
    "flowchart",
    "stats",
    "table_headers",
    "table_rows",
    "master",
    "accent_color",
    "background_color",
)


def _emu(inches: float) -> int:
    return int(round(inches * _EMU_PER_IN))


def _lines_at(text: str, pt: float, width_in: float) -> int:
    """Wrapped line count for ``text`` set at ``pt`` across ``width_in``.

    Shares ``graph_layout.wrap_label`` with the diagram layout and the quality
    linter, so the box this writer sizes and the box the linter measures are
    the same box.
    """
    return len(wrap_label(text, max(width_in / (pt / 72.0), 1.0)))


def _one_line(text: str, pt: float, width_in: float) -> str:
    """``text`` cut to a single line at ``pt``.

    The footer box holds one line by construction; a footer that wraps both
    overruns its box and reads as a mistake.
    """
    lines = wrap_label(text, max(width_in / (pt / 72.0), 1.0))
    return lines[0] if len(lines) == 1 else lines[0].rstrip() + "…"


def _fit_bullets(bullets: list[str], width_in: float, height_in: float) -> tuple[float, float]:
    """``(point size, inter-bullet gap in points)`` for one plain slide.

    Takes the largest size on the ladder whose wrapped block plus a minimum
    gap still fits the body band, then spends the room left over on that gap.
    A sparse slide therefore gets large type and generous leading instead of a
    small block marooned under the title — which is the underfilled canvas and
    the dead band the quality linter reports, and the most visible tell of a
    generated deck.
    """
    text_w = max(width_in - _BULLET_INDENT_IN, 0.5)
    gaps = max(len(bullets) - 1, 0)

    def block_h(pt: float) -> float:
        return sum(_lines_at(b, pt, text_w) for b in bullets) * pt * _LINE_H / 72.0

    pt = _BODY_PT_LADDER[-1]
    for candidate in _BODY_PT_LADDER:
        if block_h(candidate) + gaps * _BULLET_GAP_MIN_EM * candidate / 72.0 <= height_in:
            pt = candidate
            break
    if not gaps:
        return pt, 0.0
    # A quarter line held back: the gap is spent in whole points, and a block
    # sized to the last EMU of its box spills on the renderer's rounding.
    slack = max(height_in - block_h(pt) - pt * _LINE_H / 288.0, 0.0)
    return pt, min(slack * 72.0 / gaps, _BULLET_GAP_MAX_EM * pt)


def _pptx_para_xml(
    text: str,
    *,
    pt: float,
    colour: str,
    lang: str,
    bold: bool = False,
    bullet: bool = False,
    space_before_pt: float = 0.0,
) -> str:
    props = ""
    if bullet:
        indent = _emu(_BULLET_INDENT_IN)
        props = f' marL="{indent}" indent="-{indent}"'
    spc = (
        f'<a:spcBef><a:spcPts val="{int(round(space_before_pt * 100))}"/></a:spcBef>'
        if space_before_pt > 0
        else ""
    )
    # A real bullet glyph with a hanging indent, rather than a "• " prefix
    # baked into the text: a prefixed bullet wraps flush left on its second
    # line, which is what makes a plain deck look unmade.
    bu = '<a:buFont typeface="Arial"/><a:buChar char="&#8226;"/>' if bullet else "<a:buNone/>"
    rpr = (
        f'lang="{escape(lang)}" sz="{int(round(pt * 100))}"'
        + (' b="1"' if bold else "")
    )
    fill = f'<a:solidFill><a:srgbClr val="{colour}"/></a:solidFill>'
    return (
        f"<a:p><a:pPr{props}>{spc}{bu}</a:pPr>"
        f"<a:r><a:rPr {rpr}>{fill}</a:rPr><a:t>{escape(str(text))}</a:t></a:r>"
        f"<a:endParaRPr {rpr}/></a:p>"
    )


def _pptx_text_box_xml(
    shape_id: int,
    name: str,
    box: tuple[float, float, float, float],
    paras: list[str],
    anchor: str = "t",
    placeholder: str = "",
) -> str:
    # ``placeholder`` is what makes a notes box the notes body: python-pptx
    # resolves ``notes_text_frame`` through the ph element, and office.py's
    # slide copy reads ``.text`` off it without checking for None.
    ph = f'<p:ph type="{placeholder}" idx="1"/>' if placeholder else ""
    x, y, cx, cy = box
    return f"""
<p:sp>
  <p:nvSpPr><p:cNvPr id="{shape_id}" name="{escape(name)}"/><p:cNvSpPr txBox="1"/><p:nvPr>{ph}</p:nvPr></p:nvSpPr>
  <p:spPr><a:xfrm><a:off x="{_emu(x)}" y="{_emu(y)}"/><a:ext cx="{_emu(cx)}" cy="{_emu(cy)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>
  <p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="{anchor}"/><a:lstStyle/>{''.join(paras)}</p:txBody>
</p:sp>"""


def _pptx_rule_xml(shape_id: int, box: tuple[float, float, float, float]) -> str:
    x, y, cx, cy = box
    return f"""
<p:sp>
  <p:nvSpPr><p:cNvPr id="{shape_id}" name="Accent Rule"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
  <p:spPr><a:xfrm><a:off x="{_emu(x)}" y="{_emu(y)}"/><a:ext cx="{_emu(cx)}" cy="{_emu(cy)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="{_ACCENT}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>
  <p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody>
</p:sp>"""


def _pptx_slide_xml(title: str, bullets: list[str], footer: str, lang: str = "en-US") -> str:
    """One plain slide: title, accent rule under it, bullets, footer.

    The fallback serves exactly one layout, so it serves that one on purpose —
    a type scale, hanging bullet glyphs and furniture on both edges of the
    canvas — instead of two boxes of identical type stacked at the top.
    """
    title_pt = _TITLE_PT
    title_lines = _lines_at(title, title_pt, _CONTENT_W_IN)
    if title_lines > 2:
        title_pt = _TITLE_PT_LONG
        title_lines = _lines_at(title, title_pt, _CONTENT_W_IN)
    # A title long enough to need a third of the canvas is itself the defect;
    # let it overflow its box so the linter says so, rather than eating the body.
    title_h = min(
        (title_lines * _LINE_H + _BLOCK_PAD_EM) * title_pt / 72.0, _CANVAS_H_IN / 3
    )

    rule_y = _TITLE_TOP_IN + title_h + _MARGIN_IN / 4
    body_y = rule_y + _RULE_H_IN + _GUTTER_IN
    footer_h = (_LINE_H + _BLOCK_PAD_EM) * _FOOTER_PT / 72.0
    footer_y = _CANVAS_H_IN - _GUTTER_IN - footer_h
    body_h = footer_y - _GUTTER_IN - body_y

    shapes = [
        _pptx_text_box_xml(
            2,
            "Title",
            (_MARGIN_IN, _TITLE_TOP_IN, _CONTENT_W_IN, title_h),
            [_pptx_para_xml(title, pt=title_pt, colour=_INK, lang=lang, bold=True)],
        ),
        _pptx_rule_xml(3, (_MARGIN_IN, rule_y, _CONTENT_W_IN, _RULE_H_IN)),
    ]
    if bullets:
        body_pt, gap_pt = _fit_bullets(bullets, _CONTENT_W_IN, body_h)
        shapes.append(
            _pptx_text_box_xml(
                4,
                "Body",
                (_MARGIN_IN, body_y, _CONTENT_W_IN, body_h),
                [
                    _pptx_para_xml(
                        bullet,
                        pt=body_pt,
                        colour=_INK,
                        lang=lang,
                        bullet=True,
                        space_before_pt=0.0 if index == 0 else gap_pt,
                    )
                    for index, bullet in enumerate(bullets)
                ],
                anchor="ctr",
            )
        )
    if footer:
        shapes.append(
            _pptx_text_box_xml(
                5,
                "Footer",
                (_MARGIN_IN, footer_y, _CONTENT_W_IN, footer_h),
                [
                    _pptx_para_xml(
                        _one_line(footer, _FOOTER_PT, _CONTENT_W_IN),
                        pt=_FOOTER_PT,
                        colour=_MUTED,
                        lang=lang,
                    )
                ],
            )
        )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {''.join(shapes)}
  </p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""


def _pptx_notes_xml(notes: str, lang: str = "en-US") -> str:
    margin = _NOTES_W_IN / 10
    box = (margin, margin, _NOTES_W_IN - 2 * margin, _NOTES_H_IN - 2 * margin)
    paras = [
        _pptx_para_xml(line, pt=_NOTES_PT, colour=_INK, lang=lang)
        for line in (notes.splitlines() or [notes])
    ]
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:notes xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {_pptx_text_box_xml(2, "Notes Placeholder", box, paras, placeholder="body")}
  </p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:notes>"""


def _fallback_dropped_features(
    slides: list[dict[str, Any]], masters: list[dict[str, Any]]
) -> list[str]:
    """Name every spec feature the minimal OOXML fallback will not render."""
    dropped: list[str] = []
    if masters:
        dropped.append("masters")
    for idx, slide in enumerate(slides, start=1):
        for key in _FALLBACK_DROPPED_KEYS:
            if slide.get(key):
                dropped.append(f"slide {idx}: {key}")
        layout = str(slide.get("layout") or "bullets")
        if layout != "bullets":
            dropped.append(f"slide {idx}: layout '{layout}' downgraded to bullets")
    return dropped


def pptxgenjs_available() -> bool:
    """True when the polished pptxgenjs renderer can actually run.

    Callers and tests use this to tell "the deck is degraded because the
    renderer is missing" apart from a real authoring bug. Without it a bare
    clone silently exercises the minimal OOXML fallback and reports failures
    that are really just an un-run ``npm install``.
    """
    node = find_node()
    script = Path(__file__).resolve().parent / "scripts" / "pptxgen.js"
    if not node or not script.is_file():
        return False
    try:
        subprocess.run(
            [node, "-e", "require.resolve('pptxgenjs')"],
            cwd=str(script.parent),
            check=True,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        return False
    return True


def write_pptx_deck(
    path: Path,
    slides: list[dict[str, Any]],
    title: str = "Presentation",
    masters: list[dict[str, Any]] | None = None,
    *,
    lang: str | None = None,
    east_asia_font: str | None = None,
) -> dict[str, Any]:
    """Render a .pptx deck.

    ``masters`` is an optional list of master-slide specs forwarded to
    ``pptxgenjs.defineSlideMaster``; each slide may reference one via its
    ``master`` field. Charts and images on individual slides are honoured
    by the pptxgenjs path; the minimal OOXML fallback ignores them.

    ``lang`` (e.g. ``"zh-CN"``, auto-detected from content when ``None``)
    and ``east_asia_font`` are threaded to the renderer so CJK decks get
    native fonts instead of the Latin defaults.

    Returns ``{"renderer": ..., "dropped_features": [...], ...}`` so callers
    can tell the polished pptxgenjs path from the minimal OOXML fallback —
    the fallback names every spec feature it dropped instead of silently
    downgrading.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_slides = slides or [{"title": title, "bullets": [], "speaker_notes": ""}]
    safe_masters = masters or []
    if lang is None:
        probe = title + json.dumps(safe_slides, ensure_ascii=False, default=str)
        lang = "zh-CN" if _CJK_RE.search(probe) else "en-US"
    if east_asia_font is None and lang.lower().startswith("zh"):
        east_asia_font = _DEFAULT_EAST_ASIA_FONT
    script = Path(__file__).resolve().parent / "scripts" / "pptxgen.js"
    node = find_node()
    renderer_error = ""
    if node and script.is_file():
        payload = {
            "path": str(path),
            "title": title,
            "slides": safe_slides,
            "masters": safe_masters,
            "lang": lang,
            "font_face_east_asia": east_asia_font or "",
        }
        try:
            subprocess.run(
                [node, str(script)],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                check=True,
                capture_output=True,
                timeout=60,
            )
            return {
                "path": str(path),
                "renderer": "pptxgenjs",
                "lang": lang,
                "dropped_features": [],
            }
        except Exception as exc:
            # Fall back to the minimal OOXML writer below. The tool caller will
            # still get a valid deck even when Node/PptxGenJS is unavailable.
            stderr = getattr(exc, "stderr", "") or ""
            renderer_error = f"{type(exc).__name__}: {exc}" + (f" — {stderr.strip()}" if stderr else "")
    elif not node:
        renderer_error = node_diagnostic()
    else:
        renderer_error = f"pptxgen.js script missing at {script}"
    slide_overrides = "\n".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, len(safe_slides) + 1)
    )
    note_overrides = "\n".join(
        f'<Override PartName="/ppt/notesSlides/notesSlide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>'
        for i in range(1, len(safe_slides) + 1)
        if safe_slides[i - 1].get("speaker_notes")
    )
    sld_ids = "\n".join(
        f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>'
        for i in range(1, len(safe_slides) + 1)
    )
    pres_rels = [
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
    ]
    pres_rels.extend(
        f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>'
        for i in range(1, len(safe_slides) + 1)
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
{slide_overrides}
{note_overrides}
</Types>""",
        )
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""",
        )
        zf.writestr(
            "docProps/core.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{escape(title)}</dc:title></cp:coreProperties>""",
        )
        zf.writestr(
            "docProps/app.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Legal Helper</Application><Slides>{len(safe_slides)}</Slides></Properties>""",
        )
        zf.writestr(
            "ppt/presentation.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">
<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
<p:sldIdLst>{sld_ids}</p:sldIdLst>
<p:sldSz cx="{_emu(_CANVAS_W_IN)}" cy="{_emu(_CANVAS_H_IN)}" type="screen16x9"/><p:notesSz cx="{_emu(_NOTES_W_IN)}" cy="{_emu(_NOTES_H_IN)}"/>
</p:presentation>""",
        )
        zf.writestr(
            "ppt/_rels/presentation.xml.rels",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{''.join(pres_rels)}</Relationships>""",
        )
        zf.writestr(
            "ppt/slideMasters/slideMaster1.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>""",
        )
        zf.writestr(
            "ppt/slideMasters/_rels/slideMaster1.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>""",
        )
        zf.writestr(
            "ppt/slideLayouts/slideLayout1.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}" type="blank" preserve="1"><p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>""",
        )
        zf.writestr(
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>""",
        )
        zf.writestr(
            "ppt/theme/theme1.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="{_A_NS}" name="Office Theme"><a:themeElements><a:clrScheme name="Office"><a:dk1><a:srgbClr val="000000"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F1F1F"/></a:dk2><a:lt2><a:srgbClr val="F2F2F2"/></a:lt2><a:accent1><a:srgbClr val="1F4E79"/></a:accent1><a:accent2><a:srgbClr val="70AD47"/></a:accent2><a:accent3><a:srgbClr val="FFC000"/></a:accent3><a:accent4><a:srgbClr val="C00000"/></a:accent4><a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="7030A0"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface="{escape(east_asia_font or '')}"/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Arial"/><a:ea typeface="{escape(east_asia_font or '')}"/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme></a:themeElements></a:theme>""",
        )
        for i, slide in enumerate(safe_slides, start=1):
            zf.writestr(
                f"ppt/slides/slide{i}.xml",
                _pptx_slide_xml(
                    str(slide.get("title") or f"Slide {i}"),
                    [str(b) for b in (slide.get("bullets") or [])],
                    f"{title} · {i}/{len(safe_slides)}",
                    lang,
                ),
            )
            rels = [
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
            ]
            notes = str(slide.get("speaker_notes") or "")
            if notes:
                rels.append(
                    f'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide{i}.xml"/>'
                )
                zf.writestr(f"ppt/notesSlides/notesSlide{i}.xml", _pptx_notes_xml(notes, lang))
            zf.writestr(
                f"ppt/slides/_rels/slide{i}.xml.rels",
                f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{''.join(rels)}</Relationships>""",
            )
    return {
        "path": str(path),
        "renderer": "ooxml-minimal",
        "lang": lang,
        "dropped_features": _fallback_dropped_features(safe_slides, safe_masters),
        "renderer_error": renderer_error,
    }
