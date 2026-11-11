"""How often a charge applies, reduced to a closed vocabulary.

The column this produces is the one a person filters and pivots on, so it is worth more
scrutiny than most. Two failures matter and they pull in opposite directions: collapsing
distinct bases into one (a monthly fee counted as a per-transaction rate), and failing to
collapse the same basis written four ways (which is the state this replaced -- nineteen
free-text values across sixty-two charges).
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import basis  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    # the four spellings of one basis that the real documents actually used
    ("Per transaction", "PER_TRANSACTION"),
    ("Per Instance", "PER_TRANSACTION"),
    ("Per instance", "PER_TRANSACTION"),
    ("Count Based", "PER_TRANSACTION"),
    # the rest of the vocabulary
    ("One time", "ONE_TIME"),
    ("Set-up and Integration charges", "ONE_TIME"),
    ("on man-day(s) basis", "PER_MAN_DAY"),
    ("INR @13500/man-day(s) basis", "PER_MAN_DAY"),
    ("charged monthly", "MONTHLY"),
    ("INR 5,000 per month", "MONTHLY"),
    ("billed quarterly", "QUARTERLY"),
    ("Once a year", "ANNUAL"),
    ("per annum", "ANNUAL"),
    ("AMC on customisation", "ANNUAL"),
    ("rate depends on the volume slab", "VOLUME_SLAB"),
    ("tiered pricing", "VOLUME_SLAB"),
    ("at actuals", "AS_INCURRED"),
    ("As and when applicable", "AS_INCURRED"),
    ("on a reimbursement basis", "AS_INCURRED"),
    ("", "NOT_STATED"),
    (None, "NOT_STATED"),
])
def test_the_wordings_these_documents_use_each_land_in_one_class(text, expected):
    assert basis.classify(text) == expected


def test_once_a_year_is_annual_and_not_per_transaction():
    """"Once a year" contains "once"; ONE_TIME must not win on that."""
    assert basis.classify("Once a year") == "ANNUAL"


def test_per_man_day_beats_the_generic_per_rule():
    assert basis.classify("per man-day") == "PER_MAN_DAY"


@pytest.mark.parametrize("text", [
    "30 days from Invoice issuance",
    "Within 15 days",
    "With advance notice of 30 days",
    "Post UAT signoff",
    "As discussed",
])
def test_a_payment_term_in_the_frequency_column_is_caught(text):
    """These are the exact values the model put there, and none is a frequency."""
    assert basis.misfiled(text), f"{text!r} should be reported as misfiled"
    assert basis.classify(text) == "NOT_STATED"


def test_a_real_frequency_is_never_called_misfiled():
    for text in ("Per transaction", "Once a year", "on man-day(s) basis", "One time"):
        assert not basis.misfiled(text)


def test_the_basis_is_read_from_the_rate_when_the_frequency_field_is_empty():
    """Most of these documents state it in the rate itself, not in a separate column."""
    charge = {"frequency_basis": None,
              "rate_as_written": "INR 2/- Per Transaction (Including 1 Rs Credit)"}
    resolved, why = basis.resolve(charge)
    assert resolved == "PER_TRANSACTION"
    assert "document" in why


def test_the_document_outranks_the_models_own_label():
    """The page says per transaction; the model said monthly. The page wins, and says so."""
    charge = {"frequency_basis": "Per Transaction", "rate_basis": "MONTHLY"}
    resolved, why = basis.resolve(charge)
    assert resolved == "PER_TRANSACTION"
    assert "Monthly" in why, "a disagreement must be visible, not silently dropped"


def test_the_model_stands_in_only_where_the_wording_says_nothing():
    charge = {"frequency_basis": None, "rate_as_written": "Rs 500", "rate_basis": "ANNUAL"}
    resolved, why = basis.resolve(charge)
    assert resolved == "ANNUAL"
    assert "model" in why, "the reader must know this was not read off the page"


def test_an_unknown_label_from_the_model_is_not_trusted():
    charge = {"frequency_basis": None, "rate_basis": "WHENEVER"}
    assert basis.resolve(charge)[0] == "NOT_STATED"


def test_every_basis_has_a_label():
    assert set(basis.LABELS) == set(basis.BASES)


def test_the_prompt_offers_exactly_this_vocabulary():
    """The model is given a closed list; it must be the same closed list."""
    from src import extract

    prompt = extract.load_prompt("call_b_commercials.md")
    for name in basis.BASES:
        assert f"`{name}`" in prompt, f"{name} is not offered to the model"


def test_the_workbook_shows_the_class_and_the_wording_it_came_from():
    from src import export

    for column in ("Rate Basis", "Frequency / Basis", "How the Basis Was Decided"):
        assert column in export.COMMERCIALS_COLUMNS
