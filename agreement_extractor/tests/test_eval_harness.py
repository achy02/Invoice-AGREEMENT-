"""The eval harness measured against itself.

A broken measurement is worse than no measurement: it reports a number that looks like
evidence. This harness once scored a run at 3% value accuracy and 0% charge recall against
output that had actually found most of the values, because it joined the model's schema keys
(`client_name`) to the golden set's human labels (`Client Name`), and joined charge lines on
hand-assigned golden record ids (`CN-C03`) that nothing in a document could tell a model to
reproduce. Both join bugs are silent -- they fail as "the pipeline found nothing", which is
indistinguishable from the pipeline genuinely finding nothing.

These tests exist so the next such mismatch fails here, loudly, instead of in a report.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as config_module  # noqa: E402
from src import export, golden as G  # noqa: E402

harness = importlib.import_module("scripts.eval")


@pytest.fixture(scope="module")
def gold():
    return G.load(config_module.load(ROOT / "config.yaml").paths.golden)


def test_every_golden_summary_label_is_accounted_for(gold):
    """The test that would have caught the 3%.

    Every Summary label in the golden set must be one the pipeline produces, one S6 grades,
    or one explicitly recorded as not-a-value. A label in none of those groups is silently
    unscoreable, and shows up in a report as an extraction failure.
    """
    produced = set(export.SUMMARY_FIELDS.values())
    graded = set(harness.GRADED_NOT_EXTRACTED)
    unscoreable = {
        record.field for record in gold.records
        if record.sheet != "Commercials"
        and record.field not in produced
        and record.field not in graded
        and record.field not in harness.NOT_A_VALUE
    }
    assert not unscoreable, (
        f"golden Summary label(s) {sorted(unscoreable)} are compared against nothing. "
        "Either the pipeline should emit them, or they belong in GRADED_NOT_EXTRACTED "
        "or NOT_A_VALUE with a reason.")


def test_every_summary_field_reaches_a_workbook_column():
    """A field the model answers that no column shows is work thrown away on every run."""
    missing = set(export.SUMMARY_FIELDS.values()) - set(export.SUMMARY_COLUMNS)
    assert not missing, f"{sorted(missing)} map to no Summary column"


def test_the_prompt_asks_for_exactly_the_fields_the_exporter_expects():
    """The prompt, the exporter and the golden labels are three vocabularies for one schema."""
    from src import extract

    prompt = extract.load_prompt("call_a_parties_terms.md")
    for field in export.SUMMARY_FIELDS:
        assert f"`{field}`" in prompt, f"{field} is exported but never asked for"


def test_summary_rows_are_matched_by_label_not_by_schema_key():
    rows = [{"file": "Signed_ALSD Technologies Pvt Ltd - Offer Letter_04-01-2025.pdf",
             "field": "client_name", "value": "ALSD Technologies Pvt Ltd",
             "sheet": "Summary"}]
    index = harness._summary_index(rows)
    key = (G.normalise_name("Signed_ALSD Technologies Pvt Ltd - Offer Letter_04-01-2025.pdf"),
           "Client Name")
    assert index[key]["value"] == "ALSD Technologies Pvt Ltd"


def test_signed_status_is_scored_although_the_model_never_answers_it():
    rows = [{"file": "a.pdf", "field": "signed_status", "value": "SIGNED", "sheet": "Summary"}]
    index = harness._summary_index(rows)
    assert index[(G.normalise_name("a.pdf"), "Signed / Unsigned")]["value"] == "SIGNED"


@pytest.mark.parametrize("golden_field, produced_field", [
    ("E-sign - Rate", "E-sign - Rate"),
    ("E-sign - Rate", "E-Sign - Rate"),                      # case and punctuation
    ("Set-up and Integration charges - Rate", "Set-up and Integration charges - Rate"),
    ("E-sign - Rate", "Documentation - E-sign - Rate"),      # ours adds the product
])
def test_a_charge_line_is_matched_on_what_it_names(golden_field, produced_field):
    assert harness._charge_key(golden_field) in harness._charge_key(produced_field) \
        or harness._charge_key(produced_field) in harness._charge_key(golden_field)


def test_a_different_product_is_not_matched():
    assert harness._charge_key("E-sign - Rate") not in harness._charge_key("E-stamp - Rate")


def test_charge_recall_counts_the_line_and_its_rate_separately(gold):
    """Finding a charge line and reading its price correctly are different failures."""
    record = next(r for r in gold.commercials if not r.id.endswith(("S", "F")))
    rows = [{"sheet": "Commercials", "file": record.file, "field": record.field,
             "value": "999999", "normalized_value": 999999, "charge": {}}]
    found, correct, wanted, missing, wrong = harness.charge_recall(rows, gold)
    assert found == 1 and correct == 0, "a wrong rate must still count as a located line"
    assert wrong and wrong[0][0].id == record.id


def test_a_null_the_pipeline_never_emitted_is_not_credited_as_correct(gold):
    """Silence is not the same as answering "not present", and must not score as one."""
    record = next(r for r in gold.records if r.is_null)
    index = harness._summary_index([])
    assert index.get((G.normalise_name(record.file), record.field)) is None
