"""Score the pipeline against the hand-labelled MSA ground truth (82 charge lines).

Separate from `eval.py` because an MSA is a different document to an offer letter and is
graded differently. The offer-letter set asks "did you find this rate and is the number
right". An MSA asks that too, but 26 of its 82 golden lines *have no number* --
"Chargeable beyond free limit", "On reimbursement basis", "To be borne by Corporate/Bank",
"Waived off", and services listed with no price anywhere. A row that reports a number
for those is wrong in a way the offer-letter set cannot express, so it is scored here as
its own failure rather than quietly counted as a hit.

The denominator was 101 until the two hand-extractions of these same four agreements were
compared: the extra 19 rows were commercial terms and unpriced scope items rather than
rates, so the pipeline was being charged for rows this sheet never asks it to produce.
`scripts/build_golden_msa.py` rebuilds the file and records the reasoning.

Matching is fuzzy on the charge head, because the ground truth is written by a person
("Stamp paper logistic charges") and the model copies the document ("Stamp Paper logistic
charges"). Exact equality would measure transcription, not extraction.

    python -m scripts.eval_msa              all four documents
    python -m scripts.eval_msa ABFL NINJ    only these
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as config_module                      # noqa: E402
from src.validate import parse_number                        # noqa: E402

GOLDEN = ROOT / "tests" / "golden_msa" / "commercials.json"
SPLITS = ROOT / "tests" / "splits.json"


def load_splits() -> dict[str, str]:
    """Map each client code to "tune" or "holdout".

    Reported apart because they answer different questions. The tune number says how well
    the rules fit the documents they were written against; only the holdout number says
    anything about the next agreement. Quoting the first as if it were the second is the
    easiest way to overstate this pipeline, so the harness refuses to merge them.
    """
    if not SPLITS.is_file():
        return {}
    data = json.loads(SPLITS.read_text(encoding="utf-8")).get("msa", {})
    return {code: split for split in ("tune", "holdout") for code in data.get(split, [])}


def normalise(text) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower()).strip()


def similarity(a: str, b: str) -> float:
    from rapidfuzz import fuzz

    a, b = normalise(a), normalise(b)
    if not a or not b:
        return 0.0
    return max(fuzz.token_set_ratio(a, b), fuzz.partial_ratio(a, b)) / 100.0


def head_of(item: dict) -> str:
    charge = item.get("charge") or {}
    head = f"{charge.get('product') or ''} {charge.get('sub_product') or ''}".strip()
    return head or (item.get("field") or "")


def best_match(golden: dict, produced: list[dict], floor: float = 0.72):
    target = f"{golden['product']} {golden['sub']}"
    scored = [(similarity(target, head_of(i)), i) for i in produced]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored and scored[0][0] >= floor:
        return scored[0][1]
    return None


def amounts_agree(wanted, got) -> bool:
    if wanted is None:
        return got is None or str(got).strip() == ""
    number = parse_number(got)
    return number is not None and abs(number - float(wanted)) < 0.005


def score(code: str, produced: list[dict], wanted: list[dict]) -> dict:
    charges = [c for c in produced if (c.get("sheet") or "") == "Commercials"]
    used: set[int] = set()
    found, right, wrong, invented, right_page = [], [], [], [], []
    missing = []

    for golden in wanted:
        pool = [c for n, c in enumerate(charges) if n not in used]
        item = best_match(golden, pool)
        if item is None:
            missing.append(golden)
            continue
        used.add(charges.index(item))
        found.append(golden)
        got = (item.get("charge") or {}).get("rate_inr")
        if amounts_agree(golden["amount"], got):
            right.append(golden)
        elif golden["amount"] is None:
            invented.append((golden, got))
        else:
            wrong.append((golden, got))
        if item.get("page") == golden["page"]:
            right_page.append(golden)

    return {"code": code, "wanted": wanted, "produced": len(charges), "found": found,
            "right": right, "wrong": wrong, "invented": invented,
            "right_page": right_page, "missing": missing}


def show(result: dict, detail: bool = True) -> None:
    total = len(result["wanted"])
    if not total:
        return
    label = result["code"] + (f" [{result['split']}]" if result.get("split") else "")
    print(f"\n{' ' + label + ' ':=^78}")
    print(f"  golden lines : {total:>3}      produced : {result['produced']}")
    print(f"  matched      : {len(result['found']):>3}/{total}  ({len(result['found'])/total:.0%})")
    print(f"  amount right : {len(result['right']):>3}/{total}  ({len(result['right'])/total:.0%})")
    print(f"  page right   : {len(result['right_page']):>3}/{total}  ({len(result['right_page'])/total:.0%})")
    print(f"  invented     : {len(result['invented']):>3}      (document states no amount)")

    tally: dict[str, list[int]] = {}
    hits = {g["id"] for g in result["found"]}
    for golden in result["wanted"]:
        row = tally.setdefault(golden["section"], [0, 0])
        row[1] += 1
        row[0] += 1 if golden["id"] in hits else 0
    print("\n  by section:")
    for section, (hit, out_of) in sorted(tally.items(), key=lambda kv: -kv[1][1]):
        print(f"    {section:<32} {hit:>2}/{out_of:<3} {hit/out_of:>5.0%}")

    if detail and result["missing"]:
        print(f"\n  {len(result['missing'])} not found:")
        for golden in result["missing"][:18]:
            print(f"    {golden['id']}  p{golden['page']:<3} {golden['product'][:26]} / {golden['sub'][:38]}")
    if detail and result["wrong"]:
        print(f"\n  {len(result['wrong'])} wrong amount:")
        for golden, got in result["wrong"][:12]:
            print(f"    {golden['id']}  want {golden['amount']!s:<10} got {str(got)[:22]:<22} {golden['sub'][:30]}")
    if detail and result["invented"]:
        print(f"\n  {len(result['invented'])} invented a number:")
        for golden, got in result["invented"][:12]:
            print(f"    {golden['id']}  got {str(got)[:14]:<14} for {golden['sub'][:46]}")


def main(argv: list[str]) -> int:
    config = config_module.load()
    path = config.paths.cache / "qa" / "extracted.json"
    if not path.is_file():
        print(f"No {path}. Run the pipeline over tests/golden_msa first:")
        print("  python -m scripts.run_pipeline --input tests/golden_msa")
        return 1

    truth = json.loads(GOLDEN.read_text(encoding="utf-8"))
    produced = json.loads(path.read_text(encoding="utf-8"))
    wanted_codes = [a.upper() for a in argv] or list(truth["files"])
    splits = load_splits()

    def blank() -> dict:
        return {"wanted": 0, "found": 0, "right": 0, "page": 0, "invented": 0, "produced": 0}

    by_split: dict[str, dict] = {"tune": blank(), "holdout": blank(), "unassigned": blank()}
    totals = blank()
    for code in truth["files"]:
        if code not in wanted_codes:
            continue
        split = splits.get(code, "unassigned")
        marker = normalise(truth["files"][code])
        mine = [r for r in produced if marker in normalise(r.get("file"))]
        rows = [g for g in truth["charges"] if g["c"] == code]
        if not mine:
            print(f"\n{' ' + code + ' ':=^78}")
            print(f"  nothing extracted -- is {truth['files'][code]!r} in tests/golden_msa?")
            totals["wanted"] += len(rows)
            by_split[split]["wanted"] += len(rows)
            continue
        result = score(code, mine, rows)
        result["split"] = split
        show(result)
        for bucket in (totals, by_split[split]):
            bucket["wanted"] += len(rows)
            bucket["found"] += len(result["found"])
            bucket["right"] += len(result["right"])
            bucket["page"] += len(result["right_page"])
            bucket["invented"] += len(result["invented"])
            bucket["produced"] += result["produced"]

    def report(title: str, bucket: dict) -> None:
        n = max(bucket["wanted"], 1)
        print(f"\n{' ' + title + ' ':=^78}")
        print(f"  golden lines : {bucket['wanted']:>3}      produced : {bucket['produced']}")
        print(f"  matched      : {bucket['found']:>3}/{bucket['wanted']}  ({bucket['found']/n:.1%})")
        print(f"  amount right : {bucket['right']:>3}/{bucket['wanted']}  ({bucket['right']/n:.1%})")
        print(f"  page right   : {bucket['page']:>3}/{bucket['wanted']}  ({bucket['page']/n:.1%})")
        print(f"  invented     : {bucket['invented']:>3}      (document states no amount)")

    for name, title in (("tune", "TUNE SPLIT (rules were written against these)"),
                        ("holdout", "HOLDOUT SPLIT (the number that predicts new paper)"),
                        ("unassigned", "UNASSIGNED (not named in tests/splits.json)")):
        if by_split[name]["wanted"]:
            report(title, by_split[name])

    report("ALL DOCUMENTS", totals)
    if by_split["tune"]["wanted"] and by_split["holdout"]["wanted"]:
        tune_rate = by_split["tune"]["right"] / max(by_split["tune"]["wanted"], 1)
        held_rate = by_split["holdout"]["right"] / max(by_split["holdout"]["wanted"], 1)
        print(f"\n  tune {tune_rate:.1%} vs holdout {held_rate:.1%} -- a gap here is how far "
              f"the\n  headline number overstates performance on an unseen agreement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
