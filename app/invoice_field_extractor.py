"""Identify the required invoice fields in extracted PDF content.

Strategy, per field:
1. Find label segments (keyword/label detection, see invoice_patterns.FIELD_LABELS).
2. Read the value from the same segment, then segments to the right on the same line
   (stopping at the next label), then segments directly below the label.
3. Rank candidates by label priority and how they were found; flag conflicts instead of
   silently picking one; leave fields blank rather than guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import config
from . import invoice_patterns as P
from .invoice_line_items import extract_line_items
from .models import ExtractedDocument, InvoiceRecord, Issue, PageContent, Segment, Severity

# How a candidate value was located. 'Weak' methods lose to any strong candidate.
HOW_RANK = {"same": 0, "nil": 0, "column": 1, "table": 1, "below": 2, "sum": 3, "text": 3, "month": 3}
WEAK = {"below", "sum", "text", "month"}

# Words that make a following label a reference to another document ("Against Tax Invoice No").
REFERENCE_PREFIX_RE = re.compile(r"(?:against|ref|reference|vide|as\s+per)\s*[:\-]?\s*$", re.I)
# How far into a segment to look for a label that was swallowed by a narrow column gap.
_MAX_EMBEDDED_STARTS = 12


@dataclass
class Hit:
    field: str
    priority: float
    page: PageContent
    line_index: int
    seg_index: int
    seg: Segment
    remainder: str
    label: str


@dataclass
class Candidate:
    value: object
    priority: float
    how: str
    page: int
    label: str
    order: tuple[int, int, int]
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple:
        return (self.how in WEAK, self.priority, HOW_RANK[self.how])


def _clean_header(text: str) -> str:
    return re.sub(r"[\s:*]+$", "", text.strip())


def _fmt(value: object) -> str:
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, tuple):
        return " to ".join(_fmt(v) for v in value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class InvoiceFieldExtractor:
    def __init__(self, vendor_rules: list[dict] | None = None, dayfirst: bool | None = None):
        self.vendor_rules = P.load_vendor_rules(config.VENDOR_RULES_FILE) if vendor_rules is None else vendor_rules
        self.dayfirst = config.DATE_DAYFIRST if dayfirst is None else dayfirst

    def extract(self, doc: ExtractedDocument) -> InvoiceRecord:
        return _ExtractionRun(doc, self.vendor_rules, self.dayfirst).run()


class _ExtractionRun:
    def __init__(self, doc: ExtractedDocument, vendor_rules: list[dict], dayfirst: bool):
        self.doc = doc
        self.dayfirst = dayfirst
        self.pages = doc.readable_pages
        self.labels, self.matched_rules = P.labels_for_document(doc.full_text, vendor_rules)
        self.record = InvoiceRecord(
            source_file=doc.file_name, page_count=doc.page_count, unreadable_pages=doc.unreadable_pages
        )
        self._hits: dict[str, list[Hit]] = {}
        # Filled by extract_line_items, consumed by extract_amounts and address_block.
        self.table_taxable_candidates: list[Candidate] = []
        self.table_tax_candidates: dict[str, list[Candidate]] = {"igst": [], "cgst": [], "sgst": []}
        self.table_total_candidates: list[Candidate] = []
        self.line_table_top: dict[int, float] = {}
        all_label_res = [rx for lst in self.labels.values() for _, rx in lst]
        self.label_segs = {
            id(seg)
            for page in self.pages
            for line in page.lines
            for seg in line
            if any(rx.match(seg.text) for rx in all_label_res)
        }

    # ------------------------------------------------------------------ helpers

    def issue(self, fld: str, description: str, severity: Severity, value: object = "", page: int | None = None,
              action: str = "") -> None:
        self.record.issues.append(
            Issue(fld, description, severity, _fmt(value) if value not in ("", None) else "", page, action)
        )

    def hits(self, fld: str) -> list[Hit]:
        if fld not in self._hits:
            found = []
            for page in self.pages:
                for li, line in enumerate(page.lines):
                    for si, seg in enumerate(line):
                        for prio, rx in self.labels.get(fld, []):
                            m = rx.match(seg.text)
                            if m:
                                found.append(Hit(fld, prio, page, li, si, seg, seg.text[m.end():], m.group(0).strip()))
                                break
            self._hits[fld] = found or self.embedded_hits(fld)
        return self._hits[fld]

    def embedded_hits(self, fld: str) -> list[Hit]:
        """Labels found part-way through a segment, used only when the field has none at a start.

        Two columns whose gap is narrower than a word space are read as one segment, which
        swallows the label: "TRANSBNK SOLUTIONS PRIVATE LIMITED Tax Invoice No: TSD/25-26/0117".
        Matching mid-segment is looser than matching at a start, so it is a last resort.
        """
        found: list[Hit] = []
        labels = self.labels.get(fld, [])
        if not labels:
            return found
        for page in self.pages:
            for li, line in enumerate(page.lines):
                for si, seg in enumerate(line):
                    text = seg.text
                    for space in list(re.finditer(r"\s+", text))[:_MAX_EMBEDDED_STARTS]:
                        start = space.end()
                        # "Against Tax Invoice No" refers to another document, not this one.
                        if REFERENCE_PREFIX_RE.search(text[:start]):
                            continue
                        tail = text[start:]
                        hit = None
                        for prio, rx in labels:
                            m = rx.match(tail)
                            if m:
                                hit = Hit(fld, prio, page, li, si, seg, tail[m.end():], m.group(0).strip())
                                break
                        if hit is not None:
                            found.append(hit)
                            break  # one label per segment is enough
        return found

    def same_line_texts(self, hit: Hit) -> list[str]:
        texts = []
        remainder = re.sub(r"^[\s:\-#.)\]|]+", "", hit.remainder)
        if remainder:
            texts.append(remainder)
        for seg in hit.page.lines[hit.line_index][hit.seg_index + 1:]:
            if id(seg) in self.label_segs:
                break
            texts.append(seg.text)
        return texts

    def below_segments(self, hit: Hit, max_lines: int = 3) -> list[Segment]:
        out: list[Segment] = []
        seg, lines = hit.seg, hit.page.lines
        for li in range(hit.line_index + 1, min(len(lines), hit.line_index + 1 + max_lines)):
            line = lines[li]
            if min(s.y0 for s in line) - seg.y1 > 3 * seg.height:
                break
            for s in line:
                overlap = min(s.x1, seg.x1) - max(s.x0, seg.x0)
                if overlap > 0 or abs(s.x0 - seg.x0) < 15:
                    if id(s) in self.label_segs:
                        return out
                    out.append(s)
                    break
        return out

    def _cand(self, hit: Hit, value: object, how: str, **extra) -> Candidate:
        return Candidate(value, hit.priority, how, hit.page.number, hit.label,
                         (hit.page.number, hit.line_index, hit.seg_index), extra)

    def choose(self, cands: list[Candidate], column: str, prefer: str = "first", conflict_text: str | None = None,
               allow_rate_sum: bool = False) -> Candidate | None:
        if not cands:
            return None
        best = min(c.key for c in cands)
        top = sorted((c for c in cands if c.key == best), key=lambda c: c.order)
        values = list(dict.fromkeys(c.value for c in top))
        if len(values) == 1:
            return top[0]
        if allow_rate_sum:
            rates = {c.extra.get("rate") for c in top}
            pages = {c.page for c in top}
            lines = {c.order[:2] for c in top}
            if None not in rates and len(rates) == len(top) and len(pages) == 1 and len(lines) == len(top):
                total = sum((c.value for c in top), Decimal("0"))
                self.issue(column, f"Summed {len(top)} separate tax lines at rates "
                                   f"{', '.join(sorted(r + '%' for r in rates))}", Severity.WARNING, total,
                           top[0].page, "Confirm the summed tax amount")
                return Candidate(total, top[0].priority, top[0].how, top[0].page, top[0].label, top[0].order)
        chosen = top[0] if prefer == "first" else top[-1]
        self.issue(
            column,
            (conflict_text or "Conflicting values found") + f": {', '.join(_fmt(v) for v in values)}; "
            f"used {_fmt(chosen.value)} (page {chosen.page})",
            Severity.WARNING, chosen.value, chosen.page, "Check the invoice and correct the value if needed",
        )
        return chosen

    def set_field(self, attr: str, column: str, cand: Candidate | None) -> None:
        if cand is None:
            return
        setattr(self.record, attr, cand.value)
        self.record.field_pages[column] = cand.page
        if cand.how == "below" and attr in {"taxable_value", "igst", "total", "cgst", "sgst"}:
            self.issue(column, f"Value read from below the label '{cand.label}' (column layout); verify",
                       Severity.WARNING, cand.value, cand.page, "Verify the amount against the invoice")

    # ------------------------------------------------------------------ amounts

    def amount_candidates(self, fld: str) -> list[Candidate]:
        cands = []
        for hit in self.hits(fld):
            priority = hit.priority - (0.5 if hit.label.lower().startswith("total") else 0)
            rate_m = P.RATE_RE.search(hit.seg.text)
            rate = rate_m.group(1) if rate_m else None
            texts = self.same_line_texts(hit)
            amounts = [a for t in texts for a in P.find_amounts(t)]
            if amounts:
                c = self._cand(hit, amounts[-1].value, "same", rate=rate, grouping_ok=amounts[-1].grouping_ok)
            elif texts and P.NIL_RE.fullmatch(" ".join(texts)):
                c = self._cand(hit, Decimal("0"), "nil", rate=rate)
            else:
                c = None
                for seg in self.below_segments(hit):
                    found = P.find_amounts(seg.text)
                    if found:
                        c = self._cand(hit, found[0].value, "below", rate=rate, grouping_ok=found[0].grouping_ok)
                        break
            if c:
                c.priority = priority
                if not c.extra.get("grouping_ok", True):
                    self.issue(fld, "Unusual digit grouping in amount; verify", Severity.WARNING, c.value, c.page)
                cands.append(c)
        return cands

    def extract_amounts(self) -> None:
        r = self.record
        for fld, column in (("igst", "IGST Amount"), ("cgst", "CGST Amount"), ("sgst", "SGST Amount")):
            cands = self.amount_candidates(fld) + self.table_tax_candidates[fld]
            chosen = self.choose(cands, column, "last", allow_rate_sum=True)
            self.set_field(fld, column, chosen)
            if chosen is not None and chosen.label.startswith("sum of"):
                self.issue(column, f"Taken as the {chosen.label} of the line-item table",
                           Severity.INFO, chosen.value, chosen.page)

        other = self.amount_candidates("other_tax")
        if other:
            distinct = {(c.label.lower(), c.value) for c in other if c.how != "below"}
            if distinct:
                r.other_tax = sum((v for _, v in distinct), Decimal("0"))
                r.field_pages["Other Tax Amount"] = other[0].page

        if r.igst is None and not self.hits("igst") and (r.cgst is not None or r.sgst is not None):
            r.igst = Decimal("0")
            self.issue("IGST Amount", "No IGST line; invoice charges CGST/SGST (intra-state), IGST recorded as 0",
                       Severity.INFO, "0")

        for attr in ("round_off", "discount", "other_charges"):
            cands = [c for c in self.amount_candidates(attr) if c.how != "below"]
            if cands:
                hit_text = " ".join(h.seg.text for h in self.hits(attr)).lower()
                value = cands[-1].value
                if attr == "round_off" and (re.search(r"\(\s*-\s*\)|less", hit_text)) and value > 0:
                    value = -value
                setattr(r, attr, abs(value) if attr != "round_off" else value)

        taxable_cands = self.amount_candidates("taxable") + self.table_taxable_candidates
        taxable = self.choose(taxable_cands, "Taxable Value", "last")
        self.set_field("taxable_value", "Taxable Value", taxable)
        if taxable is not None:
            if taxable.how == "sum":
                self.issue("Taxable Value", "No stated taxable subtotal; computed as the sum of line-item taxable values",
                           Severity.WARNING, taxable.value, taxable.page, "Confirm against the invoice")
            elif taxable.priority >= 3:
                self.issue("Taxable Value", f"Taken from '{taxable.label}' (no explicit Taxable Value label)",
                           Severity.INFO, taxable.value, taxable.page)

        total = self.choose(self.amount_candidates("total") + self.table_total_candidates,
                            "Total Amount", "last", conflict_text="Conflicting invoice totals")
        self.set_field("total", "Total Amount", total)
        if total is not None and total.priority >= 3 and total.how not in WEAK:
            self.issue("Total Amount", f"Taken from a generic '{total.label}' label", Severity.INFO, total.value, total.page)

    # ------------------------------------------------------------------ identifiers & dates

    def text_candidates(self, fld: str, parse) -> list[Candidate]:
        cands = []
        for hit in self.hits(fld):
            found = None
            for t in self.same_line_texts(hit)[:2]:
                value = parse(t)
                if value is not None:
                    found = self._cand(hit, value, "same")
                    break
            if found is None:
                below = self.below_segments(hit)
                for seg in below:
                    value = parse(seg.text)
                    if value is not None:
                        found = self._cand(hit, value, "below")
                        break
                if found is None and len(below) > 1:
                    # A narrow column wraps the value over two lines ("31st August" / "2025").
                    value = parse(" ".join(s.text for s in below))
                    if value is not None:
                        found = self._cand(hit, value, "below")
            if found:
                cands.append(found)
        return cands

    def extract_identifiers(self) -> None:
        r = self.record
        r.po_numbers = list(dict.fromkeys(c.value for c in self.text_candidates("po_no", P.find_document_number)))
        inv = self.choose(self.text_candidates("invoice_no", P.find_document_number), "Tax Invoice No", "first",
                          conflict_text="Multiple different invoice numbers found (the PDF may contain more than one invoice)")
        self.set_field("invoice_no", "Tax Invoice No", inv)

        def date_only(text: str):
            found = P.find_date(text, self.dayfirst)
            return found[0] if found else None

        self.set_field("invoice_date", "Invoice Date",
                       self.choose(self.text_candidates("invoice_date", date_only), "Invoice Date", "first"))

    def _has_period_heading(self, page: PageContent, line_index: int, x0: float, x1: float) -> bool:
        """True if a heading such as 'Transaction Date' spans the given columns, just above them."""
        for prev in page.lines[max(0, line_index - 2):line_index]:
            for seg in prev:
                if seg.x1 > x0 - 4 and seg.x0 < x1 + 4 and P.PERIOD_PARENT_RE.fullmatch(_clean_header(seg.text)):
                    return True
        return False

    def period_from_columns(self) -> list[Candidate]:
        """A 'From' / 'To' column pair in a table, with the period dates in the rows beneath.

        Only dates count as values, so 'From'/'To' columns holding places (freight invoices)
        produce nothing. Across several rows, the widest period is used.
        """
        cands: list[Candidate] = []
        for page in self.pages:
            for li, line in enumerate(page.lines):
                f = t = None
                for seg in line:
                    header = _clean_header(seg.text)
                    if f is None and P.FROM_HEADER_RE.fullmatch(header):
                        f = seg
                    elif f is not None and t is None and seg.x0 > f.x0 and P.TO_HEADER_RE.fullmatch(header):
                        t = seg
                if f is None or t is None:
                    continue
                starts: list[date] = []
                ends: list[date] = []
                for ln in page.lines[li + 1:li + 1 + 25]:
                    if any(P.SECTION_STOP_RE.match(s.text) for s in ln):
                        break
                    span = [s for s in ln if s.x1 > f.x0 - 4 and s.x0 < t.x1 + 4]
                    start = end = None
                    for s in span:
                        found = P.find_date(s.text, self.dayfirst)
                        if not found:
                            continue
                        in_from = (min(s.x1, f.x1) - max(s.x0, f.x0)) >= (min(s.x1, t.x1) - max(s.x0, t.x0))
                        if in_from and start is None:
                            start = found[0]
                        elif not in_from and end is None:
                            end = found[0]
                    if start and end is None:
                        # Narrow columns: both dates can land in one segment.
                        pair = P.find_all_dates(" ".join(s.text for s in span), self.dayfirst)
                        if len(pair) >= 2:
                            start, end = pair[0], pair[1]
                    if start and end:
                        starts.append(start)
                        ends.append(end)
                if not starts:
                    continue
                heading = self._has_period_heading(page, li, f.x0, t.x1)
                cands.append(Candidate(
                    (min(starts), max(ends)), 1 if heading else 2.5, "column", page.number,
                    f"'{f.text.strip()}'/'{t.text.strip()}' columns", (page.number, li, 0),
                    {"spread": len(set(zip(starts, ends))) > 1, "heading": heading},
                ))
        return cands

    def extract_period(self) -> None:
        r = self.record
        ranges: list[Candidate] = self.period_from_columns()
        singles: list[Hit] = []
        for hit in self.hits("period_range"):
            joined = " ".join(self.same_line_texts(hit))
            rng = P.find_date_range(joined, self.dayfirst)
            month = P.MONTH_ONLY_RE.match(joined) or P.MONTH_PERIOD_RE.search(joined)
            if rng:
                ranges.append(self._cand(hit, (rng[0], rng[1]), "same", raw=rng[2]))
            elif month and (bounds := P.month_bounds(month.group("mon"), month.group("year"))):
                ranges.append(self._cand(hit, bounds, "month", raw=month.group(0).strip()))
            else:
                below = " ".join(s.text for s in self.below_segments(hit, 2))
                rng = P.find_date_range(below, self.dayfirst)
                if rng:
                    ranges.append(self._cand(hit, (rng[0], rng[1]), "below", raw=rng[2]))
                elif P.find_date(joined, self.dayfirst):
                    singles.append(hit)

        froms: list[Candidate] = []
        tos: list[Candidate] = []
        for fld, bucket in (("period_from", froms), ("period_to", tos)):
            for hit in self.hits(fld):
                texts = self.same_line_texts(hit)
                joined = " ".join(texts)
                rng = P.find_date_range(joined, self.dayfirst)
                if rng and fld == "period_from":
                    ranges.append(self._cand(hit, (rng[0], rng[1]), "same", raw=rng[2]))
                    continue
                found = P.find_date(texts[0], self.dayfirst) if texts else None
                how = "same"
                # A bare "From:"/"To:" only means a period when the date is on the same line;
                # "To: <customer name>" is a recipient heading, not a period.
                bare = P.BARE_PERIOD_LABEL_RE.fullmatch(hit.label) is not None
                if not found and not bare:
                    below = self.below_segments(hit, 2)
                    found = P.find_date(below[0].text, self.dayfirst) if below else None
                    how = "below"
                if found:
                    bucket.append(self._cand(hit, found[0], how))

        if not ranges and not froms and not tos:
            # Fall back to an unlabelled "date to date" range anywhere in the text.
            for page in self.pages:
                for li, line in enumerate(page.lines):
                    joined = "  ".join(s.text for s in line)
                    rng = P.find_date_range(joined, self.dayfirst)
                    month = P.MONTH_PERIOD_RE.search(joined)
                    if rng:
                        ranges.append(Candidate((rng[0], rng[1]), 9, "text", page.number, "", (page.number, li, 0),
                                                {"raw": rng[2]}))
                    elif month and (bounds := P.month_bounds(month.group("mon"), month.group("year"))):
                        ranges.append(Candidate(bounds, 9, "month", page.number, "", (page.number, li, 0),
                                                {"raw": month.group(0).strip()}))

        col_from, col_to = "Transaction Date From", "Transaction Date To"
        if ranges:
            chosen = self.choose(ranges, col_from, "first", conflict_text="Multiple transaction periods found")
            r.period_from, r.period_to = chosen.value
            r.field_pages[col_from] = r.field_pages[col_to] = chosen.page
            raw = chosen.extra.get("raw", "")
            if chosen.how == "column":
                if chosen.extra.get("spread"):
                    # Not a problem: every service line is exported as its own row carrying its
                    # own period, and this invoice-level range only covers lines that state none.
                    self.issue(col_from, f"Service lines cover different transaction periods in the {chosen.label}; "
                                         f"each line keeps its own period and the invoice range spans them all",
                               Severity.INFO, chosen.value, chosen.page)
                if not chosen.extra.get("heading"):
                    self.issue(col_from, f"Period read from {chosen.label} with no period heading above them",
                               Severity.INFO, chosen.value, chosen.page)
            elif chosen.how == "month":
                self.issue(col_from, f"Period stated as a month ('{raw}'); dates set to the first and last day of that month",
                           Severity.WARNING, raw, chosen.page, "Confirm the service period")
            elif chosen.how == "text":
                self.issue(col_from, f"Period '{raw}' found in the text without an explicit period label",
                           Severity.WARNING, raw, chosen.page, "Confirm this is the service/transaction period")
            return
        self.set_field("period_from", col_from, self.choose(froms, col_from, "first"))
        self.set_field("period_to", col_to, self.choose(tos, col_to, "first"))
        if singles and r.period_from is None and r.period_to is None:
            hit = singles[0]
            self.issue(col_from, f"Period label '{hit.label}' has a single date; start/end could not be determined",
                       Severity.WARNING, hit.seg.text, hit.page.number, "Enter the transaction period manually")

    # ------------------------------------------------------------------ parties

    def address_block(self, hit: Hit) -> tuple[list[str], list[str]]:
        """Lines of the address block under (or after) a label, plus any GSTINs inside it."""
        page, seg = hit.page, hit.seg
        line = page.lines[hit.line_index]
        right = line[hit.seg_index + 1].x0 - 1 if hit.seg_index + 1 < len(line) else page.width
        left = seg.x0
        collected: list[str] = []
        gstins: list[str] = []     # from this block's own identifier lines
        spillover: list[str] = []  # merged in from a neighbouring column
        first = re.sub(r"^[\s:\-]+", "", hit.remainder).strip()
        if first:
            collected.append(first)
        prev_bottom = seg.y1
        # An address never continues into the line-item table below it.
        table_top = self.line_table_top.get(page.number, float("inf"))
        for li in range(hit.line_index + 1, min(len(page.lines), hit.line_index + 14)):
            ln = page.lines[li]
            top = min(s.y0 for s in ln)
            if top >= table_top:
                break
            if top - prev_bottom > (2.2 if collected else 4.0) * seg.height:
                break
            starts = [s for s in ln if left - 12 <= s.x0 <= left + 40 and s.x0 < right]
            if not starts:
                continue
            parts = [starts[0]]
            for s in ln[ln.index(starts[0]) + 1:]:
                if s.x0 < right and s.x0 - parts[-1].x1 < 40:
                    parts.append(s)
                else:
                    break
            text = " ".join(p.text for p in parts)
            prev_bottom = max(p.y1 for p in parts)
            if P.ID_LINE_RE.match(text) or P.GSTIN_RE.search(text.upper()) and len(text) < 40:
                gstins += P.GSTIN_RE.findall(text.upper())
                continue
            # A neighbouring column merged into this line brings its GSTIN with it. Keep the
            # identifier but drop it from the address, and rank it behind this block's own:
            # it probably belongs to the party in that other column.
            text, embedded = P.split_gstins(text)
            spillover += embedded
            if not text:
                continue
            if id(parts[0]) in self.label_segs or P.ADDRESS_STOP_RE.match(text) or \
                    P.DESC_HEADER_RE.fullmatch(_clean_header(parts[0].text)):
                break
            collected.append(text)
            if len(collected) >= 8:
                break
        return collected, gstins + spillover

    def extract_parties(self) -> None:
        r = self.record
        combined = self.hits("bill_ship")
        bill_hits = sorted(self.hits("bill_to"), key=lambda h: h.priority)
        ship_hits = sorted(self.hits("ship_to"), key=lambda h: h.priority)
        self.party_gstins: list[str] = []

        def block_for(hits: list[Hit], column: str) -> tuple[str | None, Hit | None]:
            for hit in hits:
                lines, gstins = self.address_block(hit)
                if lines and P.DATE_RE.fullmatch(lines[0].strip()):
                    continue  # a bare "To:"/"From:" that heads a period column, not a party
                if lines:
                    self.party_gstins += gstins
                    if column == "Bill To" and gstins and r.customer_gstin is None:
                        r.customer_gstin = gstins[0]
                    r.field_pages[column] = hit.page.number
                    return "\n".join(lines), hit
            return None, None

        if combined and not bill_hits:
            text, hit = block_for(combined, "Bill To")
            if text:
                r.bill_to = r.ship_to = text
                r.field_pages["Ship To"] = hit.page.number
                self.issue("Ship To", "Combined 'Bill & Ship To' section; Ship To set equal to Bill To", Severity.INFO)
            return
        r.bill_to, _ = block_for(bill_hits, "Bill To")
        if ship_hits:
            r.ship_to, hit = block_for(ship_hits, "Ship To")
            if r.ship_to is None:
                self.issue("Ship To", "Ship To label found but no address could be read beneath it",
                           Severity.WARNING, page=ship_hits[0].page.number, action="Enter Ship To manually")

    def extract_supplier(self) -> None:
        r = self.record
        for hit in sorted(self.hits("supplier"), key=lambda h: h.priority):
            lines, gstins = self.address_block(hit)
            if lines and not P.find_date(lines[0], self.dayfirst):
                r.supplier_name = lines[0]
                if gstins and r.supplier_gstin is None:
                    r.supplier_gstin = gstins[0]
                break
        if r.supplier_name is None and self.pages:
            first_party_y = min((h.seg.y0 for h in self.hits("bill_to") + self.hits("ship_to")
                                 if h.page is self.pages[0]), default=float("inf"))
            for line in self.pages[0].lines:
                seg = line[0]
                if seg.y0 >= first_party_y:
                    break
                text = seg.text.strip()
                if (len(text) >= 3 and sum(ch.isalpha() for ch in text) >= 3 and not P.TITLE_NOISE_RE.match(text)
                        and id(seg) not in self.label_segs and not P.GSTIN_RE.search(text.upper())
                        and "@" not in text and not P.find_date(text, self.dayfirst)):
                    r.supplier_name = text
                    self.issue("Supplier Name", "Inferred from the document header (no supplier label)", Severity.INFO,
                               text, self.pages[0].number)
                    break

        for fld, attr in (("supplier_gstin", "supplier_gstin"), ("customer_gstin", "customer_gstin")):
            for hit in self.hits(fld):
                texts = self.same_line_texts(hit) + [s.text for s in self.below_segments(hit, 1)]
                found = [g for t in texts for g in P.GSTIN_RE.findall(t.upper())]
                if found:
                    setattr(r, attr, found[0])
                    break
        if r.supplier_gstin is None:
            all_gstins = list(dict.fromkeys(P.GSTIN_RE.findall(self.doc.full_text.upper())))
            others = [g for g in all_gstins if g not in self.party_gstins and g != r.customer_gstin]
            if others:
                r.supplier_gstin = others[0]

        currencies = P.detect_currencies(self.doc.full_text)
        if currencies:
            r.currency = currencies[0]
            if len(currencies) > 1:
                self.issue("Currency", f"Multiple currency markers found ({', '.join(currencies)}); used {currencies[0]}",
                           Severity.WARNING, currencies[0])

    # ------------------------------------------------------------------ line items

    def extract_line_items(self) -> None:
        """One LineItem per service, plus the invoice-level description and taxable candidates."""
        r = self.record
        found = extract_line_items(self.doc, self.dayfirst)
        r.line_items = found.items
        self.line_table_top = found.table_top
        if not found.items:
            return
        pages = [i.page for i in found.items if i.page]
        descriptions = [i.description for i in found.items if i.description]
        if descriptions:
            r.description = "\n".join(dict.fromkeys(descriptions))
            r.field_pages["Description of Services"] = min(pages, default=1)

        page = min(pages, default=1)
        # The table's own total row states the invoice-level tax; reading it beats reading the
        # first data row from under a "CGST (9%)" column header.
        for kind, candidates in self.table_tax_candidates.items():
            if kind in found.totals:
                candidates.append(
                    Candidate(found.totals[kind], 1, "table", page, "line-item table total", (page, 0, 0)))
                continue
            # No figure on the Total row (that cell is often left blank when the tax is nil),
            # but every line states one: their sum is the invoice's tax for that kind. Reading
            # the column the table identified beats reading text from under a column header.
            per_line = [getattr(item, kind) for item in found.items]
            if per_line and all(value is not None for value in per_line):
                candidates.append(Candidate(sum(per_line, Decimal("0")), 2, "table", page,
                                            f"sum of the {kind.upper()} column", (page, 0, 0)))
        # The table's Total row also gives the invoice total; a labelled total on the page is
        # trusted over it, so this mainly rescues invoices that state no total anywhere else.
        if "total" in found.totals:
            self.table_total_candidates.append(
                Candidate(found.totals["total"], 2, "table", page, "line-item table total row", (page, 0, 0)))
        if found.taxable_total is not None:
            self.table_taxable_candidates.append(
                Candidate(found.taxable_total, 2, "table", page, "line-item table total", (page, 0, 0)))
        else:
            per_line = [i.taxable_value for i in found.items if i.taxable_value is not None]
            if per_line and len(per_line) == len(found.items):
                self.table_taxable_candidates.append(
                    Candidate(sum(per_line, Decimal("0")), 4, "sum", page, "line items", (page, 0, 0)))

        missing = [i for i in found.items if i.taxable_value is None]
        if missing and len(found.items) > 1:
            self.issue("Taxable Value",
                       f"{len(missing)} of {len(found.items)} service lines state no taxable value of their own; "
                       f"those cells are left blank rather than splitting the invoice total",
                       Severity.WARNING, page=page,
                       action="Enter the taxable value for each service line, or check the invoice layout")

    # ------------------------------------------------------------------ main

    def run(self) -> InvoiceRecord:
        r = self.record
        for number in self.doc.unreadable_pages:
            self.issue("Page", "Page has no machine-readable text (scanned image?). Not processed: no OCR in this version",
                       Severity.WARNING, "SCANNED_OR_UNREADABLE_NO_OCR", number, "Enter data from this page manually")
        for note in self.doc.notes:
            self.issue("Document", note, Severity.INFO)
        if self.matched_rules:
            self.issue("Document", "Vendor rules applied: " + ", ".join(self.matched_rules), Severity.INFO)
        if not self.pages:
            return r
        self.extract_identifiers()
        self.extract_period()
        # Before the parties, so an address block knows where the line-item table starts.
        self.extract_line_items()  # also gathers table-based taxable and tax candidates
        self.extract_parties()
        self.extract_supplier()
        self.extract_amounts()
        self.finish_line_items()
        return r

    def finish_line_items(self) -> None:
        """Fill a single-line invoice from the invoice-level values, and inherit the period."""
        r = self.record
        r.ensure_line_items()
        if r.single_line:
            line = r.line_items[0]
            for attr in ("taxable_value", "igst", "cgst", "sgst"):
                if getattr(line, attr) is None:
                    setattr(line, attr, getattr(r, attr))
            if line.description is None:
                line.description = r.description
            # One service line means the invoice total is also that line's total.
            if line.line_total is None:
                line.line_total = r.total
        for line in r.line_items:
            if line.period_from is None and line.period_to is None:
                line.period_from, line.period_to = r.period_from, r.period_to
        # A period stated per line overrides the invoice-level span.
        line_periods = [(i.period_from, i.period_to) for i in r.line_items if i.period_from]
        if line_periods and r.period_from is None:
            r.period_from = min(p[0] for p in line_periods)
            r.period_to = max((p[1] for p in line_periods if p[1]), default=None)
            r.field_pages.setdefault("Transaction Date From", r.line_items[0].page or 1)
