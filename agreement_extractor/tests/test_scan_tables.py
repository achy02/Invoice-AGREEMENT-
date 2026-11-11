"""Table structure on scanned pages: the grid, and the decision to go looking for it.

An MSA's charges live in Schedule II, a ruled table on a scanned page. Plain OCR read every
character of it correctly and returned 77 loose lines with the four columns interleaved --
"Risk Radar", "Monthly", "Minimum 85,000 per month", "Bank Balance", "2 Months or" -- from
which no charge line could be recovered, because nothing said which column anything was in.
Not one charge was extracted from a 20-page signed agreement.

PP-StructureV3 rebuilds the grid but costs about three minutes a page, against fifty seconds
for plain OCR. So two things are tested here: that the grid is rebuilt correctly from what it
returns, and that it is only ever asked to look at pages that could plausibly hold a table of
charges.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import config as config_module  # noqa: E402
from src import layout, table_ocr  # noqa: E402
from src.models import Line, Page, Token  # noqa: E402


@pytest.fixture
def config():
    return config_module.load(HERE / "config.yaml")


#: Exactly what PP-StructureV3 returned for the Aditya Birla Schedule II page, including the
#: unopened </tbody> and the run-together cell text.
SCHEDULE_II = (
    "<html><body><table>"
    "<tr><td>Header</td><td>Billing Frequency</td><td>Charges</td>"
    "<td>Free period/Limit</td></tr>"
    "<tr><td>Product Set-Up Charges</td><td>One-time</td><td>NIL</td><td>NA</td></tr>"
    "<tr><td colspan=\"4\">Risk Radar</td></tr>"
    "<tr><td>Bank Balance Statement Analytical reports</td><td>Monthly</td>"
    "<td>Minimum85,000permonth</td><td>2 Months or POC completion,</td></tr>"
    "<tr><td rowspan=\"2\">Storage</td><td rowspan=\"2\">Monthly Basis</td>"
    "<td>Chargeable beyond free Limit</td><td>Storage period of 3 months</td></tr>"
    "<tr><td>INR12Per signoverandabove free limit</td><td>NA</td></tr>"
    "</tbody></table></body></html>")


def page_of(texts, number=19, width=130):
    lines = []
    for i, text in enumerate(texts, start=1):
        bbox = (100.0, 100.0 + i * 40, 100.0 + width, 130.0 + i * 40)
        lines.append(Line(f"p{number}:L{i:03d}", text, bbox, "paddle_ocr", 0.97,
                          [Token(text, bbox, 0.97)]))
    return Page(number=number, width=1654, height=2339, dpi=200,
                image_path=Path(f"p-{number}.png"), source="paddle_ocr",
                mean_conf=0.97, lines=lines)


# -- rebuilding the grid ---------------------------------------------------------------------

def test_the_columns_come_back():
    table = table_ocr.build_table(SCHEDULE_II, None, 19, 1)
    assert table is not None
    assert table.n_cols == 4
    header = {c.col: c.text for c in table.cells if c.row == 0}
    assert header == {0: "Header", 1: "Billing Frequency", 2: "Charges",
                      3: "Free period/Limit"}


def test_a_charge_keeps_its_own_row():
    """The whole point: this value and this frequency belong to this service."""
    table = table_ocr.build_table(SCHEDULE_II, None, 19, 1)
    grid = {(c.row, c.col): c.text for c in table.cells}
    assert grid[(1, 0)] == "Product Set-Up Charges"
    assert grid[(1, 1)] == "One-time"
    assert grid[(1, 2)] == "NIL"


def test_a_row_spanning_cell_does_not_shift_the_row_beneath_it():
    """`Storage` spans two rows; without resolving that, every later cell slides left.

    The row under it holds two cells, and they are the third and fourth columns -- not the
    first and second. Getting this wrong reads a rate as a service name.
    """
    table = table_ocr.build_table(SCHEDULE_II, None, 19, 1)
    grid = {(c.row, c.col): c.text for c in table.cells}
    assert grid[(5, 2)] == "INR12Per signoverandabove free limit"
    assert grid[(5, 3)] == "NA"
    assert (5, 0) not in grid and (5, 1) not in grid


def test_a_cell_spanning_columns_is_not_mistaken_for_one_column():
    table = table_ocr.build_table(SCHEDULE_II, None, 19, 1)
    banner = next(c for c in table.cells if c.text == "Risk Radar")
    assert banner.col == 0 and banner.col_span == 4


def test_malformed_markup_is_still_parsed():
    """`</tbody>` with no opening tag is what this model actually emits."""
    assert "</tbody>" in SCHEDULE_II
    assert table_ocr.build_table(SCHEDULE_II, None, 19, 1) is not None


def test_nothing_in_nothing_out():
    assert table_ocr.build_table("", None, 19, 1) is None
    assert table_ocr.build_table("<html><body></body></html>", None, 19, 1) is None


def test_the_table_renders_for_the_model_with_a_cell_id_per_cell():
    table = table_ocr.build_table(SCHEDULE_II, None, 19, 1)
    markdown = table.to_markdown()
    assert "Billing Frequency <p19:T1:R0C1>" in markdown
    assert "| One-time <p19:T1:R1C1> |" in markdown


# -- the better text wins --------------------------------------------------------------------

def test_the_main_ocr_pass_supplies_the_words_where_it_can():
    """PP-Structure's own recogniser drops spaces; the full-page pass does not.

    "INR10Opertemplate" is a letter O where there should be a zero. That is a rate the
    validator would reject and a reader would have to go and check by hand.
    """
    lines = page_of(["INR 100 per template beyond free limit"]).lines
    box = lines[0].bbox
    html = f"<table><tr><td>INR10Opertemplatebeyondfreelimit</td></tr></table>"
    table = table_ocr.build_table(html, [list(box)], 19, 1, lines)
    assert table.cells[0].text == "INR 100 per template beyond free limit"


def test_a_cell_with_no_line_under_it_keeps_the_model_s_own_text():
    html = "<table><tr><td>NA</td></tr></table>"
    table = table_ocr.build_table(html, [[9000.0, 9000.0, 9100.0, 9100.0]], 19, 1,
                                  page_of(["something far away"]).lines)
    assert table.cells[0].text == "NA"


def test_every_cell_does_not_end_up_holding_the_whole_page():
    """The defect this file exists to prevent, in its second form.

    When the detected boxes did not describe the same grid as the markup, every cell fell
    back to the *table's* box. `text_from_lines` then matched every line for every cell, so
    all 36 cells of the Aditya Birla Schedule II came back identical, each holding the whole
    page: 29,808 characters where the page held 1,741, and column identity destroyed exactly
    as thoroughly as when there was no table at all.

    The earlier version of this test passed only because its one stray box happened to sit
    nowhere near its one line. It asserted the wrong thing.
    """
    lines = page_of(["Header", "Billing Frequency", "Charges", "Free period/Limit",
                     "Product Set-Up Charges", "One-time", "NIL", "NA"]).lines
    table = table_ocr.build_table(SCHEDULE_II, [[0.0, 0.0, 10.0, 10.0]], 19, 1, lines)

    texts = [c.text for c in table.cells]
    assert len(set(texts)) > 1, "every cell holds the same text"
    # The symptom, stated directly: one cell holding content from rows other than its own.
    # "Header" is row 0 and "Product Set-Up Charges" is row 1, so no cell may hold both.
    for text in texts:
        assert not ("Header" in text and "Product Set-Up Charges" in text), \
            f"a single cell holds several rows of the page: {text[:90]!r}"
    assert table.cells[0].text == "Header"


def test_a_single_stray_box_is_not_a_grid():
    """One detected cell is a detection artefact. The markup describes the table better."""
    lines = page_of(["Header", "Billing Frequency"]).lines
    table = table_ocr.build_table(SCHEDULE_II, [list(lines[0].bbox)], 19, 1, lines)
    assert table.cells[1].text == "Billing Frequency", "from the markup, not from one box"
    assert table.n_cols == 4


def test_boxes_are_placed_by_geometry_not_by_list_order():
    """A missing box for an empty merged cell used to shift every box after it by one."""
    placed = table_ocr.map_boxes_to_grid([
        [100.0, 100.0, 200.0, 140.0],   # R0C0
        [300.0, 100.0, 400.0, 140.0],   # R0C1
        [100.0, 200.0, 200.0, 240.0],   # R1C0
        [300.0, 200.0, 400.0, 240.0],   # R1C1
    ])
    assert placed[(0, 0)][0] == 100.0 and placed[(0, 0)][1] == 100.0
    assert placed[(1, 1)][0] == 300.0 and placed[(1, 1)][1] == 200.0
    assert len(placed) == 4


def test_boxes_within_a_few_pixels_are_the_same_row():
    """Scanned rules are never perfectly straight; a 3px wobble is not a new row."""
    placed = table_ocr.map_boxes_to_grid([
        [100.0, 100.0, 200.0, 140.0],
        [300.0, 103.0, 400.0, 143.0],
    ])
    assert set(placed) == {(0, 0), (0, 1)}


def test_a_box_grid_with_no_text_under_it_falls_back_to_the_markup():
    """Geometry nowhere near the page's words describes some other page, not this one."""
    lines = page_of(["Header", "Billing Frequency"]).lines
    far_away = [[9000.0, 9000.0, 9100.0, 9100.0], [9200.0, 9000.0, 9300.0, 9100.0]]
    table = table_ocr.build_table(SCHEDULE_II, far_away, 19, 1, lines)
    assert [c.text for c in table.cells][:4] == [
        "Header", "Billing Frequency", "Charges", "Free period/Limit"]


# -- geometry for structure, the page's own OCR for text --------------------------------------

def grid_page(rows, number=20):
    """A table laid out on a page: each cell's text at its own coordinates."""
    lines, n = [], 0
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if not text:
                continue
            n += 1
            bbox = (200.0 + c * 300, 400.0 + r * 200, 200.0 + c * 300 + 260, 400.0 + r * 200 + 60)
            lines.append(Line(f"p{number}:L{n:03d}", text, bbox, "paddle_ocr", 0.97,
                              [Token(text, bbox, 0.97)]))
    boxes = [[200.0 + c * 300, 400.0 + r * 200, 200.0 + c * 300 + 280, 400.0 + r * 200 + 180]
             for r in range(len(rows)) for c in range(len(rows[0]))]
    return lines, boxes


#: The shape of the Aditya Birla infra matrix: option rows against commercial columns.
INFRA_ROWS = [
    ["Plan", "One time Infra Setup", "Monthly Infra Cost", "VAPT and Info Sec"],
    ["Option B", "INR 300000", "INR 45000", "NA"],
    ["Option C", "INR 350000", "INR 95000", "INR 400000"],
    ["Option D", "INR 500000", "To be Borne by Corp", "To be Borne by Corp"],
]


def test_the_page_s_own_words_fill_the_cells_not_the_table_model_s():
    """PP-Structure reads a cropped cell badly; the full-page pass reads it correctly.

    This is the whole fix for the infra matrix. The model's own recognition returned "on"
    and "e" for "One time"; the page's OCR had every amount right and only lacked structure.
    """
    lines, boxes = grid_page(INFRA_ROWS)
    rubbish = "<table><tr><td>on</td><td>e</td><td></td><td></td></tr></table>"
    table = table_ocr.build_table(rubbish, boxes, 20, 1, lines)

    grid = {(c.row, c.col): c.text for c in table.cells}
    assert grid[(1, 1)] == "INR 300000"
    assert grid[(2, 2)] == "INR 95000"
    assert grid[(3, 1)] == "INR 500000"


def test_every_rate_lands_against_its_own_option_and_column():
    """A rate in the wrong row is a rate for the wrong plan -- worse than no rate at all."""
    lines, boxes = grid_page(INFRA_ROWS)
    table = table_ocr.build_table("", boxes, 20, 1, lines)
    grid = {(c.row, c.col): c.text for c in table.cells}

    for row, option, setup, monthly in [(1, "Option B", "INR 300000", "INR 45000"),
                                        (2, "Option C", "INR 350000", "INR 95000"),
                                        (3, "Option D", "INR 500000", "To be Borne by Corp")]:
        assert grid[(row, 0)] == option
        assert grid[(row, 1)] == setup
        assert grid[(row, 2)] == monthly


def test_a_cell_with_no_price_comes_back_empty_rather_than_borrowing_one():
    """'NA' is the answer for Option B's VAPT. The 400000 beside it belongs to Option C."""
    lines, boxes = grid_page(INFRA_ROWS)
    table = table_ocr.build_table("", boxes, 20, 1, lines)
    grid = {(c.row, c.col): c.text for c in table.cells}
    assert grid[(1, 3)] == "NA"
    assert "400000" not in grid[(1, 3)]


def test_geometry_is_preferred_over_the_markup_when_both_are_available():
    """The markup is the fallback; it was being treated as the primary source."""
    lines, boxes = grid_page(INFRA_ROWS)
    wrong = "<table><tr><td>WRONG</td><td>WRONG</td></tr></table>"
    table = table_ocr.build_table(wrong, boxes, 20, 1, lines)
    assert "WRONG" not in " ".join(c.text for c in table.cells)


def test_without_page_text_the_markup_is_still_used():
    """A table on a page whose OCR produced nothing must not silently vanish."""
    _, boxes = grid_page(INFRA_ROWS)
    table = table_ocr.build_table(SCHEDULE_II, boxes, 19, 1, None)
    assert table is not None
    assert table.cells[0].text == "Header"


# -- deciding where to spend three minutes ----------------------------------------------------

def test_a_table_of_charges_is_worth_looking_at(config):
    """Schedule II: short lines, and money everywhere."""
    assert layout.looks_tabular(page_of([
        "Header", "Billing Frequency", "Charges", "Free period/Limit",
        "Product Set-Up Charges", "One-time", "NIL", "NA",
        "INR 100 per template beyond", "Minimum 85,000 per month",
        "INR 12 Per sign over and above", "INR 4000 per month for every",
    ]), config)


def test_a_page_of_prose_is_not(config):
    """Contract clauses mention charges and are not tables. Line length is what tells them apart."""
    clause = ("The Service Provider shall raise invoice for the fees and charges as detailed "
              "under Schedule II hereunder on a monthly basis based on the mutual "
              "confirmation between the Parties on the workings, and any rate applied shall "
              "be INR as agreed.")
    assert not layout.looks_tabular(page_of([clause] * 6), config)


def test_a_signature_block_is_not(config):
    """Short lines, but nothing about money -- this is where the money test earns its place."""
    assert not layout.looks_tabular(page_of([
        "For and on behalf of", "Transbnk Solutions Pvt Ltd", "Name : Vaibhav Tambe",
        "Designation : Director", "Signature:", "Authorised Signatory",
        "Aditya Birla Finance Limited", "Divya Aanand", "Business Partnership Manager",
    ]), config)


def test_a_scope_of_work_list_is_not(config):
    """Schedule I: bulleted, short-lined, and entirely free of prices."""
    assert not layout.looks_tabular(page_of([
        "SCHEDULE I", "SCOPE OF WORK AND RESPONSIBILITIES:", "Docuflow",
        "Template Creation and Maintenance", "New Template Creation in Trusthub",
        "Agreement Execution/Document Execution", "RiskRadar", "Onboarding of Accounts",
    ]), config)


def test_an_empty_page_is_not(config):
    assert not layout.looks_tabular(page_of([]), config)


# -- finding the commercial section by what the document calls it ------------------------------

@pytest.mark.parametrize("heading", [
    "SCHEDULE II FEES AND CHARGES",                 # Aditya Birla, page 19
    "Annexure II Commercial Arrangement",           # Ninjamonk, page 16
    "ANNEXURE - B Commercial Arrangement",          # J S Global, page 15
    "Annexure B Commercial Arrangement",            # AAR8, page 15
    "Schedule of Charges: -",                       # J S Global, page 18
    "Commercials for All Products",                 # Ninjamonk, page 23
    "Pricing Table-1",                              # J S Global, page 18
    "Product Offering:",                            # Ninjamonk, page 18
    "Other T & C",                                  # every one of them
    "Other Commercial Terms:",                      # J S Global, page 18
])
def test_the_document_s_own_heading_marks_its_commercial_section(heading):
    """These four MSAs name the section outright, in a small repeating vocabulary."""
    assert layout.heads_commercial_section(page_of([heading, "some text below it"]))


@pytest.mark.parametrize("heading", [
    "SCHEDULE I SCOPE OF WORK AND RESPONSIBILITIES",   # the scope schedule, not the fees one
    "ANNEXURE - A Services",
    "13 TERM AND TERMINATION",
    "CONFIDENTIALITY AND NON-DISCLOSURE",
    "ANNEXURE 1 EXHIBIT Services",
    "D. PAYMENT OF FEES AND CHARGES",                  # clause D, fourteen pages from the schedule
])
def test_an_unrelated_schedule_is_not_a_commercial_section(heading):
    assert not layout.heads_commercial_section(page_of([heading, "some text below it"]))


def test_a_cross_reference_in_a_sentence_is_not_a_heading():
    """Clause D.2 names Schedule II in passing; it is not Schedule II.

    This cost 211 seconds of table recognition on a page of solid prose, and pulled the
    page after it along too. A heading sits on a line of its own; a mention sits in a
    sentence.
    """
    sentence = ("The Service Provider shall raise invoice for the fees and charges as "
                "detailed under Schedule II hereunder on a monthly basis based on the "
                "mutual confirmation between the Parties on the workings.")
    assert not layout.heads_commercial_section(page_of([sentence]))


def test_a_heading_must_fit_on_a_heading_sized_line():
    """A heading is short. Anything longer is a sentence that happens to name one."""
    assert layout.heads_commercial_section(page_of(["SCHEDULE II FEES AND CHARGES"]))
    assert not layout.heads_commercial_section(page_of([
        "This appendix to the annexure B of the prior agreement dated 1 July 2024 "
        "supersedes nothing and is included for reference only."]))


def test_the_page_after_a_heading_is_included():
    """A schedule of charges overflows, and the overflow has no heading of its own.

    Aditya Birla's infra matrix is page 20 under a Schedule II beginning on 19; Ninjamonk's
    table runs from page 23 onto 24. Stopping at the heading page loses half the charges.
    """
    pages = [page_of([f"page {n} body text"], number=n) for n in range(17, 22)]
    pages[2] = page_of(["SCHEDULE II FEES AND CHARGES", "Header", "Charges"], number=19)
    assert layout.commercial_pages(pages) == {19, 20}


def test_a_heading_on_the_last_page_does_not_invent_a_page_after_it():
    pages = [page_of([f"body {n}"], number=n) for n in range(14, 16)]
    pages[-1] = page_of(["Annexure B Commercial Arrangement", "Development cost"], number=15)
    assert layout.commercial_pages(pages) == {15}


def test_two_commercial_sections_are_both_found():
    """J S Global prints its pricing twice: Annexure B on p15 and Schedule of Charges on p18."""
    pages = [page_of([f"body {n}"], number=n) for n in range(15, 20)]
    pages[0] = page_of(["ANNEXURE - B Commercial Arrangement", "Penny-Less"], number=15)
    pages[3] = page_of(["Schedule of Charges: -", "Pricing Table-1"], number=18)
    assert layout.commercial_pages(pages) == {15, 16, 18, 19}


def test_a_document_with_no_commercial_heading_yields_nothing():
    pages = [page_of(["CONFIDENTIALITY", "clause text"], number=n) for n in range(1, 4)]
    assert layout.commercial_pages(pages) == set()


def test_the_whole_thing_can_be_switched_off(config):
    rich = page_of(["Charges", "INR 100 per month", "Rate", "INR 4000", "Fees"])
    assert layout.looks_tabular(rich, config)
    object.__setattr__(config.ocr, "table_recognition_on_scans", False)
    assert not layout.looks_tabular(rich, config)


# -- the wiring --------------------------------------------------------------------------------

def test_structure_recognition_passes_the_argument_that_makes_it_run():
    """`FLAGS_use_mkldnn=0` is not enough for this pipeline; the constructor argument is.

    Without it every call dies with "ConvertPirAttribute2RuntimeAttribute not support" --
    the same oneDNN failure this project already fought once, in a pipeline that ignores the
    environment variable the first fix used.
    """
    source = (HERE / "src" / "table_ocr.py").read_text(encoding="utf-8")
    assert "enable_mkldnn=False" in source


def test_a_missing_table_model_does_not_lose_the_page_s_text(config, monkeypatch):
    """Text is the thing that must never be lost to a structure model's absence."""
    page = page_of(["Charges", "INR 100 per month", "Rate", "INR 4000", "Fees"])
    monkeypatch.setattr(layout, "_add_inferred_table", lambda p: None)

    def explode(*args, **kwargs):
        raise table_ocr.TableOcrUnavailable("paddlex[ocr] is not installed")

    monkeypatch.setattr(table_ocr, "read_tables", explode)
    layout._add_scan_tables(page, b"", config)
    assert page.tables == []
    assert len(page.lines) == 5


def test_an_existing_table_is_never_replaced(config, monkeypatch):
    """A ruled table read from a text layer is better evidence than a model's guess."""
    page = page_of(["Charges", "INR 100", "Rate", "INR 4000", "Fees"])
    page.tables = ["the real one"]
    monkeypatch.setattr(table_ocr, "read_tables",
                        lambda *a, **k: pytest.fail("should not have been called"))
    layout._add_scan_tables(page, b"", config)
    assert page.tables == ["the real one"]


def test_a_recovered_table_survives_the_cache_round_trip():
    table = table_ocr.build_table(SCHEDULE_II, None, 19, 1)
    again = layout._table_from_dict(layout._table_to_dict(table))
    assert again.n_rows == table.n_rows and again.n_cols == table.n_cols
    assert [c.text for c in again.cells] == [c.text for c in table.cells]
    assert [(c.row, c.col, c.row_span, c.col_span) for c in again.cells] == \
           [(c.row, c.col, c.row_span, c.col_span) for c in table.cells]
