"""S1 -- render every page to PNG at `render.dpi`.

These images are the pipeline's coordinate system: every bounding box it emits is in pixels
on them, origin top-left. The DPI is therefore recorded alongside each page and checked on
load, because boxes produced from a 150 dpi render mis-place silently on a 200 dpi image and
nothing downstream would notice.

200 dpi is not arbitrary -- the golden set's page images were rendered at it, so a render
here must reproduce their exact pixel sizes (1653x2341, 1654x2338, ...) or every IoU the
eval harness reports is measured against a different page.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pymupdf

from .config import Config

log = logging.getLogger(__name__)

#: PDF user space is 72 units to the inch, so this converts points to pixels at a given DPI.
POINTS_PER_INCH = 72.0


def scale_for(dpi: int) -> float:
    """Multiplier taking PDF points to pixels on the rendered image."""
    return dpi / POINTS_PER_INCH


def page_size(pdf: Path | str, page_number: int, dpi: int) -> tuple[int, int]:
    """The pixel size a page will render to, without rendering it.

    Computed the way PyMuPDF itself does -- transform the page rect and take the enclosing
    integer rect -- rather than rounding `width * dpi / 72` by hand. Rounding by hand is off
    by one on roughly half of these pages, because the product lands a float's breath either
    side of an integer (595.44 * 200/72 is 1654.0000000000002, so ceil gives 1655).
    """
    with pymupdf.open(pdf, filetype="pdf") as doc:
        rect = doc[page_number - 1].rect
    irect = (rect * pymupdf.Matrix(scale_for(dpi), scale_for(dpi))).irect
    return (irect.width, irect.height)


def render_page(pdf: Path, page_number: int, dpi: int, out: Path) -> tuple[Path, int, int]:
    """Render one page to `out`. Returns (path, width, height) in pixels."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf, filetype="pdf") as doc:
        pixmap = doc[page_number - 1].get_pixmap(dpi=dpi)
        pixmap.save(out)
    return out, pixmap.width, pixmap.height


def render_document(pdf: Path, sha: str, config: Config,
                    pages: list[int] | None = None) -> list[tuple[Path, int, int]]:
    """Render every page (or just `pages`) into `paths.page_images/<sha>/p-<n>.png`.

    A page already rendered at this DPI is reused: re-rendering is pure cost, and the image
    is keyed by the document's content hash so two copies of one agreement share it.
    """
    out_dir = Path(config.paths.page_images) / sha
    out_dir.mkdir(parents=True, exist_ok=True)
    dpi = config.render.dpi
    results: list[tuple[Path, int, int]] = []

    with pymupdf.open(pdf, filetype="pdf") as doc:
        numbers = pages or range(1, doc.page_count + 1)
        for number in numbers:
            out = out_dir / f"p-{number}.{config.render.format}"
            expected = page_size(pdf, number, dpi)
            if out.is_file():
                try:
                    from PIL import Image

                    with Image.open(out) as image:
                        if image.size == expected:
                            results.append((out, *expected))
                            continue
                except Exception:  # a damaged image is simply re-rendered
                    pass
            pixmap = doc[number - 1].get_pixmap(dpi=dpi)
            pixmap.save(out)
            results.append((out, pixmap.width, pixmap.height))
    return results
