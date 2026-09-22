"""Labeled contact sheets for visual QA of rendered documents.

A 20-slide deck sent one image per slide is both expensive and hard to reason
about. A contact sheet — a labeled grid of every page — is what a designer
actually looks at to catch the defects that matter at deck level: blank
slides, text overflowing its box, inconsistent margins, a chart that failed to
render. Anthropic's own PPTX skill takes the same approach (``thumbnail.py``
writes a labeled grid, split past 12 slides).

The model gets the sheet first and can then call ``view_image`` on a single
full-resolution page when it needs to read fine detail.
"""

from __future__ import annotations

from pathlib import Path

# Past this many pages the grid cells get too small to judge layout, so the
# sheet is split into several images.
_PER_SHEET = 12
_COLS = 4
_CELL_W = 460
_LABEL_H = 26
_PAD = 10
_BG = (240, 241, 243)
_LABEL_BG = (23, 32, 42)
_LABEL_FG = (255, 255, 255)
_CELL_BG = (255, 255, 255)


def _font(size: int):
    """A font with CJK coverage first.

    DejaVu and Liberation have none, so a Chinese label rendered with them is a
    row of tofu — and a wireframe of tofu reads as an empty slide, which sends
    a reviewer chasing a layout defect that is really a font fallback.
    """
    from PIL import ImageFont

    for candidate in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def build_contact_sheets(
    images: list[Path],
    out_dir: Path,
    prefix: str,
    *,
    label: str = "Page",
    per_sheet: int = _PER_SHEET,
) -> list[Path]:
    """Write labeled grid sheets for ``images``; return the sheet paths.

    Each cell is captioned ``{label} N`` with N being the 1-based index in the
    original sequence, so the model can name the page it wants to inspect or
    fix. Returns an empty list when ``images`` is empty.
    """
    from PIL import Image, ImageDraw

    images = [Path(p) for p in images]
    if not images:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    font = _font(16)
    sheets: list[Path] = []

    for sheet_idx in range(0, len(images), per_sheet):
        batch = images[sheet_idx:sheet_idx + per_sheet]
        thumbs: list[Image.Image] = []
        for p in batch:
            try:
                with Image.open(p) as im:
                    im = im.convert("RGB")
                    ratio = _CELL_W / im.width
                    thumbs.append(im.resize((_CELL_W, max(1, int(im.height * ratio))), Image.LANCZOS))
            except Exception:
                thumbs.append(Image.new("RGB", (_CELL_W, int(_CELL_W * 9 / 16)), (255, 220, 220)))

        cell_h = max(t.height for t in thumbs)
        cols = min(_COLS, len(thumbs))
        rows = (len(thumbs) + cols - 1) // cols
        sheet_w = cols * (_CELL_W + _PAD) + _PAD
        sheet_h = rows * (cell_h + _LABEL_H + _PAD) + _PAD
        sheet = Image.new("RGB", (sheet_w, sheet_h), _BG)
        draw = ImageDraw.Draw(sheet)

        for i, thumb in enumerate(thumbs):
            col, row = i % cols, i // cols
            x = _PAD + col * (_CELL_W + _PAD)
            y = _PAD + row * (cell_h + _LABEL_H + _PAD)
            draw.rectangle([x, y, x + _CELL_W, y + _LABEL_H], fill=_LABEL_BG)
            draw.text((x + 8, y + 4), f"{label} {sheet_idx + i + 1}", fill=_LABEL_FG, font=font)
            # White plate behind the thumb so a mostly-blank page still reads
            # as a rendered page rather than a hole in the sheet.
            draw.rectangle([x, y + _LABEL_H, x + _CELL_W, y + _LABEL_H + cell_h], fill=_CELL_BG)
            sheet.paste(thumb, (x, y + _LABEL_H))

        suffix = "" if len(images) <= per_sheet else f"-{sheet_idx // per_sheet + 1}"
        out = out_dir / f"{prefix}-contact{suffix}.jpg"
        sheet.save(out, format="JPEG", quality=80, optimize=True)
        sheets.append(out)

    return sheets
