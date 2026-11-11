"""The research tools: ``legal_sources``, ``legal_search``, ``legal_fetch``.

Three tools over every source in the registry, so a jurisdiction is a
parameter rather than a tool name to discover. Results are compact JSON; a
fetched document is cached, so reading it section by section costs one
download.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import Any, Optional

from anthropic import beta_tool

from ..context import estimate_tokens
from . import store
from .base import Hit, LegalDoc, SourceUnavailable, dedupe_hits
from .registry import (
    all_sources,
    get_source,
    import_errors,
    normalize_jurisdiction,
    sources_for,
)
from .sections import Section, detect_sections, find_section, outline
from .semantic import keyword_rank, semantic_rank

_SEARCH_WORKERS = 6
_PASSAGE_CHARS = 2400
_WINDOW_CHARS = 1500


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------- catalogue

@beta_tool
def legal_sources(jurisdiction: str = "") -> str:
    """List the legal databases legal_search and legal_fetch can reach: per source its
    jurisdictions, document kinds (legislation / case), search modes, languages, what it
    covers and whether it needs a key.

    Args:
        jurisdiction: ISO code or name (CN, JP, EU, US-CA…). Empty = every source.
    """
    code = normalize_jurisdiction(jurisdiction)
    srcs = sources_for(code, include_secondary=True) if code else all_sources()
    entries = [s.catalogue_entry() for s in sorted(srcs, key=lambda s: (s.jurisdictions, s.id))]
    payload: dict[str, Any] = {"jurisdiction": code or "all", "count": len(entries), "sources": entries}
    errors = import_errors()
    if errors:
        payload["adapters_failed_to_load"] = errors
    if code and not entries:
        payload["note"] = (
            f"No source for {code}. Use WebSearch / WebFetch on the official gazette or "
            "legislation site, and say the text was read from the web."
        )
    return _json(payload)


# ------------------------------------------------------------------ search

def _run_search(src, query: str, kind: str, mode: str, limit: int, filters: dict[str, Any]) -> list[Hit]:
    src_mode = mode
    if mode == "semantic" and "semantic" not in src.modes:
        src_mode = "fulltext" if "fulltext" in src.modes else "keyword"
    if mode == "fulltext" and "fulltext" not in src.modes:
        src_mode = "keyword"
    src.require_keys()
    return src.search(query, kind=kind, mode=src_mode, limit=limit, filters=filters)


def _interleave(groups: list[list[Hit]], limit: int) -> list[Hit]:
    out: list[Hit] = []
    i = 0
    while len(out) < limit and any(i < len(g) for g in groups):
        for g in groups:
            if i < len(g):
                out.append(g[i])
                if len(out) >= limit:
                    break
        i += 1
    return out


@beta_tool
def legal_search(
    jurisdiction: str,
    query: str,
    kind: str = "any",
    mode: str = "keyword",
    source: str = "",
    limit: int = 10,
    filters: Optional[dict[str, Any]] = None,
) -> str:
    """Search official legal databases of one jurisdiction for legislation or cases.
    Each hit has a `ref`; read it with legal_fetch. legal_sources lists what each
    jurisdiction has.

    Args:
        jurisdiction: ISO code or name (CN, JP, KR, TW, HK, MO, EU, FR, GB, US, US-CA, AU…);
            a country reaches its states/provinces too. legal_sources lists what is installed.
        query: Title, citation, number, keywords or (mode=semantic) a question in plain words.
            Search in the source's language where possible (中文 for CN/TW/HK, 日本語 for JP…).
        kind: legislation | case | any.
        mode: keyword (titles / the source's default) | fulltext (match inside the text
            where the source supports it) | semantic (meaning: native where the source has
            it, otherwise its full-text results re-ranked by a local multilingual model).
        source: One source id from legal_sources to search only that database.
        limit: Hits to return, 1-30.
        filters: Source-specific filters, e.g. {"status": "in_force"}, {"court": "ca9"},
            {"year": 2024}, {"level": "law"}; unknown keys are ignored.
    """
    kind = kind if kind in ("legislation", "case", "any") else "any"
    mode = mode if mode in ("keyword", "fulltext", "semantic") else "keyword"
    limit = max(1, min(int(limit or 10), 30))
    filters = dict(filters or {})
    code = normalize_jurisdiction(jurisdiction)
    if source:
        src = get_source(source)
        if src is None:
            return _json({"error": f"unknown source {source!r}", "hint": "call legal_sources"})
        srcs = [src]
    else:
        srcs = sources_for(code, kind)
    if not srcs:
        return _json({
            "jurisdiction": code, "query": query, "results": [],
            "note": f"No {kind} source for {code or 'that jurisdiction'}; call legal_sources, "
                    "or use WebSearch / WebFetch on the official site.",
        })

    per_source = limit if len(srcs) == 1 else max(4, limit)
    groups: dict[str, list[Hit]] = {}
    unavailable: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(_SEARCH_WORKERS, len(srcs))) as pool:
        futures = {
            pool.submit(copy_context().run, _run_search, s, query, kind, mode, per_source, filters): s
            for s in srcs
        }
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                groups[s.id] = dedupe_hits(fut.result())
            except SourceUnavailable as exc:
                unavailable.append({"source": s.id, "reason": str(exc)})
            except Exception as exc:  # noqa: BLE001 — one failing source never hides the others
                unavailable.append({"source": s.id, "reason": f"{type(exc).__name__}: {str(exc)[:300]}"})

    ordered = [groups[s.id] for s in srcs if s.id in groups]
    ranking = None
    if mode == "semantic":
        pool_hits = dedupe_hits(h for g in ordered for h in g)
        texts = [f"{h.title}\n{h.snippet}" for h in pool_hits]
        ranked, ranking = semantic_rank(query, texts, limit)
        hits = []
        for i, score in ranked:
            h = pool_hits[i]
            h.score = score
            hits.append(h)
    else:
        hits = _interleave(ordered, limit)

    payload: dict[str, Any] = {
        "jurisdiction": code or None,
        "query": query,
        "kind": kind,
        "mode": mode,
        "searched": [s.id for s in srcs if s.id in groups],
        "count": len(hits),
        "results": [h.to_dict() for h in hits],
    }
    if ranking:
        payload["ranking"] = "local multilingual reranker" if ranking == "rerank" else "keyword (reranker unavailable)"
    if unavailable:
        payload["unavailable"] = unavailable
    if not hits:
        payload["next_step"] = (
            "Nothing matched. Try the source's language, a shorter title, mode=fulltext, "
            "or another kind; then the official site via WebSearch."
        )
    return _json(payload)


# ------------------------------------------------------------------- fetch

def _load_doc(ref: str, language: Optional[str]) -> LegalDoc:
    source_id, sep, native = ref.partition(":")
    if not sep or not native:
        raise ValueError(f"ref must look like '<source>:<id>' (got {ref!r}); take it from a legal_search hit")
    src = get_source(source_id)
    if src is None:
        raise ValueError(f"unknown source {source_id!r}; call legal_sources")
    cached = store.get(ref, language=language)
    if cached is not None:
        return cached
    src.require_keys()
    doc = src.fetch(native, language=language or None)
    store.put(doc, language=language)
    return doc


def _section_ref(ref: str, section: str) -> str:
    """A source that serves provisions one by one (PKULaw) reads `section` itself."""
    source_id, sep, native = ref.partition(":")
    hook = getattr(get_source(source_id), "section_ref", None) if sep and section.strip() else None
    alt = hook(native, section.strip()) if hook else None
    return f"{source_id}:{alt}" if alt else ref


def _sections_of(doc: LegalDoc) -> list[Section]:
    if doc.sections:
        out = []
        for s in doc.sections:
            label = s.get("label", "")
            heading = s.get("heading") or label
            caption = s.get("caption") or ""
            if "caption" not in s and heading != label:
                # "第九十七条（飛行計画及びその承認）" → the caption after the label.
                caption = heading[len(label):] if heading.startswith(label) else heading
                caption = caption.strip(" \u3000.—–-:")
            out.append(Section(
                label=label, key=s.get("key") or label, heading=heading,
                start=int(s["start"]), end=int(s["end"]), level=s.get("level", "article"),
                caption=caption if s.get("level", "article") != "container" else "",
            ))
        return out
    return detect_sections(doc.text, kind=doc.kind, bare_numbers=bool(doc.metadata.get("bare_section_numbers")))


def _cut(text: str, max_tokens: int) -> tuple[str, bool]:
    if estimate_tokens(text) <= max_tokens:
        return text, False
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text.rfind("\n", 0, lo)
    return text[: cut if cut > lo * 0.7 else lo], True


def _windows(text: str) -> list[tuple[int, int]]:
    """Fixed windows for a text with no detected sections."""
    out = []
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + _WINDOW_CHARS)
        if end < len(text):
            nl = text.rfind("\n", pos + _WINDOW_CHARS // 2, end)
            end = nl if nl > 0 else end
        out.append((pos, end))
        pos = end
    return out


_BRIEF_META = ("status", "effective", "published", "date", "version", "court", "citation", "in_force", "language_note")


def _meta(doc: LegalDoc, *, full: bool) -> dict[str, Any]:
    """Document identity; the whole metadata only on the overview call."""
    d: dict[str, Any] = {"ref": doc.ref, "title": doc.title, "source": doc.source, "kind": doc.kind}
    if doc.url:
        d["url"] = doc.url
    if doc.language:
        d["language"] = doc.language
    if doc.metadata:
        if full:
            meta = dict(doc.metadata)
            for k, v in list(meta.items()):
                if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                    meta[k] = list(dict.fromkeys(v))
            d["metadata"] = meta
        else:
            brief = {k: doc.metadata[k] for k in _BRIEF_META if doc.metadata.get(k)}
            if brief:
                d["metadata"] = brief
    return d


def _resolve_sections(secs: list[Section], request: str) -> tuple[list[Section], list[str]]:
    """'9' / '9-12' / '9,11' / 'Article 9(1)' → the sections, plus requests not found."""
    picked: list[Section] = []
    missing: list[str] = []
    parts = [p.strip() for p in re.split(r"[,，、;；]", request) if p.strip()]
    for part in parts:
        rng = re.match(r"^(.+?)\s*(?:-|–|—|～|~|至|到)\s*(.+)$", part)
        # "Article 71-2", "第99-1條", "L6421-4" name one provision: try the whole
        # label before reading a hyphen as a range — unless the right side is a
        # label of its own ("第13条-第17条", "Article 9 - Article 12"), which the
        # whole-label lookup would read as its first provision alone.
        whole = [] if rng and re.match(r"^\D", rng.group(2)) else find_section(secs, part)
        if whole:
            picked.append(whole[0])
            continue
        if rng and find_section(secs, rng.group(1)) and find_section(secs, rng.group(2)):
            a = find_section(secs, rng.group(1))[0]
            b = find_section(secs, rng.group(2))[0]
            i, j = secs.index(a), secs.index(b)
            if i > j:
                i, j = j, i
            picked.extend(s for s in secs[i : j + 1] if s.level != "container")
            continue
        found = find_section(secs, part)
        if found:
            picked.append(found[0])
        else:
            missing.append(part)
    seen: set[int] = set()
    out = []
    for s in picked:
        if s.start not in seen:
            seen.add(s.start)
            out.append(s)
    return out, missing


@beta_tool
def legal_fetch(
    ref: str,
    section: str = "",
    query: str = "",
    mode: str = "auto",
    offset: int = 0,
    limit: int = 5,
    max_tokens: int = 6000,
    language: str = "",
) -> str:
    """Read a document found by legal_search (official full text). With no section or
    query: metadata, an outline and the text from `offset`. With `section`: that
    provision, a range or a list, by its own label ("第128条", "128-131", "Article 9",
    "s 58", "제3조", "Điều 5", "[45]" for a judgment paragraph). With `query`: the
    passages that match, by keyword and/or meaning.

    Args:
        ref: The `ref` of a legal_search hit, e.g. "cn.flk:7ee5a369…", "eu.eurlex:32004R0261".
        section: Provision label(s); sub-units (款, (1)(a)) return the whole provision.
        query: Find passages inside the document (any language; semantic matching is
            multilingual).
        mode: For query: keyword | semantic | auto (keyword hits first, then by meaning).
        offset: Character offset to read from (no section/query); use next_offset to go on.
        limit: Passages for a query, 1-15.
        max_tokens: Most text to return in this call (1000-20000).
        language: Language version where the source has several (e.g. en, zh, fr, ja).
    """
    ref = _section_ref(ref.strip(), section)
    try:
        doc = _load_doc(ref, language.strip() or None)
    except SourceUnavailable as exc:
        return _json({"ref": ref, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 — the model reads the error and recovers
        return _json({"ref": ref, "error": f"{type(exc).__name__}: {str(exc)[:500]}"})

    max_tokens = max(1000, min(int(max_tokens or 6000), 20000))
    text = doc.text or ""
    if not text.strip():
        out = _meta(doc, full=True)
        out["error"] = doc.metadata.get("no_text_reason") or "the source returned no text for this document"
        return _json(out)
    secs = _sections_of(doc)
    overview = not section.strip() and not query.strip() and not int(offset or 0)
    payload = _meta(doc, full=overview)
    payload["total_chars"] = len(text)
    payload["provisions"] = sum(1 for s in secs if s.level in ("article", "para"))

    if section.strip():
        picked, missing = _resolve_sections(secs, section)
        if not picked:
            payload["error"] = f"no provision labelled {section!r} in this document"
            payload["outline"] = outline(secs, text, limit=80)
            payload["hint"] = "Pick a label from the outline, or pass query= to search inside the document."
            return _json(payload)
        chunks = []
        used = 0
        truncated = False
        for s in picked:
            body = text[s.start:s.end].strip()
            cost = estimate_tokens(body)
            if used + cost > max_tokens:
                body, _ = _cut(body, max(200, max_tokens - used))
                truncated = True
            chunks.append({"label": s.label, "offset": s.start, "text": body})
            used += estimate_tokens(body)
            if used >= max_tokens:
                truncated = truncated or s is not picked[-1]
                break
        payload["sections"] = chunks
        if missing:
            payload["not_found"] = missing
        if truncated:
            payload["truncated"] = "max_tokens reached; ask for fewer provisions or raise max_tokens"
        return _json(payload)

    if query.strip():
        limit = max(1, min(int(limit or 5), 15))
        units = [s for s in secs if s.level != "container"]
        spans = [(s.start, s.end, s.label) for s in units] or [(a, b, "") for a, b in _windows(text)]
        passages = [text[a:b] for a, b, _ in spans]
        mode = mode if mode in ("keyword", "semantic", "auto") else "auto"
        ranked: list[tuple[int, float, str]] = []
        method = mode
        if mode in ("keyword", "auto"):
            ranked = [(i, sc, "keyword") for i, sc in keyword_rank(query, passages, limit)]
        if mode == "semantic" or (mode == "auto" and len(ranked) < limit):
            sem, how = semantic_rank(query, passages, limit)
            have = {i for i, _, _ in ranked}
            ranked += [(i, sc, how) for i, sc in sem if i not in have]
            method = "keyword+semantic" if mode == "auto" else how
        matches = []
        used = 0
        for i, score, how in ranked[:limit]:
            a, b, label = spans[i]
            body = text[a:b].strip()
            if len(body) > _PASSAGE_CHARS:
                body = body[:_PASSAGE_CHARS] + "…"
            used += estimate_tokens(body)
            matches.append({"label": label or None, "offset": a, "match": how, "score": round(score, 3), "text": body})
            if used >= max_tokens:
                break
        payload["query"] = query
        payload["method"] = method
        payload["matches"] = matches
        if not matches:
            payload["note"] = "No passage matched; try other words, the document's language, or mode=semantic."
        return _json(payload)

    offset = max(0, min(int(offset or 0), len(text)))
    if offset == 0:
        payload["outline"] = outline(secs, text)
        # the outline is part of this call's budget
        max_tokens = max(1000, max_tokens - estimate_tokens("\n".join(payload["outline"])))
    body, cut = _cut(text[offset:], max_tokens)
    payload["offset"] = offset
    payload["text"] = body
    if cut:
        payload["next_offset"] = offset + len(body)
        payload["hint"] = "Read on with offset=next_offset, or jump with section= / query=."
    return _json(payload)


RESEARCH_TOOLS = (legal_sources, legal_search, legal_fetch)
