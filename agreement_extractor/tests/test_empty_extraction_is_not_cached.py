"""An extraction that found nothing must never be cached.

Found on a real run. The Ninjamonk MSA went through both calls, spent twelve minutes of
compute, and returned zero fields. The pipeline wrote `"payload": []` to
`cache/extract/25c27b6d....json` and moved on -- so every later run served that empty list
from disk in milliseconds, never called the model, and logged `CACHED, 0 field(s)` as
though it had re-checked and agreed.

That is the worst shape a cache bug can take: the failure became permanent *and* silent,
and it would have survived every fix made afterwards, including the ones written to repair
it. An empty result is the one answer always worth paying to recompute.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import config as config_module          # noqa: E402
from src import pipeline                         # noqa: E402
from src.models import Document, Line, Page, Token  # noqa: E402


@pytest.fixture
def config(tmp_path):
    """The real config, pointed at a throwaway cache so the run's own cache is untouched."""
    config = config_module.load(HERE / "config.yaml")
    object.__setattr__(config.paths, "cache", tmp_path / "cache")   # Paths is frozen
    return config


def readable_document():
    """A document S2 read successfully -- so nothing downstream can blame the pages."""
    line = Line(line_id="p1:L001", text="Client: Ninjamonk Proptech Private Limited",
                bbox=(0, 0, 100, 10), source="text_layer",
                tokens=[Token("Ninjamonk", (0, 0, 50, 10))])
    page = Page(number=1, width=1000, height=1400, dpi=200, image_path="p1.png",
                source="text_layer", lines=[line])
    return Document(file_name="ninjamonk.pdf", path="ninjamonk.pdf", sha256="n" * 64,
                    page_count=1, pages=[page])


def entries(config):
    folder = Path(config.paths.cache) / "extract"
    return sorted(folder.glob("*.json")) if folder.exists() else []


def test_an_empty_extraction_is_not_written_to_the_cache(config, monkeypatch):
    document = readable_document()
    monkeypatch.setattr("src.extract.extract_document", lambda *a, **k: [])

    pipeline.run_extraction([document], config)

    assert entries(config) == [], (
        "zero fields from a readable document is a failed call, not an answer; caching it "
        "makes the failure permanent and hides it behind a CACHED log line")


def test_a_real_extraction_is_still_cached(config, monkeypatch):
    """The guard must not cost the cache its job on the documents that do work."""
    from src.models import Extracted

    def one_field(*a, **k):
        item = Extracted(field="client_name", value="Ninjamonk Proptech Private Limited",
                         evidence_text="Ninjamonk", evidence_ids=["p1:L001"], page=1)
        item.sheet = "Summary"
        return [item]

    monkeypatch.setattr("src.extract.extract_document", one_field)

    pipeline.run_extraction([readable_document()], config)

    assert len(entries(config)) == 1


def test_the_retry_actually_happens_on_the_next_run(config, monkeypatch):
    """Not caching is only worth anything if the second run calls the model again.

    This is the whole point: a transient failure -- a window too small, a server that
    stalled -- must get another chance rather than being frozen into the cache.
    """
    calls = []

    def counted(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr("src.extract.extract_document", counted)

    pipeline.run_extraction([readable_document()], config)
    pipeline.run_extraction([readable_document()], config)

    assert len(calls) == 2, "the second run inherited the empty result instead of retrying"
