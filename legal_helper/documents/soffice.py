"""One way to run LibreOffice headless, safe to call concurrently.

Every ``soffice --headless`` call used to share the user's default profile.
LibreOffice holds a lock on that profile for the life of the process, so a
second render started while the first was still running exited immediately
without producing a file: 2 of 4 renders failed in every concurrent round.
Each call here gets its own throwaway profile (``-env:UserInstallation``),
which is what makes renders safe to run side by side.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping, Optional, Sequence


class SofficeNotInstalled(RuntimeError):
    """LibreOffice is not on PATH. Callers degrade one tool, not the turn."""


def soffice_binary() -> Optional[str]:
    return shutil.which("soffice") or shutil.which("libreoffice")


def run_soffice(
    args: Sequence[str],
    *,
    timeout: float = 120,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run ``soffice --headless <args>`` under a private, per-call profile.

    Raises :class:`SofficeNotInstalled` when LibreOffice is absent and
    ``subprocess.CalledProcessError`` / ``TimeoutExpired`` on failure, so
    callers keep their existing error handling.
    """
    binary = soffice_binary()
    if not binary:
        raise SofficeNotInstalled("soffice/libreoffice not found on PATH")
    profile = Path(tempfile.mkdtemp(prefix="lh-soffice-profile-"))
    try:
        child_env = {**os.environ, **(env or {})}
        return subprocess.run(
            [
                binary,
                f"-env:UserInstallation={profile.as_uri()}",
                "--headless",
                "--norestore",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env,
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def convert(
    source: Path,
    out_dir: Path,
    target: str = "pdf",
    *,
    timeout: float = 120,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Convert ``source`` into ``out_dir`` and return the produced file.

    ``target`` is a LibreOffice filter spec (``pdf``, ``docx``, ``xlsx``,
    ``docx:"MS Word 2007 XML"`` …); the produced file's suffix is the part
    before any ``:``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    run_soffice(
        ["--convert-to", target, "--outdir", str(out_dir), str(source)],
        timeout=timeout,
        env=env,
    )
    suffix = target.split(":", 1)[0].strip().lower()
    produced = out_dir / f"{source.stem}.{suffix}"
    if produced.is_file():
        return produced
    candidates = sorted(out_dir.glob(f"*.{suffix}"))
    if not candidates:
        raise RuntimeError(f"soffice did not produce a .{suffix} for {source.name} in {out_dir}")
    return candidates[0]


__all__ = ["SofficeNotInstalled", "convert", "run_soffice", "soffice_binary"]
