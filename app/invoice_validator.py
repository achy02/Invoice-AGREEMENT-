"""Validate extracted invoice records and set their status columns.

Validation never changes extracted values; it only records issues for review.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from . import config
from .models import InvoiceRecord, Issue, Severity

# Fields whose absence is flagged, with severity.
_REQUIRED = [
    ("Tax Invoice No", "invoice_no", Severity.ERROR, "Enter the invoice number from the PDF"),
    ("Total Amount", "total", Severity.ERROR, "Enter the invoice total from the PDF"),
    ("Taxable Value", "taxable_value", Severity.WARNING, "Enter the taxable value from the PDF"),
    ("Bill To", "bill_to", Severity.WARNING, "Enter the Bill To name and address"),
    ("Description of Services", "description", Severity.WARNING, "Enter the service description"),
]
_CORE_ATTRS = ["invoice_no", "total", "taxable_value", "bill_to", "description", "igst"]


def _add(record: InvoiceRecord, fld: str, description: str, severity: Severity, value: object = "",
         action: str = "") -> None:
    record.issues.append(Issue(fld, description, severity, "" if value is None else str(value),
                               record.field_pages.get(fld), action, stage="validation"))


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _plain(value: Decimal) -> str:
    """A count or a unit rate as printed: grouped, but with no forced 2 decimals and no
    exponent, so 0.009 stays 0.009 and 2489191 does not become 2.48919e+06."""
    text = format(value.normalize(), "f")
    whole, _, fraction = text.partition(".")
    grouped = f"{int(whole):,}"
    return f"{grouped}.{fraction}" if fraction else grouped


def validate_record(record: InvoiceRecord, tolerance: Decimal | None = None, today: date | None = None) -> InvoiceRecord:
    tolerance = config.AMOUNT_TOLERANCE if tolerance is None else tolerance
    today = today or date.today()
    record.issues = [i for i in record.issues if i.stage != "validation"]
    r = record

    if r.page_count and len(r.unreadable_pages) == r.page_count:
        r.extraction_status = "SCANNED_OR_UNREADABLE_NO_OCR"
        r.validation_status = "ERRORS"
        _add(r, "Document", "No machine-readable text in any page; nothing extracted (no OCR in this version)",
             Severity.ERROR, action="Process this invoice manually")
        return r

    for column, attr, severity, action in _REQUIRED:
        if getattr(r, attr) in (None, ""):
            _add(r, column, f"{column} not found", severity, action=action)

    if r.igst is None:
        if r.cgst is not None or r.sgst is not None:
            _add(r, "IGST Amount", "IGST label present but no amount could be read", Severity.WARNING,
                 action="Enter the IGST amount or 0 if not applicable")
        else:
            _add(r, "IGST Amount", "IGST amount not found and no CGST/SGST found either", Severity.WARNING,
                 action="Enter the IGST amount, or 0 if the invoice has no IGST")
    if r.igst and (r.cgst or r.sgst):
        _add(r, "IGST Amount", "Invoice shows both IGST and CGST/SGST amounts; check the tax split", Severity.WARNING,
             r.igst, "Confirm which taxes apply")

    if not r.ship_to:
        _add(r, "Ship To", "No Ship To section found; left blank", Severity.INFO)

    # Invoice number sanity.
    if r.invoice_no:
        if not any(ch.isdigit() for ch in r.invoice_no):
            _add(r, "Tax Invoice No", "Invoice number contains no digits", Severity.WARNING, r.invoice_no)
        if r.invoice_no in r.po_numbers:
            _add(r, "Tax Invoice No", "Invoice number equals a PO/reference number on the same invoice",
                 Severity.ERROR, r.invoice_no, "Confirm this is the tax invoice number, not the PO number")

    # Dates.
    if r.period_from and r.period_to:
        if r.period_from > r.period_to:
            _add(r, "Transaction Date From", f"Period start {r.period_from:%d-%m-%Y} is after end {r.period_to:%d-%m-%Y}",
                 Severity.ERROR, r.period_from, "Check the transaction period (day/month order?)")
    elif r.period_from or r.period_to:
        missing = "Transaction Date To" if r.period_from else "Transaction Date From"
        _add(r, missing, "Only one end of the transaction period was found", Severity.WARNING,
             action="Enter the missing period date")
    elif not any(i.field.startswith("Transaction Date") for i in r.issues):
        _add(r, "Transaction Date From", "No transaction period found; left blank", Severity.INFO)
    for column, value in (("Invoice Date", r.invoice_date), ("Transaction Date From", r.period_from),
                          ("Transaction Date To", r.period_to)):
        if value and value > today + timedelta(days=366):
            _add(r, column, f"Date {value:%d-%m-%Y} is more than a year in the future", Severity.WARNING, value)

    # Amounts.
    for column, value in (("Taxable Value", r.taxable_value), ("Total Amount", r.total), ("IGST Amount", r.igst)):
        if value is not None and value < 0:
            _add(r, column, "Negative amount (credit note?)", Severity.WARNING, _money(value))
    if r.total is not None and r.taxable_value is not None and r.taxable_value > r.total + tolerance:
        _add(r, "Taxable Value", f"Taxable value {_money(r.taxable_value)} exceeds total {_money(r.total)}",
             Severity.WARNING, _money(r.taxable_value), "Check that the right amounts were picked")

    _reconcile(r, tolerance)
    _check_line_items(r, tolerance)

    # Status columns.
    missing_core = any(getattr(r, a) in (None, "") for a in _CORE_ATTRS)
    if r.unreadable_pages:
        r.extraction_status = "PARTIAL_UNREADABLE_PAGES"
    elif missing_core:
        r.extraction_status = "PARTIAL"
    else:
        r.extraction_status = "COMPLETE"
    severities = {i.severity for i in r.issues}
    r.validation_status = ("ERRORS" if Severity.ERROR in severities
                           else "WARNINGS" if Severity.WARNING in severities else "PASSED")
    return r


def _reconcile(r: InvoiceRecord, tolerance: Decimal) -> None:
    if r.taxable_value is None or r.total is None:
        return
    taxes = [t for t in (r.igst, r.cgst, r.sgst, r.other_tax) if t is not None]
    if not taxes:
        if abs(r.total - r.taxable_value) > tolerance:
            _add(r, "Total Amount", "No tax amounts identified, and total differs from taxable value", Severity.WARNING,
                 _money(r.total), "Check the tax amounts")
        return
    expected = r.taxable_value + sum(taxes, Decimal("0")) + (r.round_off or Decimal("0"))
    if abs(r.total - expected) <= tolerance:
        return
    adjusted = expected + (r.other_charges or Decimal("0")) - (r.discount or Decimal("0"))
    if (r.other_charges or r.discount) and abs(r.total - adjusted) <= tolerance:
        _add(r, "Total Amount", "Total reconciles after other charges/discount", Severity.INFO, _money(r.total))
        return
    _add(
        r, "Total Amount",
        f"Total does not reconcile: taxable {_money(r.taxable_value)} + taxes {_money(sum(taxes, Decimal('0')))}"
        f"{' + round off ' + _money(r.round_off) if r.round_off else ''} = {_money(expected)}, "
        f"invoice total {_money(r.total)} (difference {_money(r.total - expected)})",
        Severity.WARNING, _money(r.total), "Check taxable value, taxes, charges and discounts",
    )


def _check_count_times_rate(r: InvoiceRecord, tolerance: Decimal) -> None:
    """Where a line states a count and a rate, check them against its taxable value.

    Nothing is corrected or filled in: a mismatch is reported so a person can look at it.
    """
    for index, line in enumerate(r.line_items, start=1):
        if line.count is None or line.rate is None:
            continue
        expected = line.count * line.rate
        where = "" if r.single_line else f"Service line {index}: "
        if line.taxable_value is None:
            _add(r, "Taxable Value",
                 f"{where}no taxable value stated, but count {_plain(line.count)} x rate "
                 f"{_plain(line.rate)} = {_money(expected)}", Severity.WARNING, _money(expected),
                 "Check the invoice and enter the taxable value if that is correct")
            continue
        if abs(expected - line.taxable_value) > tolerance:
            _add(r, "Taxable Value",
                 f"{where}count {_plain(line.count)} x rate {_plain(line.rate)} = {_money(expected)}, "
                 f"but the taxable value is {_money(line.taxable_value)} "
                 f"(difference {_money(line.taxable_value - expected)})",
                 Severity.WARNING, _money(line.taxable_value),
                 "Check the count, the rate and the taxable value on the invoice")


def _check_line_totals(r: InvoiceRecord, tolerance: Decimal) -> None:
    """A line's own total should be its taxable value plus the taxes charged on that line."""
    for index, line in enumerate(r.line_items, start=1):
        if line.line_total is None or line.taxable_value is None:
            continue
        taxes = sum((t for t in (line.igst, line.cgst, line.sgst) if t is not None), Decimal("0"))
        expected = line.taxable_value + taxes
        if abs(expected - line.line_total) > tolerance:
            where = "" if r.single_line else f"Service line {index}: "
            _add(r, "Line Total Amount",
                 f"{where}taxable {_money(line.taxable_value)} + taxes {_money(taxes)} = {_money(expected)}, "
                 f"but the line total is {_money(line.line_total)}",
                 Severity.WARNING, _money(line.line_total), "Check the line's amounts on the invoice")


def _check_line_items(r: InvoiceRecord, tolerance: Decimal) -> None:
    r.ensure_line_items()
    _check_count_times_rate(r, tolerance)
    _check_line_totals(r, tolerance)
    if r.single_line:
        return
    blank = [i + 1 for i, line in enumerate(r.line_items) if not (line.description or "").strip()]
    if blank:
        _add(r, "Description of Services", f"Service line(s) {', '.join(map(str, blank))} have no description",
             Severity.WARNING, action="Enter the missing service descriptions")
    per_line = [line.taxable_value for line in r.line_items if line.taxable_value is not None]
    if len(per_line) == len(r.line_items) and r.taxable_value is not None:
        total = sum(per_line, Decimal("0"))
        if abs(total - r.taxable_value) > tolerance:
            _add(r, "Taxable Value",
                 f"Service lines add up to {_money(total)} but the invoice taxable value is "
                 f"{_money(r.taxable_value)} (difference {_money(total - r.taxable_value)})",
                 Severity.WARNING, _money(total), "Check the per-line amounts against the invoice subtotal")
    line_totals = [line.line_total for line in r.line_items if line.line_total is not None]
    if len(line_totals) == len(r.line_items) and r.total is not None:
        total = sum(line_totals, Decimal("0"))
        if abs(total - r.total) > tolerance:
            _add(r, "Line Total Amount",
                 f"Service line totals add up to {_money(total)} but the invoice total is "
                 f"{_money(r.total)} (difference {_money(total - r.total)})",
                 Severity.WARNING, _money(total), "Check the per-line totals against the invoice total")
    numbers = [line.sr_no for line in r.line_items if line.sr_no]
    if len(set(numbers)) != len(numbers):
        _add(r, "SR No", "Duplicate SR numbers across service lines", Severity.WARNING,
             action="Check that the line-item table was read correctly")


def flag_duplicate_invoices(records: list[InvoiceRecord]) -> None:
    """Flag records in the same batch that share an invoice number with the same supplier GSTIN or total."""
    seen: dict[str, InvoiceRecord] = {}
    for rec in records:
        if not rec.invoice_no:
            continue
        key = rec.invoice_no.strip().upper()
        other = seen.get(key)
        if other is None:
            seen[key] = rec
            continue
        same_supplier = rec.supplier_gstin and rec.supplier_gstin == other.supplier_gstin
        same_total = rec.total is not None and rec.total == other.total
        if same_supplier or same_total:
            for a, b in ((rec, other), (other, rec)):
                a.issues.append(Issue("Tax Invoice No", f"Possible duplicate invoice: same invoice number as '{b.source_file}'",
                                      Severity.WARNING, a.invoice_no or "", a.field_pages.get("Tax Invoice No"),
                                      "Remove one copy if it is the same invoice", stage="batch"))
                severities = {i.severity for i in a.issues}
                a.validation_status = "ERRORS" if Severity.ERROR in severities else "WARNINGS"
