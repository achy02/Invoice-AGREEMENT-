"""S2c -- table structure on scanned pages, via PaddleOCR's PP-StructureV3.

The commercial terms of an agreement are almost always in a table, and on a scan a table is
just ink. Plain OCR reads every character correctly and still destroys the one thing that
makes the numbers mean anything -- which column a value sits in. The Aditya Birla MSA's
Schedule II came back as this:

    Risk Radar
    Monthly
    Minimum 85,000 per month
    Bank Balance
    2 Months or
    POC completion,
    Statement
    Includes: 5 Bank's Integration

One row of four columns, shredded into eight lines with the columns interleaved. No reader,
model or human, can reliably say from that whether "Monthly" belongs to Risk Radar or to
something else. PP-StructureV3's wired-table models rebuild the grid, and the same page
becomes a row with four labelled cells.

Three things about this were expensive to learn and are therefore stated plainly:

* **`enable_mkldnn=False` must be passed to the constructor.** The `FLAGS_use_mkldnn=0`
  environment variable is enough for plain `PaddleOCR` but *not* for `PPStructureV3`, which
  still dies with "ConvertPirAttribute2RuntimeAttribute not support" on every call. The
  parameter is not in the documented signature; it reaches the sub-pipelines through
  `**kwargs`.
* **It needs `paddlex[ocr]`.** Without those extras the pipeline refuses to construct, and
  because nothing ever called it, this project shipped for weeks with table recognition
  configured, believed on, and in fact never once executed.
* **It costs about three minutes a page** on this CPU -- roughly four times plain OCR. That
  is why `layout.looks_tabular` decides where to spend it rather than running it everywhere.

Only the *structure* is taken from here. The cell text PP-Structure returns is visibly worse
than the main OCR pass ("INR10Opertemplate" for "INR 100 per template"), so where a cell can
be matched to lines the main pass already read, those words win.
"""
from __future__ import annotations

import html as html_module
import logging
import re
import time
from pathlib import Path

from .config import Config
from .models import BBox, Cell, Line, Table

log = logging.getLogger(__name__)

#: Share of a text line that must sit inside a cell box before the line is counted as
#: belonging to that cell. Scanned rules are not straight and OCR line boxes overhang their
#: cell slightly, so demanding most of the line is too strict; demanding very little lets a
#: line bleed into its neighbour. 0.4 places every value on the Aditya Birla infra matrix.
LINE_IN_CELL = 0.4

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh]([^>]*)>(.*?)</t[dh]>", re.S | re.I)
_SPAN = re.compile(r"\b(row|col)span\s*=\s*[\"']?(\d+)", re.I)
_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(_TAG.sub(" ", fragment))).strip()


def _spans(attributes: str) -> tuple[int, int]:
    found = {kind.lower(): int(n) for kind, n in _SPAN.findall(attributes or "")}
    return max(1, found.get("row", 1)), max(1, found.get("col", 1))


def parse_html_grid(markup: str) -> list[tuple[int, int, int, int, str]]:
    """PP-Structure's HTML as (row, col, row_span, col_span, text), one per cell.

    Row and column spans are resolved into real grid positions, so a cell carrying
    `rowspan=4` occupies its column in the next three rows and the cells beside it keep the
    column index a reader would point at. Without that every row under a merged cell is
    shifted one column left, which is silently wrong rather than visibly broken.

    The markup is parsed leniently on purpose: what comes back is frequently malformed --
    a `</tbody>` with no opening tag is normal -- and a strict parser would reject a grid
    that is perfectly usable.
    """
    out: list[tuple[int, int, int, int, str]] = []
    occupied: set[tuple[int, int]] = set()
    for row_index, row_markup in enumerate(_ROW.findall(markup or "")):
        column = 0
        for attributes, fragment in _CELL.findall(row_markup):
            while (row_index, column) in occupied:
                column += 1
            row_span, col_span = _spans(attributes)
            for r in range(row_index, row_index + row_span):
                for c in range(column, column + col_span):
                    occupied.add((r, c))
            out.append((row_index, column, row_span, col_span, _text(fragment)))
            column += col_span
    return out


def _bounds(boxes) -> BBox | None:
    flat = [b for b in (boxes or []) if b is not None and len(b) >= 4]
    if not flat:
        return None
    return (min(float(b[0]) for b in flat), min(float(b[1]) for b in flat),
            max(float(b[2]) for b in flat), max(float(b[3]) for b in flat))


def _overlap(box: BBox, other: BBox) -> float:
    """Share of `other`'s area that lies inside `box`."""
    x0, y0 = max(box[0], other[0]), max(box[1], other[1])
    x1, y1 = min(box[2], other[2]), min(box[3], other[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    area = (other[2] - other[0]) * (other[3] - other[1])
    return ((x1 - x0) * (y1 - y0) / area) if area > 0 else 0.0


def text_from_lines(box: BBox, lines: list[Line], minimum: float = LINE_IN_CELL) -> str:
    """The main OCR pass's words for whatever sits inside `box`, in reading order.

    PP-Structure's own recogniser runs on a cropped, rescaled cell and loses spacing and the
    occasional character; the full-page pass does not. Where a cell's box contains lines the
    good pass already read, those are the better text for the same ink.
    """
    inside = [line for line in lines if _overlap(box, line.bbox) >= minimum]
    inside.sort(key=lambda line: (round(line.bbox[1] / 8), line.bbox[0]))
    return re.sub(r"\s+", " ", " ".join(line.text for line in inside)).strip()


def _cluster(values: list[float], tolerance: float) -> list[float]:
    """Centres of groups of near-equal coordinates, in order."""
    centres: list[float] = []
    for value in sorted(values):
        if centres and value - centres[-1] <= tolerance:
            continue
        centres.append(value)
    return centres


def map_boxes_to_grid(boxes, tolerance: float = 12.0) -> dict[tuple[int, int], BBox]:
    """Detected cell boxes placed at the (row, column) their geometry puts them in.

    Not by their order in the list. The list is *nearly* one box per cell and matching it
    positionally is a trap: an empty merged cell contributes no box, so one missing entry
    silently shifts every box after it by one cell. Row and column come from where a box
    actually sits on the page, which cannot drift.
    """
    boxes = [tuple(float(v) for v in b[:4]) for b in (boxes or [])
             if b is not None and len(b) >= 4]
    if not boxes:
        return {}
    tops = _cluster([b[1] for b in boxes], tolerance)
    lefts = _cluster([b[0] for b in boxes], tolerance)

    placed: dict[tuple[int, int], BBox] = {}
    for box in boxes:
        row = max(i for i, top in enumerate(tops) if box[1] >= top - tolerance)
        column = max(i for i, left in enumerate(lefts) if box[0] >= left - tolerance)
        placed.setdefault((row, column), box)
    return placed


def build_from_boxes(boxes, page_number: int, index: int,
                     lines: list[Line]) -> Table | None:
    """The grid from PP-Structure's cell geometry, the text from the full-page OCR.

    This is the right division of labour, and getting it wrong cost twelve charge lines on
    one page. PP-Structure does two jobs and is good at one of them: its cell boxes are
    accurate, its own recognition is not -- it reads a cropped, rescaled cell and returns
    "on" and "e" where the page says "One time", and `INR10O` where it says `INR 100`. The
    full-page pass is the mirror image: the text is right and it knows nothing about cells.

    So the boxes say *where the cells are* and the page's own lines say *what is in them*.
    Nothing here uses PP-Structure's text at all.
    """
    placed = map_boxes_to_grid(boxes)
    if not placed or not lines:
        return None
    # One or two stray boxes are a detection artefact, not a grid. Below this the markup is
    # the better description, and a 1x1 "table" holding one phrase is worse than no table.
    rows = max(r for r, _ in placed) + 1
    columns = max(c for _, c in placed) + 1
    if rows < 2 or columns < 2:
        return None

    cells: list[Cell] = []
    for (row, column), box in sorted(placed.items()):
        cells.append(Cell(cell_id=f"p{page_number}:T{index}:R{row}C{column}",
                          row=row, col=column,
                          text=text_from_lines(box, lines, LINE_IN_CELL),
                          bbox=box))
    if not any(cell.text for cell in cells):
        return None                      # geometry with nothing in it is not a table
    return Table(table_id=f"p{page_number}:T{index}",
                 bbox=_bounds(placed.values()) or (0.0, 0.0, 0.0, 0.0),
                 n_rows=max(c.row for c in cells) + 1,
                 n_cols=max(c.col for c in cells) + 1,
                 cells=cells)


def build_table(markup: str, boxes, page_number: int, index: int,
                lines: list[Line] | None = None) -> Table | None:
    """One PP-Structure table result as our Table.

    Geometry first, because it is the more reliable of the two descriptions the model
    returns. The HTML is the fallback for a table whose cells were not detected, or when
    there is no page text to fill them with.
    """
    if lines:
        from_boxes = build_from_boxes(boxes, page_number, index, lines)
        if from_boxes is not None:
            return from_boxes

    grid = parse_html_grid(markup)
    if not grid:
        return None
    region = _bounds(boxes)

    # Only worth consulting when the boxes describe the same shape the markup does. A box
    # grid of different dimensions is describing a different reading of the page, and
    # guessing across the difference is how every cell ends up holding the same text.
    placed = map_boxes_to_grid(boxes)
    rows = max((r for r, _ in placed), default=-1) + 1
    columns = max((c for _, c in placed), default=-1) + 1
    html_rows = max(r + s for r, _, s, _, _ in grid)
    html_cols = max(c + s for _, c, _, s, _ in grid)
    if not placed or rows != html_rows or columns != html_cols:
        if placed:
            log.debug("Page %d: %dx%d boxes against %dx%d markup; keeping the model's text",
                      page_number, rows, columns, html_rows, html_cols)
        placed = {}

    cells: list[Cell] = []
    for row, column, row_span, col_span, cell_text in grid:
        box = placed.get((row, column))
        text = cell_text
        # Substituted only for a box that belongs to *this* cell. Falling back to the whole
        # table's box here was a real defect: every cell then matched every line, so all 36
        # cells of the Aditya Birla Schedule II came back holding the entire page.
        if lines and box is not None:
            better = text_from_lines(box, lines)
            if better:
                text = better
        cells.append(Cell(cell_id=f"p{page_number}:T{index}:R{row}C{column}",
                          row=row, col=column, text=text,
                          bbox=box or region or (0.0, 0.0, 0.0, 0.0),
                          row_span=row_span, col_span=col_span))

    if not cells:
        return None
    return Table(table_id=f"p{page_number}:T{index}",
                 bbox=region or (0.0, 0.0, 0.0, 0.0),
                 n_rows=max(c.row + c.row_span for c in cells),
                 n_cols=max(c.col + c.col_span for c in cells),
                 cells=cells)


_ENGINE = {}


def engine(config: Config):
    """Build (once) the PP-StructureV3 pipeline, with the one argument that makes it run."""
    if "it" in _ENGINE:
        return _ENGINE["it"]
    try:
        import paddleocr
    except Exception as exc:                      # pragma: no cover - native install
        raise TableOcrUnavailable(f"PaddleOCR could not be imported: {exc}") from exc

    started = time.perf_counter()
    try:
        built = paddleocr.PPStructureV3(
            use_table_recognition=True,
            use_seal_recognition=False,           # seals have their own pass
            use_formula_recognition=False,
            use_chart_recognition=False,
            use_doc_orientation_classify=config.ocr.use_doc_orientation_classify,
            use_doc_unwarping=config.ocr.use_doc_unwarping,
            use_textline_orientation=config.ocr.use_textline_orientation,
            # Not in the documented signature; reaches the sub-pipelines via **kwargs. The
            # environment variable alone does not stop oneDNN here. See the module docstring.
            enable_mkldnn=False,
        )
    except Exception as exc:                      # pragma: no cover - native install
        raise TableOcrUnavailable(
            f"PP-StructureV3 could not start ({exc}). Table structure on scanned pages "
            f"needs the paddlex OCR extras: pip install \"paddlex[ocr]==3.7.2\"") from exc

    log.info("Loaded PP-StructureV3 in %.1fs", time.perf_counter() - started)
    _ENGINE["it"] = built
    return built


class TableOcrUnavailable(RuntimeError):
    """PP-StructureV3 cannot run here. Text extraction is unaffected."""


def builder_digest() -> str:
    """A hash of this module, so cached tables are rebuilt when the parser changes.

    The grid is as much a product of the code in this file as of the model's output. A
    defect here -- and there has been one, where every cell came back holding the whole
    page -- is baked into every cached table until something notices the code changed.
    Hashing the source means fixing the parser is enough; nobody has to remember to clear
    a cache, and nobody has to re-run three minutes a page of model inference to find out
    whether they did.
    """
    import hashlib

    return hashlib.sha256(__file__.encode("utf-8")
                          + Path(__file__).read_bytes()).hexdigest()[:16]


def _results(raw):
    for item in raw or []:
        data = item.json.get("res", item.json) if hasattr(item, "json") else item
        if isinstance(data, dict):
            yield data


def read_tables(image, page_number: int, config: Config,
                lines: list[Line] | None = None) -> list[Table]:
    """Every table PP-Structure finds on one rendered page."""
    from .ocr import _as_array

    array = _as_array(image)
    predict = engine(config).predict
    tables: list[Table] = []
    for data in _results(predict(array)):
        for raw in data.get("table_res_list") or []:
            table = build_table(raw.get("pred_html") or "", raw.get("cell_box_list"),
                                page_number, len(tables) + 1, lines)
            if table is not None:
                tables.append(table)
    return tables
