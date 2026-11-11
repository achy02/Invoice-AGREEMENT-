"""Generate the consolidated Excel workbook for a processing batch."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .models import (
    ALL_COLUMNS,
    AMOUNT_COLUMNS,
    DATE_COLUMNS,
    QUANTITY_COLUMNS,
    TEXT_COLUMNS,
    WRAP_COLUMNS,
    FileResult,
    FileStatus,
    InvoiceRecord,
)

AMOUNT_FORMAT = "#,##0.00"
COUNT_FORMAT = "#,##0.####"  # a count is not money: no forced decimals, no currency grouping
DATE_FORMAT = "dd-mmm-yyyy"
HEADER_FONT = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")

COLUMN_WIDTHS = {
    "Bill To": 38, "Ship To": 38, "Description of Services": 50, "Extraction Notes": 70,
    "Tax Invoice No": 22, "Source File Name": 32, "Supplier Name": 30, "Supplier GSTIN": 18,
    "Customer GSTIN": 18, "Extraction Status": 26, "Validation Status": 16,
    "SR No": 8, "HSN / SAC": 12, "Invoice Taxable Value": 20,
    "Count of Transaction": 20, "Rate Per Unit": 14, "Line Total Amount": 18,
}

EXCEPTION_COLUMNS = ["Source File Name", "Source Page Number", "Field Name", "Extracted Value", "Issue Description",
                     "Review Required", "Suggested Action"]

_FILE_FAILURE_TEXT = {
    FileStatus.FAILED: "File could not be read",
    FileStatus.REJECTED: "File rejected",
    FileStatus.DUPLICATE: "Duplicate file (identical SHA-256 hash)",
    FileStatus.SCANNED_OR_UNREADABLE_NO_OCR: "No machine-readable text (scanned image?); no OCR in this version",
}


def build_invoice_dataframe(records: list[InvoiceRecord]) -> pd.DataFrame:
    """One row per service line, columns in the exact required order."""
    rows = [row for r in records for row in r.to_rows()]
    return pd.DataFrame(rows, columns=ALL_COLUMNS, dtype=object)


def workbook_name(now: datetime | None = None) -> str:
    return f"Invoice_Extraction_{(now or datetime.now()):%Y%m%d_%H%M%S}.xlsx"


def _style_header(ws, ncols: int) -> None:
    for col in range(1, ncols + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def _write_invoice_sheet(ws, df: pd.DataFrame) -> None:
    ws.append(ALL_COLUMNS)
    for values in df.itertuples(index=False):
        ws.append([None if v is None or (isinstance(v, float) and pd.isna(v)) else v for v in values])

    for idx, column in enumerate(ALL_COLUMNS, start=1):
        letter = get_column_letter(idx)
        ws.column_dimensions[letter].width = COLUMN_WIDTHS.get(column, 16)
        for row in range(2, len(df) + 2):
            cell = ws.cell(row=row, column=idx)
            if column in QUANTITY_COLUMNS:
                cell.number_format = COUNT_FORMAT
            elif column in AMOUNT_COLUMNS:
                cell.number_format = AMOUNT_FORMAT
            elif column in DATE_COLUMNS:
                cell.number_format = DATE_FORMAT
            elif column in TEXT_COLUMNS and cell.value is not None:
                cell.value = str(cell.value)  # keeps leading zeros in invoice, SR and HSN numbers
                cell.number_format = "@"
            if column in WRAP_COLUMNS:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            else:
                cell.alignment = Alignment(vertical="top")

    _style_header(ws, len(ALL_COLUMNS))
    ws.freeze_panes = "A2"
    last = f"{get_column_letter(len(ALL_COLUMNS))}{len(df) + 1}"
    if len(df):
        table = Table(displayName="InvoiceData", ref=f"A1:{last}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(table)  # Excel tables carry their own filter buttons
    else:
        ws.auto_filter.ref = f"A1:{last}"


def _write_summary_sheet(ws, files: list[FileResult], records: list[InvoiceRecord], generated: datetime) -> None:
    def count(*statuses: FileStatus) -> int:
        return sum(f.status in statuses for f in files)

    metrics = [
        ("Generated at", generated.strftime("%d-%m-%Y %H:%M:%S")),
        ("Total PDFs uploaded", len(files)),
        ("Successfully processed PDFs (no warnings)", count(FileStatus.PROCESSED)),
        ("PDFs processed with extraction warnings", count(FileStatus.PROCESSED_WITH_WARNINGS)),
        ("PDFs that failed", count(FileStatus.FAILED, FileStatus.SCANNED_OR_UNREADABLE_NO_OCR, FileStatus.REJECTED)),
        ("   of which: corrupt / unreadable PDF", count(FileStatus.FAILED)),
        ("   of which: scanned, no machine-readable text (no OCR)", count(FileStatus.SCANNED_OR_UNREADABLE_NO_OCR)),
        ("   of which: rejected (not a PDF / too large / empty)", count(FileStatus.REJECTED)),
        ("Duplicate file count (not processed)", count(FileStatus.DUPLICATE)),
        ("Total invoice records extracted", len(records)),
        ("Service line rows in Invoice Data", sum(len(r.to_rows()) for r in records)),
        ("Records requiring manual review", sum(r.review_required for r in records)),
    ]
    ws.append(["Metric", "Value"])
    for metric in metrics:
        ws.append(list(metric))
    _style_header(ws, 2)

    start = len(metrics) + 4
    headers = ["File Name", "Status", "Pages", "SHA-256", "Duplicate Of", "Messages"]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=start, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for offset, f in enumerate(files, start=1):
        values = [f.file_name, f.status.value, f.page_count, f.sha256, f.duplicate_of, "; ".join(f.messages)]
        for col, value in enumerate(values, start=1):
            ws.cell(row=start + offset, column=col, value=value)
    for letter, width in zip("ABCDEF", (48, 30, 8, 66, 32, 70)):
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "A2"


def _write_exceptions_sheet(ws, files: list[FileResult]) -> None:
    ws.append(EXCEPTION_COLUMNS)
    for f in files:
        if f.record is None:
            if f.status in _FILE_FAILURE_TEXT:
                detail = "; ".join(f.messages)
                ws.append([f.file_name, "", "File", f.duplicate_of or "",
                           _FILE_FAILURE_TEXT[f.status] + (f": {detail}" if detail else ""),
                           "No" if f.status == FileStatus.DUPLICATE else "Yes",
                           "Ignore (already processed)" if f.status == FileStatus.DUPLICATE
                           else "Check the file and process manually if needed"])
            continue
        for issue in f.record.issues:
            ws.append([f.file_name, issue.page, issue.field, issue.value, issue.description,
                       "Yes" if issue.review_required else "No", issue.suggested_action])
    for letter, width in zip("ABCDEFG", (40, 12, 24, 28, 80, 10, 44)):
        ws.column_dimensions[letter].width = width
    for row in ws.iter_rows(min_row=2):
        row[4].alignment = Alignment(wrap_text=True, vertical="top")
    _style_header(ws, len(EXCEPTION_COLUMNS))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:G{max(ws.max_row, 1)}"


def generate_workbook(files: list[FileResult], output_dir: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now()
    records = [f.record for f in files if f.record is not None]
    wb = Workbook()
    _write_invoice_sheet(wb.active, build_invoice_dataframe(records))
    wb.active.title = "Invoice Data"
    _write_summary_sheet(wb.create_sheet("Processing Summary"), files, records, now)
    _write_exceptions_sheet(wb.create_sheet("Extraction Exceptions"), files)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / workbook_name(now)
    suffix = 1
    while path.exists():
        path = output_dir / f"{path.stem.split('_v')[0]}_v{suffix}.xlsx"
        suffix += 1
    wb.save(path)
    return path
