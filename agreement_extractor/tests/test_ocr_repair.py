"""OCR glyph repair: fix what is provably a misread, flag what is a guess.

Every case here is a real misread recorded in the golden set's Raw Text column, not an
invented one. The file names them so a regression says which document it broke.
"""
from __future__ import annotations

import pytest

from src.ocr_repair import Repair, repair_digest, repair_text


# -- repairs that must happen ---------------------------------------------------------------

def test_repairs_sok_to_50k():
    """AR-C06: AAR8 Annexure B reads "50K per integration/product"; OCR returned "SOK"."""
    text, repairs, _ = repair_text("Other Integrations - SOK per integration/product")

    assert "50K" in text
    assert "SOK" not in text
    assert [r.rule for r in repairs] == ["amount_glyph"]
    assert (repairs[0].before, repairs[0].after) == ("SOK", "50K")


def test_repairs_stray_prefix_on_stamp_certificate():
    """JS-S05: the certificate number came back with a leading hyphen."""
    text, repairs, _ = repair_text("-IN-DL52096994936271W")

    assert text == "IN-DL52096994936271W"
    assert [r.rule for r in repairs] == ["stamp_id_prefix"]


@pytest.mark.parametrize("raw, fixed", [
    ("INR 1O,000 per month", "10,000"),
    ("Rs. S00 per stamp", "500"),
    ("fee of 2 Lacs", None),          # already clean: nothing to do
])
def test_digit_glyphs_inside_money_lines(raw, fixed):
    text, repairs, _ = repair_text(raw)
    if fixed is None:
        assert repairs == []
        assert text == raw
    else:
        assert fixed in text


# -- repairs that must NOT happen -----------------------------------------------------------

def test_does_not_rewrite_prose_that_merely_looks_like_an_amount():
    """BOOK maps to 800K under pure glyph substitution. The word guard has to stop it."""
    text, repairs, _ = repair_text("Please BOOK the service as per the charge schedule")

    assert text == "Please BOOK the service as per the charge schedule"
    assert repairs == []


def test_does_not_touch_tokens_outside_a_money_context():
    """No currency, no rate, no fee: a bare "SOK" here is prose, not an amount."""
    text, repairs, _ = repair_text("The SOK department signed the annexure")

    assert repairs == []
    assert text == "The SOK department signed the annexure"


def test_leaves_unresolvable_characters_alone():
    """A stray non-confusable letter means the rest is a guess too, so nothing moves."""
    text, repairs, _ = repair_text("INR 12A5 per month")

    assert repairs == []
    assert "12A5" in text


def test_lost_handwriting_is_flagged_not_invented():
    """AB-S16: ABFL's handwritten 22-07-2024 came back as "22- O1- Dory".

    "Dory" -> "2024" is not a glyph confusion, it is an invention. The value must stay
    wrong and say so, because a wrong date that announces itself is recoverable and a
    wrong date that does not is not.
    """
    text, repairs, suspicions = repair_text("22- O1- Dory")

    assert "Dory" in text, "must not invent a year"
    assert repairs == []
    assert any("four-digit year" in s for s in suspicions)


def test_ambiguous_glyph_is_left_to_a_suspicion():
    """AB-S04: "JUN D024" for "JUN 2024". D collides with both 0 and 2, so it is unmapped."""
    text, repairs, _ = repair_text("27 JUN D024")

    assert "D024" in text
    assert repairs == []


def test_low_confidence_money_line_raises_a_suspicion_without_changing_text():
    text, repairs, suspicions = repair_text("INR 35 per stamp", conf=0.52)

    assert text == "INR 35 per stamp"
    assert repairs == []
    assert any("low OCR confidence" in s for s in suspicions)


# -- the receipt --------------------------------------------------------------------------

def test_every_repair_names_its_rule_and_both_sides():
    _, repairs, _ = repair_text("Other Integrations - SOK per integration/product")

    assert str(repairs[0]) == "amount_glyph: 'SOK' -> '50K'"


def test_clean_text_is_returned_untouched():
    line = "Development cost - 22 Lacs (10 lac Tranch 1 and Tranch 2 - 12 lac)"
    text, repairs, suspicions = repair_text(line)

    assert (text, repairs, suspicions) == (line, [], [])


# -- cache invalidation -------------------------------------------------------------------

def test_repair_digest_is_stable_and_short():
    assert repair_digest() == repair_digest()
    assert len(repair_digest()) == 16


def test_ocr_and_page_caches_carry_the_repair_digest():
    """A rule change must invalidate cached lines, or the fix silently does nothing --
    the failure the table parser and the heading vocabulary each hit once before."""
    from src import config as config_module
    from src.pipeline import ocr_cache, page_cache

    config = config_module.load()
    assert ocr_cache(config).fingerprint["repairs"] == repair_digest()
    assert page_cache(config).fingerprint["repairs"] == repair_digest()


def test_repaired_line_keeps_the_original_text():
    """`raw_text` is the evidence that something was changed; it is only set when it was."""
    from src.models import Line

    fixed, repairs, _ = repair_text("Other Integrations - SOK per integration/product")
    line = Line(line_id="p1:L001", text=fixed, bbox=(0, 0, 1, 1), source="paddle_ocr",
                raw_text="Other Integrations - SOK per integration/product",
                repairs=[str(r) for r in repairs])

    assert "SOK" in line.raw_text and "50K" in line.text
    assert line.repairs == ["amount_glyph: 'SOK' -> '50K'"]
