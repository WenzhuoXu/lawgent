"""The shapes every research source speaks: a hit, a document, a source.

A source is one official (or authoritative free) database: the 国家法律法规数据库,
legislation.gov.uk, e-Gov 法令, CourtListener. It answers two questions —
*which documents match this query* (``search``) and *what does this document
say* (``fetch``) — and nothing else. Sections, paging, in-document keyword
and semantic search, caching and the tool surface are shared and live
elsewhere in this package, so a source stays a thin adapter over its API.

A document is addressed by a ``ref`` of the form ``<source id>:<native id>``
(``cn.flk:7ee5a369…``, ``uk.legislation:ukpga/1980/58``). A hit carries the
ref its document is fetched by; the model never has to build one.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

#: What a search can look for. ``any`` lets the source decide.
KINDS = ("legislation", "case", "any")

#: How a search matches. ``keyword`` is the source's own search over titles
#: or full text, whichever it offers; ``fulltext`` asks for full-text matching
#: where the source distinguishes it from title matching; ``semantic`` is a
#: natural-language query — native where the source has semantic search,
#: otherwise the source's keyword results re-ranked by meaning (bge reranker).
MODES = ("keyword", "fulltext", "semantic")


class SourceUnavailable(RuntimeError):
    """The source cannot answer right now (missing key, blocked, down).

    The message says why and what would fix it; the tool reports it instead
    of an empty result, so "nothing found" and "could not look" never read
    the same.
    """


@dataclass
class Hit:
    ref: str
    title: str
    source: str
    kind: str = "legislation"
    url: Optional[str] = None
    date: Optional[str] = None
    status: Optional[str] = None
    snippet: str = ""
    citation: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    score: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if v not in (None, "", {}, [])}
        if self.score is not None:
            d["score"] = round(self.score, 4)
        return d


@dataclass
class LegalDoc:
    """One document's full text plus what is known about it.

    ``text`` keeps line structure: a section heading starts a line, which is
    what the section index reads. ``sections`` may be supplied by the source
    when its format carries structure (XML with section elements); otherwise
    it is detected from the text.
    """

    ref: str
    title: str
    source: str
    text: str
    url: Optional[str] = None
    kind: str = "legislation"
    language: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Optional source-provided sections: ``[{"label", "heading", "start", "end"}]``
    #: offsets into ``text``.
    sections: Optional[list[dict[str, Any]]] = None
    #: Seconds the cached copy stays fresh; ``None`` = the store's default.
    ttl: Optional[float] = None


class Source:
    """Base class for one database.

    Subclasses set the class attributes and implement :meth:`search` and
    :meth:`fetch`. ``fetch`` receives the native id (the part of the ref
    after the colon).
    """

    id: str = ""
    name: str = ""
    jurisdictions: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ("legislation",)
    #: Matching the source itself offers (``keyword``, ``fulltext``, ``semantic``).
    #: ``semantic`` is always available through re-ranking; list it here only
    #: when the source has native semantic search.
    modes: tuple[str, ...] = ("keyword",)
    languages: tuple[str, ...] = ()
    #: Environment variables that must be set (a user-registered key).
    auth_env: tuple[str, ...] = ()
    #: How to obtain the key, shown when it is missing.
    auth_help: str = ""
    homepage: str = ""
    #: One or two lines the model reads in the catalogue: what is covered,
    #: what is not, how ids look.
    coverage: str = ""
    #: Whether the source answers "any"/"legislation"/"case" queries by
    #: default when no source is named (a secondary source sets False).
    default: bool = True

    # ------------------------------------------------------------------ api
    def search(
        self,
        query: str,
        *,
        kind: str = "any",
        mode: str = "keyword",
        limit: int = 10,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[Hit]:
        raise NotImplementedError

    def fetch(self, native_id: str, *, language: Optional[str] = None) -> LegalDoc:
        raise NotImplementedError

    # -------------------------------------------------------------- helpers
    def ref(self, native_id: str) -> str:
        return f"{self.id}:{native_id}"

    def missing_keys(self) -> list[str]:
        _load_keys()
        return [name for name in self.auth_env if not os.getenv(name)]

    def require_keys(self) -> None:
        missing = self.missing_keys()
        if missing:
            raise SourceUnavailable(
                f"{self.name} needs {', '.join(missing)} (not set). {self.auth_help}".strip()
            )

    def catalogue_entry(self) -> dict[str, Any]:
        missing = self.missing_keys()
        entry: dict[str, Any] = {
            "source": self.id,
            "name": self.name,
            "jurisdictions": list(self.jurisdictions),
            "kinds": list(self.kinds),
            "modes": list(self.modes),
            "languages": list(self.languages),
            "coverage": self.coverage,
        }
        if not self.default:
            entry["default"] = False
        if missing:
            entry["status"] = f"needs key {', '.join(missing)}"
            if self.auth_help:
                entry["how_to_enable"] = self.auth_help
        return entry


def serves(source: Source, kind: str) -> bool:
    return kind == "any" or kind in source.kinds


def _load_keys() -> None:
    """Mirror ``legal_helper/api_key`` / ``.env`` into the environment once.

    Sources are searched in parallel threads; the flag is set only after the
    keys are in the environment, under a lock, so a second thread never sees
    "loaded" while the first is still reading the file.
    """
    global _KEYS_LOADED
    if _KEYS_LOADED:
        return
    with _KEYS_LOCK:
        if _KEYS_LOADED:
            return
        try:
            from ..connectors.base import ensure_keys_loaded

            ensure_keys_loaded()
        except Exception:  # noqa: BLE001 — a missing key is reported per source
            pass
        _KEYS_LOADED = True


_KEYS_LOADED = False
_KEYS_LOCK = threading.Lock()


def dedupe_hits(hits: Iterable[Hit]) -> list[Hit]:
    seen: set[str] = set()
    out: list[Hit] = []
    for h in hits:
        if h.ref in seen:
            continue
        seen.add(h.ref)
        out.append(h)
    return out
