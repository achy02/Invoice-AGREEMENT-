"""S2c -- repairing OCR glyph confusions, with a receipt.

`validate.py` says "reports, never corrects", and it is right about *values*: silently
rewriting an extracted rate destroys the evidence that something was wrong. This module
sits one layer below that rule rather than breaking it. It does not correct the document;
it corrects a **misreading of** the document -- the recogniser emitting ``SOK`` for ink
that plainly reads ``50K``.

Three things keep that distinction honest:

* the recogniser's original output survives on the Line as `raw_text`, so nothing is lost;
* every change is recorded as a `Repair` naming the rule that made it, so a reviewer can
  see what was touched and disagree;
* anything that cannot be explained by a known glyph confusion is **flagged, not guessed**.
  ``22- O1- Dory`` is handwriting the recogniser lost. Turning ``Dory`` into ``2024`` is
  not a glyph confusion, it is an invention, so the text stays wrong and raises a
  suspicion instead. A wrong value that announces itself beats a wrong value that does not.

Why rules here rather than a fine-tuned recogniser: O/0, S/5 and l/1 confusions are
properties of glyph *shapes*, so a rule written here also holds on a counterparty's paper.
A recogniser fine-tuned on eight TBX documents would learn those documents instead.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

#: Glyphs a recogniser confuses for digits. Only well-attested pairs: each is a shape
#: collision, not a guess. `D` is deliberately absent -- it collides with both 0 and 2
#: (``JUN D024`` is 2024), so it cannot be resolved without inventing, and is left to
#: raise a suspicion instead.
GLYPH_TO_DIGIT = {
    "O": "0", "o": "0", "Q": "0",
    "S": "5", "s": "5",
    "l": "1", "I": "1", "|": "1",
    "Z": "2", "B": "8", "G": "6",
}

#: Indian magnitude suffixes that mark a token as an amount even with no digits left in it.
MAGNITUDE_TAIL = re.compile(r"(K|L|CR|LAC|LACS|LAKH|LAKHS)$", re.I)

#: The line must look like it is talking about money before any digit repair is allowed.
MONEY_CONTEXT = re.compile(r"INR|\bRs\.?|₹|\bper\b|/-|\bcharge|\bfee\b|\bcost\b", re.I)

#: What a repaired amount has to look like, or the repair is rejected and nothing changes.
CLEAN_AMOUNT = re.compile(r"^\d[\d,.]*(?:K|L|Cr|Lac|Lacs|Lakh|Lakhs)?$", re.I)

#: Ordinary words that would survive the amount test if blindly mapped: BOOK -> 800K.
#: The gate is shape-based, so it cannot tell prose from a misread amount on its own.
NOT_AMOUNTS = {
    "BOOK", "BOOKS", "LOOK", "LOOKS", "COOK", "TOOK", "BOOT", "SOON", "GOOD", "GOODS",
    "BLOG", "BOLD", "SOLD", "GOLD", "LOGO", "ZOO", "BOSS", "LOSS", "ROLL", "TOLL",
    "BILL", "BILLS", "GILL", "SILL", "GOAL", "GOALS", "SOIL", "BOIL", "COIL", "COOL",
    "POOL", "TOOL", "TOOLS", "SIGN", "SIGNS", "SOLE", "ROLE", "LOGS", "SLOT", "GLOB",
}

#: A date whose year slot did not come back as four digits. Not repairable, only reportable.
BROKEN_DATE = re.compile(r"\b\d{1,2}\s*[-/]\s*[A-Za-z0-9]{1,4}\s*[-/]\s*(?![0-9]{4}\b)"
                         r"[A-Za-z][A-Za-z0-9]{2,}\b")

#: A stamp certificate number with stray leading punctuation, e.g. ``-IN-DL5209...``.
STAMP_ID = re.compile(r"^[^A-Za-z0-9]+(IN-[A-Z]{2}\d{6,}[A-Z])\b")

#: Below this, a line sitting in a money context is worth a human glance.
LOW_CONFIDENCE = 0.80


@dataclass
class Repair:
    """One applied change, kept so the workbook can show what was touched."""

    rule: str
    before: str
    after: str

    def __str__(self) -> str:                      # for log lines and flags
        return f"{self.rule}: {self.before!r} -> {self.after!r}"


def _repair_amount(token: str) -> str | None:
    """Map a misread amount token to digits, or None if it is not safely resolvable.

    Every character outside the magnitude suffix must be either a digit already or a
    known confusable glyph. A token with a stray letter that is not confusable (``12A5``)
    is left alone: a partial repair would be a guess about the rest.
    """
    if token.upper() in NOT_AMOUNTS:
        return None

    suffix_match = MAGNITUDE_TAIL.search(token)
    suffix = suffix_match.group(0) if suffix_match else ""
    body = token[: len(token) - len(suffix)] if suffix else token
    if not body:
        return None

    mapped = []
    changed = False
    for char in body:
        if char.isdigit() or char in ",.":
            mapped.append(char)
        elif char in GLYPH_TO_DIGIT:
            mapped.append(GLYPH_TO_DIGIT[char])
            changed = True
        else:
            return None                            # unresolvable character: do not guess
    if not changed:
        return None

    candidate = "".join(mapped) + suffix
    return candidate if CLEAN_AMOUNT.match(candidate) else None


def repair_text(text: str, conf: float = 1.0) -> tuple[str, list[Repair], list[str]]:
    """Return `(repaired_text, repairs_applied, suspicions_raised)`.

    Suspicions never change the text. They name something a reviewer should look at.
    """
    repairs: list[Repair] = []
    suspicions: list[str] = []
    original = text

    stamp = STAMP_ID.match(text)
    if stamp:
        # Drop the matched prefix outright. `.strip()` would not: it removes whitespace,
        # and the prefix that prompted this rule is a hyphen.
        text = stamp.group(1) + text[stamp.end(1):]
        repairs.append(Repair("stamp_id_prefix", original, text))

    if MONEY_CONTEXT.search(text):
        parts = text.split(" ")
        for index, token in enumerate(parts):
            stripped = token.strip(".,:;()[]")
            if not stripped or stripped.isdigit():
                continue
            fixed = _repair_amount(stripped)
            if fixed:
                parts[index] = token.replace(stripped, fixed, 1)
                repairs.append(Repair("amount_glyph", stripped, fixed))
        if repairs and parts:
            text = " ".join(parts)
        if conf < LOW_CONFIDENCE:
            suspicions.append(f"low OCR confidence ({conf:.2f}) on a line naming money")

    broken = BROKEN_DATE.search(text)
    if broken:
        suspicions.append(f"date did not resolve to a four-digit year: {broken.group(0)!r}")

    return text, repairs, suspicions


def repair_lines(lines) -> int:
    """Repair a page's OCR lines in place; return how many changes were made.

    Applied after the OCR cache, not before it, so the cache keeps what the recogniser
    said and this keeps what we make of it. A line read correctly keeps an empty
    `raw_text`, so "repaired" stays distinguishable from "right first time".

    Tokens carry the same text as their line and are repaired with it: S5 grounds a quote
    by matching it against tokens, so if the two disagreed, a repaired amount would become
    ungroundable and be thrown away as unevidenced.
    """
    changed = 0
    for line in lines:
        fixed, repairs, suspicions = repair_text(line.text, line.conf)
        if suspicions:
            line.suspicions = suspicions
        if not repairs:
            continue
        line.raw_text = line.text
        line.text = fixed
        line.repairs = [str(r) for r in repairs]
        for token in line.tokens:
            token_fixed, _, _ = repair_text(token.text, token.conf)
            token.text = token_fixed
        changed += len(repairs)
    return changed


def repair_digest() -> str:
    """Hash of this module's source, for the OCR and page cache fingerprints.

    Without it a change to the rules above would not invalidate the cached OCR that the
    old rules produced, and the fix would appear to do nothing -- the same failure the
    table parser and the heading vocabulary each hit once already.
    """
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
