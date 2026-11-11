"""A local front end for the agreement pipeline: drop PDFs in, get a workbook out.

The job is narrow on purpose. Upload one or more agreements, watch which stage is running
(a scanned letter takes minutes -- roughly 50 s per page for OCR, plus two model calls), and
download the .xlsx when it finishes. Per document it shows what a person would want to know
before opening the workbook: how many pages, how many came from the text layer versus OCR,
mean OCR confidence, how many fields and charge lines came back, and how many things were
flagged for review.

It does **not** render pages or draw boxes. That machinery is gone, along with the page
images it served. S5 still verifies every value against the OCR text behind the scenes, and
a value whose quote cannot be found still lands in the workbook's Review Queue saying so --
in words, which is what a reviewer acts on anyway.

This is a separate application from the invoice web app. It shares no code with it, runs on
its own port, and nothing in `app/` knows it exists.
"""
from __future__ import annotations

import logging
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from src import config as config_module
from src.logging_setup import RunLog

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"


@dataclass
class Run:
    """One pass of the pipeline over one upload, and everything the UI needs from it."""

    id: str
    started: datetime
    file_names: list[str]
    status: str = "running"            # running | done | failed
    stage: str = "queued"
    error: str = ""
    documents: list[dict] = field(default_factory=list)
    workbook: str = ""
    upload_dir: Path | None = None

    def summary(self) -> dict:
        return {
            "id": self.id, "status": self.status, "stage": self.stage,
            "error": self.error, "started": self.started.isoformat(),
            "file_names": self.file_names,
            "documents": [{k: d[k] for k in
                           ("file_name", "page_count", "sources", "mean_conf",
                            "field_count", "charge_count", "signed_status",
                            "review_required", "flag_count")}
                          for d in self.documents],
            "workbook": self.workbook,
        }


RUNS: dict[str, Run] = {}
_LOCK = threading.Lock()


def _value_payload(item, sheet: str) -> dict:
    """One extracted value, as text: what was read, where it was read, and whether it checked out.

    `status` is read with `getattr` rather than attribute access because an item that never
    reached S5 is an `Extracted`, not a `Grounded`. That happens whenever extraction ran and
    grounding did not, which is exactly the state a reader most wants to look at -- so this
    must render it, not raise on it.
    """
    return {
        "sheet": sheet,
        "record_id": getattr(item, "record_id", ""),
        "field": item.field,
        "value": item.value,
        "normalized_value": item.normalized_value,
        "evidence_text": item.evidence_text,
        "page": item.page,
        "section_heading": item.section_heading,
        "status": getattr(item, "status", "OK"),
        "flags": list(getattr(item, "flags", None) or []),
        "charge": _charge_payload(getattr(item, "charge", {}) or {}),
    }


def _charge_payload(charge: dict) -> dict:
    """The charge dict, with the canonical basis given its readable label for the page."""
    from src import basis as basis_module

    out = dict(charge)
    resolved = out.get("resolved_basis")
    if resolved:
        out["resolved_basis_label"] = basis_module.LABELS.get(resolved, resolved)
    return out


def _execute(run: Run, root: Path, config) -> None:
    """S0-S7 for one upload. Every failure becomes a status, never a traceback at a socket."""
    from src import export, pipeline
    from src import extract as extract_stage

    try:
        run_log = RunLog()
        run.stage = "reading pages (OCR where there is no text layer)"
        documents = pipeline.run(config, root=root, run_log=run_log)

        ok, why = extract_stage.available(config)
        results = []
        if ok:
            run.stage = f"asking {config.llm.model} for values"
            results = pipeline.run_extraction(documents, config, run_log=run_log)
        else:
            run.error = f"OCR finished; extraction skipped: {why}"

        by_name = {r.file_name: r for r in results}
        payload = []
        for document in documents:
            result = by_name.get(document.file_name)
            sources: dict[str, int] = {}
            for page in document.pages:
                sources[page.source] = sources.get(page.source, 0) + 1
            confs = [p.mean_conf for p in document.pages if p.source == "paddle_ocr"]
            payload.append({
                "file_name": document.file_name,
                "sha256": document.sha256,
                "page_count": document.page_count,
                "notes": list(document.notes),
                "sources": sources,
                "mean_conf": round(sum(confs) / len(confs), 4) if confs else None,
                "fields": [_value_payload(i, "Summary") for i in (result.fields if result else [])],
                "charges": [_value_payload(i, "Commercials")
                            for i in (result.charges if result else [])],
                "findings": [{"severity": f.severity, "field": f.field, "message": f.message}
                             for f in (result.findings if result else [])],
                "field_count": len(result.fields) if result else 0,
                "charge_count": len(result.charges) if result else 0,
                "signed_status": result.signed_status if result else "",
                "review_required": bool(result.review_required) if result else False,
                "flag_count": len(result.findings) if result else 0,
            })

        run.documents = payload
        if results:
            run.workbook = str(export.write_workbook(results, config, run_log))
        run.stage = "done"
        run.status = "done"
    except Exception as exc:                      # a failed run must still report itself
        log.exception("Run %s failed", run.id)
        run.status, run.stage = "failed", "failed"
        run.error = f"{exc.__class__.__name__}: {exc}"


def create_app(config=None) -> FastAPI:
    config = config or config_module.load()
    app = FastAPI(title="Agreement extraction viewer")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/config")
    def get_config() -> dict:
        """Whether OCR and the model are actually usable, answered the same way `--check` is.

        Reusing `preflight` rather than re-testing here matters: the two Windows traps this
        project keeps hitting -- a missing VCOMP140.DLL and oneDNN failing per inference call
        rather than at import -- are exactly the ones a casual `find_spec` would call healthy.
        """
        from src import extract as extract_stage
        from src import preflight

        ocr_checks = [*preflight.paddle(), preflight.onednn_disabled()]
        failed = [c for c in ocr_checks if not c.ok]
        llm_ok, llm_why = extract_stage.available(config)
        return {
            "model": config.llm.model,
            "endpoint": config.llm.endpoint,
            "dpi": config.render.dpi,
            "ocr": {
                "ok": not failed,
                "detail": "; ".join(f"{c.name}: {c.detail}" for c in failed)
                          or f"PaddleOCR ready ({len(ocr_checks)} checks passed)",
            },
            "llm": {"ok": llm_ok, "detail": llm_why},
            "golden_dir": str(config.paths.golden),
            "sample_dir": str(config.paths.input),
        }

    @app.post("/api/runs")
    async def start_run(files: list[UploadFile] | None = None) -> dict:
        """Upload one or more PDFs and start a run over them."""
        if not files:
            raise HTTPException(400, "Attach at least one PDF.")
        run_id = uuid.uuid4().hex[:12]
        # Under cache/, so output/ holds workbooks and nothing else.
        upload_dir = config.paths.cache / "uploads" / run_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        names = []
        for upload in files:
            name = Path(upload.filename or "document.pdf").name
            if not name.lower().endswith(".pdf"):
                raise HTTPException(400, f"{name} is not a PDF.")
            target = upload_dir / name
            target.write_bytes(await upload.read())
            names.append(name)

        return _launch(run_id, names, upload_dir)

    @app.post("/api/runs/folder")
    def start_folder_run(body: dict) -> dict:
        """Run over a folder already on disk -- the golden set, or the sample agreements."""
        which = (body or {}).get("folder", "")
        folder = {"golden": config.paths.golden, "samples": config.paths.input}.get(which)
        if folder is None:
            raise HTTPException(400, "folder must be 'golden' or 'samples'.")
        if not Path(folder).is_dir():
            raise HTTPException(404, f"{folder} does not exist.")
        names = sorted(p.name for p in Path(folder).rglob("*.pdf"))
        if not names:
            raise HTTPException(404, f"No PDFs under {folder}.")
        run_id = uuid.uuid4().hex[:12]
        return _launch(run_id, names, Path(folder), own_upload=False)

    def _launch(run_id: str, names: list[str], root: Path, own_upload: bool = True) -> dict:
        run = Run(id=run_id, started=datetime.now(), file_names=names,
                  upload_dir=root if own_upload else None)
        with _LOCK:
            RUNS[run_id] = run
        thread = threading.Thread(target=_execute, args=(run, root, config), daemon=True)
        thread.start()
        return {"id": run_id, "file_names": names}

    def _run_or_404(run_id: str) -> Run:
        run = RUNS.get(run_id)
        if run is None:
            raise HTTPException(404, "No such run. It may have been lost on a restart.")
        return run

    @app.get("/api/runs")
    def list_runs() -> dict:
        return {"runs": [r.summary() for r in
                         sorted(RUNS.values(), key=lambda r: r.started, reverse=True)]}

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: str) -> dict:
        return _run_or_404(run_id).summary()

    @app.get("/api/runs/{run_id}/documents/{index}")
    def document(run_id: str, index: int) -> dict:
        run = _run_or_404(run_id)
        if not 0 <= index < len(run.documents):
            raise HTTPException(404, "No such document in this run.")
        return run.documents[index]

    @app.get("/api/runs/{run_id}/excel")
    def workbook(run_id: str) -> FileResponse:
        run = _run_or_404(run_id)
        if not run.workbook or not Path(run.workbook).is_file():
            raise HTTPException(404, "No workbook for this run yet.")
        return FileResponse(
            run.workbook, filename=Path(run.workbook).name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.delete("/api/runs/{run_id}")
    def forget(run_id: str) -> dict:
        run = _run_or_404(run_id)
        if run.status == "running":
            raise HTTPException(409, "That run is still going.")
        with _LOCK:
            RUNS.pop(run_id, None)
        if run.upload_dir and Path(run.upload_dir).is_dir():
            shutil.rmtree(run.upload_dir, ignore_errors=True)
        return {"forgotten": run_id}

    return app
