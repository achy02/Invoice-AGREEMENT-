from datetime import date
from decimal import Decimal

from app.invoice_validator import flag_duplicate_invoices, validate_record
from app.models import InvoiceRecord, Severity
from tests.conftest import issue_texts


def complete_record(**overrides) -> InvoiceRecord:
    values = dict(
        source_file="x.pdf", page_count=1, bill_to="Globex", ship_to="Globex Pune", description="Hosting",
        period_from=date(2024, 4, 1), period_to=date(2024, 4, 30), invoice_no="INV-1",
        taxable_value=Decimal("100000.00"), igst=Decimal("18000.00"), total=Decimal("118000.00"),
    )
    values.update(overrides)
    return InvoiceRecord(**values)


def test_complete_record_passes():
    r = validate_record(complete_record())
    assert r.validation_status == "PASSED"
    assert r.extraction_status == "COMPLETE"
    assert not r.review_required


def test_period_order_checked():
    r = validate_record(complete_record(period_from=date(2024, 5, 1), period_to=date(2024, 4, 1)))
    assert r.validation_status == "ERRORS"
    assert any("after end" in t for t in issue_texts(r, "Transaction Date From"))


def test_missing_amounts_are_not_zeroed():
    r = validate_record(complete_record(total=None, taxable_value=None))
    assert r.total is None and r.taxable_value is None
    assert "Total Amount not found" in issue_texts(r, "Total Amount")
    assert r.validation_status == "ERRORS"
    assert r.extraction_status == "PARTIAL"


def test_total_reconciliation_with_tolerance():
    ok = validate_record(complete_record(total=Decimal("118000.40")))
    assert ok.validation_status == "PASSED"
    bad = validate_record(complete_record(total=Decimal("120000.00")))
    assert any("does not reconcile" in t for t in issue_texts(bad, "Total Amount"))
    assert bad.validation_status == "WARNINGS"  # flagged for review, not rejected


def test_reconciliation_with_round_off_and_discount():
    r = validate_record(complete_record(total=Decimal("117000.00"), discount=Decimal("1000.00")))
    assert r.validation_status == "PASSED"
    r = validate_record(complete_record(total=Decimal("118000.00"), igst=Decimal("17999.60"),
                                        round_off=Decimal("0.40")))
    assert r.validation_status == "PASSED"


def test_cgst_sgst_reconcile():
    r = validate_record(complete_record(igst=Decimal("0"), cgst=Decimal("9000.00"), sgst=Decimal("9000.00")))
    assert r.validation_status == "PASSED"


def test_igst_with_cgst_flagged():
    r = validate_record(complete_record(cgst=Decimal("9000.00"), sgst=Decimal("9000.00"),
                                        total=Decimal("136000.00")))
    assert any("both IGST and CGST/SGST" in t for t in issue_texts(r, "IGST Amount"))


def test_invoice_number_equal_to_po_is_error():
    r = validate_record(complete_record(invoice_no="4500012345", po_numbers=["4500012345"]))
    assert r.validation_status == "ERRORS"


def test_missing_igst_without_other_taxes_flagged():
    r = validate_record(complete_record(igst=None, total=Decimal("100000.00")))
    assert any(i.field == "IGST Amount" and i.severity == Severity.WARNING for i in r.issues)


def test_revalidation_replaces_old_validation_issues():
    r = validate_record(complete_record(total=None))
    assert r.validation_status == "ERRORS"
    r.total = Decimal("118000.00")
    validate_record(r)
    assert r.validation_status == "PASSED"


def test_fully_scanned_record():
    r = validate_record(InvoiceRecord(source_file="s.pdf", page_count=2, unreadable_pages=[1, 2]))
    assert r.extraction_status == "SCANNED_OR_UNREADABLE_NO_OCR"
    assert r.review_required


def test_duplicate_invoices_across_files():
    a = validate_record(complete_record(source_file="a.pdf"))
    b = validate_record(complete_record(source_file="b.pdf"))
    c = validate_record(complete_record(source_file="c.pdf", invoice_no="INV-2"))
    flag_duplicate_invoices([a, b, c])
    assert any("Possible duplicate invoice" in t for t in issue_texts(a))
    assert any("Possible duplicate invoice" in t for t in issue_texts(b))
    assert not any("Possible duplicate invoice" in t for t in issue_texts(c))
    assert a.validation_status == "WARNINGS"
