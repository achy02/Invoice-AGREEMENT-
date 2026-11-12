"""Rebuild tests/golden_msa/commercials.json from the authoritative hand-extraction.

Why this script exists
----------------------
There are two hand-extractions of the same four MSAs, and the golden file was built from
the wrong one.

    TBX_MSA_Extraction.md       82 rows, record ids AR-C01.., carries the standard rate
                                beside the agreed one and a pixel box for every value
    Transbnk_MSA_Extraction.md  101 rows, record ids COM-001.., no standard rate, no boxes

The 19-row gap is not missing charges. Comparing them row by row, every Transbnk row
absent from TBX is either a **commercial term** (payment window, invoice objection window,
liability cap, discontinuation notice) or an **unpriced scope item** ("No rate in MSA or
offer letter"). Neither is a rate, and both belong on the Summary sheet. In the other
direction TBX is the finer of the two: where Transbnk has one Birla "VAPT" row, TBX has
four -- the included annual one, the reimbursable extra, and the two Infra-option rates.

So 82 is the right denominator for a sheet of rates and charges, and scoring against 101
was charging the pipeline for 19 rows it was never meant to produce.

This is a script rather than a one-off edit because the golden file is the definition of
correct: it has to be reproducible from the document it claims to encode, and a diff has
to be explicable. Run it again after any edit to the markdown.

    python -m scripts.build_golden_msa            rewrite the golden file
    python -m scripts.build_golden_msa --check    compare, change nothing (exit 1 on drift)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "golden_msa" / "commercials.json"

#: Where the hand-extraction lives. Outside the repo on purpose: it is an executed-contract
#: derivative, so it is not committed. Override with the first positional argument.
DEFAULT_SOURCE = Path.home() / "Downloads" / "TBX_MSA_Extraction.md"

#: Client code <- a distinctive fragment of the name as the markdown writes it.
CODES = {"aar8": "AAR8", "birla": "ABFL", "ninjamonk": "NINJ", "global": "JSGF"}

#: Client code -> the fragment of the PDF filename that identifies it, for the eval harness.
FILES = {"ABFL": "Aditya Birla", "NINJ": "Bahi Khata",
         "JSGF": "J S Global", "AAR8": "AAR8"}


def table_rows(text: str, start: str, end: str) -> list[list[str]]:
    """Every data row of the one markdown table between two headings."""
    lines = text.splitlines()
    try:
        first = next(i for i, l in enumerate(lines) if re.match(start, l))
        last = next(i for i, l in enumerate(lines[first + 1:], first + 1) if re.match(end, l))
    except StopIteration:
        raise SystemExit(f"could not find the section between {start!r} and {end!r}")
    return [[cell.strip() for cell in line.strip("|").split("|")]
            for line in lines[first:last]
            if line.startswith("|") and not re.match(r"^\|\s*[-:]", line)]


def code_for(name: str) -> str | None:
    lowered = name.lower()
    return next((code for key, code in CODES.items() if key in lowered), None)


def number(cell: str):
    """The cell's value as a number, or None when the document prints no number.

    None is a real answer here, not a gap. More than half these rows are "Waived off",
    "On reimbursement basis" or "To be borne by Corporate/Bank", and a pipeline that
    reports a number for one of those is wrong -- `eval_msa` scores it as `invented`.
    """
    cleaned = (cell or "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return int(value) if value == int(value) else value


def box(cell: str):
    """`[x0, y0, x1, y1]` in pixels, or None. This is what makes grounding scorable."""
    found = re.findall(r"-?\d+", cell or "")
    return [int(n) for n in found[:4]] if len(found) >= 4 else None


def build(source: Path) -> dict:
    text = source.read_text(encoding="utf-8")
    rows = table_rows(text, r"^## Commercials", r"^## Checks")
    if not rows:
        raise SystemExit(f"no commercials table in {source}")
    header, body = rows[0], rows[1:]
    if "Rate as Written" not in " | ".join(header):
        raise SystemExit(f"{source} does not look like the TBX extraction: {header[:4]}")

    charges = []
    for row in body:
        if len(row) < 18:
            continue
        code = code_for(row[1])
        if code is None:
            print(f"  skipped: no client code for {row[1]!r}", file=sys.stderr)
            continue
        charges.append({
            "id": row[14] or f"{code}-C{len(charges) + 1:02d}",
            "c": code,
            "product": row[4],
            "sub": row[5],
            "type": row[3],
            "standard": number(row[8]),
            "amount": number(row[7]),
            "unit": row[6],
            "as_written": row[11],
            "free_limit": row[12] or None,
            "page": number(row[15]),
            "section": row[16],
            "bbox": box(row[17]),
        })

    return {
        "_source": f"{source.name}, section 'Commercials (all {len(charges)} rates and "
                   f"charges)', record ids {charges[0]['id']}..{charges[-1]['id']}",
        "_note": "amount = the md's 'Rate (INR)'. null means the document names a charge "
                 "but prints no number ('Waived off', 'On reimbursement basis'), which is "
                 "the correct answer and not a miss -- eval_msa scores a number there as "
                 "'invented'. standard = the md's 'Standard Rate (INR)', present where a "
                 "list price is printed beside the offered one. bbox = the value's pixel "
                 "box on the rendered page, for scoring grounding rather than only values.",
        "_supersedes": "Transbnk_MSA_Extraction.md (101 rows). Its extra rows are "
                       "commercial terms and unpriced scope items, not rates; see the "
                       "module docstring of scripts/build_golden_msa.py.",
        "files": FILES,
        "charges": charges,
    }


def main(argv: list[str]) -> int:
    check = "--check" in argv
    positional = [a for a in argv if not a.startswith("-")]
    source = Path(positional[0]) if positional else DEFAULT_SOURCE
    if not source.is_file():
        print(f"No hand-extraction at {source}.\n"
              f"Pass its path: python -m scripts.build_golden_msa <path to the .md>")
        return 1

    built = build(source)
    rendered = json.dumps(built, indent=1, ensure_ascii=False) + "\n"

    per_client: dict[str, int] = {}
    for charge in built["charges"]:
        per_client[charge["c"]] = per_client.get(charge["c"], 0) + 1
    boxed = sum(1 for c in built["charges"] if c["bbox"])
    numbered = sum(1 for c in built["charges"] if c["amount"] is not None)

    print(f"{len(built['charges'])} charge rows from {source.name}")
    for code, count in sorted(per_client.items()):
        print(f"   {code}  {count:>3}")
    print(f"   {numbered} carry a number, {len(built['charges']) - numbered} correctly "
          f"carry none; {boxed} have a pixel box")

    if check:
        if not OUT.is_file():
            print(f"\n{OUT} does not exist yet.")
            return 1
        current = json.loads(OUT.read_text(encoding="utf-8"))
        same = current.get("charges") == built["charges"]
        print(f"\n{'up to date' if same else 'DRIFTED from the markdown -- rerun without --check'}")
        return 0 if same else 1

    if OUT.is_file():
        backup = OUT.with_suffix(".json.superseded")
        backup.write_text(OUT.read_text(encoding="utf-8"), encoding="utf-8")
        old = json.loads(backup.read_text(encoding="utf-8")).get("charges", [])
        print(f"\nprevious {len(old)}-row file kept at {backup.name}")
    OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
