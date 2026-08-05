"""Native flowchart-shape contract for write_pptx.

A single assertion: a `Slide.flowchart` block with mermaid source MUST
produce real OOXML `flowChart*` shapes and connector lines — not the
"vertical text box" failure mode that motivated the feature.

Skipped when mmdc or node is unavailable (matches the same gating in
``tests/test_document_writers.py``).
"""

from __future__ import annotations

import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path

import pytest


_NODE_BIN = shutil.which("node")
_MMDC_LOCAL = Path(__file__).resolve().parent.parent / "node_modules" / ".bin" / "mmdc"
_MMDC = str(_MMDC_LOCAL) if _MMDC_LOCAL.is_file() else shutil.which("mmdc")


@pytest.mark.skipif(_NODE_BIN is None, reason="node not available")
@pytest.mark.skipif(_MMDC is None, reason="mmdc (mermaid-cli) not installed")
def test_write_pptx_emits_native_flowchart_shapes(tmp_path, monkeypatch):
    """write_pptx + Slide.flowchart -> real flowChart* + line shapes."""
    # Route outputs into the test's tmp_path so we don't pollute outputs/.
    from legal_helper import config as _cfg
    settings = _cfg.current_settings()
    monkeypatch.setattr(settings, "outputs_dir", tmp_path, raising=False)

    from legal_helper.tools.documents import (
        Slide, SlideFlowchart, write_pptx,
    )

    inner = getattr(write_pptx, "__wrapped__", None) or write_pptx
    slides = [
        Slide(
            title="flowchart smoke",
            layout="bullets",
            bullets=[],
            flowchart=SlideFlowchart(
                mermaid=(
                    "flowchart TB\n"
                    "  S([Start]) --> A[Process A]\n"
                    "  A --> D{OK?}\n"
                    "  D -->|yes| E([End])\n"
                    "  D -->|no| A"
                ),
                layout="auto",
                direction="TB",
            ),
        ),
    ]
    out_path = Path(inner(filename="test_flowchart", title="t", slides=slides))
    assert out_path.is_file(), f"write_pptx returned non-existent path: {out_path}"

    with zipfile.ZipFile(out_path) as zf:
        xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")

    counts = Counter(re.findall(r'prst="([^"]+)"', xml))
    # 2 terminators (Start, End), 1 process (Process A), 1 decision (OK?),
    # 4 line connectors (4 edges). Allow >= so future shape decorations
    # don't break this test.
    assert counts.get("flowChartTerminator", 0) >= 2, counts
    assert counts.get("flowChartProcess", 0) >= 1, counts
    assert counts.get("flowChartDecision", 0) >= 1, counts
    assert counts.get("line", 0) >= 3, counts
