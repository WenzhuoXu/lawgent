r"""Legal-aware hierarchical chunker.

Preserves article / section / clause boundaries:
- 第\d+条 (PRC statute articles)
- Article \d+ / § \d+
- ##/### markdown headings
- Numbered legal clauses (1.1, 1.1.1)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_ARTICLE_RES = [
    re.compile(r"(?m)^\s*(第[一二三四五六七八九十百千零0-9]+条)"),
    re.compile(r"(?m)^\s*(Article\s+\d+)"),
    re.compile(r"(?m)^\s*(§\s*\d+(?:\.\d+)*)"),
    re.compile(r"(?m)^\s*(\d+(?:\.\d+){0,3})\s+[A-Z一-鿿]"),
    re.compile(r"(?m)^#{2,6}\s+(\S[^\n]*)"),
]
_CJK_RE = re.compile(
    "[㐀-䶿一-鿿豈-﫿　-〿＀-￯]"
)
_DEFAULT_CHUNK_TARGET = 384  # tokens (≈1700 chars EN / ≈384 chars ZH)
_DEFAULT_CHUNK_MAX = 600
_DEFAULT_OVERLAP = 64


@dataclass
class Chunk:
    text: str
    metadata: dict = field(default_factory=dict)


def _split_on_boundaries(text: str) -> list[tuple[str | None, str]]:
    """Split a document on the strongest available boundary set.

    Returns ``(label, section_text)`` pairs where ``label`` is the article
    marker that opens the section (第X条 / Article N / § N / heading) so
    pinpoint citations don't require re-parsing the chunk text downstream.
    """
    boundaries: dict[int, str] = {}
    for pat in _ARTICLE_RES:
        for m in pat.finditer(text):
            boundaries.setdefault(m.start(), m.group(1).strip())
    if not boundaries:
        return [(None, text)]
    starts = sorted(boundaries)
    if starts[0] != 0:
        starts = [0] + starts
    sections: list[tuple[str | None, str]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            sections.append((boundaries.get(start), chunk))
    return sections


def _est_tokens(text: str) -> int:
    """CJK-aware token estimate: a CJK char is ~1 token; Latin text ~4
    chars/token. The old flat ``len // 4`` made Chinese chunks ~4x
    oversized — the primary corpora are PRC statutes, so this matters."""
    cjk = len(_CJK_RE.findall(text))
    return max(1, cjk + (len(text) - cjk) // 4)


def _char_budget(text: str, tokens: int, from_end: bool = False) -> int:
    """Number of chars of ``text`` (from start, or end if ``from_end``)
    that fit in ``tokens`` estimated tokens."""
    rng = range(len(text) - 1, -1, -1) if from_end else range(len(text))
    used = 0.0
    count = 0
    for i in rng:
        used += 1.0 if _CJK_RE.match(text[i]) else 0.25
        if used > tokens:
            break
        count += 1
    return count


def _split_oversized(section: str, maximum: int) -> list[str]:
    """Character-split a section whose token estimate exceeds ``maximum``,
    honoring the CJK-aware estimator (not a flat ``maximum * 4`` chars)."""
    out: list[str] = []
    rest = section
    while rest:
        n = _char_budget(rest, maximum)
        if n <= 0:
            n = 1
        out.append(rest[:n])
        rest = rest[n:]
    return out


def _pack_sections(
    sections: list[tuple[str | None, str]],
    target: int,
    maximum: int,
    overlap: int,
) -> list[tuple[str, list[str]]]:
    """Combine small sections; split sections that exceed `maximum` tokens.

    Returns ``(chunk_text, article_labels)`` pairs; labels of every packed
    section are carried so a merged chunk keeps all its pinpoints."""
    out: list[tuple[str, list[str]]] = []
    buf: list[str] = []
    buf_labels: list[str] = []
    buf_tokens = 0

    def _flush() -> None:
        nonlocal buf, buf_labels, buf_tokens
        if buf:
            out.append(("\n\n".join(buf), list(buf_labels)))
        buf, buf_labels, buf_tokens = [], [], 0

    for label, section in sections:
        toks = _est_tokens(section)
        if toks > maximum:
            _flush()
            for piece in _split_oversized(section, maximum):
                out.append((piece, [label] if label else []))
            continue
        if buf_tokens + toks > target and buf:
            prev = buf[-1]
            prev_labels = list(buf_labels)
            _flush()
            tail = prev[len(prev) - _char_budget(prev, overlap, from_end=True):]
            if overlap and tail:
                buf = [tail]
                buf_labels = prev_labels[-1:]
                buf_tokens = _est_tokens(tail)
        buf.append(section)
        if label:
            buf_labels.append(label)
        buf_tokens += toks
    _flush()
    return out


def _doc_context(text: str, source_name: str | None = None) -> tuple[str | None, str]:
    """Local extractive document context (Summary-Augmented-Chunking-lite):
    title = first markdown heading or first non-empty line; summary = title
    plus the opening of the body. No LLM call — ingest stays offline."""
    title: str | None = None
    m = re.search(r"(?m)^#{1,6}\s+(\S[^\n]*)", text)
    if m:
        title = m.group(1).strip()
    else:
        for ln in text.splitlines():
            if ln.strip():
                title = ln.strip()[:120]
                break
    head = text.strip()[: _char_budget(text.strip(), 60)]
    parts = [p for p in (source_name, title) if p]
    context = " — ".join(dict.fromkeys(parts)) if parts else head
    return title, context


def chunk_document(
    text: str,
    metadata: dict | None = None,
    target_tokens: int = _DEFAULT_CHUNK_TARGET,
    max_tokens: int = _DEFAULT_CHUNK_MAX,
    overlap_tokens: int = _DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Chunk a document; metadata is propagated to every chunk.

    Each chunk additionally carries:
    - ``articles``: article labels (第X条 / Article N / § N / heading) found
      in the chunk, and ``article_label``: the first of them;
    - ``doc_title`` / ``doc_context``: document-level context the store
      prepends at embedding time (payload text stays the original chunk).
    """
    sections = _split_on_boundaries(text)
    packed = _pack_sections(sections, target_tokens, max_tokens, overlap_tokens)
    base_meta = dict(metadata or {})
    title, context = _doc_context(text, base_meta.get("source_name"))
    if title and "doc_title" not in base_meta:
        base_meta["doc_title"] = title
    if context and "doc_context" not in base_meta:
        base_meta["doc_context"] = context
    chunks: list[Chunk] = []
    for i, (piece, labels) in enumerate(packed):
        meta = {**base_meta, "chunk_index": i, "chunk_total": len(packed)}
        if labels:
            meta["articles"] = labels
            meta["article_label"] = labels[0]
        chunks.append(Chunk(text=piece, metadata=meta))
    return chunks


def chunk_file(path: Path, metadata: dict | None = None) -> list[Chunk]:
    from .loaders import load_any

    text = load_any(path)
    meta = {"source_path": str(path), "source_name": path.name}
    if metadata:
        meta.update(metadata)
    return chunk_document(text, meta)
