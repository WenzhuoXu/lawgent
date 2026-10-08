"""Compare two Word documents into one carrying native tracked changes.

LibreOffice's own compare (``.uno:CompareDocuments``) does the diff — it runs
headless here in about a second, works at character granularity on Han text
(签字 → 盖章 is a two-character change, not a rewritten sentence), and writes
real ``w:ins`` / ``w:del`` marks Word accepts and rejects. ``uno`` lives in the
system Python rather than the conda env, so :mod:`._lo_uno` runs there as a
subprocess.

Every result is gated by the round-trip contract, checked on the output's XML:
accepting every change must give the revised document's text and rejecting
every change the original's. Empty paragraphs are ignored in that check —
LibreOffice's DOCX export drops the tracked paragraph mark of a wholly inserted
paragraph, so a reject leaves an empty paragraph behind; that is reported, not
hidden.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

_SCRIPT = Path(__file__).with_name("_lo_uno.py")


class CompareUnavailable(RuntimeError):
    """No LibreOffice, or no Python with its ``uno`` bridge."""


@functools.lru_cache(maxsize=1)
def uno_python() -> Optional[str]:
    """A Python interpreter that can ``import uno`` (env override first)."""
    candidates = [
        os.getenv("LEGAL_HELPER_UNO_PYTHON") or "",
        "/usr/lib/libreoffice/program/python",
        "/usr/bin/python3",
        shutil.which("python3") or "",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            subprocess.run(
                [candidate, "-I", "-c", "import uno"],
                check=True,
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return candidate
    return None


def run_lo_uno(command: str, paths: list[Path], *, author: str = "", timeout: float = 180) -> None:
    from .soffice import soffice_binary

    soffice = soffice_binary()
    if not soffice:
        raise CompareUnavailable("LibreOffice (soffice) is not on PATH")
    python = uno_python()
    if not python:
        raise CompareUnavailable(
            "no Python with LibreOffice's uno module was found; set LEGAL_HELPER_UNO_PYTHON "
            "to one (on Debian/Ubuntu: /usr/bin/python3 with python3-uno)"
        )
    args = [python, "-I", str(_SCRIPT), command, *[str(p) for p in paths], "--soffice", soffice]
    if author:
        args += ["--author", author]
    try:
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise RuntimeError(f"LibreOffice {command} failed: {detail[-1] if detail else exc}") from exc


def _lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def round_trip(output: Path, original: Path, revised: Path) -> dict[str, Any]:
    """Accept-all must equal ``revised``; reject-all must equal ``original``."""
    from .redline import preview_docx_revisions

    accepted = _lines(preview_docx_revisions(output, "accept"))
    rejected = _lines(preview_docx_revisions(output, "reject"))
    want_revised = _lines(preview_docx_revisions(revised, "accept"))
    want_original = _lines(preview_docx_revisions(original, "accept"))
    result: dict[str, Any] = {
        "accept_all_equals_revised": accepted == want_revised,
        "reject_all_equals_original": rejected == want_original,
    }
    for key, got, want in (
        ("accept_mismatch", accepted, want_revised),
        ("reject_mismatch", rejected, want_original),
    ):
        if got != want:
            for i, (a, b) in enumerate(zip(got, want)):
                if a != b:
                    result[key] = {"line": i, "got": a[:160], "expected": b[:160]}
                    break
            else:
                result[key] = {"lines_got": len(got), "lines_expected": len(want)}
    return result


def compare_docx_documents(original: Path, revised: Path, output: Path, *, author: str = "") -> dict[str, Any]:
    """Write ``output``: ``revised`` with every difference from ``original``
    as a tracked change attributed to ``author``."""
    from .redline import extract_docx_revisions

    original, revised, output = Path(original), Path(revised), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    for label, path in (("original", original), ("revised", revised)):
        if path.suffix.lower() == ".docx":
            existing = extract_docx_revisions(path)
            if existing:
                notes.append(
                    f"the {label} document already carries {len(existing)} tracked change(s); the comparison "
                    "is made on its text with those changes as LibreOffice shows them"
                )
    run_lo_uno("compare", [original, revised, output], author=author)
    if not output.is_file():
        raise RuntimeError("LibreOffice compare produced no file")
    revisions = extract_docx_revisions(output)
    kinds: dict[str, int] = {}
    authors: list[str] = []
    for rev in revisions:
        kinds[rev["type"]] = kinds.get(rev["type"], 0) + 1
        if rev["author"] and rev["author"] not in authors:
            authors.append(rev["author"])
    payload: dict[str, Any] = {
        "output_path": str(output.resolve()),
        "insertions": kinds.get("insertion", 0),
        "deletions": kinds.get("deletion", 0),
        "authors": authors,
    }
    if original.suffix.lower() == ".docx" and revised.suffix.lower() == ".docx":
        payload["round_trip"] = round_trip(output, original, revised)
    if not revisions:
        notes.append("no differences found: the documents' text is identical")
    if notes:
        payload["notes"] = notes
    return payload


def accept_all(source: Path, output: Path) -> Path:
    """LibreOffice's own accept-all (used to check redlines independently)."""
    run_lo_uno("accept", [Path(source), Path(output)])
    return Path(output)


def reject_all(source: Path, output: Path) -> Path:
    run_lo_uno("reject", [Path(source), Path(output)])
    return Path(output)


__all__ = [
    "CompareUnavailable",
    "accept_all",
    "compare_docx_documents",
    "reject_all",
    "round_trip",
    "run_lo_uno",
    "uno_python",
]
