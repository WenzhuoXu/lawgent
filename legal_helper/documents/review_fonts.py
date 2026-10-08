"""Font policy for Chinese documents: name the GB fonts, review with stand-ins.

The policy, in three parts:

1. **The file names the GB fonts.** 仿宋 / 黑体 / 楷体 / 宋体 / 方正小标宋简体 are
   what the reader's Word or WPS has and what GB/T 9704 asks for. The writer
   never substitutes a font that happens to be installed on the server — a
   DOCX naming "Noto Serif CJK SC" would render in a fallback face on the
   reader's machine.
2. **A review render maps them to open faces.** ``review_fonts.conf`` (shipped
   beside this module) is handed to ``soffice`` as ``FONTCONFIG_FILE``, so a
   contact sheet shows 黑体 headings as a sans, 仿宋 body as a serif and a
   小标宋 title as a bold serif instead of one fallback face for every role.
   Only that child process sees it; the user's fontconfig is never edited.
3. **The model is told which faces are stand-ins**, so it does not "fix" a
   heading that only looks wrong because the server lacks 黑体.

No GB font is bundled: 方正 faces are commercial, and the MIT tree cannot ship
them or a GPL substitute.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Iterable

REVIEW_FONTCONFIG = Path(__file__).with_name("review_fonts.conf")

#: Named font → the face a review render draws it with on a host without it.
STAND_INS: dict[str, str] = {
    **{name: "Noto Serif CJK SC" for name in (
        "仿宋", "仿宋_GB2312", "FangSong", "FangSong_GB2312", "STFangsong",
        "宋体", "SimSun", "Songti SC", "STSong", "华文中宋",
        "楷体", "楷体_GB2312", "KaiTi", "KaiTi_GB2312", "STKaiti", "Kaiti SC",
    )},
    **{name: "Noto Sans CJK SC" for name in (
        "黑体", "SimHei", "微软雅黑", "Microsoft YaHei", "Heiti SC", "STHeiti",
        "等线", "DengXian", "等线 Light",
    )},
    **{name: "Carlito / Liberation Sans" for name in ("Calibri", "Calibri Light")},
    "Cambria": "Caladea / Liberation Serif",
    **{name: "Noto Serif CJK SC Bold" for name in (
        "方正小标宋简体", "方正小标宋_GBK", "FZXiaoBiaoSong-B05S",
    )},
}


def review_env() -> dict[str, str]:
    """Environment for a review-render ``soffice`` call."""
    if REVIEW_FONTCONFIG.is_file():
        return {"FONTCONFIG_FILE": str(REVIEW_FONTCONFIG)}
    return {}


@lru_cache(maxsize=1)
def installed_families() -> frozenset[str]:
    """Font families fontconfig knows on this host (empty when unknowable)."""
    if not shutil.which("fc-list"):
        return frozenset()
    try:
        out = subprocess.run(
            ["fc-list", "--format", "%{family}\n"],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    names: set[str] = set()
    for line in out.splitlines():
        names.update(part.strip() for part in line.split(",") if part.strip())
    return frozenset(names)


_FONT_ATTR_RE = re.compile(r'w:(?:eastAsia|ascii|hAnsi)="([^"]+)"')
# Excel: <font><name val="等线"/>; PowerPoint/themes: <a:latin typeface="…"/>.
_XLSX_FONT_RE = re.compile(r'<(?:\w+:)?name val="([^"]+)"')
_DRAWINGML_FONT_RE = re.compile(r'<a:(?:latin|ea|cs) typeface="([^"+][^"]*)"')
_FONT_PARTS = {
    ".docx": (("word/styles.xml", "word/document.xml"), _FONT_ATTR_RE),
    ".xlsx": (("xl/styles.xml",), _XLSX_FONT_RE),
    ".pptx": (("ppt/theme/theme1.xml", "ppt/slides/"), _DRAWINGML_FONT_RE),
}


def docx_font_names(path: Path) -> set[str]:
    """Every font an Office file names in its styles and body (best effort).

    DOCX, XLSX and PPTX; the name stays for the callers that only had Word.
    """
    parts, pattern = _FONT_PARTS.get(Path(path).suffix.lower(), _FONT_PARTS[".docx"])
    names: set[str] = set()
    try:
        with zipfile.ZipFile(path) as zf:
            members = zf.namelist()
            for part in parts:
                for member in (m for m in members if m == part or (part.endswith("/") and m.startswith(part))):
                    xml = zf.read(member).decode("utf-8", "replace")
                    names.update(pattern.findall(xml))
    except (OSError, zipfile.BadZipFile):
        return set()
    return names


def stand_ins_for(names: Iterable[str]) -> dict[str, str]:
    """The named fonts this host lacks, each with the face it is reviewed in."""
    installed = installed_families()
    return {
        name: STAND_INS[name]
        for name in sorted(set(names))
        if name in STAND_INS and name not in installed
    }


def stand_in_note(path: Path) -> dict[str, str]:
    """``{named font: stand-in}`` for a DOCX's fonts missing on this host."""
    return stand_ins_for(docx_font_names(path))


__all__ = [
    "REVIEW_FONTCONFIG",
    "STAND_INS",
    "docx_font_names",
    "installed_families",
    "review_env",
    "stand_in_note",
    "stand_ins_for",
]
