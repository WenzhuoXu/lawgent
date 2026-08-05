"""Structural verification for the new legal-search tools.

Confirms — without calling an LLM — that:
1. Each tool is a properly-decorated BetaFunctionTool with a JSON schema.
2. Each tool returns parseable JSON when called against the live free API.
3. The tool registry exposes them to the right skills.
4. The Anthropic and OpenAI provider tool-shaping paths both accept them.
"""

from __future__ import annotations

import json
import sys


def _section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def main() -> int:
    failures: list[str] = []

    # Trigger api_key file → os.environ propagation before tools read keys.
    from legal_helper.config import load_settings

    load_settings(refresh=True)
    import os

    print(
        f"  env GOVINFO_API_KEY set: {bool(os.environ.get('GOVINFO_API_KEY'))}, "
        f"env COURTLISTENER_API_TOKEN set: {bool(os.environ.get('COURTLISTENER_API_TOKEN'))}"
    )

    from legal_helper.tools import (
        courtlistener_search,
        ecfr_search,
        federal_register_search,
        govinfo_search,
        skill_tools,
    )

    _section("1. BetaFunctionTool shape")
    for t in (ecfr_search, federal_register_search, govinfo_search, courtlistener_search):
        name = getattr(t, "name", None)
        schema = getattr(t, "input_schema", None)
        has_call = hasattr(t, "call")
        props = list((schema or {}).get("properties", {}).keys())
        print(f"  {name}: schema_props={props} has_call={has_call}")
        if not (name and schema and has_call):
            failures.append(f"{name}: missing tool attributes")

    _section("2. Live call returns valid JSON")
    cases = [
        ("ecfr_search", ecfr_search, {"query": "airworthiness directive", "title": 14, "per_page": 2}),
        ("federal_register_search", federal_register_search, {"query": "ADS-B Out", "per_page": 2}),
        (
            "govinfo_search",
            govinfo_search,
            {
                "query": "Federal Aviation Administration airworthiness",
                "collections": ["CFR", "PLAW"],
                "page_size": 2,
            },
        ),
        ("courtlistener_search", courtlistener_search, {"query": "Montreal Convention", "per_page": 2}),
    ]
    for name, tool, args in cases:
        try:
            raw = tool.call(args)
            data = json.loads(raw)
            count = data.get("count")
            extra = ""
            if "authenticated" in data:
                extra = f" authenticated={data['authenticated']}"
            print(f"  {name}: count={count}{extra} keys={list(data.keys())}")
            if "error" in data:
                failures.append(f"{name}: returned error {data['error']!r}")
            elif count is None or count < 1:
                failures.append(f"{name}: count was {count!r}")
            if name == "courtlistener_search" and not data.get("authenticated"):
                failures.append("courtlistener_search: token not picked up from env")
            if name == "govinfo_search" and not data.get("authenticated"):
                failures.append("govinfo_search: not authenticated (key missing)")
        except Exception as e:
            failures.append(f"{name}: exception {e!r}")

    _section("3. Skill registry exposes the tools")
    expected_in = {
        "brief",
        "compliance-check",
        "legal-response",
        "legal-risk-assessment",
        "meeting-briefing",
        "review-contract",
        "vendor-check",
    }
    expected_out = {"triage-nda", "signature-request"}
    new_tool_names = {
        "ecfr_search",
        "federal_register_search",
        "govinfo_search",
        "courtlistener_search",
    }
    for skill in expected_in:
        names = {getattr(t, "name", None) for t in skill_tools(skill)}
        if not new_tool_names.issubset(names):
            failures.append(f"skill_tools({skill!r}) missing: {new_tool_names - names}")
        print(f"  {skill}: ok ({sorted(n for n in names if n in new_tool_names)})")
    for skill in expected_out:
        names = {getattr(t, "name", None) for t in skill_tools(skill)}
        leaked = new_tool_names & names
        if leaked:
            failures.append(f"skill_tools({skill!r}) unexpectedly has: {leaked}")
        print(f"  {skill}: not exposed (correct)")

    _section("4. Provider tool-shaping accepts the tools")
    from legal_helper.providers.openai_provider import _tool_to_openai

    for t in (ecfr_search, federal_register_search, govinfo_search, courtlistener_search):
        shaped = _tool_to_openai(t)
        if shaped.get("type") != "function" or not shaped.get("name") or not shaped.get("parameters"):
            failures.append(f"OpenAI shape bad for {t.name}: {shaped}")
        print(f"  {t.name} → OpenAI function shape: name={shaped.get('name')!r}")

    for t in (ecfr_search, federal_register_search, govinfo_search, courtlistener_search):
        type_name = type(t).__name__
        if type_name != "BetaFunctionTool":
            failures.append(f"{t!r} not BetaFunctionTool (got {type_name})")
        else:
            print(f"  {t.name}: BetaFunctionTool ✓")

    _section("Result")
    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — all four legal-search tools are wired correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
