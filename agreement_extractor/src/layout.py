"""S2c -- assemble one `Page` per page: text layer or OCR, plus seals and signature evidence.

The per-page decision is the heart of it:

    text layer passes the gate  -> use it, with character-level geometry   source="text_layer"
    it does not                 -> OCR the rendered image                  source="paddle_ocr"
    OCR unavailable or failed   -> no lines, and the page is flagged       source="none"

Signature evidence is collected, not decided. Whether a document was signed cannot be
settled from its prose, so this stage records what was seen -- a PDF signature widget, an
e-sign envelope id, a "Digitally signed by" block, a seal, a typed name under a signatory
caption -- and S6 grades it into SIGNED / PARTIAL / UNSIGNED / UNCERTAIN.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pymupdf

from .cache import StageCache, bytes_sha256
from .config import Config
from .inventory import is_digital
from .models import Document, Page, SealText, SignatureEvidence
from .ocr import OcrUnavailable, mean_confidence, read_page, read_seals
from .render import render_document, scale_for
from .text_layer import extract_lines, extract_tables, infer_table_from_layout

log = logging.getLogger(__name__)

# E-sign platforms flatten the signature into the page image and stamp an envelope id on
# every page, leaving no PDF signature widget behind. The id is then the only structural
# evidence the file carries.
ENVELOPE_RE = re.compile(r"(?:docusign\s+)?envelope\s+id\s*[:\-]?\s*([0-9A-Fa-f][0-9A-Fa-f-]{20,})", re.I)
SIGNED_BY_RE = re.compile(r"digitally\s+signed\s+by\s*:?\s*(?:name\s*:\s*)?([^\n]{2,70})", re.I)
SIGNATORY_CAPTION_RE = re.compile(r"authori[sz]ed\s+signator(?:y|ies)", re.I)
NAME_RE = re.compile(r"^\s*name\s*[:\-]\s*(.+?)\s*$", re.I)


def signature_fields(doc) -> list[SignatureEvidence]:
    """PDF signature widgets -- the only evidence that is part of the file's structure."""
    found: list[SignatureEvidence] = []
    try:
        for index, page in enumerate(doc, start=1):
            for widget in page.widgets() or []:
                if widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                    found.append(SignatureEvidence(
                        "pdf_signature_field",
                        (widget.field_value or widget.field_label or "").strip(),
                        tuple(widget.rect), index))
    except Exception as exc:  # widget parsing is optional evidence, never fatal
        log.debug("Signature field scan failed: %s", exc)
    return found


def signature_evidence(page: Page) -> list[SignatureEvidence]:
    """Envelope ids, "Digitally signed by" blocks, seals and names under signatory captions."""
    evidence: list[SignatureEvidence] = []

    for line in page.lines:
        match = ENVELOPE_RE.search(line.text)
        if match:
            evidence.append(SignatureEvidence("envelope_id", match.group(1).strip(),
                                              line.bbox, page.number))
        match = SIGNED_BY_RE.search(line.text)
        if match:
            evidence.append(SignatureEvidence("digitally_signed_by", match.group(1).strip(),
                                              line.bbox, page.number))

    for seal in page.seals:
        evidence.append(SignatureEvidence("seal", seal.text, seal.bbox, page.number))

    # A name printed within a few lines below an "Authorised Signatory" caption.
    for index, line in enumerate(page.lines):
        if not SIGNATORY_CAPTION_RE.search(line.text):
            continue
        evidence.append(SignatureEvidence("signatory_caption", line.text.strip(),
                                          line.bbox, page.number))
        for follower in page.lines[index + 1:index + 4]:
            name = NAME_RE.match(follower.text)
            candidate = name.group(1) if name else follower.text.strip()
            if name or (candidate and candidate.replace(" ", "").isalpha()
                        and 2 <= len(candidate.split()) <= 4):
                evidence.append(SignatureEvidence("typed_name", candidate,
                                                  follower.bbox, page.number))
                break
    return evidence


def build_page(pdf: Path, number: int, image: Path, width: int, height: int,
               config: Config, digital: bool | None = None,
               ocr_cache: StageCache | None = None,
               table_cache: StageCache | None = None) -> Page:
    """One page, read by whichever route its own text layer justifies."""
    scale = scale_for(config.render.dpi)
    page = Page(number=number, width=width, height=height, dpi=config.render.dpi,
                image_path=str(image), source="none")

    if digital is None:
        from .text_layer import page_text

        digital = is_digital(page_text(pdf, number), config)

    if digital:
        page.lines = extract_lines(pdf, number, scale)
        if page.lines:
            page.source = "text_layer"
            page.mean_conf = 1.0
            page.tables = extract_tables(
                pdf, number, scale, [t for line in page.lines for t in line.tokens])
            _add_inferred_table(page)
            page.signatures = signature_evidence(page)
            return page
        page.note = "Text layer passed the gate but yielded no lines; falling back to OCR."

    # The text layer is thin, garbled or empty. Only now is OCR worth its cost.
    data = image.read_bytes()
    digest = bytes_sha256(data)
    cached = ocr_cache.get(digest) if ocr_cache else None
    if cached is not None:
        page.lines = [_line_from_dict(d) for d in cached["lines"]]
        page.seals = [SealText(s["text"], tuple(s["bbox"]), s["conf"]) for s in cached["seals"]]
        page.source = "paddle_ocr"
        page.mean_conf = cached["mean_conf"]
    else:
        started = time.perf_counter()
        try:
            page.lines = read_page(data, number, config)
        except OcrUnavailable as exc:
            page.note = f"OCR unavailable: {exc}"
            return page
        except Exception as exc:
            page.note = f"OCR failed: {exc}"
            return page

        # Seals are *additional* evidence. PPStructureV3's seal pipeline needs optional
        # dependencies that plain PaddleOCR does not, so it can fail on a machine where
        # recognition works perfectly. Losing a page's text because a stamp could not be
        # read would be absurd, so this failure is noted and stepped over.
        try:
            page.seals = read_seals(data, config)
        except Exception as exc:
            page.note = f"Seal recognition unavailable ({exc.__class__.__name__}); text is unaffected."
            log.debug("Seal recognition failed on page %d: %s", number, exc)
        page.source = "paddle_ocr"
        page.mean_conf = mean_confidence(page.lines)
        log.info("OCR page %d in %.1fs (%d lines, mean conf %.2f)",
                 number, time.perf_counter() - started, len(page.lines), page.mean_conf)
        if ocr_cache:
            ocr_cache.put(digest, {
                "lines": [_line_to_dict(line) for line in page.lines],
                "seals": [{"text": s.text, "bbox": list(s.bbox), "conf": s.conf}
                          for s in page.seals],
                "mean_conf": page.mean_conf,
            })

    if not page.lines:
        page.source = "none"
        page.note = page.note or "OCR produced no text."
        return page

    if page.mean_conf < config.ocr.min_confidence:
        page.note = f"Low OCR confidence ({page.mean_conf:.0%})."

    # Tables are deliberately NOT recovered here. Which pages are worth three minutes of
    # table recognition depends on where the document's commercial section begins, and that
    # cannot be known from one page in isolation -- a continuation page carries no heading.
    # `add_tables` runs over the finished document instead. See `build_document`.
    page.signatures = signature_evidence(page)
    return page


def add_tables(page: Page, config: Config, table_cache: StageCache | None = None) -> None:
    """Recover the grid on one scanned page, from cache where possible.

    A scanned commercial table has no ruled lines to read from a text layer, so its grid has
    to come from the image. PP-StructureV3 does that properly and slowly; geometry from the
    line boxes is the free fallback where it declines or finds nothing.
    """
    if page.tables or page.source != "paddle_ocr" or not page.lines:
        return
    image = Path(page.image_path)
    digest = bytes_sha256(image.read_bytes()) if image.is_file() else None

    stored = table_cache.get(digest) if (table_cache and digest) else None
    if stored is not None:
        page.tables = [_table_from_dict(t) for t in stored]
    else:
        data = image.read_bytes() if image.is_file() else b""
        _add_scan_tables(page, data, config)
        if table_cache and digest:
            table_cache.put(digest, [_table_to_dict(t) for t in page.tables])
    _add_inferred_table(page)


#: What a charge looks like when you are only allowed to look at the characters: a currency
#: marker, an amount, or the vocabulary of billing.
_MONEY = re.compile(
    r"\bINR\b|\bRs\.?\s*\d|₹|\b\d{1,3}(?:,\d{2,3})+\b"
    r"|\bcharges?\b|\bfees?\b|\bper\s+(?:month|annum|year|transaction|stamp|sign|template|document)\b"
    r"|\brate\b|\bbilling\b|\bpricing\b|\bcommercials?\b",
    re.I)


#: The headings these agreements put above their money. An MSA is mostly boilerplate: the
#: charges live in a schedule or annexure near the end, under a name from a small and
#: repeating vocabulary. Naming the section outright is better evidence than inferring a
#: table from the shape of its lines, and it finds commercial pages the shape test misses --
#: J S Global's pricing table sits on page 15 of 21, Ninjamonk's on 23 and 24 of 25.
COMMERCIAL_HEADING = re.compile(
    r"\bschedule\s+of\s+charges\b"
    r"|\bcommercial\s+arrangement\b"
    r"|\bcommercials?\s+for\s+all\s+products\b"
    r"|\bpricing\s+table\b"
    r"|\bproduct\s+offering\b"
    r"|\bschedule\s*[-\s]*(?:II|2)\b"
    r"|\bannexure\s*[-\s]*(?:II|2|B)\b"
    r"|\bother\s+t\s*&\s*c\b"
    r"|\bother\s+commercial\s+terms\b",
    re.I)

#: A heading sits on a line of its own. Beyond this many characters the line is a sentence,
#: and these phrases appear in sentences constantly -- "as detailed under Schedule II
#: hereunder on a monthly basis" is a cross-reference on a page of dense prose, not the
#: schedule itself.
HEADING_LINE_CHARS = 60


def heads_commercial_section(page: Page) -> bool:
    """Does this page *announce* the commercial section, rather than mention it?

    Only short lines count, and the heading has to be most of the line. Both conditions
    were learned the expensive way: `fees and charges` used to be in the vocabulary above
    and matched clause D of the Aditya Birla MSA, "PAYMENT OF FEES AND CHARGES", which is
    fourteen pages of prose away from Schedule II. That page then cost 211 seconds of table
    recognition to find nothing, and dragged page 6 along behind it as a continuation.

    So the ambiguous phrase is gone from the vocabulary, and what is left must sit on a
    line short enough to be a heading. Together those two rules keep "PAYMENT OF FEES AND
    CHARGES" out (nothing in the vocabulary matches it any more) and keep "as detailed
    under Schedule II hereunder on a monthly basis..." out as well (far too long to be a
    heading), while "SCHEDULE II FEES AND CHARGES" and "Annexure II Commercial
    Arrangement" both still count.
    """
    return any(COMMERCIAL_HEADING.search(text)
               for text in ((line.text or "").strip() for line in page.lines)
               if 0 < len(text) <= HEADING_LINE_CHARS)


def commercial_pages(pages: list[Page], follow: int = 1) -> set[int]:
    """Page numbers in the commercial section, including its continuation pages.

    A schedule of charges rarely fits on one page and the overflow carries no heading of
    its own -- Aditya Birla's infra matrix is page 20 under a Schedule II that begins on
    19, and Ninjamonk's table runs from 23 onto 24. So each heading claims the pages that
    follow it as well.
    """
    found: set[int] = set()
    for page in pages:
        if heads_commercial_section(page):
            found.update(range(page.number, page.number + follow + 1))
    return {n for n in found if any(p.number == n for p in pages)}


def looks_tabular(page: Page, config: Config) -> bool:
    """Is this page worth three minutes of table recognition?

    Two cheap signals, and both must hold. Short lines say the page is built of cells rather
    than sentences -- on the Aditya Birla MSA the prose pages averaged 57 to 83 characters a
    line, Schedule II 24 and the plan matrix 8. Repeated mentions of money say the cells are
    charges, which is what stops the signature page (13 characters a line) and the
    scope-of-work list (32) from each costing three minutes to find nothing.

    Being wrong is survivable in one direction only, which is why both signals are needed: a
    page skipped in error loses its table, a page run in error loses three minutes, and on a
    thousand-document archive the second one is the expensive mistake.
    """
    if not config.ocr.table_recognition_on_scans or not page.lines:
        return False
    text = " ".join(line.text for line in page.lines)
    mean_length = len(text) / len(page.lines)
    if mean_length > config.ocr.table_page_max_mean_line_chars:
        return False
    return len(_MONEY.findall(text)) >= config.ocr.table_page_min_money_mentions


def _add_scan_tables(page: Page, data: bytes, config: Config) -> None:
    """Recover the grid on a scanned page that looks like a table of charges.

    Never fatal. A page whose structure cannot be read is still a page whose text was read,
    and losing that text because a table model was missing or slow would be a far worse
    outcome than losing the columns.
    """
    if page.tables or not config.ocr.table_recognition_on_scans:
        return
    from .table_ocr import TableOcrUnavailable, read_tables

    started = time.perf_counter()
    try:
        tables = read_tables(data, page.number, config, page.lines)
    except TableOcrUnavailable as exc:
        log.warning("Table structure unavailable on page %d: %s", page.number, exc)
        return
    except Exception as exc:
        log.warning("Table recognition failed on page %d: %s", page.number, exc)
        return
    if tables:
        page.tables = tables
        log.info("Table structure on page %d: %d table(s), %dx%d, in %.1fs",
                 page.number, len(tables), tables[0].n_rows, tables[0].n_cols,
                 time.perf_counter() - started)
    else:
        log.info("Page %d looked tabular but no grid was found (%.1fs)",
                 page.number, time.perf_counter() - started)


def _add_inferred_table(page: Page) -> None:
    """Recover a whitespace-aligned grid when no ruled table was found.

    Only when none was found: where rules exist they are the better evidence, and two
    overlapping descriptions of one table would give the model two cell ids for every value.
    """
    if page.tables or not page.lines:
        return
    try:
        inferred = infer_table_from_layout(page.lines, page.number)
    except Exception as exc:          # layout inference is complementary, never fatal
        log.debug("Column inference failed on page %d: %s", page.number, exc)
        return
    if inferred is not None:
        page.tables = [inferred]


def _line_to_dict(line) -> dict:
    return {"line_id": line.line_id, "text": line.text, "bbox": list(line.bbox),
            "source": line.source, "conf": line.conf,
            "tokens": [{"text": t.text, "bbox": list(t.bbox), "conf": t.conf}
                       for t in line.tokens]}


def _line_from_dict(data: dict):
    from .models import Line, Token

    return Line(data["line_id"], data["text"], tuple(data["bbox"]), data["source"],
                data["conf"], [Token(t["text"], tuple(t["bbox"]), t["conf"])
                               for t in data["tokens"]])


def _table_to_dict(table) -> dict:
    return {"table_id": table.table_id, "bbox": list(table.bbox),
            "n_rows": table.n_rows, "n_cols": table.n_cols,
            "cells": [{"cell_id": c.cell_id, "row": c.row, "col": c.col, "text": c.text,
                       "bbox": list(c.bbox), "row_span": c.row_span, "col_span": c.col_span}
                      for c in table.cells]}


def _table_from_dict(data: dict):
    from .models import Cell, Table

    return Table(data["table_id"], tuple(data["bbox"]), data["n_rows"], data["n_cols"],
                 [Cell(c["cell_id"], c["row"], c["col"], c["text"], tuple(c["bbox"]),
                       c.get("row_span", 1), c.get("col_span", 1))
                  for c in data["cells"]])


def build_document(document: Document, config: Config,
                   ocr_cache: StageCache | None = None,
                   table_cache: StageCache | None = None) -> Document:
    """Render every page, read it, and attach the results to `document`."""
    pdf = Path(document.path)
    rendered = render_document(pdf, document.sha256, config)
    digital = getattr(document, "digital_pages", None) or [None] * len(rendered)

    with pymupdf.open(pdf, filetype="pdf") as doc:
        fields = signature_fields(doc)

    document.pages = []
    for index, (image, width, height) in enumerate(rendered):
        page = build_page(pdf, index + 1, image, width, height, config,
                          digital[index] if index < len(digital) else None,
                          ocr_cache, table_cache)
        page.signatures.extend(f for f in fields if f.page == page.number)
        document.pages.append(page)

    # Now that every page has been read, decide where the money is and spend the expensive
    # table pass only there. Two signals, either of which is enough:
    #
    #   the document says so  -- a page under "Schedule II - Fees and Charges", "Annexure B
    #                            - Commercial Arrangement", "Schedule of Charges", and the
    #                            page after it, which is usually the table's overflow;
    #   the page looks like it -- short lines and repeated mentions of money, for a table
    #                            that carries no heading at all.
    #
    # The headings are the better signal and could only be used once the whole document was
    # in hand: a continuation page has none, and a page cannot tell on its own whether the
    # commercial section has already started.
    wanted = commercial_pages(document.pages)
    for page in document.pages:
        if page.number in wanted or looks_tabular(page, config):
            add_tables(page, config, table_cache)
    if wanted:
        log.info("Commercial section of %s: page(s) %s",
                 document.file_name, ", ".join(str(n) for n in sorted(wanted)))
    return document
