"""Deterministic clean-up of answer text, applied as a stream filter.

Some answer defects are provable from the text alone and need no model to fix:
`utm_source=openai` tracking parameters (85 of 409 answers), the same search
citation chip repeated three or more times (85 of 409), links to uploaded files
that no route serves (818 dead links in 41 answers), and the English pipeline
token `pinpoint unavailable` left in a Chinese answer. This filter repairs them
as the text streams — whole lines at a time, so a URL is never split — and
counts what it did. It never grades style and never runs a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .answer_contract import SOURCES_HEADING, STATUS

_URL_RE = re.compile(r"https?://[^\s<>()\"'\]]+[^\s<>()\"'\].,;:!?。，；：！？）]")
# A markdown link: [label](target). Targets may be URLs or local paths — or
# empty, or a word, when the model had no URL to give.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]*)\)")
# A citation chip: a markdown link wrapped in parentheses, as hosted web
# search emits them — `([domain](url))`.
_CHIP_RE = re.compile(r"\(\[[^\]\n]+\]\(([^)\s]+)\)\)")
_UPLOAD_RE = re.compile(
    r"(?:file://)?(?:[^\s()\[\]]*?/)?(?:outputs/)?chat_uploads/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/([^\s()\[\]]+)"
)
_ARTIFACT_RE = re.compile(
    r"(?:file://)?(?:[^\s()\[\]]*?/)?(?:outputs/)?chat_artifacts/([A-Za-z0-9_-]+)/([^\s()\[\]]+)"
)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
# A sources-list item and the separator before its status word.
_ITEM_RE = re.compile(r"^(\s*(?:\d+[.)、]|[-*•])\s+)(.+)$")
_STATUS_SPLIT_RE = re.compile(r"^(.*[^\s—–])\s*(?:[—–]+|\s-{1,2}\s)\s*(\S.*)$")
_LEADING_SEP_RE = re.compile(r"^(?:[—–]+|-{1,2}(?=\s))\s*(\S.*)$")
_LEADING_LINK_RE = re.compile(r"^\[([^\]\n]+)\]\(([^)\s]+)\)")
_KNOWN_STATUS = tuple(word for words in STATUS.values() for word in words.values())
_SOURCES_HEADING_RE = re.compile(
    r"^\s*#{1,6}\s*(?:" + "|".join(map(re.escape, SOURCES_HEADING.values())) + r"|资料来源与核验|Sources\s*&\s*Verification)\b",
    re.IGNORECASE,
)


def _status_word(candidate: str) -> str:
    candidate = candidate.strip().rstrip("。.;；,，")
    return candidate if any(word in candidate for word in _KNOWN_STATUS) else ""


def _split_status(text: str) -> tuple[str, str]:
    """(name, status) when ``text`` ends in a separator and a status word, else (text, "").

    Text that starts with the separator (what follows a linked name) is all
    status: ("", status).
    """
    text = text.strip()
    leading = _LEADING_SEP_RE.match(text)
    if leading:
        status = _status_word(leading.group(1))
        return ("", status) if status else (text, "")
    split = _STATUS_SPLIT_RE.match(text)
    if split:
        status = _status_word(split.group(2))
        if status:
            return split.group(1).strip().rstrip("，,"), status
    return text, ""


def strip_tracking(url: str) -> str:
    """``url`` without ``utm_*`` query parameters."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "utm_" not in parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def resolve_local_link(target: str) -> Optional[str]:
    """A served URL for a link to an upload or a chat artifact, else None."""
    m = _UPLOAD_RE.search(target)
    if m:
        chat_id, upload_id, name = m.groups()
        return f"/api/chats/{chat_id}/uploads/{upload_id}/{quote(name)}"
    m = _ARTIFACT_RE.search(target)
    if m:
        chat_id, rest = m.groups()
        parts = [part for part in rest.split("/") if part]
        # The route serves a chat's listed files by name. Working files — a
        # procedure's workspace, review renders — sit under "_" directories
        # and are never listed, so a link to one would be dead.
        if not parts or any(part.startswith(("_", ".")) for part in parts):
            return None
        return f"/api/artifacts/chat_artifacts/{chat_id}/{quote(parts[-1])}"
    return None


#: A link target that is neither a URL, a path nor an anchor: hosted web
#: search's internal citation ids leak through as `[source](turn2view0)`.
_OPAQUE_TARGET_RE = re.compile(r"^[A-Za-z]+\d+[A-Za-z0-9]*$")


def _is_local_path(target: str) -> bool:
    return target.startswith(("/", "file://", "./", "../", "~/")) or bool(
        re.match(r"^[A-Za-z]:[\\/]", target)
    )


@dataclass
class LintCounts:
    tracking_params: int = 0
    repeated_chips: int = 0
    upload_links_resolved: int = 0
    local_links_unlinked: int = 0
    token_leaks: int = 0
    sources_sections: int = 0
    sources_lines_normalized: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)

    def any(self) -> bool:
        return any(v for k, v in self.__dict__.items() if k != "sources_sections") or self.sources_sections > 1


@dataclass
class AnswerLinter:
    """Line-buffered filter: ``feed`` returns text safe to emit now."""

    language: str = "en"  # "zh" | "en"
    counts: LintCounts = field(default_factory=LintCounts)
    _buffer: str = ""
    _paragraph_chips: set = field(default_factory=set)
    _in_sources: bool = False

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        if "\n" not in self._buffer:
            return ""
        complete, self._buffer = self._buffer.rsplit("\n", 1)
        return "\n".join(self._line(line) for line in complete.split("\n")) + "\n"

    def flush(self) -> str:
        tail, self._buffer = self._buffer, ""
        return self._line(tail) if tail else ""

    def lint(self, text: str) -> str:
        """Whole-text form of the filter (for non-streamed answers)."""
        return self.feed(text) + self.flush()

    # --- per line ---------------------------------------------------------

    def _line(self, line: str) -> str:
        if not line.strip():
            self._paragraph_chips.clear()
            return line
        if _SOURCES_HEADING_RE.match(line):
            self.counts.sources_sections += 1
            self._in_sources = True
        elif _HEADING_RE.match(line):
            self._in_sources = False
        if not self._in_sources:
            # Each sources line is its own source: two of them citing one page
            # must both keep the link.
            line = self._collapse_chips(line)
        line = _MD_LINK_RE.sub(self._fix_link, line)
        line = _URL_RE.sub(self._fix_bare_url, line)
        if self._in_sources:
            line = self._source_line(line)
        if self.language == "zh" and STATUS["en"]["no_pinpoint"] in line:
            self.counts.token_leaks += line.count(STATUS["en"]["no_pinpoint"])
            line = line.replace(STATUS["en"]["no_pinpoint"], STATUS["zh"]["no_pinpoint"])
        return line

    def _source_line(self, line: str) -> str:
        """``N. [name](url) — status``, whatever order the link came in.

        Hosted web search attaches its citation as a chip, which lands after
        the status; a URL may arrive bare. The interface links the source name
        and reads the status after the last dash, so the link moves onto the
        name and the status goes last. A "——" with no spaces is a separator too.
        """
        m = _ITEM_RE.match(line)
        if not m:
            return line
        marker, body = m.groups()
        urls = [c.group(1) for c in _CHIP_RE.finditer(body)]
        body = _CHIP_RE.sub("", body)
        # A URL that is not already a link target.
        bare = [u for u in _URL_RE.finditer(body) if body[max(0, u.start() - 2) : u.start()] != "]("]
        urls += [u.group(0) for u in bare]
        for u in reversed(bare):
            body = body[: u.start()] + body[u.end() :]
        body = re.sub(r"[（(]\s*[)）]", "", body).strip()
        # A leading link is one unit: its label is the source's name, and a
        # status written inside it ("[name — status](url)") comes out of it.
        lead = _LEADING_LINK_RE.match(body)
        link_url = ""
        if lead:
            label, link_url = lead.group(1), lead.group(2)
            rest = body[lead.end() :].strip()
            if not rest.strip("。.;；,，"):
                name, status = _split_status(label)
            else:
                tail_name, status = _split_status(rest)
                name = f"{label} {tail_name}".strip() if tail_name else label
        else:
            name, status = _split_status(body)
        url = link_url or (urls[0] if urls else "")
        if url:
            name = f"[{name}]({url})"
        rebuilt = f"{marker}{name} — {status}" if status else f"{marker}{name}"
        if rebuilt != line:
            self.counts.sources_lines_normalized += 1
        return rebuilt

    def _collapse_chips(self, line: str) -> str:
        def repl(match: re.Match[str]) -> str:
            key = strip_tracking(match.group(1))
            if key in self._paragraph_chips:
                self.counts.repeated_chips += 1
                return ""
            self._paragraph_chips.add(key)
            return match.group(0)

        collapsed = _CHIP_RE.sub(repl, line)
        return re.sub(r"[ \t]{2,}", " ", collapsed) if collapsed != line else line

    def _fix_link(self, match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if target.startswith(("http://", "https://")):
            clean = strip_tracking(target)
            if clean != target:
                self.counts.tracking_params += 1
            return f"[{label}]({clean})"
        resolved = resolve_local_link(target)
        if resolved:
            self.counts.upload_links_resolved += 1
            return f"[{label}]({resolved})"
        if target.startswith(("#", "mailto:")):
            return match.group(0)
        if target.startswith("www."):
            return f"[{label}](https://{target})"
        if not target or _OPAQUE_TARGET_RE.match(target) or not _is_local_path(target):
            # No address at all: an empty target, a hosted-search citation id
            # ("turn2view0"), or a word ("用户提供"). The label is the citation.
            self.counts.local_links_unlinked += 1
            return label
        if _is_local_path(target):
            # A path no route serves is a dead link in the UI; the file name
            # still tells the reader which document is meant.
            self.counts.local_links_unlinked += 1
            return label
        return match.group(0)

    def _fix_bare_url(self, match: re.Match[str]) -> str:
        url = match.group(0)
        clean = strip_tracking(url)
        if clean != url:
            self.counts.tracking_params += 1
        return clean


def lint_answer(text: str, language: str = "en") -> tuple[str, LintCounts]:
    linter = AnswerLinter(language=language)
    return linter.lint(text), linter.counts


__all__ = ["AnswerLinter", "LintCounts", "lint_answer", "resolve_local_link", "strip_tracking"]
