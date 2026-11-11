"""Section index for a fetched legal text, in every language we fetch.

A statute is read one provision at a time, so a fetched document is cut at
its own headings: 第一百二十八条, 第3条の2, 제3조의2, Article 9, Section 58,
§ 254.4, Artikel 7:17, Article L. 121-1, Điều 5, Pasal 12, Seksyen 3, มาตรา 7,
Artigo 5.º, المادة 4, មាត្រា 6 — and, in a judgment, its numbered paragraphs.
Containers (编/章/节, Part, Chapter, Titre, Hoofdstuk, Chương, BAB, หมวด …)
are kept for the outline but never returned as "the section".

Every label is reduced to a key that a request reduces to as well, so
"第128条", "第一百二十八条第二款", "128", "Art. 128" all find the same provision
(sub-units — 款, (1)(a), paragraph 2 — return their whole section).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from ..documents.clauses import parse_number

_CN_NUM = r"[〇零一二三四五六七八九十百千万两0-9０-９]+"

_DIGIT_TABLES = str.maketrans(
    "๐๑๒๓๔๕๖๗๘๙٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹០១២៣៤៥៦៧៨៩０１２３４５６７８９",
    "0123456789" * 5,
)


@dataclass
class Section:
    label: str
    key: str
    heading: str
    start: int
    end: int
    level: str = "article"  # "article" | "container" | "para"
    #: A provision's own caption, when the source's structure gives one
    #: (e-Gov 見出し, CLML headings); detected sections leave it empty.
    caption: str = ""

    def to_outline(self) -> str:
        return self.heading if self.level != "container" else f"[{self.heading}]"


def _ascii_digits(s: str) -> str:
    return s.translate(_DIGIT_TABLES)


def _cn_key(num: str, sub: Optional[str]) -> Optional[str]:
    value = parse_number(_ascii_digits(num))
    if value is None:
        return None
    key = str(value)
    if sub:
        sv = parse_number(_ascii_digits(sub))
        if sv:
            key += f".{sv}"
    return key


def _plain_key(num: str) -> str:
    """'58A' → '58A', '9.1' → '9.1', 'L. 121-1' → 'L121-1', '7:17' → '7:17'."""
    s = _ascii_digits(num).strip().rstrip(".:")
    s = re.sub(r"[\s.](?=\d)", "", s) if re.match(r"^[A-Z]\.?\s", s) else s
    s = s.replace(" ", "")
    return s.upper() if re.search(r"\d", s) else s


# Each article pattern: (regex, key function). The regex must match at the
# start of a (stripped) line; group "n" is the number, optional "s" a sub-number.
_ARTICLE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 第一百二十八条 / 第128條 / 第一百二十条之一 / 第三条の二 (JP) / 第 99-1 條 (TW display)
    (re.compile(r"^第\s*(?P<n>\d+)\s*-\s*(?P<s>\d+)\s*[条條]"), "cn"),
    (re.compile(rf"^第\s*(?P<n>{_CN_NUM})\s*[条條](?:\s*(?:之|の)\s*(?P<s>{_CN_NUM}))?"), "cn"),
    # 제3조 / 제3조의2 (KR)
    (re.compile(r"^제\s*(?P<n>\d+)\s*조(?:\s*의\s*(?P<s>\d+))?"), "cn"),
    # Article 9 / Art. 9a / Article L. 121-1 / Article 7:17 / Artikel 7:17 / Artigo 5.º / Articolo 3
    (re.compile(
        r"^(?:Article|ARTICLE|Art\.|Artikel|ARTIKEL|Artigo|ARTIGO|Articolo|Artículo|Art)\s*"
        r"(?P<n>(?:[LRDA]\.?\s?)?\d+[a-zA-Z]{0,3}(?:[-:.]\d+[a-zA-Z]?)*)(?:\.?\s*[ºo°](?![a-zA-Z]))?"
    ), "plain"),
    # Section 58 / Sec. 3A / § 254.4 / s 58 (only when short; see _accept)
    (re.compile(r"^(?:Section|SECTION|Sec\.|SEC\.|§+)\s*(?P<n>\d+[A-Za-z]{0,3}(?:[-./]\d+[A-Za-z]?)*)"), "plain"),
    # Rule 3 / Regulation 4 / Reg. 4 / Order 5
    (re.compile(r"^(?:Rule|RULE|Regulation|REGULATION|Reg\.)\s+(?P<n>\d+[A-Za-z]{0,3}(?:\.\d+)?)"), "plain"),
    # Điều 5 (VN), Pasal 12 (ID), Seksyen 3 / Perkara 3 (MY), Mục is a container
    (re.compile(r"^(?:Điều|ĐIỀU|Pasal|PASAL|Seksyen|SEKSYEN|Perkara)\s+(?P<n>\d+[A-Za-z]{0,2})"), "plain"),
    # มาตรา 7 / มาตรา ๗/๑ (TH)
    (re.compile(r"^(?:มาตรา|ข้อ)\s*(?P<n>[๐-๙0-9]+(?:/[๐-๙0-9]+)?)"), "plain"),
    # المادة (4) / مادة 4 (AR)
    (re.compile(r"^(?:المادة|مادة)\s*\(?\s*(?P<n>[0-9٠-٩]+)"), "plain"),
    # មាត្រា ៦ (KH)
    (re.compile(r"^មាត្រា\s*(?P<n>[0-9០-៩]+)"), "plain"),
]

# A bare-number section heading as legislation.gov.uk / AustLII print it:
# "58 Duty to …", "58.—(1) …", "12A Interpretation". Only used for legislation,
# and only when the rest of the line looks like a heading or a first subsection.
_BARE_SECTION = re.compile(r"^(?P<n>\d{1,4}[A-Z]{0,3})(?:\.?—|\.|\s)\s*(?P<rest>.*)$")

# Numbered judgment paragraphs: "[12]", "12.", "12 " at line start.
_PARA = re.compile(
    r"^\[(?P<n>\d{1,4}(?:\.\d{1,3}){0,3})\]"
    r"|^(?P<m>\d{1,4}(?:\.\d{1,3}){0,3})(?:\.\s+|\s*\|\s*|\s{2,}|\s+(?=[A-Z\u00C0-\u024F]))\S"
)

_CONTAINER = re.compile(
    rf"^(?:第\s*{_CN_NUM}\s*[编編章节節部款]"
    r"|(?:PART|Part|CHAPTER|Chapter|TITLE|Title|DIVISION|Division|Subpart|SUBPART|SCHEDULE|Schedule)\s+[\dIVXLC]+[A-Z]?\b"
    r"|제\s*\d+\s*(?:장|절|편|관)"
    r"|(?:Chương|CHƯƠNG|Mục|MỤC)\s+[IVXLC\d]+"
    r"|(?:BAB|Bab|Bahagian|BAHAGIAN)\s+[IVXLC\d]+"
    r"|(?:Titre|TITRE|Chapitre|CHAPITRE|Livre|LIVRE)\s+[IVXLC\dpremier]+"
    r"|(?:Hoofdstuk|HOOFDSTUK|Titel|TITEL|Afdeling|AFDELING|Paragraaf)\s+\d+"
    r"|(?:หมวด|ส่วนที่)\s*[๐-๙0-9]+"
    r"|(?:Capítulo|CAPÍTULO|Secção|SECÇÃO)\s+[IVXLC\d]+"
    r"|(?:الباب|الفصل)\s"
    r")"
)


def _accept_label(line: str, match: re.Match[str], style: str) -> bool:
    """Reject an in-sentence reference that happens to open a line."""
    if style == "cn":
        return True
    rest = line[match.end():].strip()
    if not rest:
        return True
    if rest[0] in ".—–-:)":
        return True
    if re.match(r"^\([A-Z][^()]{1,80}\)", rest):
        # "Article 7 (Purpose) (1) …": a captioned article run onto one line.
        return True
    if rest[0].isalpha() and rest[0].islower():
        # "Article 37 of the Constitution…", "Section 3 shall apply…": a
        # sentence that opens with a reference, not a heading.
        return False
    # A heading or a first subsection follows ("Article 9 Right to care",
    # "Section 3 (1) The…"); a sentence ("Article 9 of Regulation … applies")
    # runs on.
    if len(line) <= 160:
        return True
    return bool(re.match(r"^\(?1\)|^\(a\)", rest))


def _key(match: re.Match[str], style: str) -> Optional[str]:
    n = match.group("n")
    s = match.groupdict().get("s")
    if style == "cn":
        return _cn_key(n, s)
    return _plain_key(n)


def detect_sections(text: str, *, kind: str = "legislation", bare_numbers: bool = False) -> list[Section]:
    """Cut ``text`` at its own headings; offsets index into ``text``.

    ``bare_numbers`` also cuts at "58 Duty to …" / "2. Definitions" lines,
    for a source whose provisions carry no word ("Section", "Pasal"): it
    is off by default because numbered list items look the same (985 false
    sections in an Indonesian statute).
    """
    marks: list[tuple[int, str, str, str, str]] = []  # (start, label, key, heading, level)
    pos = 0
    bare_ok = bare_numbers and kind != "case"
    for raw in text.split("\n"):
        line = raw.strip()
        start = pos
        pos += len(raw) + 1
        if not line:
            continue
        found = False
        if kind == "case":
            # A judgment is cut at its numbered paragraphs only: "Article 7 of
            # that regulation provides…" opening a line is a quotation.
            m = _PARA.match(line)
            if m:
                n = m.group("n") or m.group("m")
                marks.append((start, f"[{n}]", n, line[:120], "para"))
            continue
        for pattern, style in _ARTICLE_PATTERNS:
            m = pattern.match(line)
            if m and _accept_label(line, m, style):
                key = _key(m, style)
                if key:
                    label = line[: m.end()].strip()
                    marks.append((start, label, key, line[:120], "article"))
                    found = True
                break
        if found:
            continue
        if _CONTAINER.match(line) and len(line) <= 120:
            marks.append((start, line, "", line[:120], "container"))
            continue
        if bare_ok:
            m = _BARE_SECTION.match(line)
            if m and _looks_like_bare_heading(m.group("rest")):
                n = m.group("n")
                marks.append((start, n, n.upper(), line[:120], "article"))
    sections: list[Section] = []
    for i, (start, label, key, heading, level) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        sections.append(Section(label=label, key=key, heading=heading, start=start, end=end, level=level))
    return _drop_toc_runs(sections)


def _looks_like_bare_heading(rest: str) -> bool:
    rest = rest.strip()
    if not rest:
        return False
    if rest.startswith("(1)") or rest.startswith("—(1)"):
        return True
    # A capitalised heading of a few words, not a sentence fragment or a date.
    return bool(re.match(r"^[A-Z][A-Za-z’'\-,() ]{2,90}$", rest)) and not re.match(
        r"^(January|February|March|April|May|June|July|August|September|October|November|December)\b",
        rest,
    )


def _drop_toc_runs(sections: list[Section]) -> list[Section]:
    """A table of contents lists the same keys again with no body between.

    Article entries shorter than their heading line (nothing but the label
    and a title) that are later repeated with a body are dropped; containers
    are kept once.
    """
    seen_with_body: dict[str, int] = {}
    for s in sections:
        if s.level == "article" and (s.end - s.start) > len(s.heading) + 2:
            seen_with_body.setdefault(s.key, 0)
            seen_with_body[s.key] += 1
    out: list[Section] = []
    container_seen: set[str] = set()
    for s in sections:
        if s.level == "article" and (s.end - s.start) <= len(s.heading) + 2 and s.key in seen_with_body:
            continue
        if s.level == "container":
            norm = re.sub(r"\s+", "", s.heading)
            if norm in container_seen:
                # keep the later (body) occurrence's position for the outline
                out = [x for x in out if not (x.level == "container" and re.sub(r"\s+", "", x.heading) == norm)]
            container_seen.add(norm)
        out.append(s)
    return out


# ----------------------------------------------------------------- requests

_REQUEST_PREFIX = re.compile(
    r"^(?:articolo|artikel|artigo|article|art\.|art(?![a-z])|section|sec\.|sec(?![a-z])|ss?\.|ss?(?![a-z])|§+|"
    r"regulation|reg\.|reg(?![a-z])|rule|r\.|paragraph|paras?\.|paras?(?![a-z])|¶|"
    r"điều|pasal|seksyen|perkara|มาตรา|ข้อ|المادة|مادة|មាត្រា)\s*",
    re.I,
)


def request_key(request: str) -> Optional[str]:
    """Reduce a requested section to the key its heading reduces to."""
    r = (request or "").strip()
    if not r:
        return None
    r = _ascii_digits(r)
    # 第 99-1 條 (TW display) / 第一百二十八条第二款 / 第128条之一 / 第3条の2 / 제3조의2
    m = re.match(r"^第?\s*(?P<n>\d+)\s*-\s*(?P<s>\d+)\s*[条條]", r)
    if m:
        return _cn_key(m.group("n"), m.group("s"))
    m = re.match(rf"^第?\s*(?P<n>{_CN_NUM})\s*[条條](?:\s*(?:之|の)\s*(?P<s>{_CN_NUM}))?", r)
    if m:
        return _cn_key(m.group("n"), m.group("s"))
    m = re.match(r"^제?\s*(?P<n>\d+)\s*조(?:\s*의\s*(?P<s>\d+))?", r)
    if m:
        return _cn_key(m.group("n"), m.group("s"))
    r = _REQUEST_PREFIX.sub("", r).strip()
    r = re.sub(r"^\[(\d+(?:\.\d+)*)\]$", r"\1", r)
    r = re.sub(r"(?<=\d)\.?\s*[ºo°](?![A-Za-z])", "", r)  # Artigo 30.º → 30
    # French code articles keep their letter: "L. 121-1" → "L121-1".
    m = re.match(r"^([LRDA])\.?\s*(\d+(?:-\d+)*)", r)
    if m:
        return m.group(1).upper() + m.group(2)
    # Drop sub-units: 58(4)(a) → 58
    r = re.split(r"[\s(]", r, maxsplit=1)[0] if r else r
    if not r:
        return None
    if re.fullmatch(_CN_NUM, r):
        v = parse_number(r)
        return str(v) if v is not None else None
    return _plain_key(r)


def _norm_label(value: str) -> str:
    return re.sub(r"\s+", "", _ascii_digits(value or "")).casefold()


def _norm_key(value: str) -> str:
    return (value or "").upper().replace("-", ".")


def find_section(sections: Iterable[Section], request: str) -> list[Section]:
    """Every section whose label or key matches the request (first is the best).

    The request is first compared with the labels as written, so a label the
    reduction cannot express ("Supplementary Provisions", "المادة 1 مكرر",
    "Article 71-2") still resolves; then by key, where "71-2" and "71.2" are
    the same provision.
    """
    secs = [s for s in sections if s.level != "container"]
    wanted = _norm_label(request)
    if wanted:
        by_label = [s for s in secs if _norm_label(s.label) == wanted or _norm_label(s.key) == wanted]
        if by_label:
            return by_label
    key = request_key(request)
    if not key:
        return []
    exact = [s for s in secs if s.key == key]
    if exact:
        return exact
    nk = _norm_key(key)
    return [s for s in secs if _norm_key(s.key) == nk]


def _title_of(s: Section, text: str) -> str:
    """A provision's own title, when it has one ("9 Right to care"), else ""."""
    if s.caption:
        return s.caption if len(s.caption) <= 90 else s.caption[:89] + "…"
    if s.label.startswith(("第", "제")):
        # CN / TW / KR statutes do not title their articles in the text.
        return ""
    first = text[s.start:s.end].split("\n", 1)[0].strip()
    rest = first[len(s.label):].strip(" .—–-:　") if first.startswith(s.label) else ""
    if rest and len(rest) <= 90 and not rest.endswith(("。", ".", "；", ";", "，", ",", "：", ":")):
        return rest
    if not rest:
        # Title on the next line (EUR-Lex prints "Article 9" then "Right to care").
        nxt = text[s.start:s.end].split("\n")[1:3]
        for line in nxt:
            line = line.strip()
            if line and len(line) <= 90 and not line.endswith(("。", ".", "；", ";", "：", ":", "，", ",")):
                return line
            if line:
                break
    return ""


def outline(sections: list[Section], text: str, *, limit: int = 300) -> list[str]:
    """Compact outline of a document.

    Containers are listed with the range of provisions they hold
    ("[第四节 承运人责任] 第一百二十八条–第一百四十五条"); a provision is listed
    on its own only when it carries a title ("Article 9 Right to care"), so a
    262-article statute outlines in a few dozen lines.
    """
    lines: list[str] = []
    pending: Optional[str] = None
    first_label: Optional[str] = None
    last_label: Optional[str] = None

    def flush() -> None:
        nonlocal pending, first_label, last_label
        if pending is not None:
            span = ""
            if first_label:
                span = f" {first_label}" if first_label == last_label else f" {first_label}–{last_label}"
            lines.append(f"[{pending}]{span}")
        pending, first_label, last_label = None, None, None

    for s in sections:
        if s.level == "para":
            continue
        if s.level == "container":
            flush()
            pending = s.heading
            continue
        title = _title_of(s, text)
        if title:
            flush()
            lines.append(f"{s.label} {title}")
            continue
        if pending is None:
            pending = ""
        first_label = first_label or s.label
        last_label = s.label
    flush()
    lines = [ln.replace("[] ", "") for ln in lines]
    if len(lines) > limit:
        lines = lines[:limit] + [f"… {len(lines) - limit} more outline lines"]
    return lines
