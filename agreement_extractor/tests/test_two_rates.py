"""Both prices per service: the standard rate and the rate this client actually gets.

These letters print two numbers per line. Reading them the wrong way round does not look
like an error anywhere downstream -- every field is populated, every quote grounds, nothing
is flagged -- it simply reports the list price as what the client pays. On the Capitalneed
letter that was every rate on the page: 1,00,000 instead of 15,000, 20 instead of 15, 70
instead of 55, 10,000 instead of 500.

Two things let that happen and both are tested here: the table was never recovered as a grid
(so the column headers that distinguish the two were lost), and the eval skipped every
golden record ending in `S`, so no number ever disagreed with anything.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import config as config_module  # noqa: E402
from src import export, validate  # noqa: E402
from src.models import Grounded, Line, Token  # noqa: E402
from src.text_layer import infer_table_from_layout  # noqa: E402


@pytest.fixture
def config():
    return config_module.load(HERE / "config.yaml")


def line(line_id, text, x0, y0, width=120, height=30):
    bbox = (float(x0), float(y0), float(x0 + width), float(y0 + height))
    return Line(line_id, text, bbox, "text_layer", 1.0, [Token(text, bbox)])


def capitalneed_page():
    """The real layout: five columns held apart by whitespace, no ruled lines at all."""
    rows = [
        (533, [(109, "Header"), (582, "Frequency"), (786, "Free Limit"),
               (964, "Standard"), (1239, "For Client")]),
        (618, [(109, "Set-up and Integration charges"), (582, "One time"), (786, "NA"),
               (964, "INR 1,00,000/-"), (1239, "INR 15,000/-")]),
        (663, [(109, "E-sign"), (582, "Count Based"), (786, "NA"),
               (964, "INR 20"), (1239, "INR 15 /-")]),
        (708, [(109, "E-Stamp (E-Repository Based)"), (582, "Count Based"), (786, "NA"),
               (964, "INR 70 per stamp"), (1239, "INR 55 per stamp")]),
        (881, [(109, "Templatization"), (582, "Count Based"), (786, "1"),
               (964, "INR 10000"), (1239, "INR 500/-")]),
    ]
    out, n = [], 0
    for y, cells in rows:
        for x, text in cells:
            n += 1
            out.append(line(f"p3:L{n:03d}", text, x, y))
    return out


# -- recovering the grid --------------------------------------------------------------------

def test_an_unruled_commercial_table_is_recovered_as_a_grid():
    table = infer_table_from_layout(capitalneed_page(), 3)
    assert table is not None, "no ruled lines means no table unless geometry is used"
    assert table.n_cols == 5


def test_the_two_rate_columns_keep_their_headers():
    """The headers are the only thing that says which number is which."""
    table = infer_table_from_layout(capitalneed_page(), 3)
    header = {cell.col: cell.text for cell in table.cells if cell.row == 0}
    assert header[3] == "Standard"
    assert header[4] == "For Client"


def test_each_rate_lands_under_its_own_header():
    table = infer_table_from_layout(capitalneed_page(), 3)
    by_cell = {(c.row, c.col): c.text for c in table.cells}
    assert by_cell[(1, 3)] == "INR 1,00,000/-", "the standard rate"
    assert by_cell[(1, 4)] == "INR 15,000/-", "what this client pays"


def test_prose_is_not_turned_into_a_table():
    """Numbered contract clauses have a row's geometry; this once made grids out of them."""
    clauses = [
        line("p1:L001", "5.5.", 120, 100, width=60),
        line("p1:L002", "The Service Provider and its employees shall not have or claim "
                        "any charge, lien or right of retention over any property.",
             250, 100, width=1200),
        line("p1:L003", "5.6.", 120, 150, width=60),
        line("p1:L004", "It is the responsibility of the Service Provider to conduct "
                        "suitable background checks while recruiting its employees.",
             250, 150, width=1200),
        line("p1:L005", "5.7.", 120, 200, width=60),
        line("p1:L006", "The Service Provider shall maintain the confidentiality of all "
                        "information disclosed under this Agreement at all times.",
             250, 200, width=1200),
    ]
    assert infer_table_from_layout(clauses, 1) is None


def test_a_signature_block_is_not_a_commercial_table():
    """Two parallel columns run uninterrupted and once beat the real table on length."""
    block = [
        line("p3:L001", "For TransBnk Solutions Pvt Ltd", 250, 1867, width=400),
        line("p3:L002", "For Capitalneed", 1000, 1867, width=300),
        line("p3:L003", "Authorized Signatory", 250, 2011, width=300),
        line("p3:L004", "Authorized Signatory", 1000, 2011, width=300),
        line("p3:L005", "Pulak Jain", 250, 2050, width=200),
        line("p3:L006", "Tribhuvan Petshali", 1000, 2050, width=250),
    ]
    assert infer_table_from_layout(block, 3) is None


def test_too_few_rows_is_not_a_table():
    assert infer_table_from_layout(capitalneed_page()[:5], 3) is None


# -- catching a swap without the golden set -------------------------------------------------

def charge(offered, standard, field="E-sign - Rate"):
    item = Grounded(field=field, value=offered, evidence_text="x", page=3)
    item.charge = {"rate_inr": offered, "standard_rate_inr": standard}
    return item


def findings_for(item, config):
    result = validate.DocumentResult(file_name="x.pdf", charges=[item])
    validate.validate_document(result, config)
    return result.findings


def test_a_client_rate_above_the_standard_rate_is_an_error(config):
    """An offer letter records a concession; paying over list means the columns were swapped."""
    findings = findings_for(charge(offered=100000, standard=15000), config)
    assert any(f.severity == "ERROR" and "higher than the standard rate" in f.message
               for f in findings)


def test_a_proper_discount_passes_quietly(config):
    assert not [f for f in findings_for(charge(offered=15000, standard=100000), config)
                if "standard rate" in f.message]


def test_the_same_number_in_both_columns_is_questioned(config):
    findings = findings_for(charge(offered=15, standard=15), config)
    assert any("both 15" in f.message for f in findings)


def test_one_rate_alone_is_not_second_guessed(config):
    assert not [f for f in findings_for(charge(offered=15, standard=None), config)
                if "standard rate" in f.message]


# -- the workbook ---------------------------------------------------------------------------

def test_the_workbook_shows_both_rates_and_the_discount():
    for column in ("Rate for Client (INR)", "Standard Rate (INR)", "Discount %",
                   "Standard Rate as Written"):
        assert column in export.COMMERCIALS_COLUMNS


def test_the_client_rate_column_cannot_be_mistaken_for_a_generic_rate():
    """"Rate (INR)" gave no clue which of the two prices it held."""
    assert "Rate (INR)" not in export.COMMERCIALS_COLUMNS


@pytest.mark.parametrize("offered, standard, expected", [
    (15000, 100000, 85.0),
    (55, 70, 21.4),
    (15, 15, 0.0),
    (15, None, ""),
    (None, 100, ""),
    (10, 0, ""),
])
def test_the_discount_is_computed_here_not_asked_of_the_model(offered, standard, expected):
    assert export._discount({"rate_inr": offered, "standard_rate_inr": standard}) == expected


def test_the_prompt_names_both_columns_and_which_is_which():
    from src import extract

    prompt = extract.load_prompt("call_b_commercials.md")
    assert "`standard_rate_inr`" in prompt and "`rate_inr`" in prompt
    assert "For Client" in prompt and "Standard" in prompt
    assert "the rate this client pays" in prompt
