"""Manage bulk uploads: validation, duplicate detection, per-file processing and status."""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from . import config
from .excel_generator import generate_workbook
from .invoice_field_extractor import InvoiceFieldExtractor
from .invoice_patterns import parse_amount, parse_date
from .invoice_validator import flag_duplicate_invoices, validate_record
from .models import (
    AMOUNT_COLUMNS,
    COLUMN_TO_ATTR,
    DATE_COLUMNS,
    INVOICE_COLUMN_TO_ATTR,
    LINE_COLUMN_TO_ATTR,
    Batch,
    FileResult,
    FileStatus,
    Severity,
)
from .pdf_text_extractor import PdfExtractionError, extract_document, is_pdf_bytes

log = logging.getLogger(__name__)


class BatchError(Exception):
    """The batch as a whole cannot be accepted (e.g. too many files)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)[:120] or "file.pdf"


class BatchProcessor:
    def __init__(self, upload_dir: Path | None = None, output_dir: Path | None = None,
                 extractor: InvoiceFieldExtractor | None = None, delete_uploads: bool | None = None):
        self.upload_dir = Path(upload_dir or config.UPLOAD_DIR)
        self.output_dir = Path(output_dir or config.OUTPUT_DIR)
        self.extractor = extractor or InvoiceFieldExtractor()
        self.delete_uploads = config.DELETE_UPLOADS_AFTER_PROCESSING if delete_uploads is None else delete_uploads
        self.batches: dict[str, Batch] = {}
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ intake

    def create_batch(self, files: Iterable[tuple[str, bytes]]) -> Batch:
        """Validate and store uploaded files. Rejected and duplicate files get their final status here.

        `files` may be a generator that produces one file at a time. Each file's bytes are
        written to disk before the next is asked for, so uploading a large folder does not need
        the whole folder in memory.
        """
        batch = Batch(batch_id=uuid.uuid4().hex[:12], created_at=datetime.now())
        batch_dir = self.upload_dir / batch.batch_id
        seen: dict[str, str] = {}
        max_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
        for index, (name, data) in enumerate(files):
            if index >= config.MAX_FILES_PER_BATCH:
                shutil.rmtree(batch_dir, ignore_errors=True)
                raise BatchError(
                    f"Too many files: the limit is {config.MAX_FILES_PER_BATCH} per batch. "
                    f"Raise INVOICE_MAX_FILES_PER_BATCH in .env and restart, or upload in parts.")
            result = FileResult(file_id=index, file_name=name, size_bytes=len(data))
            batch.files.append(result)
            if not data:
                result.status = FileStatus.REJECTED
                result.messages.append("File is empty.")
                continue
            if not name.lower().endswith(".pdf") or not is_pdf_bytes(data):
                result.status = FileStatus.REJECTED
                result.messages.append("Unsupported file format: only PDF files are accepted.")
                continue
            if len(data) > max_bytes:
                result.status = FileStatus.REJECTED
                result.messages.append(f"File is larger than the {config.MAX_FILE_SIZE_MB} MB limit.")
                continue
            result.sha256 = sha256_bytes(data)
            if result.sha256 in seen:
                result.status = FileStatus.DUPLICATE
                result.duplicate_of = seen[result.sha256]
                result.messages.append(f"Identical to '{seen[result.sha256]}'; not processed again.")
                continue
            seen[result.sha256] = name
            batch_dir.mkdir(parents=True, exist_ok=True)
            result.stored_path = batch_dir / f"{index:04d}_{_safe_name(name)}"
            result.stored_path.write_bytes(data)
        if not batch.files:
            raise BatchError("No files were uploaded.")
        with self.lock:
            self.batches[batch.batch_id] = batch
        return batch

    def create_batch_from_paths(self, paths: list[Path], labels: list[str] | None = None) -> Batch:
        """`labels` names each file; use it to keep a file's sub-folder path (e.g. '2025/April/inv.pdf')."""
        names = labels if labels is not None else [Path(p).name for p in paths]
        if len(names) != len(paths):
            raise BatchError("Each file needs exactly one name.")
        return self.create_batch([(name, Path(p).read_bytes()) for name, p in zip(names, paths)])

    # ------------------------------------------------------------------ processing

    def process_batch(self, batch_id: str) -> Batch:
        batch = self.get(batch_id)
        for result in batch.files:
            if result.status == FileStatus.QUEUED:
                self.process_file(result)
        flag_duplicate_invoices(batch.records)
        for result in batch.files:
            if result.record is not None:
                result.status = self._status_for(result)
        if self.delete_uploads:
            for result in batch.files:
                if result.stored_path and result.stored_path.exists():
                    result.stored_path.unlink()
            batch_dir = self.upload_dir / batch.batch_id
            if batch_dir.exists() and not any(batch_dir.iterdir()):
                batch_dir.rmdir()
        batch.complete = True
        return batch

    def process_file(self, result: FileResult) -> None:
        result.status = FileStatus.PROCESSING
        try:
            doc = extract_document(result.stored_path, result.file_name)
            result.page_count = doc.page_count
            if not doc.readable_pages:
                result.status = FileStatus.SCANNED_OR_UNREADABLE_NO_OCR
                result.messages.append("No page contains machine-readable text. Scanned PDFs need OCR, which this version does not use.")
                return
            record = self.extractor.extract(doc)
            validate_record(record)
            result.record = record
            if doc.unreadable_pages:
                result.messages.append(
                    "Pages without readable text (not processed): " + ", ".join(map(str, doc.unreadable_pages)))
            result.status = self._status_for(result)
        except PdfExtractionError as exc:
            result.status = FileStatus.FAILED
            result.messages.append(str(exc))
        except Exception as exc:  # one bad file must never stop the batch
            log.exception("Unexpected error processing %s", result.file_name)
            result.status = FileStatus.FAILED
            result.messages.append(f"Unexpected error: {exc.__class__.__name__}: {exc}")

    @staticmethod
    def _status_for(result: FileResult) -> FileStatus:
        if result.record is not None and result.record.review_required:
            return FileStatus.PROCESSED_WITH_WARNINGS
        return FileStatus.PROCESSED

    def process_paths(self, paths: list[Path], labels: list[str] | None = None) -> Batch:
        """Synchronous convenience API: create, process and return a batch."""
        batch = self.create_batch_from_paths(paths, labels)
        return self.process_batch(batch.batch_id)

    def start_background(self, batch_id: str) -> None:
        threading.Thread(target=self.process_batch, args=(batch_id,), daemon=True).start()

    # ------------------------------------------------------------------ review & export

    def get(self, batch_id: str) -> Batch:
        with self.lock:
            if batch_id not in self.batches:
                raise KeyError(batch_id)
            return self.batches[batch_id]

    def update_record(self, batch_id: str, file_id: int, changes: dict[str, object],
                      line_index: int | None = None) -> FileResult:
        """Apply user corrections (keyed by Excel column name) and re-run validation.

        `line_index` selects which service line the line-level columns apply to; invoice-level
        columns always apply to the whole invoice.
        """
        batch = self.get(batch_id)
        result = next((f for f in batch.files if f.file_id == file_id), None)
        if result is None or result.record is None:
            raise KeyError(file_id)
        record = result.record
        record.ensure_line_items()
        if line_index is None:
            line_index = 0
        if not 0 <= line_index < len(record.line_items):
            raise KeyError(f"line {line_index}")
        line = record.line_items[line_index]

        parsed: dict[str, object] = {}
        errors = []
        for column, raw in changes.items():
            if column not in COLUMN_TO_ATTR:
                errors.append(f"'{column}' is not an editable column")
                continue
            text = "" if raw is None else str(raw).strip()
            try:
                if column in AMOUNT_COLUMNS:
                    parsed[column] = parse_amount(text)
                elif column in DATE_COLUMNS:
                    parsed[column] = _parse_user_date(text)
                elif column in {"Supplier GSTIN", "Customer GSTIN", "Currency"}:
                    parsed[column] = text.upper() or None
                else:
                    parsed[column] = text or None
            except ValueError as exc:
                errors.append(f"{column}: {exc}")
        if errors:
            raise ValueError("; ".join(errors))

        for column, value in parsed.items():
            target, attr = ((line, LINE_COLUMN_TO_ATTR[column]) if column in LINE_COLUMN_TO_ATTR
                            else (record, INVOICE_COLUMN_TO_ATTR[column]))
            if getattr(target, attr) == value:
                continue
            setattr(target, attr, value)
            target.edited_fields.add(column)
            # With one service line, the line and the invoice hold the same amount.
            if target is line and record.single_line and hasattr(record, attr):
                setattr(record, attr, value)
            if target is line and attr == "description":
                record.description = "\n".join(
                    dict.fromkeys(i.description for i in record.line_items if i.description))
            # A manual correction supersedes extraction warnings for that field.
            record.issues = [i for i in record.issues if not (i.field == column and i.stage == "extraction")]
            if column == "Transaction Date From":
                record.issues = [i for i in record.issues
                                 if not (i.field == "Transaction Date From" and i.severity != Severity.ERROR)]
        validate_record(record)
        result.status = self._status_for(result)
        return result

    def export(self, batch_id: str) -> Path:
        return generate_workbook(self.get(batch_id).files, self.output_dir)


def _parse_user_date(text: str) -> date | None:
    if not text:
        return None
    value = parse_date(text, config.DATE_DAYFIRST)
    if value is None:
        raise ValueError(f"Not a valid date: {text!r}")
    return value
