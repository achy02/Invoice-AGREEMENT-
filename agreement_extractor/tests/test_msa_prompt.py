"""What Call B must be told about an MSA that an offer letter never needed.

Measured on the Aditya Birla MSA against hand-labelled ground truth: 19 of 34 charge lines
found, and every one of the 15 misses fell in one of two places the prompt never mentioned
-- a table of plans (12 misses) and ordinary contract clauses (3). Separately, 4 lines came
back carrying a number the document does not state.

These are assertions about the prompt text rather than about model output, which is the
only part of a prompt that can be tested deterministically. They exist so that an edit that
drops one of these instructions fails here rather than silently costing a third of the
charge lines on the next MSA.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src.extract import load_prompt  # noqa: E402


@pytest.fixture(scope="module")
def prompt():
    return load_prompt("call_b_commercials.md")


# -- where the money hides in a 20-page agreement ---------------------------------------------

@pytest.mark.parametrize("heading", [
    "Schedule II", "Annexure B", "Schedule of Charges",
    "Pricing Table", "Commercials for All Products", "Other T & C",
])
def test_the_prompt_names_the_sections_these_agreements_use(prompt, heading):
    assert heading in prompt


@pytest.mark.parametrize("clause", [
    "payment terms", "liability cap", "invoice dispute window",
    "discontinuation notice", "penalty",
])
def test_charges_hidden_in_ordinary_clauses_are_asked_for(prompt, clause):
    """COM-032, COM-033 and COM-034 live on pages 5, 14 and 11 -- nowhere near a table."""
    assert clause in prompt.lower()


def test_the_clause_charges_have_a_category_of_their_own(prompt):
    assert "Commercial term" in prompt


def test_a_service_that_is_never_priced_has_a_category_too(prompt):
    """Seven rows of the golden set are services listed with no rate anywhere."""
    assert "Not priced" in prompt


# -- a table of plans is many charges, not one ------------------------------------------------

def test_the_plan_matrix_is_explained(prompt):
    """12 of the 15 misses were one table whose columns are charges, not rates."""
    assert "Option C (Expert)" in prompt
    assert "Monthly Infra Cost" in prompt
    assert "One time Infra Setup" in prompt


def test_the_prompt_says_one_row_of_that_table_is_several_charges(prompt):
    assert "four" in prompt.lower() and "charge line" in prompt.lower()


# -- a charge can have no price, and that is the answer ----------------------------------------

@pytest.mark.parametrize("wording", [
    "Chargeable beyond free Limit",
    "On reimbursement basis",
    "To be Borne by Corp/Bank",
    "Waived Off",
    "Up to 500 Documents/Month.",
    "As per the Offer Letter",
])
def test_the_wordings_that_mean_no_price_are_listed(prompt, wording):
    """Each of these produced an invented number or a wrong one in the measured run."""
    assert wording in prompt


def test_borrowing_a_number_from_a_neighbour_is_forbidden(prompt):
    """COM-008 took 12 from the row above; COM-011 took 12 instead of 4000."""
    assert "Never borrow a number" in prompt


def test_the_merged_cell_rule_is_stated(prompt):
    """85,000 spans three Risk Radar bullets; it prices them once, not three times."""
    assert "85,000" in prompt
    assert "merged" in prompt.lower()


# -- the two-rate columns, in the vocabulary MSAs use ------------------------------------------

@pytest.mark.parametrize("header, field", [
    ("Standard Charges", "standard_rate_inr"),
    ("Commercials offered", "rate_inr"),
])
def test_the_msa_rate_column_headers_are_mapped(prompt, header, field):
    """Ninjamonk's table says `Standard Charges | Commercials offered`, not `Standard | For Client`."""
    assert header in prompt
    assert field in prompt
