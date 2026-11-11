"""The context window is sized per prompt, not per worst case.

Measured on this machine (qwen3:8b on an 8 GB card, generating 300 tokens):

    num_ctx     tok/s
       4096     28.7
       8192     30.8
      16384     18.0
      32768     10.8

Nothing about the work differs between those rows. The KV cache for a 32k window will not
fit beside the weights, so 41% of the model runs on the CPU. Every three-page offer letter
in the archive was asking for 32k and paying nearly three times over for room it never
touched.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import config as config_module  # noqa: E402
from src import extract  # noqa: E402
from src.llm_input import WINDOWS, plan_window  # noqa: E402


@pytest.fixture
def config():
    return config_module.load(HERE / "config.yaml")


# -- choosing the window ----------------------------------------------------------------------

def test_an_offer_letter_does_not_ask_for_a_thirty_two_k_window():
    """~2,900 tokens of document. The old behaviour asked for 32768 regardless."""
    window, cap = plan_window(2900, 32768, 6000)
    assert window == 8192
    assert 2900 + cap <= window


def test_an_msa_still_gets_the_room_it_needs():
    """22,337 tokens of document: nothing smaller can hold it and an answer."""
    window, cap = plan_window(22337, 32768, 6000)
    assert window == 32768
    assert cap >= 1500


def test_the_window_always_has_room_for_the_answer():
    """The bug this whole area exists to prevent: no room to answer in means a runaway."""
    for prompt_tokens in (500, 2900, 7000, 12000, 20000, 24000, 30000):
        window, cap = plan_window(prompt_tokens, 32768, 6000)
        assert prompt_tokens + cap <= window, f"{prompt_tokens} tokens left no room"


def test_the_smallest_window_that_fits_is_the_one_chosen():
    for prompt_tokens in (500, 2900, 7000, 12000, 20000):
        window, cap = plan_window(prompt_tokens, 32768, 6000)
        smaller = [w for w in WINDOWS if w < window and w >= prompt_tokens + cap]
        assert not smaller, f"{window} chosen when {smaller} would have fitted"


def test_the_ceiling_in_config_is_never_exceeded():
    """A machine with less memory sets a lower ceiling and must be obeyed."""
    for prompt_tokens in (500, 9000, 20000):
        window, _ = plan_window(prompt_tokens, 16384, 6000)
        assert window <= 16384


def test_a_document_too_large_for_any_window_still_gets_an_answer():
    """Degrade, never deadlock: the body is trimmed to fit elsewhere, but be safe here."""
    window, cap = plan_window(31000, 32768, 6000)
    assert window == 32768
    assert cap >= 1500


# -- choosing the output cap --------------------------------------------------------------------

def test_the_answer_cap_scales_with_the_document():
    """A three-page letter cannot contain six thousand tokens of charges."""
    small, _ = plan_window(2900, 32768, 6000)[1], None
    assert plan_window(2900, 32768, 6000)[1] < plan_window(22337, 32768, 6000)[1]


def test_a_tiny_document_still_gets_a_usable_answer():
    """Scaled strictly with the prompt, a 300-token page would get a 300-token answer."""
    assert plan_window(300, 32768, 6000)[1] >= 1500


def test_the_cap_never_exceeds_the_configured_maximum():
    assert plan_window(60000, 32768, 6000)[1] <= 6000


# -- the wiring ------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, frames):
        import json
        self._frames = [json.dumps(f).encode("utf-8") for f in frames]

    def __iter__(self):
        return iter(self._frames)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture(monkeypatch):
    import json

    sent = {}

    def fake_urlopen(request, timeout=None):
        sent.update(json.loads(request.data.decode("utf-8")))
        return FakeResponse([{"response": "{}", "done": True, "done_reason": "stop"}])

    monkeypatch.setattr(extract.urllib.request, "urlopen", fake_urlopen)
    return sent


def test_a_short_document_sends_a_small_window(config, monkeypatch):
    sent = capture(monkeypatch)
    extract.call_b("[p1:L001] E-sign INR 15 per signature", config)
    assert sent["options"]["num_ctx"] == 8192


def test_a_long_document_sends_the_large_window(config, monkeypatch):
    sent = capture(monkeypatch)
    extract.call_b("x" * (23000 * 4), config)
    assert sent["options"]["num_ctx"] == 32768


def test_call_b_is_never_given_a_smaller_window_than_call_a(config, monkeypatch):
    """Call B's prompt is the longer of the two and its answer is much the longer.

    An earlier version of this test asserted the two calls land in the *same* bucket, and
    passed on a synthetic body where they happened to. On real documents they often do not:
    a Capitalneed offer letter sends ~3,801 tokens for Call A and ~4,553 for Call B, which
    straddles 8192. That costs a model reload between the calls, and it is worth it --
    Call A at 30.8 tok/s instead of 18 saves more than the reload costs. What must never
    happen is the reverse, Call B squeezed into the smaller window, because Call B is the
    one that generates a list.
    """
    body = "[p1:L001] Commercials\n" * 200
    sent_a = capture(monkeypatch)
    extract.call_a(body, config)
    sent_b = capture(monkeypatch)
    extract.call_b(body, config)
    assert sent_b["options"]["num_ctx"] >= sent_a["options"]["num_ctx"]
    assert sent_b["options"]["num_predict"] >= sent_a["options"]["num_predict"]


def test_the_hard_generation_cap_is_still_always_sent(config, monkeypatch):
    """Whatever else changes, a runaway must remain impossible."""
    sent = capture(monkeypatch)
    extract.call_b("[p1:L001] short", config)
    assert 0 < sent["options"]["num_predict"] <= config.llm.max_output_tokens
