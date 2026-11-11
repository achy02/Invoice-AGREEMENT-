"""Uploading a whole invoice folder: sub-folders are scanned and their paths kept."""
import pytest
from openpyxl import load_workbook

from app import config
from app.batch_processor import BatchError
from app.models import FileStatus
from main import run_extract
from tests import synthetic_invoices as S


def build_archive(root):
    """A folder of invoices arranged in sub-folders, with some non-PDF clutter."""
    (root / "2025" / "Q1").mkdir(parents=True)
    (root / "2025" / "Q2").mkdir(parents=True)
    (root / "2026" / "Q4" / "revised").mkdir(parents=True)
    S.igst_ruled(root / "2025" / "Q1" / "a.pdf")
    S.cgst_sgst_text(root / "2025" / "Q1" / "b.pdf")
    S.multi_service_lines(root / "2025" / "Q2" / "c.pdf")
    S.per_line_tax_columns(root / "2026" / "Q4" / "revised" / "d.pdf")
    S.po_before_invoice(root / "top.pdf")
    (root / "2025" / "notes.txt").write_text("not a pdf")
    (root / "2025" / "Q2" / "readme.md").write_text("nor this")
    return root


def test_cli_scans_sub_folders(tmp_path, capsys):
    archive = build_archive(tmp_path / "archive")
    assert run_extract([str(archive)], str(tmp_path / "out")) == 0
    out = capsys.readouterr().out
    assert "Found 5 PDF file(s)." in out
    for name in ("2025/Q1/a.pdf", "2025/Q1/b.pdf", "2025/Q2/c.pdf",
                 "2026/Q4/revised/d.pdf", "top.pdf"):
        assert name in out, name
    # The non-PDF clutter is never offered to the processor at all.
    assert "notes.txt" not in out and "readme.md" not in out


def test_cli_reports_a_folder_with_no_pdfs(tmp_path, capsys):
    empty = tmp_path / "empty"
    (empty / "sub").mkdir(parents=True)
    (empty / "sub" / "x.txt").write_text("nothing here")
    assert run_extract([str(empty)], str(tmp_path / "out")) == 2
    assert "No PDF files found" in capsys.readouterr().err


def test_sub_folder_path_reaches_the_workbook(tmp_path, processor):
    archive = build_archive(tmp_path / "archive")
    pdfs = sorted(p for p in archive.rglob("*") if p.suffix.lower() == ".pdf")
    labels = [p.relative_to(archive).as_posix() for p in pdfs]
    batch = processor.process_paths(pdfs, labels)

    assert sorted(f.file_name for f in batch.files) == sorted(labels)
    ws = load_workbook(processor.export(batch.batch_id))["Invoice Data"]
    column = [c.value for c in ws[1]].index("Source File Name")
    sources = {row[column] for row in ws.iter_rows(min_row=2, values_only=True)}
    assert "2026/Q4/revised/d.pdf" in sources
    assert "2025/Q2/c.pdf" in sources


def test_same_name_in_two_sub_folders_stays_distinct(tmp_path, processor):
    """Two different invoices sharing a file name must not be confused for one another."""
    root = tmp_path / "archive"
    (root / "apr").mkdir(parents=True)
    (root / "may").mkdir(parents=True)
    S.igst_ruled(root / "apr" / "invoice.pdf")
    S.po_before_invoice(root / "may" / "invoice.pdf")
    pdfs = [root / "apr" / "invoice.pdf", root / "may" / "invoice.pdf"]
    batch = processor.process_paths(pdfs, ["apr/invoice.pdf", "may/invoice.pdf"])

    assert [f.file_name for f in batch.files] == ["apr/invoice.pdf", "may/invoice.pdf"]
    assert all(f.status == FileStatus.PROCESSED for f in batch.files)
    assert {f.record.invoice_no for f in batch.files} == {"INV/24-25/0007", "DS-55/2024"}


def test_identical_file_in_two_sub_folders_is_still_a_duplicate(tmp_path, processor):
    root = tmp_path / "archive"
    (root / "one").mkdir(parents=True)
    (root / "two").mkdir(parents=True)
    S.igst_ruled(root / "one" / "x.pdf")
    (root / "two" / "x.pdf").write_bytes((root / "one" / "x.pdf").read_bytes())
    batch = processor.process_paths(
        [root / "one" / "x.pdf", root / "two" / "x.pdf"], ["one/x.pdf", "two/x.pdf"])

    assert batch.files[0].status == FileStatus.PROCESSED
    assert batch.files[1].status == FileStatus.DUPLICATE
    assert batch.files[1].duplicate_of == "one/x.pdf"


def test_files_are_stored_one_at_a_time(tmp_path, processor):
    """create_batch must consume its input lazily, so a big folder never sits in memory at once."""
    pdf = S.igst_ruled(tmp_path / "src.pdf").read_bytes()
    alive = 0
    peak = 0

    def one_at_a_time():
        nonlocal alive, peak
        for n in range(25):
            alive += 1
            peak = max(peak, alive)
            yield (f"{n}.pdf", pdf.replace(b"INV/24-25/0007", f"INV/{n:010d}".encode()))
            # Handed over and stored, so this file's bytes are no longer needed.
            alive -= 1

    batch = processor.create_batch(one_at_a_time())
    assert len(batch.files) == 25
    assert peak == 1, f"{peak} files were held at once; the input was not consumed lazily"


def test_batch_size_limit_is_enforced_and_cleans_up(tmp_path, processor, monkeypatch):
    pdf = S.igst_ruled(tmp_path / "src.pdf").read_bytes()
    monkeypatch.setattr(config, "MAX_FILES_PER_BATCH", 3)
    with pytest.raises(BatchError, match="Too many files"):
        processor.create_batch((f"{n}.pdf", pdf) for n in range(10))
    # Nothing is left behind on disk from the abandoned batch.
    assert not list((tmp_path / "uploads").rglob("*.pdf"))


def test_empty_upload_is_rejected(processor):
    with pytest.raises(BatchError, match="No files were uploaded"):
        processor.create_batch(iter([]))


def test_default_limits_allow_a_whole_archive():
    assert config.MAX_FILES_PER_BATCH >= 5000
    assert config.MAX_FILE_SIZE_MB >= 50


def test_api_accepts_more_than_starlette_default_file_count(tmp_path, monkeypatch):
    """Starlette refuses >1000 files per form by default; the upload route must raise that cap."""
    from fastapi.testclient import TestClient

    from app import main as app_main

    monkeypatch.setattr(app_main.processor, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(app_main.processor, "output_dir", tmp_path / "outputs")
    monkeypatch.setattr(app_main.processor, "delete_uploads", False)
    pdf = S.po_before_invoice(tmp_path / "src.pdf").read_bytes()

    # 1001 files: one more than the default the framework would allow through.
    files = [("files", (f"f{n}/inv.pdf", pdf, "application/pdf")) for n in range(1001)]
    with TestClient(app_main.app) as client:
        resp = client.post("/api/batches", files=files)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["files"]) == 1001


def test_api_rejects_a_batch_over_the_configured_limit(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as app_main

    monkeypatch.setattr(config, "MAX_FILES_PER_BATCH", 5)
    monkeypatch.setattr(app_main.processor, "upload_dir", tmp_path / "uploads")
    pdf = S.po_before_invoice(tmp_path / "src.pdf").read_bytes()
    files = [("files", (f"{n}.pdf", pdf, "application/pdf")) for n in range(6)]
    with TestClient(app_main.app) as client:
        resp = client.post("/api/batches", files=files)
    assert resp.status_code == 400
    assert "INVOICE_MAX_FILES_PER_BATCH" in resp.json()["detail"]


def test_upload_name_cannot_escape_the_upload_folder(tmp_path, processor):
    """A name carrying directory parts is kept for display but never used as a path."""
    pdf = S.igst_ruled(tmp_path / "src.pdf").read_bytes()
    batch = processor.create_batch([("../../evil.pdf", pdf), ("a/b/c/deep.pdf", pdf)])
    stored = [f.stored_path for f in batch.files if f.stored_path]
    uploads = (tmp_path / "uploads").resolve()
    for path in stored:
        assert uploads in path.resolve().parents, path
        assert path.parent.resolve() == (uploads / batch.batch_id).resolve()
    assert batch.files[0].file_name == "../../evil.pdf"  # display name is untouched
