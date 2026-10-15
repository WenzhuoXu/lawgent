"""The connector functions visible under the active jurisdictions + domain
packs, for the author's and the specialists' tool surfaces.

The connectors themselves live in ``legal_helper/connectors/`` and are
``@beta_tool``-decorated, so they are already function-tool-shaped for both
providers; this module just filters them.
"""

from __future__ import annotations

from typing import Iterable

from ..connectors import REGISTRY, filter_for


def visible_connector_tools(
    jurisdictions: Iterable[str],
    active_packs: Iterable[str],
) -> list:
    """Return the connector functions visible per the active config."""
    return [e.fn for e in filter_for(jurisdictions, active_packs)]


def all_connector_tools() -> list:
    return [e.fn for e in REGISTRY]
