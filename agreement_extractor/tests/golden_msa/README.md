# Golden set — master service agreements

This folder is intentionally empty in the repository, for the same reason as
`../golden`: every file in it derives from an executed client MSA and carries
counterparty names, negotiated rates, GST and stamp-certificate numbers.

An MSA is scored separately from an offer letter because it is a different
document. The offer-letter set asks "did you find this rate, and is the number
right". An MSA asks that too, but a large share of its ground-truth lines have
*no* number — "Chargeable beyond free limit", "On reimbursement basis", "To be
borne by Corporate/Bank", "Waived off", and services listed with no price
anywhere. A row that reports a number for one of those is wrong in a way the
offer-letter harness cannot express, so `scripts/eval_msa.py` counts it as its
own failure rather than quietly scoring it as a hit.

## What belongs here

| Path | What it is |
|---|---|
| `*.pdf` | The four executed MSAs (AAR8, Aditya Birla, Ninjamonk, J S Global) |
| `commercials.json` | Ground truth: every charge line, keyed by client code, with page and section |
| `abfl_commercials.json` | The Aditya Birla subset, kept for iterating on one document |

`commercials.json` carries a `files` map from client code (`ABFL`, `NINJ`,
`JSGF`, `AAR8`) to a substring of the PDF filename, which is how a scored row is
tied back to the document it came from. Renaming a PDF without updating that map
makes the document score zero.

## Restoring it

Copy the folder from the source archive onto this path. Then:

    python -m scripts.run_pipeline --input tests/golden_msa
    python -m scripts.eval_msa

`eval_msa` reads the extraction the pipeline leaves in the QA cache, so the
pipeline has to run first. Pass client codes to score a subset:

    python -m scripts.eval_msa ABFL NINJ
