"""PPTX writer.

Prefers a Node.js + pptxgenjs renderer (handles layouts, tables, stats,
two-column, timeline, theme) and falls back to a minimal OOXML zip when
Node is unavailable.
"""

from __future__ import annotations

import json
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ._node import find_node, node_diagnostic


_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CJK_RE = re.compile(
    "[\u2e80-\u2eff\u3000-\u303f\u31c0-\u31ef\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]"
)
_DEFAULT_EAST_ASIA_FONT = "微软雅黑"  # Microsoft YaHei-class deck font

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


def _pptx_text_box_xml(
    shape_id: int, name: str, x: int, y: int, cx: int, cy: int, lines: list[str], lang: str = "en-US"
) -> str:
    paras = []
    for line in lines or [""]:
        paras.append(
            f"<a:p><a:r><a:rPr lang=\"{escape(lang)}\" sz=\"1800\"/>"
            f"<a:t>{escape(str(line))}</a:t></a:r><a:endParaRPr lang=\"{escape(lang)}\" sz=\"1800\"/></a:p>"
        )
    return f"""
<p:sp>
  <p:nvSpPr><p:cNvPr id="{shape_id}" name="{escape(name)}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
  <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>
  <p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0"/><a:lstStyle/>{''.join(paras)}</p:txBody>
</p:sp>"""


def _pptx_slide_xml(title: str, bullets: list[str], lang: str = "en-US") -> str:
    body_lines = [f"• {bullet}" for bullet in bullets] if bullets else []
    shapes = [
        _pptx_text_box_xml(2, "Title", 685800, 457200, 7772400, 914400, [title], lang),
        _pptx_text_box_xml(3, "Body", 914400, 1600200, 7315200, 4114800, body_lines, lang),
    ]
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {''.join(shapes)}
  </p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""


def _pptx_notes_xml(notes: str, lang: str = "en-US") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:notes xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">
  <p:cSld><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {_pptx_text_box_xml(2, "Notes Placeholder", 685800, 685800, 7772400, 4114800, [notes] if notes else [], lang)}
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
<p:sldSz cx="9144000" cy="5143500" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/>
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
