"""run_skill: orchestrator tool that dispatches a task to a sub-agent."""

from typing import Literal

from anthropic import beta_tool


SKILL_NAMES = Literal[
    "brief",
    "cite-check",
    "compliance-check",
    "draft-agreement",
    "legal-response",
    "legal-risk-assessment",
    "litigation-analysis",
    "meeting-briefing",
    "review-contract",
    "signature-request",
    "tabular-review",
    "triage-nda",
    "vendor-check",
]


@beta_tool
def run_skill(skill_name: SKILL_NAMES, task: str) -> str:
    """Dispatch the task to a fresh, forked-context sub-agent.

    Only the sub-agent's final answer is returned to the orchestrator — the
    sub-agent's internal turns stay in its own JSONL log under the same
    parent run_id. The sub-agent starts with a compact skill manifest and can
    read exact SKILL.md / playbook sections through resource tools as needed.

    Args:
        skill_name: One of the available generic legal skills. Use
            ``review-contract`` for clause-by-clause contract review,
            ``triage-nda`` for NDA classification, ``compliance-check`` for
            statutes / regulations / approvals / reporting duties that apply
            to a proposed action, ``draft-agreement`` for drafting a new
            agreement or PRC filing (起诉状 / 答辩状) from deal terms or case
            facts, ``legal-risk-assessment`` for severity-by-
            likelihood risk scoring, ``litigation-analysis`` for dispute
            matters (claims/defenses element mapping, evidence chronology,
            诉讼时效 limitation check, verified authority list),
            ``brief`` for standalone legal enquiries,
            topic research, and daily / topic / incident briefings,
            ``meeting-briefing`` for counterparty or authority meeting prep,
            ``legal-response`` for regulator / authority / counterparty
            response drafts, ``signature-request`` for multi-party closings,
            ``tabular-review`` for reviewing a document set as a grid
            (docs as rows, questions as columns, per-cell pinpoints),
            ``vendor-check`` for third-party diligence, ``cite-check`` for
            extracting and validating citations in a draft.
        task: A self-contained instruction for the sub-agent (include file
            paths to load, parties, deadlines, language preference, source
            requirements, any focus areas, and specific skill/playbook topics
            to inspect; do not paste whole playbook excerpts).
    """
    # Local import to avoid circular dependency at module load time.
    from ..agent import SkillAgent
    from ..config import current_settings
    from ..logging_setup import log_workflow_event
    from ..providers import build_provider

    log_workflow_event(
        "run_skill_called",
        {
            "skill_name": skill_name,
            "task_preview": task[:800],
            # Per the language policy, internal turns use whichever language
            # fits the material — no English-only expectation.
            "internal_language_expected": "any",
        },
    )
    settings = current_settings()
    provider = build_provider(settings)
    result = SkillAgent(skill_name, provider, settings).execute(task)
    legacy_markers = (
        "Evidence Verification Log",
        "Claim-Level Sanity Check",
        "Reporting / Escalation Map",
        "在线证据核验记录",
        "逐项主张核验",
    )
    leaked = [m for m in legacy_markers if m in result]
    findings_idx = result.find("## Findings")
    sources_idx = result.find("## Sources")
    log_workflow_event(
        "run_skill_returned",
        {
            "skill_name": skill_name,
            "response_chars": len(result),
            "has_findings": findings_idx >= 0,
            "has_out_of_scope": "## Out of scope" in result,
            "has_sources": sources_idx >= 0,
            "has_legacy_scaffold_leak": bool(leaked),
            "legacy_markers_leaked": leaked,
            "findings_preview": (
                result[findings_idx : findings_idx + 1600] if findings_idx >= 0 else ""
            ),
            "sources_preview": (
                result[sources_idx : sources_idx + 1600] if sources_idx >= 0 else ""
            ),
        },
    )
    return result
