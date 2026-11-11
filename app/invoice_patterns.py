"""Reusable regular expressions, label vocabularies and value parsers for invoice fields.

Labels are matched against the *start* of a text segment (a run of words on one visual
line, separated from its neighbours by a column-sized gap). Each field has an ordered list
of (priority, pattern) pairs; a lower priority number means a more specific, more trusted
label. Vendor-specific labels from vendor_rules.json are added with priority 0.
"""
from __future__ import annotations

import calendar
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dateutil import parser as date_parser

log = logging.getLogger(__name__)

I = re.IGNORECASE

# --------------------------------------------------------------------------------------
# Label construction helpers
# --------------------------------------------------------------------------------------

# A label must end at a word boundary-like position (punctuation, space, digit or end).
_LABEL_END = r"(?=$|[\s:.\-#(\[/,|]|\d)"
# Prevents address/party labels from matching identifier lines such as "Customer GSTIN".
_NOT_ID = (
    r"(?!\s*[:\-]?\s*(?:gstin|gst\b|pan\b|id\b|code\b|no\b|number|ref|p\.?\s?o\b|order|state\b"
    r"|contact|phone|mobile|email|e-mail))"
)
_PAREN = r"(?:\s*\([^)]*\))?"
_TAX_PREFIX = r"(?:(?:add|less|total)\s*[:\-]?\s*)?"
_NOT_RATE = r"(?!\s*(?:rate|%|\(%\)))"


def _rx(body: str) -> re.Pattern[str]:
    return re.compile(r"^\s*(?:" + body + r")" + _LABEL_END, I)


LabelList = list[tuple[float, re.Pattern[str]]]

FIELD_LABELS: dict[str, LabelList] = {
    "invoice_no": [
        # A credit/debit note's own number comes first: such a document also prints the
        # invoice it is raised against, which is a reference and not its own number.
        (1, _rx(r"(?:credit|debit)\s*note\s*(?:number|num|no|#)\.?")),
        (1, _rx(r"tax\s+invoice\s*(?:number|num|no|#)\.?(?:\s*(?:&|and)\s*date)?")),
        (1, _rx(r"invoice\s*(?:number|num|no|#|serial\s+no)\.?(?:\s*(?:&|and)\s*date)?")),
        (2, _rx(r"inv\.?\s*(?:number|no)\.?")),
        (3, _rx(r"(?:bill|document|doc)\s*(?:number|no|#)\.?")),
    ],
    # Other document references that must never be mistaken for the invoice number.
    "po_no": [
        (1, _rx(r"(?:customer\s+|buyer'?s?\s+)?(?:p\.?\s*o\.?|purchase\s+order|order)\s*(?:number|no|ref|#)\.?")),
        # "Against Tax Invoice No" on a credit note: the invoice being offset, not this document.
        (1, _rx(r"against\s+(?:tax\s+)?invoice\s*(?:number|no|#)\.?")),
        (2, _rx(r"(?:delivery\s+note|challan|d\.?c\.?|dispatch\s+doc(?:ument)?|reference|ref|e-?way\s+bill)\s*(?:number|no|#)\.?")),
    ],
    "invoice_date": [
        (1, _rx(r"(?:tax\s+)?invoice\s*(?:date|dt)\.?")),
        (1, _rx(r"date\s+of\s+(?:invoice|issue)")),
        (2, _rx(r"invoice\s*no\.?\s*(?:&|and)\s*date")),
        (2, _rx(r"(?:bill|inv\.?)\s*(?:date|dt)\.?")),
        (2, _rx(r"dated")),
        (3, _rx(r"date(?!\s+(?:of\s+supply|from|to)\b)")),
    ],
    "period_range": [
        (1, _rx(r"(?:service|billing|invoice|transaction|usage|statement|rental|subscription|contract|supply)\s+period" + _PAREN)),
        (1, _rx(r"period\s+of\s+(?:service|supply|billing|usage)")),
        (2, _rx(r"(?:for\s+the\s+)?period(?!\s*(?:from|to)\b)")),
        (2, _rx(r"billing\s+cycle|duration")),
        # A heading such as "Transaction Date" above a From/To column pair (see PERIOD_PARENT_RE).
        (2, _rx(r"(?:transaction|service|billing|usage|rental|subscription|contract|statement)\s+dates?" + _PAREN)),
    ],
    "period_from": [
        (1, _rx(r"(?:service\s+|billing\s+|transaction\s+)?period\s+from")),
        (1, _rx(r"from\s+date|date\s+from")),
        (1, _rx(r"service\s+(?:start\s+date|from)|billing\s+start\s+date")),
        (2, _rx(r"start\s+date")),
        # Bare "From :" — only trusted when a date follows on the same line (see BARE_PERIOD_LABEL_RE).
        (2.5, _rx(r"(?:w\.?e\.?f\.?|from)(?=\s*[:\-])")),
    ],
    "period_to": [
        (1, _rx(r"(?:service\s+|billing\s+|transaction\s+)?period\s+(?:to|till|upto|until)")),
        (1, _rx(r"to\s+date|date\s+to")),
        (1, _rx(r"service\s+(?:end\s+date|to|till|upto)|billing\s+end\s+date")),
        (2, _rx(r"end\s+date")),
        (2.5, _rx(r"(?:to|till|upto|until)(?=\s*[:\-])")),
    ],
    "bill_ship": [
        (1, _rx(r"bill(?:ed)?\s*(?:&|and|/)\s*ship(?:ped)?\s*to" + _PAREN)),
    ],
    "bill_to": [
        (1, _rx(r"bill(?:ed)?\s*-?\s*to(?:\s+party)?" + _PAREN + _NOT_ID)),
        (1, _rx(r"details\s+of\s+(?:receiver|recipient|buyer)" + _PAREN)),
        (1, _rx(r"(?:billing|invoice)\s+(?:address|details)" + _NOT_ID)),
        (1, _rx(r"(?:invoice|sold)\s+to" + _NOT_ID)),
        (2, _rx(r"(?:buyer|customer|client|recipient|receiver)(?:'s)?(?:\s+(?:details|name|address|information))?" + _PAREN + _NOT_ID)),
        # Bare "To:" as a recipient heading; rejected if a date follows instead of a name.
        (3, _rx(r"to(?=\s*:)" + _NOT_ID)),
    ],
    "ship_to": [
        (1, _rx(r"ship(?:ped)?\s*-?\s*to(?:\s+party)?" + _PAREN + _NOT_ID)),
        (1, _rx(r"details\s+of\s+consignee" + _PAREN)),
        (1, _rx(r"(?:shipping|delivery)\s+(?:address|details)" + _NOT_ID)),
        (1, _rx(r"deliver(?:ed)?\s+to" + _NOT_ID)),
        (2, _rx(r"consignee(?:'s)?(?:\s+(?:details|name|address))?" + _PAREN + _NOT_ID)),
        (2, _rx(r"(?:service|installation|site)\s+(?:location|address)")),
    ],
    "supplier": [
        (1, _rx(r"(?:supplier|seller|vendor|service\s+provider)(?:'s)?(?:\s+(?:details|name))?" + _PAREN + _NOT_ID)),
        (1, _rx(r"sold\s+by")),
        (2, _rx(r"from(?=\s*:)")),
    ],
    "supplier_gstin": [
        (1, _rx(r"(?:supplier|seller|vendor|our|company)(?:'s)?\s+gst(?:in)?(?:/uin)?(?:\s*(?:number|no))?\.?")),
    ],
    "customer_gstin": [
        (1, _rx(r"(?:customer|buyer|recipient|client|receiver|party)(?:'s)?\s+gst(?:in)?(?:/uin)?(?:\s*(?:number|no))?\.?")),
        (1, _rx(r"gstin\s+of\s+(?:the\s+)?(?:recipient|buyer|customer|receiver)")),
    ],
    "taxable": [
        (1, _rx(r"(?:total\s+)?taxable\s+(?:value|amount|amt|sub\s*-?\s*total|total)")),
        (1, _rx(r"(?:total\s+)?assessable\s+value")),
        (1, _rx(r"(?:total\s+)?(?:amount|value)\s+before\s+(?:tax|gst)|total\s+before\s+(?:tax|gst)")),
        (1, _rx(r"total\s+value\s+of\s+supply")),
        (3, _rx(r"sub\s*-?\s*total")),
    ],
    "igst": [
        (1, _rx(_TAX_PREFIX + r"(?:igst|i\.g\.s\.t\.?|integrated\s+(?:gst|goods\s+(?:and|&)\s+services?\s+tax|tax))(?:\s+(?:amount|amt|value))?" + _NOT_RATE)),
    ],
    "cgst": [
        (1, _rx(_TAX_PREFIX + r"(?:cgst|c\.g\.s\.t\.?|central\s+(?:gst|tax|goods\s+(?:and|&)\s+services?\s+tax))(?:\s+(?:amount|amt|value))?" + _NOT_RATE)),
    ],
    "sgst": [
        (1, _rx(_TAX_PREFIX + r"(?:sgst|utgst|s\.g\.s\.t\.?|ut\s*gst|state\s*(?:/\s*ut\s*)?(?:gst|tax)|union\s+territory\s+(?:gst|tax))(?:\s*/\s*utgst)?(?:\s+(?:amount|amt|value))?" + _NOT_RATE)),
    ],
    "other_tax": [
        (1, _rx(_TAX_PREFIX + r"(?:(?:compensation\s+)?cess|tcs|tax\s+collected\s+at\s+source|other\s+tax(?:es)?)(?:\s+(?:amount|amt))?" + _NOT_RATE)),
    ],
    "round_off": [
        (1, _rx(r"(?:add|less)?\s*[:\-]?\s*(?:round(?:ed|ing)?\s*-?\s*off|rounding(?:\s+adjustment)?)")),
    ],
    "discount": [
        (1, _rx(r"(?:less\s*[:\-]?\s*)?(?:trade\s+|cash\s+|special\s+)?discount")),
    ],
    "other_charges": [
        (1, _rx(r"(?:add\s*[:\-]?\s*)?(?:freight|shipping|delivery|packing|packaging|handling|courier|other)\s+(?:charges?|cost)")),
    ],
    "total": [
        (1, _rx(r"grand\s+total")),
        (1, _rx(r"(?:total\s+)?invoice\s+(?:total|value|amount)")),
        (1, _rx(r"total\s+(?:amount\s+)?(?:payable|due)|(?:net\s+)?amount\s+payable|net\s+payable")),
        (1, _rx(r"total\s+(?:amount\s+|value\s+)?(?:after|incl(?:uding|\.)?|inclusive\s+of|with)\s+(?:tax|taxes|gst)")),
        (2, _rx(r"total\s+amount(?!\s*(?:before|excl|exclusive|in\s+words))")),
        (2, _rx(r"net\s+amount|amount\s+due|balance\s+due")),
        (3, _rx(r"total(?!\s*(?:taxable|tax\b|taxes|igst|cgst|sgst|utgst|gst|qty|quantity|before|excl|discount"
                r"|items?\b|units?\b|no\b|in\s+words|amount\s+before|value\s+of\s+supply|hsn|hours|hrs))")),
    ],
}

# Table/column header vocabularies (matched with fullmatch against a whole cell or segment).
_GOODS = r"(?:services?|goods|items?|supply|products?|materials?|works?)"
# "Goods / Services", "Goods and Services", "Goods, Services"
_GOODS_LIST = _GOODS + r"(?:\s*(?:&|and|/|,|\+)\s*" + _GOODS + r")*"
_HSN_WORD = r"(?:hsn|sac|hsn\s*/\s*sac|sac\s*/\s*hsn)(?:\s*code)?(?:\s*(?:no|number)\.?)?"
DESC_HEADER_RE = re.compile(
    r"(?:(?:item|product|service|goods)\s+)?(?:description|particulars|details|narration|descriptions)"
    r"(?:\s+of\s+" + _GOODS_LIST + r")?"
    r"(?:\s*(?:&|and|/|,)\s*" + _HSN_WORD + r")?(?:\s*\([^)]*\))?"
    r"|" + _GOODS_LIST + r"(?:\s+(?:name|rendered|provided|description|supplied))?(?:\s*\([^)]*\))?"
    r"|nature\s+of\s+(?:services?|supply|work)"
    r"|(?:name|particulars)\s+of\s+" + _GOODS_LIST,
    I,
)
TAXABLE_HEADER_RE = re.compile(
    r"(?:total\s+)?(?:taxable\s+(?:value|amount|amt)|assessable\s+value|value\s+of\s+(?:supply|services?|goods))"
    r"(?:\s*\([^)]*\))?", I
)

# Line-item table columns.
HSN_HEADER_RE = re.compile(_HSN_WORD, I)
SERIAL_HEADER_RE = re.compile(
    r"(?:s\.?\s*r?\.?\s*no|sl\.?\s*no|sr\.?\s*no|serial(?:\s*(?:no|number))?|item\s*no|line\s*no|#|no)\.?", I)
_PER_UNIT = r"(?:\s*(?:per|/)\s*(?:unit|transaction|txn|item|piece|pc|nos?))?"
QTY_HEADER_RE = re.compile(
    r"(?:qty|quantity|nos|units?|hours?|hrs|days?"
    r"|(?:count|number|no\.?)\s*(?:of\s+)?(?:transactions?|txns?|units?|items?|services?|nos)"
    r"|counts?|transactions?)(?:\s*\([^)]*\))?", I)
RATE_HEADER_RE = re.compile(
    r"(?:rate" + _PER_UNIT + r"|unit\s*(?:price|rate|cost)|price" + _PER_UNIT +
    r"|mrp|charges?" + _PER_UNIT + r")(?:\s*\([^)]*\))?", I)
# A line's own total, which may carry a currency word: "Total Amount Rs.", "Amount (INR)".
LINE_AMOUNT_HEADER_RE = re.compile(
    r"(?:(?:line|net|gross|total|taxable)\s+)?(?:amount|amt|value|total|charges?)"
    r"(?:\s*(?:rs\.?|inr|₹|usd|\$))?(?:\s*\([^)]*\))?", I)
# Optional "@ 18%" between the tax name and the word Amount, so "IGST 18% Amount" maps to the
# amount column while a bare "IGST Rate" / "IGST %" column does not.
_RATE_PART = r"(?:\s*@?\s*\d+(?:\.\d+)?\s*%)?"


def _tax_header(body: str) -> re.Pattern[str]:
    return re.compile(r"(?:" + body + r")" + _RATE_PART + r"(?:\s*(?:amount|amt|value))?(?:\s*\([^)]*\))?", I)


IGST_HEADER_RE = _tax_header(r"igst|i\.g\.s\.t\.?|integrated\s+(?:gst|tax)")
CGST_HEADER_RE = _tax_header(r"cgst|c\.g\.s\.t\.?|central\s+(?:gst|tax)")
SGST_HEADER_RE = _tax_header(r"sgst|utgst|s\.g\.s\.t\.?|state\s*(?:/\s*ut\s*)?(?:gst|tax)|sgst\s*/\s*utgst")

# An HSN/SAC code printed inside the description cell, which must not stay in the description.
HSN_LABELLED_RE = re.compile(
    r"[\[(]?\s*(?:hsn|sac)(?:\s*/\s*(?:hsn|sac))?\s*(?:code)?\s*(?:no\.?)?\s*[:\-]?\s*(\d{4,8})\s*[\])]?", I)
HSN_TRAILING_RE = re.compile(r"[\s\-:(\[]+(\d{6}|\d{8})\s*[)\]]?\s*$")
# A period expressed as a pair of table columns: a "From" column and a "To" column, usually
# under a spanning heading such as "Transaction Date". Matched with fullmatch on a cell/segment.
# Repeated so a header merged down two rows ("Transaction Date" + "From") still resolves.
_COL_PREFIX = r"(?:(?:period|dates?|service|transaction|txn|billing|supply|invoice|usage)\s+)*"
FROM_HEADER_RE = re.compile(
    _COL_PREFIX + r"(?:from|w\.?e\.?f\.?)(?:\s+date)?|start(?:ing)?(?:\s+date)?|date\s+from", I)
TO_HEADER_RE = re.compile(
    _COL_PREFIX + r"(?:to|till|upto|until)(?:\s+date)?|end(?:ing)?(?:\s+date)?|date\s+to", I)
PERIOD_PARENT_RE = re.compile(
    r"(?:(?:transaction|service|billing|usage|statement|rental|subscription|contract|supply|travel|journey|stay)\s+)?"
    r"(?:date(?:\s*(?:range|period))?s?|periods?|duration)" + _PAREN, I)
# Labels like "From"/"To" that only mean a period when a date follows them.
BARE_PERIOD_LABEL_RE = re.compile(r"(?:from|to|till|upto|until|w\.?e\.?f\.?)\s*[:\-]?", I)

# Lines that end a line-item section (totals, taxes, footers).
SECTION_STOP_RE = re.compile(
    r"^\s*(?:(?:add|less)\s*[:\-]?\s*)?(?:sub\s*-?\s*total|grand\s+total|total\b|taxable\b|igst\b|cgst\b|sgst\b"
    r"|utgst\b|integrated\s+tax|central\s+tax|state\s+tax|tax\s+amount|amount\s+(?:in\s+words|chargeable|payable)"
    r"|in\s+words|round(?:ing|ed)?\s*-?\s*off|bank\b|terms\b|declaration|e\.?\s*&\s*o\.?\s*e|authori[sz]ed"
    r"|payment\s+(?:terms|details|instructions)|notes?\s*:|discount\b|net\s+amount|balance\s+due|amount\s+due)",
    I,
)
# Text that is never a service description.
NOISE_RE = re.compile(
    r"terms\s*(?:&|and)\s*conditions|bank\s+details|account\s+(?:no|number)|ifsc|thank\s+you"
    r"|authori[sz]ed\s+signatory|computer\s+generated|declaration|amount\s+in\s+words",
    I,
)
# Lines inside an address block that carry identifiers rather than address text.
ID_LINE_RE = re.compile(
    r"^\s*(?:(?:customer|buyer|recipient|party|client)(?:'s)?\s+)?(?:gstin|gst\s*(?:no|number|in|reg)|pan\b|state\s*code|cin\b)",
    I,
)
# Labels that end an address block even though they are not extracted fields.
ADDRESS_STOP_RE = re.compile(
    r"^\s*(?:s\.?\s*no|sl\.?\s*no|sr\.?\s*no|place\s+of\s+supply|hsn|sac\b|due\s+date|terms|payment|reverse\s+charge)",
    I,
)
TITLE_NOISE_RE = re.compile(
    r"^(?:tax\s+invoice|invoice|bill\s+of\s+supply|proforma\s+invoice|commercial\s+invoice|e-?invoice"
    r"|original(?:\s+for\s+recipient)?|duplicate(?:\s+for\s+\w+)?|triplicate(?:\s+for\s+\w+)?|page\s+\d+.*"
    r"|\(?original\s+copy\)?)$",
    I,
)

_GSTIN_BODY = r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]"
GSTIN_RE = re.compile(r"\b" + _GSTIN_BODY + r"\b")
# A labelled GSTIN sitting inside a line of address text, which happens when a narrow gap
# merges the next column into this one: "..., Sakhar GSTIN: 27ABOCS0349A1ZG".
GSTIN_LABELLED_RE = re.compile(
    r"\s*\b(?:gstin|gst(?:\s*(?:no|in|number|reg))?)\s*(?:/\s*uin)?\s*[:\-]?\s*(" + _GSTIN_BODY + r")\b", I)


def split_gstins(text: str) -> tuple[str, list[str]]:
    """Separate labelled GSTINs from a line of address text, returning the text without them."""
    found = [m.group(1).upper() for m in GSTIN_LABELLED_RE.finditer(text)]
    if not found:
        return text, []
    cleaned = GSTIN_LABELLED_RE.sub(" ", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ,;|-"), found
NIL_RE = re.compile(r"\s*(?:nil|n\.?\s*a\.?|not\s+applicable|-+|0+(?:\.0+)?)\s*", I)
RATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# --------------------------------------------------------------------------------------
# Amounts
# --------------------------------------------------------------------------------------

_CUR = r"(?:₹|rs\.?|inr|usd|us\$|\$|eur|€|gbp|£)"
AMOUNT_RE = re.compile(
    r"(?<![\w/.\-])"
    r"(?P<open>\()?(?P<neg>-)?\s?(?:" + _CUR + r"\s?)?(?P<neg2>-)?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<close>\))?"
    r"(?![\w/%]|[.,]\d|\s?%|-\d)",
    I,
)
_RATE_STRIP_RE = re.compile(r"@\s*\d+(?:\.\d+)?\s*%?|\d+(?:\.\d+)?\s*%")


@dataclass
class AmountMatch:
    value: Decimal
    raw: str
    grouping_ok: bool


def valid_grouping(num: str) -> bool:
    """True for plain digits, Western (1,234,567) or Indian (12,34,567) digit grouping."""
    integer = num.split(".")[0]
    if "," not in integer:
        return True
    groups = integer.split(",")
    if not 1 <= len(groups[0]) <= 3 or any(not g.isdigit() for g in groups):
        return False
    if all(len(g) == 3 for g in groups[1:]):
        return True
    return len(groups[-1]) == 3 and all(len(g) == 2 for g in groups[1:-1]) and len(groups[0]) <= 2


def find_amounts(text: str) -> list[AmountMatch]:
    """All monetary amounts in a piece of text, ignoring tax rates such as '@ 18%'."""
    cleaned = _RATE_STRIP_RE.sub(" ", text)
    out = []
    for m in AMOUNT_RE.finditer(cleaned):
        num = m.group("num")
        try:
            value = Decimal(num.replace(",", ""))
        except InvalidOperation:
            continue
        if m.group("neg") or m.group("neg2") or (m.group("open") and m.group("close")):
            value = -value
        out.append(AmountMatch(value, m.group(0).strip(), valid_grouping(num)))
    return out


def parse_amount(text: str | None) -> Decimal | None:
    """Strictly parse a single amount (used for user-edited values). Raises ValueError if invalid."""
    if text is None or not str(text).strip():
        return None
    s = re.sub(_CUR, "", str(text).strip(), flags=I).replace(" ", "")
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s.startswith("-"):
        negative, s = True, s[1:]
    if not re.fullmatch(r"\d[\d,]*(?:\.\d+)?", s) or not valid_grouping(s):
        raise ValueError(f"Not a valid amount: {text!r}")
    value = Decimal(s.replace(",", ""))
    return -value if negative else value


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------

_MON = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE_BODY = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[/.\-]\d{1,2}[/.\-](?:\d{4}|\d{2})"
    r"|\d{1,2}(?:st|nd|rd|th)?[\s\-/.]*" + _MON + r"\.?[\s\-/.,']*(?:\d{4}|\d{2})"
    r"|" + _MON + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})"
)
_DATE_START = r"(?<![\w/.\-])"
_DATE_END = r"(?![\w/]|[.\-]\d)"
DATE_RE = re.compile(_DATE_START + _DATE_BODY + _DATE_END, I)
DATE_RANGE_RE = re.compile(
    _DATE_START + r"(?P<d1>" + _DATE_BODY + r")\s*(?:to|till|until|through|thru|up\s*to|-|–|—|~)\s*(?P<d2>"
    + _DATE_BODY + r")" + _DATE_END,
    I,
)
MONTH_PERIOD_RE = re.compile(
    r"(?:for\s+the\s+month\s+of|month\s*(?:of)?\s*[:\-]?)\s*(?P<mon>" + _MON + r")\.?[\s\-/,']*(?P<year>\d{4}|\d{2})\b", I
)
MONTH_ONLY_RE = re.compile(r"^\s*(?P<mon>" + _MON + r")\.?[\s\-/,']*(?P<year>\d{4}|\d{2})\s*$", I)


def parse_date(raw: str, dayfirst: bool = True) -> date | None:
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw.strip(), flags=I)
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):
            d = datetime.strptime(s, "%Y-%m-%d").date()
        else:
            d = date_parser.parse(s, dayfirst=dayfirst, fuzzy=False).date()
    except (ValueError, OverflowError, TypeError):
        return None
    return d if 1990 <= d.year <= 2100 else None


def find_date(text: str, dayfirst: bool = True) -> tuple[date, str] | None:
    for m in DATE_RE.finditer(text):
        d = parse_date(m.group(0), dayfirst)
        if d:
            return d, m.group(0)
    return None


def split_hsn(text: str) -> tuple[str, str | None]:
    """Separate an HSN/SAC code from a description cell: 'Risk Radar 998314' -> ('Risk Radar', '998314').

    Only labelled codes, or a bare 6/8-digit code at the end, are removed. A 4-digit number is
    left alone unless labelled, so descriptions ending in a year survive intact.
    """
    text = (text or "").strip()
    m = HSN_LABELLED_RE.search(text)
    if m:
        cleaned = (text[:m.start()] + " " + text[m.end():]).strip(" -:,;|")
        return re.sub(r"\s{2,}", " ", cleaned), m.group(1)
    m = HSN_TRAILING_RE.search(text)
    if m and len(text[:m.start()].strip()) >= 3:
        return text[:m.start()].strip(" -:,;|"), m.group(1)
    return text, None


def find_all_dates(text: str, dayfirst: bool = True) -> list[date]:
    found = [parse_date(m.group(0), dayfirst) for m in DATE_RE.finditer(text)]
    return [d for d in found if d]


def find_date_range(text: str, dayfirst: bool = True) -> tuple[date, date, str] | None:
    for m in DATE_RANGE_RE.finditer(text):
        d1, d2 = parse_date(m.group("d1"), dayfirst), parse_date(m.group("d2"), dayfirst)
        if d1 and d2:
            return d1, d2, m.group(0)
    return None


def month_bounds(mon: str, year: str) -> tuple[date, date] | None:
    try:
        month = date_parser.parse(f"1 {mon[:3]} 2000").month
    except (ValueError, OverflowError):
        return None
    y = int(year)
    if y < 100:
        y += 2000
    return date(y, month, 1), date(y, month, calendar.monthrange(y, month)[1])


# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/_.]*")
_NOT_A_NUMBER = {"date", "dated", "dt", "no", "number", "na", "nil"}


def find_document_number(text: str) -> str | None:
    """First invoice/PO-style identifier in text, exactly as printed (leading zeros kept)."""
    text = re.sub(r"^[\s:#.\-)\]]+", "", text)
    text = re.sub(r"^\([^)]*\)\s*[:#\-]?\s*", "", text)
    tokens = list(_TOKEN_RE.finditer(text))
    if not tokens:
        return None
    first = tokens[0].group(0).rstrip(".-/_")
    if first.lower() in _NOT_A_NUMBER:
        return None
    # "INV 0012" printed with a space: keep both parts.
    if first.isalpha() and len(first) <= 5 and len(tokens) > 1 and tokens[1].group(0)[0].isdigit():
        between = text[tokens[0].end():tokens[1].start()]
        if between.strip() == "":
            first = f"{first} {tokens[1].group(0).rstrip('.-/_')}"
    if not any(ch.isdigit() for ch in first):
        return None
    if DATE_RE.fullmatch(first):
        return None
    return first if len(first) <= 40 else None


CURRENCY_MARKERS = [
    ("INR", re.compile(r"₹|\bINR\b|\bRs\.?(?=[\s\d.)])|\brupees\b", I)),
    ("USD", re.compile(r"\bUSD\b|US\$|\$")),
    ("EUR", re.compile(r"\bEUR\b|€")),
    ("GBP", re.compile(r"\bGBP\b|£")),
]


def detect_currencies(text: str) -> list[str]:
    return [code for code, rx in CURRENCY_MARKERS if rx.search(text)]


# --------------------------------------------------------------------------------------
# Vendor-specific rules
# --------------------------------------------------------------------------------------


def load_vendor_rules(path: Path | None) -> list[dict]:
    if path is None or not Path(path).is_file():
        return []
    try:
        rules = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read vendor rules %s: %s", path, exc)
        return []
    return rules if isinstance(rules, list) else []


def _label_from_text(label: str) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(part) for part in label.split())
    return _rx(body)


def labels_for_document(text: str, rules: list[dict]) -> tuple[dict[str, LabelList], list[str]]:
    """Base labels plus labels from every vendor rule whose match conditions hold for this text."""
    labels = {k: list(v) for k, v in FIELD_LABELS.items()}
    matched: list[str] = []
    upper, lower = text.upper(), text.lower()
    for rule in rules:
        cond = rule.get("match", {})
        if not cond:
            continue
        gstin = cond.get("gstin")
        contains = cond.get("text_contains")
        if gstin and gstin.upper() not in upper:
            continue
        if contains and contains.lower() not in lower:
            continue
        for field_name, extra in rule.get("labels", {}).items():
            if field_name not in labels:
                log.warning("Vendor rule %r refers to unknown field %r", rule.get("name"), field_name)
                continue
            labels[field_name] = [(0, _label_from_text(lbl)) for lbl in extra] + labels[field_name]
        matched.append(rule.get("name", "unnamed rule"))
    return labels, matched
