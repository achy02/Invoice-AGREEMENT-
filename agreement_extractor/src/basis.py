"""How often a charge applies, as a closed set rather than free text.

`frequency_basis` holds the words the document used, which is right for provenance and
useless for analysis: across four offer letters it produced nineteen distinct values for
sixty-two charges, four of which meant "per transaction" ("Per transaction", "Per Instance",
"Per instance", "Count Based") and four of which were not frequencies at all ("30 days from
Invoice issuance" is a payment term; "With advance notice of 30 days" is a notice period).
A column like that cannot be filtered, pivoted or summed.

So the verbatim words stay exactly as extracted, and a canonical class is derived beside
them. Deriving it in code rather than trusting the model's own label matters for the same
reason S5 re-finds every quote: a classification that cannot be reproduced from the text is
an opinion, and an opinion that disagrees with the document should surface as a flag rather
than settle silently into a spreadsheet column.
"""
from __future__ import annotations

import re

#: The vocabulary, in the order a reader would group a price list.
BASES = (
    "ONE_TIME",            # setup, integration, onboarding -- charged once
    "PER_TRANSACTION",     # per transaction, instance, API call, report, SMS, stamp
    "PER_MAN_DAY",         # priced by effort
    "MONTHLY",
    "QUARTERLY",
    "ANNUAL",              # per year, once a year, AMC
    "VOLUME_SLAB",         # the rate depends on which volume tier the client lands in
    "AS_INCURRED",         # at actuals, reimbursable, as and when applicable
    "NOT_STATED",          # the document genuinely does not say
)

LABELS = {
    "ONE_TIME": "One-time",
    "PER_TRANSACTION": "Per transaction",
    "PER_MAN_DAY": "Per man-day",
    "MONTHLY": "Monthly",
    "QUARTERLY": "Quarterly",
    "ANNUAL": "Annual",
    "VOLUME_SLAB": "Volume / slab based",
    "AS_INCURRED": "As incurred",
    "NOT_STATED": "Not stated",
}

#: Tried in order; the first hit wins, so the specific patterns come before the loose ones.
#: "Once a year" must beat "per ... year", and "per man-day" must beat "per <anything>".
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PER_MAN_DAY", re.compile(r"\bman[-\s]?day|\bperson[-\s]?day|\bper\s+day\b", re.I)),
    ("VOLUME_SLAB", re.compile(r"\bslab|\btier(?:ed|s)?\b|\bvolume[-\s]based|"
                               r"\bbased\s+on\s+volume|\bper\s+slab", re.I)),
    ("QUARTERLY", re.compile(r"\bquarter(?:ly)?\b|\bper\s+quarter\b|\bq-?o-?q\b", re.I)),
    ("MONTHLY", re.compile(r"\bmonthly\b|\bper\s+month\b|\bp\.?m\.?\b|\ba\s+month\b", re.I)),
    ("ANNUAL", re.compile(r"\bannual(?:ly)?\b|\byearly\b|\bper\s+(?:year|annum)\b|"
                          r"\bonce\s+a\s+year\b|\bp\.?a\.?\b|\bamc\b", re.I)),
    ("ONE_TIME", re.compile(r"\bone[-\s]?time\b|\bonce\s+only\b|\bset[-\s]?up\b|"
                            r"\bsetup\b|\bonboarding\b|\bone\s+off\b", re.I)),
    ("PER_TRANSACTION", re.compile(
        r"\bper\s+(?:transaction|instance|txn|hit|call|api|request|record|report|sms|"
        r"email|stamp|document|doc|page|verification|validation|check|query|lead|user|"
        r"account|unit|count)\b"
        r"|\bcount[-\s]based\b|\bper\s+successful\b|\btransaction\s+basis\b"
        r"|\bper\s+e-?sign\b|\bper\s+e-?stamp\b", re.I)),
    ("AS_INCURRED", re.compile(r"\bat\s+actuals?\b|\bas\s+per\s+actuals?\b|\breimburs|"
                               r"\bas\s+and\s+when\s+applicable\b|\bas\s+applicable\b|"
                               r"\bon\s+actuals?\b", re.I)),
)

#: Text that belongs to another column entirely. Finding one of these in `frequency_basis`
#: is a reliable sign the model filled the field from the wrong part of the document --
#: payment terms and notice periods both read like durations and neither is a frequency.
NOT_A_FREQUENCY = re.compile(
    r"\bfrom\s+invoice\b|\bof\s+invoice\b|\binvoice\s+(?:issuance|date)\b"
    r"|\badvance\s+notice\b|\bnotice\s+period\b|\bwithin\s+\d+\s+days\b"
    r"|\b\d+\s+days\s+from\b|\bpost\s+(?:uat|evaluation)\b|\bas\s+discussed\b"
    r"|\bmutually\s+agreed\b", re.I)


def classify(*texts: object) -> str:
    """The canonical basis implied by the words given, or NOT_STATED.

    Several strings are accepted because the answer is often not in `frequency_basis` at
    all: "INR 2/- Per Transaction (Including 1 Rs Credit)" states it in the rate itself, and
    "Additional scope ... charged on man-day(s) basis" states it in the prose. They are
    searched in the order passed, so the caller decides which field speaks first.
    """
    for text in texts:
        blob = str(text or "")
        if not blob.strip():
            continue
        for basis, pattern in RULES:
            if pattern.search(blob):
                return basis
    return "NOT_STATED"


def misfiled(text: object) -> bool:
    """Does this look like a payment term or a notice period rather than a frequency?"""
    blob = str(text or "")
    if not blob.strip():
        return False
    # A phrase that also carries a real frequency is not misfiled, merely wordy.
    if classify(blob) != "NOT_STATED":
        return False
    return bool(NOT_A_FREQUENCY.search(blob))


def resolve(charge: dict) -> tuple[str, str]:
    """The basis for one charge, and a one-line account of where it came from.

    The document's own words decide it wherever they can. Only when they say nothing usable
    does the model's own `rate_basis` stand in, and then the note says so -- the difference
    between "the page says per transaction" and "the model thinks per transaction" is
    exactly the difference a reviewer needs to see.
    """
    stated = classify(charge.get("frequency_basis"),
                      charge.get("rate_as_written"),
                      charge.get("condition_notes"))
    proposed = str(charge.get("rate_basis") or "").strip().upper()

    if stated != "NOT_STATED":
        if proposed in BASES and proposed != stated:
            return stated, (f"derived from the document's wording; the model proposed "
                            f"{LABELS[proposed]}")
        return stated, "derived from the document's wording"
    if proposed in BASES:
        return proposed, "stated by the model; the wording does not say"
    return "NOT_STATED", "neither the document nor the model states it"
