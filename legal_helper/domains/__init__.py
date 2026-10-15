"""Domain packs — opt-in specialised legal areas layered over the generic core.

A pack is active when pinned (settings, a project, `--domain-pack`) or when the
author loads it with `load_pack` (tools/pack_tools.py). There is no keyword
auto-detection.
"""

from .pack import DomainPack, available_packs, load_pack

__all__ = [
    "DomainPack",
    "available_packs",
    "load_pack",
]
