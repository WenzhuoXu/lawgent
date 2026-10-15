"""One clause index for every reader: DOCX auto-numbering and plain-text labels.

Two defects motivated this module, both measured on real uploads:

- **Labels changed form.** Word stores 第一条 as a paragraph with
  ``numFmt=chineseCounting`` and ``lvlText="第%1条"``; the text "第一条" is not in
  the XML at all. anydoc renders that level as Arabic (第1条, often as a list
  item ``- 第1条``), so 108 of 286 Chinese clause labels across 17 documents
  changed form between the document and what the model read — and a model that
  quoted 第1条 back to ``redline_docx`` found nothing to anchor on.
- **No clause structure.** The corpus outline and ``extract_clauses`` only saw
  a bare ``第X条`` / ``N Title`` at the start of a line, so the outline was empty
  for 36 of 52 uploads and ``extract_clauses`` returned one PREAMBLE for every
  contract shape tried.

So the rendered label is computed here from ``numbering.xml`` the way Word
computes it (level counters, restarts, ``startOverride``, multi-level
``%1.%2`` templates, Chinese formats), and one detector turns labelled lines —
whether a numbering label or typed text — into a clause tree that the outline,
``read_document(section=…)``, ``extract_clauses`` and ``redline_docx`` anchors
all share.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Number formats (ECMA-376 ST_NumberFormat), the subset real documents use
# ---------------------------------------------------------------------------

_CN_DIGITS = "零一二三四五六七八九"
_CN_UPPER_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_CN_UNITS = ("", "十", "百", "千")
_CN_UPPER_UNITS = ("", "拾", "佰", "仟")
_CN_GROUPS = ("", "万", "亿", "万亿")
_TIANGAN = "甲乙丙丁戊己庚辛壬癸"
_DIZHI = "子丑寅卯辰巳午未申酉戌亥"
_IDEOGRAPH_DIGITS = "〇一二三四五六七八九"
_FULLWIDTH = str.maketrans("0123456789", "０１２３４５６７８９")
_ROMAN = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
    (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)
_EN_ONES = (
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
    "Eighteen", "Nineteen",
)
_EN_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")
_EN_ORDINAL_IRREGULAR = {
    "One": "First", "Two": "Second", "Three": "Third", "Five": "Fifth", "Eight": "Eighth",
    "Nine": "Ninth", "Twelve": "Twelfth",
}


def _cn_group(value: int, digits: str, units: tuple[str, ...]) -> str:
    """Render 1..9999 with internal 零 for skipped places (一千零一十)."""
    out = ""
    pending_zero = False
    for place, unit in ((1000, units[3]), (100, units[2]), (10, units[1]), (1, units[0])):
        digit = value // place % 10
        if digit == 0:
            if out:
                pending_zero = True
            continue
        if pending_zero:
            out += digits[0]
            pending_zero = False
        out += digits[digit] + unit
    return out


def chinese_numeral(value: int, *, upper: bool = False) -> str:
    """一, 二 … 十, 十一 … 一百一十 … 一千零一; ``upper`` gives 壹拾 legal digits.

    The lower form drops the leading 一 of 一十 (第十条, 第十一条), as Word's
    ``chineseCounting`` and every PRC statute do; the legal form keeps 壹拾.
    """
    if value <= 0:
        return str(value)
    digits = _CN_UPPER_DIGITS if upper else _CN_DIGITS
    units = _CN_UPPER_UNITS if upper else _CN_UNITS
    groups: list[int] = []
    rest = value
    while rest:
        groups.append(rest % 10_000)
        rest //= 10_000
    out = ""
    zero_needed = False
    for idx in range(len(groups) - 1, -1, -1):
        group = groups[idx]
        if group == 0:
            zero_needed = bool(out)
            continue
        part = _cn_group(group, digits, units)
        if out and (zero_needed or group < 1000):
            part = digits[0] + part
        out += part + _CN_GROUPS[idx]
        zero_needed = False
    if not upper and out.startswith("一十"):
        out = out[1:]
    return out


def _roman(value: int) -> str:
    if value <= 0 or value >= 4000:
        return str(value)
    out = ""
    for amount, numeral in _ROMAN:
        while value >= amount:
            out += numeral
            value -= amount
    return out


def _letters(value: int) -> str:
    """A … Z, AA, BB … — Word repeats the letter rather than counting base-26."""
    if value <= 0:
        return str(value)
    return chr(ord("A") + (value - 1) % 26) * ((value - 1) // 26 + 1)


def _cardinal_text(value: int) -> str:
    if value <= 0 or value >= 100:
        return str(value)
    if value < 20:
        return _EN_ONES[value]
    tens, ones = divmod(value, 10)
    return _EN_TENS[tens] + ("-" + _EN_ONES[ones] if ones else "")


def _ordinal_text(value: int) -> str:
    words = _cardinal_text(value)
    if words == str(value):
        return words
    head, _, last = words.rpartition("-")
    if last in _EN_ORDINAL_IRREGULAR:
        last = _EN_ORDINAL_IRREGULAR[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last = last + "th"
    return f"{head}-{last}" if head else last


def _ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _enclosed(value: int, ranges: tuple[tuple[int, int, int], ...]) -> str:
    for low, high, base in ranges:
        if low <= value <= high:
            return chr(base + value - low)
    return str(value)


_CIRCLED = ((1, 20, 0x2460), (21, 35, 0x3251), (36, 50, 0x32B1))
_PARENTHESIZED = ((1, 20, 0x2474),)
_FULLSTOP = ((1, 20, 0x2488),)


def format_number(value: int, fmt: str) -> str:
    """Render ``value`` in an OOXML ``numFmt``; unknown formats fall back to decimal."""
    fmt = (fmt or "decimal").strip()
    if fmt in {"none", "bullet"}:
        return ""
    if fmt in {
        "chineseCounting", "chineseCountingThousand", "taiwaneseCounting",
        "taiwaneseCountingThousand", "japaneseCounting", "ideographLegalTraditional",
    }:
        return chinese_numeral(value)
    if fmt in {"chineseLegalSimplified", "japaneseLegal"}:
        return chinese_numeral(value, upper=True)
    if fmt in {"ideographTraditional"}:
        return _TIANGAN[(value - 1) % 10] if value > 0 else str(value)
    if fmt in {"ideographZodiac", "ideographZodiacTraditional"}:
        return _DIZHI[(value - 1) % 12] if value > 0 else str(value)
    if fmt in {"ideographDigital", "taiwaneseDigital", "japaneseDigitalTenThousand"}:
        return "".join(_IDEOGRAPH_DIGITS[int(d)] for d in str(value))
    if fmt in {"decimalEnclosedCircle", "decimalEnclosedCircleChinese", "ideographEnclosedCircle"}:
        return _enclosed(value, _CIRCLED)
    if fmt == "decimalEnclosedParen":
        return _enclosed(value, _PARENTHESIZED)
    if fmt == "decimalEnclosedFullstop":
        return _enclosed(value, _FULLSTOP)
    if fmt in {"decimalFullWidth", "decimalFullWidth2"}:
        return str(value).translate(_FULLWIDTH)
    if fmt == "decimalZero":
        return f"{value:02d}" if 0 <= value < 10 else str(value)
    if fmt == "upperRoman":
        return _roman(value)
    if fmt == "lowerRoman":
        return _roman(value).lower()
    if fmt == "upperLetter":
        return _letters(value)
    if fmt == "lowerLetter":
        return _letters(value).lower()
    if fmt == "ordinal":
        return _ordinal(value)
    if fmt == "cardinalText":
        return _cardinal_text(value)
    if fmt == "ordinalText":
        return _ordinal_text(value)
    if fmt == "hex":
        return format(value, "X")
    return str(value)


_CN_VALUES = {ch: i for i, ch in enumerate(_CN_DIGITS)}
_CN_VALUES.update({ch: i for i, ch in enumerate(_CN_UPPER_DIGITS)})
_CN_VALUES.update({"〇": 0, "两": 2, "０": 0})
_CN_MULTIPLIERS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG = {"万": 10_000, "亿": 100_000_000}
_FULLWIDTH_BACK = str.maketrans("０１２３４５６７８９", "0123456789")


def parse_number(token: str) -> Optional[int]:
    """Integer value of a label number: Arabic, fullwidth, Chinese (十二, 一百零二,
    壹拾), circled (③) or Roman (IV). ``None`` when it is none of those."""
    token = (token or "").strip().translate(_FULLWIDTH_BACK)
    if not token:
        return None
    if token.isascii() and token.isdigit():
        return int(token)
    if len(token) == 1:
        code = ord(token)
        for low, high, base in _CIRCLED + _PARENTHESIZED + _FULLSTOP:
            if base <= code <= base + (high - low):
                return low + code - base
    if all(ch in _CN_VALUES or ch in _CN_MULTIPLIERS or ch in _CN_BIG for ch in token):
        total = 0
        section = 0
        digit = 0
        for ch in token:
            if ch in _CN_VALUES:
                digit = _CN_VALUES[ch]
            elif ch in _CN_MULTIPLIERS:
                section += (digit or 1) * _CN_MULTIPLIERS[ch]
                digit = 0
            else:
                section += digit
                total += section * _CN_BIG[ch]
                section = 0
                digit = 0
        return total + section + digit
    upper = token.upper()
    if re.fullmatch(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", upper):
        value = 0
        rest = upper
        for amount, numeral in _ROMAN:
            while rest.startswith(numeral):
                value += amount
                rest = rest[len(numeral):]
        return value or None
    return None


# ---------------------------------------------------------------------------
# DOCX numbering: numbering.xml + styles.xml → rendered labels, in order
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _w(name: str) -> str:
    return f"{_W}{name}"


def _val(el: Any, default: Optional[str] = None) -> Optional[str]:
    if el is None:
        return default
    return el.get(_w("val"), default)


def _int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


@dataclass
class NumberingLevel:
    start: int = 1
    fmt: str = "decimal"
    text: str = ""
    restart: Optional[int] = None  # one-based level that restarts this one; 0 = never
    is_lgl: bool = False
    suffix: str = "tab"  # tab | space | nothing
    pstyle: str = ""


@dataclass
class StyleInfo:
    style_id: str
    name: str = ""
    based_on: str = ""
    num_id: Optional[str] = None
    ilvl: Optional[int] = None
    outline_level: Optional[int] = None
    kind: str = "paragraph"


@dataclass
class ParagraphNumbering:
    label: str
    ilvl: int
    num_id: str
    fmt: str
    suffix: str

    @property
    def is_bullet(self) -> bool:
        return self.fmt == "bullet"


def parse_styles(styles_xml: Optional[bytes]) -> dict[str, StyleInfo]:
    from lxml import etree

    out: dict[str, StyleInfo] = {}
    if not styles_xml:
        return out
    try:
        root = etree.fromstring(styles_xml)
    except etree.XMLSyntaxError:
        return out
    for style in root.iter(_w("style")):
        sid = style.get(_w("styleId")) or ""
        if not sid:
            continue
        info = StyleInfo(style_id=sid, kind=style.get(_w("type")) or "paragraph")
        info.name = _val(style.find(_w("name")), "") or ""
        info.based_on = _val(style.find(_w("basedOn")), "") or ""
        ppr = style.find(_w("pPr"))
        if ppr is not None:
            num_pr = ppr.find(_w("numPr"))
            if num_pr is not None:
                num_id = _val(num_pr.find(_w("numId")))
                if num_id is not None:
                    info.num_id = num_id
                ilvl = _val(num_pr.find(_w("ilvl")))
                if ilvl is not None:
                    info.ilvl = _int(ilvl, 0)
            outline = _val(ppr.find(_w("outlineLvl")))
            if outline is not None:
                info.outline_level = _int(outline, 9)
        out[sid] = info
    return out


_HEADING_NAME_RE = re.compile(r"^\s*(?:heading|标题)\s*(\d)\s*$", re.I)


def style_chain(styles: dict[str, StyleInfo], style_id: str) -> list[StyleInfo]:
    chain: list[StyleInfo] = []
    seen: set[str] = set()
    current = style_id
    while current and current in styles and current not in seen and len(chain) < 12:
        seen.add(current)
        chain.append(styles[current])
        current = styles[current].based_on
    return chain


def heading_level(styles: dict[str, StyleInfo], style_id: str, outline_level: Optional[int]) -> Optional[int]:
    """1-based heading level from the style name (Heading N / 标题 N / Title) or
    an outline level (direct or inherited); ``None`` for body text."""
    chain = style_chain(styles, style_id)
    for info in chain:
        name = (info.name or info.style_id).strip()
        if name.lower() == "title" or info.style_id.lower() == "title":
            return 1
        m = _HEADING_NAME_RE.match(name) or _HEADING_NAME_RE.match(info.style_id)
        if m:
            return max(1, min(int(m.group(1)), 6))
    level = outline_level
    if level is None:
        for info in chain:
            if info.outline_level is not None:
                level = info.outline_level
                break
    if level is not None and 0 <= level <= 8:
        return min(level + 1, 6)
    return None


class DocxNumbering:
    """Stateful label renderer: call :meth:`label_for` on paragraphs in order.

    Counters follow Word (and LibreOffice, checked against its rendering):
    every ``num`` of one ``abstractNum`` shares one set of counters, so lists
    continue one another; a ``num`` carrying ``startOverride`` resets the
    shared counter the first time it is used at that level ("restart
    numbering"), and later items of any ``num`` continue from there; a level
    restarts when a shallower level is used (or only the level named by
    ``lvlRestart``; ``0`` means never).
    """

    def __init__(self, numbering_xml: Optional[bytes], styles: dict[str, StyleInfo]):
        from lxml import etree

        self.styles = styles
        self.abstracts: dict[str, dict[int, NumberingLevel]] = {}
        self.style_links: dict[str, str] = {}
        self.num_style_links: dict[str, str] = {}
        self.nums: dict[str, tuple[str, dict[int, int], dict[int, NumberingLevel]]] = {}
        self._counters: dict[Any, dict[int, int]] = {}
        self._overrides_applied: set[tuple[str, int]] = set()
        if numbering_xml:
            try:
                root = etree.fromstring(numbering_xml)
            except etree.XMLSyntaxError:
                root = None
            if root is not None:
                self._parse(root)

    @staticmethod
    def _level(lvl: Any) -> NumberingLevel:
        level = NumberingLevel()
        level.start = _int(_val(lvl.find(_w("start"))), 1)
        level.fmt = _val(lvl.find(_w("numFmt")), "decimal") or "decimal"
        text_el = lvl.find(_w("lvlText"))
        level.text = _val(text_el, "") or "" if text_el is not None else ""
        restart = _val(lvl.find(_w("lvlRestart")))
        level.restart = _int(restart, -1) if restart is not None else None
        if level.restart is not None and level.restart < 0:
            level.restart = None
        lgl = lvl.find(_w("isLgl"))
        level.is_lgl = lgl is not None and (_val(lgl, "1") or "1") not in {"0", "false", "off"}
        level.suffix = _val(lvl.find(_w("suff")), "tab") or "tab"
        level.pstyle = _val(lvl.find(_w("pStyle")), "") or ""
        return level

    def _parse(self, root: Any) -> None:
        for abstract in root.iter(_w("abstractNum")):
            aid = abstract.get(_w("abstractNumId")) or ""
            levels: dict[int, NumberingLevel] = {}
            for lvl in abstract.findall(_w("lvl")):
                levels[_int(lvl.get(_w("ilvl")), 0)] = self._level(lvl)
            self.abstracts[aid] = levels
            link = _val(abstract.find(_w("styleLink")))
            if link:
                self.style_links[link] = aid
            num_link = _val(abstract.find(_w("numStyleLink")))
            if num_link:
                self.num_style_links[aid] = num_link
        for num in root.iter(_w("num")):
            nid = num.get(_w("numId")) or ""
            aid = _val(num.find(_w("abstractNumId")), "") or ""
            starts: dict[int, int] = {}
            levels: dict[int, NumberingLevel] = {}
            for override in num.findall(_w("lvlOverride")):
                ilvl = _int(override.get(_w("ilvl")), 0)
                start = override.find(_w("startOverride"))
                if start is not None:
                    starts[ilvl] = _int(_val(start), 1)
                lvl = override.find(_w("lvl"))
                if lvl is not None:
                    levels[ilvl] = self._level(lvl)
            self.nums[nid] = (aid, starts, levels)

    def _abstract_levels(self, aid: str, depth: int = 0) -> tuple[str, dict[int, NumberingLevel]]:
        levels = self.abstracts.get(aid) or {}
        if levels or depth > 4:
            return aid, levels
        link = self.num_style_links.get(aid)
        if link:
            target = self.style_links.get(link)
            if target and target != aid:
                return self._abstract_levels(target, depth + 1)
            style = self.styles.get(link)
            if style is not None and style.num_id and style.num_id in self.nums:
                return self._abstract_levels(self.nums[style.num_id][0], depth + 1)
        return aid, levels

    def resolve(self, ppr: Any, style_id: str) -> Optional[tuple[str, int]]:
        """(numId, ilvl) in effect for a paragraph, or None when unnumbered."""
        num_id: Optional[str] = None
        ilvl: Optional[int] = None
        if ppr is not None:
            num_pr = ppr.find(_w("numPr"))
            if num_pr is not None:
                num_id = _val(num_pr.find(_w("numId")))
                raw_ilvl = _val(num_pr.find(_w("ilvl")))
                if raw_ilvl is not None:
                    ilvl = _int(raw_ilvl, 0)
        if num_id is None or ilvl is None:
            for info in style_chain(self.styles, style_id):
                if num_id is None and info.num_id is not None:
                    num_id = info.num_id
                if ilvl is None and info.ilvl is not None:
                    ilvl = info.ilvl
                if num_id is not None and ilvl is not None:
                    break
        if num_id is None or num_id == "0" or num_id not in self.nums:
            return None
        if ilvl is None:
            # A style-linked list names its level by pStyle on the lvl.
            aid, levels = self._abstract_levels(self.nums[num_id][0])
            ilvl = next((i for i, lvl in levels.items() if lvl.pstyle and lvl.pstyle == style_id), 0)
        return num_id, max(0, min(int(ilvl), 8))

    def _levels_for(self, num_id: str) -> tuple[Any, dict[int, NumberingLevel], dict[int, int]]:
        aid, starts, overrides = self.nums[num_id]
        aid, levels = self._abstract_levels(aid)
        if overrides:
            levels = {**levels, **overrides}
        return ("abstract", aid), levels, starts

    def label_for(self, ppr: Any, style_id: str) -> Optional[ParagraphNumbering]:
        resolved = self.resolve(ppr, style_id)
        if resolved is None:
            return None
        num_id, ilvl = resolved
        key, levels, starts = self._levels_for(num_id)
        level = levels.get(ilvl)
        if level is None:
            return None
        counters = self._counters.setdefault(key, {})

        def start_of(i: int) -> int:
            if i in starts:
                return starts[i]
            lvl = levels.get(i)
            return lvl.start if lvl is not None else 1

        if ilvl in starts and (num_id, ilvl) not in self._overrides_applied:
            # "Restart numbering": the first use of this num at this level
            # starts the shared counter over from the override value.
            self._overrides_applied.add((num_id, ilvl))
            counters.pop(ilvl, None)
        counters[ilvl] = counters[ilvl] + 1 if ilvl in counters else start_of(ilvl)
        for deeper in list(counters):
            if deeper <= ilvl:
                continue
            deeper_level = levels.get(deeper)
            restart = deeper_level.restart if deeper_level is not None else None
            threshold = deeper if restart is None else restart  # one-based level number
            if restart == 0:
                continue
            if ilvl <= threshold - 1:
                del counters[deeper]

        def render(match: re.Match) -> str:
            ref = int(match.group(1)) - 1
            ref_level = levels.get(ref)
            value = counters.get(ref, start_of(ref))
            fmt = "decimal" if level.is_lgl else (ref_level.fmt if ref_level is not None else "decimal")
            return format_number(value, fmt)

        if level.fmt == "bullet":
            label = level.text
        else:
            label = re.sub(r"%([1-9])", render, level.text)
        return ParagraphNumbering(label=label, ilvl=ilvl, num_id=num_id, fmt=level.fmt, suffix=level.suffix)


# ---------------------------------------------------------------------------
# Clause labels in text, and the clause tree
# ---------------------------------------------------------------------------

_CN = "零〇一二三四五六七八九十百千万两"
_NUM = rf"[{_CN}0-9０-９]+"
_PREFIX_RE = re.compile(r"^(?:\s|#{1,6}\s+|[-*+]\s+|>\s*|\*\*|__)*")
_TOC_LINE_RE = re.compile(r"(?:\.{3,}|…{2,}|·{3,}|\t)\s*\d+\s*$")

# (family, pattern). The first match at the start of a line wins. Families are
# the unit of the hierarchy: a family seen inside another nests under it.
_LABEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cn_bian", re.compile(rf"第({_NUM})编")),
    ("cn_fenbian", re.compile(rf"第({_NUM})分编")),
    ("cn_zhang", re.compile(rf"第({_NUM})章")),
    ("cn_jie", re.compile(rf"第({_NUM})节")),
    ("cn_tiao", re.compile(rf"第({_NUM})条(?:之([{_CN}]+))?")),
    ("cn_kuan", re.compile(rf"第({_NUM})款")),
    ("cn_annex", re.compile(rf"附件\s*({_NUM})(?=[\s:：、.．]|$)")),
    ("cn_dun", re.compile(rf"([{_CN}]+)、")),
    ("cn_paren", re.compile(rf"[（(]([{_CN}]+)[)）]")),
    ("dec_paren", re.compile(r"[（(]([0-9０-９]+)[)）]")),
    ("circle", re.compile(r"([①-⑳㉑-㉟㊱-㊿])")),
    ("article", re.compile(r"(?:ARTICLE|Article|Art\.)\s+([0-9]+[A-Za-z]?|[IVXLCDM]+)\b")),
    ("section", re.compile(r"(?:SECTION|Section|Sec\.|§)\s*([0-9]+(?:\.[0-9]+)*)")),
    ("clause", re.compile(r"(?:CLAUSE|Clause)\s+([0-9]+(?:\.[0-9]+)*)")),
    ("schedule", re.compile(r"(?:SCHEDULE|Schedule|EXHIBIT|Exhibit|ANNEX|Annex|APPENDIX|Appendix)\s+([A-Z0-9]+)\b")),
    ("dec", re.compile(r"([0-9]+(?:\.[0-9]+){0,4})(?:\.(?!\d)|、|(?=\s))")),
    ("dec_rparen", re.compile(r"([0-9]+)[)）](?=\s|[一-鿿])")),
    ("letter_paren", re.compile(r"[（(]([a-z])[)）]")),
    ("roman_paren", re.compile(r"[（(]((?:ii|iii|iv|vi|vii|viii|ix|xi|xii)|[ivx]{2,})[)）]")),
)

# Fixed rank among families whose nesting is a convention, not a guess.
_RANK = {"cn_bian": 0, "cn_fenbian": 1, "cn_zhang": 2, "cn_jie": 3, "cn_tiao": 4, "cn_kuan": 5}


@dataclass
class LabelMatch:
    family: str
    number: str  # normalised: "3", "1.2", "12-1" for 第十二条之一, "A"
    label: str  # as written: "第三条", "1.2", "Article 5"
    rest: str  # the line after the label


def detect_label(line: str) -> Optional[LabelMatch]:
    """The clause label opening ``line`` (after markdown list/heading markers)."""
    if not line or not line.strip():
        return None
    stripped = _PREFIX_RE.sub("", line, count=1)
    if not stripped or _TOC_LINE_RE.search(stripped):
        return None
    for family, pattern in _LABEL_PATTERNS:
        m = pattern.match(stripped)
        if not m:
            continue
        raw = m.group(1)
        rest = stripped[m.end():]
        if family == "dec":
            # "1.5 million", "3 days" and "2026年" are not clause labels. A
            # decimal label is either closed by its own "." / "、" or followed
            # by a capitalised / CJK word, and it always has text after it.
            closed = stripped[len(raw):len(raw) + 1] in {".", "、"}
            if not closed and not re.match(r"\s*[A-Z一-鿿（(\"“「《]", rest):
                continue
            if not rest.strip():
                continue
            family = f"dec{raw.count('.') + 1}"
            number = raw
        elif family == "article":
            value = int(raw) if raw.isascii() and raw.isdigit() else parse_number(raw)
            number = str(value) if value is not None else raw.upper()
        elif family in {"section", "clause"}:
            number = raw
        elif family == "schedule":
            number = raw.upper()
        elif family in {"letter_paren", "roman_paren"}:
            number = raw.lower()
        else:
            value = parse_number(raw)
            if value is None:
                continue
            number = str(value)
            if family == "cn_tiao" and m.lastindex and m.lastindex >= 2 and m.group(2):
                sub = parse_number(m.group(2))
                if sub:
                    number = f"{value}-{sub}"
        label = stripped[: m.end()].strip()
        return LabelMatch(family=family, number=number, label=label, rest=rest.strip())
    return None


@dataclass
class ClauseUnit:
    """One addressable unit a clause is built from: a line of text or a DOCX block."""

    text: str
    paragraph: Optional[int] = None
    heading_level: Optional[int] = None
    is_table: bool = False


@dataclass
class Clause:
    label: str
    family: str
    number: str
    title: str
    depth: int
    start: int  # unit index of the clause's own line
    end: int  # exclusive: next clause at the same or a shallower depth
    own_end: int  # exclusive: next clause of any depth (flat segmentation)
    paragraph: Optional[int] = None
    parent: Optional[int] = None  # index into the clause list
    children: list[int] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.family}:{self.number}"

    def heading(self) -> str:
        return f"{self.label} {self.title}".strip() if self.title else self.label


def _is_heading_family(family: str) -> bool:
    return family.startswith("h") and family[1:].isdigit()


def _family_deeper_or_equal(existing: str, new: str) -> bool:
    if existing in _RANK and new in _RANK:
        return _RANK[existing] >= _RANK[new]
    if existing.startswith("dec") and new.startswith("dec") and existing[3:].isdigit() and new[3:].isdigit():
        return int(existing[3:]) >= int(new[3:])
    if _is_heading_family(existing) and _is_heading_family(new):
        return int(existing[1:]) >= int(new[1:])
    # An unlabelled heading opens a new part of the document: it closes any
    # labelled clause still open rather than nesting inside it.
    if _is_heading_family(new) and not _is_heading_family(existing):
        return True
    return False


def build_clause_index(units: list[ClauseUnit], *, include_headings: bool = True) -> list[Clause]:
    """Clause tree over ``units``: labelled lines (and, optionally, unlabelled
    headings) become clauses; nesting follows first appearance of each label
    family, corrected by the fixed 编 > 章 > 节 > 条 > 款 and 1 > 1.1 ranks."""
    starts: list[tuple[int, str, str, str, str]] = []  # (unit, family, number, label, title)
    for idx, unit in enumerate(units):
        if unit.is_table or not unit.text.strip():
            continue
        first_line = unit.text.strip().splitlines()[0]
        match = detect_label(first_line)
        if match is not None:
            starts.append((idx, match.family, match.number, match.label, match.rest))
        elif include_headings and unit.heading_level:
            title = _PREFIX_RE.sub("", first_line, count=1).strip()
            if title:
                starts.append((idx, f"h{unit.heading_level}", title.lower()[:60], "", title))
    clauses: list[Clause] = []
    stack: list[tuple[str, int]] = []  # (family, clause index)
    for unit_idx, family, number, label, title in starts:
        families = [f for f, _ in stack]
        if family in families:
            while stack and stack[-1][0] != family:
                stack.pop()
            stack.pop()
        else:
            while stack and _family_deeper_or_equal(stack[-1][0], family):
                stack.pop()
        parent = stack[-1][1] if stack else None
        clause = Clause(
            label=label,
            family=family,
            number=number,
            title=title[:120],
            depth=len(stack),
            start=unit_idx,
            end=len(units),
            own_end=len(units),
            paragraph=units[unit_idx].paragraph,
            parent=parent,
        )
        clauses.append(clause)
        if parent is not None:
            clauses[parent].children.append(len(clauses) - 1)
        stack.append((family, len(clauses) - 1))
    for i, clause in enumerate(clauses):
        if i + 1 < len(clauses):
            clause.own_end = clauses[i + 1].start
        for later in clauses[i + 1:]:
            if later.depth <= clause.depth:
                clause.end = later.start
                break
    return clauses


def clause_index_from_text(text: str) -> tuple[list[ClauseUnit], list[Clause]]:
    lines = (text or "").splitlines()
    units = []
    for line in lines:
        heading = None
        m = re.match(r"^(#{1,6})\s+\S", line)
        if m:
            heading = len(m.group(1))
        units.append(ClauseUnit(text=line, heading_level=heading))
    return units, build_clause_index(units)


def _query_key(query: str) -> Optional[LabelMatch]:
    q = (query or "").strip()
    if not q:
        return None
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*\.?", q):
        # A bare "3" / "3.2" has no text after it, which the line detector
        # (rightly) refuses; give it a word so it parses as a label.
        return detect_label(q.rstrip(".") + " X")
    return detect_label(q)


def find_clauses(clauses: list[Clause], query: str) -> list[Clause]:
    """Clauses addressed by ``query``: a label in any written form (第三条 =
    第3条, "Article 3", "3.2", "（二）") or, failing that, words of a title.

    Several clauses can share a label (a table of contents, an annex restarting
    its numbering); the longest comes first, which puts the body before the
    TOC line that names it.
    """
    q = (query or "").strip()
    if not q or not clauses:
        return []
    match = _query_key(q)
    hits: list[Clause] = []
    if match is not None:
        hits = [c for c in clauses if c.family == match.family and c.number == match.number]
        if not hits and match.family.startswith("dec") and "." not in match.number:
            # A bare number: try the label families a user means by "3".
            for family in ("cn_tiao", "article", "section", "clause", "cn_dun", "dec1"):
                hits = [c for c in clauses if c.family == family and c.number == match.number]
                if hits:
                    break
    if not hits:
        needle = q.lower()
        hits = [c for c in clauses if needle in c.title.lower() or needle == c.label.lower()]
    hits.sort(key=lambda c: (-(c.end - c.start), c.start))
    return hits


def outline_labels(clauses: Iterable[Clause], *, max_entries: int = 60, max_depth: int = 2) -> list[str]:
    """Short ``label title`` entries for an attachment outline."""
    out: list[str] = []
    for clause in clauses:
        if clause.depth > max_depth:
            continue
        entry = clause.heading()
        entry = " ".join(entry.split())[:60]
        if entry and entry not in out:
            out.append(entry)
        if len(out) >= max_entries:
            break
    return out


__all__ = [
    "Clause",
    "ClauseUnit",
    "DocxNumbering",
    "LabelMatch",
    "NumberingLevel",
    "ParagraphNumbering",
    "StyleInfo",
    "build_clause_index",
    "chinese_numeral",
    "clause_index_from_text",
    "detect_label",
    "find_clauses",
    "format_number",
    "heading_level",
    "outline_labels",
    "parse_number",
    "parse_styles",
    "style_chain",
]
