"""Ethiopia: the federal-law source (et.negarit) and the aviation pack's ECAA tools."""

from __future__ import annotations

import json

import pytest

from legal_helper.connectors import REGISTRY, ethiopia, filter_for
from legal_helper.research.sources import _ethiopia_catalog as catalog
from legal_helper.research.sources import et


def test_normalize_proclamation_accepts_many_forms():
    n = catalog._normalize_proclamation
    assert n("616/2008") == (616, 2008, "pr_616_2008")
    assert n("616-2008") == (616, 2008, "pr_616_2008")
    assert n("pr_616_2008") == (616, 2008, "pr_616_2008")
    assert n("Proclamation No. 1179/2020") == (1179, 2020, "pr_1179_2020")
    assert n("273 of 2002") == (273, 2002, "pr_273_2002")
    assert n("not a law") is None
    assert n("616/1800") is None  # implausible year rejected


def test_search_ranks_aviation_and_numbers():
    src = et.EthiopiaSource()
    hits = src.search("civil aviation")
    assert hits and hits[0].ref == "et.negarit:616/2008"
    assert src.search("1179/2020")[0].ref == "et.negarit:1179/2020"
    # A number outside the catalogue is still offered, fetchable by number.
    assert src.search("1051/2017")[0].ref == "et.negarit:1051/2017"
    assert src.search("zzz nonsense") == []


def test_extract_transcript_detects_shell_vs_body():
    shell = "<html><body>Ethiopian Federal Law: Proclamations and Regulations MetaAppz Home Tools</body></html>"
    assert catalog._extract_transcript(shell) == ""
    real = (
        "<html><body>... Text (English) A PROCLAMATION TO PROVIDE FOR X. "
        "WHEREAS it has become necessary; it is hereby proclaimed: 1. Short Title "
        + ("aviation safety oversight " * 40)
        + "</body></html>"
    )
    out = catalog._extract_transcript(real)
    assert out and "WHEREAS" in out
    assert "MetaAppz" not in out  # chrome before 'Text (English)' is stripped


class _Resp:
    def __init__(self, text="", content=b"", ctype="text/html"):
        self.text = text
        self.content = content
        self.headers = {"content-type": ctype}


def test_fetch_prefers_transcript_then_reads_the_gazette_pdf(monkeypatch):
    body = "Text (English) A PROCLAMATION ... WHEREAS hereby Short Title " + ("air operator certificate " * 40)
    monkeypatch.setattr(et, "request_with_retry", lambda m, url, **k: _Resp(text=f"<html>{body}</html>"))
    doc = et.EthiopiaSource().fetch("3/1995")
    assert doc.metadata["read_from"] == "typed transcript" and "WHEREAS" in doc.text

    # No transcript: the gazette PDF goes through the shared reader (OCR for scans).
    def routed(m, url, **k):
        if url.endswith("/en/txt"):
            return _Resp(text="<html>Ethiopian Federal Law: Proclamations and Regulations</html>")
        return _Resp(content=b"%PDF-1.4 fake", ctype="application/pdf")

    monkeypatch.setattr(et, "request_with_retry", routed)
    monkeypatch.setattr(et, "bytes_to_text", lambda data, ctype, name_hint="": "1. Short Title\n2. Definitions\nበአማርኛ")
    doc = et.EthiopiaSource().fetch("616/2008")
    assert doc.metadata["read_from"].startswith("gazette PDF")
    assert "Definitions" in doc.text and "በአማርኛ" not in doc.text  # Ge'ez-only lines dropped


def test_fetch_rejects_unparseable():
    with pytest.raises(ValueError):
        et.EthiopiaSource().fetch("not a law")


def test_ecaa_index_returns_framework():
    payload = json.loads(ethiopia.ecaa_index.call({}))
    assert payload["regulator"]["name"].startswith("Ethiopian Civil Aviation Authority")
    instruments = " ".join(r["instrument"] for r in payload["results"])
    assert "616/2008" in instruments
    assert "Chicago" in instruments


def test_ecaa_connectors_registered_with_et_jurisdiction():
    names = {e.name: e for e in REGISTRY}
    assert {"ecaa_index", "ecaa_fetch"} <= names.keys()
    # ECAA connectors are aviation-pack gated, ET/ICAO scoped
    assert names["ecaa_index"].domain_packs == ("aviation",)
    assert "ET" in names["ecaa_index"].jurisdiction


def test_ecaa_visible_only_under_aviation_pack():
    # ET in jurisdictions but no pack -> ecaa hidden
    no_pack = {e.name for e in filter_for(["ET"], [])}
    assert "ecaa_index" not in no_pack
    # aviation pack active -> ecaa visible
    with_pack = {e.name for e in filter_for(["ET"], ["aviation"])}
    assert "ecaa_index" in with_pack


def test_active_pack_widens_jurisdiction_filter_for_ecaa():
    from legal_helper.tools import _active_pack_jurisdictions

    assert "ET" in _active_pack_jurisdictions(["aviation"])
