import pytest

from app.pdf_text_extractor import PdfExtractionError, extract_document, is_pdf_bytes
from tests import synthetic_invoices as S


def test_text_pdf_is_readable_with_segments(tmp_path):
    doc = extract_document(S.igst_ruled(tmp_path / "a.pdf"), "Original Name.pdf")
    assert doc.file_name == "Original Name.pdf"
    assert doc.page_count == 1
    page = doc.pages[0]
    assert page.readable
    assert "INV/24-25/0007" in page.text
    # Two-column line: Bill To and Ship To must be separate segments.
    label_line = next(line for line in page.lines if line[0].text == "Bill To:")
    assert [s.text for s in label_line] == ["Bill To:", "Ship To:"]


def test_ruled_table_detected_by_pdfplumber(tmp_path):
    doc = extract_document(S.igst_ruled(tmp_path / "a.pdf"))
    assert doc.tables, "line-item table should be detected"
    header = doc.tables[0].rows[0]
    assert "Description of Services" in header
    assert "Taxable Value" in header


def test_multipage_pdf_reads_all_pages(tmp_path):
    doc = extract_document(S.multipage(tmp_path / "m.pdf"))
    assert doc.page_count == 2
    assert all(p.readable for p in doc.pages)
    assert {t.page for t in doc.tables} == {1, 2}


def test_scanned_pdf_has_no_readable_pages(tmp_path):
    doc = extract_document(S.scanned(tmp_path / "s.pdf", pages=2))
    assert doc.page_count == 2
    assert doc.readable_pages == []
    assert doc.unreadable_pages == [1, 2]


def test_mixed_pdf_flags_only_scanned_page(tmp_path):
    doc = extract_document(S.mixed_text_and_scanned(tmp_path / "mixed.pdf"))
    assert [p.readable for p in doc.pages] == [True, False]
    assert doc.unreadable_pages == [2]


def test_corrupt_pdf_raises(tmp_path):
    with pytest.raises(PdfExtractionError):
        extract_document(S.corrupt(tmp_path / "c.pdf"))


def test_non_pdf_bytes_detected():
    assert is_pdf_bytes(b"%PDF-1.7\n...")
    assert not is_pdf_bytes(b"PK\x03\x04 this is a zip")
