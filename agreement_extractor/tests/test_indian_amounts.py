"""Amounts as Indian commercial documents actually write them.

`parse_number` decides whether an extracted rate agrees with the text it came from. On the
Aditya Birla MSA it read "Template creation service available INR 6k per template" as **6**,
disagreed with the model's correct 6000, and reported a correct extraction to a reviewer as
an error.

That is the expensive kind of bug. A reviewer who finds the first three flags are wrong
stops reading the fourth, and the fourth is the real one.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src.validate import parse_number  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    # -- the shorthand -----------------------------------------------------------------
    ("INR 6k per template", 6_000),
    ("INR 6K", 6_000),
    ("6 k", 6_000),
    ("1.5 Lakh", 150_000),
    ("INR 2.5L", 250_000),
    ("Rs 3 lakhs", 300_000),
    ("2 lac", 200_000),
    ("INR 2 crore", 20_000_000),
    ("1.2 Cr", 12_000_000),
    ("5 crores", 50_000_000),
])
def test_indian_shorthand_resolves_to_the_amount_meant(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("text, expected", [
    # -- the lakh-grouped digits that were already handled ------------------------------
    ("INR 1,00,000/-", 100_000),
    ("INR 85,000 per month", 85_000),
    ("INR 13500/man-day(s)", 13_500),
    ("INR 0.40/Instance", 0.40),
    ("INR 12 Per sign over and above free limit", 12),
])
def test_plain_amounts_are_unchanged(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("text, expected", [
    # -- a letter after a number is usually just a letter -------------------------------
    ("20 Templates @ NIL cost", 20),
    ("15 days of invoice", 15),
    ("Includes: 5 Bank's Integration", 5),
    ("2 Months or POC completion", 2),
    ("60 days notice", 60),
    ("Storage period of 3 months", 3),
    ("additional 25 documents or part of in a month", 25),
    ("Up to 500 Documents/Month", 500),
])
def test_an_ordinary_unit_is_not_mistaken_for_a_magnitude(text, expected):
    """The risk of reading suffixes: "3 months" must never become three thousand."""
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", ["", None, "NIL", "NA", "At actuals", "Mutually agreed"])
def test_text_with_no_number_stays_none(text):
    """"NIL" is a real answer and must not become zero by accident."""
    assert parse_number(text) is None


def test_a_number_already_a_number_is_left_alone():
    assert parse_number(6000) == 6000.0
    assert parse_number(0.4) == 0.4


def test_the_msa_rate_no_longer_contradicts_its_own_evidence():
    """The exact pair that produced a false ERROR in the workbook's Review Queue."""
    quoted = "Template creation service available INR 6k per template, post free limit."
    assert parse_number(quoted) == parse_number(6000)
