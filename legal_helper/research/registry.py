"""Every research source, by jurisdiction.

Source modules live in ``research/sources/``; each exports ``SOURCES``, a list
of :class:`~legal_helper.research.base.Source` instances. A module that fails
to import is reported in the catalogue rather than taking the others down.

Jurisdiction codes are ISO-3166 alpha-2 (``CN``, ``HK``, ``GB``…) with ``EU``
and ``INTL``, and ``XX-YY`` for a sub-national unit (``AU-VIC``, ``US-CA``,
``CA-BC``). A national query reaches its sub-national sources and a
sub-national query reaches the national ones, because both bodies of law
apply there.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import threading
from typing import Optional

from .base import Source, serves

#: Source modules that are live. A module in ``research/sources/`` is loaded
#: only once it is listed here — after it passes its live health probe — so
#: an adapter being written never reaches a running server.
ENABLED_MODULES = frozenset({"cn", "eu", "us", "et", "fr", "eu_curia", "nl", "us_extra", "jp", "kr", "tw", "mo", "qa", "gb", "au", "ca", "hk",
    "th", "vn", "my", "ph", "sg", "idn", "kh",
})


def _enabled_modules() -> set[str]:
    """``ENABLED_MODULES`` plus any named in ``LEGAL_HELPER_RESEARCH_MODULES``
    (comma-separated) — how an adapter is tested before it is listed."""
    extra = {m.strip() for m in os.getenv("LEGAL_HELPER_RESEARCH_MODULES", "").split(",") if m.strip()}
    return set(ENABLED_MODULES) | extra

_LOCK = threading.Lock()
_SOURCES: Optional[dict[str, Source]] = None
_IMPORT_ERRORS: dict[str, str] = {}

#: Names a model or a user may type, mapped to the code the sources use.
ALIASES = {
    "PRC": "CN", "CHINA": "CN", "中国": "CN", "内地": "CN", "大陆": "CN",
    "HONG KONG": "HK", "HKSAR": "HK", "香港": "HK",
    "MACAU": "MO", "MACAO": "MO", "澳门": "MO", "澳門": "MO",
    "TAIWAN": "TW", "台湾": "TW", "臺灣": "TW",
    "UK": "GB", "UNITED KINGDOM": "GB", "ENGLAND": "GB", "BRITAIN": "GB", "英国": "GB",
    "USA": "US", "UNITED STATES": "US", "美国": "US",
    "CALIFORNIA": "US-CA", "加州": "US-CA", "NEW YORK": "US-NY", "纽约": "US-NY",
    "EUROPEAN UNION": "EU", "欧盟": "EU",
    "AUSTRALIA": "AU", "澳大利亚": "AU", "VICTORIA": "AU-VIC", "维州": "AU-VIC",
    "NSW": "AU-NSW", "NEW SOUTH WALES": "AU-NSW", "新州": "AU-NSW",
    "CANADA": "CA", "加拿大": "CA", "BRITISH COLUMBIA": "CA-BC", "BC": "CA-BC",
    "JAPAN": "JP", "日本": "JP", "KOREA": "KR", "SOUTH KOREA": "KR", "韩国": "KR",
    "SINGAPORE": "SG", "新加坡": "SG", "MALAYSIA": "MY", "马来西亚": "MY",
    "INDONESIA": "ID", "印尼": "ID", "印度尼西亚": "ID", "PHILIPPINES": "PH", "菲律宾": "PH",
    "THAILAND": "TH", "泰国": "TH", "VIETNAM": "VN", "VIET NAM": "VN", "越南": "VN",
    "CAMBODIA": "KH", "柬埔寨": "KH", "QATAR": "QA", "卡塔尔": "QA",
    "FRANCE": "FR", "法国": "FR", "NETHERLANDS": "NL", "荷兰": "NL",
    "ETHIOPIA": "ET", "埃塞俄比亚": "ET",
    "INTERNATIONAL": "INTL", "国际": "INTL",
}


def normalize_jurisdiction(code: str) -> str:
    c = (code or "").strip()
    if not c:
        return ""
    up = c.upper()
    return ALIASES.get(up, ALIASES.get(c, up))


def _load() -> dict[str, Source]:
    global _SOURCES
    with _LOCK:
        if _SOURCES is not None:
            return _SOURCES
        from . import sources as pkg

        found: dict[str, Source] = {}
        for mod in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
            if mod.name.startswith("_") or mod.name not in _enabled_modules():
                continue
            try:
                module = importlib.import_module(f"{pkg.__name__}.{mod.name}")
            except Exception as exc:  # noqa: BLE001 — one broken adapter is reported, not fatal
                _IMPORT_ERRORS[mod.name] = f"{type(exc).__name__}: {exc}"
                continue
            for src in getattr(module, "SOURCES", []):
                found[src.id] = src
        _SOURCES = found
        return found


def all_sources() -> list[Source]:
    return list(_load().values())


def get_source(source_id: str) -> Optional[Source]:
    return _load().get((source_id or "").strip())


def import_errors() -> dict[str, str]:
    _load()
    return dict(_IMPORT_ERRORS)


def _matches(code: str, source: Source) -> bool:
    for j in source.jurisdictions:
        if j == code or j.startswith(code + "-") or code.startswith(j + "-"):
            return True
    return False


def sources_for(jurisdiction: str, kind: str = "any", *, include_secondary: bool = False) -> list[Source]:
    code = normalize_jurisdiction(jurisdiction)
    out = []
    for s in all_sources():
        if code and not _matches(code, s):
            continue
        if not serves(s, kind):
            continue
        if not s.default and not include_secondary:
            continue
        out.append(s)
    return out


def jurisdictions() -> list[str]:
    codes: set[str] = set()
    for s in all_sources():
        codes.update(s.jurisdictions)
    return sorted(codes)


def coverage_line() -> str:
    """'CN (北大法宝 PKULaw, 国家法律法规数据库); JP (e-Gov 法令, …); …' — what a
    prompt tells the model the research tools reach, from the live registry."""
    by: dict[str, list[str]] = {}
    for src in all_sources():  # registry order: a module lists its primary source first
        short = src.name.split(" (")[0].strip()
        for j in src.jurisdictions:
            names = by.setdefault(j, [])
            if short not in names:
                names.append(short)
    return "; ".join(f"{j} ({', '.join(names)})" for j, names in sorted(by.items()))

