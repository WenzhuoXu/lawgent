"""Configuration: env vars + local api_key + .env override config.yaml."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel, Field


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_API_KEY_FILENAME = Path("legal_helper") / "api_key"
# claude-sonnet-5 is the Anthropic fast tier, so it is offered through
# ``anthropic_fast_model`` rather than listed here.
ANTHROPIC_HIGH_EFFORT_MODELS = ("claude-opus-5-5",)
# gpt-6 ships only as named variants (astra / sol / luna); there is no bare
# "gpt-6" alias, so always spell out the variant. Every retired model stays
# priced in usage.py so historical ledger months still replay.
OPENAI_HIGH_EFFORT_MODELS = ("gpt-6-sol",)

# Why a model stopped being offered, for the log line when a stored selection is
# migrated off it. Retirement must be enforced at *load*, not only at selection:
# a chat persists its model at creation (``ChatStore.default_settings``) and
# re-applies it on every later run, so dropping a model from the tuples above
# only ever affected new chats. gpt-5.5 kept billing on pre-retirement chats for
# 18 requests after 2026-09-11 that way. The enforcement rule is the allowlist in
# ``offered_models_for_provider`` — this map only explains the substitution.
RETIRED_MODELS: dict[str, str] = {
    "gpt-5.5": "retired 2026-09-11: $5.00/$30.00 against gpt-5.6-terra's $2.00/$12.00 for the same work",
    "gpt-5.6-terra": "retired 2026-09-22: superseded by gpt-6-sol ($2.00/$10.00 against $2.00/$12.00)",
    "gpt-5.6-sol": "retired 2026-09-22: superseded by gpt-6-sol ($2.00/$10.00 against $4.00/$20.00)",
    "gpt-5.6-luna": "retired 2026-09-22: fast tier superseded by gpt-6-luna ($0.10/$0.50 against $0.20/$1.20)",
    "claude-opus-5": "retired 2026-09-22: superseded by claude-opus-5-5 ($4.00/$20.00 against $5.00/$25.00)",
    "claude-opus-4-8": "retired 2026-09-22: superseded by claude-opus-5-5 ($4.00/$20.00 against $5.00/$25.00)",
    "claude-opus-4-7": "retired 2026-09-22: superseded by claude-opus-5-5 ($4.00/$20.00 against $5.00/$25.00)",
    "claude-haiku-4-5": "retired 2026-09-22: fast tier moved to claude-sonnet-5",
}


def _dedupe(values: "list[str]") -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list_env(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _max_concurrent_agents_value(raw: object) -> int | Literal["auto"]:
    if raw is None:
        return "auto"
    if isinstance(raw, str) and raw.strip().lower() in {"", "auto", "default"}:
        return "auto"
    return max(1, int(raw))


def _read_dotenv_values(path: Path) -> dict[str, str]:
    """Read a dotenv/shell-export file without mutating process environment."""
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


JurisdictionCode = Literal["CN", "US", "EU", "UK", "HK", "ET", "ICAO"]
CitationStyle = Literal["gb_t_7714", "bluebook", "oscola"]


class Settings(BaseModel):
    provider: Literal["anthropic", "openai"] = "openai"

    # Jurisdiction & domain layer
    default_jurisdiction: JurisdictionCode = "CN"
    secondary_jurisdictions: list[JurisdictionCode] = Field(default_factory=lambda: ["US", "EU"])
    default_language: Literal["zh", "en"] = "zh"
    citation_style: CitationStyle = "gb_t_7714"
    active_domain_packs: list[str] = Field(default_factory=list)

    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-opus-5-5"
    anthropic_fast_model: str = "claude-sonnet-5"

    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-6-sol"
    openai_fast_model: str = "gpt-6-luna"
    openai_reasoning_effort: str = "medium"
    openai_enable_file_search: bool = False
    openai_file_search_vector_store_ids: list[str] = Field(default_factory=list)

    # Tool-loop round limits. 0 means none: a turn runs until the model stops
    # asking for tools. Caps used to sit here (12 / 8 / 8 / 80) and every one of
    # them traded task completion for cost — the orchestrator stopped a
    # workbook reshape at ~33 calls with the edit half done and no answer
    # written. What grows during a long loop is the tool results already in the
    # transcript, and `turn_compaction` clears the old ones (saved to disk,
    # reopenable) when a request nears the turn ceiling, so a long loop stays
    # under the pricing cliff without being cut short. A positive value is
    # still honoured for anyone who wants a hard stop.
    max_iterations: int = 0
    parent_max_iterations: int = 0
    sub_agent_max_iterations: int = 0
    external_skill_max_iterations: int = 0
    max_concurrent_agents: int | Literal["auto"] = "auto"
    # Per-turn within-chat context digest (rolling summary + recent transcript)
    # fed to the planner/answer. The effective budget is
    # ``chat_context_window_fraction × model window``, floored at
    # ``chat_context_token_budget`` — so the digest scales with the model
    # actually serving the turn instead of handing a 1M-window model the same
    # 16K digest as a 200K one.
    chat_context_token_budget: int = 16_000
    chat_context_window_fraction: float = 0.25
    # Compact the chat (fold older turns into the rolling summary) once the
    # transcript reaches this fraction of the model's context window — early,
    # per the Claude Code "compact at ~0.6, not 0.95" guidance.
    # 0.0 (the default) means: derive from the model tier — see
    # context.compaction_threshold_for. Any positive value pins it.
    chat_compaction_threshold: float = 0.0
    # Output ceiling per turn. 8192 truncated long legal memos mid-analysis
    # (stop_reason="max_tokens" → the "incomplete chunk" symptom). 32000 gives
    # comprehensive multi-jurisdiction analyses room to finish; Opus 4.x allows
    # up to 128000 with the streaming path. Override in config.yaml as needed.
    max_tokens: int = 32000

    project_root: Path = _PROJECT_ROOT
    api_key_path: Path = _PROJECT_ROOT / _API_KEY_FILENAME
    outputs_dir: Path = _PROJECT_ROOT / "outputs"
    state_dir: Path = _PROJECT_ROOT / "state"
    logs_dir: Path = _PROJECT_ROOT / "logs"
    playbook_path: Path = _PROJECT_ROOT / "legal_helper" / "playbook" / "general_playbook.md"

    enable_web_search: bool = True
    enable_web_fetch: bool = True
    # Post-synthesis citation audit (the /cite-check skill). Disable to skip it
    # on every run (faster iteration; the orchestrator still cites inline).
    enable_cite_check: bool = True
    # Audit a substantive legal answer before the reader sees it, rather than
    # streaming it and labelling the problems afterwards. The answer is
    # buffered, audited, repaired if the audit flags anything, and then
    # revealed; research progress, tool calls and phases still stream live.
    # False restores stream-then-label.
    verify_before_reveal: bool = True
    # How many repair passes a failed audit may trigger. Each round costs one
    # synthesis turn plus one re-audit, so this is a cost dial as much as a
    # quality one; 0 disables repair and keeps the audit advisory.
    max_repair_rounds: int = 1

    def model_for_provider(self, fast: bool = False) -> str:
        if self.provider == "anthropic":
            return self.anthropic_fast_model if fast else self.anthropic_model
        return self.openai_fast_model if fast else self.openai_model

    def high_effort_models_for_provider(self, provider: Optional[str] = None) -> list[str]:
        selected = provider or self.provider
        if selected == "anthropic":
            return list(ANTHROPIC_HIGH_EFFORT_MODELS)
        return list(OPENAI_HIGH_EFFORT_MODELS)

    def offered_models_for_provider(self, provider: Optional[str] = None) -> list[str]:
        """Every model this provider may legitimately serve a turn with.

        The high-effort tier, the fast tier, and whatever is pinned in
        ``config.yaml`` — an explicit pin is by definition the user's selection,
        so it must stay valid even when it is not in the tuples above. This is
        the allowlist ``resolve_model_for_provider`` enforces, and the same set
        ``/api/runtime-options`` offers the UI.
        """
        selected = provider or self.provider
        if selected == "anthropic":
            return _dedupe(
                [*ANTHROPIC_HIGH_EFFORT_MODELS, self.anthropic_model, self.anthropic_fast_model]
            )
        return _dedupe([*OPENAI_HIGH_EFFORT_MODELS, self.openai_model, self.openai_fast_model])

    def resolve_model_for_provider(
        self, model: Optional[str], provider: Optional[str] = None
    ) -> str:
        """``model`` if it is still offered for ``provider``, else the user's default.

        Stored selections outlive the policy that created them. Anything no
        longer offered — a retired model, a model from the other provider in a
        cross-wired record, a typo — collapses to ``model_for_provider()``, the
        model the user has actually selected in config.
        """
        selected = provider or self.provider
        candidate = (model or "").strip()
        if candidate and candidate in self.offered_models_for_provider(selected):
            return candidate
        if selected == "anthropic":
            return self.anthropic_model
        return self.openai_model

    def effective_max_concurrent_agents(self) -> int:
        if self.max_concurrent_agents != "auto":
            return max(1, int(self.max_concurrent_agents))
        if self.provider == "anthropic":
            return 2
        if self.provider == "openai":
            return 4
        return 2


_CACHED: Optional[Settings] = None
_CURRENT_SETTINGS_OVERRIDE: ContextVar[Optional[Settings]] = ContextVar(
    "legal_helper_current_settings_override",
    default=None,
)


def load_settings(*, refresh: bool = False) -> Settings:
    """Load settings from config.yaml + .env + legal_helper/api_key.

    API key precedence is:
    1. real process environment, for explicit one-off shell overrides
    2. legal_helper/api_key, the local startup key reference file
    3. .env
    4. unset
    """
    global _CACHED
    if _CACHED is not None and not refresh:
        return _CACHED

    original_env = dict(os.environ)
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    api_key_path = _PROJECT_ROOT / _API_KEY_FILENAME
    api_key_values = _read_dotenv_values(api_key_path)

    # Make every key in the api_key file visible to downstream tools (e.g.
    # govinfo_search, courtlistener_search) via os.getenv, without overriding
    # values already present in the real process environment.
    for _k, _v in api_key_values.items():
        os.environ.setdefault(_k, _v)

    def _api_key_value(name: str) -> Optional[str]:
        return original_env.get(name) or api_key_values.get(name) or os.getenv(name)

    cfg_path = _PROJECT_ROOT / "config.yaml"
    cfg: dict = {}
    if cfg_path.is_file():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}

    provider = os.getenv("MODEL_PROVIDER", cfg.get("provider", "openai")).lower()
    if provider not in {"anthropic", "openai"}:
        provider = "openai"

    s = Settings(
        provider=provider,  # type: ignore[arg-type]
        anthropic_api_key=_api_key_value("ANTHROPIC_API_KEY"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", cfg.get("anthropic_model", "claude-opus-5-5")),
        anthropic_fast_model=os.getenv(
            "ANTHROPIC_FAST_MODEL", cfg.get("anthropic_fast_model", "claude-sonnet-5")
        ),
        openai_api_key=_api_key_value("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", cfg.get("openai_model", "gpt-6-sol")),
        openai_fast_model=os.getenv("OPENAI_FAST_MODEL", cfg.get("openai_fast_model", "gpt-6-luna")),
        openai_reasoning_effort=os.getenv(
            "OPENAI_REASONING_EFFORT", cfg.get("openai_reasoning_effort", "medium")
        ),
        openai_enable_file_search=_bool_env(
            "OPENAI_ENABLE_FILE_SEARCH", bool(cfg.get("openai_enable_file_search", False))
        ),
        openai_file_search_vector_store_ids=(
            _list_env("OPENAI_FILE_SEARCH_VECTOR_STORE_IDS")
            or list(cfg.get("openai_file_search_vector_store_ids", []) or [])
        ),
        max_iterations=int(os.getenv("LEGAL_HELPER_MAX_ITERATIONS", cfg.get("max_iterations", 0))),
        parent_max_iterations=int(
            os.getenv("LEGAL_HELPER_PARENT_MAX_ITERATIONS", cfg.get("parent_max_iterations", 0))
        ),
        sub_agent_max_iterations=int(
            os.getenv("LEGAL_HELPER_SUB_AGENT_MAX_ITERATIONS", cfg.get("sub_agent_max_iterations", 0))
        ),
        external_skill_max_iterations=int(
            os.getenv(
                "LEGAL_HELPER_EXTERNAL_SKILL_MAX_ITERATIONS",
                cfg.get("external_skill_max_iterations", 0),
            )
        ),
        max_concurrent_agents=_max_concurrent_agents_value(
            os.getenv(
                "LEGAL_HELPER_MAX_CONCURRENT_AGENTS",
                cfg.get("max_concurrent_agents", "auto"),
            )
        ),
        max_tokens=int(cfg.get("max_tokens", 32000)),
        chat_context_token_budget=int(cfg.get("chat_context_token_budget", 16000)),
        chat_context_window_fraction=float(cfg.get("chat_context_window_fraction", 0.25)),
        chat_compaction_threshold=float(cfg.get("chat_compaction_threshold", 0.0)),
        api_key_path=api_key_path,
        outputs_dir=_PROJECT_ROOT / os.getenv("LEGAL_HELPER_OUTPUTS_DIR", cfg.get("outputs_dir", "outputs")),
        state_dir=_PROJECT_ROOT / os.getenv("LEGAL_HELPER_STATE_DIR", cfg.get("state_dir", "state")),
        logs_dir=_PROJECT_ROOT / os.getenv("LEGAL_HELPER_LOGS_DIR", cfg.get("logs_dir", "logs")),
        playbook_path=_PROJECT_ROOT / cfg.get(
            "playbook_path", "legal_helper/playbook/general_playbook.md"
        ),
        enable_web_search=_bool_env("LEGAL_HELPER_ENABLE_WEB_SEARCH", bool(cfg.get("enable_web_search", True))),
        enable_web_fetch=_bool_env("LEGAL_HELPER_ENABLE_WEB_FETCH", bool(cfg.get("enable_web_fetch", True))),
        enable_cite_check=_bool_env("LEGAL_HELPER_ENABLE_CITE_CHECK", bool(cfg.get("enable_cite_check", True))),
        # These three were declared as fields and documented in config.yaml but
        # never read from it, so editing the file changed nothing and the values
        # only happened to match the defaults.
        verify_before_reveal=_bool_env(
            "LEGAL_HELPER_VERIFY_BEFORE_REVEAL", bool(cfg.get("verify_before_reveal", True))
        ),
        max_repair_rounds=int(
            os.getenv("LEGAL_HELPER_MAX_REPAIR_ROUNDS", cfg.get("max_repair_rounds", 1))
        ),
        default_jurisdiction=cfg.get("default_jurisdiction", "CN"),
        secondary_jurisdictions=list(cfg.get("secondary_jurisdictions", ["US", "EU"]) or []),
        default_language=cfg.get("default_language", "zh"),
        citation_style=cfg.get("citation_style", "gb_t_7714"),
        active_domain_packs=(
            _list_env("LEGAL_HELPER_ACTIVE_DOMAIN_PACKS")
            or list(cfg.get("active_domain_packs", []) or [])
        ),
    )

    s.outputs_dir.mkdir(parents=True, exist_ok=True)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    _CACHED = s
    return s


def current_settings() -> Settings:
    """Always-cheap accessor; assumes load_settings has been (or will be) called."""
    override = _CURRENT_SETTINGS_OVERRIDE.get()
    if override is not None:
        return override
    return load_settings()


@contextmanager
def override_current_settings(settings: Settings) -> Iterator[None]:
    """Temporarily make tool helpers see request-scoped settings."""
    token = _CURRENT_SETTINGS_OVERRIDE.set(settings)
    try:
        yield
    finally:
        _CURRENT_SETTINGS_OVERRIDE.reset(token)
