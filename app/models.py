"""Data models shared across the extraction pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

# The first nine columns, in the exact required order.
REQUIRED_COLUMNS = [
    "Bill To",
    "Ship To",
    "Transaction Date From",
    "Transaction Date To",
    "Description of Services",
    "Taxable Value",
    "Tax Invoice No",
    "IGST Amount",
    "Total Amount",
]

SUPPORTING_COLUMNS = [
    "SR No",
    "HSN / SAC",
    "Count of Transaction",
    "Rate Per Unit",
    "Line Total Amount",
    "Source File Name",
    "Source Page Number",
    "Invoice Date",
    "Supplier Name",
    "Supplier GSTIN",
    "Customer GSTIN",
    "Currency",
    "CGST Amount",
    "SGST Amount",
    "Other Tax Amount",
    "Invoice Taxable Value",
    "Extraction Status",
    "Validation Status",
    "Review Required",
    "Extraction Notes",
]

ALL_COLUMNS = REQUIRED_COLUMNS + SUPPORTING_COLUMNS

# Columns whose value belongs to one service line, mapped to LineItem attributes.
LINE_COLUMN_TO_ATTR = {
    "SR No": "sr_no",
    "Transaction Date From": "period_from",
    "Transaction Date To": "period_to",
    "Description of Services": "description",
    "HSN / SAC": "hsn_sac",
    "Count of Transaction": "count",
    "Rate Per Unit": "rate",
    "Taxable Value": "taxable_value",
    "IGST Amount": "igst",
    "CGST Amount": "cgst",
    "SGST Amount": "sgst",
    # This service line's own total (its taxable value plus its taxes), as the invoice prints
    # it under "Total Amount Rs.". The invoice-level "Total Amount" is a separate column.
    "Line Total Amount": "line_total",
}

# Columns that belong to the invoice as a whole, mapped to InvoiceRecord attributes.
INVOICE_COLUMN_TO_ATTR = {
    "Bill To": "bill_to",
    "Ship To": "ship_to",
    "Tax Invoice No": "invoice_no",
    "Total Amount": "total",
    "Invoice Date": "invoice_date",
    "Supplier Name": "supplier_name",
    "Supplier GSTIN": "supplier_gstin",
    "Customer GSTIN": "customer_gstin",
    "Currency": "currency",
    "Other Tax Amount": "other_tax",
    "Invoice Taxable Value": "taxable_value",
}

COLUMN_TO_ATTR = {**LINE_COLUMN_TO_ATTR, **INVOICE_COLUMN_TO_ATTR}
AMOUNT_COLUMNS = {"Taxable Value", "IGST Amount", "Total Amount", "CGST Amount", "SGST Amount", "Other Tax Amount",
                  "Invoice Taxable Value", "Rate Per Unit", "Line Total Amount", "Count of Transaction"}
# Counts are parsed as numbers like the amounts, but written without a money format.
QUANTITY_COLUMNS = {"Count of Transaction"}
DATE_COLUMNS = {"Transaction Date From", "Transaction Date To", "Invoice Date"}
TEXT_COLUMNS = {"Tax Invoice No", "SR No", "HSN / SAC"}
WRAP_COLUMNS = {"Bill To", "Ship To", "Description of Services", "Extraction Notes"}

UNREADABLE_PAGE_STATUS = "SCANNED_OR_UNREADABLE_NO_OCR"


class Severity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class FileStatus(str, Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    PROCESSED_WITH_WARNINGS = "PROCESSED_WITH_WARNINGS"
    SCANNED_OR_UNREADABLE_NO_OCR = UNREADABLE_PAGE_STATUS
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


@dataclass
class Segment:
    """A run of words on one visual line, separated from neighbours by a column-sized gap."""

    page: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)


@dataclass
class PageContent:
    number: int  # 1-based
    width: float
    height: float
    text: str  # normalized text
    raw_text: str  # text exactly as returned by PyMuPDF, kept for debugging
    lines: list[list[Segment]]
    readable: bool


@dataclass
class TableContent:
    page: int
    bbox: tuple[float, float, float, float]
    rows: list[list[str]]
    # Top y of each row in `rows`, same length and order; empty if pdfplumber did not supply it.
    row_tops: list[float] = field(default_factory=list)


@dataclass
class ExtractedDocument:
    file_name: str
    page_count: int
    pages: list[PageContent]
    tables: list[TableContent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def readable_pages(self) -> list[PageContent]:
        return [p for p in self.pages if p.readable]

    @property
    def unreadable_pages(self) -> list[int]:
        return [p.number for p in self.pages if not p.readable]

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages if p.readable)


@dataclass
class Issue:
    field: str
    description: str
    severity: Severity
    value: str = ""
    page: int | None = None
    suggested_action: str = ""
    stage: str = "extraction"  # "extraction", "validation" or "batch"

    @property
    def review_required(self) -> bool:
        return self.severity != Severity.INFO


@dataclass
class LineItem:
    """One service line of an invoice, with whatever the invoice states per line.

    A field left as None means the invoice does not state it for this line; it is never
    filled by splitting an invoice-level amount across lines.
    """

    sr_no: str | None = None
    description: str | None = None
    hsn_sac: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    count: Decimal | None = None       # "Count of Transaction"
    rate: Decimal | None = None        # "Rate Per Unit"
    taxable_value: Decimal | None = None
    igst: Decimal | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    line_total: Decimal | None = None  # this line's "Total Amount Rs."
    page: int | None = None
    edited_fields: set[str] = field(default_factory=set)


@dataclass
class InvoiceRecord:
    source_file: str
    page_count: int = 0
    bill_to: str | None = None
    ship_to: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    description: str | None = None
    taxable_value: Decimal | None = None
    invoice_no: str | None = None
    igst: Decimal | None = None
    total: Decimal | None = None
    invoice_date: date | None = None
    supplier_name: str | None = None
    supplier_gstin: str | None = None
    customer_gstin: str | None = None
    currency: str | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    other_tax: Decimal | None = None
    # Used only for the total reconciliation check; not exported as columns.
    round_off: Decimal | None = None
    discount: Decimal | None = None
    other_charges: Decimal | None = None
    po_numbers: list[str] = field(default_factory=list)
    field_pages: dict[str, int] = field(default_factory=dict)
    unreadable_pages: list[int] = field(default_factory=list)
    extraction_status: str = ""
    validation_status: str = ""
    issues: list[Issue] = field(default_factory=list)
    edited_fields: set[str] = field(default_factory=set)
    # Always holds at least one line; an invoice with no identifiable line-item table gets a
    # single line carrying the invoice-level description and amounts (see ensure_line_items).
    line_items: list[LineItem] = field(default_factory=list)

    @property
    def review_required(self) -> bool:
        return any(i.review_required for i in self.issues)

    @property
    def source_pages(self) -> str:
        pages = sorted(set(self.field_pages.values()))
        return ", ".join(str(p) for p in pages)

    @property
    def notes(self) -> str:
        parts = [f"{i.field}: {i.description}" for i in self.issues]
        edited = set(self.edited_fields)
        for index, line in enumerate(self.line_items, start=1):
            edited |= {f"line {index} {col}" for col in line.edited_fields}
        if edited:
            parts.append("Manually edited: " + ", ".join(sorted(edited)))
        return "; ".join(parts)

    def ensure_line_items(self) -> None:
        """Guarantee at least one line so every invoice produces a row."""
        if not self.line_items:
            self.line_items = [LineItem(
                description=self.description, period_from=self.period_from, period_to=self.period_to,
                taxable_value=self.taxable_value, igst=self.igst, cgst=self.cgst, sgst=self.sgst,
                page=self.field_pages.get("Description of Services"),
            )]

    @property
    def single_line(self) -> bool:
        return len(self.line_items) == 1

    def to_rows(self) -> list[dict[str, object]]:
        """One row per service line; invoice-level fields repeat on every row."""
        self.ensure_line_items()
        invoice = {col: getattr(self, attr) for col, attr in INVOICE_COLUMN_TO_ATTR.items()}
        invoice.update({
            "Source File Name": self.source_file,
            "Source Page Number": self.source_pages,
            "Extraction Status": self.extraction_status,
            "Validation Status": self.validation_status,
            "Review Required": "Yes" if self.review_required else "No",
            "Extraction Notes": self.notes,
        })
        rows = []
        for line in self.line_items:
            row = dict(invoice)
            row.update({col: getattr(line, attr) for col, attr in LINE_COLUMN_TO_ATTR.items()})
            # A line that states no period of its own inherits the invoice's period.
            row["Transaction Date From"] = line.period_from or self.period_from
            row["Transaction Date To"] = line.period_to or self.period_to
            if line.page:
                row["Source Page Number"] = str(line.page)
            rows.append({col: row.get(col) for col in ALL_COLUMNS})
        return rows


@dataclass
class FileResult:
    file_id: int
    file_name: str
    size_bytes: int = 0
    sha256: str | None = None
    page_count: int | None = None
    status: FileStatus = FileStatus.QUEUED
    messages: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    record: InvoiceRecord | None = None
    stored_path: Path | None = None


@dataclass
class Batch:
    batch_id: str
    created_at: datetime
    files: list[FileResult] = field(default_factory=list)
    complete: bool = False

    @property
    def records(self) -> list[InvoiceRecord]:
        return [f.record for f in self.files if f.record is not None]
