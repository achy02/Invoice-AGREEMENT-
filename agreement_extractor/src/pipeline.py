"""Stage orchestration: run S0-S2 over a folder, caching and isolating per file."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from .cache import StageCache
from .config import Config
from .inventory import discover, inventory
from .layout import build_document
from .logging_setup import RunLog
from .models import Document

log = logging.getLogger(__name__)


def ocr_cache(config: Config) -> StageCache:
    """OCR output keyed by page-image hash, fingerprinted with the settings that shaped it."""
    return StageCache(config.paths.cache, "ocr", {
        "dpi": config.render.dpi, "lang": config.ocr.lang,
        "orientation": config.ocr.use_doc_orientation_classify,
        "unwarp": config.ocr.use_doc_unwarping,
        "textline_orientation": config.ocr.use_textline_orientation,
        # Deliberately *not* fingerprinted with the repair rules. This entry holds what the
        # recogniser said, which rules cannot change; repairs are applied downstream in
        # `layout.build_page`. Keeping them out is what lets a rule be edited without
        # paying ~50s a page to re-read documents the recogniser already read correctly.
    })


def table_cache(config: Config) -> StageCache:
    """Table structure, cached apart from the text OCR it sits beside.

    Separate because the two cost different amounts and go stale for different reasons.
    Text OCR is ~50 s a page and changes only when the renderer or the language does; table
    structure is ~3 minutes a page and changes whenever the gate, the settings or the
    parser in `table_ocr.py` change. Kept in one entry, every parser fix would throw away
    the text as well and re-run the cheap thing to repair the expensive one -- and that is
    not hypothetical: tables that held a whole page per cell survived their own fix,
    because the entry they lived in had no reason to think anything had changed.
    """
    from .table_ocr import builder_digest

    return StageCache(config.paths.cache, "tables", {
        "dpi": config.render.dpi,
        "enabled": config.ocr.table_recognition_on_scans,
        "builder": builder_digest(),
    })


def page_cache(config: Config) -> StageCache:
    """S2 output, fingerprinted with every setting that can change what a page contains.

    This entry holds the finished pages, tables included, so it is the outermost of the
    three caches S2 reads and the only one a caller ever sees. That makes it the one that
    must know about *everything* underneath it. Twice it did not, and both times a correct
    fix appeared to do nothing at all:

    * the table settings were missing, so turning table recognition on changed nothing;
    * `builder` was missing, so repairing the parser that had put a whole page into every
      cell changed nothing either -- the repaired code never ran, because this entry had no
      reason to believe anything had changed.

    `builder` is a hash of `table_ocr.py`, so the code that shapes a table counts as an
    input to it, exactly as the prompts count as an input to S4.
    """
    from .table_ocr import builder_digest

    return StageCache(config.paths.cache, "pages", {
        "dpi": config.render.dpi,
        "min_chars": config.text_layer.min_chars_per_page,
        "lang": config.ocr.lang,
        "scan_tables": config.ocr.table_recognition_on_scans,
        "table_line_chars": config.ocr.table_page_max_mean_line_chars,
        "table_money": config.ocr.table_page_min_money_mentions,
        "builder": builder_digest(),
        # Which pages get a table depends on this too, so a change to the heading
        # vocabulary has to invalidate the finished pages the same way a parser fix does.
        "headings": _heading_digest(),
        # Finished pages embed the repaired OCR lines, so this entry has to know about
        # the repair rules as well -- it is the outermost cache and owns everything below.
        "repairs": _repair_digest(),
    })


def _repair_digest() -> str:
    """A hash of the OCR repair rules, for the caches that hold repaired text."""
    from .ocr_repair import repair_digest

    return repair_digest()


def _heading_digest() -> str:
    """A hash of the commercial-heading vocabulary that decides where tables are sought."""
    import hashlib

    from .layout import COMMERCIAL_HEADING

    return hashlib.sha256(COMMERCIAL_HEADING.pattern.encode("utf-8")).hexdigest()[:16]


def run(config: Config, root: Path | None = None, limit: int | None = None,
        resume: bool | None = None, run_log: RunLog | None = None) -> list[Document]:
    """S0 inventory -> S1 render -> S2 text/layout, for every PDF under `root`."""
    root = Path(root or config.paths.input)
    run_log = run_log or RunLog()
    resume = config.run.resume if resume is None else resume

    paths = discover(root)
    if limit:
        paths = paths[:limit]
    log.info("S0: %d PDF(s) under %s", len(paths), root)

    documents = inventory(paths, config)
    for document in documents:
        run_log.add("S0", document.file_name,
                    "FAILED" if not document.page_count else "OK",
                    detail="; ".join(document.notes))

    pages, ocr = page_cache(config), ocr_cache(config)
    out: list[Document] = []
    pending: list[Document] = []

    for document in documents:
        if not document.page_count:
            out.append(document)
            continue
        if document.duplicate_of:
            run_log.add("S2", document.file_name, "SKIPPED",
                        detail=f"duplicate of {document.duplicate_of}")
            out.append(document)
            continue

        cached = pages.get(document.sha256) if resume else None
        if cached is not None:
            document.pages = [_page_from_dict(p) for p in cached]
            run_log.add("S2", document.file_name, "CACHED", detail=f"{len(document.pages)} page(s)")
            out.append(document)
            continue

        pending.append(document)

    _process(pending, config, pages, ocr, run_log)
    out.extend(pending)
    out.sort(key=lambda d: paths.index(Path(d.path)) if Path(d.path) in paths else 1 << 30)
    return out


def _process(documents: list[Document], config: Config, pages: StageCache,
             ocr: StageCache, run_log: RunLog) -> None:
    """Read each document, in parallel when asked, isolating failures per file."""
    workers = max(1, config.run.ocr_workers)
    # One worker means inline: easier to debug, and the only mode a test ever needs.
    if workers == 1 or len(documents) < 2:
        for document in documents:
            _read_one(document, config, pages, ocr, run_log)
        return

    # A process pool rather than threads: PaddleOCR holds native state that is not safe to
    # share, and each worker pays the ~1.5s model load once across many pages.
    from concurrent.futures import ProcessPoolExecutor, as_completed

    log.info("S2: %d document(s) across %d worker(s)", len(documents), workers)
    index = {id(d): d for d in documents}
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_read_detached, d.path, d.sha256,
                                   getattr(d, "digital_pages", None), config): id(d)
                       for d in documents}
            for future in as_completed(futures):
                document = index[futures[future]]
                try:
                    payload, seconds, error = future.result()
                except Exception as exc:                      # pragma: no cover - pool crash
                    payload, seconds, error = None, 0.0, str(exc)
                if error or payload is None:
                    document.notes.append(f"S2 failed: {error}")
                    run_log.add("S2", document.file_name, "FAILED", seconds, str(error))
                    continue
                document.pages = [_page_from_dict(p) for p in payload]
                pages.put(document.sha256, payload)
                run_log.add("S2", document.file_name, "OK", seconds, _sources(document))
    except Exception as exc:
        # A pool that cannot start at all (frozen interpreter, spawn failure) must degrade
        # to working slowly rather than failing the run.
        log.warning("Parallel S2 unavailable (%s); falling back to one worker", exc)
        for document in documents:
            if not document.pages:
                _read_one(document, config, pages, ocr, run_log)


def _sources(document: Document) -> str:
    counts: dict[str, int] = {}
    for page in document.pages:
        counts[page.source] = counts.get(page.source, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))


def _read_one(document: Document, config: Config, pages: StageCache,
              ocr: StageCache, run_log: RunLog) -> None:
    started = time.perf_counter()
    try:
        build_document(document, config, ocr)
    except Exception as exc:
        # Per-file isolation: one bad PDF must never stop the run.
        log.exception("S2 failed for %s", document.file_name)
        document.notes.append(f"S2 failed: {exc}")
        run_log.add("S2", document.file_name, "FAILED", time.perf_counter() - started, str(exc))
        if not config.run.continue_on_error:
            raise
        return
    pages.put(document.sha256, [_page_to_dict(p) for p in document.pages])
    run_log.add("S2", document.file_name, "OK", time.perf_counter() - started,
                _sources(document))


def _read_detached(path: str, sha: str, digital, config: Config):
    """Worker entry point: read one document in its own process."""
    import time as _time

    started = _time.perf_counter()
    try:
        document = Document(Path(path).name, path, sha, 0)
        if digital is not None:
            document.digital_pages = digital      # type: ignore[attr-defined]
        build_document(document, config, ocr_cache(config), table_cache(config))
        return ([_page_to_dict(p) for p in document.pages],
                _time.perf_counter() - started, None)
    except Exception as exc:
        return (None, _time.perf_counter() - started, f"{exc.__class__.__name__}: {exc}")


def _page_to_dict(page) -> dict:
    return asdict(page)


def _page_from_dict(data: dict):
    from .models import Cell, Line, Page, SealText, SignatureEvidence, Table, Token

    page = Page(
        number=data["number"], width=data["width"], height=data["height"],
        dpi=data["dpi"], image_path=data["image_path"], source=data["source"],
        mean_conf=data.get("mean_conf", 1.0), note=data.get("note", ""),
    )
    page.lines = [Line(d["line_id"], d["text"], tuple(d["bbox"]), d["source"], d["conf"],
                       [Token(t["text"], tuple(t["bbox"]), t["conf"]) for t in d["tokens"]])
                  for d in data.get("lines", [])]
    page.tables = [Table(t["table_id"], tuple(t["bbox"]), t["n_rows"], t["n_cols"],
                         [Cell(c["cell_id"], c["row"], c["col"], c["text"], tuple(c["bbox"]),
                               c.get("row_span", 1), c.get("col_span", 1))
                          for c in t.get("cells", [])])
                   for t in data.get("tables", [])]
    page.seals = [SealText(s["text"], tuple(s["bbox"]), s["conf"]) for s in data.get("seals", [])]
    page.signatures = [SignatureEvidence(s["kind"], s.get("text", ""),
                                         tuple(s["bbox"]) if s.get("bbox") else None,
                                         s.get("page", 0))
                       for s in data.get("signatures", [])]
    return page


def extract_cache(config: Config) -> StageCache:
    """S4 output, fingerprinted with everything that shaped it -- the prompts included.

    The prompt text is the largest single input to this stage, so leaving it out of the
    fingerprint would be worse than having no cache: editing a prompt to fix an extraction
    would appear to change nothing, because every document would be served from a cache
    built by the old wording. Rule 5 says a stage is keyed by what it depended on, and this
    stage depends on the prompts more than on any config value beside the model itself.
    """
    from .extract import prompt_digest

    return StageCache(config.paths.cache, "extract", {
        "model": config.llm.model, "temperature": config.llm.temperature,
        "seed": config.llm.seed, "thinking": config.llm.thinking,
        "prompts": prompt_digest(),
    })


def extract_key(document: Document, body: str) -> str:
    """The S4 cache key: this file, as it was actually read.

    The file's own hash is not enough. S4's input is the *rendered body*, and that changes
    whenever S2 learns to read the page better -- recovering a table, say. Keyed on the PDF
    alone, a document would be served its old extraction from before the improvement, and
    the improvement would appear to do nothing. Hashing the body makes the key describe the
    input rather than the file it came from.
    """
    import hashlib

    digest = hashlib.sha256(document.sha256.encode("utf-8"))
    digest.update(body.encode("utf-8"))
    return digest.hexdigest()


def run_extraction(documents: list[Document], config: Config,
                   run_log: RunLog | None = None, resume: bool | None = None):
    """S3 llm_input -> S4 extract -> S5 ground -> S6 validate, per document.

    Returns the per-document results. S5 still runs: it is what decides whether a value the
    model produced actually appears in the document, and a value that fails goes to the
    Review Queue instead of the Summary sheet. Only the coordinates it computes along the
    way are no longer reported anywhere.

    Raises LlmUnavailable only if the endpoint is down; a failure on one document is
    isolated and recorded like any other.
    """
    from . import extract as extract_stage
    from . import ground as ground_stage
    from . import llm_input
    from .validate import DocumentResult, validate_document

    run_log = run_log or RunLog()
    resume = config.run.resume if resume is None else resume
    cache = extract_cache(config)
    results: list[DocumentResult] = []

    for document in documents:
        if not document.pages:
            continue

        result = DocumentResult(file_name=document.file_name)
        started = time.perf_counter()

        # Rendered before the cache is consulted, because the body is what the key is made
        # of -- see `extract_key`. Rendering is string work; it costs nothing next to a
        # model call, and it is the only way the cache can tell "same file" from "same input".
        body = llm_input.build(document, config.llm.num_ctx,
                               config.llm.max_output_tokens,
                               config.llm.instruction_tokens)
        key = extract_key(document, body)

        raw = cache.get(key) if resume else None
        if raw is None:
            try:
                items = extract_stage.extract_document(document, body, config)
            except extract_stage.LlmUnavailable:
                raise
            except Exception as exc:
                log.exception("S4 failed for %s", document.file_name)
                run_log.add("S4", document.file_name, "FAILED",
                            time.perf_counter() - started, str(exc))
                if not config.run.continue_on_error:
                    raise
                results.append(result)
                continue
            if items:
                cache.put(key, [_item_to_dict(i) for i in items])
                run_log.add("S4", document.file_name, "OK", time.perf_counter() - started,
                            f"{len(items)} field(s)")
            else:
                # Deliberately not cached. A document with readable pages that yields no
                # field at all is a failed call, not an answer: the model timed out into an
                # empty object, replied in the wrong shape, or drowned in a prompt too long
                # for the window. Caching that would make the failure permanent *and*
                # silent -- the next run would serve [] from disk in milliseconds, never
                # call the model, and report "CACHED, 0 field(s)" as though it had checked.
                # An empty result is the one answer always worth paying to recompute.
                log.warning("S4 extracted nothing from %s; not caching, so the next run "
                            "retries instead of inheriting the failure", document.file_name)
                run_log.add("S4", document.file_name, "FAILED",
                            time.perf_counter() - started, "0 field(s) -- not cached")
        else:
            items = [_item_from_dict(d) for d in raw]
            run_log.add("S4", document.file_name, "CACHED", detail=f"{len(items)} field(s)")

        grounded = ground_stage.ground_document(items, document.pages, config)
        for item, source in zip(grounded, items):
            item.sheet = getattr(source, "sheet", "Summary")          # type: ignore[attr-defined]
            item.charge = getattr(source, "charge", {})               # type: ignore[attr-defined]
            item.raw_text = item.evidence_text                        # type: ignore[attr-defined]

        prefix = _initials(document.file_name)
        summary = [g for g in grounded if getattr(g, "sheet", "") != "Commercials"]
        charges = [g for g in grounded if getattr(g, "sheet", "") == "Commercials"]
        for index, item in enumerate(summary, start=1):
            item.record_id = f"{prefix}-S{index:02d}"                 # type: ignore[attr-defined]
        for index, item in enumerate(charges, start=1):
            item.record_id = f"{prefix}-C{index:02d}"                 # type: ignore[attr-defined]

        result.fields, result.charges = summary, charges
        client = next((g.value for g in summary if g.field == "client_name"), "")
        result.client = str(client or "")
        kinds = {e.kind for page in document.pages for e in page.signatures}
        validate_document(result, config, kinds)
        run_log.add("S6", document.file_name,
                    "OK" if not result.review_required else "REVIEW",
                    detail=f"{len(result.findings)} finding(s)")
        results.append(result)

    return results


def _initials(file_name: str) -> str:
    letters = [c for c in Path(file_name).stem if c.isalpha()]
    return ("".join(letters[:2]) or "XX").upper()


def _item_to_dict(item) -> dict:
    return {"field": item.field, "value": item.value,
            "normalized_value": item.normalized_value,
            "evidence_text": item.evidence_text, "evidence_ids": item.evidence_ids,
            "page": item.page, "section_heading": item.section_heading,
            "key_text": item.key_text, "sheet": getattr(item, "sheet", "Summary"),
            "charge": getattr(item, "charge", {})}


def _item_from_dict(data: dict):
    from .models import Extracted

    item = Extracted(
        field=data["field"], value=data.get("value"),
        normalized_value=data.get("normalized_value"),
        evidence_text=data.get("evidence_text") or "",
        evidence_ids=data.get("evidence_ids") or [], page=data.get("page"),
        section_heading=data.get("section_heading") or "",
        key_text=data.get("key_text") or "")
    item.sheet = data.get("sheet", "Summary")      # type: ignore[attr-defined]
    item.charge = data.get("charge", {})           # type: ignore[attr-defined]
    return item


def dump_pages(documents: list[Document], out_dir: Path) -> list[Path]:
    """Write one JSON per document, for inspection and for the next stages."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for document in documents:
        path = out_dir / f"{Path(document.file_name).stem}.json"
        path.write_text(json.dumps(document.to_dict(), indent=2, ensure_ascii=False,
                                   default=str), encoding="utf-8")
        written.append(path)
    return written
