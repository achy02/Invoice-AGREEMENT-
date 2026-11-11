"""The golden set is the definition of correct, so it is itself verified.

These tests do not test the pipeline. They assert that the ground truth is internally
consistent and complete, because an accuracy figure measured against a broken reference is
worse than no figure at all.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import golden as G  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"

pytestmark = pytest.mark.skipif(
    not (GOLDEN / "source_map_annotations.json").is_file(),
    reason="golden set not present",
)


@pytest.fixture(scope="module")
def golden():
    return G.load(GOLDEN)


def test_it_is_internally_consistent(golden):
    """Every normalised box must equal its pixel box scaled by that page's recorded size.

    If this drifts, every bbox IoU the eval harness reports is measured against the wrong
    coordinate system and the number is meaningless.
    """
    problems = G.check(golden)
    assert problems == [], "\n".join(problems)


def test_it_has_the_111_records_the_readme_claims(golden):
    assert len(golden.records) == 111
    assert len(golden.summary) == 48
    assert len(golden.commercials) == 63


def test_the_58_charge_lines_are_complete(golden):
    """63 Commercials records cover 58 charge lines; 5 carry an extra column.

    Capitalneed's table states a standard rate and a free limit beside the client rate, so
    those rows contribute more than one extracted cell. Charge-line recall must be counted
    over row numbers, not over records, or the target of 58 is unreachable by construction.
    """
    rows = {r.id for r in golden.commercials}
    extra = {i for i in rows if i.endswith(("S", "F"))}
    assert len(rows) - len(extra) == 58
    assert extra == {"CN-C01S", "CN-C02S", "CN-C03S", "CN-C05S", "CN-C05F"}

    import json
    raw = json.loads((GOLDEN / "source_map_annotations.json").read_text(encoding="utf-8"))
    row_numbers = {r["row"] for r in raw
                   if r["sheet"] == "Commercials" and isinstance(r["row"], int)}
    assert row_numbers == set(range(1, 59)), "charge-line row numbers must be 1..58 with no gaps"


def test_the_four_documents_resolve_despite_the_naming_mismatch(golden):
    """Annotations spell file names with underscores; the PDFs on disk use spaces."""
    assert len(golden.clients) == 4
    for record in golden.records:
        assert golden.pdf_for(record) is not None, record.file
    assert any(" " in p.name for p in golden.pdfs.values())


def test_null_examples_are_kept_and_carry_no_location(golden):
    """Stamp Date and Effective Date are absent on purpose.

    They are the examples that teach the pipeline to return null rather than substitute the
    letter date, which is the single easiest mistake to make on these documents.
    """
    assert len(golden.nulls) == 10
    by_field = {}
    for record in golden.nulls:
        by_field[record.field] = by_field.get(record.field, 0) + 1
    assert by_field == {"Stamp Date": 4, "Effective Date": 4,
                        "Billing Model": 1, "Payment Terms": 1}
    for record in golden.nulls:
        assert record.page is None and record.value_bbox is None and record.value_text == ""


def test_every_located_record_points_at_a_page_image_that_exists(golden):
    located = [r for r in golden.records if not r.is_null]
    assert len(located) == 101
    for record in located:
        assert golden.image_for(record) is not None, f"{record.id}: {record.image}"


def test_the_scanned_document_is_the_one_that_needed_ocr(golden):
    """Bajaj is a scan with no text layer; its 26 records came from OCR, not the text layer.

    This is the subset that decides whether PaddleOCR is good enough to replace the Tesseract
    pass that produced the reference.
    """
    ocr = [r for r in golden.records if r.method.startswith("Tesseract")]
    assert len(ocr) == 26
    assert {r.client for r in ocr} == {"Bajaj Finance Limited", "ALSD Technologies Pvt Ltd"}
    # ALSD contributes exactly one: a Director title readable only inside a stamp image.
    alsd = [r for r in ocr if r.client == "ALSD Technologies Pvt Ltd"]
    assert len(alsd) == 1 and alsd[0].value == "Director (name not printed)"


def test_the_paddleocr_label_files_cover_the_same_pages(golden):
    """Both label files are keyed by page image, and must name images that exist."""
    for name in ("paddleocr_Label.txt", "paddleocr_kie_xfund_style.txt"):
        path = GOLDEN / name
        assert path.is_file(), name
        pages = [line.split("\t", 1)[0] for line in
                 path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert pages, name
        for page in pages:
            assert (GOLDEN / Path(page).name).exists() or \
                   (GOLDEN / "TBX_pages" / Path(page).name).exists(), f"{name}: {page}"


def test_our_render_reproduces_the_golden_page_sizes(golden):
    """Our 200 dpi render must land on the same pixel grid the golden boxes were drawn on.

    One known exception: the three annotated Bajaj pages. Their MediaBox is 595.08pt wide,
    which is exactly 1653 px at 200 dpi, but poppler's float arithmetic lands a hair above
    and rounds up to 1654. PyMuPDF gives the arithmetically correct 1653. The difference is
    one pixel in 1653 -- 0.06%, far below any IoU threshold -- but it is why eval normalises
    golden boxes against the golden image's own size rather than against our render.
    """
    from PIL import Image

    from src import config as C, render

    dpi = C.load(ROOT / "config.yaml").render.dpi
    mismatches = []
    for record in golden.records:
        if record.is_null or not record.image:
            continue
        pdf = golden.pdf_for(record)
        ours = render.page_size(pdf, record.page, dpi)
        with Image.open(golden.image_for(record)) as image:
            theirs = image.size
        assert theirs == record.page_size, f"{record.id}: annotation disagrees with its image"
        if ours != theirs:
            mismatches.append((pdf.name, record.page, ours, theirs))
            assert abs(ours[0] - theirs[0]) <= 1 and abs(ours[1] - theirs[1]) <= 1, \
                f"{record.id}: render is more than a pixel off {ours} vs {theirs}"

    assert {m[0] for m in mismatches} <= {"Signed_Bajaj Finance Limited - Offer letter - 01 Aug 2024.pdf"}


def test_normalise_name_bridges_underscores_and_spaces():
    assert G.normalise_name("Signed_Bajaj_Finance_Limited_-_Offer_letter.pdf") == \
           G.normalise_name("Signed Bajaj Finance Limited - Offer letter.pdf")
    assert G.normalise_name("A  B") == G.normalise_name("A_B")


def test_check_catches_a_normalised_box_that_does_not_match_its_pixels(golden):
    """The guard must actually fire -- a check that cannot fail proves nothing."""
    import copy

    broken = copy.deepcopy(golden)
    record = next(r for r in broken.records if r.value_norm and r.page_size)
    record.value_norm = (0, 0, 1, 1)
    problems = G.check(broken)
    assert any("does not match" in p for p in problems), problems
