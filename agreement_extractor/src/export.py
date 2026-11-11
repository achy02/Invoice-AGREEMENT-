"""S7 -- the agreement workbook. The workbook is the deliverable; nothing else is.

Sheets: Read Me, Summary (one row per agreement), Commercials (one row per charge line),
Review Queue and Run Log.

**Coordinates are deliberately absent.** S5 still checks every value against the OCR text
and still refuses to report one whose quote cannot be found -- that check is what keeps an
invented rate out of the Summary sheet, and it is not optional. What is gone is the *output*
of its arithmetic: the pixel boxes, the Source Map sheet that listed them, and the annotated
page images. Those existed to be eyeballed, and nobody is eyeballing them. A value that
failed verification still says so, in the Review Queue, in words.

This writes **its own workbook**, separate from the invoice extractor's. The two pipelines
produce two files and share no code; see this folder's README.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import basis
from .config import Config
from .validate import DocumentResult

log = logging.getLogger(__name__)

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF")
FLAG_FILL = PatternFill("solid", fgColor="FFF2CC")
ERROR_FILL = PatternFill("solid", fgColor="F8CBAD")

SUMMARY_COLUMNS = [
    "Client Name", "Source File", "Offer Letter Date", "Stamp Date", "Effective Date",
    "Signed / Unsigned", "TBX Signatory", "Client Signatory",
    "Client Signature Date/Time", "TBX Signature Date/Time", "Signature Mode",
    "Service / Product Offered", "Billing Model", "Payment Terms", "Termination Notice",
    "Other Key Terms", "Data Gaps / Flags", "Review Required",
]

COMMERCIALS_COLUMNS = [
    "#", "Client Name", "Charge Category", "Product / Service", "Sub-product / Charge Item",
    # Two columns, deliberately. "Rate Basis" is one of nine fixed values and is what you
    # filter, group and pivot on; "Frequency / Basis" keeps the document's own wording so a
    # reviewer can see what that classification was made from. See src/basis.py.
    "Rate Basis", "Frequency / Basis", "How the Basis Was Decided",
    # Both prices, never one. These letters print a list price and the price this client
    # actually gets, and reporting only one of them misstates the commercial terms.
    "Rate for Client (INR)", "Standard Rate (INR)", "Discount %",
    "Rate Type", "Rate as Written in Letter", "Standard Rate as Written",
    "Free Limit", "Charging Condition / Notes",
    # The page number stays. It is the one locator a person can actually use -- they open
    # the PDF at that page -- unlike a pixel box, which needs a tool to mean anything.
    "Source Record ID", "Source Page", "Source Section", "Verified", "Quoted Text",
]

#: Field name in the extraction -> Summary column. This is the single place the two
#: vocabularies meet: the model answers in schema keys, the golden workbook and this
#: workbook speak human labels, and `scripts/eval.py` translates through this same map so a
#: renamed field cannot quietly read as a wrong answer.
SUMMARY_FIELDS = {
    "client_name": "Client Name", "offer_letter_date": "Offer Letter Date",
    "stamp_date": "Stamp Date", "effective_date": "Effective Date",
    "tbx_signatory": "TBX Signatory", "client_signatory": "Client Signatory",
    "client_signature_datetime": "Client Signature Date/Time",
    "tbx_signature_datetime": "TBX Signature Date/Time",
    "signature_mode": "Signature Mode",
    "services_offered": "Service / Product Offered", "billing_model": "Billing Model",
    "payment_terms": "Payment Terms", "termination_notice": "Termination Notice",
    "other_key_terms": "Other Key Terms",
}


def _write_header(sheet, columns: list[str]) -> None:
    sheet.append(columns)
    for index in range(1, len(columns) + 1):
        cell = sheet.cell(row=1, column=index)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.freeze_panes = "A2"


def _autosize(sheet, maximum: int = 52) -> None:
    for column in sheet.columns:
        width = max((len(str(c.value)) for c in column if c.value is not None), default=8)
        sheet.column_dimensions[get_column_letter(column[0].column)].width = \
            min(max(width + 2, 10), maximum)


def _discount(charge: dict):
    """How far below list this client's rate sits, where both numbers are known.

    Computed here rather than asked of the model: it is arithmetic, and arithmetic the model
    gets to do is arithmetic nobody can check. Blank when either rate is missing, which is
    most rows -- a blank is honest, a zero would read as "no discount given".
    """
    from .validate import parse_number

    offered, standard = parse_number(charge.get("rate_inr")), parse_number(
        charge.get("standard_rate_inr"))
    if offered is None or standard is None or standard == 0:
        return ""
    return round((standard - offered) / standard * 100, 1)


def build_workbook(results: list[DocumentResult], config: Config,
                   run_log=None) -> Workbook:
    book = Workbook()

    # -- Read Me ---------------------------------------------------------------------------
    sheet = book.active
    sheet.title = "Read Me"
    for row in [
        ["Agreement extraction — generated by agreement_extractor"],
        [f"Generated {datetime.now():%d-%b-%Y %H:%M}"],
        [],
        ["This workbook is produced by the agreement pipeline, which is separate from the "
         "invoice extractor. Invoice data is in its own workbook under outputs/."],
        [],
        ["Sheet", "What it holds"],
        ["Summary", "One row per agreement."],
        ["Commercials", "One row per rate or charge line, including charges stated in prose."],
        ["", "'Rate Basis' is one of nine fixed values — One-time, Per transaction, "
             "Per man-day, Monthly, Quarterly, Annual, Volume / slab based, As incurred, "
             "Not stated — so the sheet can be filtered and pivoted. "
             "'Frequency / Basis' beside it keeps the document's own wording, and "
             "'How the Basis Was Decided' says whether the page stated it or the model "
             "inferred it. A yellow Rate Basis cell means the document does not say."],
        ["Review Queue", "Everything a person needs to look at, and why."],
        ["Run Log", "What ran, how long it took, and what failed."],
        [],
        ["How to check a value"],
        ["Each charge line carries the page it came from and the exact words quoted from "
         "that page. Open the PDF at that page and the quote should be on it verbatim. "
         "That is the whole audit trail, and it needs no tool to follow."],
        [],
        ["Values the pipeline could not ground"],
        ["The model returns a verbatim quote with every value. If that quote cannot be found "
         "on the page it cites, the value is marked UNGROUNDED and sent to the Review Queue "
         "instead of being reported as fact. Blank is a real answer: Stamp Date and "
         "Effective Date are genuinely absent from most of these documents."],
    ]:
        sheet.append(row)
    sheet["A1"].font = Font(bold=True, size=14)
    sheet["A6"].font = sheet["A13"].font = sheet["A17"].font = Font(bold=True)
    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 96

    # -- Summary ---------------------------------------------------------------------------
    sheet = book.create_sheet("Summary")
    _write_header(sheet, SUMMARY_COLUMNS)
    for result in results:
        by_field = {g.field: g for g in result.fields}
        row = {column: "" for column in SUMMARY_COLUMNS}
        row["Source File"] = result.file_name
        for name, column in SUMMARY_FIELDS.items():
            item = by_field.get(name)
            row[column] = "" if item is None or item.status != "OK" else (item.value or "")
        row["Client Name"] = row["Client Name"] or result.client
        row["Signed / Unsigned"] = result.signed_status
        row["Data Gaps / Flags"] = "; ".join(
            f"{f.severity}: {f.message}" for f in result.findings) or ""
        row["Review Required"] = "YES" if result.review_required else "no"
        sheet.append([row[column] for column in SUMMARY_COLUMNS])
        if result.review_required:
            sheet.cell(row=sheet.max_row, column=len(SUMMARY_COLUMNS)).fill = FLAG_FILL

    # -- Commercials -------------------------------------------------------------------------
    sheet = book.create_sheet("Commercials")
    _write_header(sheet, COMMERCIALS_COLUMNS)
    number = 0
    for result in results:
        for charge in result.charges:
            number += 1
            data = getattr(charge, "charge", {}) or {}
            resolved = data.get("resolved_basis") or "NOT_STATED"
            sheet.append([
                number, result.client or "", data.get("charge_category", ""),
                data.get("product", ""), data.get("sub_product", ""),
                basis.LABELS.get(resolved, resolved), data.get("frequency_basis", ""),
                data.get("basis_note", ""),
                data.get("rate_inr"),
                data.get("standard_rate_inr"), _discount(data),
                data.get("rate_type", ""),
                data.get("rate_as_written", ""), data.get("rate_as_written_standard", ""),
                data.get("free_limit", ""),
                data.get("condition_notes", ""), getattr(charge, "record_id", ""),
                charge.page or "", charge.section_heading or "",
                # Replaces the pixel box: the same fact in a form a person can act on.
                "yes" if charge.status == "OK" else charge.status.lower(),
                charge.evidence_text or "",
            ])
            if charge.status != "OK":
                sheet.cell(
                    row=sheet.max_row,
                    column=COMMERCIALS_COLUMNS.index("Rate for Client (INR)") + 1
                ).fill = ERROR_FILL
            if resolved == "NOT_STATED":
                sheet.cell(row=sheet.max_row,
                           column=COMMERCIALS_COLUMNS.index("Rate Basis") + 1).fill = FLAG_FILL

    # -- Review Queue --------------------------------------------------------------------------
    sheet = book.create_sheet("Review Queue")
    _write_header(sheet, ["Source File", "Severity", "Field", "What to check"])
    for result in results:
        for finding in sorted(result.findings,
                              key=lambda f: {"ERROR": 0, "WARNING": 1}.get(f.severity, 2)):
            sheet.append([result.file_name, finding.severity, finding.field, finding.message])
            if finding.severity == "ERROR":
                sheet.cell(row=sheet.max_row, column=2).fill = ERROR_FILL

    # -- Run Log -------------------------------------------------------------------------------
    sheet = book.create_sheet("Run Log")
    _write_header(sheet, ["Stage", "File", "Status", "Seconds", "Detail"])
    for record in (run_log.records if run_log else []):
        sheet.append([record.stage, record.file_name, record.status,
                      record.seconds, record.detail])

    for name in book.sheetnames:
        _autosize(book[name])
    return book


def write_workbook(results: list[DocumentResult], config: Config, run_log=None) -> Path:
    config.paths.output.mkdir(parents=True, exist_ok=True)
    path = config.paths.output / f"Agreement_Extraction_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    build_workbook(results, config, run_log).save(path)
    log.info("Wrote %s", path)
    return path
