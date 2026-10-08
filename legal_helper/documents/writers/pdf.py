"""Markdown → HTML → PDF via WeasyPrint, with print CSS for legal documents.

The Markdown front end is shared with the Word writer (:mod:`._markdown`), so a
list nested two spaces deep, ``~~strike~~``, ``**结论：**正文`` and footnotes come
out the same in both formats. Python-Markdown, used here before, printed a
two-space nested list as literal ``- item`` text.

* **Page.** A4 for a Chinese document (decided by Han share, as in the Word
  writer), Letter otherwise; ``page_size`` overrides. Page numbers read
  "第 X 页 共 Y 页" or "Page X of Y"; the GB/T 9704 layout prints "— n —"
  outside-aligned, 7 mm under the type area.
* **Footnotes** are real page-bottom footnotes (CSS ``float: footnote``).
* **Tables** repeat their header row on every page and keep rows whole.
* **Fonts.** The stacks name the GB / Windows faces first and the open Noto
  CJK faces after them, so a host with the real fonts uses them and this one
  embeds the stand-in. A PDF embeds what it drew, so unlike a DOCX there is no
  way to "name" a font the host lacks. ``<html lang>`` keeps Han glyphs on
  their zh-CN forms.
* **Metadata.** The disclaimer, when there is one, is the PDF ``Subject`` —
  never a box on page one.
* **No network, no disk.** Images load only from ``image_roots``; every other
  URL the HTML could name is refused by the fetcher.
"""

from __future__ import annotations

import html as _html
from pathlib import Path
from typing import Any, Optional, Sequence

from markdown_it.renderer import RendererHTML
from markdown_it.token import Token

from ._markdown import Parsed, gb_ordinal_level, heading_levels, inline_text, parse
from ._text import Lang, bcp47, resolve_lang

_PAGE_SIZES = {"A4": "A4", "Letter": "Letter"}
_FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2"}


def _detect_lang(*texts: Optional[str]) -> str:
    """``zh-CN`` when the texts are mostly Chinese, else ``en`` (Han share)."""
    return "zh-CN" if resolve_lang("auto", *texts) == "zh" else "en"


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_SERIF_STACK = (
    "'Times New Roman', 'Liberation Serif', '宋体', 'SimSun', 'Songti SC', "
    "'Noto Serif CJK SC', 'Source Han Serif SC', serif"
)
_SANS_STACK = (
    "'Helvetica', 'Arial', 'Liberation Sans', '黑体', 'SimHei', 'Heiti SC', "
    "'Noto Sans CJK SC', 'Source Han Sans SC', sans-serif"
)
_FANGSONG_STACK = (
    "'Times New Roman', 'Liberation Serif', '仿宋', 'FangSong', '仿宋_GB2312', 'STFangsong', "
    "'Noto Serif CJK SC', serif"
)
_KAI_STACK = "'Times New Roman', '楷体', 'KaiTi', '楷体_GB2312', 'STKaiti', 'AR PL UKai CN', 'Noto Serif CJK SC', serif"
_HEI_STACK = "'Times New Roman', '黑体', 'SimHei', 'Heiti SC', 'Noto Sans CJK SC', sans-serif"

_COMMON_CSS = """
table { border-collapse: collapse; width: 100%; margin: 0.6em 0; }
thead { display: table-header-group; }
tr, th, td { break-inside: avoid; page-break-inside: avoid; }
th, td { border: 1px solid #999; padding: 4pt 6pt; vertical-align: top; text-indent: 0; }
code, pre { font-family: 'Menlo', 'Consolas', 'DejaVu Sans Mono', monospace; font-size: 0.85em; }
pre { background: #f6f8fa; padding: 0.6em; border-radius: 4px; white-space: pre-wrap; text-indent: 0; }
blockquote { color: #333; border-left: 3px solid #888; margin-left: 0; padding-left: 0.8em; }
hr { border: none; border-top: 1px solid #bbb; margin: 1.2em 0; }
li > p { margin: 0; }
.footnote { float: footnote; font-size: 9pt; line-height: 1.35; text-indent: 0; text-align: left; }
::footnote-call { content: counter(footnote); vertical-align: super; font-size: 0.65em; line-height: 1; }
::footnote-marker { content: counter(footnote) ". "; }
sup.fn-repeat { font-size: 0.65em; }
"""

_MEMO_CSS = """
@page {
    size: %(size)s;
    margin: %(margin)s;
    @bottom-center {
        content: %(page_label)s;
        font-family: %(serif)s;
        font-size: 9pt;
        color: #444;
    }
    @footnote { border-top: 0.5pt solid #999; padding-top: 4pt; margin-top: 8pt; }
}
body {
    /* The @page margin is the margin; the browser's 8px body margin is not. */
    margin: 0;
    font-family: %(serif)s;
    font-size: %(body_pt)spt;
    line-height: 1.45;
    color: #1a1a1a;
}
h1, h2, h3, h4, h5, h6 {
    font-family: %(sans)s;
    color: #0d1b2a;
    page-break-after: avoid;
}
:lang(zh) h1, :lang(zh) h2, :lang(zh) h3, :lang(zh) h4, :lang(zh) h5, :lang(zh) h6 { color: #000; }
:lang(zh) p { text-indent: 2em; text-align: justify; }
:lang(zh) li p, :lang(zh) blockquote p, :lang(zh) .subtitle, :lang(zh) .doc-title { text-indent: 0; }
h1.doc-title { font-size: 22pt; text-align: center; margin-bottom: 0.4em; }
.subtitle { text-align: center; font-style: italic; color: #555; margin-bottom: 1.5em; }
:lang(zh) .subtitle { font-style: normal; font-family: %(kai)s; }
"""

# GB/T 9704-2012: A4; 天头 37 mm, 订口 28 mm, 版心 156 x 225 mm; 3号仿宋 body
# on a 28 pt pitch; 2号小标宋 title; 层次序数 in 黑体 / 楷体 / 仿宋 / 仿宋, each 空二字;
# "— n —" in 4号宋体, odd pages right and even pages left, 空一字, its rule 7 mm
# below the type area.
_GBT_CSS = """
@font-face {
    font-family: "GB Title";
    src: local("方正小标宋简体"), local("FZXiaoBiaoSong-B05S"), local("方正小标宋_GBK"),
         local("Noto Serif CJK SC Bold"), local("Source Han Serif SC Bold");
}
@page {
    size: A4;
    margin: 37mm 26mm 35mm 28mm;
    @footnote { border-top: 0.5pt solid #000; padding-top: 4pt; margin-top: 8pt; }
}
@page :right {
    @bottom-right {
        content: "— " counter(page) " —";
        font-family: 'Times New Roman', '宋体', 'SimSun', 'Noto Serif CJK SC', serif;
        font-size: 14pt; vertical-align: top; padding-top: 7mm; padding-right: 14pt;
    }
}
@page :left {
    @bottom-left {
        content: "— " counter(page) " —";
        font-family: 'Times New Roman', '宋体', 'SimSun', 'Noto Serif CJK SC', serif;
        font-size: 14pt; vertical-align: top; padding-top: 7mm; padding-left: 14pt;
    }
}
/* 5.2.2 "每行排28个字": 156 mm holds 27 glyphs at 16 pt, so the pitch is
   tightened to 156 mm / 28 = 15.79 pt, as the DOCX character grid does. */
body { margin: 0; font-family: %(fangsong)s; font-size: 16pt; line-height: 28pt; color: #000; letter-spacing: -0.22pt; }
p { margin: 0; text-indent: 31.5pt; /* 空二字 on the tightened 15.78 pt pitch */ text-align: justify; }
h1.doc-title {
    font-family: "GB Title", 'Noto Serif CJK SC', serif; font-weight: normal;
    font-size: 22pt; line-height: 28pt; text-align: center; text-indent: 0; margin: 0 0 28pt 0;
}
.subtitle { font-family: %(kai)s; text-align: center; text-indent: 0; }
h1, h2, h3, h4, h5, h6, .gb-h1, .gb-h2, .gb-h3, .gb-h4 {
    font-size: 16pt; line-height: 28pt; font-weight: normal; text-indent: 31.5pt; margin: 0;
    color: #000; page-break-after: avoid;
}
.gb-h1 { font-family: %(hei)s; margin-top: 8pt; }
.gb-h2 { font-family: %(kai)s; margin-top: 4pt; }
.gb-h3 { font-family: %(fangsong)s; margin-top: 2pt; }
.gb-h4 { font-family: %(fangsong)s; }
td p, th p, td, th { text-indent: 0; }
"""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class _Renderer(RendererHTML):
    """HTML with page-bottom footnotes and, for GB/T 9704, structure classes."""

    def __init__(self, parsed: Parsed, *, gbt9704: bool, image_roots: Sequence[Path]) -> None:
        super().__init__()
        self.parsed = parsed
        self.gbt = gbt9704
        self.image_roots = [Path(r).resolve() for r in image_roots]
        levels = heading_levels(parsed.tokens)
        self.min_level = min(levels) if levels else 1
        self.fn_display: dict[int, int] = {}

    # Footnotes ---------------------------------------------------------------

    def footnote_ref(self, tokens: list[Token], idx: int, options, env) -> str:
        meta = tokens[idx].meta or {}
        local = int(meta.get("id", 0))
        if local in self.fn_display:
            return f'<sup class="fn-repeat">{self.fn_display[local]}</sup>'
        self.fn_display[local] = len(self.fn_display) + 1
        pieces: list[str] = []
        for tok in self.parsed.footnotes.get(local, []):
            if tok.type == "inline":
                pieces.append(self.renderInline(tok.children or [], options, env))
        return f'<span class="footnote">{"<br>".join(p for p in pieces if p)}</span>'

    # Lists -----------------------------------------------------------------------

    def ordered_list_open(self, tokens: list[Token], idx: int, options, env) -> str:
        # WeasyPrint ignores <ol start>; the counter has to be reset in CSS, or
        # a list the author started at 3 prints as 1 — the defect in reverse.
        try:
            start = int(tokens[idx].attrGet("start") or 1)
        except (TypeError, ValueError):
            start = 1
        if start == 1:
            return "<ol>\n"
        return f'<ol start="{start}" style="counter-reset: list-item {start - 1}">\n'

    # Images --------------------------------------------------------------------

    def image(self, tokens: list[Token], idx: int, options, env) -> str:
        tok = tokens[idx]
        alt = _html.escape(inline_text(tok.children) or tok.content or "")
        src = tok.attrGet("src") or ""
        resolved = _allowed_image(src, self.image_roots)
        if resolved is None:
            return alt
        return f'<img src="{_html.escape(resolved.as_uri())}" alt="{alt}" style="max-width:100%">'

    # GB/T 9704 structure -------------------------------------------------------

    def heading_open(self, tokens: list[Token], idx: int, options, env) -> str:
        if not self.gbt:
            return super().renderToken(tokens, idx, options, env)
        level = int(tokens[idx].tag[1]) - self.min_level + 1
        text = inline_text(tokens[idx + 1].children) if idx + 1 < len(tokens) else ""
        gb = gb_ordinal_level(text) or max(1, min(level, 4))
        return f'<p class="gb-h{gb}">'

    def heading_close(self, tokens: list[Token], idx: int, options, env) -> str:
        return "</p>\n" if self.gbt else super().renderToken(tokens, idx, options, env)

    def paragraph_open(self, tokens: list[Token], idx: int, options, env) -> str:
        if self.gbt and not tokens[idx].hidden and idx + 1 < len(tokens):
            gb = gb_ordinal_level(inline_text(tokens[idx + 1].children))
            if gb:
                return f'<p class="gb-h{gb}">'
        return super().renderToken(tokens, idx, options, env)


def _allowed_image(src: str, roots: Sequence[Path]) -> Optional[Path]:
    if not src or "://" in src or not roots:
        return None
    candidate = Path(src).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    if not any(root == resolved or root in resolved.parents for root in roots):
        return None
    return resolved


def _flatten_lists_for_gbt(tokens: list[Token]) -> list[Token]:
    """公文 numbers its structure in the text: lists become numbered paragraphs."""
    out: list[Token] = []
    stack: list[dict[str, Any]] = []
    for tok in tokens:
        if tok.type in {"bullet_list_open", "ordered_list_open"}:
            start = 1
            if tok.type == "ordered_list_open":
                try:
                    start = int(tok.attrGet("start") or 1)
                except (TypeError, ValueError):
                    start = 1
            stack.append({"ordered": tok.type == "ordered_list_open", "n": start - 1, "first": False})
            continue
        if tok.type in {"bullet_list_close", "ordered_list_close"}:
            if stack:
                stack.pop()
            continue
        if tok.type == "list_item_open":
            if stack:
                stack[-1]["n"] += 1
                stack[-1]["first"] = True
            continue
        if tok.type == "list_item_close":
            continue
        if tok.type == "paragraph_open" and stack:
            tok.hidden = False
        if tok.type == "paragraph_close" and stack:
            tok.hidden = False
        if tok.type == "inline" and stack and stack[-1]["first"]:
            state = stack[-1]
            state["first"] = False
            if state["ordered"]:
                prefix = f"{state['n']}." if len(stack) == 1 else f"（{state['n']}）"
                head = Token("text", "", 0)
                head.content = prefix
                tok.children = [head, *(tok.children or [])]
        out.append(tok)
    return out


def _render_body(markdown: str, *, gbt9704: bool, image_roots: Sequence[Path]) -> str:
    from markdown_it.utils import OptionsDict

    from ._markdown import _parser

    parsed = parse(markdown)
    tokens = _flatten_lists_for_gbt(parsed.tokens) if gbt9704 else parsed.tokens
    parsed.tokens = tokens
    renderer = _Renderer(parsed, gbt9704=gbt9704, image_roots=image_roots)
    # A soft break left inside a list item is a line break, as in the DOCX.
    options = OptionsDict({**dict(_parser().options), "breaks": True})
    return renderer.render(tokens, options, parsed.env)


def _fetcher_for(roots: Sequence[Path]):
    """A WeasyPrint fetcher that serves only files inside ``roots``.

    The HTML is built from model-authored Markdown; without this an
    ``<img>`` could make the renderer read any file the server can, or call
    out to the network.
    """
    from urllib.parse import unquote, urlparse

    from weasyprint.urls import URLFetcher

    resolved_roots = [Path(r).resolve() for r in roots]

    class _Confined(URLFetcher):
        def fetch(self, url, headers=None):  # noqa: D401 - WeasyPrint's signature
            parsed_url = urlparse(url)
            if parsed_url.scheme == "file":
                path = Path(unquote(parsed_url.path)).resolve()
                # An @font-face local() face is read through this fetcher too.
                if path.suffix.lower() in _FONT_SUFFIXES:
                    return super().fetch(url, headers)
                if any(root == path or root in path.parents for root in resolved_roots):
                    return super().fetch(url, headers)
            raise ValueError(f"refused to fetch {url!r}: a document loads nothing outside its image roots")

    return _Confined(allowed_protocols={"file"})


def _resolve_page_size(page_size: Optional[str], lang: Lang, gbt9704: bool) -> str:
    value = (page_size or "auto").strip().lower()
    if value in {"", "auto"}:
        return "A4" if (gbt9704 or lang == "zh") else "Letter"
    if value == "a4":
        return "A4"
    if value == "letter":
        if gbt9704:
            raise ValueError("layout='gbt9704' is an A4 layout (GB/T 9704-2012 5.1); drop page_size='Letter'")
        return "Letter"
    raise ValueError(f"Unknown page_size: {page_size!r} (expected 'A4', 'Letter' or 'auto')")


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
    text-indent: 0;
    transform: rotate(-30deg);
    transform-origin: center;
    white-space: pre-wrap;
    overflow: hidden;
}
"""


def _watermark_div(text: str) -> str:
    """Build the tiled watermark ``<div>`` body markup for ``text``."""
    safe = _html.escape(text)
    row = (safe + "  ") * 3
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
    layout: Optional[str] = None,
    page_size: Optional[str] = None,
    image_roots: Sequence[Path | str] = (),
) -> Path:
    """Render ``body_markdown`` into a PDF.

    ``lang``: ``"zh"``/``"zh-CN"``, ``"en"``, or ``None``/``"auto"`` (Han
    share). ``layout``: ``"memo"`` (default) or ``"gbt9704"``. ``page_size``:
    ``"A4"``, ``"Letter"`` or auto. ``disclaimer`` becomes the PDF ``Subject``
    metadata, not page content. ``image_roots`` lists the directories a
    Markdown image may load from.

    ``watermark`` stamps a fixed, repeating diagonal watermark on every page
    (e.g. ``"DO NOT FILE · 禁止对外提交"``) — used when the citation audit
    returns a DO-NOT-FILE verdict so an exported draft cannot be mistaken for a
    filed document."""
    from weasyprint import HTML

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    layout_key = (layout or "memo").strip().lower()
    if layout_key not in {"memo", "gbt9704"}:
        raise ValueError(f"Unknown layout: {layout!r} (expected 'memo' or 'gbt9704')")
    gbt9704 = layout_key == "gbt9704"
    if gbt9704 and (lang or "auto").strip().lower() in {"", "auto"}:
        doc_lang: Lang = "zh"
    else:
        doc_lang = resolve_lang(lang, title, subtitle, body_markdown)
    size = _resolve_page_size(page_size, doc_lang, gbt9704)
    roots = [Path(r) for r in image_roots]

    body_html = _render_body(body_markdown, gbt9704=gbt9704, image_roots=roots)

    if gbt9704:
        css = _GBT_CSS % {"fangsong": _FANGSONG_STACK, "kai": _KAI_STACK, "hei": _HEI_STACK}
    else:
        if doc_lang == "zh":
            page_label = '"第 " counter(page) " 页 共 " counter(pages) " 页"'
            margin = "25.4mm 31.7mm"
        else:
            page_label = '"Page " counter(page) " of " counter(pages)'
            margin = "1in"
        css = _MEMO_CSS % {
            "size": _PAGE_SIZES[size],
            "margin": margin,
            "page_label": page_label,
            "serif": _SERIF_STACK,
            "sans": _SANS_STACK,
            "kai": _KAI_STACK,
            "body_pt": "12" if doc_lang == "zh" else "11",
        }

    safe_title = _html.escape(title or "")
    safe_subtitle = _html.escape(subtitle) if subtitle else ""
    meta_description = (
        f'<meta name="description" content="{_html.escape(disclaimer, quote=True)}">' if disclaimer else ""
    )
    watermark_css = _WATERMARK_CSS if watermark else ""
    watermark_div = _watermark_div(watermark) if watermark else ""

    document = f"""<!doctype html>
<html lang="{bcp47(doc_lang)}"><head><meta charset="utf-8"><title>{safe_title}</title>
{meta_description}<meta name="generator" content="legal_helper write_pdf">
<style>{css}{_COMMON_CSS}{watermark_css}</style></head>
<body>
{watermark_div}
{f'<h1 class="doc-title">{safe_title}</h1>' if title else ''}
{f'<div class="subtitle">{safe_subtitle}</div>' if subtitle else ''}
{body_html}
</body></html>"""

    base = roots[0].resolve() if roots else None
    HTML(
        string=document,
        base_url=str(base) + "/" if base else None,
        url_fetcher=_fetcher_for(roots),
    ).write_pdf(str(output_path))
    return output_path


__all__ = ["write_pdf_document"]
