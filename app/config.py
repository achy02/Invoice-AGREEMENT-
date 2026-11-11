"""Application configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: str) -> Path:
    """Relative paths are resolved against the project folder, not the current directory."""
    path = Path(os.getenv(name, default))
    return path if path.is_absolute() else BASE_DIR / path


UPLOAD_DIR = _env_path("INVOICE_UPLOAD_DIR", "uploads")
OUTPUT_DIR = _env_path("INVOICE_OUTPUT_DIR", "outputs")
VENDOR_RULES_FILE = _env_path("INVOICE_VENDOR_RULES_FILE", "vendor_rules.json")

# A whole invoice archive can be uploaded in one go: files are stored one at a time, so the
# ceiling is processing time and disk, not memory. Raise it further if an archive is bigger.
MAX_FILES_PER_BATCH = _env_int("INVOICE_MAX_FILES_PER_BATCH", 5000)
MAX_FILE_SIZE_MB = _env_int("INVOICE_MAX_FILE_SIZE_MB", 50)

# A page needs at least this many letters/digits of extractable text to count as readable.
MIN_TEXT_CHARS_PER_PAGE = _env_int("INVOICE_MIN_TEXT_CHARS_PER_PAGE", 20)

# Allowed difference when checking Taxable Value + taxes (+ round off) against Total Amount.
AMOUNT_TOLERANCE = Decimal(os.getenv("INVOICE_AMOUNT_TOLERANCE", "1.00"))

# Numeric dates such as 04/05/2024 are read day-first (Indian convention) unless disabled.
DATE_DAYFIRST = _env_bool("INVOICE_DATE_DAYFIRST", True)

# Delete uploaded PDFs from disk once a batch has been processed.
DELETE_UPLOADS_AFTER_PROCESSING = _env_bool("INVOICE_DELETE_UPLOADS_AFTER_PROCESSING", True)

HOST = os.getenv("INVOICE_HOST", "127.0.0.1")
PORT = _env_int("INVOICE_PORT", 8000)
