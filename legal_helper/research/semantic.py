"""Keyword and semantic ranking over passages (sections, search hits).

Semantic ranking uses the local models the corpus search already uses: the
bge-m3 embedder narrows a large pool, the bge-reranker-v2-m3 cross-encoder
orders what is left. Both are multilingual, so a Chinese question ranks an
English statute's sections and the other way round. The models load per call
and are released after (``rag.retrieve.models_held``). When they cannot load,
ranking falls back to keyword scores and says so.
"""

from __future__ import annotations

import math
import re
import sys
from typing import Sequence

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯฀-๿]")
_SPLIT = re.compile(r"[\s,，;；、。.:：!?！？()（）\[\]【】\"“”'‘’《》<>/|]+")

#: Passages longer than this are cut before scoring (the cross-encoder reads
#: 512 tokens anyway).
PASSAGE_CHARS = 1600
#: Above this many passages the embedder pre-selects the pool for the reranker.
RERANK_POOL = 48


def query_terms(query: str) -> list[str]:
    """Split a query into match terms. Quoted phrases stay whole."""
    phrases = re.findall(r'"([^"]+)"|“([^”]+)”', query)
    terms = [a or b for a, b in phrases]
    rest = re.sub(r'"[^"]+"|“[^”]+”', " ", query)
    for tok in _SPLIT.split(rest):
        tok = tok.strip()
        if not tok:
            continue
        if _CJK.search(tok) and len(tok) > 4 and " " not in tok:
            # A long CJK run has no word boundaries; match it as overlapping
            # 2-character pieces so 旅客随身携带物品 still finds 随身携带的物品.
            terms.extend(tok[i:i + 2] for i in range(0, len(tok) - 1))
        else:
            terms.append(tok)
    seen: set[str] = set()
    out = []
    for t in terms:
        k = t.lower()
        if k not in seen and len(k) > 0:
            seen.add(k)
            out.append(t)
    return out


def keyword_scores(query: str, passages: Sequence[str]) -> list[float]:
    """TF-IDF-like score: rare terms count more; every term found adds."""
    terms = [t.lower() for t in query_terms(query)]
    if not terms:
        return [0.0] * len(passages)
    lowered = [p.lower() for p in passages]
    n = len(passages)
    df = {t: sum(1 for p in lowered if t in p) for t in terms}
    scores = []
    for p in lowered:
        s = 0.0
        hit_terms = 0
        for t in terms:
            c = p.count(t)
            if c:
                hit_terms += 1
                idf = math.log(1 + n / (1 + df[t]))
                s += (1 + math.log(c)) * idf
        coverage = hit_terms / len(terms)
        scores.append(s * (0.5 + coverage))
    return scores


def keyword_rank(query: str, passages: Sequence[str], k: int) -> list[tuple[int, float]]:
    scores = keyword_scores(query, passages)
    order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: scores[i], reverse=True)
    return [(i, scores[i]) for i in order[:k]]


def semantic_rank(query: str, passages: Sequence[str], k: int) -> tuple[list[tuple[int, float]], str]:
    """``([(index, score)], method)`` — method is ``rerank`` or ``keyword`` (fallback)."""
    if not passages:
        return [], "rerank"
    texts = [p[:PASSAGE_CHARS] for p in passages]
    try:
        from ..rag.rerank import get_reranker
        from ..rag.retrieve import models_held

        with models_held():
            pool = list(range(len(texts)))
            if len(texts) > RERANK_POOL:
                pool = _embed_prefilter(query, texts, RERANK_POOL)
            scores = get_reranker().score(query, [texts[i] for i in pool])
        ranked = sorted(zip(pool, scores), key=lambda x: x[1], reverse=True)
        return [(i, float(s)) for i, s in ranked[:k]], "rerank"
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the tool
        print(f"research.semantic: falling back to keyword ranking ({exc!s})", file=sys.stderr)
        return keyword_rank(query, passages, k), "keyword"


def _embed_prefilter(query: str, texts: Sequence[str], keep: int) -> list[int]:
    from ..rag.embeddings import default_embedder

    emb = default_embedder()
    vecs = emb.encode([query] + list(texts))
    q = vecs[0]
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    sims = []
    for i, v in enumerate(vecs[1:]):
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        sims.append((sum(a * b for a, b in zip(q, v)) / (qn * vn), i))
    # Keep the best by meaning, plus every passage with a literal term match
    # (a statute's exact phrase must not be lost to the embedder).
    best = [i for _, i in sorted(sims, reverse=True)[:keep]]
    literal = [i for i, _ in keyword_rank(query, texts, keep // 3)]
    out = list(dict.fromkeys(literal + best))
    return out[: keep + len(literal)]
