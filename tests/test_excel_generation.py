import time
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.models import ALL_COLUMNS, REQUIRED_COLUMNS, FileStatus
from tests import synthetic_invoices as S

EXPECTED_REQUIRED = [
    "Bill To", "Ship To", "Transaction Date From", "Transaction Date To", "Description of Services",
    "Taxable Value", "Tax Invoice No", "IGST Amount", "Total Amount",
]


def make_batch_files(tmp_path):
    paths = [
        S.igst_ruled(tmp_path / "a_igst.pdf"),
        S.cgst_sgst_text(tmp_path / "b_cgst.pdf"),
        S.multipage(tmp_path / "c_multi.pdf"),
        S.scanned(tmp_path / "d_scanned.pdf"),
        S.corrupt(tmp_path / "e_corrupt.pdf"),
    ]
    dup = tmp_path / "f_copy_of_a.pdf"
    dup.write_bytes(paths[0].read_bytes())
    txt = tmp_path / "g_notes.txt"
    txt.write_text("not a pdf")
    return paths + [dup, txt]


def test_required_columns_order_constant():
    assert REQUIRED_COLUMNS == EXPECTED_REQUIRED
    assert ALL_COLUMNS[:9] == EXPECTED_REQUIRED


def test_batch_statuses_and_isolation(tmp_path, processor):
    batch = processor.process_paths(make_batch_files(tmp_path))
    status = {f.file_name: f.status for f in batch.files}
    assert status["a_igst.pdf"] == FileStatus.PROCESSED
    assert status["b_cgst.pdf"] == FileStatus.PROCESSED
    assert status["c_multi.pdf"] == FileStatus.PROCESSED
    assert status["d_scanned.pdf"] == FileStatus.SCANNED_OR_UNREADABLE_NO_OCR
    assert status["e_corrupt.pdf"] == FileStatus.FAILED
    assert status["f_copy_of_a.pdf"] == FileStatus.DUPLICATE
    assert status["g_notes.txt"] == FileStatus.REJECTED
    assert len(batch.records) == 3
    dup = next(f for f in batch.files if f.file_name == "f_copy_of_a.pdf")
    assert dup.duplicate_of == "a_igst.pdf"
    # Uploaded copies are removed after processing by default.
    assert not any((tmp_path / "uploads").rglob("*.pdf"))


def test_workbook_structure_and_types(tmp_path, processor):
    batch = processor.process_paths(make_batch_files(tmp_path))
    path = processor.export(batch.batch_id)
    assert path.name.startswith("Invoice_Extraction_") and path.suffix == ".xlsx"

    wb = load_workbook(path)
    assert wb.sheetnames == ["Invoice Data", "Processing Summary", "Extraction Exceptions"]
    ws = wb["Invoice Data"]
    headers = [c.value for c in ws[1]]
    assert headers[:9] == EXPECTED_REQUIRED
    assert headers == ALL_COLUMNS
    # One row per service line: 2 (a_igst) + 2 (b_cgst) + 5 (c_multi) = 9, plus the header.
    assert ws.max_row == 10
    assert ws.freeze_panes == "A2"
    assert "InvoiceData" in ws.tables
    assert all(ws.cell(1, c).font.bold for c in range(1, len(headers) + 1))
    assert not ws.merged_cells.ranges

    col = {name: i + 1 for i, name in enumerate(headers)}
    rows = {ws.cell(r, col["Source File Name"]).value: r for r in range(2, ws.max_row + 1)}
    a = rows["a_igst.pdf"]
    for name in ("Taxable Value", "IGST Amount", "Total Amount"):
        cell = ws.cell(a, col[name])
        assert cell.data_type == "n" and cell.number_format == "#,##0.00"
    assert ws.cell(a, col["Total Amount"]).value == 118000
    assert isinstance(ws.cell(a, col["Transaction Date From"]).value, datetime)
    inv = ws.cell(rows["c_multi.pdf"], col["Tax Invoice No"])
    assert inv.value == "0004567" and inv.data_type == "s"  # leading zeros preserved as text
    assert ws.cell(a, col["Bill To"]).alignment.wrap_text

    # The five services of c_multi each get a row, with the invoice fields repeated.
    multi = [r for r in range(2, ws.max_row + 1) if ws.cell(r, col["Source File Name"]).value == "c_multi.pdf"]
    assert len(multi) == 5
    assert [ws.cell(r, col["Description of Services"]).value for r in multi] == [
        "Network support - Site A", "Network support - Site B", "Firewall management",
        "Endpoint security licences", "Quarterly vulnerability assessment",
    ]
    assert {ws.cell(r, col["Tax Invoice No"]).value for r in multi} == {"0004567"}
    assert {ws.cell(r, col["Total Amount"]).value for r in multi} == {295000}
    assert {ws.cell(r, col["Invoice Taxable Value"]).value for r in multi} == {250000}
    assert [ws.cell(r, col["Taxable Value"]).value for r in multi] == [50000] * 5
    assert [ws.cell(r, col["SR No"]).value for r in multi] == ["1", "2", "3", "4", "5"]

    summary = {r[0].value: r[1].value for r in wb["Processing Summary"].iter_rows(min_row=2, max_row=12)}
    assert summary["Total PDFs uploaded"] == 7
    assert summary["Successfully processed PDFs (no warnings)"] == 3
    assert summary["PDFs that failed"] == 3
    assert summary["Duplicate file count (not processed)"] == 1
    assert summary["Total invoice records extracted"] == 3

    exc = wb["Extraction Exceptions"]
    assert [c.value for c in exc[1]] == ["Source File Name", "Source Page Number", "Field Name", "Extracted Value",
                                         "Issue Description", "Review Required", "Suggested Action"]
    files_with_exceptions = {r[0].value for r in exc.iter_rows(min_row=2)}
    assert {"d_scanned.pdf", "e_corrupt.pdf", "f_copy_of_a.pdf", "g_notes.txt"} <= files_with_exceptions


def test_review_edit_is_exported(tmp_path, processor):
    batch = processor.process_paths([S.no_line_amounts(tmp_path / "n.pdf")])
    record = batch.records[0]
    assert record.taxable_value is None and record.review_required
    processor.update_record(batch.batch_id, 0, {"Taxable Value": "50,000.00", "Ship To": "Globex Pune"})
    assert record.line_items[0].taxable_value == Decimal("50000.00")
    assert record.taxable_value == Decimal("50000.00")  # single line keeps the invoice value in step
    assert record.validation_status == "PASSED"
    ws = load_workbook(processor.export(batch.batch_id))["Invoice Data"]
    headers = [c.value for c in ws[1]]
    assert ws.cell(2, headers.index("Taxable Value") + 1).value == 50000
    assert ws.cell(2, headers.index("Ship To") + 1).value == "Globex Pune"
    assert "Manually edited" in ws.cell(2, headers.index("Extraction Notes") + 1).value


def test_edit_targets_one_service_line(tmp_path, processor):
    """Correcting line 3 of a four-service invoice must leave the other lines alone."""
    batch = processor.process_paths([S.multi_service_lines(tmp_path / "ms.pdf")])
    record = batch.records[0]
    assert len(record.line_items) == 4
    processor.update_record(batch.batch_id, 0, {"Description of Services": "RECON Platform",
                                               "Taxable Value": "1,80,000.00"}, line_index=2)
    assert record.line_items[2].description == "RECON Platform"
    assert record.line_items[2].taxable_value == Decimal("180000.00")
    assert record.line_items[0].description == "Risk Radar"
    assert record.line_items[0].taxable_value == Decimal("450000.00")
    assert record.taxable_value == Decimal("1000000.00")  # invoice subtotal untouched
    ws = load_workbook(processor.export(batch.batch_id))["Invoice Data"]
    headers = [c.value for c in ws[1]]
    descriptions = [ws.cell(r, headers.index("Description of Services") + 1).value for r in range(2, 6)]
    assert descriptions == ["Risk Radar", "Risk Radar WB", "RECON Platform", "Debt Syndication"]


def test_web_api_end_to_end(tmp_path, processor, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "processor", processor)
    client = TestClient(main.app)
    assert "Upload PDF invoices" in client.get("/").text

    files = [
        ("files", ("a.pdf", S.igst_ruled(tmp_path / "a.pdf").read_bytes(), "application/pdf")),
        ("files", ("b.pdf", S.cgst_sgst_text(tmp_path / "b.pdf").read_bytes(), "application/pdf")),
        ("files", ("x.docx", b"PK\x03\x04", "application/octet-stream")),
    ]
    resp = client.post("/api/batches", files=files)
    assert resp.status_code == 200
    batch_id = resp.json()["batch_id"]
    for _ in range(100):
        data = client.get(f"/api/batches/{batch_id}").json()
        if data["complete"]:
            break
        time.sleep(0.1)
    assert data["complete"]
    statuses = [f["status"] for f in data["files"]]
    assert statuses == ["PROCESSED", "PROCESSED", "REJECTED"]
    assert data["files"][0]["record"]["values"]["Total Amount"] == "118000.00"

    bad = client.put(f"/api/batches/{batch_id}/records/0", json={"Total Amount": "abc"})
    assert bad.status_code == 422
    ok = client.put(f"/api/batches/{batch_id}/records/0", json={"Tax Invoice No": "INV/24-25/0007A"})
    assert ok.status_code == 200
    assert ok.json()["record"]["values"]["Tax Invoice No"] == "INV/24-25/0007A"

    xl = client.get(f"/api/batches/{batch_id}/excel")
    assert xl.status_code == 200
    assert xl.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert client.get("/api/batches/nope").status_code == 404
