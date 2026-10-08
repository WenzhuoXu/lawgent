"""One Markdown front end for the Word and PDF writers.

The Word writer used to read Markdown line by line with regexes, so anything
the regexes did not know reached the document as literal text: 517 ``[text](url)``
links across 28 documents, ``####`` headings, ``[^1]`` footnote markers,
``~~strike~~`` and ``<br>``. Nesting was stripped. The PDF writer used
Python-Markdown, which does better but drops a list nested two spaces deep —
the indentation most models write — back to literal ``- item`` text.

Both writers now consume the same token stream from markdown-it-py
(CommonMark, MIT) with GFM tables and strikethrough enabled and footnotes from
mdit-py-plugins (MIT). Three adjustments sit on top, each for a reason:

* **CJK-friendly emphasis.** CommonMark decides whether ``**`` can open or
  close by looking at the characters on either side, and treats a fullwidth
  colon followed by a Han character as "punctuation then letter" — so
  ``**结论：**本交易`` keeps its asterisks. That pattern is everywhere in Chinese
  model output. The flanking test below counts a CJK character next to a
  punctuation mark the way it counts whitespace, which is the amendment
  proposed in tats-u/markdown-cjk-friendly (MIT; the specification is
  implemented here, not vendored — its Python port pins markdown-it-py < 4).
* **A single newline ends a paragraph.** Strict CommonMark joins
  ``甲方：…\\n乙方：…`` into one paragraph. Party blocks, signature blocks and
  address lines are written that way, and the old writer kept each line, so
  each line of a plain paragraph stays its own paragraph. Inside a list item
  the newline becomes a line break instead, so the item keeps its number.
* **Raw HTML is an allowlist.** ``<br>`` and a few inline formatting tags
  render; every other tag is shown as the text it is. ``<甲方名称>`` is a
  placeholder in a contract, not markup, and a PDF renderer that fetched an
  ``<img src="file://…">`` would be reading the server's disk.
"""

from __future__ import annotations

import re
import types
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

from markdown_it import MarkdownIt
from markdown_it.rules_inline.state_inline import StateInline
from markdown_it.token import Token

from ._text import is_wide

try:  # mdit-py-plugins is a declared dependency; degrade to no footnotes without it.
    from mdit_py_plugins.footnote import footnote_plugin
except Exception:  # pragma: no cover - exercised only on a broken install
    footnote_plugin = None


# ---------------------------------------------------------------------------
# CJK-friendly delimiter scan
# ---------------------------------------------------------------------------


def _is_cjk(ch: str) -> bool:
    return bool(ch) and is_wide(ch)


class _CjkFriendlyState(StateInline):
    """StateInline whose emphasis scan treats CJK text as a word boundary."""

    def scanDelims(self, start: int, canSplitWord: bool):  # noqa: N802 — upstream name
        from markdown_it.common.utils import isMdAsciiPunct, isPunctChar, isWhiteSpace
        from markdown_it.rules_inline.state_inline import Scanned

        pos = start
        maximum = self.posMax
        marker = self.src[start]
        last_char = self.src[start - 1] if start > 0 else " "
        while pos < maximum and self.src[pos] == marker:
            pos += 1
        count = pos - start
        next_char = self.src[pos] if pos < maximum else " "

        last_punct = isMdAsciiPunct(ord(last_char)) or isPunctChar(last_char)
        next_punct = isMdAsciiPunct(ord(next_char)) or isPunctChar(next_char)
        last_space = isWhiteSpace(ord(last_char))
        next_space = isWhiteSpace(ord(next_char))
        # The amendment: a CJK neighbour on the far side counts like
        # whitespace when the near side is punctuation.
        last_cjk = _is_cjk(last_char)
        next_cjk = _is_cjk(next_char)

        left_flanking = not (
            next_space or (next_punct and not (last_space or last_punct or last_cjk))
        )
        right_flanking = not (
            last_space or (last_punct and not (next_space or next_punct or next_cjk))
        )
        can_open = left_flanking and (canSplitWord or (not right_flanking) or last_punct)
        can_close = right_flanking and (canSplitWord or (not left_flanking) or next_punct)
        return Scanned(can_open, can_close, count)


def _cjk_inline_parse(self, src, md, env, tokens):
    """``ParserInline.parse`` with the CJK-friendly state (upstream body)."""
    state = _CjkFriendlyState(src, md, env, tokens)
    self.tokenize(state)
    for rule in self.ruler2.getRules(""):
        rule(state)
    return state.tokens


@lru_cache(maxsize=1)
def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    if footnote_plugin is not None:
        md.use(footnote_plugin)
    md.inline.parse = types.MethodType(_cjk_inline_parse, md.inline)
    return md


# ---------------------------------------------------------------------------
# Raw HTML allowlist
# ---------------------------------------------------------------------------

#: Inline tags that render. No attributes are allowed on any of them.
ALLOWED_INLINE_TAGS = frozenset({"br", "sup", "sub", "u", "b", "strong", "i", "em", "s", "del", "strike"})
_TAG_RE = re.compile(r"^<\s*(/?)\s*([a-zA-Z]+)\s*(/?)\s*>$")


def allowed_tag(raw: str) -> tuple[str, bool, bool] | None:
    """``(tag, is_closing, is_self_closing)`` for an allowed tag, else None."""
    match = _TAG_RE.match((raw or "").strip())
    if not match:
        return None
    tag = match.group(2).lower()
    if tag not in ALLOWED_INLINE_TAGS:
        return None
    return tag, bool(match.group(1)), bool(match.group(3)) or tag == "br"


def _text_token(content: str, like: Token | None = None) -> Token:
    tok = Token("text", "", 0)
    tok.content = content
    if like is not None:
        tok.level = like.level
    return tok


def _sanitize_inline(children: list[Token]) -> list[Token]:
    out: list[Token] = []
    for child in children:
        if child.type == "html_inline" and allowed_tag(child.content) is None:
            out.append(_text_token(child.content, child))
        else:
            out.append(child)
    return out


_BR_ONLY_RE = re.compile(r"^(?:\s*<\s*br\s*/?\s*>\s*)+$", re.I)


def _html_block_as_paragraph(tok: Token) -> list[Token]:
    """An HTML block becomes a paragraph holding its literal text."""
    if _BR_ONLY_RE.match(tok.content or ""):
        return []
    p_open = Token("paragraph_open", "p", 1)
    p_open.map, p_open.level, p_open.block = tok.map, tok.level, True
    inline = Token("inline", "", 0)
    inline.content = (tok.content or "").rstrip("\n")
    inline.level, inline.map, inline.block = tok.level + 1, tok.map, True
    children: list[Token] = []
    for index, line in enumerate(inline.content.splitlines()):
        if index:
            children.append(Token("softbreak", "br", 0))
        children.append(_text_token(line))
    inline.children = children
    p_close = Token("paragraph_close", "p", -1)
    p_close.level, p_close.block = tok.level, True
    return [p_open, inline, p_close]


# ---------------------------------------------------------------------------
# Soft breaks
# ---------------------------------------------------------------------------


def _split_soft_paragraphs(tokens: list[Token]) -> list[Token]:
    """Split each visible paragraph at its soft breaks.

    Hidden paragraphs (the content of a tight list item) keep their soft
    breaks, which the renderers draw as line breaks.
    """
    out: list[Token] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (
            tok.type == "paragraph_open"
            and not tok.hidden
            and i + 2 < len(tokens)
            and tokens[i + 1].type == "inline"
            and tokens[i + 2].type == "paragraph_close"
            and any(c.type == "softbreak" for c in tokens[i + 1].children or [])
        ):
            inline, close = tokens[i + 1], tokens[i + 2]
            segments: list[list[Token]] = [[]]
            for child in inline.children or []:
                if child.type == "softbreak":
                    segments.append([])
                else:
                    segments[-1].append(child)
            for segment in segments:
                if not any(c.type != "text" or c.content.strip() for c in segment):
                    continue
                p_open = Token("paragraph_open", "p", 1)
                p_open.map, p_open.level, p_open.block = tok.map, tok.level, True
                piece = Token("inline", "", 0)
                piece.level, piece.map, piece.block = inline.level, inline.map, True
                piece.children = segment
                piece.content = "".join(c.content for c in segment)
                p_close = Token("paragraph_close", "p", -1)
                p_close.level, p_close.block = close.level, True
                out.extend([p_open, piece, p_close])
            i += 3
            continue
        out.append(tok)
        i += 1
    return out


# ---------------------------------------------------------------------------
# Public parse
# ---------------------------------------------------------------------------


@dataclass
class Parsed:
    """A parsed Markdown body: block tokens plus its footnote definitions."""

    tokens: list[Token]
    #: footnote id -> the block tokens of its definition (anchors removed).
    footnotes: dict[int, list[Token]] = field(default_factory=dict)
    env: dict[str, Any] = field(default_factory=dict)


def parse(markdown: str) -> Parsed:
    env: dict[str, Any] = {}
    raw = _parser().parse(markdown or "", env)
    tokens: list[Token] = []
    footnotes: dict[int, list[Token]] = {}
    current: Optional[int] = None
    in_block = False
    for tok in raw:
        if tok.type == "inline" and tok.children is not None:
            tok.children = _sanitize_inline(tok.children)
        if tok.type == "footnote_block_open":
            in_block = True
            continue
        if tok.type == "footnote_block_close":
            in_block = False
            continue
        if in_block:
            if tok.type == "footnote_open":
                current = int(tok.meta.get("id", 0))
                footnotes[current] = []
            elif tok.type == "footnote_close":
                current = None
            elif current is not None and tok.type != "footnote_anchor":
                if tok.type == "inline" and tok.children:
                    tok.children = [c for c in tok.children if c.type != "footnote_anchor"]
                footnotes[current].append(tok)
            continue
        if tok.type == "html_block":
            tokens.extend(_html_block_as_paragraph(tok))
            continue
        tokens.append(tok)
    tokens = _split_soft_paragraphs(tokens)
    for fid, body in list(footnotes.items()):
        footnotes[fid] = _split_soft_paragraphs(body)
    return Parsed(tokens=tokens, footnotes=footnotes, env=env)


def inline_text(children: list[Token] | None) -> str:
    """Plain text of an inline token's children, as a reader would see it."""
    parts: list[str] = []
    for child in children or []:
        if child.type in {"text", "code_inline"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif child.type == "html_inline":
            tag = allowed_tag(child.content)
            if tag and tag[0] == "br":
                parts.append("\n")
        elif child.type == "image":
            parts.append(inline_text(child.children) or child.content)
        elif child.type == "footnote_ref":
            continue
    return "".join(parts)


def heading_levels(tokens: list[Token]) -> list[int]:
    return [int(t.tag[1]) for t in tokens if t.type == "heading_open" and t.tag[:1] == "h"]


# ---------------------------------------------------------------------------
# GB/T 9704 ordinals
# ---------------------------------------------------------------------------

_CN_NUM = "一二三四五六七八九十百零〇"
_ORDINAL_RES = (
    (1, re.compile(rf"^[{_CN_NUM}]+、")),
    (2, re.compile(rf"^[（(][{_CN_NUM}]+[）)]")),
    # "1." but not "1.5亿元": the dot must not start a decimal fraction.
    (3, re.compile(r"^\d+[.．](?!\d)")),
    (4, re.compile(r"^[（(]\d+[）)]")),
)


def gb_ordinal_level(text: str) -> Optional[int]:
    """GB/T 9704 structure level (1-4) from a leading 层次序数, else None.

    7.3.3: "文中结构层次序数依次可以用'一、''（一）''1.''（1）'标注".
    """
    stripped = (text or "").lstrip()
    for level, pattern in _ORDINAL_RES:
        if pattern.match(stripped):
            return level
    return None


__all__ = [
    "ALLOWED_INLINE_TAGS",
    "Parsed",
    "allowed_tag",
    "gb_ordinal_level",
    "heading_levels",
    "inline_text",
    "parse",
]
