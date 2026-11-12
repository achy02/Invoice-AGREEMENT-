"""The MSA golden set has to stay the thing the evaluator assumes it is.

These are cheap structural checks, not a re-derivation: the hand-extraction they come from
is an executed-contract derivative and does not live in the repo, so the file itself is the
closest thing to a source available here. What they catch is the golden set quietly
changing shape underneath a score -- a rebuild that drops a client, loses the pixel boxes,
or re-introduces the commercial-terms rows that made the denominator 101.

A score is only comparable across runs if its denominator is. Skipped where the golden set
is absent, because it is deliberately not committed.
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

GOLDEN = HERE / "tests" / "golden_msa" / "commercials.json"
pytestmark = pytest.mark.skipif(not GOLDEN.is_file(),
                                reason="golden_msa is not committed; see its README")


@pytest.fixture(scope="module")
def truth():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_denominator_is_eighty_two(truth):
    """Changing this changes every accuracy number this project has ever reported."""
    assert len(truth["charges"]) == 82


def test_every_client_is_present_and_none_is_empty(truth):
    counts = {code: 0 for code in truth["files"]}
    for charge in truth["charges"]:
        counts[charge["c"]] = counts.get(charge["c"], 0) + 1
    assert counts == {"AAR8": 8, "ABFL": 30, "JSGF": 20, "NINJ": 24}


def test_record_ids_are_unique(truth):
    ids = [c["id"] for c in truth["charges"]]
    assert len(set(ids)) == len(ids)


def test_every_row_carries_a_pixel_box(truth):
    """Without the boxes the harness can score values but not *grounding*."""
    boxless = [c["id"] for c in truth["charges"] if not c.get("bbox")]
    assert boxless == []


def test_boxes_are_ordered_and_non_empty(truth):
    for charge in truth["charges"]:
        x0, y0, x1, y1 = charge["bbox"]
        assert x1 > x0 and y1 > y0, f"{charge['id']} has an inside-out box"


def test_a_missing_amount_is_null_rather_than_zero(truth):
    """`null` means the document prints no number; 0 means it prints a zero charge.

    Collapsing the two would make "Waived off" and "NIL - 0" the same answer, and the
    evaluator's `invented` check -- which exists to catch a model inventing a rate for a
    line that has none -- would stop working.
    """
    unpriced = [c for c in truth["charges"] if c["amount"] is None]
    assert len(unpriced) == 26
    assert any(c["amount"] == 0 for c in truth["charges"]), "a real zero must still be 0"


def test_the_file_says_what_it_supersedes(truth):
    """A rebuilt golden set with no note is indistinguishable from a corrupted one."""
    assert "Transbnk" in truth["_supersedes"]
    assert "TBX_MSA_Extraction.md" in truth["_source"]
