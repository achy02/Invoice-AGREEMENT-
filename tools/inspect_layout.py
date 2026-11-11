"""Show how the engine reads one PDF's line-item table, for diagnosing a new vendor layout.

    .venv\\Scripts\\python tools\\inspect_layout.py "C:\\path\\to\\invoice.pdf"

Prints the tables pdfplumber found, the header rows it merged, what each header was
classified as, the column mapping it settled on, and the service lines that came out.
When a column is mapped to the wrong index, this is where it shows.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.invoice_line_items import (  # noqa: E402
    KIND_PATTERNS,
    _header_segments,
    _merge_header_rows,
    classify,
    extract_line_items,
    find_header,
)
from app.invoice_field_extractor import InvoiceFieldExtractor  # noqa: E402
from app.invoice_validator import validate_record  # noqa: E402
from app.pdf_text_extractor import extract_document  # noqa: E402

KINDS = [kind for kind, _ in KIND_PATTERNS]


def show_headers(headers: list[str]) -> None:
    print("      index | classified as | header text")
    for index, header in enumerate(headers):
        kind = classify(header)
        flag = "  <-- UNRECOGNISED" if kind is None and header.strip() else ""
        print(f"      {index:>5} | {str(kind):<13} | {header!r}{flag}")


def main(path: Path) -> int:
    doc = extract_document(path)
    print(f"file      : {doc.file_name}")
    print(f"pages     : {doc.page_count} ({len(doc.readable_pages)} with text, "
          f"unreadable: {doc.unreadable_pages or 'none'})")
    for note in doc.notes:
        print(f"note      : {note}")

    print(f"\n=== ruled tables found by pdfplumber: {len(doc.tables)} ===")
    matched = False
    for number, table in enumerate(doc.tables):
        print(f"\n-- table {number}: page {table.page}, {len(table.rows)} rows, "
              f"bbox top={table.bbox[1]:.1f}")
        for row in table.rows[:12]:
            print(f"      {row}")
        if len(table.rows) > 12:
            print(f"      ... {len(table.rows) - 12} more rows")
        found = find_header(table.rows)
        if not found:
            print("   header    : NOT a line-item table (no description column found)")
            continue
        matched = True
        mapping, header_rows, start = found
        print(f"   header    : {header_rows} header row(s) merged, starting at row {start}")
        show_headers(_merge_header_rows(table.rows, header_rows, start))
        print("   mapping   :")
        for kind in KINDS:
            if kind in mapping:
                print(f"      {kind:<12} -> column {mapping[kind]}")
        missing = [k for k in ("serial", "description", "hsn", "taxable") if k not in mapping]
        if missing:
            print(f"      not mapped: {', '.join(missing)}")

    if not matched:
        print("\n=== no ruled line-item table matched; the text-column path is used ===")
        for page in doc.readable_pages:
            for index in range(len(page.lines)):
                columns = _header_segments(page, index)
                if not columns:
                    continue
                print(f"-- page {page.number}, header on text line {index}")
                print("      kind         | x0..x1        | header text")
                for kind, seg in columns:
                    print(f"      {kind:<12} | {seg.x0:>6.1f}..{seg.x1:<6.1f} | {seg.text!r}")
                break

    print("\n=== service lines extracted ===")
    result = extract_line_items(doc, config.DATE_DAYFIRST)
    if not result.items:
        print("   NONE - the invoice will fall back to a single row with no description")
    for item in result.items:
        print(f"   SR {str(item.sr_no):<4} desc={str(item.description)!r:<40} hsn={item.hsn_sac!r}")
        print(f"            {item.period_from} -> {item.period_to}  count={item.count} rate={item.rate} "
              f"taxable={item.taxable_value}")
        print(f"            igst={item.igst} cgst={item.cgst} sgst={item.sgst} line_total={item.line_total}")
    print(f"   table total row: taxable={result.taxable_total} taxes={result.totals or '{}'}")

    record = validate_record(InvoiceFieldExtractor().extract(doc))
    print("\n=== invoice-level values ===")
    for label, value in (("Tax Invoice No", record.invoice_no), ("Invoice Date", record.invoice_date),
                         ("Bill To", record.bill_to), ("Ship To", record.ship_to),
                         ("Taxable Value", record.taxable_value), ("IGST", record.igst),
                         ("CGST", record.cgst), ("SGST", record.sgst), ("Total", record.total)):
        printable = repr(value)[:90] if isinstance(value, str) else value
        print(f"   {label:<16} {printable}")
    print(f"   status           {record.extraction_status} / {record.validation_status}")
    print("\n=== issues ===")
    for issue in record.issues:
        print(f"   [{issue.severity.value:<7}] {issue.field}: {issue.description}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))
