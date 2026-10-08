"""Diagram pictures: a PNG or SVG for a Word document or a chat reply.

A diagram that belongs on a slide is not drawn here. ppt-master draws it inside
the deck as native shapes (``tools/decks.create_deck``); this module makes the
picture a user pastes into a memo or receives as an image.

Mermaid is the authoring syntax because models write it fluently, but it is
not the renderer for the common cases. ``flowchart`` / ``graph`` and
``mindmap`` sources are parsed here and drawn by Graphviz ``dot`` with a CJK
font, so a process map needs no Node, no Chrome and no network. Every other
Mermaid type (sequence, class, state, ER, gantt, pie…) goes to mermaid-cli,
and D2 handles nested containers (:func:`.graph_layout.render_d2_to_file`).

``mmdc`` is a JavaScript file with a ``#!/usr/bin/env node`` shebang, so the
launching process's PATH decides whether it runs at all. The server is started
by an absolute interpreter path and its PATH has no conda ``bin``, which made
every Mermaid render fail with exit 127. ``resolve_mermaid_cli`` runs it as
``node <cli.js>`` through :func:`.writers._node.find_node`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

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
    """Render ``source`` (a Mermaid string) to ``output_path`` with mermaid-cli.

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
        # what made the degraded path unreachable: callers catch
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
# Mermaid flowchart parser
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
    #: ``none`` for an open link (``---``, ``===``, ``-.-``): a relationship
    #: drawn without a direction.
    arrow: str = "end"
    thick: bool = False


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
    r"|(?P<arrow>-\.->|\.\.>|==>|-->|---|===|-\.-)\s*(?:\|(?P<label>[^|]*)\|)?"
    r")"
    r"\s*(?P<to>[A-Za-z_][A-Za-z0-9_]*)" + _SHAPE_OPT
)

#: Arrow tokens that draw a line without an arrowhead.
_OPEN_LINKS = frozenset({"---", "===", "-.-", ".-"})


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
    * the asymmetric-shape alternative ``>.+?\\]`` matched across an arrow —
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


def _iter_edges(line: str):
    """Every edge on a line, chains included.

    ``A --> B --> C`` is two edges. A plain ``finditer`` consumes ``B`` as the
    first edge's target and so never sees the second edge at all; resuming the
    search at each target keeps the chain.
    """
    pos = 0
    while True:
        match = _EDGE_RE.search(line, pos)
        if match is None:
            return
        yield match
        pos = match.start("to")


def parse_mermaid_flowchart(source: str) -> tuple[list[_ParsedNode], list[_ParsedEdge]]:
    """Minimal parser for ``flowchart`` / ``graph`` blocks.

    Supports node shapes ``[]``, ``{}``, ``()``, ``(())``, ``([])``,
    ``[[]]``, ``[//]``, ``>]``; edges ``-->``, ``-.->``, ``==>``, ``---``,
    ``===``, ``-.-`` with an optional ``|label|`` or inline label; and chains
    ``A --> B --> C``. Subgraphs, ``classDef`` and styling lines are skipped
    here (see :func:`parse_mermaid_subgraphs`).
    """
    nodes: dict[str, _ParsedNode] = {}
    edges: list[_ParsedEdge] = []

    def _record_node(node_id: str, kind: str, text: str) -> None:
        text = _clean_label(text)
        existing = nodes.get(node_id)
        if existing is None:
            nodes[node_id] = _ParsedNode(id=node_id, kind=kind, text=text or node_id)
        else:
            # First explicit shape wins; only update text if missing.
            if (not existing.text or existing.text == existing.id) and text:
                existing.text = text
            if existing.kind == "process" and kind != "process":
                existing.kind = kind

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
        for m in _iter_edges(line):
            arrow = (m.group("arrow") or m.group("iarrow") or m.group("darrow")
                     or m.group("tarrow") or "-->")
            label = (m.group("label") or m.group("ilabel") or m.group("dlabel")
                     or m.group("tlabel") or "").strip()
            edges.append(_ParsedEdge(
                from_id=m.group("from"),
                to_id=m.group("to"),
                label=_clean_label(label),
                style="dashed" if "." in arrow else "solid",
                arrow="none" if arrow in _OPEN_LINKS else "end",
                thick=arrow.startswith("="),
            ))
        # 3) Bare references — only after stripping label/shape bodies so
        #    we don't promote "yes", "Start", etc. to standalone nodes.
        bare_line = _strip_constructs(line)
        # Also remove arrows so the surrounding identifiers don't double-count
        # already-handled edges.
        bare_line = _EDGE_RE.sub(
            lambda m: f" {m.group('from')} {m.group('to')} ", bare_line
        )
        bare_line = re.sub(r"-\.->|\.\.>|==>|-->|---|===|-\.-", " ", bare_line)
        for token in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", bare_line):
            if token in _RESERVED_TOKENS:
                continue
            if token not in nodes:
                nodes[token] = _ParsedNode(id=token, kind="process", text=token)

    return list(nodes.values()), edges


def _clean_label(text: str) -> str:
    """Label text as a reader should see it.

    Mermaid allows ``A["quoted"]``, markdown strings in backticks and ``<br>``
    line breaks; none of those characters belong in the drawn label.
    """
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return text.replace("#quot;", '"').strip()


# ---------------------------------------------------------------------------
# Mermaid → DOT
# ---------------------------------------------------------------------------


@dataclass
class _Cluster:
    id: str
    title: str
    parent: str | None
    members: list[str] = field(default_factory=list)


def mermaid_kind(source: str) -> str:
    """The diagram type a Mermaid source declares on its first real line."""
    for raw in (source or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("%%") or line.startswith("---"):
            continue
        return line.split()[0].rstrip(":").lower()
    return ""


def mermaid_direction(source: str) -> str:
    """Graphviz ``rankdir`` for a flowchart header (``TD`` is Mermaid's ``TB``)."""
    for raw in (source or "").splitlines():
        parts = raw.strip().split()
        if parts and parts[0].lower() in {"flowchart", "graph"}:
            if len(parts) > 1:
                direction = parts[1].upper().rstrip(";")
                return {"TD": "TB"}.get(direction, direction) if direction in {
                    "TB", "TD", "BT", "LR", "RL"
                } else "TB"
            return "TB"
    return "TB"


def parse_mermaid_subgraphs(source: str) -> tuple[list[_Cluster], dict[str, str]]:
    """Subgraph blocks as clusters, and which cluster each node belongs to.

    A node belongs to the innermost subgraph it is first mentioned in — the
    reading a swimlane author intends when a lane lists its steps.
    """
    clusters: list[_Cluster] = []
    membership: dict[str, str] = {}
    stack: list[str] = []
    for raw in (source or "").splitlines():
        line = raw.strip()
        low = line.lower()
        if not line or line.startswith("%%"):
            continue
        if low.startswith("subgraph"):
            rest = line[len("subgraph"):].strip()
            named = re.match(r"^([A-Za-z_][\w-]*)\s*\[(.*)\]\s*$", rest)
            if named:
                cid, title = named.group(1), _clean_label(named.group(2))
            elif re.fullmatch(r"[A-Za-z_][\w-]*", rest):
                cid, title = rest, rest
            else:
                cid, title = f"sg{len(clusters)}", _clean_label(rest)
            clusters.append(_Cluster(id=cid, title=title, parent=stack[-1] if stack else None))
            stack.append(cid)
            continue
        if low == "end" or low.startswith("end "):
            if stack:
                stack.pop()
            continue
        if not stack:
            continue
        line_nodes, _ = parse_mermaid_flowchart(line)
        for node in line_nodes:
            if node.id not in membership:
                membership[node.id] = stack[-1]
    for cluster in clusters:
        cluster.members = [n for n, c in membership.items() if c == cluster.id]
    return clusters, membership


#: Fill, outline, text and edge colours per Mermaid theme name.
_PALETTES = {
    "default": {"fill": "#EAF1FB", "decision": "#FFF4D6", "terminal": "#DDE8F6",
                "line": "#1F4E79", "text": "#17202A", "edge": "#44546A", "cluster": "#F6F8FB"},
    "neutral": {"fill": "#F2F2F2", "decision": "#E6E6E6", "terminal": "#E0E0E0",
                "line": "#4D4D4D", "text": "#1A1A1A", "edge": "#595959", "cluster": "#FAFAFA"},
    "forest": {"fill": "#E8F4E5", "decision": "#FFF6D5", "terminal": "#D4EBCF",
               "line": "#2E6B30", "text": "#16301A", "edge": "#3E5E40", "cluster": "#F4FAF2"},
    "dark": {"fill": "#2B3A4A", "decision": "#4A3F28", "terminal": "#24425E",
             "line": "#9DB7D5", "text": "#F2F5F8", "edge": "#B8C4D0", "cluster": "#1E2A36"},
}

#: How a parsed node kind is drawn.
_KIND_ATTRS = {
    "process": {"shape": "box"},
    "terminator": {"shape": "box", "style": "rounded,filled"},
    "decision": {"shape": "diamond"},
    "io": {"shape": "parallelogram"},
    "subprocess": {"shape": "box", "peripheries": "2"},
    "data": {"shape": "note"},
}

#: Label width before wrapping, in ems (one em per Han glyph). A diamond
#: spends most of its box on its corners, so it wraps sooner.
_WRAP_EM = {"decision": 7.0}
_DEFAULT_WRAP_EM = 11.0
_EDGE_WRAP_EM = 9.0


def _dot_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _wrapped(text: str, max_em: float) -> str:
    """``text`` broken into lines of about equal width, none over ``max_em``.

    Balanced rather than greedy: a greedy fill of an eleven-glyph label at ten
    ems leaves one character alone on the second line ("…运营 / 者"), which
    reads as a mistake. The line count is fixed first, then each line takes an
    equal share.
    """
    lines: list[str] = []
    for para in (text or "").split("\n"):
        width = graph_layout.display_width(para)
        if width <= max_em:
            lines.append(para)
            continue
        count = -(-width // max_em)  # ceiling division on floats
        lines.extend(ln.strip() for ln in graph_layout.wrap_label(para, width / count + 0.5))
    return "\n".join(lines)


def _graph_header(rankdir: str, theme: str, background: str, font: str) -> list[str]:
    palette = _PALETTES.get(theme, _PALETTES["default"])
    bg = "transparent" if background.lower() in {"transparent", "none"} else background
    return [
        "digraph G {",
        f"  graph [rankdir={rankdir}, bgcolor={_dot_quote(bg)}, pad=0.3, nodesep=0.45, "
        f"ranksep=0.55, dpi=180, fontname={_dot_quote(font)}, fontsize=12, "
        f"fontcolor={_dot_quote(palette['text'])}];",
        f"  node [fontname={_dot_quote(font)}, fontsize=13, shape=box, style=filled, "
        f"fillcolor={_dot_quote(palette['fill'])}, color={_dot_quote(palette['line'])}, "
        f"fontcolor={_dot_quote(palette['text'])}, penwidth=1.3, margin=\"0.22,0.1\"];",
        f"  edge [fontname={_dot_quote(font)}, fontsize=11, color={_dot_quote(palette['edge'])}, "
        f"fontcolor={_dot_quote(palette['text'])}, penwidth=1.2, arrowsize=0.8];",
    ]


def flowchart_to_dot(
    source: str, *, theme: str = "default", background: str = "white", font: str | None = None
) -> str:
    """DOT for a Mermaid ``flowchart`` / ``graph`` source.

    Raises ``ValueError`` when the source yields no nodes, so the caller can
    hand syntax this parser does not cover to mermaid-cli instead of drawing
    an empty picture.
    """
    nodes, edges = parse_mermaid_flowchart(source)
    if not nodes:
        raise ValueError("no nodes could be parsed from the flowchart source")
    font = font or graph_layout.cjk_font_family()
    palette = _PALETTES.get(theme, _PALETTES["default"])
    clusters, membership = parse_mermaid_subgraphs(source)
    out = _graph_header(mermaid_direction(source), theme, background, font)

    def node_line(node: _ParsedNode) -> str:
        attrs = dict(_KIND_ATTRS.get(node.kind, _KIND_ATTRS["process"]))
        if node.kind == "decision":
            attrs["fillcolor"] = palette["decision"]
        elif node.kind == "terminator":
            attrs["fillcolor"] = palette["terminal"]
        attrs["label"] = _wrapped(node.text or node.id, _WRAP_EM.get(node.kind, _DEFAULT_WRAP_EM))
        body = ", ".join(f"{k}={_dot_quote(v)}" for k, v in attrs.items())
        return f"{_dot_quote(node.id)} [{body}];"

    by_id = {n.id: n for n in nodes}
    children: dict[str | None, list[_Cluster]] = {}
    for cluster in clusters:
        children.setdefault(cluster.parent, []).append(cluster)

    def emit_cluster(cluster: _Cluster, depth: int) -> None:
        pad = "  " * (depth + 1)
        out.append(f"{pad}subgraph {_dot_quote('cluster_' + cluster.id)} {{")
        out.append(
            f"{pad}  label={_dot_quote(cluster.title)}; style=\"rounded,filled\"; "
            f"fillcolor={_dot_quote(palette['cluster'])}; color={_dot_quote(palette['edge'])};"
        )
        for member in cluster.members:
            if member in by_id:
                out.append(f"{pad}  {node_line(by_id[member])}")
        for child in children.get(cluster.id, []):
            emit_cluster(child, depth + 1)
        out.append(f"{pad}}}")

    for cluster in children.get(None, []):
        emit_cluster(cluster, 0)
    for node in nodes:
        if node.id not in membership:
            out.append(f"  {node_line(node)}")
    for edge in edges:
        attrs: dict[str, str] = {}
        if edge.label:
            attrs["label"] = " " + _wrapped(edge.label, _EDGE_WRAP_EM) + " "
        if edge.style == "dashed":
            attrs["style"] = "dashed"
        if edge.arrow == "none":
            attrs["arrowhead"] = "none"
        if edge.thick:
            attrs["penwidth"] = "2.2"
        body = ", ".join(f"{k}={_dot_quote(v)}" for k, v in attrs.items())
        suffix = f" [{body}]" if body else ""
        out.append(f"  {_dot_quote(edge.from_id)} -> {_dot_quote(edge.to_id)}{suffix};")
    out.append("}")
    return "\n".join(out)


_MINDMAP_SHAPE_RE = re.compile(
    r"^(?:[A-Za-z_][\w-]*)?\s*"
    r"(?:\(\((?P<circle>.+)\)\)|\)\)(?P<bang>.+)\(\(|\)(?P<cloud>.+)\(|\{\{(?P<hex>.+)\}\}"
    r"|\((?P<round>.+)\)|\[(?P<square>.+)\])\s*$"
)


def parse_mermaid_mindmap(source: str) -> tuple[list[tuple[str, int]], list[tuple[int, int]]]:
    """``([(label, depth)], [(parent index, child index)])`` for a ``mindmap``.

    Mermaid nests a mind map by indentation alone, relative rather than fixed:
    a child is any line indented deeper than the line it follows.
    """
    nodes: list[tuple[str, int]] = []
    edges: list[tuple[int, int]] = []
    stack: list[tuple[int, int]] = []  # (indent, node index)
    started = False
    for raw in (source or "").splitlines():
        if not raw.strip() or raw.strip().startswith("%%"):
            continue
        if not started:
            if raw.strip().lower().startswith("mindmap"):
                started = True
            continue
        stripped = raw.strip()
        if stripped.startswith("::icon(") or stripped.startswith("::"):
            continue
        stripped = re.sub(r":::\s*[\w\s-]+$", "", stripped).strip()
        match = _MINDMAP_SHAPE_RE.match(stripped)
        label = next((g for g in match.groups() if g), stripped) if match else stripped
        label = _clean_label(label)
        if not label:
            continue
        indent = len(raw) - len(raw.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        index = len(nodes)
        nodes.append((label, len(stack)))
        if stack:
            edges.append((stack[-1][1], index))
        stack.append((indent, index))
    return nodes, edges


def mindmap_to_dot(
    source: str, *, theme: str = "default", background: str = "white", font: str | None = None
) -> str:
    """DOT for a Mermaid ``mindmap``: a left-to-right tree with plain ties.

    Left to right, because a ranked tree reads better on a page than the
    radial arrangement that wastes the corners; no arrowheads, because a branch
    of a mind map is a relationship, not a step.
    """
    nodes, edges = parse_mermaid_mindmap(source)
    if not nodes:
        raise ValueError("no nodes could be parsed from the mindmap source")
    font = font or graph_layout.cjk_font_family()
    palette = _PALETTES.get(theme, _PALETTES["default"])
    out = _graph_header("LR", theme, background, font)
    out.append("  edge [arrowhead=none];")
    for index, (label, depth) in enumerate(nodes):
        attrs = {"label": _wrapped(label, 10.0 if depth else 8.0)}
        if depth == 0:
            attrs |= {"shape": "ellipse", "fillcolor": palette["terminal"], "fontsize": "16",
                      "penwidth": "2"}
        elif depth == 1:
            attrs |= {"style": "rounded,filled", "fillcolor": palette["fill"], "fontsize": "14"}
        else:
            attrs |= {"style": "rounded,filled", "fillcolor": palette["cluster"]}
        body = ", ".join(f"{k}={_dot_quote(v)}" for k, v in attrs.items())
        out.append(f"  n{index} [{body}];")
    for parent, child in edges:
        out.append(f"  n{parent} -> n{child};")
    out.append("}")
    return "\n".join(out)


#: Mermaid types Graphviz draws here; every other type goes to mermaid-cli.
GRAPHVIZ_KINDS = frozenset({"flowchart", "graph", "flowchart-elk", "mindmap"})


def render_mermaid_picture(
    source: str,
    output_path: Path,
    *,
    theme: str = "default",
    background: str = "white",
) -> tuple[Path, str]:
    """Draw a Mermaid source to ``output_path``; return ``(path, engine)``.

    Flowcharts and mind maps go to Graphviz, so the common case needs no
    browser. A source this parser cannot read, or any other Mermaid type, goes
    to mermaid-cli. Raises :class:`MermaidNotInstalled` when the engine the
    source needs is missing, ``RuntimeError`` when a render fails.
    """
    kind = mermaid_kind(source)
    if kind in GRAPHVIZ_KINDS and graph_layout.graphviz_available():
        try:
            dot = (
                mindmap_to_dot(source, theme=theme, background=background)
                if kind == "mindmap"
                else flowchart_to_dot(source, theme=theme, background=background)
            )
        except ValueError:
            dot = ""
        if dot:
            return graph_layout.render_dot_to_file(dot, output_path), "graphviz"
    try:
        return render_mermaid_to_file(source, output_path, theme=theme, background=background), "mermaid-cli"
    except MermaidNotInstalled as exc:
        if kind in GRAPHVIZ_KINDS and not graph_layout.graphviz_available():
            raise MermaidNotInstalled(
                f"Neither Graphviz nor mermaid-cli is available for this {kind}: install "
                f"graphviz (`conda install -n llm graphviz`). {exc}"
            ) from exc
        raise


__all__ = [
    "GRAPHVIZ_KINDS",
    "MermaidCli",
    "MermaidNotInstalled",
    "flowchart_to_dot",
    "mermaid_direction",
    "mermaid_kind",
    "mindmap_to_dot",
    "parse_mermaid_flowchart",
    "parse_mermaid_mindmap",
    "parse_mermaid_subgraphs",
    "render_mermaid_picture",
    "render_mermaid_to_file",
    "resolve_mermaid_cli",
]
