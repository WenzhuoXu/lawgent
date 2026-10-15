"""HEIC/HEIF support for Pillow, without a GPL binary.

Phone photos arrive as HEIC: 17 in one chat never reached the model. Pillow
reads HEIC once a HEIF plugin registers its opener. ``pi-heif`` (BSD-3; its
wheels bundle libheif and libde265, LGPLv3 shared libraries) decodes only,
which is all reading needs. ``pillow-heif`` also encodes, and its binary
wheels are GPLv2 because they bundle the x265 encoder, so it is used only as
a fallback when it is what happens to be installed.
"""

from __future__ import annotations

_REGISTERED = False


def register_heif_opener() -> bool:
    """Make ``PIL.Image.open`` read HEIC/HEIF; ``False`` when no plugin exists."""
    global _REGISTERED
    if _REGISTERED:
        return True
    for module in ("pi_heif", "pillow_heif"):
        try:
            plugin = __import__(module)
            plugin.register_heif_opener()
        except Exception:  # noqa: BLE001 — try the next plugin
            continue
        _REGISTERED = True
        return True
    return False


__all__ = ["register_heif_opener"]
