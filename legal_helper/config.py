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
ANTHROPIC_HIGH_EFFORT_MODELS = ("claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5")
# gpt-5.6 ships only as named variants; the bare "gpt-5.6" alias routes to Sol,
# so always spell out "-terra" / "-sol" / "-luna".
OPENAI_HIGH_EFFORT_MODELS = ("gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.5")


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
    anthropic_model: str = "claude-opus-4-8"
    anthropic_fast_model: str = "claude-haiku-4-5"

    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-5.6-terra"
    openai_fast_model: str = "gpt-5.6-luna"
    openai_reasoning_effort: str = "medium"
    openai_enable_file_search: bool = False
    openai_file_search_vector_store_ids: list[str] = Field(default_factory=list)

    max_iterations: int = 12
    parent_max_iterations: int = 8
    sub_agent_max_iterations: int = 8
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
    chat_compaction_threshold: float = 0.6
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

    def model_for_provider(self, fast: bool = False) -> str:
        if self.provider == "anthropic":
            return self.anthropic_fast_model if fast else self.anthropic_model
        return self.openai_fast_model if fast else self.openai_model

    def high_effort_models_for_provider(self, provider: Optional[str] = None) -> list[str]:
        selected = provider or self.provider
        if selected == "anthropic":
            return list(ANTHROPIC_HIGH_EFFORT_MODELS)
        return list(OPENAI_HIGH_EFFORT_MODELS)

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
        anthropic_model=os.getenv("ANTHROPIC_MODEL", cfg.get("anthropic_model", "claude-opus-4-8")),
        anthropic_fast_model=os.getenv(
            "ANTHROPIC_FAST_MODEL", cfg.get("anthropic_fast_model", "claude-haiku-4-5")
        ),
        openai_api_key=_api_key_value("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", cfg.get("openai_model", "gpt-5.6-terra")),
        openai_fast_model=os.getenv("OPENAI_FAST_MODEL", cfg.get("openai_fast_model", "gpt-5.6-luna")),
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
        max_iterations=int(os.getenv("LEGAL_HELPER_MAX_ITERATIONS", cfg.get("max_iterations", 12))),
        parent_max_iterations=int(cfg.get("parent_max_iterations", 8)),
        sub_agent_max_iterations=int(cfg.get("sub_agent_max_iterations", 12)),
        max_concurrent_agents=_max_concurrent_agents_value(
            os.getenv(
                "LEGAL_HELPER_MAX_CONCURRENT_AGENTS",
                cfg.get("max_concurrent_agents", "auto"),
            )
        ),
        max_tokens=int(cfg.get("max_tokens", 32000)),
        chat_context_token_budget=int(cfg.get("chat_context_token_budget", 16000)),
        chat_context_window_fraction=float(cfg.get("chat_context_window_fraction", 0.25)),
        chat_compaction_threshold=float(cfg.get("chat_compaction_threshold", 0.6)),
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
