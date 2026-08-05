#!/usr/bin/env python
"""Scan the local CAAC corpus for expired / stub / superseded entries.

For every ``source_page`` entry in
``legal_helper/rag/corpus/caac_ccar/manifest.yaml`` this script computes a set
of flags:

  pdf_stub          — markdown body is only an "附件：xxx.pdf" pointer; the
                      statute text is not available locally.
  explicit_expired  — the manifest's ``validity`` field is one of
                      失效 / 废止 / 历史版本.
  superseded        — another entry in the same title family has a later
                      ``publish_date`` and is not itself flagged expired.
                      ``superseded_by`` records the winning entry's file path.
  future_effective  — the title says "...YYYY年M月D日起施行" with a future
                      date (relative to today / ``--today``).

Default mode prints a tab-separated report to stdout plus a summary. With
``--apply`` the per-entry ``corpus_status`` (comma-separated flags) and
``superseded_by`` fields are written back into ``manifest.yaml`` in place so
``caac_local_search`` / ``retrieve_legal`` can read them. The script is
idempotent: running it again replaces these fields based on the current state
of the corpus.

Usage::

    conda run -n llm python scripts/flag_expired_corpus.py
    conda run -n llm python scripts/flag_expired_corpus.py --apply
    conda run -n llm python scripts/flag_expired_corpus.py --format json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = REPO_ROOT / "legal_helper" / "rag" / "corpus" / "caac_ccar"
MANIFEST_PATH = CORPUS_ROOT / "manifest.yaml"

EXPIRED_VALIDITY_TOKENS = ("失效", "废止", "已废止", "历史版本")
STUB_BODY_CHAR_LIMIT = 200
ATTACHMENT_REF_RE = re.compile(
    r"附件\s*[:：][\s\S]{0,400}\.(pdf|docx?|doc)[^\n]*", re.IGNORECASE
)
EFFECTIVE_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日起施行")
PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
DATE_RE = re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$")


@dataclass
class EntryReport:
    file: str
    title: str
    validity: str | None
    publish_date: str | None
    ccar_part: str | None
    flags: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    body_chars: int = 0


def _norm_title(title: str | None) -> str:
    if not title:
        return ""
    cleaned = PAREN_RE.sub("", title)
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned.strip()


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    m = DATE_RE.match(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _read_body(corpus_root: Path, rel_file: str) -> str:
    path = (corpus_root / rel_file).resolve()
    try:
        path.relative_to(corpus_root.resolve())
    except ValueError:
        return ""
    if not path.is_file():
        return ""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    parts = raw.split("## 正文", 1)
    return parts[1] if len(parts) == 2 else raw


def _is_pdf_stub(body: str) -> tuple[bool, int]:
    """Return ``(is_stub, body_chars_excluding_attachment_refs)``."""
    if not body:
        return True, 0
    has_attachment = bool(ATTACHMENT_REF_RE.search(body))
    cleaned = ATTACHMENT_REF_RE.sub("", body)
    cleaned = re.sub(r"附件\s*[:：]\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", "", cleaned)
    body_chars = len(cleaned)
    is_stub = body_chars < STUB_BODY_CHAR_LIMIT and (
        has_attachment or body_chars < 60
    )
    return is_stub, body_chars


def _future_effective(title: str | None, today: date) -> bool:
    if not title:
        return False
    m = EFFECTIVE_DATE_RE.search(title)
    if not m:
        return False
    try:
        eff = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return False
    return eff > today


def _explicit_expired(validity: str | None) -> bool:
    if not validity:
        return False
    return any(tok in validity for tok in EXPIRED_VALIDITY_TOKENS)


def _resolve_superseded(
    entry: dict[str, Any],
    family: list[dict[str, Any]],
    my_expired: bool,
) -> str | None:
    """Pick the newest entry in ``family`` that supersedes ``entry`` (or None).

    Heuristic: among entries with the same normalized title, an entry is
    superseded if another entry has a strictly later ``publish_date`` and is
    not itself explicitly expired. If multiple candidates exist, prefer the
    latest. Entries already flagged ``explicit_expired`` are eligible to be
    superseded by any later, non-expired entry.
    """
    my_date = _parse_date(entry.get("publish_date"))
    if my_date is None and not my_expired:
        return None
    best: tuple[date, str] | None = None
    for other in family:
        if other is entry:
            continue
        if _explicit_expired(other.get("validity")):
            continue
        other_date = _parse_date(other.get("publish_date"))
        if other_date is None:
            continue
        if my_date is not None and other_date <= my_date:
            continue
        cand = (other_date, str(other.get("file")))
        if best is None or cand > best:
            best = cand
    return best[1] if best else None


def classify_entries(
    entries: Iterable[dict[str, Any]],
    corpus_root: Path,
    today: date,
) -> list[EntryReport]:
    items = [e for e in entries if e.get("file_kind") == "source_page"]
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in items:
        families[_norm_title(e.get("title"))].append(e)

    reports: list[EntryReport] = []
    for e in items:
        body = _read_body(corpus_root, str(e.get("file")))
        is_stub, body_chars = _is_pdf_stub(body)
        rep = EntryReport(
            file=str(e.get("file")),
            title=str(e.get("title") or ""),
            validity=e.get("validity"),
            publish_date=e.get("publish_date"),
            ccar_part=e.get("ccar_part"),
            body_chars=body_chars,
        )
        if is_stub:
            rep.flags.append("pdf_stub")
        if _explicit_expired(e.get("validity")):
            rep.flags.append("explicit_expired")
        if _future_effective(e.get("title"), today):
            rep.flags.append("future_effective")
        family = families[_norm_title(e.get("title"))]
        if len(family) > 1:
            superseded_by = _resolve_superseded(
                e, family, my_expired="explicit_expired" in rep.flags
            )
            if superseded_by:
                rep.flags.append("superseded")
                rep.superseded_by = superseded_by
        reports.append(rep)
    return reports


def _write_back(
    manifest_path: Path,
    raw_manifest: dict[str, Any],
    reports: list[EntryReport],
) -> int:
    by_file = {r.file: r for r in reports}
    files = raw_manifest.get("files") or []
    touched = 0
    for entry in files:
        if entry.get("file_kind") != "source_page":
            continue
        rep = by_file.get(str(entry.get("file")))
        if rep is None:
            continue
        new_status = ",".join(rep.flags) if rep.flags else None
        old_status = entry.get("corpus_status")
        old_superseded = entry.get("superseded_by")
        changed = False
        if new_status != old_status:
            if new_status is None:
                entry.pop("corpus_status", None)
            else:
                entry["corpus_status"] = new_status
            changed = True
        if rep.superseded_by != old_superseded:
            if rep.superseded_by is None:
                entry.pop("superseded_by", None)
            else:
                entry["superseded_by"] = rep.superseded_by
            changed = True
        if changed:
            touched += 1
    if touched:
        manifest_path.write_text(
            yaml.safe_dump(raw_manifest, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    return touched


def _print_report(reports: list[EntryReport], fmt: str) -> None:
    flagged = [r for r in reports if r.flags]
    if fmt == "json":
        payload = {
            "total": len(reports),
            "flagged": len(flagged),
            "by_flag": _flag_counts(reports),
            "entries": [
                {
                    "file": r.file,
                    "title": r.title,
                    "ccar_part": r.ccar_part,
                    "validity": r.validity,
                    "publish_date": r.publish_date,
                    "body_chars": r.body_chars,
                    "flags": r.flags,
                    "superseded_by": r.superseded_by,
                }
                for r in flagged
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    counts = _flag_counts(reports)
    print(f"# CAAC corpus flag report — {len(reports)} source_page entries")
    print(f"# flagged: {len(flagged)}  ", end="")
    print(
        "  ".join(
            f"{k}={counts[k]}"
            for k in ("pdf_stub", "explicit_expired", "superseded", "future_effective")
        )
    )
    print("file\tflags\tvalidity\tpublish_date\tccar_part\tsuperseded_by\ttitle")
    for r in flagged:
        print(
            "\t".join(
                [
                    r.file,
                    ",".join(r.flags),
                    r.validity or "",
                    r.publish_date or "",
                    r.ccar_part or "",
                    r.superseded_by or "",
                    r.title,
                ]
            )
        )


def _flag_counts(reports: list[EntryReport]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for r in reports:
        for f in r.flags:
            counts[f] += 1
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST_PATH,
        help="Manifest path (default: legal_helper/rag/corpus/caac_ccar/manifest.yaml)",
    )
    p.add_argument(
        "--corpus-root",
        type=Path,
        default=CORPUS_ROOT,
        help="Corpus root containing 'documents/...' (default sibling of the manifest)",
    )
    p.add_argument(
        "--today",
        type=str,
        default=None,
        help="Override today's date (YYYY-MM-DD) for future_effective tests",
    )
    p.add_argument(
        "--format",
        choices=("tsv", "json"),
        default="tsv",
        help="Report format when not applying",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write corpus_status / superseded_by fields back into the manifest",
    )
    args = p.parse_args(argv)

    if args.today:
        parsed = _parse_date(args.today)
        if parsed is None:
            print(f"--today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
            return 2
        today = parsed
    else:
        today = date.today()

    raw_manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8")) or {}
    files = raw_manifest.get("files") or []
    reports = classify_entries(files, args.corpus_root, today)

    if args.apply:
        touched = _write_back(args.manifest, raw_manifest, reports)
        counts = _flag_counts(reports)
        print(
            f"updated {touched} entries in {args.manifest}",
            f"(flagged total: {sum(1 for r in reports if r.flags)} /"
            f" {len(reports)};",
            "  ".join(f"{k}={v}" for k, v in counts.items()) + ")",
        )
    else:
        _print_report(reports, args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
