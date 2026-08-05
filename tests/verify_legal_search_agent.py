"""Live verification job: exercise eCFR / Federal Register / CourtListener tools
through the compliance-check sub-agent.

Run from project root with the conda llm env active:

    conda run -n llm python tests/verify_legal_search_agent.py
"""

from __future__ import annotations

import os
import pathlib
import sys


def _load_api_key_file() -> None:
    p = pathlib.Path(__file__).resolve().parent.parent / "legal_helper" / "api_key"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            k, _, v = line[len("export ") :].partition("=")
            os.environ.setdefault(k, v)


def main() -> int:
    os.environ["MODEL_PROVIDER"] = "openai"
    _load_api_key_file()

    from legal_helper.agent import SkillAgent
    from legal_helper.config import load_settings
    from legal_helper.providers import build_provider

    settings = load_settings(refresh=True)
    print(f"provider={settings.provider} model={settings.model_for_provider()}")

    provider = build_provider(settings)
    agent = SkillAgent("compliance-check", provider, settings)

    task = (
        "Question: Under US 14 CFR Part 39, what is an operator's obligation "
        "when an airworthiness directive (AD) sets a compliance deadline that "
        "has passed for an aircraft in their fleet? "
        "Cite the specific eCFR section number and at least one Federal "
        "Register document about FAA AD compliance. Keep the answer under "
        "250 words. You MUST call the ecfr_search tool and the "
        "federal_register_search tool at least once each. CourtListener is "
        "not needed for this question. Include the required Evidence "
        "Verification Log, Claim-Level Sanity Check, and Sources sections."
    )

    result = agent.execute(task)
    print("=" * 70)
    print(result)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
