"""S2b -- PaddleOCR for scanned pages and for image regions on digital pages.

Two settings here were expensive to find, so they are stated plainly:

* **oneDNN must be off.** PaddlePaddle 3.3.1 fails every inference call through its PIR
  executor with "ConvertPirAttribute2RuntimeAttribute not support". It fails per page rather
  than at startup, so with oneDNN enabled an archive OCRs to nothing while the run looks
  healthy. Here it is turned off twice over: the `FLAGS_use_mkldnn` environment variable
  below, set before paddle is imported, and `enable_mkldnn=False` on the constructor.
  The environment variable alone is **not** enough for every pipeline -- `PPStructureV3`
  ignores it and needs the constructor argument, which is why `table_ocr.py` passes it.
* **Doc orientation and unwarping are off.** They correct photographs taken at an angle.
  These documents are flatbed scans and PDF renders, already square, and each model costs
  seconds per page.

Results are cached by a hash of the page image, so re-running over the same archive is free.
"""
from __future__ import annotations

import io
import logging
import os
import time
from pathlib import Path

from .config import Config
from .models import Line, SealText, Token

log = logging.getLogger(__name__)

# Must precede `import paddle`, which happens lazily inside `engine()`.
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("GLOG_minloglevel", "2")


class OcrUnavailable(RuntimeError):
    """PaddleOCR is not installed, or cannot run in this environment."""


_ENGINES: dict[tuple, object] = {}


def engine(config: Config, structure: bool = False):
    """Build (once) the PaddleOCR or PPStructureV3 pipeline described by `config`."""
    key = (structure, config.ocr.lang, config.ocr.use_table_recognition,
           config.ocr.use_seal_recognition)
    if key in _ENGINES:
        return _ENGINES[key]

    try:
        import paddleocr
    except Exception as exc:  # pragma: no cover - depends on the native install
        raise OcrUnavailable(f"PaddleOCR could not be imported: {exc}") from exc

    started = time.perf_counter()
    common = {
        "use_doc_orientation_classify": config.ocr.use_doc_orientation_classify,
        "use_doc_unwarping": config.ocr.use_doc_unwarping,
        "use_textline_orientation": config.ocr.use_textline_orientation,
    }
    try:
        if structure:
            built = paddleocr.PPStructureV3(
                use_table_recognition=config.ocr.use_table_recognition,
                use_seal_recognition=config.ocr.use_seal_recognition,
                use_formula_recognition=config.ocr.use_formula_recognition,
                use_chart_recognition=config.ocr.use_chart_recognition,
                **common,
            )
        else:
            built = paddleocr.PaddleOCR(lang=config.ocr.lang,
                                        enable_mkldnn=False, **common)
    except Exception as exc:  # pragma: no cover - depends on the native install
        raise OcrUnavailable(f"PaddleOCR could not start: {exc}") from exc

    log.info("Loaded %s in %.1fs", "PPStructureV3" if structure else "PaddleOCR",
             time.perf_counter() - started)
    _ENGINES[key] = built
    return built


def _as_array(image: Path | bytes):
    import numpy as np
    from PIL import Image

    source = io.BytesIO(image) if isinstance(image, bytes) else image
    with Image.open(source) as handle:
        return np.array(handle.convert("RGB"))


def _quad_to_bbox(points) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def flatten(raw) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """Normalise PaddleOCR's output into (text, confidence, bbox).

    The shape differs between releases: 2.x returns [[[quad, (text, score)], ...]] per image,
    3.x a list of dicts with parallel `rec_texts` / `rec_scores` / `rec_polys`. Both are
    handled so an upgrade does not silently produce empty pages.
    """
    out: list[tuple[str, float, tuple[float, float, float, float]]] = []
    for page in raw or []:
        if isinstance(page, dict):                                   # 3.x
            texts = page.get("rec_texts") or []
            scores = page.get("rec_scores") or []
            polys = page.get("rec_polys") or page.get("dt_polys") or []
            for text, score, poly in zip(texts, scores, polys):
                if str(text).strip():
                    out.append((str(text), float(score), _quad_to_bbox(poly)))
            continue
        for entry in page or []:                                     # 2.x
            try:
                quad, (text, score) = entry[0], entry[1]
            except (TypeError, IndexError, ValueError):
                continue
            if str(text).strip():
                out.append((str(text), float(score), _quad_to_bbox(quad)))
    return out


def read_page(image: Path | bytes, page_number: int, config: Config) -> list[Line]:
    """OCR one rendered page into lines with word-level boxes.

    PaddleOCR returns one entry per detected text line, so each becomes a Line whose single
    Token carries the same box. That is coarser than the text layer's character geometry --
    which is the honest cost of a scan, and why `locate_method` records which path was used.
    """
    array = _as_array(image)
    pipeline = engine(config, structure=False)
    recognise = getattr(pipeline, "predict", None) or pipeline.ocr
    results = flatten(recognise(array))

    # Deliberately raw. Glyph repair happens in `layout.build_page`, *after* this output
    # has been cached, so that changing a repair rule costs a second rather than a 50s
    # re-read of every page. See `ocr_repair` for why the rules live outside the cache.
    lines: list[Line] = []
    for text, score, bbox in results:
        lines.append(Line(line_id="", text=text, bbox=bbox, source="paddle_ocr",
                          conf=score, tokens=[Token(text, bbox, score)]))

    lines.sort(key=lambda line: (round(line.bbox[1] / 4), line.bbox[0]))
    for index, line in enumerate(lines, start=1):
        line.line_id = f"p{page_number}:L{index:03d}"
    return lines


def read_seals(image: Path | bytes, config: Config) -> list[SealText]:
    """Text recognised inside seals and stamps.

    Worth its own pass because a client's signatory title often exists only inside a stamp
    image: ALSD's "Director" is in the golden set as OCR-only for exactly this reason.
    """
    if not config.ocr.use_seal_recognition:
        return []
    try:
        import paddleocr
    except Exception as exc:
        raise OcrUnavailable(str(exc)) from exc
    if not hasattr(paddleocr, "SealRecognition"):
        return []

    array = _as_array(image)
    pipeline = paddleocr.SealRecognition()
    recognise = getattr(pipeline, "predict", None) or pipeline
    try:
        raw = recognise(array)
    except Exception as exc:
        log.warning("Seal recognition failed: %s", exc)
        return []
    return [SealText(text, bbox, score) for text, score, bbox in flatten(raw)]


def mean_confidence(lines: list[Line]) -> float:
    return sum(line.conf for line in lines) / len(lines) if lines else 0.0
