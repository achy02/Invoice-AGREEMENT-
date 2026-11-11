"""The viewer, exercised through its API the way the browser uses it.

The point of the viewer is to answer "is OCR reading this, and is the model reading the
OCR" without opening a PDF by hand. So what these tests guard is that the data needed to
answer that actually reaches the page: the source each page was read by, its confidence,
every line with its box, and every value with the quote and box behind it.

No model and no OCR are involved here -- the pipeline is stubbed. What is being tested is
the serialisation and the wiring, which is where a viewer silently goes wrong: a box that
arrives as `None`, or a value whose page number is missing, draws nothing and looks like a
clean page.
"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from fastapi.testclient import TestClient  # noqa: E402

from src import config as config_module  # noqa: E402
from src.models import Cell, Extracted, Grounded, Line, Page, SealText, Table, Token  # noqa: E402
from web import app as web_app  # noqa: E402


@pytest.fixture
def config():
    return config_module.load(HERE / "config.yaml")


@pytest.fixture
def client(config):
    web_app.RUNS.clear()
    return TestClient(web_app.create_app(config))


def a_page(number=1, source="paddle_ocr"):
    tokens = [Token("INR", (10, 20, 60, 44), 0.99), Token("0.85", (64, 20, 120, 44), 0.97)]
    return Page(
        number=number, width=1654, height=2338, dpi=200,
        image_path=f"p-{number}.png", source=source, mean_conf=0.98,
        lines=[Line(f"p{number}:L001", "INR 0.85", (10, 20, 120, 44), source, 0.98, tokens)],
        tables=[Table(f"p{number}:T1", (10, 60, 400, 140), 1, 2,
                      [Cell(f"p{number}:T1:R0C0", 0, 0, "Product", (10, 60, 200, 100)),
                       Cell(f"p{number}:T1:R0C1", 0, 1, "Rate", (200, 60, 400, 100))])],
        seals=[SealText("Director", (300, 500, 420, 560), 0.91)],
    )


def test_no_page_payload_is_built_any_more():
    """Pages were serialised only to be drawn on. The drawing is gone, so this is too.

    It mattered for size as well as for scope: a 40-page MSA's lines, tables, seals and
    boxes were held in memory per run and shipped to the browser, to render something
    nobody was looking at.
    """
    assert not hasattr(web_app, "_page_payload")
    assert not hasattr(web_app, "_bbox")


def test_a_value_carries_its_quote_and_its_page():
    """What is left is what a person can act on: the words, and where to find them."""
    item = Grounded(field="client_name", value="ALSD Technologies Pvt Ltd",
                    evidence_text="ALSD Technologies Pvt Ltd", page=2,
                    value_bbox=(10.0, 20.0, 300.0, 50.0), key_bbox=(5.0, 20.0, 9.0, 50.0),
                    match_score=0.97, locate_method="text layer (char-level)")
    payload = web_app._value_payload(item, "Summary")

    assert payload["evidence_text"] == "ALSD Technologies Pvt Ltd"
    assert payload["page"] == 2
    assert payload["status"] == "OK"
    assert "value_bbox" not in payload and "key_bbox" not in payload


def test_an_ungrounded_value_still_reaches_the_viewer_marked_as_such():
    """It is kept out of the workbook, but hiding it here would hide the failure."""
    item = Grounded(field="effective_date", value="1 Jan 2025",
                    evidence_text="with effect from 1 Jan 2025", page=1,
                    value_bbox=None, status="UNGROUNDED",
                    flags=["quote not found on page 1"])
    payload = web_app._value_payload(item, "Summary")

    assert payload["status"] == "UNGROUNDED"
    assert payload["flags"] == ["quote not found on page 1"]


def test_a_null_stays_null_rather_than_becoming_the_word_none():
    """"Not present" is a real answer; printing it as the string "None" would be a lie."""
    payload = web_app._value_payload(Extracted(field="stamp_date", value=None), "Summary")
    assert payload["value"] is None


def test_the_page_is_served_and_calls_this_folders_own_api(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "/api/runs" in response.text


def test_the_health_check_reports_ocr_and_the_model_separately(client):
    body = client.get("/api/config").json()
    assert set(body["ocr"]) == {"ok", "detail"}
    assert set(body["llm"]) == {"ok", "detail"}
    assert body["dpi"] == 200, "boxes are in pixels at this dpi; the page must say so"


def test_a_run_over_an_unknown_folder_is_refused(client):
    assert client.post("/api/runs/folder", json={"folder": "wherever"}).status_code == 400


def test_uploading_something_that_is_not_a_pdf_is_refused(client):
    response = client.post("/api/runs",
                           files={"files": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400
    assert "not a PDF" in response.json()["detail"]


def test_an_upload_with_no_files_is_refused(client):
    assert client.post("/api/runs").status_code == 400


def test_asking_for_a_run_that_does_not_exist_says_so(client):
    assert client.get("/api/runs/deadbeef").status_code == 404


def test_a_failed_run_reports_its_reason_rather_than_hanging(client, config, monkeypatch):
    """A crash inside the worker thread must surface as a status, not a spinner forever."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("paddle fell over")

    monkeypatch.setattr("src.pipeline.run", explode)
    run = web_app.Run(id="x", started=__import__("datetime").datetime.now(), file_names=[])
    web_app._execute(run, HERE / "tests" / "golden", config)

    assert run.status == "failed"
    assert "paddle fell over" in run.error


def test_a_finished_run_can_be_forgotten_but_a_running_one_cannot(client):
    from datetime import datetime

    web_app.RUNS["busy"] = web_app.Run(id="busy", started=datetime.now(),
                                       file_names=[], status="running")
    web_app.RUNS["done"] = web_app.Run(id="done", started=datetime.now(),
                                       file_names=[], status="done")
    assert client.delete("/api/runs/busy").status_code == 409
    assert client.delete("/api/runs/done").status_code == 200
    assert "done" not in web_app.RUNS
