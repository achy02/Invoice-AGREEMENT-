"""Extract invoice line items: one per service, each with its own dates, HSN/SAC and amounts.

Two paths, tried in order:

1. **Ruled tables** found by pdfplumber. Header rows are merged downwards, so a header such as
   "Transaction Date" spanning "From" and "To" sub-columns maps to two separate date columns.
2. **Text columns** located from PyMuPDF word coordinates, for tables drawn without rules.

Both paths produce the same mapping of column kind -> column, then one LineItem per data row.
Values are only ever read from the invoice: an invoice-level amount is never split across lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from . import invoice_patterns as P
from .models import ExtractedDocument, LineItem, PageContent, Segment, TableContent

# Checked in order, so a specific header wins over a generic one ("Taxable Value" is a taxable
# column, not a generic amount column; "Description of Goods / Services" is not an HSN column).
KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("serial", P.SERIAL_HEADER_RE),
    ("hsn", P.HSN_HEADER_RE),
    ("date_from", P.FROM_HEADER_RE),
    ("date_to", P.TO_HEADER_RE),
    ("igst", P.IGST_HEADER_RE),
    ("cgst", P.CGST_HEADER_RE),
    ("sgst", P.SGST_HEADER_RE),
    ("taxable", P.TAXABLE_HEADER_RE),
    ("qty", P.QTY_HEADER_RE),
    ("rate", P.RATE_HEADER_RE),
    ("description", P.DESC_HEADER_RE),
    ("amount", P.LINE_AMOUNT_HEADER_RE),
]
AMOUNT_KINDS = ("taxable", "igst", "cgst", "sgst", "amount")
# Kinds that mark a row as a real line rather than a wrapped continuation of the one above.
OWN_ROW_KINDS = ("serial", "taxable", "amount", "qty", "rate", "date_from", "igst")

_AMOUNT_WORD_RE = re.compile(r"amount|amt|value", re.I)


def classify(header: str) -> str | None:
    text = re.sub(r"\s+", " ", (header or "").strip()).strip(" :*.|")
    if not text:
        return None
    for kind, rx in KIND_PATTERNS:
        if rx.fullmatch(text):
            return kind
    return None


def _prefer(new: str, old: str) -> bool:
    """When two columns claim the same kind, keep the one that names an amount."""
    new_has, old_has = bool(_AMOUNT_WORD_RE.search(new)), bool(_AMOUNT_WORD_RE.search(old))
    if new_has != old_has:
        return new_has
    return "%" in old and "%" not in new


def map_columns(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        kind = classify(header)
        if kind is None:
            continue
        if kind not in mapping or _prefer(header, headers[mapping[kind]]):
            mapping[kind] = index
    return mapping


def _merge_header_rows(rows: list[list[str]], count: int, start: int = 0) -> list[str]:
    """Combine stacked header rows column-wise: 'Transaction Date' + 'From' -> 'Transaction Date From'."""
    block = rows[start:start + count]
    width = max((len(r) for r in block), default=0)
    merged = []
    for i in range(width):
        parts = [r[i].strip() for r in block if i < len(r) and r[i].strip()]
        merged.append(" ".join(dict.fromkeys(parts)))
    return merged


def find_header(rows: list[list[str]], max_header_rows: int = 3) -> tuple[dict[str, int], int, int] | None:
    """Best (column mapping, number of header rows, index of the first header row).

    The header is searched for on every row, not just the first few: a page with an outer
    border comes back from pdfplumber as a single table whose first rows are the title and
    the address blocks, with the line-item header part-way down.
    """
    best: tuple[int, dict[str, int], int, int] | None = None
    for start in range(len(rows) - 1):
        for count in range(1, min(max_header_rows, len(rows) - start - 1) + 1):
            mapping = map_columns(_merge_header_rows(rows, count, start))
            if "description" not in mapping:
                continue
            score = len(mapping)
            # Strictly greater, so the earliest header wins when two score the same.
            if best is None or score > best[0]:
                best = (score, mapping, count, start)
    return (best[1], best[2], best[3]) if best else None


@dataclass
class LineItemResult:
    items: list[LineItem]
    taxable_total: Decimal | None = None  # a "Total" row inside the line-item table
    # Tax amounts stated on that same "Total" row, by kind ("igst", "cgst", "sgst").
    totals: dict[str, Decimal] = None  # type: ignore[assignment]
    # Top y of the line-item table on each page, so other extractors know where it starts.
    table_top: dict[int, float] = None  # type: ignore[assignment]
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.totals is None:
            self.totals = {}
        if self.table_top is None:
            self.table_top = {}
        if self.notes is None:
            self.notes = []

    def absorb(self, part: LineItemResult) -> None:
        self.items += part.items
        if part.taxable_total is not None:
            self.taxable_total = part.taxable_total
        self.totals.update(part.totals)


def _amount(text: str) -> Decimal | None:
    found = P.find_amounts(text or "")
    return found[-1].value if found else None


def _tax_amount(text: str) -> Decimal | None:
    """A tax cell: a dash or 'Nil' is an explicit zero, an empty cell stays unknown."""
    value = _amount(text)
    if value is None and (text or "").strip() and P.NIL_RE.fullmatch(text.strip()):
        return Decimal("0")
    return value


def _date(text: str, dayfirst: bool) -> object:
    found = P.find_date(text or "", dayfirst)
    return found[0] if found else None


# A subtotal row inside the line-item table, whose amount is the invoice taxable value.
# A tax row ("Total IGST", "Total Tax") or a payable row is excluded: those are not taxable values.
TOTAL_ROW_RE = re.compile(
    r"\s*(?:sub\s*-?\s*)?total"
    r"(?!\s*(?:igst|cgst|sgst|utgst|gst|tax|amount\s+payable|payable|due|invoice\s+value))"
    r"(?:\s+(?:taxable|value|amount|amt))?\b", re.I)


def _stop_text(row: list[str], mapping: dict[str, int]) -> str:
    """The label of a row that ends the line items (a total, a tax line, amount in words), else ''.

    The label is often not under the description column: right-aligned "Sub Total" text lands in
    whichever column it happens to overlap, and an "Amount Chargeable (in words) :" label sits in
    the first column while the words themselves sit under the description. So every cell is
    checked, even when the description cell has text of its own.
    """
    desc = row[mapping["description"]] if mapping["description"] < len(row) else ""
    if P.SECTION_STOP_RE.match(desc):
        return desc
    for cell in row:
        text = (cell or "").strip()
        if text and P.SECTION_STOP_RE.match(text):
            return text
    return ""


def rows_to_items(rows: list[list[str]], mapping: dict[str, int], page: int, dayfirst: bool) -> LineItemResult:
    result = LineItemResult(items=[])
    dcol = mapping["description"]
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        stop = _stop_text(row, mapping)
        if stop:
            # Only a subtotal row carries the taxable value; a tax line (IGST, CGST...) does not.
            if TOTAL_ROW_RE.match(stop):
                col = mapping.get("taxable", mapping.get("amount"))
                value = _amount(row[col]) if col is not None and col < len(row) else None
                if value is None:
                    value = _amount(stop)
                if value is not None:
                    result.taxable_total = value
                # The same row states the invoice-level tax under each tax column, and under the
                # line-total column the grand total of every service charged.
                for kind in ("igst", "cgst", "sgst"):
                    index = mapping.get(kind)
                    if index is not None and index < len(row):
                        tax = _tax_amount(row[index])
                        if tax is not None:
                            result.totals[kind] = tax
                grand = mapping.get("amount") if "taxable" in mapping else None
                if grand is not None and grand < len(row):
                    overall = _amount(row[grand])
                    if overall is not None:
                        result.totals["total"] = overall
            continue
        cell = (lambda kind: row[mapping[kind]].strip()
                if kind in mapping and mapping[kind] < len(row) else "")
        desc = cell("description")
        if P.NOISE_RE.search(desc):
            continue
        has_own = any(cell(k) for k in OWN_ROW_KINDS)
        if not has_own and desc and result.items:
            # A row carrying only description text continues the line above.
            previous = result.items[-1]
            previous.description = f"{previous.description} {desc}".strip()
            continue
        if not desc and not has_own:
            continue
        hsn = cell("hsn") or None
        if hsn:
            digits = re.search(r"\d{4,8}", hsn)
            hsn = digits.group(0) if digits else None
        else:
            desc, hsn = P.split_hsn(desc)
        if not desc and not any(cell(k) for k in AMOUNT_KINDS):
            continue
        # With a taxable column of its own, the amount column is this line's total (taxable plus
        # its taxes). Without one, the amount column *is* the taxable value, as it always was.
        amount = _amount(cell("amount"))
        has_taxable_column = "taxable" in mapping
        taxable = _amount(cell("taxable")) if has_taxable_column else amount
        result.items.append(LineItem(
            sr_no=cell("serial").strip(" .)") or None,
            description=re.sub(r"\s{2,}", " ", desc) or None,
            hsn_sac=hsn,
            period_from=_date(cell("date_from"), dayfirst),
            period_to=_date(cell("date_to"), dayfirst),
            count=_amount(cell("qty")),
            rate=_amount(cell("rate")),
            taxable_value=taxable,
            igst=_tax_amount(cell("igst")),
            cgst=_tax_amount(cell("cgst")),
            sgst=_tax_amount(cell("sgst")),
            line_total=amount if has_taxable_column else None,
            page=page,
        ))
    return result


# ---------------------------------------------------------------------------- ruled tables


def _from_tables(tables: list[TableContent], dayfirst: bool) -> LineItemResult:
    result = LineItemResult(items=[])
    active: dict | None = None
    for table in sorted(tables, key=lambda t: (t.page, t.bbox[1])):
        rows = table.rows
        found = find_header(rows)
        if found:
            mapping, header_rows, start = found
            active = {"mapping": mapping, "width": len(_merge_header_rows(rows, header_rows, start)),
                      "page": table.page}
            body = rows[start + header_rows:]
            # Where the line items begin: the header row, not the top of an outer page border.
            header_y = table.row_tops[start] if start < len(table.row_tops) else table.bbox[1]
        elif active and table.page >= active["page"] and rows and len(rows[0]) == active["width"]:
            body, mapping = rows, active["mapping"]  # continuation on a later page, header not repeated
            active["page"] = table.page
            header_y = table.bbox[1]
        else:
            continue
        top = result.table_top.get(table.page)
        result.table_top[table.page] = header_y if top is None else min(top, header_y)
        result.absorb(rows_to_items(body, mapping, table.page, dayfirst))
    return result


# ---------------------------------------------------------------------------- text columns


def _header_segments(page: PageContent, line_index: int) -> list[tuple[str, Segment]] | None:
    """Header columns on a line, taking in a second header line (From/To under a spanning title)."""
    found: list[tuple[str, Segment]] = []
    for offset in (0, 1):
        if line_index + offset >= len(page.lines):
            break
        for seg in page.lines[line_index + offset]:
            kind = classify(seg.text)
            if kind:
                found.append((kind, seg))
        if offset == 0 and not any(k == "description" for k, _ in found):
            return None
    # Drop a spanning header that fully contains narrower sub-headers ("Transaction Date").
    kept = [
        (kind, seg) for kind, seg in found
        if not any(other is not seg and seg.x0 <= other.x0 and other.x1 <= seg.x1 for _, other in found)
    ]
    by_kind: dict[str, tuple[str, Segment]] = {}
    for kind, seg in kept:
        if kind not in by_kind or _prefer(seg.text, by_kind[kind][1].text):
            by_kind[kind] = (kind, seg)
    return sorted(by_kind.values(), key=lambda ks: ks[1].x0) or None


def _assign(segments: list[Segment], columns: list[tuple[str, Segment]]) -> list[str]:
    """Place each segment of a data line into the column it overlaps most."""
    cells = [""] * len(columns)
    for seg in segments:
        best, score = None, 0.0
        for index, (_, header) in enumerate(columns):
            overlap = min(seg.x1, header.x1) - max(seg.x0, header.x0)
            if overlap > score:
                best, score = index, overlap
        if best is None:  # no overlap: fall back to the nearest column centre
            centre = (seg.x0 + seg.x1) / 2
            best = min(range(len(columns)),
                       key=lambda i: abs(centre - (columns[i][1].x0 + columns[i][1].x1) / 2))
        cells[best] = f"{cells[best]} {seg.text}".strip()
    return cells


def _from_text(pages: list[PageContent], dayfirst: bool) -> LineItemResult:
    result = LineItemResult(items=[])
    for page in pages:
        for line_index in range(len(page.lines)):
            columns = _header_segments(page, line_index)
            if not columns:
                continue
            mapping = {kind: index for index, (kind, _) in enumerate(columns)}
            header_bottom = max(seg.y1 for _, seg in columns)
            rows: list[list[str]] = []
            for line in page.lines[line_index + 1:]:
                if min(s.y0 for s in line) < header_bottom:
                    continue  # still inside the stacked header
                if any(P.SECTION_STOP_RE.match(s.text) for s in line):
                    rows.append(_assign(line, columns))
                    break
                rows.append(_assign(line, columns))
            result.table_top.setdefault(page.number, min(seg.y0 for _, seg in columns))
            result.absorb(rows_to_items(rows, mapping, page.number, dayfirst))
            break  # one line-item table per page
    return result


def extract_line_items(doc: ExtractedDocument, dayfirst: bool = True) -> LineItemResult:
    result = _from_tables(doc.tables, dayfirst)
    if not result.items:
        result = _from_text(doc.readable_pages, dayfirst)
    return result
