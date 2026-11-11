"""Live health check of every research source: search, fetch, read a provision.

Connector failures used to be invisible: a source that 404'd or returned an
empty body came back as an ``{"error": …}`` payload, the model fell back to
web search, and nothing told anyone (EUR-Lex judgments 404'd from July to
October 2026, CourtListener snippets were empty from the first day). This
probe runs the real request path of each source against a known document and
says, per source, what works.

    python -m legal_helper.research.health            # every source
    python -m legal_helper.research.health CN EU      # some jurisdictions
    python -m legal_helper.research.health --source us.ecfr

``serve`` runs it in the background at startup and logs the summary.
Each source declares its probe as ``Source.probe``:
``{"query": …, "kind": …, "ref": <native id to fetch>, "section": <label>}``;
a source without one is reported as unprobed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from .base import Source, SourceUnavailable
from .registry import all_sources, sources_for
from .sections import find_section


def probe_source(src: Source) -> dict[str, Any]:
    spec = getattr(src, "probe", None)
    out: dict[str, Any] = {"source": src.id, "jurisdictions": list(src.jurisdictions)}
    if not spec:
        out["status"] = "unprobed"
        return out
    missing = src.missing_keys()
    if missing:
        out["status"] = "needs_key"
        out["detail"] = f"{', '.join(missing)} not set. {src.auth_help}".strip()
        return out
    steps: dict[str, Any] = {}
    t0 = time.monotonic()
    try:
        if spec.get("query"):
            t = time.monotonic()
            hits = src.search(spec["query"], kind=spec.get("kind", "any"), mode=spec.get("mode", "keyword"), limit=3)
            steps["search"] = {"ok": bool(hits), "hits": len(hits), "s": round(time.monotonic() - t, 1)}
            if not hits:
                raise RuntimeError(f"search {spec['query']!r} returned nothing")
        native = spec.get("ref")
        if native and not spec.get("search_only"):
            t = time.monotonic()
            doc = src.fetch(native)
            chars = len(doc.text or "")
            floor = int(spec.get("min_chars", 200))
            steps["fetch"] = {"ok": chars >= floor, "chars": chars, "s": round(time.monotonic() - t, 1)}
            if chars < floor:
                raise RuntimeError(f"fetch {native!r} returned {chars} chars: {doc.metadata.get('no_text_reason', '')}")
            if spec.get("section"):
                # The same resolution legal_fetch uses (labels, keys, supplied sections).
                from .tools import _sections_of

                found = find_section(_sections_of(doc), spec["section"])
                steps["section"] = {"ok": bool(found), "label": spec["section"]}
                if not found:
                    raise RuntimeError(f"provision {spec['section']!r} not found in {native!r}")
        out["status"] = "ok"
    except SourceUnavailable as exc:
        out["status"] = "unavailable"
        out["detail"] = str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 — a probe reports, never raises
        out["status"] = "failed"
        out["detail"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    out["steps"] = steps
    out["seconds"] = round(time.monotonic() - t0, 1)
    return out


def probe_all(sources: Optional[list[Source]] = None, *, workers: int = 6) -> list[dict[str, Any]]:
    srcs = sources if sources is not None else all_sources()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(srcs) or 1))) as pool:
        return list(pool.map(probe_source, srcs))


def summary_line(results: list[dict[str, Any]]) -> str:
    by: dict[str, list[str]] = {}
    for r in results:
        by.setdefault(r["status"], []).append(r["source"])
    parts = [f"{k} {len(v)}" for k, v in sorted(by.items())]
    bad = [f"{r['source']} ({r['status']}: {r.get('detail', '')[:120]})" for r in results if r["status"] in ("failed", "unavailable")]
    line = "research sources: " + ", ".join(parts)
    if bad:
        line += " | not working: " + "; ".join(bad)
    return line


def run_in_background(log) -> None:
    """Probe every source on a daemon thread and hand the summary to ``log``."""
    import threading

    def _run() -> None:
        try:
            results = probe_all()
            log(summary_line(results))
            try:
                from ..config import load_settings
                from pathlib import Path

                path = Path(load_settings().state_dir) / "legal_docs" / "health.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                            "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            log(f"research sources: health check failed to run ({exc!s})")

    threading.Thread(target=_run, name="research-health", daemon=True).start()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("jurisdictions", nargs="*")
    ap.add_argument("--source", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.source:
        srcs = [s for s in all_sources() if s.id in args.source]
    elif args.jurisdictions:
        seen: dict[str, Source] = {}
        for j in args.jurisdictions:
            for s in sources_for(j, include_secondary=True):
                seen[s.id] = s
        srcs = list(seen.values())
    else:
        srcs = all_sources()
    results = probe_all(srcs)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        for r in sorted(results, key=lambda r: r["source"]):
            steps = " ".join(f"{k}:{'ok' if v.get('ok') else 'FAIL'}" for k, v in (r.get("steps") or {}).items())
            print(f"{r['status']:<11} {r['source']:<24} {r.get('seconds', 0):>5}s  {steps}  {r.get('detail', '')}")
        print(summary_line(results))
    return 0 if all(r["status"] in ("ok", "needs_key", "unprobed") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
