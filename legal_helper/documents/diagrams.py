"""Flowchart / diagram helpers.

Wraps the ``@mermaid-js/mermaid-cli`` (``mmdc``) binary so the rest of the
codebase can:

* render a Mermaid string to a PNG/SVG image (``render_mermaid_to_file``),
* extract node positions from a Mermaid-rendered SVG and project them onto
  a PowerPoint slide's bounding box (``autolayout_flowchart``),
* be invoked once per ``write_pptx`` call as a pre-processing step
  (``prepare_flowchart_slides``).

The Node binary lives at ``node_modules/.bin/mmdc`` (preferred) or on
PATH. ``PUPPETEER_EXECUTABLE_PATH`` is auto-set to ``/usr/bin/google-chrome``
when present so first-run Chromium downloads aren't needed on this box.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


_REPO_ROOT = Path(__file__).resolve().parents[2]


class MermaidNotInstalled(RuntimeError):
    """Raised when ``mmdc`` cannot be located."""


def find_mmdc() -> Path:
    """Return the path to the ``mmdc`` binary.

    Search order: project-local ``node_modules/.bin/mmdc`` first
    (matches the package.json devDependency the repo ships with), then
    ``$PATH``. Raise ``MermaidNotInstalled`` with an actionable hint when
    nothing resolves.
    """
    local = _REPO_ROOT / "node_modules" / ".bin" / "mmdc"
    if local.is_file():
        return local
    found = shutil.which("mmdc")
    if found:
        return Path(found)
    raise MermaidNotInstalled(
        "mmdc (mermaid-cli) is not installed. Run "
        "`conda run -n llm npm i @mermaid-js/mermaid-cli --save-dev` "
        "inside the legal_helper repo."
    )


def _chrome_executable() -> str | None:
    """Best-effort discovery of a Chrome/Chromium binary so puppeteer
    doesn't try to download Chromium on first mmdc run."""
    for candidate in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if Path(candidate).is_file():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def _puppeteer_config_file() -> Path | None:
    """Write a tiny puppeteer config that points to the system Chrome,
    if one is available. mmdc 11+ honours ``--puppeteerConfigFile``.
    """
    chrome = _chrome_executable()
    if not chrome:
        return None
    cfg = Path(tempfile.gettempdir()) / "legal_helper_mmdc_puppeteer.json"
    if not cfg.is_file():
        cfg.write_text(
            '{"executablePath": "' + chrome + '", "args": ["--no-sandbox"]}',
            encoding="utf-8",
        )
    return cfg


def render_mermaid_to_file(
    source: str,
    output_path: Path,
    *,
    theme: str = "default",
    background: str = "white",
    timeout: int = 60,
) -> Path:
    """Render ``source`` (a Mermaid string) to ``output_path``.

    ``output_path``'s suffix selects the format (``.svg``, ``.png``, ``.pdf``).
    Returns the absolute output path. Raises ``MermaidNotInstalled`` if
    ``mmdc`` is missing, ``RuntimeError`` if the render fails.
    """
    mmdc = find_mmdc()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        str(mmdc),
        "-i", "-",
        "-o", str(output_path),
        "-t", theme,
        "-b", background,
    ]
    pup_cfg = _puppeteer_config_file()
    if pup_cfg is not None:
        args.extend(["--puppeteerConfigFile", str(pup_cfg)])
    env = None
    chrome = _chrome_executable()
    if chrome:
        import os
        env = dict(os.environ)
        env.setdefault("PUPPETEER_EXECUTABLE_PATH", chrome)
    try:
        subprocess.run(
            args,
            input=source,
            text=True,
            check=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"mmdc failed (exit {exc.returncode}): {exc.stderr.strip()[:500]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"mmdc timed out after {timeout}s") from exc
    if not output_path.is_file():
        raise RuntimeError(f"mmdc did not produce output at {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Mermaid -> internal nodes/edges parser
# ---------------------------------------------------------------------------


@dataclass
class _ParsedNode:
    id: str
    kind: str = "process"
    text: str = ""


@dataclass
class _ParsedEdge:
    from_id: str
    to_id: str
    label: str = ""
    style: str = "solid"


_NODE_SHAPE_RE = re.compile(
    # An identifier followed by a shape constructor. The constructor's
    # closing bracket is matched lazily so nested brackets in labels are OK.
    r"(?P<id>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:"
    r"\[\[(?P<sub>.+?)\]\]"           # [[subprocess]]
    r"|\[\/(?P<io>.+?)\/\]"            # [/IO/]
    r"|\(\[(?P<stad>.+?)\]\)"         # ([stadium])  -> terminator
    r"|\(\((?P<term2>.+?)\)\)"        # ((circle))   -> terminator
    r"|\[(?P<proc>.+?)\]"              # [process]
    r"|\{(?P<dec>.+?)\}"               # {decision}
    r"|\((?P<term>.+?)\)"              # (terminator)
    r"|>(?P<note>.+?)\]"               # >note]
    r")"
)


_SHAPE_OPT = (
    r"(?:\[\[.+?\]\]|\[\/.+?\/\]|\(\[.+?\]\)|\(\(.+?\)\)|"
    r"\[.+?\]|\{.+?\}|\(.+?\)|>.+?\])?"
)

_EDGE_RE = re.compile(
    r"(?P<from>[A-Za-z_][A-Za-z0-9_]*)" + _SHAPE_OPT +
    r"\s*(?P<arrow>-\.->|\.\.>|==>|-->|---)\s*"
    r"(?:\|(?P<label>[^|]*)\|\s*)?"
    r"(?P<to>[A-Za-z_][A-Za-z0-9_]*)" + _SHAPE_OPT
)


_HEADER_PREFIXES = (
    "flowchart", "graph", "subgraph", "end", "classdef",
    "class ", "linkstyle", "style ", "click ", "direction",
)
_RESERVED_TOKENS = {
    "flowchart", "graph", "TB", "BT", "LR", "RL", "TD",
    "subgraph", "end", "classDef", "class", "direction",
}


def _strip_constructs(line: str) -> str:
    """Remove shape constructors and edge labels from a line so the
    bare-identifier scan won't pick up label text as new node IDs."""
    # Strip pipe-delimited edge labels first.
    cleaned = re.sub(r"\|[^|]*\|", " ", line)
    # Then strip shape bodies (the brackets themselves and their content).
    cleaned = re.sub(r"\[\[.+?\]\]|\[\/.+?\/\]|\(\[.+?\]\)|\(\(.+?\)\)|"
                     r"\[.+?\]|\{.+?\}|\(.+?\)|>.+?\]", " ", cleaned)
    return cleaned


def parse_mermaid_flowchart(source: str) -> tuple[list[_ParsedNode], list[_ParsedEdge]]:
    """Minimal parser for ``flowchart`` / ``graph`` blocks.

    Supports node shapes ``[]``, ``{}``, ``()``, ``(())``, ``([])``,
    ``[[]]``, ``[//]``, ``>]`` and edges ``-->``, ``-.->``, ``==>``,
    ``---`` with optional ``|label|``. Anything fancier (subgraphs,
    classDefs) is skipped — declared nodes still get a position from
    the SVG.
    """
    nodes: dict[str, _ParsedNode] = {}
    edges: list[_ParsedEdge] = []
    referenced: set[str] = set()  # IDs we've seen *with* a shape decl

    def _record_node(node_id: str, kind: str, text: str) -> None:
        text = (text or "").strip()
        existing = nodes.get(node_id)
        if existing is None:
            nodes[node_id] = _ParsedNode(id=node_id, kind=kind, text=text or node_id)
        else:
            # First explicit shape wins; only update text if missing.
            if (not existing.text or existing.text == existing.id) and text:
                existing.text = text
            if existing.kind == "process" and kind != "process":
                existing.kind = kind
        referenced.add(node_id)

    for raw_line in source.splitlines():
        line = raw_line.strip()
        low = line.lower()
        if not line or line.startswith("%%") or any(low.startswith(p) for p in _HEADER_PREFIXES):
            continue
        # 1) Pull out explicit shape declarations
        for m in _NODE_SHAPE_RE.finditer(line):
            node_id = m.group("id")
            if m.group("sub"):
                _record_node(node_id, "subprocess", m.group("sub"))
            elif m.group("io"):
                _record_node(node_id, "io", m.group("io"))
            elif m.group("stad"):
                _record_node(node_id, "terminator", m.group("stad"))
            elif m.group("term2"):
                _record_node(node_id, "terminator", m.group("term2"))
            elif m.group("proc"):
                _record_node(node_id, "process", m.group("proc"))
            elif m.group("dec"):
                _record_node(node_id, "decision", m.group("dec"))
            elif m.group("term"):
                _record_node(node_id, "terminator", m.group("term"))
            elif m.group("note"):
                _record_node(node_id, "data", m.group("note"))
        # 2) Edges
        for m in _EDGE_RE.finditer(line):
            arrow = m.group("arrow")
            style = "dashed" if "." in arrow else "solid"
            label = (m.group("label") or "").strip()
            edges.append(_ParsedEdge(
                from_id=m.group("from"),
                to_id=m.group("to"),
                label=label,
                style=style,
            ))
        # 3) Bare references — only after stripping label/shape bodies so
        #    we don't promote "yes", "Start", etc. to standalone nodes.
        bare_line = _strip_constructs(line)
        # Also remove arrows so the surrounding identifiers don't double-count
        # already-handled edges.
        bare_line = re.sub(r"-\.->|\.\.>|==>|-->|---", " ", bare_line)
        for token in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", bare_line):
            if token in _RESERVED_TOKENS:
                continue
            if token not in nodes:
                nodes[token] = _ParsedNode(id=token, kind="process", text=token)

    return list(nodes.values()), edges


def synthesize_mermaid(nodes: Iterable[dict[str, Any]], edges: Iterable[dict[str, Any]],
                      direction: str = "TB") -> str:
    """Build a mermaid flowchart string from explicit node/edge dicts."""
    def _shape(kind: str, text: str) -> str:
        t = (text or "").replace("\n", " ").replace("\"", "&quot;")
        if kind == "decision":
            return f"{{{t}}}"
        if kind == "terminator":
            return f"(({t}))"
        if kind == "io":
            return f"[/{t}/]"
        if kind == "data":
            return f">{t}]"
        if kind == "subprocess":
            return f"[[{t}]]"
        return f"[{t}]"

    lines = [f"flowchart {direction}"]
    for n in nodes:
        lines.append(f"  {n['id']}{_shape(n.get('kind', 'process'), n.get('text', ''))}")
    for e in edges:
        arrow = "-.->" if e.get("style") == "dashed" else "-->"
        label = (e.get("label") or "").strip()
        if label:
            lines.append(f"  {e['from_id']} {arrow}|{label}| {e['to_id']}")
        else:
            lines.append(f"  {e['from_id']} {arrow} {e['to_id']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SVG -> positions
# ---------------------------------------------------------------------------


_VIEWBOX_RE = re.compile(r'viewBox="([^"]+)"')
_NODE_GROUP_RE = re.compile(
    r'<g[^>]*class="node[^"]*"[^>]*id="(?P<svg_id>[^"]+)"[^>]*transform="translate\((?P<cx>[\-\d.]+)\s*,\s*(?P<cy>[\-\d.]+)\)"'
)


@dataclass
class _SvgPos:
    user_id: str
    cx: float
    cy: float


def parse_mermaid_svg_positions(svg_text: str) -> tuple[float, float, list[_SvgPos]]:
    """Extract (svg_width, svg_height, [_SvgPos…]) from a mermaid SVG.

    Node ``id`` in mermaid SVG looks like ``<svg-id>-flowchart-<USER_ID>-<n>``.
    We split off the trailing ``-<n>`` and the leading ``-flowchart-`` to
    recover the user's original node ID.
    """
    viewbox = _VIEWBOX_RE.search(svg_text)
    if not viewbox:
        raise RuntimeError("SVG has no viewBox; cannot scale coordinates")
    parts = viewbox.group(1).split()
    if len(parts) != 4:
        raise RuntimeError(f"Unexpected viewBox: {viewbox.group(1)!r}")
    svg_w = float(parts[2])
    svg_h = float(parts[3])

    positions: list[_SvgPos] = []
    for m in _NODE_GROUP_RE.finditer(svg_text):
        svg_id = m.group("svg_id")
        # Strip "<svg-id>-flowchart-" prefix and "-<digits>" suffix to get user ID.
        user_id = svg_id
        # Suffix: remove trailing "-<digits>"
        user_id = re.sub(r"-\d+$", "", user_id)
        # Prefix: keep everything after the LAST "-flowchart-" marker
        if "-flowchart-" in user_id:
            user_id = user_id.rsplit("-flowchart-", 1)[1]
        positions.append(_SvgPos(
            user_id=user_id,
            cx=float(m.group("cx")),
            cy=float(m.group("cy")),
        ))
    return svg_w, svg_h, positions


# ---------------------------------------------------------------------------
# Public auto-layout entrypoint
# ---------------------------------------------------------------------------


def autolayout_flowchart(fc: dict[str, Any]) -> dict[str, Any]:
    """Mutate ``fc`` in place: fill node x/y/w/h (inches) using mermaid layout.

    Accepts the flat dict form (``Slide.flowchart.model_dump()``). When
    ``mermaid`` is set, parses the string for kinds/labels/edges; when
    ``nodes``/``edges`` are set, synthesizes a mermaid string for layout.
    Skips work if layout="manual" and every node already has x/y/w/h.

    Returns the same dict.
    """
    if not fc:
        return fc

    layout_mode = fc.get("layout", "auto")
    direction = fc.get("direction", "TB")
    bbox = (
        float(fc.get("x", 0.55)),
        float(fc.get("y", 1.25)),
        float(fc.get("w", 12.2)),
        float(fc.get("h", 5.6)),
    )
    default_w = float(fc.get("node_w", 2.2))
    default_h = float(fc.get("node_h", 0.9))

    mermaid_source = (fc.get("mermaid") or "").strip()
    nodes: list[dict[str, Any]] = list(fc.get("nodes") or [])
    edges: list[dict[str, Any]] = list(fc.get("edges") or [])

    if not nodes and mermaid_source:
        parsed_nodes, parsed_edges = parse_mermaid_flowchart(mermaid_source)
        nodes = [{
            "id": n.id, "kind": n.kind, "text": n.text,
            "x": None, "y": None, "w": None, "h": None,
            "fill": "", "stroke": "",
        } for n in parsed_nodes]
        edges = [{
            "from_id": e.from_id, "to_id": e.to_id,
            "label": e.label, "style": e.style, "arrow": "end",
        } for e in parsed_edges]

    if not nodes:
        # Nothing to lay out — leave fc alone so the JS writer skips it.
        fc["nodes"] = []
        fc["edges"] = []
        return fc

    if layout_mode == "manual" and all(
        n.get("x") is not None and n.get("y") is not None for n in nodes
    ):
        for n in nodes:
            n.setdefault("w", default_w)
            n.setdefault("h", default_h)
            if n.get("w") is None:
                n["w"] = default_w
            if n.get("h") is None:
                n["h"] = default_h
        fc["nodes"] = nodes
        fc["edges"] = edges
        return fc

    # Build a mermaid string we control (either user-provided or synthesized).
    layout_source = mermaid_source if mermaid_source else synthesize_mermaid(
        nodes, edges, direction=direction,
    )
    # Ensure the direction is honoured even when user supplied mermaid that
    # uses a different one — append a fresh header only if mermaid_source is
    # missing the flowchart/graph directive.
    if not re.search(r"^\s*(flowchart|graph)\b", layout_source, flags=re.MULTILINE | re.IGNORECASE):
        layout_source = f"flowchart {direction}\n" + layout_source

    # Render to a temp SVG.
    tmp_svg = Path(tempfile.gettempdir()) / f"legal_helper_fc_{abs(hash(layout_source)) % (10**8)}.svg"
    try:
        render_mermaid_to_file(layout_source, tmp_svg, background="white")
        svg_text = tmp_svg.read_text(encoding="utf-8")
    except MermaidNotInstalled:
        # Fall back to a vertical stack so the deck is still useful.
        _fallback_stack_layout(nodes, bbox, default_w, default_h)
        fc["nodes"] = nodes
        fc["edges"] = edges
        return fc
    finally:
        try:
            tmp_svg.unlink(missing_ok=True)
        except Exception:
            pass

    try:
        svg_w, svg_h, positions = parse_mermaid_svg_positions(svg_text)
    except Exception:
        _fallback_stack_layout(nodes, bbox, default_w, default_h)
        fc["nodes"] = nodes
        fc["edges"] = edges
        return fc

    by_id = {p.user_id: p for p in positions}

    bx, by, bw, bh = bbox
    # Inset so node bounding boxes don't clip the bbox edge.
    inset_w = default_w / 2.0
    inset_h = default_h / 2.0
    avail_w = max(bw - default_w, 0.1)
    avail_h = max(bh - default_h, 0.1)

    for n in nodes:
        pos = by_id.get(n["id"])
        nw = float(n.get("w") or default_w)
        nh = float(n.get("h") or default_h)
        if pos is None:
            # Mermaid couldn't lay this node out — drop it into the corner;
            # the user will still see it and can move it.
            n["x"] = bx
            n["y"] = by
            n["w"] = nw
            n["h"] = nh
            continue
        # Scale center coordinates into the available area, then convert
        # center -> top-left for pptxgenjs.
        cx_in = bx + inset_w + (pos.cx / max(svg_w, 1e-6)) * avail_w
        cy_in = by + inset_h + (pos.cy / max(svg_h, 1e-6)) * avail_h
        n["x"] = round(cx_in - nw / 2.0, 3)
        n["y"] = round(cy_in - nh / 2.0, 3)
        n["w"] = round(nw, 3)
        n["h"] = round(nh, 3)

    fc["nodes"] = nodes
    fc["edges"] = edges
    return fc


def _fallback_stack_layout(
    nodes: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
    default_w: float,
    default_h: float,
) -> None:
    """Stack nodes vertically inside ``bbox`` when mmdc isn't available.

    Better than the historical "naked text boxes" failure mode because the
    nodes are still real flowChart* shapes — just on a regular grid.
    """
    bx, by, bw, bh = bbox
    n = len(nodes)
    if n == 0:
        return
    gap = max((bh - n * default_h) / max(n + 1, 1), 0.1)
    cx = bx + bw / 2.0 - default_w / 2.0
    y = by + gap
    for node in nodes:
        node["x"] = round(cx, 3)
        node["y"] = round(y, 3)
        node["w"] = round(default_w, 3)
        node["h"] = round(default_h, 3)
        y += default_h + gap


def prepare_flowchart_slides(slides: list[dict[str, Any]]) -> None:
    """Iterate ``slides`` (already-dumped Pydantic dicts) and auto-layout
    every flowchart block in place. Safe to call on slides without
    flowcharts — they're left untouched.
    """
    for slide in slides:
        fc = slide.get("flowchart") if isinstance(slide, dict) else None
        if not fc:
            continue
        autolayout_flowchart(fc)


__all__ = [
    "MermaidNotInstalled",
    "autolayout_flowchart",
    "find_mmdc",
    "parse_mermaid_flowchart",
    "parse_mermaid_svg_positions",
    "prepare_flowchart_slides",
    "render_mermaid_to_file",
    "synthesize_mermaid",
]
