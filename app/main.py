"""FastAPI application: upload PDFs, watch processing, review/correct data, download Excel."""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from . import config
from .batch_processor import BatchError, BatchProcessor
from .models import (
    ALL_COLUMNS,
    AMOUNT_COLUMNS,
    COLUMN_TO_ATTR,
    LINE_COLUMN_TO_ATTR,
    QUANTITY_COLUMNS,
    REQUIRED_COLUMNS,
    Batch,
    FileResult,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = Path(__file__).resolve().parent / "static"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = FastAPI(title="Invoice PDF to Excel Extraction")
processor = BatchProcessor()


def _json_value(column: str, value: object) -> object:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, date):
        return value.isoformat()
    return value


def _file_json(f: FileResult) -> dict:
    data = {
        "file_id": f.file_id,
        "file_name": f.file_name,
        "page_count": f.page_count,
        "status": f.status.value,
        "messages": f.messages,
        "duplicate_of": f.duplicate_of,
        "record": None,
    }
    if f.record is not None:
        rows = f.record.to_rows()
        data["record"] = {
            # One entry per service line; invoice-level values repeat on each.
            "lines": [{col: _json_value(col, row[col]) for col in ALL_COLUMNS} for row in rows],
            "values": {col: _json_value(col, rows[0][col]) for col in ALL_COLUMNS},
            "line_count": len(rows),
            "issues": [
                {"field": i.field, "description": i.description, "severity": i.severity.value, "page": i.page,
                 "value": i.value, "suggested_action": i.suggested_action, "review_required": i.review_required}
                for i in f.record.issues
            ],
            "review_required": f.record.review_required,
        }
    return data


def _batch_json(batch: Batch) -> dict:
    return {
        "batch_id": batch.batch_id,
        "complete": batch.complete,
        "files": [_file_json(f) for f in batch.files],
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def get_config() -> dict:
    return {
        "max_files": config.MAX_FILES_PER_BATCH,
        "max_file_size_mb": config.MAX_FILE_SIZE_MB,
        "required_columns": REQUIRED_COLUMNS,
        "editable_columns": list(COLUMN_TO_ATTR),
        "line_columns": list(LINE_COLUMN_TO_ATTR),
        "amount_columns": sorted(AMOUNT_COLUMNS),
        "quantity_columns": sorted(QUANTITY_COLUMNS),
    }


@app.post("/api/batches")
async def upload(request: Request) -> dict:
    """Accept an upload of many PDFs at once (a whole folder of them).

    The form is parsed by hand rather than through the usual `File(...)` dependency, because
    Starlette caps an upload at 1000 files by default, which is below this app's own limit and
    would refuse a large folder before it ever got here.

    Starlette spools each uploaded part to a temporary file as it arrives, so the request never
    sits in memory. The parts are then read back one at a time and handed to `create_batch`,
    which stores each before the next is read. Peak memory stays at roughly one invoice,
    however many are uploaded. The storing is blocking, so it runs in a worker thread.
    """
    max_files = config.MAX_FILES_PER_BATCH
    size_limit = config.MAX_FILE_SIZE_MB * 1024 * 1024
    try:
        # One over the limit, so an oversized batch is reported by us with a useful message.
        # (max_part_size is left alone: it bounds ordinary form fields, not uploaded files.)
        form = await request.form(max_files=max_files + 1, max_fields=max_files + 1)
    except MultiPartException as exc:
        raise HTTPException(400, f"Could not read the upload: {exc.message}") from exc

    try:
        uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
        if not uploads:
            raise HTTPException(400, "No files were uploaded.")
        if len(uploads) > max_files:
            raise HTTPException(
                400,
                f"Too many files: {len(uploads)} uploaded, the limit is {max_files} per batch. "
                f"Raise INVOICE_MAX_FILES_PER_BATCH in .env and restart, or upload the folder in parts.",
            )

        def one_at_a_time():
            for upload_file in uploads:
                upload_file.file.seek(0)
                # Read at most size_limit+1 bytes: an oversized file is rejected, not loaded whole.
                yield (upload_file.filename or "unnamed", upload_file.file.read(size_limit + 1))

        batch = await run_in_threadpool(processor.create_batch, one_at_a_time())
    except BatchError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        # Releases the spooled temporary files; the PDFs are already stored under uploads/.
        await form.close()
    processor.start_background(batch.batch_id)
    return _batch_json(batch)


def _get_batch(batch_id: str) -> Batch:
    try:
        return processor.get(batch_id)
    except KeyError:
        raise HTTPException(404, "Batch not found (batches are kept in memory until the server restarts).")


@app.get("/api/batches/{batch_id}")
def batch_status(batch_id: str) -> dict:
    return _batch_json(_get_batch(batch_id))


@app.put("/api/batches/{batch_id}/records/{file_id}")
def update_record(batch_id: str, file_id: int, changes: dict = Body(...), line: int = 0) -> dict:
    """Apply corrections to one service line (`line`, 0-based) and its invoice-level fields."""
    batch = _get_batch(batch_id)
    if not batch.complete:
        raise HTTPException(409, "The batch is still processing.")
    try:
        result = processor.update_record(batch_id, file_id, changes, line_index=line)
    except KeyError:
        raise HTTPException(404, "Record or service line not found.")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _file_json(result)


@app.get("/api/batches/{batch_id}/excel")
def download_excel(batch_id: str) -> FileResponse:
    batch = _get_batch(batch_id)
    if not batch.complete:
        raise HTTPException(409, "The batch is still processing.")
    path = processor.export(batch_id)
    return FileResponse(path, media_type=XLSX_MEDIA_TYPE, filename=path.name)
