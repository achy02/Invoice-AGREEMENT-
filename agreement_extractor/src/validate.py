"""S6 -- rule-based checks. Reports, never corrects.

The invoice half of this project settled the principle and it carries over unchanged: a
check that silently repairs a number destroys the evidence that something was wrong. Every
rule here raises a flag and leaves the value exactly as extracted.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime

from . import basis
from .config import Config
from .models import Grounded

log = logging.getLogger(__name__)

#: Text left over from an unfilled template. "Commercials for Company Name" is in the real
#: ALSD letter -- the placeholder was never replaced -- so this is not hypothetical.
PLACEHOLDERS = [
    re.compile(r"\bcompany name\b", re.I),
    re.compile(r"\[\s*client\s*\]", re.I),
    re.compile(r"\bXXXX+\b"),
    re.compile(r"<\s*insert[^>]*>", re.I),
    re.compile(r"\bTBD\b"),
]

#: A "revised" schedule implies an earlier version, which may still be the operative one.
REVISED = re.compile(r"\brevised\b|\bamend(?:ed|ment)\b|\bsupersed", re.I)

DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %B %Y", "%d %b %Y",
                "%B %d, %Y", "%b %d, %Y")


@dataclass
class Finding:
    severity: str          # ERROR | WARNING | INFO
    field: str
    message: str


@dataclass
class DocumentResult:
    file_name: str
    client: str = ""
    fields: list[Grounded] = dataclass_field(default_factory=list)
    charges: list[Grounded] = dataclass_field(default_factory=list)
    findings: list[Finding] = dataclass_field(default_factory=list)
    signed_status: str = "UNCERTAIN"

    @property
    def review_required(self) -> bool:
        return (any(f.severity != "INFO" for f in self.findings)
                or any(g.status != "OK" for g in self.fields + self.charges))


def parse_date(value) -> date | None:
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text, flags=re.I).replace("–", "-")
    cleaned = re.sub(r"\s*-\s*", "-", cleaned).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    try:
        from dateutil import parser as dateutil_parser

        return dateutil_parser.parse(cleaned, dayfirst=True).date()
    except Exception:
        return None


#: Indian magnitude shorthand, as these documents actually write it. The suffix must follow
#: the number immediately (at most one space), so "20 Templates" and "15 days" are untouched
#: while "INR 6k" and "1.5 Lakh" are not.
#: Order matters: the spelled-out words are tried before the single letter, so the "L" of
#: "Lakh" is never mistaken for a bare "L". The bare "L" alone stays case-sensitive --
#: lowercase "l" beside a number is far more often a stray character than a lakh.
MAGNITUDES = (
    (re.compile(r"cr(?:ore)?s?\b", re.I), 10_000_000),
    (re.compile(r"(?:lakhs?|lacs?)\b", re.I), 100_000),
    (re.compile(r"L\b"), 100_000),
    (re.compile(r"k\b", re.I), 1_000),
)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_number(text) -> float | None:
    """The first number in a string as printed, with Indian shorthand resolved.

        "INR 1,00,000/-"   -> 100000.0
        "INR 6k"           -> 6000.0
        "1.5 Lakh"         -> 150000.0
        "2 crore"          -> 20000000.0

    The shorthand matters because this function decides whether an extracted rate agrees
    with the text it was taken from. The MSA prints "Template creation service available
    INR 6k per template"; the model read 6000, which is right, and this read 6, which is
    not -- so a correct extraction was reported to a human as a mismatch. A validator that
    cries wolf is worse than no validator, because the one real error is now in a list of
    false ones.

    The suffix is only honoured directly after the number. "20 Templates @ NIL cost" has a
    T, not a magnitude, and "15 days" must stay 15.
    """
    if isinstance(text, (int, float)):
        return float(text)
    source = str(text or "")
    match = _NUMBER.search(source)
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None

    tail = source[match.end():match.end() + 8]
    if tail[:1] == " ":
        tail = tail[1:]
    for pattern, multiplier in MAGNITUDES:
        if pattern.match(tail):
            return value * multiplier
    return value


def signed_status(evidence_kinds: set[str], tbx_signatory, client_signatory) -> str:
    """SIGNED only with evidence for both sides; PARTIAL for one; else UNSIGNED/UNCERTAIN.

    Structural evidence -- a PDF signature widget or an e-sign envelope id -- is definitive
    for the document as a whole. Otherwise each side needs its own sign: a named signatory,
    a seal, or ink. A typed name alone never reaches SIGNED, because that is also what an
    unsigned template looks like.
    """
    if {"pdf_signature_field", "envelope_id"} & evidence_kinds:
        return "SIGNED"
    sides = sum(bool(x) for x in (tbx_signatory, client_signatory))
    seals = sum(1 for kind in evidence_kinds if kind in {"seal", "ink"})
    if sides >= 2 or (sides >= 1 and seals >= 1):
        return "SIGNED" if seals >= 1 or sides >= 2 else "PARTIAL"
    if sides == 1 or seals == 1:
        return "PARTIAL"
    if evidence_kinds:
        return "UNCERTAIN"
    return "UNSIGNED"


#: Fields whose value is a date. The document says "10th – Nov – 2025", "1st Aug 2024" or
#: "Tue Nov 25 18:16:45 IST 2025"; all three mean one day, and a workbook column that has to
#: be read three ways cannot be sorted, filtered or compared against anything.
DATE_FIELDS = ("offer_letter_date", "stamp_date", "effective_date",
               "client_signature_datetime", "tbx_signature_datetime")


def canonical_date(value) -> str:
    """A date as DD-MMM-YYYY, keeping any time of day the document printed.

    `value` is left untouched -- this only ever fills `normalized_value`, so the verbatim
    text the model quoted stays visible beside it. Reporting, never correcting.
    """
    parsed = parse_date(value)
    if parsed is None:
        return ""
    clock = re.search(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\s*([A-Z]{2,4})?", str(value or ""))
    stamp = f"{parsed:%d-%b-%Y}"
    if clock:
        stamp += " " + clock.group(1) + (f" {clock.group(2)}" if clock.group(2) else "")
    return stamp


def validate_document(result: DocumentResult, config: Config,
                      evidence_kinds: set[str] | None = None) -> DocumentResult:
    by_field = {g.field: g for g in result.fields}

    def value_of(name):
        item = by_field.get(name)
        return item.value if item else None

    # -- dates ----------------------------------------------------------------------------
    for name in DATE_FIELDS:
        item = by_field.get(name)
        if item is not None and item.value:
            item.normalized_value = canonical_date(item.value) or item.normalized_value

    # -- grounding ------------------------------------------------------------------------
    for item in result.fields + result.charges:
        if item.status == "UNGROUNDED":
            result.findings.append(Finding(
                "ERROR", item.field,
                "evidence quote could not be found on the page it cites; "
                "value withheld from the workbook"))
        elif item.match_score and item.match_score * 100 < config.validation.review_on_confidence_below * 100:
            result.findings.append(Finding(
                "WARNING", item.field, f"weak evidence match ({item.match_score:.2f})"))

    # -- how often each charge applies ----------------------------------------------------
    for charge in result.charges:
        data = getattr(charge, "charge", None)
        if data is None:
            continue
        resolved, why = basis.resolve(data)
        data["resolved_basis"] = resolved
        data["basis_note"] = why
        if basis.misfiled(data.get("frequency_basis")):
            result.findings.append(Finding(
                "WARNING", charge.field,
                f"{data.get('frequency_basis')!r} is a payment term or condition, not a "
                f"charging frequency; basis recorded as {basis.LABELS[resolved]}"))

    # -- standard rate against the rate actually offered -----------------------------------
    for charge in result.charges:
        data = getattr(charge, "charge", None) or {}
        offered, standard = parse_number(data.get("rate_inr")), parse_number(
            data.get("standard_rate_inr"))
        if offered is None or standard is None:
            continue
        if offered > standard:
            # An offer letter exists to record a concession, so the client's rate is at or
            # below list. Above it, the two columns have almost certainly been read the
            # wrong way round -- which is how every rate on the Capitalneed letter once came
            # back as the list price. Reported, never silently swapped.
            result.findings.append(Finding(
                "ERROR", charge.field,
                f"rate offered to the client ({offered:g}) is higher than the standard rate "
                f"({standard:g}); check the two columns have not been read the wrong way "
                f"round"))
        elif offered == standard:
            result.findings.append(Finding(
                "WARNING", charge.field,
                f"client rate and standard rate are both {offered:g}; if the document "
                f"prints only one rate, the standard rate should be blank"))

    # -- rates ----------------------------------------------------------------------------
    for charge in result.charges:
        stated = parse_number(getattr(charge, "charge", {}).get("rate_as_written")
                              or charge.evidence_text)
        given = getattr(charge, "charge", {}).get("rate_inr")
        if stated is not None and given is not None:
            try:
                if abs(float(given) - stated) > config.validation.rate_tolerance:
                    result.findings.append(Finding(
                        "ERROR", charge.field,
                        f"rate {given} does not match the text it cites "
                        f"({stated:g} in {charge.evidence_text[:60]!r})"))
            except (TypeError, ValueError):
                result.findings.append(Finding("WARNING", charge.field,
                                               f"rate {given!r} is not a number"))

    # -- dates ----------------------------------------------------------------------------
    offer = parse_date(value_of("offer_letter_date"))
    if value_of("offer_letter_date") and offer is None:
        result.findings.append(Finding("ERROR", "offer_letter_date",
                                       f"could not parse {value_of('offer_letter_date')!r}"))
    # Either party's signing date is enough to catch a letter dated after it was signed.
    for name in ("client_signature_datetime", "tbx_signature_datetime"):
        signed_on = parse_date(value_of(name))
        if offer and signed_on and offer > signed_on:
            result.findings.append(Finding(
                "ERROR", "offer_letter_date",
                f"letter is dated {offer} but {name.split('_')[0]} signed earlier, "
                f"on {signed_on}"))

    for name in ("stamp_date", "effective_date"):
        item = by_field.get(name)
        if item and item.value and item.status == "OK" and not item.evidence_text:
            result.findings.append(Finding(
                "ERROR", name,
                f"{name} given as {item.value!r} with no supporting text; "
                "this field must be null unless the document states it"))

    # -- contradictions and flags ---------------------------------------------------------
    billing = str(value_of("billing_model") or "").lower()
    terms = str(value_of("payment_terms") or "").lower()
    if "prepaid" in billing and re.search(r"\bdays?\b.*invoice|invoice.*\bdays?\b", terms):
        result.findings.append(Finding(
            "WARNING", "billing_model",
            f"billing model is prepaid but payment terms say {value_of('payment_terms')!r}"))

    if not value_of("payment_terms"):
        result.findings.append(Finding("WARNING", "payment_terms", "not stated in the document"))
    if not value_of("termination_notice"):
        result.findings.append(Finding("WARNING", "termination_notice", "not stated"))

    haystack = " ".join(str(g.evidence_text or "") for g in result.fields + result.charges)
    for pattern in PLACEHOLDERS:
        found = pattern.search(haystack)
        if found:
            result.findings.append(Finding(
                "WARNING", "template", f"unreplaced template placeholder: {found.group(0)!r}"))
            break
    if REVISED.search(haystack):
        result.findings.append(Finding(
            "INFO", "commercials",
            "schedule is labelled revised or amended; an earlier version may exist"))

    # -- signatures -----------------------------------------------------------------------
    result.signed_status = signed_status(evidence_kinds or set(),
                                         value_of("tbx_signatory"),
                                         value_of("client_signatory"))
    if result.signed_status != "SIGNED":
        result.findings.append(Finding(
            "WARNING", "signed_status",
            f"signature evidence is {result.signed_status}; confirm by eye"))

    return result
