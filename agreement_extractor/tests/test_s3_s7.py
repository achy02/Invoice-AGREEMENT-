"""S3 prompt building, S4 parsing, S5 grounding, S6 validation, S7 export."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config as config_module  # noqa: E402
from src import export, extract, ground, llm_input, validate  # noqa: E402
from src.models import Cell, Document, Extracted, Line, Page, SealText, SignatureEvidence, Table, Token  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def config():
    return config_module.load(ROOT / "config.yaml")


def token(text, x0, y0, x1, y1):
    return Token(text, (float(x0), float(y0), float(x1), float(y1)))


def line(line_id, text, x0, y0, x1, y1, tokens=None):
    return Line(line_id, text, (float(x0), float(y0), float(x1), float(y1)),
                "text_layer", 1.0, tokens or [token(text, x0, y0, x1, y1)])


def page(number=1, lines=None, **kw):
    return Page(number=number, width=1654, height=2338, dpi=200,
                image_path=f"p-{number}.png", source=kw.pop("source", "text_layer"),
                lines=lines or [], **kw)


# -- S3 ------------------------------------------------------------------------------------

def test_every_line_is_citable_and_tables_carry_cell_ids():
    table = Table("p1:T1", (0, 0, 100, 50), 2, 2, [
        Cell("p1:T1:R0C0", 0, 0, "Product", (0, 0, 50, 25)),
        Cell("p1:T1:R0C1", 0, 1, "Charges", (50, 0, 100, 25)),
        Cell("p1:T1:R1C0", 1, 0, "E-sign", (0, 25, 50, 50)),
        Cell("p1:T1:R1C1", 1, 1, "INR 15", (50, 25, 100, 50)),
    ])
    p = page(lines=[line("p1:L001", "Date: 4th January, 2025", 10, 10, 200, 30)])
    p.tables = [table]
    rendered = llm_input.render_page(p)
    assert "[p1:L001] Date: 4th January, 2025" in rendered
    assert "INR 15 <p1:T1:R1C1>" in rendered


def test_seals_and_signature_evidence_reach_the_prompt():
    """A client's signatory title often exists only inside a stamp image."""
    p = page(lines=[line("p1:L001", "For ALSD Technologies Pvt Ltd", 10, 10, 300, 30)])
    p.seals = [SealText("Director", (100.0, 200.0, 160.0, 220.0), 0.9)]
    p.signatures = [SignatureEvidence("envelope_id", "2C05A903", (0.0, 0.0, 1.0, 1.0), 1)]
    rendered = llm_input.render_page(p)
    assert "[seal text] Director" in rendered
    assert "signature evidence: envelope_id" in rendered


def test_an_unreadable_page_says_so_rather_than_appearing_blank():
    p = page(source="none", lines=[])
    p.note = "OCR produced no text."
    assert "no readable text" in llm_input.render_page(p)


def test_trimming_drops_the_middle_not_the_end():
    """Signatures and the Other Terms charges live on the last page; truncation loses them."""
    document = Document("a.pdf", "a.pdf", "sha", 6, [
        page(n, [line(f"p{n}:L001", f"PAGE{n}CONTENT " * 40, 0, 0, 500, 20)])
        for n in range(1, 7)])
    trimmed = llm_input.render_document(document, max_chars=1800)
    assert "PAGE1CONTENT" in trimmed
    assert "PAGE6CONTENT" in trimmed
    assert "omitted to fit the context window" in trimmed


# -- S4 ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"fields": [{"field": "client_name", "value": "A"}]}',
    'Here you go:\n```json\n{"fields": [{"field": "client_name", "value": "A"}]}\n```',
    '{"fields": [{"field": "client_name", "value": "A"},]}',
    'blah {"fields": [{"field": "client_name", "value": "A"}]} trailing words',
])
def test_model_output_is_parsed_through_the_usual_malformations(text):
    assert extract.parse_json(text)["fields"][0]["value"] == "A"


def test_unparseable_output_returns_empty_rather_than_raising():
    assert extract.parse_json("not json at all") == {}
    assert extract.parse_json("") == {}


def test_a_streamed_reply_is_rejoined_in_order():
    """Call B's answer arrives as hundreds of fragments; the caller must see one string."""
    frames = [
        b'{"response": "{\\"charges\\": ["}\n',
        b'\n',                                   # keep-alive, not a fragment
        b'{"response": "{\\"rate_inr\\": 2}"}\n',
        b'{"response": "]}", "done": true}\n',
        b'{"response": "IGNORED AFTER DONE"}\n',
    ]
    assert extract._read_stream(iter(frames)) == '{"charges": [{"rate_inr": 2}]}'


def test_a_stream_that_errors_midway_raises_rather_than_truncating():
    """A half-sent charge list parses cleanly and would silently drop rows."""
    frames = [
        b'{"response": "{\\"charges\\": [{\\"rate_inr\\": 2}"}\n',
        b'{"error": "context canceled"}\n',
    ]
    with pytest.raises(RuntimeError, match="context canceled"):
        extract._read_stream(iter(frames))


def test_the_timeout_is_long_enough_to_survive_prompt_processing(config):
    """Streaming fixes silence *during* generation; prefill is silent too.

    Ollama sends nothing while it ingests the prompt. A whole offer letter at 32k context on
    a partly-CPU model took over five minutes to get through prefill, so a 300s timeout cut
    the request off before the first token and every retry died in the same place -- which
    on the wire is indistinguishable from a hung server.
    """
    assert config.llm.request_timeout_seconds >= 600, (
        "too short to cover prompt processing on a CPU/GPU split; the symptom is a run of "
        "identical 5m0s HTTP 500s in the Ollama log")


def test_the_request_asks_for_a_stream():
    """Unstreamed, request_timeout_seconds silently becomes a cap on answer length: the
    socket stays idle until the whole reply is built, so Call B is cut off mid-generation
    and every retry dies at the same point."""
    import inspect

    assert '"stream": True' in inspect.getsource(extract.generate)


def test_the_prompts_forbid_substituting_the_letter_date():
    """The single easiest mistake on these documents, so it is stated explicitly."""
    prompt = extract.load_prompt("call_a_parties_terms.md")
    assert "NEVER" in prompt and "effective_date" in prompt
    assert "stamp paper" in prompt.lower()


def test_the_commercials_prompt_asks_for_charges_hidden_in_prose():
    """Named as the documents name it: "Other T & C", not a tidied-up paraphrase.

    The prompt used to say "Other Terms & Conditions", which appears in none of the seven
    agreements here. Aditya Birla and Ninjamonk print "Other T & C" and J S Global prints
    "Other Commercial Terms"; a heading the model is told to look for should be one it can
    actually find.
    """
    prompt = extract.load_prompt("call_b_commercials.md")
    assert "Other T & C" in prompt
    assert "Other Commercial Terms" in prompt
    assert "man-day" in prompt


# -- S5 ------------------------------------------------------------------------------------

def test_normalise_ignores_spacing_and_punctuation():
    assert ground.normalise("INR 2/- Per Transaction") == ground.normalise("INR2/-PerTransaction")


def test_a_quote_inside_a_long_token_gets_a_trimmed_box():
    """OCR returns whole lines; without trimming a phrase inherits the entire line's box."""
    tokens = [token("in addition to above Charges.", 0, 0, 290, 20)]
    match = ground.exact_matches(tokens, "above Charges.")[0]
    # "abovecharges" is the back half of "inadditiontoabovecharges", so the trimmed box
    # starts halfway across the line instead of inheriting all of it.
    assert match.bbox[0] == pytest.approx(145, abs=2)
    assert match.bbox[2] == pytest.approx(290, abs=1)


def test_a_fuzzy_match_may_never_change_a_number(config):
    """Fuzzy matching forgives OCR letter slips, never a different rate.

    "INR 99 per transaction" scores well above threshold against a page that says
    "INR 2 per transaction"; letting that ground would put a hallucinated rate into the
    workbook as a located fact.
    """
    tokens = [token("Penny-Drop INR 2 per transaction", 0, 0, 400, 20)]
    assert ground.fuzzy_matches(tokens, "INR 99 per transaction",
                                config.grounding.fuzzy_threshold) == []
    # A letter slip in the same span is still forgiven.
    slipped = [token("Enhanced (15 ixsuer)", 0, 0, 300, 20)]
    assert ground.fuzzy_matches(slipped, "Enhanced (15 issuer)",
                                config.grounding.fuzzy_threshold) != []


def test_a_short_quote_prefers_a_whole_word_over_one_inside_a_word(config):
    """Normalising to alphanumerics puts "To" inside "Solutions"."""
    tokens = [token("Solutions", 500, 500, 600, 520), token("To", 100, 100, 130, 120)]
    found = ground.locate(tokens, "To", config.grounding.fuzzy_threshold)
    assert found.bbox[0] == 100


def test_the_line_join_marker_is_split_and_the_parts_unioned(config):
    """'INR 55 per stamp / (Delhi)' wraps across two lines; searched literally it is never found."""
    tokens = [token("INR 55 per stamp", 1239, 714, 1420, 744),
              token("(Delhi)", 1239, 755, 1330, 785)]
    found = ground.locate_quote(tokens, "INR 55 per stamp / (Delhi)",
                                config.grounding.fuzzy_threshold)
    assert found is not None
    bbox, _, method = found
    assert bbox[1] == pytest.approx(714) and bbox[3] == pytest.approx(785)
    assert "joined" in method


def test_the_copy_on_the_labels_row_wins(config):
    """'INR 12' twice in one rate table: the wrong one puts a rate on the wrong product."""
    tokens = [token("Bank Statement Analyser", 206, 417, 519, 447),
              token("INR 12", 1153, 415, 1235, 445),
              token("Equifax Report", 206, 510, 390, 540),
              token("INR 12", 1153, 508, 1235, 538)]
    key = (206.0, 417.0, 519.0, 447.0)
    found = ground.locate(tokens, "INR 12", config.grounding.fuzzy_threshold, key)
    assert found.bbox[1] == pytest.approx(415)


def test_area_alone_does_not_decide(config):
    """A smaller font elsewhere makes the wrong occurrence the smaller box."""
    tokens = [token("Authorized Signatory", 235, 2016, 535, 2047),
              token("Pulak Jain", 235, 2049, 365, 2085),       # right, larger
              token("Pulak Jain", 495, 2116, 582, 2143)]       # footer, smaller
    key = (235.0, 2016.0, 535.0, 2047.0)
    found = ground.locate(tokens, "Pulak Jain", config.grounding.fuzzy_threshold, key)
    assert found.bbox[1] == pytest.approx(2049)


def test_an_unfindable_quote_is_ungrounded_not_invented(config):
    """The check that stops a fluent model putting a plausible rate in the workbook."""
    pages = {1: page(lines=[line("p1:L001", "Penny-Drop INR 2 per transaction", 0, 0, 400, 20)])}
    item = Extracted(field="rate", value="INR 99", evidence_text="INR 99 per transaction",
                     page=1)
    result = ground.ground_field(item, pages, config)
    assert result.status == "UNGROUNDED" and result.value_bbox is None


def test_a_deliberate_null_needs_no_location(config):
    item = Extracted(field="effective_date", value=None, evidence_text="", page=None)
    result = ground.ground_field(item, {}, config)
    assert result.status == "OK" and result.locate_method == "not present in document"


def test_a_wrong_citation_costs_time_not_the_value(config):
    """Citing the wrong line is a hint being wrong, not the value being absent."""
    pages = {1: page(lines=[
        line("p1:L001", "Header", 0, 0, 100, 20),
        line("p1:L009", "Payment term to be 30 days from Invoice issuance.", 0, 40, 600, 60)])}
    item = Extracted(field="payment_terms", value="30 days",
                     evidence_text="30 days from Invoice issuance.",
                     evidence_ids=["p1:L001"], page=1)
    result = ground.ground_field(item, pages, config)
    assert result.status == "OK" and result.value_bbox is not None
    assert any("whole page" in f for f in result.flags)


# -- S6 ------------------------------------------------------------------------------------

@pytest.mark.parametrize("kinds,tbx,client,expected", [
    ({"envelope_id"}, None, None, "SIGNED"),
    ({"pdf_signature_field"}, None, None, "SIGNED"),
    (set(), "Pulak Jain", "Abhijeet Goel", "SIGNED"),
    ({"seal"}, "Pulak Jain", None, "SIGNED"),
    (set(), "Pulak Jain", None, "PARTIAL"),
    (set(), None, None, "UNSIGNED"),
    ({"typed_name"}, None, None, "UNCERTAIN"),
])
def test_signed_status_is_graded_not_guessed(kinds, tbx, client, expected):
    assert validate.signed_status(kinds, tbx, client) == expected


def test_a_rate_that_contradicts_its_own_evidence_is_flagged(config):
    item = ground.Grounded(field="Penny-Drop - Rate", value="INR 2",
                           evidence_text="INR 2/- Per Transaction")
    item.charge = {"rate_inr": 20, "rate_as_written": "INR 2/- Per Transaction"}
    result = validate.DocumentResult("a.pdf", charges=[item])
    validate.validate_document(result, config)
    assert any("does not match the text it cites" in f.message for f in result.findings)


def test_prepaid_contradicting_invoice_terms_is_flagged(config):
    """Real: Ninjamonk says prepaid and 30 days from invoice."""
    fields = [ground.Grounded(field="billing_model", value="Prepaid"),
              ground.Grounded(field="payment_terms", value="30 days from Invoice issuance")]
    result = validate.DocumentResult("a.pdf", fields=fields)
    validate.validate_document(result, config)
    assert any("prepaid but payment terms" in f.message for f in result.findings)


def test_an_unreplaced_template_placeholder_is_flagged(config):
    """Real: the ALSD letter's table header still reads 'Commercials for Company Name'."""
    item = ground.Grounded(field="x", value="y",
                           evidence_text="Commercials for Company Name")
    result = validate.DocumentResult("a.pdf", fields=[item])
    validate.validate_document(result, config)
    assert any("template placeholder" in f.message for f in result.findings)


def test_revised_commercials_imply_an_earlier_version(config):
    item = ground.Grounded(field="x", value="y", evidence_text="Proposed revised Commercials")
    result = validate.DocumentResult("a.pdf", fields=[item])
    validate.validate_document(result, config)
    assert any("earlier version may exist" in f.message for f in result.findings)


def test_an_ungrounded_value_sends_the_document_to_review(config):
    item = ground.Grounded(field="rate", value="INR 99", status="UNGROUNDED")
    result = validate.DocumentResult("a.pdf", charges=[item])
    validate.validate_document(result, config)
    assert result.review_required
    assert any(f.severity == "ERROR" and "withheld" in f.message for f in result.findings)


def test_validation_never_rewrites_a_value(config):
    item = ground.Grounded(field="Rate", value="INR 2", evidence_text="INR 2/- Per Transaction")
    item.charge = {"rate_inr": 20, "rate_as_written": "INR 2/- Per Transaction"}
    result = validate.DocumentResult("a.pdf", charges=[item])
    validate.validate_document(result, config)
    assert item.charge["rate_inr"] == 20, "a check must report, never correct"


@pytest.mark.parametrize("text,expected", [
    ("4th January, 2025", (2025, 1, 4)),
    ("10th – Nov – 2025", (2025, 11, 10)),
    ("30th-June-2025", (2025, 6, 30)),
    ("1st Aug 2024", (2024, 8, 1)),
])
def test_the_date_formats_these_letters_actually_use(text, expected):
    parsed = validate.parse_date(text)
    assert parsed is not None and (parsed.year, parsed.month, parsed.day) == expected


@pytest.mark.parametrize("printed, expected", [
    ("10th – Nov – 2025", "10-Nov-2025"),
    ("4th January, 2025", "04-Jan-2025"),
    ("1st Aug 2024", "01-Aug-2024"),
    ("30th-June-2025", "30-Jun-2025"),
    ("Tue Nov 25 18:16:45 IST 2025", "25-Nov-2025 18:16:45 IST"),
    ("not a date at all", ""),
])
def test_a_date_gets_one_canonical_form(printed, expected):
    """Four documents print one day four ways; a column read four ways cannot be sorted."""
    assert validate.canonical_date(printed) == expected


def test_canonicalising_a_date_never_replaces_what_the_page_said(config):
    item = Extracted(field="offer_letter_date", value="10th – Nov – 2025",
                     evidence_text="10th – Nov – 2025", page=1)
    grounded = ground.ground_field(item, {1: page(1, [line("p1:L001", "10th – Nov – 2025",
                                                           10, 10, 200, 30)])}, config)
    result = validate.DocumentResult(file_name="x.pdf", fields=[grounded])
    validate.validate_document(result, config)
    assert result.fields[0].value == "10th – Nov – 2025", "the verbatim text must survive"
    assert result.fields[0].normalized_value == "10-Nov-2025"


@pytest.mark.parametrize("text,expected", [
    ("INR 1,00,000/-", 100000.0), ("Rs. 0.30", 0.30), ("INR @13500/man-day(s)", 13500.0),
    ("at actuals", None),
])
def test_indian_number_formats_parse(text, expected):
    assert validate.parse_number(text) == expected


# -- S7 ------------------------------------------------------------------------------------

def test_the_workbook_has_the_sheets_a_reader_needs(config, tmp_path):
    item = ground.Grounded(field="client_name", value="ALSD Technologies Pvt Ltd",
                           evidence_text="ALSD Technologies Pvt Ltd", page=1,
                           value_bbox=(138.0, 568.0, 471.0, 598.0), match_score=1.0)
    item.sheet, item.record_id = "Summary", "AL-S01"
    result = validate.DocumentResult("a.pdf", client="ALSD", fields=[item],
                                     signed_status="SIGNED")
    book = export.build_workbook([result], config)
    assert book.sheetnames == ["Read Me", "Summary", "Commercials",
                               "Review Queue", "Run Log"]


def test_the_workbook_reports_no_coordinates_anywhere(config):
    """Boxes were for drawing on page images, and there are no page images now."""
    item = ground.Grounded(field="client_name", value="ALSD", evidence_text="ALSD", page=1,
                           value_bbox=(138.0, 568.0, 471.0, 598.0), match_score=1.0)
    item.sheet = "Summary"
    charge = ground.Grounded(field="E-sign - Rate", value="15", evidence_text="INR 15",
                             page=3, value_bbox=(10.0, 20.0, 30.0, 40.0))
    charge.sheet, charge.charge = "Commercials", {"rate_inr": 15}
    book = export.build_workbook(
        [validate.DocumentResult("a.pdf", fields=[item], charges=[charge])], config)
    text = " ".join(str(c.value) for name in book.sheetnames
                    for row in book[name] for c in row)
    assert "138" not in text and "568" not in text
    assert "BBox" not in text


def test_a_charge_still_says_where_it_came_from(config):
    """The page number and the quote replace the box: both usable without a tool."""
    charge = ground.Grounded(field="E-sign - Rate", value="15",
                             evidence_text="E-sign INR 15 /- per signature", page=3)
    charge.sheet, charge.charge = "Commercials", {"rate_inr": 15, "product": "E-sign"}
    book = export.build_workbook(
        [validate.DocumentResult("a.pdf", charges=[charge])], config)
    header = [c.value for c in book["Commercials"][1]]
    row = {h: c.value for h, c in zip(header, book["Commercials"][2])}
    assert row["Source Page"] == 3
    assert row["Quoted Text"] == "E-sign INR 15 /- per signature"
    assert row["Verified"] == "yes"


def test_an_ungrounded_value_is_kept_out_of_the_summary(config):
    """It belongs in the Review Queue, not reported as fact.

    This is the check that survived the cull of everything else S5 produced, and it is the
    reason S5 survived with it: without it an invented value reads as an extracted one.
    """
    item = ground.Grounded(field="client_name", value="Invented Ltd", status="UNGROUNDED")
    item.sheet = "Summary"
    result = validate.DocumentResult("a.pdf", fields=[item])
    validate.validate_document(result, config)
    book = export.build_workbook([result], config)
    header = [c.value for c in book["Summary"][1]]
    row = {h: c.value for h, c in zip(header, book["Summary"][2])}
    assert row["Client Name"] == ""
    queue = " ".join(str(c.value) for r in book["Review Queue"] for c in r)
    assert "client_name" in queue and "could not be found" in queue


def test_the_workbook_name_cannot_collide_with_the_invoice_one(config, tmp_path):
    object.__setattr__(config.paths, "output", tmp_path)
    path = export.write_workbook([], config)
    assert path.name.startswith("Agreement_Extraction_")
    assert "Invoice" not in path.name


def test_the_pipeline_no_longer_draws_on_pages():
    """The QA overlay machinery is gone; nothing should be able to call it back."""
    assert not hasattr(export, "overlay_sample")
    assert not (Path(__file__).resolve().parent.parent / "scripts" / "overlay.py").exists()
