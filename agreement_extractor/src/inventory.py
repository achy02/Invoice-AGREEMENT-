"""S0 -- inventory: which PDFs exist, what they hash to, and which pages are digital.

Runs before anything expensive. Hashing first means a duplicate agreement filed under two
names is processed once, and a resumed run can tell at a glance what is already done.

A file that cannot be opened becomes a Document with `page_count` 0 and a note, never an
exception: one bad PDF must not stop a 1000-document run.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pymupdf

from .cache import file_sha256
from .config import Config
from .models import Document

log = logging.getLogger(__name__)


def discover(root: Path | str, pattern: str = "*.pdf") -> list[Path]:
    """Every PDF under `root`, recursively, in a stable order.

    A single file is also accepted, so one document can be re-run without reprocessing the
    folder it sits in -- which matters when a neighbour takes three minutes to OCR.
    """
    root = Path(root)
    if root.is_file():
        return [root]
    return sorted((p for p in root.rglob(pattern) if p.is_file()),
                  key=lambda p: str(p).casefold())


def probe(path: Path, config: Config) -> tuple[int, list[bool], list[str]]:
    """Page count and, per page, whether its text layer is usable."""
    notes: list[str] = []
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:
        return 0, [], [f"Cannot open PDF (corrupt or invalid): {exc}"]

    digital: list[bool] = []
    with doc:
        if doc.needs_pass:
            return 0, [], ["PDF is password-protected; remove the password and try again."]
        for index in range(doc.page_count):
            try:
                text = doc[index].get_text("text")
            except Exception as exc:
                notes.append(f"Page {index + 1} could not be read: {exc}")
                digital.append(False)
                continue
            digital.append(is_digital(text, config))
        count = doc.page_count
    return count, digital, notes


def is_digital(text: str, config: Config) -> bool:
    """Does this page's own text layer make OCR unnecessary?

    Dense enough to be real prose, and not mostly U+FFFD -- a page whose fonts carry no
    usable encoding decodes to a wall of replacement characters and must go to OCR even
    though it is nominally "text".
    """
    alnum = sum(ch.isalnum() for ch in text)
    if alnum < config.text_layer.min_chars_per_page:
        return False
    return text.count("�") <= alnum * config.text_layer.max_replacement_ratio


def inventory(paths: list[Path], config: Config) -> list[Document]:
    """Hash each file, count pages, detect a text layer per page, and mark duplicates."""
    documents: list[Document] = []
    seen: dict[str, str] = {}   # sha256 -> the first file name that had it

    for path in paths:
        try:
            sha = file_sha256(path)
        except OSError as exc:
            log.warning("Cannot read %s: %s", path, exc)
            documents.append(Document(path.name, str(path), "", 0, notes=[f"Unreadable: {exc}"]))
            continue

        count, digital, notes = probe(path, config)
        document = Document(path.name, str(path), sha, count, notes=list(notes))
        document.duplicate_of = seen.get(sha)
        if document.duplicate_of:
            document.notes.append(f"Identical content to {document.duplicate_of}.")
        else:
            seen[sha] = path.name

        scanned = [i + 1 for i, ok in enumerate(digital) if not ok]
        if scanned:
            document.notes.append(
                f"{len(scanned)} of {count} page(s) have no usable text layer and need OCR.")
        # Stashed for S1/S2 so the probe is not repeated.
        document.digital_pages = digital  # type: ignore[attr-defined]
        documents.append(document)

    return documents
