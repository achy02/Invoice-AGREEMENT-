"""S0 inventory, S1 render, S2 text/layout."""
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config as config_module  # noqa: E402
from src import golden as G  # noqa: E402
from src import inventory, layout, render, text_layer  # noqa: E402
from src.cache import StageCache  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
has_golden = (GOLDEN / "source_map_annotations.json").is_file()


@pytest.fixture
def config(tmp_path):
    cfg = config_module.load(ROOT / "config.yaml")
    object.__setattr__(cfg.paths, "cache", tmp_path / "cache")
    object.__setattr__(cfg.paths, "page_images", tmp_path / "pages")
    object.__setattr__(cfg.paths, "output", tmp_path / "out")
    return cfg


def make_pdf(path, text="Agreement text. " * 40, pages=1):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 560, 780), text, fontsize=10)
    doc.save(path)
    doc.close()
    return path


def make_scan(path, pages=1):
    """An image-only PDF: no text layer at all."""
    source = make_pdf(path.with_name("src.pdf"), pages=pages)
    out = pymupdf.open()
    with pymupdf.open(source) as doc:
        for page in doc:
            pixmap = page.get_pixmap(dpi=100)
            out.new_page(width=page.rect.width, height=page.rect.height).insert_image(
                pymupdf.Rect(0, 0, page.rect.width, page.rect.height), pixmap=pixmap)
    out.save(path)
    out.close()
    return path


# -- S0 ------------------------------------------------------------------------------------

def test_inventory_hashes_counts_and_detects_the_text_layer(tmp_path, config):
    make_pdf(tmp_path / "a.pdf", pages=2)
    docs = inventory.inventory(inventory.discover(tmp_path), config)
    doc = next(d for d in docs if d.file_name == "a.pdf")
    assert doc.page_count == 2 and len(doc.sha256) == 64
    assert doc.digital_pages == [True, True]


def test_a_scan_is_reported_as_needing_ocr(tmp_path, config):
    make_scan(tmp_path / "s.pdf", pages=2)
    docs = inventory.inventory([tmp_path / "s.pdf"], config)
    assert docs[0].digital_pages == [False, False]
    assert any("need OCR" in n for n in docs[0].notes)


def test_the_same_content_under_two_names_is_marked_duplicate(tmp_path, config):
    make_pdf(tmp_path / "a.pdf")
    (tmp_path / "b.pdf").write_bytes((tmp_path / "a.pdf").read_bytes())
    docs = inventory.inventory(inventory.discover(tmp_path), config)
    assert docs[0].duplicate_of is None
    assert docs[1].duplicate_of == "a.pdf"


def test_a_corrupt_pdf_is_recorded_rather_than_raising(tmp_path, config):
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.4 not really")
    docs = inventory.inventory([tmp_path / "bad.pdf"], config)
    assert docs[0].page_count == 0 and docs[0].notes


@pytest.mark.parametrize("text,expected", [
    ("a" * 200, True),
    ("a" * 199, False),
    ("", False),
    ("a" * 200 + "�" * 10, True),     # a few bad glyphs are tolerated
    ("a" * 200 + "�" * 40, False),    # a wall of them is not
])
def test_text_layer_gate(text, expected, config):
    assert inventory.is_digital(text, config) is expected


# -- S1 ------------------------------------------------------------------------------------

def test_render_produces_the_size_page_size_predicts(tmp_path, config):
    pdf = make_pdf(tmp_path / "a.pdf")
    results = render.render_document(pdf, "sha", config)
    path, width, height = results[0]
    assert path.is_file()
    assert (width, height) == render.page_size(pdf, 1, config.render.dpi)


def test_an_already_rendered_page_is_not_redrawn(tmp_path, config):
    pdf = make_pdf(tmp_path / "a.pdf")
    first = render.render_document(pdf, "sha", config)[0][0]
    stamp = first.stat().st_mtime_ns
    render.render_document(pdf, "sha", config)
    assert first.stat().st_mtime_ns == stamp


def test_scale_is_points_to_pixels():
    assert render.scale_for(72) == 1.0
    assert render.scale_for(200) == pytest.approx(200 / 72)


# -- S2 ------------------------------------------------------------------------------------

def test_lines_carry_citable_ids_and_word_tokens(tmp_path, config):
    pdf = make_pdf(tmp_path / "a.pdf", text="Payment term to be 30 days from Invoice issuance.")
    lines = text_layer.extract_lines(pdf, 1, render.scale_for(config.render.dpi))
    assert lines and lines[0].line_id == "p1:L001"
    assert lines[0].source == "text_layer"
    words = [t.text for t in lines[0].tokens]
    assert "Payment" in words and "30" in words
    for token in lines[0].tokens:
        assert token.bbox[0] < token.bbox[2] and token.bbox[1] < token.bbox[3]


def test_tokens_are_in_page_image_pixels_not_points(tmp_path, config):
    """A box in points would place every value at roughly a third of its true position."""
    pdf = make_pdf(tmp_path / "a.pdf")
    lines = text_layer.extract_lines(pdf, 1, render.scale_for(config.render.dpi))
    _, width, height = render.render_document(pdf, "sha", config)[0]
    for line in lines:
        assert 0 <= line.bbox[0] and line.bbox[2] <= width + 1
        assert 0 <= line.bbox[1] and line.bbox[3] <= height + 1
    assert max(line.bbox[2] for line in lines) > width * 0.3


@pytest.mark.skipif(not has_golden, reason="golden set not present")
def test_spaces_are_reinserted_where_the_pdf_has_no_space_glyphs():
    """ALSD's text layer has no spaces: "ALSDTechnologiesPvtLtd".

    The golden set records that under `raw`. Grounding a quote like "ALSD Technologies Pvt
    Ltd" is impossible unless word breaks are recovered from geometry, so they are.
    """
    gold = G.load(GOLDEN)
    record = next(r for r in gold.records if r.id == "AL-S01")
    assert record.raw_text == "ALSDTechnologiesPvtLtd", "premise changed"

    cfg = config_module.load(ROOT / "config.yaml")
    lines = text_layer.extract_lines(gold.pdf_for(record), 1, render.scale_for(cfg.render.dpi))
    joined = " ".join(line.text for line in lines)
    assert "ALSD Technologies Pvt Ltd" in joined


def test_a_scanned_page_without_ocr_is_flagged_not_guessed_at(tmp_path, config, monkeypatch):
    pdf = make_scan(tmp_path / "s.pdf")
    image, width, height = render.render_document(pdf, "sha", config)[0]
    monkeypatch.setattr(layout, "read_page",
                        lambda *a, **k: (_ for _ in ()).throw(layout.OcrUnavailable("no paddle")))
    page = layout.build_page(pdf, 1, image, width, height, config, digital=False)
    assert page.source == "none" and page.lines == []
    assert "OCR unavailable" in page.note


def test_a_failing_seal_pass_does_not_discard_the_page_text(tmp_path, config, monkeypatch):
    """Seals are extra evidence; losing a page's text because a stamp failed would be absurd."""
    from src.models import Line, Token

    pdf = make_scan(tmp_path / "s.pdf")
    image, width, height = render.render_document(pdf, "sha", config)[0]
    monkeypatch.setattr(layout, "read_page", lambda data, n, cfg: [
        Line("", "Rs 2.5", (1.0, 2.0, 3.0, 4.0), "paddle_ocr", 0.99,
             [Token("Rs 2.5", (1.0, 2.0, 3.0, 4.0), 0.99)])])
    monkeypatch.setattr(layout, "read_seals",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("dependency error")))
    page = layout.build_page(pdf, 1, image, width, height, config, digital=False)
    assert page.source == "paddle_ocr" and len(page.lines) == 1
    assert "Seal recognition unavailable" in page.note


def test_ocr_is_cached_by_page_image(tmp_path, config, monkeypatch):
    from src.models import Line, Token

    pdf = make_scan(tmp_path / "s.pdf")
    image, width, height = render.render_document(pdf, "sha", config)[0]
    calls = []
    monkeypatch.setattr(layout, "read_page", lambda data, n, cfg: calls.append(n) or [
        Line("", "x", (0.0, 0.0, 1.0, 1.0), "paddle_ocr", 0.9,
             [Token("x", (0.0, 0.0, 1.0, 1.0), 0.9)])])
    monkeypatch.setattr(layout, "read_seals", lambda *a, **k: [])
    cache = StageCache(tmp_path / "c", "ocr", {"dpi": config.render.dpi})

    for _ in range(2):
        page = layout.build_page(pdf, 1, image, width, height, config, False, cache)
    assert len(calls) == 1, "the second read must come from the cache"
    assert page.source == "paddle_ocr"


def test_low_ocr_confidence_is_noted(tmp_path, config, monkeypatch):
    from src.models import Line, Token

    pdf = make_scan(tmp_path / "s.pdf")
    image, width, height = render.render_document(pdf, "sha", config)[0]
    monkeypatch.setattr(layout, "read_page", lambda data, n, cfg: [
        Line("", "x", (0.0, 0.0, 1.0, 1.0), "paddle_ocr", 0.41,
             [Token("x", (0.0, 0.0, 1.0, 1.0), 0.41)])])
    monkeypatch.setattr(layout, "read_seals", lambda *a, **k: [])
    page = layout.build_page(pdf, 1, image, width, height, config, digital=False)
    assert "Low OCR confidence (41%)" in page.note


def test_an_envelope_id_is_collected_as_signature_evidence(tmp_path, config):
    pdf = make_pdf(tmp_path / "a.pdf",
                   text="DocuSign Envelope ID: 2C05A903-C641-4C43-AA2C-141790B3C685 " * 6)
    image, width, height = render.render_document(pdf, "sha", config)[0]
    page = layout.build_page(pdf, 1, image, width, height, config, digital=True)
    kinds = {e.kind for e in page.signatures}
    assert "envelope_id" in kinds


@pytest.mark.skipif(not has_golden, reason="golden set not present")
def test_a_ruled_commercial_table_keeps_its_rows_together(config):
    """Flattened to lines, the ALSD rate table emits the rate BEFORE its sub-product.

    A model reading that order has to guess which rate belongs to which product, which is
    exactly how a rate lands on the wrong row. The grid removes the guess, and the cell text
    must read as printed so an evidence quote can ground against it.
    """
    from src import render, text_layer

    gold = G.load(GOLDEN)
    record = next(r for r in gold.records if r.id == "AL-C01")
    pdf = gold.pdf_for(record)
    scale = render.scale_for(config.render.dpi)
    tokens = [t for line in text_layer.extract_lines(pdf, 3, scale) for t in line.tokens]
    tables = text_layer.extract_tables(pdf, 3, scale, tokens)

    assert len(tables) == 1
    table = tables[0]
    assert (table.n_rows, table.n_cols) == (5, 4)

    cells = {c.cell_id: c.text for c in table.cells}
    assert cells["p3:T1:R1C2"] == "Penny-Drop (IMPS)"
    # Character-for-character what the golden set records as this rate's evidence.
    assert cells["p3:T1:R1C3"] == record.value_text
    assert "INR 0.65/- Per Transaction" == cells["p3:T1:R3C3"]


def test_a_folder_runs_end_to_end_with_caching(tmp_path, config):
    from src import pipeline

    for name in ("a.pdf", "b.pdf"):
        make_pdf(tmp_path / name, pages=2)
    docs = pipeline.run(config, root=tmp_path)
    assert len(docs) == 2
    assert all(len(d.pages) == 2 for d in docs)
    assert all(p.source == "text_layer" for d in docs for p in d.pages)

    from src.logging_setup import RunLog

    again = RunLog()
    pipeline.run(config, root=tmp_path, run_log=again)
    assert all(r.status == "CACHED" for r in again.records if r.stage == "S2")


def test_parallel_workers_produce_the_same_pages_as_one(tmp_path, config):
    """The 1000-document run needs workers; they must not change the answer."""
    from src import pipeline

    for index in range(3):
        make_pdf(tmp_path / f"doc{index}.pdf", text=f"Document {index} content. " * 30)

    object.__setattr__(config.run, "ocr_workers", 1)
    serial = pipeline.run(config, root=tmp_path, resume=False)
    serial_text = {d.file_name: [p.text for p in d.pages] for d in serial}

    object.__setattr__(config.run, "ocr_workers", 2)
    parallel = pipeline.run(config, root=tmp_path, resume=False)
    parallel_text = {d.file_name: [p.text for p in d.pages] for d in parallel}

    assert parallel_text == serial_text


def test_one_corrupt_file_does_not_stop_the_others(tmp_path, config):
    """Per-file isolation, the rule that lets a 1000-document run finish."""
    from src import pipeline
    from src.logging_setup import RunLog

    make_pdf(tmp_path / "good.pdf")
    (tmp_path / "bad.pdf").write_bytes(b"%PDF-1.4 broken")
    run_log = RunLog()
    docs = pipeline.run(config, root=tmp_path, run_log=run_log)
    good = next(d for d in docs if d.file_name == "good.pdf")
    assert good.pages and good.pages[0].source == "text_layer"
    assert any(r.status == "FAILED" for r in run_log.records)


def test_ocr_output_shapes_from_both_paddleocr_generations():
    from src.ocr import flatten

    quad = [[10, 20], [60, 20], [60, 40], [10, 40]]
    v2 = [[[quad, ("INR 0.35", 0.98)]]]
    v3 = [{"rec_texts": ["INR 0.35"], "rec_scores": [0.98], "rec_polys": [quad]}]
    assert flatten(v2) == [("INR 0.35", 0.98, (10.0, 20.0, 60.0, 40.0))]
    assert flatten(v3) == flatten(v2)
    assert flatten(None) == [] and flatten([]) == []
