"""Native PDF text extraction (no OCR).

PyMuPDF is the primary source of text and word coordinates. pdfplumber is used only to
detect ruled tables (line items); its text is never concatenated with PyMuPDF's.
"""
from __future__ import annotations

import logging
import re
import statistics
from pathlib import Path

import pdfplumber
import pymupdf

from . import config
from .invoice_patterns import valid_grouping
from .models import ExtractedDocument, PageContent, Segment, TableContent

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


class PdfExtractionError(Exception):
    """The PDF could not be opened or read at all."""


def is_pdf_bytes(data: bytes) -> bool:
    return PDF_MAGIC in data[:1024]


def normalize_text(text: str) -> str:
    """Collapse runs of spaces, keep meaningful line breaks, leave all other characters intact."""
    text = text.replace(" ", " ").replace("\t", " ")
    lines = [re.sub(r" {2,}", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _is_readable(text: str, min_chars: int) -> bool:
    alnum = sum(ch.isalnum() for ch in text)
    garbage = text.count("�")
    return alnum >= min_chars and garbage <= alnum * 0.3


def build_lines(words: list[tuple], page_no: int) -> list[list[Segment]]:
    """Group PyMuPDF words into visual lines, then split each line into column segments."""
    items = sorted((w for w in words if str(w[4]).strip()), key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows: list[list] = []  # [y_center, height, words]
    for w in items:
        yc, h = (w[1] + w[3]) / 2, w[3] - w[1]
        if rows and abs(yc - rows[-1][0]) <= max(2.0, 0.4 * min(h, rows[-1][1])):
            rows[-1][2].append(w)
        else:
            rows.append([yc, h, [w]])

    lines: list[list[Segment]] = []
    for _, _, row_words in rows:
        row_words.sort(key=lambda w: w[0])
        height = statistics.median(w[3] - w[1] for w in row_words)
        gap_limit = max(6.0, 0.6 * height)
        segments: list[list[tuple]] = [[row_words[0]]]
        for w in row_words[1:]:
            if w[0] - segments[-1][-1][2] > gap_limit:
                segments.append([w])
            else:
                segments[-1].append(w)
        lines.append(
            [
                Segment(
                    page=page_no,
                    text=" ".join(str(w[4]) for w in seg),
                    x0=min(w[0] for w in seg),
                    y0=min(w[1] for w in seg),
                    x1=max(w[2] for w in seg),
                    y1=max(w[3] for w in seg),
                )
                for seg in segments
            ]
        )
    return lines


# A cell that is nothing but one number: digits, grouping commas and an optional decimal part.
_AMOUNT_CELL_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _repair_split_number(text: str) -> str:
    """Rejoin a number pdfplumber split with a stray space: '1 ,000,000' -> '1,000,000'.

    pdfplumber inserts a space wherever it judges the gap between two characters to be wide,
    and in some invoice fonts that happens inside a grouped amount. PyMuPDF reads the very same
    text without the space, so the space is an artifact rather than content. Left alone it is
    destructive: '1 ,000,000' parses as 000,000 = 0, and '1 80,000' as 80,000.

    Only a cell that is entirely one comma-grouped or decimal number is repaired, and only if
    the result is validly grouped, so dates ('01 02 2026') and separate values are untouched.
    """
    if " " not in text or ("," not in text and "." not in text):
        return text
    candidate = text.replace(" ", "")
    if _AMOUNT_CELL_RE.fullmatch(candidate) and valid_grouping(candidate):
        return candidate
    return text


def _clean_cell(cell: str | None) -> str:
    return _repair_split_number(re.sub(r"\s+", " ", cell or "").strip())


def extract_tables(path: Path, page_numbers: list[int], notes: list[str]) -> list[TableContent]:
    tables: list[TableContent] = []
    if not page_numbers:
        return tables
    try:
        with pdfplumber.open(path) as pdf:
            for number in page_numbers:
                page = pdf.pages[number - 1]
                found = sorted(page.find_tables(), key=lambda t: t.bbox[1])
                for table in found:
                    extracted = [[_clean_cell(c) for c in row] for row in table.extract()]
                    # Keep each row's y alongside it, so a header part-way down can be located.
                    tops = [r.bbox[1] for r in table.rows]
                    pairs = list(zip(extracted, tops)) if len(tops) == len(extracted) else \
                        [(r, table.bbox[1]) for r in extracted]
                    pairs = [p for p in pairs if any(p[0])]
                    rows = [r for r, _ in pairs]
                    if rows and max(len(r) for r in rows) >= 2:
                        tables.append(TableContent(number, tuple(table.bbox), rows, [y for _, y in pairs]))
    except Exception as exc:  # pdfplumber is complementary; never fail the document because of it
        log.warning("Table extraction failed for %s: %s", path, exc)
        notes.append(f"Table detection skipped ({exc.__class__.__name__}); text layout used instead.")
    return tables


def extract_document(path: Path, file_name: str | None = None, min_chars: int | None = None) -> ExtractedDocument:
    path = Path(path)
    file_name = file_name or path.name
    min_chars = config.MIN_TEXT_CHARS_PER_PAGE if min_chars is None else min_chars
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:
        raise PdfExtractionError(f"Cannot open PDF (corrupt or invalid file): {exc}") from exc

    notes: list[str] = []
    pages: list[PageContent] = []
    with doc:
        if doc.needs_pass:
            raise PdfExtractionError("PDF is password-protected; remove the password and upload again.")
        if doc.page_count == 0:
            raise PdfExtractionError("PDF contains no pages (corrupt or empty file).")
        for index in range(doc.page_count):
            try:
                page = doc[index]
                raw = page.get_text("text", sort=True)
                words = page.get_text("words", sort=True)
                width, height = page.rect.width, page.rect.height
            except Exception as exc:
                notes.append(f"Page {index + 1} could not be read: {exc}")
                pages.append(PageContent(index + 1, 0, 0, "", "", [], False))
                continue
            text = normalize_text(raw)
            readable = _is_readable(text, min_chars)
            lines = build_lines(words, index + 1) if readable else []
            pages.append(PageContent(index + 1, width, height, text, raw, lines, readable))

    readable_numbers = [p.number for p in pages if p.readable]
    tables = extract_tables(path, readable_numbers, notes)
    return ExtractedDocument(file_name, len(pages), pages, tables, notes)
