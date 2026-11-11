"""Console and file logging, plus the Run Log the workbook carries.

The Run Log is part of the deliverable, not a debugging aid: a 1000-document run that
reports "done" without saying which files failed, how long stages took, and what settings
were used cannot be audited. Every record here ends up on the Run Log sheet.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def setup(level: str = "INFO", log_file: Path | str | None = None) -> logging.Logger:
    """Configure the root logger once; repeated calls replace the handlers."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(name)-22s %(message)s",
                            datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    # Indian agreements carry Devanagari stamp headers, and a Windows console is cp1252;
    # without this, logging a page's text would raise UnicodeEncodeError.
    try:
        console.stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # Paddle and its dependencies log a screenful per page at INFO.
    for noisy in ("paddle", "paddlex", "ppocr", "PIL", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root


@dataclass
class StageRecord:
    stage: str
    file_name: str
    status: str            # OK | CACHED | FAILED | SKIPPED
    seconds: float = 0.0
    detail: str = ""


@dataclass
class RunLog:
    """Collected per file per stage, and written to the Run Log sheet at the end."""

    started: float = field(default_factory=time.time)
    records: list[StageRecord] = field(default_factory=list)

    def add(self, stage: str, file_name: str, status: str, seconds: float = 0.0,
            detail: str = "") -> StageRecord:
        record = StageRecord(stage, file_name, status, round(seconds, 2), detail)
        self.records.append(record)
        return record

    def failures(self) -> list[StageRecord]:
        return [r for r in self.records if r.status == "FAILED"]

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.status] = counts.get(record.status, 0) + 1
        parts = ", ".join(f"{status} {n}" for status, n in sorted(counts.items()))
        return f"{len(self.records)} stage runs in {time.time() - self.started:.1f}s ({parts})"
