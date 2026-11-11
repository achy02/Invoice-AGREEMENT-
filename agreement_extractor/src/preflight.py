"""Check that the environment can actually run the pipeline, and say precisely what cannot.

This exists because two of the three problems that cost the most time on this project were
environment problems that reported themselves badly:

* PaddlePaddle without VCOMP140.DLL fails with "DLL load failed while importing libpaddle",
  which names no missing file;
* Paddle's oneDNN backend fails per page rather than at startup, so an archive OCRs to
  nothing while the run appears healthy.

A check that runs in seconds and names the actual fix is worth more than either discovery.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def python_version() -> Check:
    major, minor = sys.version_info[:2]
    ok = (3, 10) <= (major, minor) < (3, 14)
    return Check(
        "Python", ok, f"{major}.{minor}.{sys.version_info[2]}",
        "" if ok else "PaddlePaddle has no wheel for 3.14; use Python 3.13 (.venv313).",
    )


def libraries() -> list[Check]:
    wanted = {
        "pymupdf": "pip install pymupdf==1.28.2",
        "pdfplumber": "pip install pdfplumber",
        "rapidfuzz": "pip install rapidfuzz",
        "openpyxl": "pip install openpyxl",
        "yaml": "pip install PyYAML",
        "PIL": "pip install pillow",
        "numpy": "pip install numpy",
    }
    out = []
    for module, fix in wanted.items():
        found = importlib.util.find_spec(module) is not None
        out.append(Check(module, found, "installed" if found else "missing", "" if found else fix))
    return out


def paddle() -> list[Check]:
    """Import paddle for real -- a find_spec would miss the DLL failure entirely."""
    if importlib.util.find_spec("paddle") is None:
        return [Check("paddlepaddle", False, "not installed",
                      "pip install -r requirements-ocr.txt (Python 3.13 only)")]
    try:
        import paddle as paddle_module
    except Exception as exc:
        detail = str(exc)
        fix = ("Install the MSVC runtime, which supplies VCOMP140.DLL:\n"
               "      winget install --id Microsoft.VCRedist.2015+.x64"
               if "DLL load failed" in detail else "")
        return [Check("paddlepaddle", False, detail.splitlines()[0][:160], fix)]

    checks = [Check("paddlepaddle", True, paddle_module.__version__)]
    if importlib.util.find_spec("paddleocr") is None:
        checks.append(Check("paddleocr", False, "not installed", "pip install paddleocr"))
        return checks

    import paddleocr

    checks.append(Check("paddleocr", True, paddleocr.__version__))
    for name in ("PPStructureV3", "SealRecognition", "TableStructureRecognition"):
        present = hasattr(paddleocr, name)
        checks.append(Check(f"paddleocr.{name}", present,
                            "available" if present else "not in this release",
                            "" if present else "Upgrade paddleocr to 3.x."))
    return checks


def onednn_disabled() -> Check:
    """oneDNN on means every OCR call fails, one page at a time, with no run-level error."""
    import os

    value = os.environ.get("FLAGS_use_mkldnn")
    ok = value == "0"
    return Check(
        "oneDNN disabled", ok, f"FLAGS_use_mkldnn={value!r}",
        "" if ok else ("Paddle 3.3.1 fails every inference call through oneDNN. "
                       "Set FLAGS_use_mkldnn=0 before paddle is imported."),
    )


def llm(config: Config) -> list[Check]:
    """Is a Qwen3 model actually served at the configured endpoint?"""
    url = config.llm.endpoint.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return [Check("LLM endpoint", False, f"{config.llm.endpoint} unreachable ({exc})",
                      "Install Ollama from https://ollama.com, then:\n"
                      f"      ollama pull {config.llm.model}")]

    models = [m.get("name", "") for m in payload.get("models", [])]
    wanted = config.llm.model
    present = any(name == wanted or name.startswith(wanted.split(":")[0]) for name in models)
    return [
        Check("LLM endpoint", True, f"{config.llm.endpoint} ({len(models)} model(s))"),
        Check(f"model {wanted}", present,
              "available" if present else f"not pulled; have: {', '.join(models) or 'none'}",
              "" if present else f"ollama pull {wanted}"),
    ]


def paths(config: Config) -> list[Check]:
    out = [Check("input folder", config.paths.input.is_dir(), str(config.paths.input),
                 "" if config.paths.input.is_dir() else "Create it, or pass --input <folder>.")]
    pdfs = list(config.paths.input.rglob("*.pdf")) if config.paths.input.is_dir() else []
    out.append(Check("agreement PDFs", bool(pdfs), f"{len(pdfs)} found"))

    golden = config.paths.golden
    golden_pdfs = list(golden.rglob("*.pdf")) if golden.is_dir() else []
    annotations = golden / "source_map_annotations.json"
    out.append(Check(
        "golden set", bool(golden_pdfs) and annotations.is_file(),
        f"{len(golden_pdfs)} PDF(s), annotations {'present' if annotations.is_file() else 'missing'}",
        "" if golden_pdfs and annotations.is_file() else
        f"eval.py cannot run without it. Put the 4 offer-letter PDFs and\n"
        f"      source_map_annotations.json in {golden}",
    ))
    return out


def run(config: Config) -> list[Check]:
    return [python_version(), *libraries(), *paddle(), onednn_disabled(),
            *llm(config), *paths(config)]


def report(config: Config) -> bool:
    """Print every check. Returns True when nothing required is broken."""
    checks = run(config)
    width = max(len(c.name) for c in checks)
    print("\nEnvironment check\n" + "=" * 70)
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        print(f"  [{mark}] {check.name:<{width}}  {check.detail}")
        if check.fix:
            for line in check.fix.splitlines():
                print(f"         -> {line}")
    failed = [c for c in checks if not c.ok]
    print("=" * 70)
    print(f"{len(checks) - len(failed)} of {len(checks)} checks passed.\n")
    return not failed
