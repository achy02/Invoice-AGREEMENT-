"""S2a -- lines and word-level tokens from a PDF's embedded text layer.

Preferred over OCR wherever it exists, for two reasons. OCR re-derives from pixels what the
PDF already states exactly, misreading digits in rates and dates on the way; and the text
layer yields character-level geometry, so a value's box can be tightened to the characters
that actually spell it rather than to a whole OCR word.

Coordinates arrive in PDF points and are scaled to pixels at `render.dpi` here, so that
everything downstream speaks one coordinate system.

One quirk of these documents, already met on the invoice side: several of them have no
space glyphs in the text layer at all. ALSD's "ALSD Technologies Pvt Ltd" extracts as
"ALSDTechnologiesPvtLtd" -- the golden set records exactly that under `raw`. Spaces are
therefore reinserted from geometry: a gap wider than a fraction of the font size is a word
break. Without this, no evidence quote containing a space would ever ground.
"""
from __future__ import annotations

import logging
import re
import statistics
from pathlib import Path

import pymupdf

from .models import Line, Token

log = logging.getLogger(__name__)


def _chars_to_tokens(chars: list[dict], scale: float, gap_ratio: float) -> list[Token]:
    """Group characters into words, splitting where the horizontal gap is wide enough.

    `gap_ratio` is a fraction of the median character width, so it adapts to font size
    rather than assuming a point threshold.
    """
    if not chars:
        return []

    widths = [c["bbox"][2] - c["bbox"][0] for c in chars if c["c"].strip()]
    median = statistics.median(widths) if widths else 1.0
    threshold = max(median * gap_ratio, 0.5)

    tokens: list[Token] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        text = "".join(c["c"] for c in current).strip()
        if text:
            tokens.append(Token(
                text=text,
                bbox=(min(c["bbox"][0] for c in current) * scale,
                      min(c["bbox"][1] for c in current) * scale,
                      max(c["bbox"][2] for c in current) * scale,
                      max(c["bbox"][3] for c in current) * scale),
            ))
        current.clear()

    for char in chars:
        if char["c"].isspace():
            flush()
            continue
        if current:
            gap = char["bbox"][0] - current[-1]["bbox"][2]
            if gap > threshold:
                flush()
        current.append(char)
    flush()
    return tokens


def extract_lines(pdf: Path | str, page_number: int, scale: float,
                  gap_ratio: float = 0.28) -> list[Line]:
    """Reading-order lines with word tokens, in page-image pixels.

    `line_id` is `p<page>:L<nnn>`, which is what the LLM cites and what S5 looks up first.
    """
    with pymupdf.open(pdf, filetype="pdf") as doc:
        raw = doc[page_number - 1].get_text("rawdict", sort=True)

    lines: list[Line] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:          # 0 is text; 1 is an image
            continue
        for line in block.get("lines", []):
            chars: list[dict] = []
            for span in line.get("spans", []):
                chars.extend(span.get("chars", []))
            if not chars:
                continue
            tokens = _chars_to_tokens(chars, scale, gap_ratio)
            if not tokens:
                continue
            text = " ".join(token.text for token in tokens)
            lines.append(Line(
                line_id="",                 # assigned below, once ordering is settled
                text=text,
                bbox=(min(t.bbox[0] for t in tokens), min(t.bbox[1] for t in tokens),
                      max(t.bbox[2] for t in tokens), max(t.bbox[3] for t in tokens)),
                source="text_layer",
                conf=1.0,
                tokens=tokens,
            ))

    # Reading order: top to bottom, then left to right. `sort=True` above mostly does this,
    # but a two-column signature block can still come back interleaved.
    lines.sort(key=lambda line: (round(line.bbox[1] / 4), line.bbox[0]))
    for index, line in enumerate(lines, start=1):
        line.line_id = f"p{page_number}:L{index:03d}"
    return lines


def page_text(pdf: Path | str, page_number: int) -> str:
    with pymupdf.open(pdf, filetype="pdf") as doc:
        return doc[page_number - 1].get_text("text")


def _cell_text(bbox, tokens: list[Token], fallback: str) -> str:
    """Compose a cell's text from the word tokens inside it.

    pdfplumber's own `extract()` returns the raw characters, which in these PDFs have no
    space glyphs -- "INR2/-PerTransaction(Including1RsCredit)". The tokens already have word
    breaks recovered from geometry, so reusing them keeps the cell readable for the model
    and keeps an evidence quote groundable against it.
    """
    x0, y0, x1, y1 = bbox
    inside = [t for t in tokens
              if x0 - 1 <= (t.bbox[0] + t.bbox[2]) / 2 <= x1 + 1
              and y0 - 1 <= (t.bbox[1] + t.bbox[3]) / 2 <= y1 + 1]
    if not inside:
        return fallback
    inside.sort(key=lambda t: (round(t.bbox[1] / 4), t.bbox[0]))
    return " ".join(t.text for t in inside)


def extract_tables(pdf: Path | str, page_number: int, scale: float,
                   tokens: list[Token] | None = None) -> list:
    """Ruled tables on a text-layer page, as a cell grid in page-image pixels.

    Without this a commercial table is flattened into lines and the reading order scrambles
    it -- on the ALSD letter the rate is emitted *before* the sub-product it belongs to:

        [p3:L015] INR 2/- Per Transaction (Including 1 Rs Credit)
        [p3:L016] Penny-Drop (IMPS)

    A model reading that has to guess which rate goes with which product, which is precisely
    how a rate ends up on the wrong row. Given the grid it does not have to guess, and S5
    can ground against a specific cell.

    pdfplumber rather than PyMuPDF because the invoice half of this project already proved
    its ruled-table detection on hundreds of real documents.
    """
    import pdfplumber

    from .models import Cell, Table

    tables: list[Table] = []
    try:
        with pdfplumber.open(pdf) as book:
            page = book.pages[page_number - 1]
            found = sorted(page.find_tables(), key=lambda t: t.bbox[1])
            for index, table in enumerate(found, start=1):
                table_id = f"p{page_number}:T{index}"
                rows = table.extract()
                if not rows or max((len(r) for r in rows), default=0) < 2:
                    continue
                cells: list[Cell] = []
                for row_index, row in enumerate(rows):
                    for col_index, text in enumerate(row):
                        text = re.sub(r"\s+", " ", text or "").strip()
                        bbox = _cell_bbox(table, row_index, col_index, scale)
                        if bbox is None:
                            continue
                        if tokens:
                            text = _cell_text(bbox, tokens, text)
                        if not text:
                            continue
                        cells.append(Cell(f"{table_id}:R{row_index}C{col_index}",
                                          row_index, col_index, text, bbox))
                if cells:
                    tables.append(Table(
                        table_id,
                        tuple(v * scale for v in table.bbox),
                        len(rows), max(len(r) for r in rows), cells))
    except Exception as exc:  # table detection is complementary; never fail the page for it
        log.warning("Table detection failed on page %d of %s: %s", page_number, pdf, exc)
    return tables


def infer_table_from_layout(lines: list, page_number: int, index: int = 1,
                            min_rows: int = 3, min_cols: int = 3):
    """Recover a grid from a table that is laid out with whitespace rather than ruled lines.

    pdfplumber's `find_tables` needs rules to find. The Capitalneed offer letter's commercial
    table has none -- its columns exist only as x positions:

        x=109        x=582        x=786        x=964            x=1239
        Header       Frequency    Free Limit   Standard         For Client
        E-sign       Count Based  NA           INR 20           INR 15 /-

    Flattened into reading order that becomes "INR 20" then "INR 15 /-" with nothing to say
    which column each belongs to, and a model asked for the client's rate reasonably takes
    the first number it meets. It is the standard rate. Every rate on the page comes out one
    column to the left, which is not a wrong guess so much as a destroyed distinction.

    So columns are reconstructed from geometry: lines are grouped into rows by their vertical
    centres, their left edges are clustered into columns, and each line is placed in the
    column it starts in. Returning `None` unless the result really looks like a grid matters
    as much as the detection -- prose has x positions too, and inventing a table over a
    paragraph would corrupt pages that are currently read correctly.
    """
    from .models import Cell, Table

    if len(lines) < min_rows:
        return None

    heights = [line.bbox[3] - line.bbox[1] for line in lines]
    row_gap = max(statistics.median(heights) * 0.6, 4.0) if heights else 8.0

    rows: list[list] = []
    for line in sorted(lines, key=lambda l: ((l.bbox[1] + l.bbox[3]) / 2, l.bbox[0])):
        centre = (line.bbox[1] + line.bbox[3]) / 2
        if rows and abs(centre - rows[-1][0]) <= row_gap:
            rows[-1][1].append(line)
        else:
            rows.append([centre, [line]])
    grouped = [sorted(members, key=lambda l: l.bbox[0]) for _, members in rows]

    # Column edges: cluster the left edges every line starts at. A real column is one that
    # several rows use, which is what separates a table from an indented paragraph.
    edges = sorted(line.bbox[0] for line in lines)
    columns: list[list[float]] = []
    for edge in edges:
        if columns and edge - columns[-1][-1] <= 18:
            columns[-1].append(edge)
        else:
            columns.append([edge])
    starts = [min(group) for group in columns if len(group) >= min_rows - 1]
    if len(starts) < min_cols:
        return None

    def column_of(x: float) -> int:
        best = 0
        for position, start in enumerate(starts):
            if x >= start - 12:
                best = position
        return best

    placed = []
    for members in grouped:
        used: dict[int, list] = {}
        for line in members:
            used.setdefault(column_of(line.bbox[0]), []).append(line)
        placed.append(used)

    # The tabular band, not the whole page: a commercial table sits among a letterhead,
    # paragraphs of terms and a signature block, all of which have x positions of their own.
    #
    # The band is anchored on *wide* rows -- rows that span min_cols columns at once. Those
    # are what only a table produces. The first attempt here took the longest unbroken run
    # of rows with two or more columns instead, and on the Capitalneed letter that chose the
    # seven-row signature block over the four-row commercial table, because the table's run
    # is broken by continuation lines ("charges" wrapping under "logistics and maintenance")
    # while two parallel signature columns run uninterrupted. Width distinguishes them;
    # length does not.
    wide = [position for position, used in enumerate(placed) if len(used) >= min_cols]
    if len(wide) < min_rows:
        return None

    band = placed[wide[0]:wide[-1] + 1]

    # Prose can still reach here -- numbered contract clauses sit in a column beside their
    # number -- so two gates follow. A *dense* column is one most rows of the band use;
    # clause text yields one long column plus sparse markers, rarely three dense ones.
    dense = [column for column in {c for used in band for c in used}
             if sum(1 for used in band if column in used) >= len(band) * 0.5]
    if len(dense) < min_cols:
        return None

    # And table cells are short. Clause text ran to a hundred characters a cell; a rate, a
    # frequency or a product name does not.
    lengths = [len(" ".join(l.text for l in group))
               for used in band for group in used.values()]
    if lengths and statistics.median(lengths) > 45:
        return None

    # Column positions were clustered over the whole page, so the band inherits slots that
    # only prose or a letterhead ever used. Keeping them would hand the model a grid padded
    # with empty columns and cell ids that skip numbers, which is harder to read than the
    # flat text this replaces. Keep the columns the band actually fills, renumbered.
    kept = sorted({column for used in band for column in used})
    renumber = {column: position for position, column in enumerate(kept)}

    cells: list[Cell] = []
    table_id = f"p{page_number}:T{index}"
    for row_index, used in enumerate(band):
        for col_index, group in sorted(used.items()):
            text = " ".join(l.text for l in group).strip()
            if not text:
                continue
            cells.append(Cell(
                f"{table_id}:R{row_index}C{renumber[col_index]}", row_index,
                renumber[col_index], text,
                (min(l.bbox[0] for l in group), min(l.bbox[1] for l in group),
                 max(l.bbox[2] for l in group), max(l.bbox[3] for l in group))))
    if not cells:
        return None

    return Table(table_id,
                 (min(c.bbox[0] for c in cells), min(c.bbox[1] for c in cells),
                  max(c.bbox[2] for c in cells), max(c.bbox[3] for c in cells)),
                 len(band), len(kept), cells)


def _cell_bbox(table, row_index: int, col_index: int, scale: float):
    """pdfplumber keeps per-cell geometry alongside the extracted text; scale it to pixels."""
    try:
        row = table.rows[row_index]
    except (AttributeError, IndexError):
        return None
    try:
        cell = row.cells[col_index]
    except (AttributeError, IndexError):
        return None
    if not cell:
        return None
    x0, top, x1, bottom = cell
    return (x0 * scale, top * scale, x1 * scale, bottom * scale)
