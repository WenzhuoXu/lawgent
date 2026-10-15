"""Domain packs, loaded by the author when a request falls in their domain.

The system prompt names the packs that exist; whether a request falls in one
is the author's call, never keyword matching, and `load_pack` hands it the
pack as a tool result — its playbook, its source routing, the methods it
overlays, and its source tools — so the cached system prompt never changes
mid-chat.

The tool list is fixed for a whole tool loop, so a pack's source tools sit
on it from the start, deferred behind the provider's native tool search like
every other source tool (tool_exposure.py): the model finds them when it
needs them and calls them by name. Packs pinned in settings, a project or
`--domain-pack` are inlined in the system prompt from the start.
"""

from __future__ import annotations

from typing import Any

from anthropic import beta_tool

from ..logging_setup import log_workflow_event


def _turn_settings() -> Any:
    from ..config import current_settings
    from ..turn_context import current_turn

    turn = current_turn()
    return turn.settings if turn is not None else current_settings()


def _manifest(name: str):
    from ..domains import load_pack as load_pack_manifest

    return load_pack_manifest(name)


def loadable_packs(settings: Any) -> list[tuple[str, str]]:
    """(name, display name) of each installed pack not already active."""
    from ..domains import available_packs

    active = set(getattr(settings, "active_domain_packs", []) or [])
    out: list[tuple[str, str]] = []
    for name in available_packs():
        if name in active:
            continue
        try:
            out.append((name, _manifest(name).display_name))
        except Exception:  # noqa: BLE001 — a malformed pack.yaml never breaks a turn
            continue
    return out


def pack_catalog_block(settings: Any) -> str:
    """The system prompt's pack list: names only, so no domain's regulators or
    tools reach a turn that never loads the pack."""
    packs = loadable_packs(settings)
    if not packs:
        return ""
    lines = "\n".join(f"- `{name}` — {display}" for name, display in packs)
    return (
        "# Domain packs\n"
        "A domain pack adds a practice area's playbook, source routing, method "
        "overlays and source tools. When a request falls in a pack's domain, load "
        "it with `load_pack` before you research or load a method; the result says "
        "how to reach its source tools. A request outside every pack needs none.\n"
        + lines
    )


def _pack_tools(settings: Any, name: str) -> list[Any]:
    """The tools a pack adds to the author's surface under these settings."""
    from . import _active_pack_jurisdictions
    from .connectors_tools import visible_connector_tools

    active = list(getattr(settings, "active_domain_packs", []) or [])
    base_jurisdictions = [
        settings.default_jurisdiction,
        *settings.secondary_jurisdictions,
        *_active_pack_jurisdictions(active),
    ]
    base = {t.name for t in visible_connector_tools(base_jurisdictions, active)}
    widened = visible_connector_tools(
        [*base_jurisdictions, *_active_pack_jurisdictions([name])], [*active, name]
    )
    tools = [t for t in widened if t.name not in base]
    if name == "aviation":  # the pack's own dispatcher, as `author_tool_surface` adds it
        from .aviation_search import aviation_source_search

        tools.append(aviation_source_search)
    return tools


def pack_source_tools(settings: Any) -> list[Any]:
    """Every loadable pack's source tools, for a surface that defers them
    behind the provider's tool search (each name once)."""
    seen: set[str] = set()
    out: list[Any] = []
    for name, _display in loadable_packs(settings):
        for tool in _pack_tools(settings, name):
            if tool.name not in seen:
                seen.add(tool.name)
                out.append(tool)
    return out


@beta_tool
def load_pack(name: str) -> str:
    """Load a domain pack listed in the system prompt, when the request falls in its domain.

    Returns the pack's playbook and source routing, the methods it overlays
    (`load_skill` includes the overlay from now on), and its source tools.
    Specialists started with `run_workflow` afterwards hold the pack too.

    Args:
        name: A pack name from the system prompt's pack list, e.g. `aviation`.
    """
    from ..domains import available_packs
    from ..turn_context import current_turn

    name = (name or "").strip().strip("`")
    if name not in available_packs():
        return "ERROR: Unknown pack " + repr(name) + ". Packs: " + ", ".join(available_packs())
    settings = _turn_settings()
    active = list(getattr(settings, "active_domain_packs", []) or [])
    pack = _manifest(name)
    if name in active:
        # Pinned (its playbook is in the system prompt) or loaded earlier in
        # this turn (it is in an earlier tool result): never send it twice.
        return (
            f"The `{name}` pack is already loaded for this turn; its playbook and "
            "source routing are above, and tool search finds its source tools."
        )
    tools = _pack_tools(settings, name)
    updated = settings.model_copy(update={"active_domain_packs": [*active, name]})
    turn = current_turn()
    if turn is not None:
        turn.settings = updated
    log_workflow_event("active_domain_packs", {"packs": [*active, name], "loaded": name})
    blocks = [f'<pack_content name="{name}">', f"# {pack.display_name} (`{name}` pack)"]
    for label, path in (("Playbook", pack.playbook_path), ("Research sources", pack.research_sources_path)):
        if path is not None and path.is_file():
            try:
                blocks.append(f"## {label}\n\n{path.read_text(encoding='utf-8').strip()}")
            except OSError:
                continue
    overlays = sorted(pack.skill_overlays)
    if overlays:
        blocks.append(
            "## Method overlays\n`load_skill` now appends this pack's overlay for: "
            + ", ".join(f"`{m}`" for m in overlays)
        )
    if tools:
        blocks.append(
            "## Source tools — found through tool search, then called by name\n"
            + "\n".join(
                f"- `{t.name}` — {(getattr(t, 'description', '') or '').strip().splitlines()[0][:200]}"
                for t in tools
            )
        )
    blocks.append("</pack_content>")
    return "\n\n".join(blocks)


__all__ = ["load_pack", "loadable_packs", "pack_catalog_block", "pack_source_tools"]
