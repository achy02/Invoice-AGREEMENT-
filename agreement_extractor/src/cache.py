"""Per-stage, per-file JSON cache keyed by the PDF's SHA-256.

Two of the spec's rules rest on this:

* **deterministic and resumable** -- a re-run skips any file whose stage output is already
  here, so a 1000-document run can be stopped and restarted without losing work;
* **per-file isolation** -- a stage writes one file per document, so one bad PDF cannot
  corrupt or block the rest.

Writes are atomic (temp file, then replace) because a run interrupted mid-write would
otherwise leave a truncated JSON that the next run would read back as valid.

A cache entry records the config values the stage depended on. If those change -- a
different render DPI, say -- the entry is treated as a miss rather than silently reused,
which is what keeps stale 150 dpi boxes from being mixed into a 200 dpi run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CHUNK = 1 << 20


def file_sha256(path: Path | str) -> str:
    """Hash a PDF by content, so the same document under two names is one cache entry."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StageCache:
    """`cache/<stage>/<sha256>.json`, one entry per document per stage."""

    def __init__(self, root: Path | str, stage: str, fingerprint: dict[str, Any] | None = None):
        self.dir = Path(root) / stage
        self.stage = stage
        self.fingerprint = fingerprint or {}

    def path(self, sha: str) -> Path:
        return self.dir / f"{sha}.json"

    def get(self, sha: str) -> Any | None:
        """Return the cached payload, or None on a miss or a changed fingerprint."""
        path = self.path(sha)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Discarding unreadable cache entry %s: %s", path, exc)
            return None
        if entry.get("fingerprint") != self.fingerprint:
            log.info("Stage %s: settings changed for %s, recomputing", self.stage, sha[:12])
            return None
        return entry.get("payload")

    def put(self, sha: str, payload: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {"sha256": sha, "stage": self.stage,
                 "fingerprint": self.fingerprint, "payload": payload}
        # Write to a temp file in the same directory, then replace: an interrupted run must
        # never leave a half-written JSON that the next run would read as complete.
        handle, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, ensure_ascii=False, default=str)
            os.replace(tmp, self.path(sha))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def has(self, sha: str) -> bool:
        return self.get(sha) is not None

    def clear(self, sha: str) -> None:
        self.path(sha).unlink(missing_ok=True)
