# Agreement extraction pipeline

Extracts structured data from client agreements and offer letters into Excel, with full
provenance — file, page, section and pixel bounding box — for every value. Everything runs
locally: PaddleOCR for pixels, Qwen3 for meaning, ordinary code for every number and
coordinate. No cloud APIs.

This is a **separate pipeline from the invoice extractor**. It shares no code with `app/`,
imports nothing from it, writes its own workbook, and runs in its own interpreter.

---

## Removing this pipeline

If you ever want the invoice extractor on its own, the whole of it comes out in one step:

```powershell
Remove-Item -Recurse -Force agreement_extractor
Remove-Item -Recurse -Force .venv313          # the Python 3.13 environment it needs
```

Then delete two things that only point at it:

1. the **"Agreements — a separate pipeline"** section of the root `README.md`;
2. the block in `.gitignore` headed *"Agreement pipeline (self-contained; delete this block
   with the folder)"*.

Nothing else refers to it. `app/`, `tools/`, `tests/`, `main.py`, `requirements.txt` and the
root `.venv` are untouched by this folder, and the invoice test suite passes without it.
That separation is deliberate and is enforced by a test — see `tests/test_separation.py`.

---

## Why it is separate

Invoices and agreements look like the same problem and are not:

| | Invoices | Agreements |
|---|---|---|
| Source | billing software | often a scan of signed paper |
| Text layer | always present | **absent on 47 of 50 pages** in the real archive |
| OCR | never needed | the main path |
| Structure | a table with known columns | prose, with charges buried in Terms & Conditions |
| Extraction | deterministic rules | a local LLM, with every value grounded back to pixels |
| Python | 3.14 (`.venv`) | 3.13 (`.venv313`) — PaddlePaddle has no 3.14 wheel |

Merging them would force the invoice pipeline onto an older interpreter and a heavy native
dependency it has no use for.

---

## Environment

```powershell
py -3.13 -m venv .venv313
.venv313\Scripts\pip install -r requirements.txt -r agreement_extractor\requirements.txt
winget install --id Microsoft.VCRedist.2015+.x64    # supplies VCOMP140.DLL
winget install --id Ollama.Ollama
ollama pull qwen3:8b
```

Check it before running anything:

```powershell
cd agreement_extractor
..\.venv313\Scripts\python -m scripts.run_pipeline --check
```

Three Windows traps, each of which reports itself badly and is therefore a preflight check:

| Symptom | Cause |
|---|---|
| `DLL load failed while importing libpaddle` | `VCOMP140.DLL` (the MSVC OpenMP runtime) is missing. Not on a stock Windows install, not bundled in the wheel, and the error names no file. |
| OCR "runs" but every page comes back empty | PaddlePaddle 3.3.1's oneDNN backend fails **per inference call**, not at startup, so the run looks healthy. `FLAGS_use_mkldnn=0` must be set before paddle is imported. |
| `import pymupdf` exits `-1073741819`, no traceback | pymupdf 2.0 segfaults on Python 3.13. Pinned to 1.28.2. |

---

## Running it

```powershell
cd agreement_extractor
..\.venv313\Scripts\python -m scripts.serve                       # the viewer, port 8100
..\.venv313\Scripts\python -m scripts.run_pipeline --input <folder>
..\.venv313\Scripts\python -m scripts.overlay --golden --doc Bajaj --page 4
..\.venv313\Scripts\python -m scripts.eval
```

### The viewer

`python -m scripts.serve` opens a local page at **http://127.0.0.1:8100** for checking a
document by eye. It is a separate application from the invoice web app on port 8000; both
can run at once.

A spreadsheet can tell you a rate came out as `85` and that something flagged it. It cannot
show you the `0.85` printed on the page, which is the only thing that settles whether OCR
misread the document or the model misread the OCR. So the viewer answers those two
questions on separate tabs, because they fail for different reasons:

| Tab | Answers |
|---|---|
| **Did OCR read the page?** | Which route each page took (`text_layer` or `paddle_ocr`), its mean confidence, and every line found — click one to light up its box on the page image. Ruled tables are shown as a grid with their cell ids. |
| **Did the model pick the right values?** | Each field and charge line with its value, the verbatim quote behind it, the page it cited, and whether that quote was located. Click a row to see the matched box, with its label in green. `UNGROUNDED` rows are shown, not hidden — that is the failure you most want to see. |
| **What was flagged** | S6's findings, worst first. |

Upload PDFs, or run the four golden offer letters or the sample agreements in place. The
workbook is downloadable from the same page.

Everything the viewer needs lives in `web/`, so the removal step above takes it too.

Stages, each caching JSON per file SHA-256 so a stopped run resumes and a re-run is nearly
free:

```
S0 inventory   hash, page count, per-page text-layer detection, duplicates
S1 render      every page to PNG at 200 dpi -- the coordinate system for every box
S2 text/layout lines, tokens, tables, seals, signature evidence
S3 llm_input   reading-order text with line ids; tables as markdown with cell ids
S4 extract     two Qwen3 calls per document: values + verbatim evidence, never boxes
S5 ground      match each evidence quote back to tokens to find its box
S6 validate    arithmetic, dates, signed status, contradictions -> flags
S7 export      the workbook, per-document JSON, QA overlays
```

### Measured on the golden set

| | result | target |
|---|---|---|
| pages read | 14/14 — 9 text layer, 5 OCR, 0 unreadable | — |
| OCR confidence | 0.98–1.00 mean | — |
| evidence quote located | 100/101 (99%) | — |
| **bbox IoU ≥ 0.5** | **97/101 (96%)** | ≥ 95% |
| … via text layer | 73/76 (96%) | |
| … via OCR | 24/25 (96%) | |

The four misses are honest limits rather than tuning gaps: ALSD's "Director" is printed
inside a stamp image and needs seal OCR (an optional PaddleOCR dependency that is not
installed here); two `Signed / Unsigned` records score 0.41 and 0.43 because their golden
box is a hand-drawn *region* spanning two signature columns, including the whitespace
between them, rather than a quote.

Re-running a processed folder costs 0.1 s instead of 176 s — every stage is cached by file
hash and invalidated by the settings it depended on.

### The rule that holds it together

**The LLM never produces coordinates.** It returns values and verbatim quotes; code finds
the boxes by matching those quotes against the tokens recorded in S2. If a quote cannot be
found on the page it claims, the value is marked `UNGROUNDED` and goes to the review queue
instead of the workbook. That check is what stops a fluent model inventing a plausible rate
— the same principle as the invoice side's "never invent a value", carried into a setting
where the extractor can hallucinate.

### Text layer first, OCR per page

OCR on a page that already has a clean text layer is slower *and* worse: it re-derives from
pixels what the PDF states exactly, and misreads digits in rates and dates on the way. So
each page is graded on its own.

| Page | Source | Result |
|---|---|---|
| text layer passes the gate | `text_layer` | used, with character-level geometry |
| it does not, OCR ran | `paddle_ocr` | OCR text with a confidence score |
| it does not, OCR unavailable | `none` | blank, and flagged |

### Signed or unsigned

Whether a document was signed cannot be settled from its prose, so evidence is graded, not
decided: a PDF signature widget or an e-sign envelope id is structural; a seal or a
"Digitally signed by" block is strong but is page content; a typed name under a signatory
caption is also what an *unsigned* template looks like. S6 turns the collection into
SIGNED / PARTIAL / UNSIGNED / UNCERTAIN, and anything below SIGNED is review-flagged.

---

## The golden set

`tests/golden/` holds four hand-verified offer letters and 111 labelled cells — the
definition of correct. It is itself verified on load (`src/golden.py`), because an accuracy
figure measured against a broken reference is worse than no figure.

- 111 records = 48 Summary + 63 Commercials
- 63 Commercials records cover **58 charge lines**; five Capitalneed rows carry an extra
  standard-rate or free-limit cell. Charge-line recall is counted over rows, not records.
- 10 deliberate **null** examples (Stamp Date ×4, Effective Date ×4, Billing Model, Payment
  Terms). A model that invents an Effective Date is wrong even though the document looks
  like it has one.
- 26 records came from OCR rather than a text layer — the subset that decides whether
  PaddleOCR is good enough.

Two quirks worth knowing: the annotations spell file names with underscores while the PDFs
on disk use spaces (matching goes through `normalise_name`), and the golden page images
were rendered with `pdftoppm`, which is one pixel wider than PyMuPDF on three Bajaj pages
because of floating-point rounding. Golden boxes are therefore normalised against the
golden image's own size.
