"""Count of Transaction, Rate Per Unit, the per-line total and the invoice total."""
from decimal import Decimal

import pytest

from app.invoice_line_items import classify, rows_to_items
from app.invoice_validator import validate_record
from app.models import ALL_COLUMNS, LINE_COLUMN_TO_ATTR, REQUIRED_COLUMNS, Severity
from tests import synthetic_invoices as S
from tests.conftest import issue_texts

# SR | From | To | Description | HSN | Count | Rate | Taxable | CGST | SGST | IGST | Total Amount Rs.
MAPPING = {"serial": 0, "date_from": 1, "date_to": 2, "description": 3, "hsn": 4, "qty": 5,
           "rate": 6, "taxable": 7, "cgst": 8, "sgst": 9, "igst": 10, "amount": 11}


def row(count, rate, taxable, cgst, sgst, total, sr="1", desc="Platform Fees"):
    return [sr, "01-03-2025", "31-03-2025", desc, "997119", count, rate, taxable,
            cgst, sgst, "-", total]


@pytest.mark.parametrize("header,kind", [
    ("Count of Transaction", "qty"),
    ("Count of Transactions", "qty"),
    ("No. of Transactions", "qty"),
    ("Count", "qty"),
    ("Qty", "qty"),
    ("Rate Per Unit", "rate"),
    ("Rate", "rate"),
    ("Price Per Unit", "rate"),
    ("Total Amount Rs.", "amount"),
    ("Amount (INR)", "amount"),
])
def test_new_column_headers_are_recognised(header, kind):
    assert classify(header) == kind


def test_count_rate_and_line_total_are_extracted():
    result = rows_to_items([row("3", "50,000.00", "150,000.00", "13,500.00", "13,500.00", "177,000.00")],
                           MAPPING, page=1, dayfirst=True)
    line = result.items[0]
    assert line.count == Decimal("3")
    assert line.rate == Decimal("50000.00")
    assert line.taxable_value == Decimal("150000.00")
    assert line.line_total == Decimal("177000.00")


def test_table_total_row_gives_the_invoice_grand_total():
    rows = [
        row("1", "100.00", "100.00", "9.00", "9.00", "118.00", sr="1"),
        row("2", "100.00", "200.00", "18.00", "18.00", "236.00", sr="2"),
        ["", "", "", "Total", "", "", "", "300.00", "27.00", "27.00", "-", "354.00"],
    ]
    result = rows_to_items(rows, MAPPING, page=1, dayfirst=True)
    assert len(result.items) == 2
    assert result.taxable_total == Decimal("300.00")
    assert result.totals["total"] == Decimal("354.00")
    assert result.totals["cgst"] == Decimal("27.00")


def test_dash_placeholders_leave_count_and_rate_blank():
    result = rows_to_items([row("-", "-", "150,000.00", "13,500.00", "13,500.00", "177,000.00")],
                           MAPPING, page=1, dayfirst=True)
    line = result.items[0]
    assert line.count is None and line.rate is None
    assert line.taxable_value == Decimal("150000.00")


def test_count_times_rate_mismatch_is_flagged(tmp_path, extract):
    r = extract(S.counted_services(tmp_path / "bad.pdf", taxable="99,999.00"))
    texts = issue_texts(r, "Taxable Value")
    assert any("count 3 x rate 50,000 = 150,000.00" in t for t in texts), texts
    assert r.review_required


def test_count_times_rate_match_raises_nothing(tmp_path, extract):
    r = extract(S.counted_services(tmp_path / "good.pdf"))
    assert not any("x rate" in t for t in issue_texts(r, "Taxable Value"))
    line = r.line_items[0]
    assert (line.count, line.rate) == (Decimal("3"), Decimal("50000.00"))
    assert line.count * line.rate == line.taxable_value


def test_fractional_rate_is_reported_as_printed(tmp_path, extract):
    """A rate of 0.009 must not be rounded to 0.01 in the message."""
    r = extract(S.counted_services(tmp_path / "frac.pdf", count="814560", rate="0.009",
                                   taxable="7,574.04", cgst="681.66", sgst="681.66",
                                   total="8,937.36"))
    text = " ".join(issue_texts(r, "Taxable Value"))
    assert "rate 0.009" in text, text
    assert "814,560" in text and "e+" not in text


def test_line_total_and_invoice_total_are_separate_columns(tmp_path, extract):
    r = extract(S.counted_services(tmp_path / "two.pdf", second_line=True))
    rows = r.to_rows()
    assert len(rows) == 2
    assert [row["Line Total Amount"] for row in rows] == [Decimal("177000.00"), Decimal("118000.00")]
    # The invoice-level total repeats on every row and is the sum of the line totals.
    assert {row["Total Amount"] for row in rows} == {Decimal("295000.00")}
    assert sum(row["Line Total Amount"] for row in rows) == Decimal("295000.00")
    assert r.validation_status == "PASSED"


def test_line_totals_not_adding_to_the_invoice_total_is_flagged(tmp_path, extract):
    r = extract(S.counted_services(tmp_path / "off.pdf", second_line=True, grand_total="2,95,999.00"))
    assert any("add up to" in t for t in issue_texts(r, "Line Total Amount"))


def test_new_columns_are_line_level_and_required_order_is_unchanged():
    assert REQUIRED_COLUMNS == ALL_COLUMNS[:9]
    for column in ("Count of Transaction", "Rate Per Unit", "Line Total Amount"):
        assert column in LINE_COLUMN_TO_ATTR
        assert column in ALL_COLUMNS
    assert "Total Amount" not in LINE_COLUMN_TO_ATTR  # the invoice total stays invoice level


def test_count_is_written_to_excel_without_a_money_format(tmp_path, processor):
    from openpyxl import load_workbook

    batch = processor.process_paths([S.counted_services(tmp_path / "x.pdf")])
    ws = load_workbook(processor.export(batch.batch_id))["Invoice Data"]
    header = [c.value for c in ws[1]]
    count_cell = ws.cell(row=2, column=header.index("Count of Transaction") + 1)
    rate_cell = ws.cell(row=2, column=header.index("Rate Per Unit") + 1)
    total_cell = ws.cell(row=2, column=header.index("Line Total Amount") + 1)
    assert count_cell.value == 3
    assert count_cell.number_format == "#,##0.####"
    assert rate_cell.value == 50000 and rate_cell.number_format == "#,##0.00"
    assert total_cell.value == 177000 and total_cell.number_format == "#,##0.00"


def test_tax_summed_from_its_column_when_the_total_row_is_blank(tmp_path, extract):
    """The Total row often leaves a nil tax cell empty; the column still states it per line."""
    r = extract(S.counted_services(tmp_path / "blank.pdf", second_line=True, blank_total_igst=True))
    assert r.igst == Decimal("0")
    assert all(line.igst == Decimal("0") for line in r.line_items)
    assert any("sum of the IGST column" in t for t in issue_texts(r, "IGST Amount"))
    # With IGST at 0 the invoice reconciles, and no tax-split warning is raised.
    assert not any("both IGST and CGST/SGST" in t for t in issue_texts(r))
    assert not any("does not reconcile" in t for t in issue_texts(r))
    assert r.validation_status == "PASSED"


@pytest.mark.parametrize("text,expected_text,expected_ids", [
    ("Office 62, Plot-230, Sakhar GSTIN: 27ABOCS0349A1ZG",
     "Office 62, Plot-230, Sakhar", ["27ABOCS0349A1ZG"]),
    ("Thane, Maharashtra GST: 27AACCI7893N1ZQ", "Thane, Maharashtra", ["27AACCI7893N1ZQ"]),
    ("GSTIN: 27AAJCT1769M1ZN", "", ["27AAJCT1769M1ZN"]),
    ("45 Park Street, Kolkata 700016", "45 Park Street, Kolkata 700016", []),
])
def test_split_gstins(text, expected_text, expected_ids):
    from app import invoice_patterns as P

    assert P.split_gstins(text) == (expected_text, expected_ids)


def test_neighbouring_column_gstin_is_kept_out_of_the_address(tmp_path, extract):
    r = extract(S.counted_services(tmp_path / "addr.pdf", merged_gstin=True))
    assert "GSTIN" not in (r.bill_to or ""), r.bill_to
    assert "27ABOCS0349A1ZG" not in (r.bill_to or "")
    assert r.bill_to.splitlines()[-1] == "Thane, Maharashtra, 400604"
    assert r.customer_gstin == "27AACCI7893N1ZQ"


def test_period_spread_note_is_not_a_review_trigger(tmp_path, extract):
    r = extract(S.counted_services(tmp_path / "p.pdf", second_line=True))
    spread = [i for i in r.issues if "different transaction periods" in i.description]
    assert all(i.severity is Severity.INFO for i in spread)
