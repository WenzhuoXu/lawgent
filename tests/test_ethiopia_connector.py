from __future__ import annotations

import json

from legal_helper.connectors import REGISTRY, ethiopia, filter_for


def test_normalize_proclamation_accepts_many_forms():
    n = ethiopia._normalize_proclamation
    assert n("616/2008") == (616, 2008, "pr_616_2008")
    assert n("616-2008") == (616, 2008, "pr_616_2008")
    assert n("pr_616_2008") == (616, 2008, "pr_616_2008")
    assert n("Proclamation No. 1179/2020") == (1179, 2020, "pr_1179_2020")
    assert n("273 of 2002") == (273, 2002, "pr_273_2002")
    assert n("not a law") is None
    assert n("616/1800") is None  # implausible year rejected


def test_ethiopia_law_search_ranks_aviation_and_numbers():
    payload = json.loads(ethiopia.ethiopia_law_search.call({"query": "civil aviation"}))
    assert payload["count"] >= 1
    assert "616/2008" in payload["results"][0]["proclamation"]

    by_number = json.loads(ethiopia.ethiopia_law_search.call({"query": "1179/2020"}))
    assert by_number["results"][0]["proclamation"] == "1179/2020"

    # an unmatched query no longer misdirects to the aviation core — it
    # returns zero hits and routes the caller to web_search instead.
    empty = json.loads(ethiopia.ethiopia_law_search.call({"query": "zzz nonsense"}))
    assert empty["count"] == 0
    assert empty["results"] == []
    assert empty["next_step"] == "web_search"


def test_extract_transcript_detects_shell_vs_body():
    shell = "<html><body>Ethiopian Federal Law: Proclamations and Regulations MetaAppz Home Tools</body></html>"
    assert ethiopia._extract_transcript(shell) == ""
    real = (
        "<html><body>... Text (English) A PROCLAMATION TO PROVIDE FOR X. "
        "WHEREAS it has become necessary; it is hereby proclaimed: 1. Short Title "
        + ("aviation safety oversight " * 40)
        + "</body></html>"
    )
    out = ethiopia._extract_transcript(real)
    assert out and "WHEREAS" in out
    assert "MetaAppz" not in out  # chrome before 'Text (English)' is stripped


def test_ethiopia_law_fetch_prefers_transcript_then_pdf(monkeypatch):
    # Case 1: typed transcript available
    body = "Text (English) A PROCLAMATION ... WHEREAS hereby Short Title " + ("x " * 300)
    monkeypatch.setattr(ethiopia, "http_get_text", lambda url, *a, **k: f"<html>{body}</html>")
    payload = json.loads(ethiopia.ethiopia_law_fetch.call({"proclamation": "3/1995"}))
    assert payload["body_extractor"] == "html-transcript"
    assert "WHEREAS" in payload["body_text"]
    assert payload["url"].endswith("/pr_3_1995/en/txt")

    # Case 2: no transcript (shell) -> PDF fallback via fetch_body_text.
    # The body must read as machine-readable English law prose to be returned.
    pdf_body = (
        "A PROCLAMATION TO PROVIDE FOR CIVIL AVIATION. WHEREAS it has become "
        "necessary; it is hereby proclaimed: 1. Short Title " + ("aviation safety " * 30)
    )
    monkeypatch.setattr(
        ethiopia, "http_get_text",
        lambda url, *a, **k: "<html>Ethiopian Federal Law: Proclamations and Regulations</html>",
    )
    monkeypatch.setattr(
        ethiopia, "fetch_body_text",
        lambda url, **k: {"body_text": pdf_body, "extractor": "pdf"},
    )
    payload = json.loads(ethiopia.ethiopia_law_fetch.call({"proclamation": "616/2008"}))
    assert payload["body_extractor"] == "pdf"
    assert "CIVIL AVIATION" in payload["body_text"]
    assert payload["url"].endswith("/2008/pr_616_2008.pdf")

    # Case 3: PDF extraction yields Ge'ez/(cid:NN) mojibake -> redirect to
    # web_search with an empty body instead of dumping unusable text.
    monkeypatch.setattr(
        ethiopia, "fetch_body_text",
        lambda url, **k: {"body_text": "(cid:12)(cid:34) ጠቅላይ ሚኒስትር (cid:56)", "extractor": "pdf"},
    )
    payload = json.loads(ethiopia.ethiopia_law_fetch.call({"proclamation": "616/2008"}))
    assert payload["body_text"] == ""
    assert payload["next_step"] == "web_search"
    assert "body_extractor" not in payload


def test_ethiopia_law_fetch_rejects_unparseable():
    payload = json.loads(ethiopia.ethiopia_law_fetch.call({"proclamation": "not a law"}))
    assert "error" in payload


def test_ecaa_index_returns_framework():
    payload = json.loads(ethiopia.ecaa_index.call({}))
    assert payload["regulator"]["name"].startswith("Ethiopian Civil Aviation Authority")
    instruments = " ".join(r["instrument"] for r in payload["results"])
    assert "616/2008" in instruments
    assert "Chicago" in instruments


def test_ethiopia_connectors_registered_with_et_jurisdiction():
    names = {e.name: e for e in REGISTRY}
    assert {"ethiopia_law_search", "ethiopia_law_fetch", "ecaa_index", "ecaa_fetch"} <= names.keys()
    # general ET law connectors are not pack-gated
    assert names["ethiopia_law_search"].jurisdiction == ("ET",)
    assert names["ethiopia_law_search"].domain_packs == ()
    # ECAA connectors are aviation-pack gated, ET/ICAO scoped
    assert names["ecaa_index"].domain_packs == ("aviation",)
    assert "ET" in names["ecaa_index"].jurisdiction


def test_ecaa_visible_only_under_aviation_pack():
    # ET in jurisdictions but no pack -> ecaa hidden, general ET law visible
    no_pack = {e.name for e in filter_for(["ET"], [])}
    assert "ethiopia_law_search" in no_pack
    assert "ecaa_index" not in no_pack
    # aviation pack active -> ecaa visible
    with_pack = {e.name for e in filter_for(["ET"], ["aviation"])}
    assert "ecaa_index" in with_pack


def test_active_pack_widens_jurisdiction_filter_for_ecaa():
    from legal_helper.tools import _active_pack_jurisdictions

    assert "ET" in _active_pack_jurisdictions(["aviation"])
