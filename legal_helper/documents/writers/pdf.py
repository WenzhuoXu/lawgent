"""Markdown → HTML → PDF via weasyprint, with a print-friendly legal CSS.

CJK-aware: the font stacks name the Simplified-Chinese Noto/Source Han
families explicitly and the ``<html lang>`` attribute is set from the
content, so Han glyphs resolve to zh-CN variants (not the Japanese forms a
bare ``serif`` fallback picks) and CJK line-breaking/punctuation rules
apply.
"""

from __future__ import annotations

import html as _html
import re
from pathlib import Path
from typing import Optional

import markdown
from weasyprint import HTML


_CJK_RE = re.compile(
    "[\u2e80-\u2eff\u3000-\u303f\u31c0-\u31ef\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]"
)


def _detect_lang(*texts: Optional[str]) -> str:
    return "zh-CN" if _CJK_RE.search("\n".join(t or "" for t in texts)) else "en"


_PRINT_CSS = """
@page {
    size: Letter;
    margin: 1in;
    @bottom-right {
        content: counter(page) " / " counter(pages);
        font-family: 'Helvetica', sans-serif;
        font-size: 9pt;
        color: #444;
    }
}
body {
    font-family: 'Georgia', 'Times New Roman', 'Noto Serif CJK SC',
        'Source Han Serif SC', 'SimSun', '宋体', serif;
    font-size: 11pt;
    line-height: 1.45;
    color: #1a1a1a;
}
h1, h2, h3, h4 {
    font-family: 'Helvetica', 'Arial', 'Noto Sans CJK SC',
        'Source Han Sans SC', 'SimHei', '黑体', sans-serif;
    color: #0d1b2a;
    page-break-after: avoid;
}
:lang(zh) h1, :lang(zh) h2, :lang(zh) h3, :lang(zh) h4 { color: #000; }
h1 { font-size: 22pt; text-align: center; margin-bottom: 0.4em; }
.subtitle { text-align: center; font-style: italic; color: #555; margin-bottom: 1.5em; }
.disclaimer {
    border-left: 3px solid #c9a227;
    padding: 0.5em 0.8em;
    background: #fdf8e6;
    font-size: 9.5pt;
    margin-bottom: 1.5em;
}
table { border-collapse: collapse; width: 100%; margin: 0.6em 0; }
th, td { border: 1px solid #999; padding: 4pt 6pt; vertical-align: top; }
th { background: #f0f2f5; }
code, pre { font-family: 'Menlo', 'Consolas', monospace; font-size: 10pt; }
pre { background: #f6f8fa; padding: 0.6em; border-radius: 4px; }
blockquote { color: #444; border-left: 3px solid #888; margin-left: 0; padding-left: 0.8em; }
hr { border: none; border-top: 1px solid #bbb; margin: 1.2em 0; }
"""


# Fixed, repeating diagonal watermark stamped on every printed page.
# WeasyPrint repeats ``position: fixed`` elements on every page, so a single
# fixed layer covers the whole document. Kept light so body text stays legible.
_WATERMARK_CSS = """
#legal-watermark {
    position: fixed;
    top: -20%; left: -20%; right: -20%; bottom: -20%;
    z-index: 9999;
    color: rgba(201, 68, 66, 0.13);
    font-family: 'Helvetica', 'Arial', 'Noto Sans CJK SC', sans-serif;
    font-weight: 700;
    font-size: 42pt;
    line-height: 2.8;
    letter-spacing: 0.06em;
    text-align: center;
    transform: rotate(-30deg);
    transform-origin: center;
    white-space: pre-wrap;
    overflow: hidden;
}
"""


def _watermark_div(text: str) -> str:
    """Build the tiled watermark ``<div>`` body markup for ``text``."""
    safe = _html.escape(text)
    row = (safe + "  ") * 3
    return '<div id="legal-watermark" aria-hidden="true">' + "\n".join([row] * 6) + "</div>"


def write_pdf_document(
    *,
    output_path: Path | str,
    title: str,
    body_markdown: str,
    subtitle: Optional[str] = None,
    disclaimer: Optional[str] = None,
    lang: Optional[str] = None,
    watermark: Optional[str] = None,
) -> Path:
    """``lang`` overrides the ``<html lang>`` attribute (e.g. ``"zh-CN"``);
    when ``None`` it is auto-detected from the title/subtitle/body.

    ``watermark`` stamps a fixed, repeating diagonal watermark on every page
    (e.g. ``"DO NOT FILE · 禁止对外提交"``) — used when the citation audit
    returns a DO-NOT-FILE verdict so an exported draft cannot be mistaken for a
    filed document."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    body_html = markdown.markdown(
        body_markdown,
        extensions=["extra", "toc", "tables", "sane_lists"],
    )
    doc_lang = lang or _detect_lang(title, subtitle, disclaimer, body_markdown)

    # Title/subtitle/disclaimer are plain text, not markdown — escape them.
    safe_title = _html.escape(title)
    safe_subtitle = _html.escape(subtitle) if subtitle else ""
    safe_disclaimer = _html.escape(disclaimer) if disclaimer else ""
    watermark_css = _WATERMARK_CSS if watermark else ""
    watermark_div = _watermark_div(watermark) if watermark else ""

    html = f"""<!doctype html>
<html lang="{doc_lang}"><head><meta charset="utf-8"><title>{safe_title}</title>
<style>{_PRINT_CSS}{watermark_css}</style></head>
<body>
{watermark_div}
<h1>{safe_title}</h1>
{f'<div class="subtitle">{safe_subtitle}</div>' if subtitle else ''}
{f'<div class="disclaimer">{safe_disclaimer}</div>' if disclaimer else ''}
{body_html}
</body></html>"""

    HTML(string=html).write_pdf(str(output_path))
    return output_path
