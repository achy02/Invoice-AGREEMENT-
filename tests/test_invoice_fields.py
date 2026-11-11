from datetime import date
from decimal import Decimal

import pytest

from app import invoice_patterns as P
from app.invoice_field_extractor import InvoiceFieldExtractor
from app.models import Severity
from app.pdf_text_extractor import extract_document
from tests import synthetic_invoices as S
from tests.conftest import issue_texts


def test_igst_invoice_all_fields(tmp_path, extract):
    r = extract(S.igst_ruled(tmp_path / "a.pdf"))
    assert r.bill_to == "Globex India Ltd\n45 Park Street, Kolkata 700016"
    assert r.ship_to == "Globex India Ltd - Pune Plant\nPlot 7, Hinjewadi, Pune 411057"
    assert (r.period_from, r.period_to) == (date(2024, 4, 1), date(2024, 4, 30))
    assert r.description == "Cloud hosting services - April 2024\nManaged backup and monitoring"
    assert r.taxable_value == Decimal("100000.00")
    assert r.invoice_no == "INV/24-25/0007"
    assert r.igst == Decimal("18000.00")
    assert r.total == Decimal("118000.00")
    assert r.invoice_date == date(2024, 5, 15)
    assert r.supplier_name == "Acme Cloud Services Pvt Ltd"
    assert r.supplier_gstin == "29AAACA1234A1Z5"
    assert r.customer_gstin == "19AABCG5678B1Z2"
    assert r.currency == "INR"
    assert r.cgst is None and r.sgst is None
    assert r.po_numbers == ["4500012345"]
    assert r.extraction_status == "COMPLETE"
    assert r.validation_status == "PASSED"
    assert not r.review_required


def test_cgst_sgst_invoice_boxed_layout(tmp_path, extract):
    r = extract(S.cgst_sgst_text(tmp_path / "b.pdf"))
    assert r.invoice_no == "SC-000123"  # value printed below the label, leading zeros kept
    assert r.invoice_date == date(2024, 6, 3)
    assert r.bill_to.startswith("Initech Solutions Pvt Ltd\nLower Parel, Mumbai 400013")
    assert r.cgst == Decimal("11250.00")
    assert r.sgst == Decimal("11250.00")
    assert r.igst == Decimal("0")  # explicitly intra-state, recorded with a note
    assert any("IGST recorded as 0" in t for t in issue_texts(r, "IGST Amount"))
    assert r.taxable_value == Decimal("125000.00")
    assert r.total == Decimal("147500.00")  # 'Rs. 1,47,500.00' in Indian format
    assert r.validation_status == "PASSED"


def test_missing_ship_to_and_period_left_blank(tmp_path, extract):
    r = extract(S.cgst_sgst_text(tmp_path / "b.pdf"))
    assert r.ship_to is None
    assert r.period_from is None and r.period_to is None
    infos = [i for i in r.issues if i.field in ("Ship To", "Transaction Date From")]
    assert infos and all(not i.review_required for i in infos)


def test_multiple_descriptions_with_wrapped_line(tmp_path, extract):
    r = extract(S.cgst_sgst_text(tmp_path / "b.pdf"))
    assert r.description.split("\n") == [
        "Statutory audit fees for FY 2023-24 including limited review of quarterly results",
        "Tax advisory retainer",
    ]
    assert "Declaration" not in r.description


def test_multipage_invoice_keeps_all_descriptions(tmp_path, extract):
    r = extract(S.multipage(tmp_path / "m.pdf"))
    assert r.description.split("\n") == [
        "Network support - Site A", "Network support - Site B", "Firewall management",
        "Endpoint security licences", "Quarterly vulnerability assessment",
    ]
    assert r.invoice_no == "0004567"
    assert (r.period_from, r.period_to) == (date(2024, 6, 1), date(2024, 6, 30))
    assert r.taxable_value == Decimal("250000.00")
    assert r.igst == Decimal("45000.00")
    assert r.total == Decimal("295000.00")
    assert r.source_pages == "1, 2"
    assert r.ship_to.startswith("Contoso Retail - Chennai Warehouse")


def test_taxable_value_derived_from_line_amount_is_flagged(tmp_path, extract):
    """No Taxable Value label, but the single line amount is one: usable, and flagged as derived."""
    r = extract(S.missing_taxable(tmp_path / "d.pdf"))
    assert r.taxable_value == Decimal("10000.00")
    assert any("sum of line-item" in t for t in issue_texts(r, "Taxable Value"))
    assert r.total == Decimal("11800.00")
    assert r.igst == Decimal("1800.00")
    assert r.review_required  # derived, so a human confirms it


def test_taxable_value_absent_is_left_blank(tmp_path, extract):
    """Nothing in the invoice states a taxable value: it must stay blank, never computed."""
    r = extract(S.no_line_amounts(tmp_path / "n.pdf"))
    assert r.taxable_value is None
    assert all(line.taxable_value is None for line in r.line_items)
    assert "Taxable Value not found" in issue_texts(r, "Taxable Value")
    assert r.total == Decimal("59000.00")
    assert r.igst == Decimal("9000.00")
    assert r.extraction_status == "PARTIAL"
    assert r.review_required


def test_conflicting_totals_flagged(tmp_path, extract):
    r = extract(S.igst_ruled(tmp_path / "c.pdf", extra_total="1,20,000.00"))
    assert any("Conflicting invoice totals" in t for t in issue_texts(r, "Total Amount"))
    assert r.review_required


def test_invoice_number_not_confused_with_po(tmp_path, extract):
    r = extract(S.po_before_invoice(tmp_path / "po.pdf"))
    assert r.invoice_no == "DS-55/2024"
    assert r.po_numbers == ["7700123"]


def test_period_from_transaction_date_columns(tmp_path, extract):
    """SR.NO | Transaction Date (From | To) | Description | Taxable Value."""
    r = extract(S.transaction_date_columns(tmp_path / "tc.pdf"))
    assert r.period_from == date(2025, 8, 1)
    assert r.period_to == date(2025, 8, 31)
    assert r.invoice_date == date(2025, 9, 5)  # the invoice date is not mistaken for the period
    assert r.invoice_no == "BRF/2025/0412"
    assert r.description.split("\n") == ["Freight forwarding - Mumbai to Delhi", "Warehouse handling charges"]
    assert r.taxable_value == Decimal("350000.00")
    assert r.total == Decimal("413000.00")
    assert r.field_pages["Transaction Date From"] == 1
    assert r.validation_status == "PASSED"


def test_period_columns_use_widest_range_and_note_it(tmp_path, extract):
    """Lines covering different periods are normal: each is exported as its own row with its
    own dates, and the invoice-level range simply spans them. Noted, but not a reason to review."""
    r = extract(S.transaction_date_columns(tmp_path / "tc2.pdf", same_period=False))
    assert (r.period_from, r.period_to) == (date(2025, 8, 1), date(2025, 8, 31))
    assert any("different transaction periods" in t for t in issue_texts(r, "Transaction Date From"))
    spread = next(i for i in r.issues if "different transaction periods" in i.description)
    assert spread.severity is Severity.INFO
    rows = r.to_rows()
    assert rows[0]["Transaction Date From"] == date(2025, 8, 1)
    assert rows[1]["Transaction Date From"] == date(2025, 8, 15)
    assert not r.review_required


def test_label_swallowed_by_a_narrow_column_gap_is_still_found(tmp_path, extract):
    """Two columns closer than a word space merge into one segment, hiding the label."""
    from tests.synthetic_invoices import make_pdf

    pdf = make_pdf(tmp_path / "merged.pdf", [[
        ("t", 40, 50, "ACME SOLUTIONS PRIVATE LIMITED Tax Invoice No: TSD/25-26/0117", 9),
        ("t", 40, 64, "GSTIN: 27AAJCT1769M1ZN"),
        ("t", 40, 90, "Bill To:"), ("t", 40, 104, "Globex India Ltd"),
        ("t", 40, 140, "Description"), ("t", 400, 140, "Amount"),
        ("t", 40, 156, "Platform Fees"), ("t", 400, 156, "1,000.00"),
        ("t", 300, 180, "Taxable Value"), ("t", 400, 180, "1,000.00"),
        ("t", 300, 194, "IGST @ 18%"), ("t", 400, 194, "180.00"),
        ("t", 300, 208, "Grand Total"), ("t", 400, 208, "1,180.00"),
    ]])
    assert extract(pdf).invoice_no == "TSD/25-26/0117"


def test_credit_note_uses_its_own_number_not_the_invoice_it_offsets(tmp_path, extract):
    from tests.synthetic_invoices import make_pdf

    pdf = make_pdf(tmp_path / "cn.pdf", [[
        ("t", 40, 50, "ACME SOLUTIONS PRIVATE LIMITED", 11),
        ("t", 40, 70, "Credit Note No.: TCNM/25-26/024"),
        ("t", 40, 84, "Against Tax Invoice No: TSM/25-26/0103"),
        ("t", 40, 98, "Invoice Date: 31-08-2025"),
        ("t", 40, 130, "Bill To:"), ("t", 40, 144, "Globex India Ltd"),
        ("t", 40, 180, "Description"), ("t", 400, 180, "Amount"),
        ("t", 40, 196, "Service credit"), ("t", 400, 196, "5,000.00"),
        ("t", 300, 220, "Taxable Value"), ("t", 400, 220, "5,000.00"),
        ("t", 300, 234, "IGST @ 18%"), ("t", 400, 234, "900.00"),
        ("t", 300, 248, "Grand Total"), ("t", 400, 248, "5,900.00"),
    ]])
    r = extract(pdf)
    assert r.invoice_no == "TCNM/25-26/024"
    assert "TSM/25-26/0103" in r.po_numbers


def test_from_to_columns_with_places_are_ignored(tmp_path, extract):
    """A freight invoice's From/To cities must not be read as a period."""
    r = extract(S.freight_from_to_places(tmp_path / "fr.pdf"))
    assert r.period_from is None and r.period_to is None
    assert r.invoice_no == "RR-9001"
    assert r.total == Decimal("21000.00")


def test_bare_from_to_colon_labels(tmp_path, extract):
    r = extract(S.period_colon_labels(tmp_path / "pc.pdf"))
    assert (r.period_from, r.period_to) == (date(2025, 7, 1), date(2025, 7, 31))
    # The same invoice uses "To:" as the recipient heading; it must still find Bill To.
    assert r.bill_to == "Initech Solutions Pvt Ltd\nLower Parel, Mumbai 400013"
    assert r.invoice_date == date(2025, 9, 2)


def test_explicit_period_label_beats_column_pair(tmp_path, extractor):
    """An invoice with both a labelled period and a From/To column pair prefers the label."""
    from app.pdf_text_extractor import extract_document
    items = [
        ("t", 40, 50, "Dual Period Co", 14),
        ("t", 40, 80, "Invoice No: DP-1"),
        ("t", 40, 94, "Service Period: 01/01/2025 to 31/03/2025"),
        ("t", 40, 120, "Bill To:"), ("t", 40, 134, "Globex India Ltd"),
    ]
    items += S.ruled_table([40, 90, 180, 300, 555], 170,
                           [["S.No", "From", "To", "Description of Services"],
                            ["1", "01-02-2025", "28-02-2025", "Monthly retainer"]])
    items += [("t", 300, 240, "Taxable Value"), ("t", 440, 240, "1,000.00"),
              ("t", 300, 254, "Grand Total"), ("t", 440, 254, "1,000.00")]
    r = extractor.extract(extract_document(S.make_pdf(tmp_path / "dual.pdf", [items])))
    assert (r.period_from, r.period_to) == (date(2025, 1, 1), date(2025, 3, 31))


@pytest.mark.parametrize("text", ["From", "From Date", "Period From", "Start Date", "Date From", "W.E.F."])
def test_from_column_headers(text):
    assert P.FROM_HEADER_RE.fullmatch(text)


@pytest.mark.parametrize("text", ["To", "To Date", "Period To", "End Date", "Upto", "Till", "Date To"])
def test_to_column_headers(text):
    assert P.TO_HEADER_RE.fullmatch(text)


@pytest.mark.parametrize("text", ["Transaction Date", "Period", "Dates", "Service Period", "Date Range",
                                  "Billing Period", "Duration"])
def test_period_parent_headers(text):
    assert P.PERIOD_PARENT_RE.fullmatch(text)


@pytest.mark.parametrize("text", ["Description of Services", "Taxable Value", "Invoice No", "HSN", "Amount"])
def test_period_parent_rejects_other_headers(text):
    assert not P.PERIOD_PARENT_RE.fullmatch(text)


def test_vendor_rule_adds_custom_labels(tmp_path):
    path = S.vendor_custom_labels(tmp_path / "v.pdf")
    plain = InvoiceFieldExtractor(vendor_rules=[]).extract(extract_document(path))
    assert plain.invoice_no is None
    rules = [{"name": "Umbrella", "match": {"gstin": "07AAACU9999Q1Z1"},
              "labels": {"invoice_no": ["Bill Ref"], "total": ["Net Receivable"]}}]
    r = InvoiceFieldExtractor(vendor_rules=rules).extract(extract_document(path))
    assert r.invoice_no == "UL-77-0001"
    assert r.total == Decimal("5900.00")


@pytest.mark.parametrize("text,expected", [
    ("INV/24-25/0007", "INV/24-25/0007"),
    (": 000123", "000123"),
    ("# AB-12/34-X", "AB-12/34-X"),
    ("INV 0042 dated 01/02/2024", "INV 0042"),
    ("dated 01/02/2024", None),
    ("01/02/2024", None),
])
def test_document_number_formats(text, expected):
    assert P.find_document_number(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("1,25,000.00", Decimal("125000.00")),
    ("Rs. 12,34,567.50", Decimal("1234567.50")),
    ("INR 1,234,567.89", Decimal("1234567.89")),
    ("₹ 999", Decimal("999")),
    ("(1,000.00)", Decimal("-1000.00")),
])
def test_indian_and_western_amounts(text, expected):
    found = P.find_amounts(text)
    assert found and found[-1].value == expected


def test_amounts_ignore_rates_dates_and_ids():
    assert [a.value for a in P.find_amounts("IGST @ 18% 18,000.00")] == [Decimal("18000.00")]
    assert P.find_amounts("01/04/2024") == []
    assert P.find_amounts("GSTIN 29AAACA1234A1Z5") == []
    assert P.find_amounts("12.5%") == []


def test_parse_amount_strict():
    assert P.parse_amount("1,25,000.00") == Decimal("125000.00")
    assert P.parse_amount("") is None
    with pytest.raises(ValueError):
        P.parse_amount("12,34,5")
    with pytest.raises(ValueError):
        P.parse_amount("abc")


@pytest.mark.parametrize("text,expected", [
    ("01/04/2024", date(2024, 4, 1)),
    ("1-Apr-2024", date(2024, 4, 1)),
    ("2024-07-01", date(2024, 7, 1)),
    ("April 3, 2024", date(2024, 4, 3)),
    ("12.08.2024", date(2024, 8, 12)),
])
def test_dates_day_first(text, expected):
    assert P.find_date(text)[0] == expected


def test_label_variations():
    rx = dict((k, [p for _, p in v]) for k, v in P.FIELD_LABELS.items())
    def matches(field, text):
        return any(p.match(text) for p in rx[field])
    assert matches("invoice_no", "TAX INVOICE NO.")
    assert matches("invoice_no", "Invoice #")
    assert not matches("invoice_no", "Tax Invoice")
    assert not matches("invoice_no", "Purchase Order No")
    assert matches("igst", "Integrated Goods and Services Tax")
    assert not matches("igst", "CGST @ 9%")
    assert not matches("igst", "IGST Rate")
    assert matches("bill_to", "Details of Receiver (Billed to)")
    assert not matches("bill_to", "Customer GSTIN")
    assert not matches("total", "Total Taxable Value")
    assert matches("total", "Grand Total")
