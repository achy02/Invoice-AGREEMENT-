# Golden set — offer letters

This folder is intentionally empty in the repository.

It holds the hand-verified corpus the offer-letter accuracy tests score against,
and every file in it derives from an executed client offer letter: counterparty
names, negotiated rates, GST numbers and signatory names. None of that is
published, so the contents are git-ignored and live only on the machine that
runs the evaluation.

## What belongs here

| Path | What it is |
|---|---|
| `*.pdf` | The four executed offer letters (ALSD, Bajaj, Capitalneed, Ninjamonk) |
| `MD_TBX_Offer_Letters_Extraction.md` | Ground truth: every field and charge, with its page and section |
| `TBX_Offer_Letters_Extraction.xlsx` | The same ground truth as the workbook the business works from |
| `source_map_annotations.json` | One record per extracted cell, with the pixel box it came from |
| `paddleocr_Label.txt` | PaddleOCR detection/recognition labels for the page images |
| `paddleocr_kie_xfund_style.txt` | The same pages as key/answer links, XFUND style |
| `TBX_pages/*.png` | 200 DPI page renders the boxes are measured against |
| `qa_overlays/*.jpg` | Those renders with the boxes drawn, for visual checking |

## Restoring it

Copy the folder from the source archive onto this path. Then:

    python -m scripts.eval

The eval refuses to score rather than guess if the corpus is missing, so a
clone without these files fails loudly instead of reporting a false number.
