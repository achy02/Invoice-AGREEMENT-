# Bulk PDF Invoice Extraction to Excel (no OCR)

Upload many PDF invoices at once. The app pulls the invoice fields out of the PDFs' text
layer, lets you review and correct them, and produces one consolidated Excel workbook.

It uses only Python PDF libraries (PyMuPDF and pdfplumber). It does **not** use OCR, image
recognition or any external AI service. If a page has no machine-readable text (a scanned
image), the app flags it as `SCANNED_OR_UNREADABLE_NO_OCR` and does not guess.

Sales Register mapping and reconciliation are not part of this phase.

## Requirements

- Python 3.11 or newer (developed and tested on Python 3.14, Windows 11)
- The packages in `requirements.txt`

## Installation

```powershell
cd C:\Users\Infosec\Documents\Invoice
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Optional: copy `.env.example` to `.env` to change limits, folders or the port.

## Running the application

```powershell
.venv\Scripts\python main.py
```

Then open http://127.0.0.1:8000 in a browser. Press Ctrl+C in the terminal to stop.

### Uploading multiple PDFs

1. **Upload PDF invoices.** Three ways, and you can combine them before processing:
   - Drag PDFs onto the drop area.
   - **Drag a whole folder** onto the drop area. It is scanned right through, including every
     sub-folder; files that are not PDFs are skipped and counted.
   - Click **Choose files** for individual PDFs, or **Choose folder** to pick a folder.

   The count of selected PDFs, how many folders they came from and how many non-PDFs were
   skipped are shown above the buttons. Click **Process files** when ready.

   A file inside a folder keeps its path in **Source File Name** (`2025/Q1/invoice.pdf`), so
   two invoices with the same file name in different sub-folders stay distinct. Identical files
   are still detected as duplicates by their SHA-256 hash, wherever they sit.
2. **Processing status.** Each file shows its page count, status and, under **Details**, the
   reason it is flagged: file-level messages first, then every WARNING and ERROR that makes the
   record need review. A file marked `PROCESSED_WITH_WARNINGS` always says why. Processing runs
   in the background and the table updates by itself.
3. **Extracted invoice data.** One row per invoice. The nine required columns come first.
4. **Review and correct.** Click a row to edit it. The form is split into **this service line**
   (its dates, description, HSN/SAC and amounts) and **the invoice** (fields shared by every
   line, such as Bill To and Total Amount). For a multi-service invoice, the numbered buttons
   switch between its service lines. Saving re-runs validation, and the issues are listed
   under the form. Edited fields are recorded in the Extraction Notes.
5. **Download Excel.** Click **Download Excel workbook**. A copy is also saved in `outputs/`.

### Command-line mode (no browser)

```powershell
.venv\Scripts\python main.py extract C:\path\to\invoices\folder another.pdf -o C:\path\to\output
```

A folder is scanned recursively, so one path covers an archive arranged in sub-folders. Files
that are not `.pdf` are ignored, and each invoice is named by its path inside the folder.

### Trying it with sample invoices

```powershell
.venv\Scripts\python -m tests.synthetic_invoices sample_invoices
```

This writes sixteen synthetic invoices (fictitious data) to `sample_invoices/`. They cover
different layouts, a multi-page invoice, a four-service invoice with per-line dates and HSN
codes, an invoice with per-line CGST/SGST/IGST columns and a totals row, one with Count of
Transaction and Rate Per Unit beside a per-line total, From/To period columns, conflicting
totals, a scanned PDF and a corrupt PDF.

## The Excel workbook

File name: `Invoice_Extraction_YYYYMMDD_HHMMSS.xlsx`

**Invoice Data** has **one row per service line**. An invoice listing four services produces
four rows: each carries its own SR No, transaction period, description, HSN/SAC and taxable
value, while the invoice-level fields (Bill To, Tax Invoice No, Total Amount, Invoice Taxable
Value) repeat on every row. An invoice with a single service produces a single row.

The columns are in this exact order:

| # | Column | Level | # | Column | Level |
|---|--------|-------|---|--------|-------|
| 1 | Bill To | invoice | 16 | Source Page Number | **line** |
| 2 | Ship To | invoice | 17 | Invoice Date | invoice |
| 3 | Transaction Date From | **line** | 18 | Supplier Name | invoice |
| 4 | Transaction Date To | **line** | 19 | Supplier GSTIN | invoice |
| 5 | Description of Services | **line** | 20 | Customer GSTIN | invoice |
| 6 | Taxable Value | **line** | 21 | Currency | invoice |
| 7 | Tax Invoice No | invoice | 22 | CGST Amount | **line** |
| 8 | IGST Amount | **line** | 23 | SGST Amount | **line** |
| 9 | Total Amount | invoice | 24 | Other Tax Amount | invoice |
| 10 | SR No | **line** | 25 | Invoice Taxable Value | invoice |
| 11 | HSN / SAC | **line** | 26 | Extraction Status | invoice |
| 12 | Count of Transaction | **line** | 27 | Validation Status | invoice |
| 13 | Rate Per Unit | **line** | 28 | Review Required | invoice |
| 14 | Line Total Amount | **line** | 29 | Extraction Notes | invoice |
| 15 | Source File Name | invoice | | | |

The nine required columns keep positions 1-9 exactly as specified. The line's own identity and
figures follow at 10-14, and `Invoice Taxable Value` carries the invoice subtotal so the
per-line values can be reconciled against it.

### Per-line totals versus the invoice total

Two different "total" columns, because an invoice prints two different things:

| Column | Level | What the invoice calls it |
|---|---|---|
| `Line Total Amount` | **line** | `Total Amount Rs.` on that service's own row: its taxable value plus the tax charged on it |
| `Total Amount` | invoice | the `Total` row under the table: the sum across every service charged |

`Line Total Amount` is only filled when the table has a taxable column of its own. Where the
only money column *is* the amount column, that column is the taxable value, as before.

### Count, rate and the taxable check

`Count of Transaction` and `Rate Per Unit` are read per line where the invoice states them
(a `-` leaves them blank). Where a line gives both, **count x rate** is checked against that
line's taxable value and a difference beyond `INVOICE_AMOUNT_TOLERANCE` is reported for review.
Nothing is recalculated or overwritten: the taxable value stays exactly as the invoice prints
it, and the check only tells you where the two disagree. `Line Total Amount` is checked the
same way against taxable + line taxes, and the sum of the line totals against `Total Amount`.

Amounts are real numbers (`#,##0.00`) and dates are real Excel dates. Invoice, SR and HSN
numbers are stored as text, so leading zeros are kept. The sheet is an Excel table with
filters and a frozen header row.

**Processing Summary** has the batch counts (uploaded, processed, with warnings, failed,
duplicates, records needing review), followed by one line per file with its SHA-256 hash.

**Extraction Exceptions** has one row per issue: missing fields, unreadable pages,
conflicting values, totals that don't reconcile, ambiguous periods, and failed, rejected or
duplicate files.

### Status values

| Column | Values |
|---|---|
| File status (UI and summary) | `PROCESSED`, `PROCESSED_WITH_WARNINGS`, `SCANNED_OR_UNREADABLE_NO_OCR`, `FAILED`, `DUPLICATE`, `REJECTED` |
| Extraction Status | `COMPLETE`; `PARTIAL` (a core field is missing); `PARTIAL_UNREADABLE_PAGES` |
| Validation Status | `PASSED`; `WARNINGS` (review recommended); `ERRORS` (e.g. no invoice number or total, period dates in the wrong order, invoice number equal to the PO number) |

Fully scanned PDFs, corrupt PDFs, non-PDF files and duplicate files get no row in Invoice
Data. They are reported in Processing Summary and Extraction Exceptions.

## How extraction works

```
app/
  pdf_text_extractor.py      PyMuPDF text + word coordinates -> lines split into column segments;
                             pdfplumber for ruled tables only; flags pages without text
  invoice_patterns.py        label vocabularies, column headers, amount/date/GSTIN/HSN regexes, vendor rules
  invoice_field_extractor.py label detection -> value on the same line / to the right / below;
                             address blocks by coordinates; invoice-level amounts and periods
  invoice_line_items.py      one LineItem per service: maps the line-item table's columns
                             (SR No, description, HSN/SAC, From/To dates, taxable, taxes)
  invoice_validator.py       missing fields, date order, PO confusion, total reconciliation,
                             per-line sums against the invoice subtotal, duplicates
  batch_processor.py         uploads, SHA-256 duplicate detection, per-file isolation, edits, export
  excel_generator.py         the three-sheet workbook
  main.py                    FastAPI app; static/index.html is the web page
tools/
  inspect_layout.py          diagnose one PDF: tables found, headers classified, columns mapped
```

Main rules:

- Every amount is tied to its own label (e.g. "Grand Total", "IGST @ 18%"). There is no
  global "pick any number" regex. Tax rates (`18%`) are never read as amounts.
- Labels have priorities. For example, "Grand Total" beats a plain "Total", and a stated
  taxable subtotal beats a sum of line items. When labels of equal strength give different
  values, the record is flagged for review.
- Missing values stay blank. They are never turned into zero. The exceptions are explicit
  statements of nil: a tax column whose cells hold `-`, `Nil` or `N/A` is read as `0`, and IGST
  is recorded as `0` (with a note) when the invoice charges CGST/SGST and has no IGST line at all.
  An empty cell is still blank, not zero.
- Numbers like `1,25,000.00` (Indian) and `125,000.00` (Western) are both read correctly.
- Numeric dates are read day-first (`04/05/2024` is 4 May 2024). Set `INVOICE_DATE_DAYFIRST=false` to change this.
- Total check: Taxable Value + IGST + CGST + SGST + other tax + round off should equal Total
  Amount within `INVOICE_AMOUNT_TOLERANCE` (default 1.00). If it doesn't, the check is
  retried with any freight/charges and discounts found. A mismatch is recorded as a warning;
  the invoice is not rejected.

### Service lines

- The line-item table's columns are identified from its headers, including headers stacked
  over two rows: "Transaction Date" spanning "From" and "To" becomes two date columns.
- The header is looked for on **every row** of a detected table, not just the first few. An
  invoice drawn inside one outer border comes back as a single table whose opening rows are the
  title and the address blocks, with the line-item header part-way down.
- An HSN/SAC code is kept out of the description. It is taken from an HSN/SAC column when
  there is one, and otherwise split out of the description text itself ("Risk Radar 998314"
  becomes `Risk Radar` + `998314`). A 4-digit number is only treated as a code when it is
  labelled, so a description ending in a year survives intact.
- A row carrying only description text is treated as a wrapped continuation of the line above,
  not as a new service.
- The invoice-level CGST, SGST and IGST come from the line-item table's own tax columns: the
  figure on its Total row, or, when that cell is left blank (common where a tax is nil), the sum
  of the column. Both beat reading a value from under a tax heading in the raw text, which can
  pick up the neighbouring column.
- A GSTIN that lands inside a line of address text, because a narrow column gap merged two
  columns, is kept as an identifier and removed from the address. It ranks behind the GSTIN on
  the block's own identifier line, since it probably belongs to the party in the other column.
- Totals and tax rows inside the table are not services. A subtotal row supplies the invoice
  taxable value; a tax row does not. The same applies to an "Amount Chargeable (in words)" row,
  whose label sits in the first column while the words land under the description: every cell of
  a row is checked for such a label, not only the description cell.
- pdfplumber sometimes inserts a space inside a number when reading a cell (`1 ,000,000`), which
  would otherwise parse as `000,000` = 0. A cell that is entirely one comma-grouped or decimal
  number is rejoined; dates and anything else are left exactly as read. When the table has its own CGST/SGST/IGST columns, that
  subtotal row also supplies the invoice-level tax amounts, so they are the column totals and
  not the first service line's figures.
- An address block stops at the top of the line-item table, so column headers printed directly
  under a Bill To box are never absorbed into the address.
- Per-line amounts are only ever read from the invoice. An invoice-level total is **never**
  split across lines: if the invoice states no per-line taxable value, those cells stay blank
  and the row is flagged. The sum of the per-line values is checked against the invoice
  subtotal, and a difference is reported for review.
- A line that states no period of its own inherits the invoice's period.

### Vendor-specific rules

If a vendor uses unusual labels, copy `vendor_rules.example.json` to `vendor_rules.json` and
add a rule. A rule applies when every condition in its `match` section holds: a GSTIN that
appears in the PDF, and/or a piece of text that appears in it. Its labels are tried before
the built-in ones.

Field keys: `invoice_no`, `invoice_date`, `period_range`, `period_from`, `period_to`, `bill_to`,
`ship_to`, `supplier`, `supplier_gstin`, `customer_gstin`, `taxable`, `igst`, `cgst`, `sgst`,
`other_tax`, `round_off`, `discount`, `other_charges`, `total`, `po_no`.

## Running the tests

```powershell
.venv\Scripts\python -m pytest
```

The tests build synthetic PDFs on the fly. They cover different layouts, multi-page
invoices, multiple PDFs in one batch, missing Ship To and periods, invoice-number formats,
Indian number formats, IGST and CGST/SGST invoices, missing taxable values, conflicting
totals, duplicate files, corrupt and scanned PDFs, review edits, and the Excel column order
and types. `tests/test_line_items.py` covers multi-service invoices: per-line dates and
amounts, HSN/SAC separation, wrapped descriptions, totals rows, and editing one line only.

Every test uses invoices generated by `tests/synthetic_invoices.py`, so they confirm the
engine handles the layouts it was built for; they are not evidence about invoices from a
vendor whose layout has not been seen yet.

## Known limitations of text-only extraction

- **No OCR.** Scanned or photographed invoices, and PDFs whose fonts have no text mapping
  (copying text from them gives garbage), cannot be read. They are flagged, not guessed.
- **One invoice per PDF.** If a PDF contains several invoices, you get a warning about
  "multiple different invoice numbers", and only the first is extracted.
- **Credit notes** are read like invoices. `Tax Invoice No` holds the credit note's own number
  (`Credit Note No.`), and the invoice it is raised against (`Against Tax Invoice No`) is kept
  as a reference, not as the document's number. Amounts are not negated: a credit note's values
  are reported exactly as printed, so treat the sign yourself when consuming the workbook.
- **Layout heuristics.** The engine handles common layouts: label and value on the same line,
  value to the right, or value under the label as in Tally-style boxes; ruled or unruled
  item tables; Bill To and Ship To side by side or stacked. Very unusual layouts may need a
  vendor rule or manual correction.
- **Descriptions and service lines** come from the line-item table, found by its description
  header (Description, Description of Goods / Services, Particulars, Item, Nature of Services
  and similar). A table with no such header is not read as line items, and the invoice falls
  back to a single row with no description.
- **Transaction period** is read from an inline label ("Service Period: 01/04/2024 to 30/04/2024",
  "Period From" / "Period To", "From : ... To : ..."), or from a **From / To column pair** in a
  table, usually under a heading such as "Transaction Date". Only values that parse as dates are
  used, so From/To columns holding places (freight invoices) are ignored. If line items carry
  different periods, the widest range is used and the row is flagged for review. A month alone
  ("for the month of March 2024") is expanded to the 1st to the last day, with a warning.
- **Batches are kept in memory.** Reviewed edits are lost if the server restarts, so download
  the workbook before stopping the server. The extracted records are held, not the PDFs: each
  uploaded file is written to `uploads/` as it arrives and read back one at a time, so the
  size of a batch is limited by processing time and disk rather than by memory. Measured on
  this machine, 1,008 invoices in 24 sub-folders uploaded in 0.5 s and took 74 s to process
  (about 74 ms each, 3,798 service-line rows), with the server's memory rising by 84 MB.
  Invoices are processed one after another on a single background thread.

## Troubleshooting

| Problem | What to do |
|---|---|
| `SCANNED_OR_UNREADABLE_NO_OCR` | Open the PDF and try to select text. If you can't, it is an image; enter the data manually, or wait for OCR in a later phase. |
| `FAILED: Cannot open PDF` / `no pages` | The file is corrupt or not really a PDF. Re-download or re-export it. |
| `FAILED: password-protected` | Remove the password (print to PDF) and upload again. |
| `REJECTED` | Only `.pdf` files up to `INVOICE_MAX_FILE_SIZE_MB` are accepted. |
| "Too many files: ... the limit is 5000 per batch" | Raise `INVOICE_MAX_FILES_PER_BATCH` in `.env` and restart, or upload the folder in parts. |
| A field is consistently wrong for one vendor | Run `python tools\inspect_layout.py <pdf>` to see which column the engine mapped each field to, then add a vendor rule (see above). |
| Dates have day and month swapped | Check `INVOICE_DATE_DAYFIRST`. |
| Port 8000 already in use | Set `INVOICE_PORT` in `.env`. |
| `ModuleNotFoundError` | Activate the virtual environment or run with `.venv\Scripts\python`. |

## Agreements — a separate pipeline

The second half of the project extracts **agreement** data so it can be mapped to invoice
data. It is deliberately a **separate pipeline in a separate folder**, with its own config,
its own virtual environment and its own Excel output. Nothing in `app/` imports it.

    agreement_extractor/        everything agreement-related lives here

Run order is invoice first, agreements second, and the two produce two workbooks:

    .venv\Scripts\python main.py                              -> outputs\Invoice_*.xlsx
    cd agreement_extractor
    ..\.venv313\Scripts\python -m scripts.run_pipeline        -> output\Agreement_*.xlsx

See `agreement_extractor\README.md` for its environment, design and accuracy figures —
including how to remove it again in one step if you ever want the invoice pipeline alone.
## Data handling

Uploaded PDFs are stored under `uploads/` only while they are processed, and are then deleted
(`INVOICE_DELETE_UPLOADS_AFTER_PROCESSING`). Generated workbooks are kept in `outputs/`.
`.gitignore` excludes PDFs, workbooks, `uploads/`, `outputs/` and `sample_invoices/` contents,
so real invoices and reports are not committed to version control. The server listens on
127.0.0.1 (this computer only) by default.

