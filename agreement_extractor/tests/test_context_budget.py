"""The context window holds three things, and one of them was never budgeted for.

An Aditya Birla MSA rendered to a 24,181-token prompt inside a 32,768-token window. Call B
then generated past the ~8,500 tokens left over, so llama-server discarded 16,381 tokens to
make room -- the instructions and the document among them -- and the model carried on
generating from the wreckage. It never reached a stop token. Tokens arrived the whole time
at 2.8/s, so the streaming timeout never fired either, and the run simply never ended.

Two independent defects had to line up for that, so both are tested here: the body was
sized against the whole window with only 2,000 tokens held back for *the prompt and the
answer together*, and no cap existed on how long an answer could run.
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import config as config_module  # noqa: E402
from src import extract, llm_input  # noqa: E402
from src.models import Document, Line, Page, Token  # noqa: E402


@pytest.fixture
def config():
    return config_module.load(HERE / "config.yaml")


def page(number, lines_of_text):
    lines = []
    for i, text in enumerate(lines_of_text, start=1):
        bbox = (100.0, 100.0 + i * 30, 1400.0, 130.0 + i * 30)
        lines.append(Line(f"p{number}:L{i:03d}", text, bbox, "text_layer", 1.0,
                          [Token(text, bbox)]))
    return Page(number=number, width=1654, height=2339, dpi=200,
                image_path=Path(f"p{number}.png"), source="text_layer", lines=lines)


def long_document(n_pages=40, lines_per_page=45):
    """Roughly the size of a signed MSA: dense prose over dozens of pages."""
    filler = ("The Service Provider shall at all times during the Term maintain adequate "
              "records of the Services rendered under this Agreement and shall make them "
              "available for inspection upon reasonable prior written notice.")
    return Document(
        file_name="msa.pdf", path=Path("msa.pdf"), sha256="0" * 64,
        page_count=n_pages,
        pages=[page(n, [filler] * lines_per_page) for n in range(1, n_pages + 1)])


# -- sizing the body ------------------------------------------------------------------------

def test_the_body_leaves_room_for_the_answer():
    """The whole bug in one assertion: a long document must not fill the window."""
    num_ctx, answer, instructions = 32768, 6000, 2000
    body = llm_input.build(long_document(), num_ctx, answer, instructions)
    assert llm_input.estimate_tokens(body) <= num_ctx - answer - instructions


def test_a_long_document_is_actually_long_enough_to_test_this():
    """Guards the test above: a body that fits anyway would prove nothing."""
    body = llm_input.render_document(long_document())
    assert llm_input.estimate_tokens(body) > 32768


def test_a_bigger_answer_reservation_shrinks_the_body():
    doc = long_document()
    generous = llm_input.build(doc, 32768, 12000, 2000)
    frugal = llm_input.build(doc, 32768, 2000, 2000)
    assert len(generous) < len(frugal)


def test_reserving_more_than_the_window_is_an_error_not_an_empty_body():
    """Silently sending nothing would look like a model that found nothing."""
    with pytest.raises(ValueError):
        llm_input.build(long_document(), 4096, 6000, 2000)


def test_pages_dropped_from_the_middle_are_announced_in_the_text():
    body = llm_input.build(long_document(), 32768, 6000, 2000)
    assert "omitted to fit the context window" in body


def test_the_first_and_last_pages_always_survive():
    """Parties and dates open a document; commercials and signatures close it."""
    body = llm_input.build(long_document(), 32768, 6000, 2000)
    assert "--- page 1 " in body and "--- page 40 " in body


# -- capping the answer ---------------------------------------------------------------------

class FakeResponse:
    """Stands in for the urllib stream: an iterable of newline-framed JSON frames."""

    def __init__(self, frames):
        self._frames = [json.dumps(f).encode("utf-8") for f in frames]

    def __iter__(self):
        return iter(self._frames)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_request_carries_a_hard_generation_cap(config, monkeypatch):
    """Nothing else can stop a runaway: at temperature 0 the model simply keeps going."""
    sent = {}

    def fake_urlopen(request, timeout=None):
        sent.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse([{"response": "{}", "done": True}])

    monkeypatch.setattr(extract.urllib.request, "urlopen", fake_urlopen)
    extract.generate("hello", config)
    assert sent["options"]["num_predict"] == config.llm.max_output_tokens
    assert sent["options"]["num_predict"] > 0


def test_the_cap_matches_what_the_body_was_sized_against(config):
    """Two numbers in two files; if they drift apart the budget stops being a budget."""
    assert (config.llm.max_output_tokens + config.llm.instruction_tokens
            < config.llm.num_ctx)


def test_an_answer_cut_off_at_the_ceiling_is_reported(config, monkeypatch, caplog):
    """A truncated charge list parses fine and quietly drops rows, so it must be logged."""
    monkeypatch.setattr(
        extract.urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(
            [{"response": '{"charges": [{"product": "E-sign"'},
             {"response": "", "done": True, "done_reason": "length"}]))
    with caplog.at_level("WARNING"):
        extract.generate("hello", config)
    assert any("cut off" in r.message for r in caplog.records)


def test_a_normal_answer_is_not_reported_as_truncated(config, monkeypatch, caplog):
    monkeypatch.setattr(
        extract.urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(
            [{"response": '{"charges": []}'}, {"response": "", "done": True,
                                               "done_reason": "stop"}]))
    with caplog.at_level("WARNING"):
        assert extract.generate("hello", config) == '{"charges": []}'
    assert not [r for r in caplog.records if "cut off" in r.message]
