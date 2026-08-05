"""Aviation Legal-Helper LLM Agent."""

__version__ = "0.1.0"

from .config import Settings, current_settings, load_settings

__all__ = ["Settings", "current_settings", "load_settings", "__version__"]
