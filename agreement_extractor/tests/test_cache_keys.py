"""A cached stage must be keyed by everything that shaped it.

Both failures here were found the same way: a fix was written, tested, shipped -- and then
changed nothing at all on the real document, because the stage it fixed was served whole
from a cache built before the fix existed. That is the worst kind of cache bug. Nothing
errors, the run gets faster, and the output is quietly stale.

    S2  turning on table recognition did not invalidate the page cache, so the new code
        never ran and the pages came back exactly as they were.
    S4  the extraction cache was keyed on the PDF's hash, so even once S2 did recover the
        table, extraction was served the answer it had given before the table existed.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import config as config_module  # noqa: E402
from src import pipeline  # noqa: E402
from src.models import Document  # noqa: E402


@pytest.fixture
def config():
    return config_module.load(HERE / "config.yaml")


def document(sha="a" * 64):
    return Document(file_name="x.pdf", path=Path("x.pdf"), sha256=sha, page_count=1)


# -- S2: the page cache ----------------------------------------------------------------------

def fingerprint(cache):
    """The settings blob a StageCache mixes into its key, whatever it is called."""
    for name in ("fingerprint", "settings", "_fingerprint", "_settings"):
        if hasattr(cache, name):
            return getattr(cache, name)
    raise AssertionError(f"no fingerprint on {type(cache).__name__}")


def test_turning_table_recognition_on_invalidates_the_page_cache(config):
    """Without this, switching the setting on changes nothing and says nothing."""
    before = fingerprint(pipeline.page_cache(config))
    object.__setattr__(config.ocr, "table_recognition_on_scans", False)
    assert fingerprint(pipeline.page_cache(config)) != before


def test_retuning_the_table_gate_invalidates_the_page_cache(config):
    """The thresholds decide which pages get a table at all, so they shape S2's output."""
    before = fingerprint(pipeline.page_cache(config))
    object.__setattr__(config.ocr, "table_page_max_mean_line_chars", 999)
    after = fingerprint(pipeline.page_cache(config))
    assert after != before

    object.__setattr__(config.ocr, "table_page_min_money_mentions", 99)
    assert fingerprint(pipeline.page_cache(config)) != after


def test_fixing_the_table_parser_invalidates_every_cache_that_holds_a_table(config):
    """Code is an input too, and this one had to be learned twice.

    `table_ocr.py` once built tables in which every cell held the whole page. Repairing it
    changed nothing on the document it was repaired for, because the finished pages came
    from a cache that had no idea the parser was different. A settings-only fingerprint
    cannot see a code change, so the code is hashed into it.
    """
    from src.table_ocr import builder_digest

    digest = builder_digest()
    assert fingerprint(pipeline.page_cache(config)).get("builder") == digest
    assert fingerprint(pipeline.table_cache(config)).get("builder") == digest


def test_the_table_digest_actually_tracks_the_file(tmp_path, monkeypatch):
    """A constant that someone must remember to bump is not a digest."""
    from src import table_ocr

    before = table_ocr.builder_digest()
    source = Path(table_ocr.__file__)
    original = source.read_bytes()
    try:
        source.write_bytes(original + b"\n# a change to the parser\n")
        assert table_ocr.builder_digest() != before
    finally:
        source.write_bytes(original)
    assert table_ocr.builder_digest() == before


def test_table_structure_is_cached_apart_from_the_text(config):
    """Different costs, different reasons to go stale: ~3 min a page against ~50 s.

    Together in one entry, every parser fix would throw away the text as well and re-run
    the cheap thing to repair the expensive one.
    """
    assert pipeline.table_cache(config).__dict__ != pipeline.ocr_cache(config).__dict__
    assert "builder" not in fingerprint(pipeline.ocr_cache(config))


def test_an_unrelated_setting_does_not_throw_the_page_cache_away(config):
    """Over-keying is its own cost: 50 s a page to re-read what had not changed."""
    before = fingerprint(pipeline.page_cache(config))
    object.__setattr__(config.llm, "temperature", 0.7)
    assert fingerprint(pipeline.page_cache(config)) == before


# -- S4: the extraction cache ----------------------------------------------------------------

def test_the_same_file_read_better_is_a_different_extraction(config):
    """The MSA's bytes never changed; what we could read off them did.

    Keyed on the file alone, the recovered Schedule II table would have been handed to a
    cache that answered with the extraction from before the table was recoverable.
    """
    doc = document()
    flat = "Risk Radar\nMonthly\nMinimum 85,000 per month\nBank Balance"
    gridded = "| Header | Billing Frequency | Charges |\n| Risk Radar | Monthly | 85,000 |"
    assert pipeline.extract_key(doc, flat) != pipeline.extract_key(doc, gridded)


def test_the_same_file_read_the_same_way_hits_the_cache(config):
    """The cache still has to work, or every re-run pays for the model again."""
    doc = document()
    body = "| Header | Charges |\n| Storage | INR 100 |"
    assert pipeline.extract_key(doc, body) == pipeline.extract_key(doc, body)


def test_two_different_files_with_identical_text_stay_separate():
    """Two agreements can render alike; their records must not be confused for each other."""
    body = "| Header | Charges |"
    assert pipeline.extract_key(document("a" * 64), body) != \
           pipeline.extract_key(document("b" * 64), body)


def test_editing_a_prompt_still_invalidates_the_extraction(config):
    """The protection that was already there, kept honest alongside the new one."""
    assert "prompts" in fingerprint(pipeline.extract_cache(config))
