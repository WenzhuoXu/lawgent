"""Flowchart / diagram helpers.

Mermaid is an authoring syntax here, not a layout engine. It is parsed for
node kinds, labels and edges (``parse_mermaid_flowchart``) and it is still
rendered to a picture when a picture is what the caller wants
(``render_mermaid_to_file``), but the geometry that reaches a slide comes from
:mod:`legal_helper.documents.graph_layout`, which asks Graphviz.

The old path rendered a Mermaid SVG with headless Chromium in order to scrape
node centres back out of it. It cost a browser per diagram and it recovered
only the centres, so arrows were drawn corner-to-corner and the two axes were
scaled independently — which is how fixed-size nodes ended up overlapping.

``mmdc`` is a JavaScript file with a ``#!/usr/bin/env node`` shebang, so the
launching process's PATH decides whether it runs at all. The server is started
by an absolute interpreter path and its PATH has no conda ``bin``, which made
every Mermaid render fail with exit 127. ``resolve_mermaid_cli`` runs it as
``node <cli.js>`` through the same ``find_node`` the PPTX writer uses.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import graph_layout
from .writers._node import find_node, node_diagnostic


_REPO_ROOT = Path(__file__).resolve().parents[2]


class MermaidNotInstalled(RuntimeError):
    """Raised when ``mmdc`` cannot be located."""


@dataclass(frozen=True)
class MermaidCli:
    """How to launch mmdc: an argv prefix plus the PATH the child needs."""

    argv: list[str]
    env_path: str


def _path_with(directory: Path) -> str:
    """Prepend ``directory`` to PATH so a shebang script finds its interpreter."""
    existing = os.environ.get("PATH", "")
    return f"{directory}{os.pathsep}{existing}" if existing else str(directory)


def resolve_mermaid_cli() -> MermaidCli:
    """Locate mmdc and the Node interpreter that has to run it.

    The in-tree package is resolved from its ``package.json`` ``bin`` entry
    rather than from ``node_modules/.bin/mmdc``, so a version bump that moves
    the file is a clear failure instead of a stale symlink. Raises
    :class:`MermaidNotInstalled` when either half is missing.
    """
    pkg = _REPO_ROOT / "node_modules" / "@mermaid-js" / "mermaid-cli"
    manifest = pkg / "package.json"
    if manifest.is_file():
        try:
            entry = str(json.loads(manifest.read_text(encoding="utf-8"))["bin"]["mmdc"])
        except (ValueError, KeyError, TypeError):
            entry = "./src/cli.js"
        script = (pkg / entry.lstrip("./")).resolve()
        if script.is_file():
            node = find_node()
            if not node:
                raise MermaidNotInstalled(
                    f"mermaid-cli is installed at {script} but Node is not. {node_diagnostic()}"
                )
            return MermaidCli(argv=[node, str(script)], env_path=_path_with(Path(node).parent))

    found = shutil.which("mmdc")
    if found:
        node = find_node()
        return MermaidCli(
            argv=[found],
            env_path=_path_with(Path(node).parent) if node else os.environ.get("PATH", ""),
        )

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
    cli = resolve_mermaid_cli()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        *cli.argv,
        "-i", "-",
        "-o", str(output_path),
        "-t", theme,
        "-b", background,
    ]
    pup_cfg = _puppeteer_config_file()
    if pup_cfg is not None:
        args.extend(["--puppeteerConfigFile", str(pup_cfg)])
    env = dict(os.environ)
    env["PATH"] = cli.env_path
    chrome = _chrome_executable()
    if chrome:
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
        stderr = (exc.stderr or "").strip()
        # A missing interpreter is not a render failure. Reporting it as one is
        # what made the degraded layout path unreachable: callers catch
        # MermaidNotInstalled, and this raised RuntimeError.
        if exc.returncode == 127 or "No such file or directory" in stderr:
            raise MermaidNotInstalled(
                f"mmdc could not start (exit {exc.returncode}): {stderr[:300]}. {node_diagnostic()}"
            ) from exc
        raise RuntimeError(f"mmdc failed (exit {exc.returncode}): {stderr[:500]}") from exc
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

# Mermaid writes an edge label two ways and both are common: the piped form
# ``A -->|yes| B`` and the inline form ``A -- yes --> B``. Only the piped form
# was matched, so every labelled decision branch was dropped without a word —
# a process map would render with its yes/no arms simply missing.
_EDGE_RE = re.compile(
    r"(?P<from>[A-Za-z_][A-Za-z0-9_]*)" + _SHAPE_OPT + r"\s*"
    r"(?:"
    r"--\s*(?P<ilabel>[^>|\-][^>|]*?)\s*(?P<iarrow>-->|---)"      # A -- yes --> B
    r"|-\.\s*(?P<dlabel>[^>|.][^>|]*?)\s*(?P<darrow>\.->|\.-)"    # A -. yes .-> B
    r"|==\s*(?P<tlabel>[^>|=][^>|]*?)\s*(?P<tarrow>==>|===)"       # A == yes ==> B
    r"|(?P<arrow>-\.->|\.\.>|==>|-->|---)\s*(?:\|(?P<label>[^|]*)\|)?"
    r")"
    r"\s*(?P<to>[A-Za-z_][A-Za-z0-9_]*)" + _SHAPE_OPT
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
    bare-identifier scan won't pick up label text as new node IDs.

    Both halves of this had a bug that produced phantom nodes:

    * only the pipe form ``A -->|yes| B`` was stripped, so a label in the
      inline form survived as a bare identifier;
    * the asymmetric-shape alternative ``>.+?\]`` matched across an arrow —
      in ``A{X} -- yes --> B[Accept]`` it consumed ``> B[Accept]``, leaving
      ``A -- yes --`` with no arrow and no target. The edge regex then failed
      to match the stripped line, so the caller's edge-collapsing pass became
      a no-op and ``yes`` was promoted to a process box.

    Net effect on a four-node decision tree: six nodes, two of them the words
    "yes" and "no". ASCII labels only — CJK never reached the scan, which is
    why it survived a Chinese-language test suite.
    """
    # Inline edge labels first, keeping the arrow so the edge stays parseable.
    cleaned = re.sub(r"--\s*[^>|\-][^>|]*?\s*(-->|---)", r"\1", line)
    cleaned = re.sub(r"-\.\s*[^>|.][^>|]*?\s*(\.->|\.-)", r"\1", cleaned)
    cleaned = re.sub(r"==\s*[^>|=][^>|]*?\s*(==>|===)", r"\1", cleaned)
    # Then pipe-delimited edge labels.
    cleaned = re.sub(r"\|[^|]*\|", " ", cleaned)
    # Then shape bodies. The asymmetric form must not start at an arrowhead,
    # and must not span past its own closing bracket.
    cleaned = re.sub(r"\[\[.+?\]\]|\[\/.+?\/\]|\(\[.+?\]\)|\(\(.+?\)\)|"
                     r"\[.+?\]|\{.+?\}|\(.+?\)|(?<![-=.>])>[^\]]*?\]",
                     " ", cleaned)
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
            arrow = (m.group("arrow") or m.group("iarrow") or m.group("darrow")
                     or m.group("tarrow") or "-->")
            style = "dashed" if "." in arrow else "solid"
            label = (m.group("label") or m.group("ilabel") or m.group("dlabel")
                     or m.group("tlabel") or "").strip()
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
        bare_line = _EDGE_RE.sub(
            lambda m: f" {m.group('from')} {m.group('to')} ", bare_line
        )
        bare_line = re.sub(r"-\.->|\.\.>|==>|-->|---", " ", bare_line)
        for token in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", bare_line):
            if token in _RESERVED_TOKENS:
                continue
            if token not in nodes:
                nodes[token] = _ParsedNode(id=token, kind="process", text=token)

    return list(nodes.values()), edges


# ---------------------------------------------------------------------------
# Public auto-layout entrypoint
# ---------------------------------------------------------------------------


def autolayout_flowchart(fc: dict[str, Any]) -> dict[str, Any]:
    """Fill a flowchart block's geometry in place. Returns the same dict.

    Accepts the flat dict form (``Slide.flowchart.model_dump()``). A ``mermaid``
    string is parsed for kinds, labels and edges; explicit ``nodes``/``edges``
    are used as given. ``layout="manual"`` with every node positioned is left
    alone.

    Beyond ``x``/``y``/``w``/``h`` per node this injects three computed keys the
    emitter needs and the tool schema deliberately does not carry, so the model
    never tries to author them: ``points`` (the routed polyline, ending at the
    arrowhead), ``label_x``/``label_y`` per edge, and ``font_pt`` on the block.
    They are documented in ``skills/flowchart/references/flowchart_authoring.md``.
    """
    if not fc:
        return fc

    layout_mode = fc.get("layout", "auto")
    direction = str(fc.get("direction") or "auto").upper()
    slot = (
        float(fc.get("x", 0.55)),
        float(fc.get("y", 1.25)),
        float(fc.get("w", 12.2)),
        float(fc.get("h", 5.6)),
    )
    default_w = float(fc.get("node_w", 2.2))
    default_h = float(fc.get("node_h", 0.9))

    kind = str(fc.get("kind") or "flow").lower()
    outline = (fc.get("outline") or "").strip()
    mermaid_source = (fc.get("mermaid") or "").strip()
    nodes: list[dict[str, Any]] = list(fc.get("nodes") or [])
    edges: list[dict[str, Any]] = list(fc.get("edges") or [])

    # A mindmap and a structure chart are the same geometry problem as a flow
    # with a different reading convention, so they share the engine and differ
    # only in rank direction and whether the connectors carry arrowheads.
    if outline and kind in ("mindmap", "structure"):
        geometry = (
            graph_layout.layout_mindmap(outline, box=slot)
            if kind == "mindmap"
            else graph_layout.layout_structure(outline, box=slot)
        )
        laid_nodes, laid_edges = geometry.as_slide_dicts()
        fc["nodes"] = laid_nodes
        fc["edges"] = laid_edges
        fc["font_pt"] = round(geometry.font_pt, 1)
        fc["direction"] = geometry.rankdir
        fc["engine"] = geometry.engine
        return fc

    if not nodes and mermaid_source:
        parsed_nodes, parsed_edges = parse_mermaid_flowchart(mermaid_source)
        nodes = [
            {"id": n.id, "kind": n.kind, "text": n.text, "fill": "", "stroke": ""}
            for n in parsed_nodes
        ]
        edges = [
            {"from_id": e.from_id, "to_id": e.to_id, "label": e.label,
             "style": e.style, "arrow": "end"}
            for e in parsed_edges
        ]

    if not nodes:
        fc["nodes"] = []
        fc["edges"] = []
        return fc

    if layout_mode == "manual" and all(
        n.get("x") is not None and n.get("y") is not None for n in nodes
    ):
        for n in nodes:
            n["w"] = float(n.get("w") or default_w)
            n["h"] = float(n.get("h") or default_h)
        fc["nodes"] = nodes
        fc["edges"] = edges
        return fc

    try:
        geometry = graph_layout.layout_graph(
            nodes, edges, box=slot,
            rankdir=None if direction == "AUTO" else direction,
        )
    except graph_layout.GraphvizNotInstalled:
        geometry = graph_layout.stack_layout(nodes, edges, box=slot)
    except RuntimeError:
        # A layout engine that fails should cost this diagram its routing, not
        # the slide it sits on.
        geometry = graph_layout.stack_layout(nodes, edges, box=slot)

    laid_nodes, laid_edges = geometry.as_slide_dicts()
    fc["nodes"] = laid_nodes
    fc["edges"] = laid_edges
    fc["font_pt"] = round(geometry.font_pt, 1)
    fc["direction"] = geometry.rankdir
    fc["engine"] = geometry.engine
    return fc


#: The emitter's content area. Duplicated here because the layout has to know
#: the box before the emitter draws anything, and the emitter derives the
#: bullet rail back from the box this sets — so the box is the single source.
_BODY = (0.6, 1.52, 12.133, 5.40)
_RAIL_SHARE = 0.38
_RAIL_GAP = 0.35


def prepare_flowchart_slides(slides: list[dict[str, Any]]) -> list[str]:
    """Iterate ``slides`` (already-dumped Pydantic dicts) and auto-layout
    every diagram block in place. Safe to call on slides without one — they
    are left untouched.

    A slide carrying both bullets and a diagram gets the diagram laid out in
    the right-hand column, because a chain is inherently a band and the space
    beside it is where the takeaways belong. The emitter reads the resulting
    ``x`` back to size the bullet rail.

    Returns one string per slide whose diagram could not be laid out, empty
    when everything succeeded. **The guard is per slide on purpose**: a single
    ``try`` around the whole loop let one unlayoutable diagram abort every
    later slide's layout too, and the caller still reported success — so a
    ten-slide deck could come back with nine silently diagram-less slides.
    A slide that fails is emptied of nodes and edges rather than left holding
    half-computed geometry.
    """
    failures: list[str] = []
    for index, slide in enumerate(slides, start=1):
        fc = slide.get("flowchart") if isinstance(slide, dict) else None
        if not fc:
            continue
        bullets = [b for b in (slide.get("bullets") or []) if b]
        if bullets and str(slide.get("layout") or "bullets") not in ("title", "section", "chart"):
            bx, by, bw, bh = _BODY
            rail = bw * _RAIL_SHARE
            fc["x"] = round(bx + rail + _RAIL_GAP, 3)
            fc["w"] = round(bw - rail - _RAIL_GAP, 3)
            fc["y"] = by
            fc["h"] = bh
        try:
            autolayout_flowchart(fc)
        except Exception as exc:  # noqa: BLE001 — one slide must not cost the rest
            fc["nodes"] = []
            fc["edges"] = []
            failures.append(f"slide {index}: {type(exc).__name__}: {exc}")
    return failures


__all__ = [
    "MermaidCli",
    "MermaidNotInstalled",
    "autolayout_flowchart",
    "parse_mermaid_flowchart",
    "prepare_flowchart_slides",
    "render_mermaid_to_file",
    "resolve_mermaid_cli",
]
