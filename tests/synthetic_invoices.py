"""Synthetic invoice PDFs for automated tests (no real invoice data).

Run `python -m tests.synthetic_invoices sample_invoices` to write the samples to a folder
for trying out the web interface.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

Item = tuple  # ("t", x, y, text[, size]) | ("rect", x0, y0, x1, y1) | ("image", x0, y0, x1, y1)


def make_pdf(path: Path, pages: list[list[Item]], fontsize: float = 9) -> Path:
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page(width=595, height=842)
        for item in items:
            kind = item[0]
            if kind == "t":
                size = item[4] if len(item) > 4 else fontsize
                page.insert_text((item[1], item[2]), item[3], fontsize=size, fontname="helv")
            elif kind == "rect":
                page.draw_rect(pymupdf.Rect(*item[1:5]), color=(0, 0, 0), width=0.6)
            elif kind == "image":
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200), False)
                pix.clear_with(180)
                page.insert_image(pymupdf.Rect(*item[1:5]), pixmap=pix)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def ruled_table(cols: list[float], top: float, rows: list[list[str]], row_h: float = 16) -> list[Item]:
    """A fully ruled grid (detectable by pdfplumber) with text in each cell."""
    items: list[Item] = []
    for r, row in enumerate(rows):
        y0 = top + r * row_h
        for c, text in enumerate(row):
            items.append(("rect", cols[c], y0, cols[c + 1], y0 + row_h))
            if text:
                items.append(("t", cols[c] + 3, y0 + row_h - 5, text))
    return items


# ----------------------------------------------------------------------------- layouts


def igst_ruled(path: Path, *, grand_total: str = "1,18,000.00", extra_total: str | None = None) -> Path:
    """Layout A: inter-state IGST invoice, side-by-side Bill To / Ship To, ruled line-item table."""
    items: list[Item] = [
        ("t", 40, 50, "Acme Cloud Services Pvt Ltd", 14),
        ("t", 420, 50, "TAX INVOICE", 13),
        ("t", 40, 66, "12 MG Road, Bengaluru, Karnataka 560001"),
        ("t", 40, 78, "GSTIN: 29AAACA1234A1Z5"),
        ("t", 40, 100, "Service Period: 01/04/2024 to 30/04/2024"),
        ("t", 360, 100, "Tax Invoice No: INV/24-25/0007"),
        ("t", 360, 114, "Invoice Date: 15/05/2024"),
        ("t", 360, 128, "PO No: 4500012345"),
        ("t", 40, 150, "Bill To:"),
        ("t", 300, 150, "Ship To:"),
        ("t", 40, 164, "Globex India Ltd"),
        ("t", 300, 164, "Globex India Ltd - Pune Plant"),
        ("t", 40, 176, "45 Park Street, Kolkata 700016"),
        ("t", 300, 176, "Plot 7, Hinjewadi, Pune 411057"),
        ("t", 40, 188, "GSTIN: 19AABCG5678B1Z2"),
    ]
    items += ruled_table(
        [40, 70, 330, 400, 450, 555], 220,
        [
            ["S.No", "Description of Services", "SAC", "Qty", "Taxable Value"],
            ["1", "Cloud hosting services - April 2024", "998315", "1", "60,000.00"],
            ["2", "Managed backup and monitoring", "998316", "1", "40,000.00"],
            ["", "Total", "", "", "1,00,000.00"],
        ],
    )
    items += [
        ("t", 360, 310, "Taxable Value"), ("t", 480, 310, "1,00,000.00"),
        ("t", 360, 324, "IGST @ 18%"), ("t", 480, 324, "18,000.00"),
        ("t", 360, 338, "Grand Total"), ("t", 480, 338, grand_total),
    ]
    if extra_total:
        items += [("t", 360, 352, "Invoice Total"), ("t", 480, 352, extra_total)]
    items += [
        ("t", 40, 400, "Amount in words: Rupees One Lakh Eighteen Thousand Only"),
        ("t", 40, 420, "Bank Details: HDFC Bank, A/c No 1234567890, IFSC HDFC0000123"),
        ("t", 40, 440, "Terms & Conditions: Payment due within 30 days."),
    ]
    return make_pdf(path, [items])


def cgst_sgst_text(path: Path) -> Path:
    """Layout B: intra-state CGST+SGST invoice, Tally-style boxed labels (value below the label),
    unruled line items with a wrapped description, no Ship To and no service period."""
    items: list[Item] = [
        ("t", 40, 50, "Sharma Consulting LLP", 14),
        ("t", 250, 50, "TAX INVOICE", 12),
        ("t", 40, 66, "Andheri East, Mumbai 400069"),
        ("t", 40, 78, "GSTIN/UIN: 27AAFFS1234K1Z9"),
        ("t", 40, 100, "Buyer (Bill to)"),
        ("t", 360, 100, "Invoice No."), ("t", 470, 100, "Dated"),
        ("t", 40, 112, "Initech Solutions Pvt Ltd"),
        ("t", 360, 112, "SC-000123"), ("t", 470, 112, "3-Jun-2024"),
        ("t", 40, 124, "Lower Parel, Mumbai 400013"),
        ("t", 40, 136, "State Name: Maharashtra, Code: 27"),
        ("t", 40, 180, "Sl No"), ("t", 80, 180, "Particulars"), ("t", 330, 180, "HSN/SAC"),
        ("t", 420, 180, "Rate"), ("t", 490, 180, "Amount"),
        ("t", 40, 196, "1"), ("t", 80, 196, "Statutory audit fees for FY 2023-24"), ("t", 330, 196, "998221"),
        ("t", 490, 196, "75,000.00"),
        ("t", 80, 208, "including limited review of quarterly results"),
        ("t", 40, 222, "2"), ("t", 80, 222, "Tax advisory retainer"), ("t", 330, 222, "998231"),
        ("t", 490, 222, "50,000.00"),
        ("t", 330, 246, "Sub Total"), ("t", 490, 246, "1,25,000.00"),
        ("t", 330, 260, "CGST @ 9%"), ("t", 490, 260, "11,250.00"),
        ("t", 330, 274, "SGST @ 9%"), ("t", 490, 274, "11,250.00"),
        ("t", 330, 288, "Total Amount Payable"), ("t", 490, 288, "Rs. 1,47,500.00"),
        ("t", 40, 330, "Declaration: We declare that this invoice shows the actual price of the services."),
    ]
    return make_pdf(path, [items])


def multipage(path: Path) -> Path:
    """Layout C: line-item table continues on page 2 without a repeated header; totals on page 2."""
    header_rows = [["#", "Item Description", "Qty", "Rate", "Taxable Amount"]]
    page1_rows = [
        ["1", "Network support - Site A", "1", "50,000.00", "50,000.00"],
        ["2", "Network support - Site B", "1", "50,000.00", "50,000.00"],
        ["3", "Firewall management", "1", "50,000.00", "50,000.00"],
    ]
    page2_rows = [
        ["4", "Endpoint security licences", "1", "50,000.00", "50,000.00"],
        ["5", "Quarterly vulnerability assessment", "1", "50,000.00", "50,000.00"],
    ]
    cols = [40, 65, 330, 380, 460, 555]
    page1: list[Item] = [
        ("t", 40, 50, "Northwind IT Services Ltd", 14),
        ("t", 40, 66, "Sector 62, Noida, Uttar Pradesh 201301"),
        ("t", 40, 78, "GSTIN: 09AAACN4321M1Z3"),
        ("t", 360, 100, "Invoice Number: 0004567"),
        ("t", 360, 114, "Invoice Date: 2024-07-01"),
        ("t", 40, 100, "Billing Period: 01 Jun 2024 - 30 Jun 2024"),
        ("t", 40, 130, "Billed To:"),
        ("t", 40, 144, "Contoso Retail India Pvt Ltd"),
        ("t", 40, 156, "Whitefield, Bengaluru 560066"),
        ("t", 40, 168, "GSTIN: 29AAECC1111D1Z7"),
        ("t", 300, 130, "Shipped To:"),
        ("t", 300, 144, "Contoso Retail - Chennai Warehouse"),
        ("t", 300, 156, "Guindy, Chennai 600032"),
    ]
    page1 += ruled_table(cols, 200, header_rows + page1_rows)
    page1 += [("t", 40, 800, "Continued on next page")]
    page2: list[Item] = [("t", 40, 40, "Invoice Number: 0004567 (page 2)")]
    page2 += ruled_table(cols, 60, page2_rows)
    page2 += [
        ("t", 330, 120, "Taxable Amount"), ("t", 470, 120, "2,50,000.00"),
        ("t", 330, 134, "Integrated GST @ 18%"), ("t", 470, 134, "45,000.00"),
        ("t", 330, 148, "Invoice Total"), ("t", 470, 148, "2,95,000.00"),
    ]
    return make_pdf(path, [page1, page2])


def missing_taxable(path: Path) -> Path:
    """Layout D: no taxable value label or taxable column; only line amounts, tax and a generic total."""
    items: list[Item] = [
        ("t", 40, 50, "Bright Events Co", 14),
        ("t", 40, 80, "Invoice # BE/1043"),
        ("t", 300, 80, "Date: 12.08.2024"),
        ("t", 40, 110, "Customer:"),
        ("t", 40, 124, "Wayne Enterprises India"),
        ("t", 40, 136, "Juhu, Mumbai 400049"),
        ("t", 40, 170, "Description"), ("t", 400, 170, "Amount"),
        ("t", 40, 186, "Event management for product launch"), ("t", 400, 186, "10,000.00"),
        ("t", 300, 220, "IGST 18%"), ("t", 400, 220, "1,800.00"),
        ("t", 300, 234, "Total"), ("t", 400, 234, "11,800.00"),
    ]
    return make_pdf(path, [items])


def no_line_amounts(path: Path) -> Path:
    """Services listed with no per-line amount and no taxable subtotal anywhere:
    the taxable value genuinely cannot be read and must stay blank."""
    items: list[Item] = [
        ("t", 40, 50, "Quiet Ledger Services", 14),
        ("t", 40, 80, "Invoice No: QL-77"),
        ("t", 300, 80, "Invoice Date: 20-09-2025"),
        ("t", 40, 110, "Bill To:"),
        ("t", 40, 124, "Globex India Ltd"),
        ("t", 40, 160, "Description of Services"),
        ("t", 40, 176, "Annual maintenance contract FY 2025-26"),
        ("t", 300, 220, "IGST"), ("t", 420, 220, "9,000.00"),
        ("t", 300, 234, "Total Amount Payable"), ("t", 420, 234, "59,000.00"),
    ]
    return make_pdf(path, [items])


def po_before_invoice(path: Path) -> Path:
    """PO number printed before the invoice number on the same line."""
    items: list[Item] = [
        ("t", 40, 50, "Delta Supplies", 14),
        ("t", 40, 80, "PO Number: 7700123"),
        ("t", 250, 80, "Invoice No: DS-55/2024"),
        ("t", 420, 80, "Invoice Date: 05/09/2024"),
        ("t", 40, 110, "Bill To:"),
        ("t", 40, 124, "Stark Industries"),
        ("t", 40, 160, "Particulars"), ("t", 400, 160, "Amount"),
        ("t", 40, 176, "Office supplies"), ("t", 400, 176, "2,000.00"),
        ("t", 300, 200, "Taxable Value"), ("t", 400, 200, "2,000.00"),
        ("t", 300, 214, "IGST @ 12%"), ("t", 400, 214, "240.00"),
        ("t", 300, 228, "Grand Total"), ("t", 400, 228, "2,240.00"),
    ]
    return make_pdf(path, [items])


def transaction_date_columns(path: Path, *, same_period: bool = True) -> Path:
    """Layout E: the period lives in a line-item table under a 'Transaction Date' header
    that spans two sub-columns, 'From' and 'To' (no inline period label anywhere)."""
    x = [40, 90, 180, 270, 450, 555]  # SR.NO | From | To | Description | Taxable Value
    top, h = 200, 16
    items: list[Item] = [
        ("t", 40, 50, "Blue River Freight Pvt Ltd", 14),
        ("t", 40, 66, "Hinjewadi, Pune, Maharashtra 411057"),
        ("t", 40, 78, "GSTIN: 27AABCB7777L1Z4"),
        ("t", 360, 50, "TAX INVOICE", 13),
        ("t", 360, 90, "Tax Invoice No: BRF/2025/0412"),
        ("t", 360, 104, "Invoice Date: 05-09-2025"),
        ("t", 40, 130, "Bill To:"),
        ("t", 40, 144, "Globex India Ltd"),
        ("t", 40, 156, "45 Park Street, Kolkata 700016"),
        ("t", 40, 168, "GSTIN: 19AABCG5678B1Z2"),
        # Header row 1: SR.NO, Description and Taxable Value span both header rows.
        ("rect", x[0], top, x[1], top + 2 * h), ("t", x[0] + 4, top + h - 4, "SR.NO"),
        ("rect", x[1], top, x[3], top + h), ("t", x[1] + 40, top + h - 4, "Transaction Date"),
        ("rect", x[3], top, x[4], top + 2 * h), ("t", x[3] + 4, top + h - 4, "Description of Services"),
        ("rect", x[4], top, x[5], top + 2 * h), ("t", x[4] + 4, top + h - 4, "Taxable Value"),
        # Header row 2: the From / To sub-columns.
        ("rect", x[1], top + h, x[2], top + 2 * h), ("t", x[1] + 32, top + 2 * h - 4, "From"),
        ("rect", x[2], top + h, x[3], top + 2 * h), ("t", x[2] + 36, top + 2 * h - 4, "To"),
    ]
    rows = [
        ["1", "01-08-2025", "31-08-2025", "Freight forwarding - Mumbai to Delhi", "2,50,000.00"],
        ["2", "01-08-2025" if same_period else "15-08-2025", "31-08-2025", "Warehouse handling charges", "1,00,000.00"],
    ]
    for r, row in enumerate(rows):
        y0 = top + (2 + r) * h
        for c, text in enumerate(row):
            items.append(("rect", x[c], y0, x[c + 1], y0 + h))
            items.append(("t", x[c] + 4, y0 + h - 5, text))
    bottom = top + 4 * h + 20
    items += [
        ("t", 330, bottom, "Taxable Value"), ("t", 470, bottom, "3,50,000.00"),
        ("t", 330, bottom + 14, "IGST @ 18%"), ("t", 470, bottom + 14, "63,000.00"),
        ("t", 330, bottom + 28, "Grand Total"), ("t", 470, bottom + 28, "4,13,000.00"),
    ]
    return make_pdf(path, [items])


def multi_service_lines(path: Path, *, per_line_dates: bool = True, hsn_column: bool = True) -> Path:
    """Layout F (matches the shared sample): SR.NO | Transaction Date (From|To) |
    Description of Goods / Services | HSN/SAC | Taxable Value, with four services."""
    if hsn_column:
        x = [40, 85, 175, 265, 420, 480, 555]
    else:
        x = [40, 85, 175, 265, 480, 480, 555]  # HSN merged into the description column
    top, h = 190, 17
    items: list[Item] = [
        ("t", 40, 50, "Trusted Box Technologies Pvt Ltd", 13),
        ("t", 40, 65, "BKC, Mumbai, Maharashtra 400051"),
        ("t", 40, 77, "GSTIN: 27AAFCT1234N1ZB"),
        ("t", 380, 50, "TAX INVOICE", 12),
        ("t", 380, 80, "Tax Invoice No: TBX/25-26/0231"),
        ("t", 380, 94, "Invoice Date: 05-03-2026"),
        ("t", 40, 120, "Bill To:"),
        ("t", 40, 134, "Globex India Ltd"),
        ("t", 40, 146, "45 Park Street, Kolkata 700016"),
        ("t", 40, 158, "GSTIN: 19AABCG5678B1Z2"),
        ("rect", x[0], top, x[1], top + 2 * h), ("t", x[0] + 6, top + h - 2, "SR. NO"),
        ("rect", x[1], top, x[3], top + h), ("t", x[1] + 22, top + h - 4, "Transaction Date"),
        ("rect", x[1], top + h, x[2], top + 2 * h), ("t", x[1] + 30, top + 2 * h - 4, "From"),
        ("rect", x[2], top + h, x[3], top + 2 * h), ("t", x[2] + 36, top + 2 * h - 4, "To"),
        ("rect", x[3], top, x[4], top + 2 * h), ("t", x[3] + 4, top + h - 2, "Description of Goods / Services"),
    ]
    if hsn_column:
        items += [("rect", x[4], top, x[5], top + 2 * h), ("t", x[4] + 6, top + h - 2, "HSN/SAC")]
    items += [("rect", x[5], top, x[6], top + 2 * h), ("t", x[5] + 6, top + h - 2, "Taxable Value")]

    services = [
        ("Risk Radar", "998314", "4,50,000.00", "01-02-2026", "28-02-2026"),
        ("Risk Radar WB", "998314", "2,25,000.00", "01-02-2026", "28-02-2026"),
        ("RECON", "998313", "1,75,000.00", "01-02-2026", "28-02-2026"),
        ("Debt Syndication", "997159", "1,50,000.00",
         "01-02-2026" if per_line_dates else "01-01-2026", "28-02-2026"),
    ]
    for r, (name, hsn, amount, d1, d2) in enumerate(services):
        y0 = top + (2 + r) * h
        desc = name if hsn_column else f"{name} {hsn}"
        cells = [str(r + 1), d1, d2, desc] + ([hsn] if hsn_column else [""]) + [amount]
        for c, text in enumerate(cells):
            if not hsn_column and c == 4:
                continue
            items.append(("rect", x[c], y0, x[c + 1], y0 + h))
            items.append(("t", x[c] + 5, y0 + h - 5, text))
    bottom = top + (2 + len(services)) * h + 22
    items += [
        ("t", 350, bottom, "Taxable Value"), ("t", 480, bottom, "10,00,000.00"),
        ("t", 350, bottom + 14, "IGST @ 18%"), ("t", 480, bottom + 14, "1,80,000.00"),
        ("t", 350, bottom + 28, "Grand Total"), ("t", 480, bottom + 28, "11,80,000.00"),
        ("t", 40, bottom + 60, "Terms & Conditions: Payment due within 30 days."),
    ]
    return make_pdf(path, [items])


def per_line_tax_columns(path: Path, *, igst_dashed: bool = True, wrap_date: bool = True) -> Path:
    """Layout G: every tax has its own per-line column (CGST | SGST | IGST) and the table ends
    with a Total row carrying the invoice-level taxable value, taxes and grand total.

    The IGST column is dashed out because the supply is intra-state, placeholder '-' cells sit
    in the Count/Rate columns, and the recipient is printed in two 'Bill To' boxes side by side
    with no Ship To anywhere.

    The whole invoice sits inside one outer border, so pdfplumber returns the page as a single
    table whose first rows are the title and the addresses and whose line-item header is part
    of the way down. `wrap_date` puts the invoice date on two lines, as a narrow Date column does.
    """
    # SR | From | To | Description | HSN | Count | Rate | Taxable | CGST | SGST | IGST | Total
    x = [30, 50, 94, 138, 233, 265, 307, 347, 397, 441, 485, 517, 565]
    top, h, f = 195, 15, 6.5
    igst_cell = "-" if igst_dashed else ""
    items: list[Item] = [
        ("rect", 30, 30, 565, 44), ("t", 260, 40, "T A X   I N V O I C E", 8),
        ("rect", 30, 44, 185, 110),
        ("t", 33, 54, "TRANSBNK SOLUTIONS PRIVATE", 7), ("t", 33, 62, "LIMITED", 7),
        ("t", 33, 71, "103, Dheraj Kawal Pvt Ltd, Lal Bahadur", f),
        ("t", 33, 79, "Shastri Marg, Vikhroli West, Mumbai,", f),
        ("t", 33, 87, "Mumbai Suburban, Maharashtra - 400079", f),
        ("t", 33, 96, "GSTIN: 27AAJCT1769M1ZN", 7),
        ("rect", 185, 44, 400, 62), ("t", 188, 55, "Tax Invoice No: TSM/25-26/0006", 7),
        ("rect", 185, 62, 400, 110),
        ("t", 188, 72, "Place of Supply", 7), ("t", 188, 81, "Maharashtra 27", f),
        ("image", 410, 50, 490, 105),
        ("rect", 500, 44, 565, 110), ("t", 503, 55, "Date", 7),
    ]
    # The date sits on the same visual lines as the supplier address block to its left.
    items += ([("t", 503, 62, "21st April", f), ("t", 503, 71, "2025", f)] if wrap_date
              else [("t", 503, 62, "21st April 2025", f)])
    items += [
        ("rect", 30, 110, 185, 180), ("t", 33, 120, "Bill To", 7),
        ("t", 33, 130, "IMPERATIVE BUSINESS VENTURES", 7), ("t", 33, 138, "LIMITED", 7),
        ("t", 33, 147, "4th Floor, Plot No C-15, Bhairaav Milestone,", f),
        ("t", 33, 155, "Road Number 16, Nehru Nagar, THE TRIBE", f),
        ("t", 33, 163, "BAR, Wagle Industrial Estate, Thane, Thane,", f),
        ("t", 33, 171, "Maharashtra, 400604", f),
        ("t", 33, 179, "GST: 27AACCI7893N1ZQ", 7),
        ("rect", 185, 110, 565, 180), ("t", 188, 120, "Bill To", 7),
        ("t", 188, 130, "IMPERATIVE BUSINESS VENTURES LIMITED", 7),
        ("t", 188, 140, "4th Floor, Plot No C-15, Bhairaav Milestone, Road Number 16, Nehru Nagar, THE TRIBE", f),
        ("t", 188, 148, "BAR, Wagle Industrial Estate, Thane, Thane, Maharashtra, 400604", f),
        ("t", 188, 158, "GST: 27AACCI7893N1ZQ", 7),
    ]
    # Headers: every column spans both header rows except From / To under "Transaction Date".
    stacked = [(0, ["SR", "NO"]), (3, ["Description of Goods /", "Services"]), (4, ["HSN", "Code"]),
               (5, ["Count of", "Transaction"]), (6, ["Rate Per", "Unit"]), (7, ["Taxable", "value"]),
               (8, ["CGST", "(9%)"]), (9, ["SGST", "(9%)"]), (10, ["IGST", "(18%)"]),
               (11, ["Total", "Amount Rs."])]
    for col, texts in stacked:
        items.append(("rect", x[col], top, x[col + 1], top + 2 * h))
        items += [("t", x[col] + 3, top + 10 + n * 8, s, f) for n, s in enumerate(texts)]
    items += [
        ("rect", x[1], top, x[3], top + h), ("t", x[1] + 14, top + 10, "Transaction Date", f),
        ("rect", x[1], top + h, x[2], top + 2 * h), ("t", x[1] + 14, top + 2 * h - 4, "From", f),
        ("rect", x[2], top + h, x[3], top + 2 * h), ("t", x[2] + 18, top + 2 * h - 4, "To", f),
    ]
    rows = [
        ["1", "01-03-2025", "31-03-2025", "Implementation", "997119", "-", "-",
         "1,000,000.00", "90,000.00", "90,000.00", igst_cell, "1,180,000.00"],
        ["2", "01-03-2025", "31-03-2025", "Risk Radar", "997119", "-", "-",
         "300,000.00", "27,000.00", "27,000.00", igst_cell, "354,000.00"],
        ["3", "01-03-2025", "31-03-2025", "Debt Syndication", "997119", "-", "-",
         "1,600,002.00", "144,000.18", "144,000.18", igst_cell, "1,888,002.36"],
    ]
    for r, row in enumerate(rows):
        y0 = top + (2 + r) * h
        for c, text in enumerate(row):
            items.append(("rect", x[c], y0, x[c + 1], y0 + h))
            if text:
                items.append(("t", x[c] + 3, y0 + h - 5, text, f))
    # Total row: the label spans SR..HSN, the totals sit under their own columns.
    y0 = top + 5 * h
    items += [("rect", x[0], y0, x[5], y0 + h), ("t", x[3] + 20, y0 + h - 5, "Total", f)]
    for c, text in ((5, "-"), (6, ""), (7, "2,900,002.00"), (8, "261,000.18"),
                    (9, "261,000.18"), (10, igst_cell), (11, "3,422,002.36")):
        items.append(("rect", x[c], y0, x[c + 1], y0 + h))
        if text:
            items.append(("t", x[c] + 3, y0 + h - 5, text, f))
    y = top + 6 * h + 6
    items += [
        ("rect", 30, y, 150, y + 20),
        ("t", 33, y + 9, "Amount Chargeable (in", f), ("t", 33, y + 17, "words) :", f),
        ("rect", 150, y, 565, y + 20),
        ("t", 153, y + 12, "Thirty Four Lakh Twenty Two Thousand Two Rupees and Thirty Six Paisa Only", f),
        ("rect", 30, y + 20, 565, y + 56),
        ("t", 33, y + 30, "Remittance Details- Account Name: TRANSBNK SOLUTIONS PVT. LTD.", 7),
        ("t", 33, y + 40, "Bank Name: RBL BANK LIMITED", f),
        ("t", 33, y + 50, "Current Account No: 409001893152    IFSC Code: RATN0000088", f),
        # The outer border that makes the whole page one table.
        ("rect", 30, 30, 565, y + 56),
    ]
    return make_pdf(path, [items])


def counted_services(path: Path, *, count: str = "3", rate: str = "50,000.00",
                     taxable: str = "1,50,000.00", cgst: str = "13,500.00", sgst: str = "13,500.00",
                     total: str = "1,77,000.00", second_line: bool = False,
                     grand_total: str | None = None, blank_total_igst: bool = False,
                     merged_gstin: bool = False) -> Path:
    """Layout H: the full column set, including Count of Transaction, Rate Per Unit and a
    per-line "Total Amount Rs." beside the invoice-wide "Total" row.

    `blank_total_igst` leaves the Total row's IGST cell empty, as these invoices do when no
    IGST applies. `merged_gstin` puts a second party's GSTIN immediately after an address line,
    close enough that the two columns are read as one segment.
    """
    x = [30, 52, 96, 140, 232, 268, 310, 352, 404, 448, 492, 524, 565]
    top, h, f = 150, 15, 6.5
    items: list[Item] = [
        ("t", 33, 50, "TRANSBNK SOLUTIONS PRIVATE LIMITED", 9),
        ("t", 33, 62, "GSTIN: 27AAJCT1769M1ZN", 7),
        ("t", 380, 50, "Tax Invoice No: TSM/25-26/0501", 7),
        ("t", 380, 62, "Date: 21st April 2025", 7),
        ("t", 33, 95, "Bill To", 7),
        ("t", 33, 107, "IMPERATIVE BUSINESS VENTURES LIMITED", 7),
        ("t", 33, 118, "Thane, Maharashtra, 400604", f),
        ("t", 33, 129, "GST: 27AACCI7893N1ZQ", 7),
    ]
    if merged_gstin:
        # Sits a couple of points past the address line, so the two merge into one segment.
        items.append(("t", 143, 118, "GSTIN: 27ABOCS0349A1ZG", f))
    stacked = [(0, ["SR", "NO"]), (3, ["Description of Goods /", "Services"]), (4, ["HSN", "Code"]),
               (5, ["Count of", "Transaction"]), (6, ["Rate Per", "Unit"]), (7, ["Taxable", "value"]),
               (8, ["CGST", "(9%)"]), (9, ["SGST", "(9%)"]), (10, ["IGST", "(18%)"]),
               (11, ["Total", "Amount Rs."])]
    for col, texts in stacked:
        items.append(("rect", x[col], top, x[col + 1], top + 2 * h))
        items += [("t", x[col] + 3, top + 10 + n * 8, s, f) for n, s in enumerate(texts)]
    items += [
        ("rect", x[1], top, x[3], top + h), ("t", x[1] + 14, top + 10, "Transaction Date", f),
        ("rect", x[1], top + h, x[2], top + 2 * h), ("t", x[1] + 12, top + 2 * h - 4, "From", f),
        ("rect", x[2], top + h, x[3], top + 2 * h), ("t", x[2] + 16, top + 2 * h - 4, "To", f),
    ]
    rows = [["1", "01-03-2025", "31-03-2025", "Platform Fees", "997119", count, rate,
             taxable, cgst, sgst, "-", total]]
    if second_line:
        rows.append(["2", "01-04-2025", "30-04-2025", "Risk Radar", "997119", "2", "50,000.00",
                     "1,00,000.00", "9,000.00", "9,000.00", "-", "1,18,000.00"])
    for r, data in enumerate(rows):
        y0 = top + (2 + r) * h
        for c, text in enumerate(data):
            items.append(("rect", x[c], y0, x[c + 1], y0 + h))
            if text:
                items.append(("t", x[c] + 3, y0 + h - 5, text, f))
    sums = (("2,50,000.00", "22,500.00", "22,500.00", grand_total or "2,95,000.00") if second_line
            else (taxable, cgst, sgst, grand_total or total))
    y0 = top + (2 + len(rows)) * h
    items += [("rect", x[0], y0, x[5], y0 + h), ("t", x[3] + 20, y0 + h - 5, "Total", f)]
    total_igst = "" if blank_total_igst else "-"
    for c, text in ((5, "-"), (6, ""), (7, sums[0]), (8, sums[1]), (9, sums[2]),
                    (10, total_igst), (11, sums[3])):
        items.append(("rect", x[c], y0, x[c + 1], y0 + h))
        if text:
            items.append(("t", x[c] + 3, y0 + h - 5, text, f))
    y = y0 + h + 6
    items += [
        ("rect", 30, y, 150, y + 16), ("t", 33, y + 11, "Amount Chargeable (in words) :", f),
        ("rect", 150, y, 565, y + 16), ("t", 153, y + 11, "Rupees Only", f),
        ("rect", 30, 40, 565, y + 16),  # outer border, as the real invoices have
    ]
    return make_pdf(path, [items])


def period_colon_labels(path: Path) -> Path:
    """Bare 'From :' / 'To :' labels, with a 'To:' recipient block on the same invoice."""
    items: list[Item] = [
        ("t", 40, 50, "Cedar Analytics LLP", 14),
        ("t", 40, 66, "GSTIN: 29AADCC2222J1Z8"),
        ("t", 360, 50, "Invoice No: CA-2025-118"),
        ("t", 360, 64, "Invoice Date: 02-09-2025"),
        ("t", 40, 100, "To:"),
        ("t", 40, 114, "Initech Solutions Pvt Ltd"),
        ("t", 40, 126, "Lower Parel, Mumbai 400013"),
        ("t", 40, 160, "Transaction Date"),
        ("t", 40, 174, "From : 01-07-2025"),
        ("t", 200, 174, "To : 31-07-2025"),
        ("t", 40, 210, "Description"), ("t", 400, 210, "Amount"),
        ("t", 40, 226, "Data analytics retainer"), ("t", 400, 226, "80,000.00"),
        ("t", 300, 250, "Taxable Value"), ("t", 400, 250, "80,000.00"),
        ("t", 300, 264, "IGST @ 18%"), ("t", 400, 264, "14,400.00"),
        ("t", 300, 278, "Grand Total"), ("t", 400, 278, "94,400.00"),
    ]
    return make_pdf(path, [items])


def vendor_custom_labels(path: Path) -> Path:
    """Unusual labels that only a vendor rule can resolve."""
    items: list[Item] = [
        ("t", 40, 50, "Umbrella Logistics", 14),
        ("t", 40, 66, "GSTIN: 07AAACU9999Q1Z1"),
        ("t", 40, 90, "Bill Ref: UL-77-0001"),
        ("t", 40, 120, "Bill To:"),
        ("t", 40, 134, "Hooli India"),
        ("t", 40, 170, "Description"), ("t", 400, 170, "Amount"),
        ("t", 40, 186, "Freight forwarding"), ("t", 400, 186, "5,000.00"),
        ("t", 300, 210, "Taxable Value"), ("t", 400, 210, "5,000.00"),
        ("t", 300, 224, "IGST @ 18%"), ("t", 400, 224, "900.00"),
        ("t", 300, 238, "Net Receivable"), ("t", 400, 238, "5,900.00"),
    ]
    return make_pdf(path, [items])


def freight_from_to_places(path: Path) -> Path:
    """From / To columns that hold places, not dates: must not become a transaction period."""
    items: list[Item] = [
        ("t", 40, 50, "Rapid Roadways", 14),
        ("t", 40, 80, "Invoice No: RR-9001"),
        ("t", 300, 80, "Invoice Date: 10-09-2025"),
        ("t", 40, 110, "Bill To:"),
        ("t", 40, 124, "Globex India Ltd"),
    ]
    items += ruled_table(
        [40, 70, 160, 260, 400, 555], 170,
        [
            ["S.No", "From", "To", "Description of Services", "Amount"],
            ["1", "Mumbai", "Delhi", "Road transport of goods", "20,000.00"],
        ],
    )
    items += [
        ("t", 300, 230, "Taxable Value"), ("t", 440, 230, "20,000.00"),
        ("t", 300, 244, "IGST @ 5%"), ("t", 440, 244, "1,000.00"),
        ("t", 300, 258, "Grand Total"), ("t", 440, 258, "21,000.00"),
    ]
    return make_pdf(path, [items])


def scanned(path: Path, pages: int = 1) -> Path:
    """Image-only pages with no text layer (what a scanner produces without OCR)."""
    return make_pdf(path, [[("image", 40, 40, 555, 800)] for _ in range(pages)])


def mixed_text_and_scanned(path: Path) -> Path:
    tmp = igst_ruled(Path(path).with_suffix(".tmp.pdf"))
    doc = pymupdf.open(tmp)
    page = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 200), False)
    pix.clear_with(180)
    page.insert_image(pymupdf.Rect(40, 40, 555, 800), pixmap=pix)
    doc.save(path)
    doc.close()
    tmp.unlink()
    return Path(path)


def corrupt(path: Path) -> Path:
    Path(path).write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Catalog >> garbage without xref\n%%EOF")
    return Path(path)


ALL_SAMPLES = {
    "01_igst_ruled.pdf": igst_ruled,
    "02_cgst_sgst_boxed.pdf": cgst_sgst_text,
    "03_multipage.pdf": multipage,
    "04_missing_taxable.pdf": missing_taxable,
    "05_po_before_invoice.pdf": po_before_invoice,
    "10_transaction_date_columns.pdf": transaction_date_columns,
    "11_period_colon_labels.pdf": period_colon_labels,
    "12_multi_service_lines.pdf": multi_service_lines,
    "13_hsn_in_description.pdf": lambda p: multi_service_lines(p, hsn_column=False),
    "14_no_line_amounts.pdf": no_line_amounts,
    "15_per_line_tax_columns.pdf": per_line_tax_columns,
    "16_counted_services.pdf": lambda p: counted_services(p, second_line=True),
    "06_conflicting_totals.pdf": lambda p: igst_ruled(p, extra_total="1,20,000.00"),
    "07_scanned.pdf": scanned,
    "08_mixed_pages.pdf": mixed_text_and_scanned,
    "09_corrupt.pdf": corrupt,
}


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_invoices")
    out.mkdir(parents=True, exist_ok=True)
    for name, builder in ALL_SAMPLES.items():
        builder(out / name)
        print(out / name)
