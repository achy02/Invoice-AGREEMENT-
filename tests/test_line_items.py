"""Line-item extraction: one row per service, with its own dates, HSN/SAC and amounts."""
from datetime import date
from decimal import Decimal

import pytest

from app import invoice_patterns as P
from app.invoice_line_items import classify, find_header, map_columns
from tests import synthetic_invoices as S
from tests.conftest import issue_texts


def test_four_services_each_become_a_row(tmp_path, extract):
    """Matches the shared sample: SR.NO | Transaction Date (From|To) | Description | HSN/SAC | Taxable."""
    r = extract(S.multi_service_lines(tmp_path / "ms.pdf"))
    assert len(r.line_items) == 4
    assert [i.sr_no for i in r.line_items] == ["1", "2", "3", "4"]
    assert [i.description for i in r.line_items] == [
        "Risk Radar", "Risk Radar WB", "RECON", "Debt Syndication"]
    assert [i.hsn_sac for i in r.line_items] == ["998314", "998314", "998313", "997159"]
    assert [i.taxable_value for i in r.line_items] == [
        Decimal("450000.00"), Decimal("225000.00"), Decimal("175000.00"), Decimal("150000.00")]
    assert all(i.period_from == date(2026, 2, 1) and i.period_to == date(2026, 2, 28) for i in r.line_items)
    assert r.taxable_value == Decimal("1000000.00")  # the stated invoice subtotal, not the sum
    assert r.total == Decimal("1180000.00")
    assert r.validation_status == "PASSED"


def test_each_service_row_repeats_the_invoice_fields(tmp_path, extract):
    r = extract(S.multi_service_lines(tmp_path / "ms.pdf"))
    rows = r.to_rows()
    assert len(rows) == 4
    assert [row["Description of Services"] for row in rows] == [
        "Risk Radar", "Risk Radar WB", "RECON", "Debt Syndication"]
    assert [row["SR No"] for row in rows] == ["1", "2", "3", "4"]
    assert {row["Tax Invoice No"] for row in rows} == {"TBX/25-26/0231"}
    assert {row["Bill To"] for row in rows} == {"Globex India Ltd\n45 Park Street, Kolkata 700016"}
    assert {row["Total Amount"] for row in rows} == {Decimal("1180000.00")}
    assert {row["Invoice Taxable Value"] for row in rows} == {Decimal("1000000.00")}
    assert {row["Transaction Date From"] for row in rows} == {date(2026, 2, 1)}


def test_hsn_code_kept_out_of_the_description(tmp_path, extract):
    """The HSN code printed inside the description cell belongs in its own column."""
    r = extract(S.multi_service_lines(tmp_path / "hsn.pdf", hsn_column=False))
    assert [i.description for i in r.line_items] == [
        "Risk Radar", "Risk Radar WB", "RECON", "Debt Syndication"]
    assert [i.hsn_sac for i in r.line_items] == ["998314", "998314", "998313", "997159"]
    assert all("998" not in (i.description or "") for i in r.line_items)


def test_per_line_periods_can_differ(tmp_path, extract):
    r = extract(S.multi_service_lines(tmp_path / "mixed.pdf", per_line_dates=False))
    assert r.line_items[0].period_from == date(2026, 2, 1)
    assert r.line_items[3].period_from == date(2026, 1, 1)
    rows = r.to_rows()
    assert rows[0]["Transaction Date From"] == date(2026, 2, 1)
    assert rows[3]["Transaction Date From"] == date(2026, 1, 1)


def test_per_line_tax_columns_give_one_row_per_sr_no(tmp_path, extract):
    """Layout G: each SR No becomes its own row, with its own taxable value and CGST/SGST."""
    r = extract(S.per_line_tax_columns(tmp_path / "tax.pdf"))
    assert [i.sr_no for i in r.line_items] == ["1", "2", "3"]
    assert [i.description for i in r.line_items] == ["Implementation", "Risk Radar", "Debt Syndication"]
    assert [i.hsn_sac for i in r.line_items] == ["997119", "997119", "997119"]
    assert [i.taxable_value for i in r.line_items] == [
        Decimal("1000000.00"), Decimal("300000.00"), Decimal("1600002.00")]
    assert [i.cgst for i in r.line_items] == [
        Decimal("90000.00"), Decimal("27000.00"), Decimal("144000.18")]
    assert [i.sgst for i in r.line_items] == [
        Decimal("90000.00"), Decimal("27000.00"), Decimal("144000.18")]
    assert all(i.period_from == date(2025, 3, 1) and i.period_to == date(2025, 3, 31)
               for i in r.line_items)


def test_invoice_tax_comes_from_the_table_total_row(tmp_path, extract):
    """Not from the first data row sitting under the 'CGST (9%)' column header."""
    r = extract(S.per_line_tax_columns(tmp_path / "tax.pdf"))
    assert r.taxable_value == Decimal("2900002.00")
    assert r.cgst == Decimal("261000.18")
    assert r.sgst == Decimal("261000.18")
    assert r.total == Decimal("3422002.36")
    assert r.validation_status == "PASSED"
    assert "Total does not reconcile" not in " ".join(issue_texts(r))


def test_dashed_tax_column_is_an_explicit_zero(tmp_path, extract):
    """A '-' in the IGST column states that no IGST applies; an absent column leaves it unknown."""
    r = extract(S.per_line_tax_columns(tmp_path / "dash.pdf"))
    assert r.igst == Decimal("0")
    assert all(i.igst == Decimal("0") for i in r.line_items)
    blank = extract(S.per_line_tax_columns(tmp_path / "blank.pdf", igst_dashed=False))
    assert blank.igst is None
    assert all(i.igst is None for i in blank.line_items)


def test_whole_page_border_does_not_hide_the_line_items(tmp_path, extract):
    """The outer border makes the page one table; the header is part-way down, not at row 0."""
    from app.pdf_text_extractor import extract_document
    doc = extract_document(S.per_line_tax_columns(tmp_path / "border.pdf"))
    assert len(doc.tables) == 1, "the outer border should merge the page into a single table"
    assert find_header(doc.tables[0].rows)[2] > 0, "header is below the title and address rows"
    r = extract(tmp_path / "border.pdf")
    assert [i.description for i in r.line_items] == [
        "Implementation", "Risk Radar", "Debt Syndication"]
    assert all(i.hsn_sac == "997119" for i in r.line_items)


def test_invoice_date_wrapped_over_two_lines(tmp_path, extract):
    """A narrow Date column splits '21st April 2025' across two lines."""
    assert extract(S.per_line_tax_columns(tmp_path / "wrap.pdf")).invoice_date == date(2025, 4, 21)
    assert extract(S.per_line_tax_columns(
        tmp_path / "nowrap.pdf", wrap_date=False)).invoice_date == date(2025, 4, 21)


def test_amount_in_words_row_is_not_a_service_line():
    """Its label sits in the first column while the words land under the description."""
    from app.invoice_line_items import rows_to_items

    mapping = {"serial": 0, "description": 3, "hsn": 4, "taxable": 7}
    rows = [
        ["1", "-", "-", "Platform Fees", "998313", "-", "-", "1200000.00"],
        ["", "", "", "Total", "", "", "", "1200000.00"],
        ["Amount Chargeable (in words) :", "", "", "Fourteen Lakh Sixteen Thousand Only", "", "", "", ""],
        ["Remittance Details- Account Name: ACME PVT. LTD. IFSC Code: RATN0000088", "", "", "", "", "", "", ""],
    ]
    result = rows_to_items(rows, mapping, page=1, dayfirst=True)
    assert [i.description for i in result.items] == ["Platform Fees"]
    assert result.taxable_total == Decimal("1200000.00")


@pytest.mark.parametrize("cell,expected", [
    ("1 ,000,000", Decimal("1000000")),   # pdfplumber splits the number; PyMuPDF does not
    ("1 80,000", Decimal("180000")),
    ("2 36,000", Decimal("236000")),
    ("6 ,80,000", Decimal("680000")),     # Indian grouping
    ("1,000,000", Decimal("1000000")),    # already clean
    ("1,234.56", Decimal("1234.56")),
])
def test_number_split_by_a_stray_space_is_repaired(cell, expected):
    from app.pdf_text_extractor import _clean_cell

    from app.invoice_line_items import _amount
    assert _amount(_clean_cell(cell)) == expected


@pytest.mark.parametrize("cell", ["01 02 2026", "Platform Fees", "2 units", "1 2"])
def test_cells_that_are_not_one_number_are_left_alone(cell):
    from app.pdf_text_extractor import _clean_cell

    assert _clean_cell(cell) == cell


def test_address_block_stops_at_the_line_item_table(tmp_path, extract):
    """The Bill To block must not swallow the column headers printed below it."""
    r = extract(S.per_line_tax_columns(tmp_path / "tax.pdf"))
    assert r.bill_to.startswith("IMPERATIVE BUSINESS VENTURES")
    assert r.bill_to.endswith("Maharashtra, 400604")
    for noise in ("SR", "Transaction Date", "Description of Goods", "HSN"):
        assert noise not in r.bill_to, r.bill_to
    assert r.customer_gstin == "27AACCI7893N1ZQ"


def test_line_sum_mismatch_is_flagged(tmp_path, extract, extractor):
    r = extract(S.multi_service_lines(tmp_path / "ms.pdf"))
    r.line_items[0].taxable_value = Decimal("9,00,000".replace(",", ""))
    from app.invoice_validator import validate_record
    validate_record(r)
    assert any("Service lines add up to" in t for t in issue_texts(r, "Taxable Value"))
    assert r.validation_status == "WARNINGS"


def test_single_service_invoice_still_gives_one_row(tmp_path, extract):
    r = extract(S.po_before_invoice(tmp_path / "one.pdf"))
    assert len(r.line_items) == 1
    rows = r.to_rows()
    assert len(rows) == 1
    assert rows[0]["Description of Services"] == "Office supplies"
    assert rows[0]["Taxable Value"] == Decimal("2000.00")


def test_wrapped_description_stays_with_its_line(tmp_path, extract):
    r = extract(S.cgst_sgst_text(tmp_path / "w.pdf"))
    assert len(r.line_items) == 2
    assert r.line_items[0].description == (
        "Statutory audit fees for FY 2023-24 including limited review of quarterly results")
    assert r.line_items[1].description == "Tax advisory retainer"
    assert r.line_items[0].hsn_sac == "998221"


def test_multipage_line_items_continue(tmp_path, extract):
    r = extract(S.multipage(tmp_path / "mp.pdf"))
    assert len(r.line_items) == 5
    assert [i.sr_no for i in r.line_items] == ["1", "2", "3", "4", "5"]
    assert r.line_items[4].description == "Quarterly vulnerability assessment"
    assert r.line_items[4].page == 2


def test_totals_row_is_not_a_service_line(tmp_path, extract):
    for builder in (S.igst_ruled, S.cgst_sgst_text, S.multi_service_lines):
        r = extract(builder(tmp_path / f"{builder.__name__}.pdf"))
        for line in r.line_items:
            assert not P.SECTION_STOP_RE.match(line.description or "x"), line.description
            assert line.description, f"{builder.__name__} produced a line with no description"


@pytest.mark.parametrize("header,kind", [
    ("Description of Goods / Services", "description"),
    ("Description of Goods and Services", "description"),
    ("Particulars", "description"),
    ("Nature of Services", "description"),
    ("HSN/SAC", "hsn"),
    ("SAC Code", "hsn"),
    ("SR. NO", "serial"),
    ("Sl No.", "serial"),
    ("From", "date_from"),
    ("Transaction Date From", "date_from"),
    ("To", "date_to"),
    ("Taxable Value", "taxable"),
    ("IGST Amount", "igst"),
    ("CGST", "cgst"),
    ("Qty", "qty"),
    ("Rate", "rate"),
    ("Amount", "amount"),
])
def test_column_header_classification(header, kind):
    assert classify(header) == kind


def test_stacked_header_rows_are_merged():
    rows = [
        ["SR. NO", "Transaction Date", "", "Description of Goods / Services", "Taxable Value"],
        ["", "From", "To", "", ""],
        ["1", "01-02-2026", "28-02-2026", "Risk Radar", "4,50,000.00"],
    ]
    mapping, header_rows, start = find_header(rows)
    assert (header_rows, start) == (2, 0)
    assert mapping == {"serial": 0, "date_from": 1, "date_to": 2, "description": 3, "taxable": 4}


def test_header_found_below_title_and_address_rows():
    """A page with an outer border comes back as one table whose first rows are not line items."""
    rows = [
        ["TAX INVOICE", "", "", "", ""],
        ["ACME PRIVATE LIMITED GSTIN: 27AAJCT1769M1ZN", "", "", "", ""],
        ["Bill To GLOBEX INDIA LTD, Kolkata 700016", "", "", "", ""],
        ["SR. NO", "Transaction Date", "", "Description of Goods / Services", "Taxable value"],
        ["", "From", "To", "", ""],
        ["1", "01-02-2026", "28-02-2026", "Risk Radar", "3,00,000.00"],
    ]
    mapping, header_rows, start = find_header(rows)
    assert (header_rows, start) == (2, 3)
    assert mapping == {"serial": 0, "date_from": 1, "date_to": 2, "description": 3, "taxable": 4}


def test_amount_column_preferred_over_rate_column():
    assert map_columns(["Description", "IGST Rate", "IGST Amount"]) == {"description": 0, "igst": 2}


@pytest.mark.parametrize("text,expected", [
    ("Risk Radar 998314", ("Risk Radar", "998314")),
    ("Cloud hosting (SAC: 998315)", ("Cloud hosting", "998315")),
    ("Consulting HSN 99831400", ("Consulting", "99831400")),
    ("Audit fees for FY 2023-24", ("Audit fees for FY 2023-24", None)),
    ("Retainer 2024", ("Retainer 2024", None)),
    ("998314", ("998314", None)),
])
def test_split_hsn(text, expected):
    assert P.split_hsn(text) == expected
