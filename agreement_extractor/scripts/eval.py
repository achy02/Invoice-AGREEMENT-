"""Measure the pipeline against the golden set.

    python -m scripts.eval                 everything that can run
    python -m scripts.eval --grounding     S5 only, using the golden values as input

The grounding-only mode matters: it separates "can code find a value's box once it is
given the value" from "can the model read the document". Those fail for different reasons
and mixing them hides both. It also runs without an LLM, so S5 can be tuned to target
before Qwen3 is in the picture at all.

Reported per the build plan:
  * value accuracy (exact after normalisation; numeric equality for rates)
  * null accuracy (correctly returned null for Stamp Date / Effective Date, etc.)
  * charge-line recall and precision, counted over the 58 rows
  * bbox IoU >= 0.5 rate for grounded fields
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src import config as config_module  # noqa: E402
from src import golden as G  # noqa: E402
from src import ground  # noqa: E402
from src.models import BBox, Extracted  # noqa: E402
from src.pipeline import _page_from_dict  # noqa: E402

IOU_PASS = 0.5


def iou(a: BBox, b: BBox) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def load_pages(config, file_name: str):
    """The S2 page JSON for one document, keyed by the name the annotations use."""
    for path in (config.paths.cache / "qa" / "pages").glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        if G.normalise_name(document["file_name"]) == G.normalise_name(file_name):
            return [_page_from_dict(p) for p in document["pages"]]
    return None


def grounding_report(config, gold: G.GoldenSet) -> int:
    """Feed the golden values into S5 and see whether code can re-find their boxes."""
    rows = [r for r in gold.records if not r.is_null and r.value_bbox]
    by_file: dict[str, list] = {}
    for record in rows:
        by_file.setdefault(record.file, []).append(record)

    located = passed = 0
    failures = []
    by_source = {"text_layer": [0, 0], "paddle_ocr": [0, 0]}

    for file_name, records in sorted(by_file.items()):
        pages = load_pages(config, file_name)
        if pages is None:
            print(f"  ! no S2 output for {file_name}; run the pipeline first")
            continue
        page_numbers = {p.number: p for p in pages}

        for record in records:
            item = Extracted(field=record.field, value=record.value,
                             evidence_text=record.value_text, page=record.page,
                             section_heading=record.section, key_text=record.key_text)
            result = ground.ground_field(item, page_numbers, config)
            page = page_numbers.get(record.page)
            source = page.source if page else "none"
            if source in by_source:
                by_source[source][1] += 1

            if result.value_bbox is None:
                failures.append((record, "NOT FOUND", 0.0))
                continue
            located += 1
            # Golden boxes are in the golden image's pixel grid, which differs from our
            # render by at most a pixel on three Bajaj pages; IoU absorbs that.
            score = iou(result.value_bbox, record.value_bbox)
            if score >= IOU_PASS:
                passed += 1
                if source in by_source:
                    by_source[source][0] += 1
            else:
                failures.append((record, f"IoU {score:.2f}", score))

    total = len(rows)
    print(f"\n{'S5 GROUNDING':=^78}")
    print(f"  records with a box   : {total}")
    print(f"  quote located        : {located}  ({located / total:.1%})")
    print(f"  IoU >= {IOU_PASS}          : {passed}  ({passed / total:.1%})"
          f"   target >= 95%")
    for source, (good, n) in by_source.items():
        if n:
            print(f"    via {source:<12}: {good}/{n}  ({good / n:.0%})")

    if failures:
        print(f"\n  {len(failures)} failure(s):")
        for record, why, _ in sorted(failures, key=lambda f: f[2])[:30]:
            print(f"    {record.id:<8} {record.field[:36]:<36} {why:<12} "
                  f"{record.value_text[:40]!r}")
    return passed, total


#: Golden labels S6 grades from evidence rather than asking the model to read.
GRADED_NOT_EXTRACTED = {"Signed / Unsigned": "signed_status"}
#: Golden columns that record a flag, not a value to compare.
NOT_A_VALUE = {"Flag - template placeholder", "Flag - revised commercials"}

#: Fields whose golden value is a human paraphrase rather than what the page says.
#:
#: The annotator wrote "30 days from invoice" where the letter prints "30 days from Invoice
#: issuance.", and "Docuflow via TrustHub (E-sign and E-stamp)" where it prints "Docuflow
#: services through Trusthub Platform". Both readings are right. Demanding an exact match
#: would score the pipeline on its ability to guess one person's phrasing, which is not
#: extraction -- but quietly folding them into one lenient "accuracy" number would hide the
#: fields where exactness genuinely matters. So they are measured separately, by whether the
#: extracted text carries the same fact, and both strings are printed for a human to judge.
PARAPHRASED = {
    "Service / Product Offered", "Payment Terms", "Termination Notice",
    "Client Signatory", "TBX Signatory", "Other Key Terms", "Signature Mode",
}

RATE_SUFFIX = re.compile(r"\s*[-–]\s*(?:standard\s+)?rate$", re.I)


def _summary_index(produced: list[dict]) -> dict:
    """Produced rows keyed by (file, *golden label*).

    The model answers in schema keys -- `client_name` -- while the golden set speaks human
    labels -- `Client Name`. Comparing those two literally scores every correct answer as
    missing, which is exactly how this harness once reported 3% against output that was
    largely right. `export.SUMMARY_FIELDS` is the one place the mapping is defined, so it is
    imported rather than restated: a field renamed on one side and not the other has to
    break loudly instead of reading as a wrong answer.
    """
    from src.export import SUMMARY_FIELDS

    labels = dict(SUMMARY_FIELDS)
    labels.update({field: label for label, field in GRADED_NOT_EXTRACTED.items()})

    index = {}
    for row in produced:
        label = labels.get(row.get("field"))
        if label:
            index[(G.normalise_name(row["file"]), label)] = row
    return index


def _charge_key(text) -> str:
    """What makes two charge lines the same line: the product named, minus the suffix."""
    return ground.normalise(RATE_SUFFIX.sub("", str(text or "")))


def _same_value(got: dict, record) -> bool:
    """Exact after normalisation, on either the verbatim value or S6's canonical form.

    Dates are why both are checked: the page prints "10th - Nov - 2025" and the golden set
    records "10-Nov-2025". The pipeline keeps the verbatim text as the value and puts the
    canonical date in `normalized_value`, so a correct read matches on the second.
    """
    want = ground.normalise(record.value)
    for candidate in (got.get("value"), got.get("normalized_value")):
        if ground.normalise(candidate) == want:
            return True
    try:
        return abs(float(got.get("normalized_value")) - float(record.value)) < 1e-6
    except (TypeError, ValueError):
        return False


def _same_fact(got, want) -> bool:
    """Does the extracted text carry the fact the annotator recorded?

    Deliberately one-directional: the golden paraphrase must be present in what was
    extracted, not the reverse. "30 days" found inside "with advance notice of 30 days."
    is a correct read; "30 days" offered in place of a paragraph that said something else
    would not be.
    """
    from rapidfuzz import fuzz

    got_text, want_text = ground.normalise(got), ground.normalise(want)
    if not got_text or not want_text:
        return False
    # The annotator's own asides are not in the document and cannot be required.
    want_text = ground.normalise(re.sub(r"\([^)]*not printed[^)]*\)", "", str(want), flags=re.I))
    return want_text in got_text or fuzz.partial_ratio(want_text, got_text) >= 90


def _rate_matches(got: dict, record) -> bool:
    if ground.normalise(got.get("value")) == ground.normalise(record.value):
        return True
    for candidate in (got.get("normalized_value"), got.get("value")):
        try:
            if abs(float(candidate) - float(record.value)) < 1e-6:
                return True
        except (TypeError, ValueError):
            continue
    return False


def standard_rate_report(produced: list[dict], gold: G.GoldenSet):
    """The standard (list) rate beside each offered rate, which this harness used to skip.

    Golden ids ending in `S` carry it, and `charge_recall` filters them out because they are
    not separate charge lines. Filtering them out of the *measurement* too meant nobody was
    checking the second of the two prices these letters print -- and the two were in fact
    being read the wrong way round, every rate coming back as the list price. An unmeasured
    field is an unprotected one.
    """
    wanted = [r for r in gold.commercials if r.id.endswith("S")]
    by_file: dict[str, list] = {}
    for row in produced:
        if row.get("sheet") == "Commercials":
            by_file.setdefault(G.normalise_name(row["file"]), []).append(row)

    found = correct = swapped = 0
    misses = []
    for record in wanted:
        key = _charge_key(record.field)
        hit = None
        for candidate in by_file.get(G.normalise_name(record.file), []):
            other = _charge_key(candidate.get("field"))
            if key and other and (key in other or other in key):
                hit = candidate
                break
        if hit is None:
            misses.append((record, "charge line not found"))
            continue
        charge = hit.get("charge") or {}
        got_standard = charge.get("standard_rate_inr")
        got_offered = charge.get("rate_inr")
        if got_standard is None:
            misses.append((record, "no standard rate captured"))
            continue
        found += 1
        if _numbers_equal(got_standard, record.value):
            correct += 1
        elif _numbers_equal(got_offered, record.value):
            swapped += 1
            misses.append((record, f"SWAPPED: {record.value} is in rate_inr, "
                                   f"standard_rate_inr holds {got_standard}"))
        else:
            misses.append((record, f"got {got_standard}"))
    return found, correct, swapped, len(wanted), misses


def _numbers_equal(got, want) -> bool:
    try:
        return abs(float(got) - float(want)) < 1e-6
    except (TypeError, ValueError):
        return ground.normalise(got) == ground.normalise(want)


def charge_recall(produced: list[dict], gold: G.GoldenSet):
    """Charge-line recall, matched on what the line names rather than on a record id.

    Golden ids like `CN-C03` are hand-assigned labels on a reference spreadsheet. Nothing in
    a document tells a model to call a row `CN-C03`, and the pipeline numbers its own rows
    in the order it finds them, so an id-to-id join can only ever score 0% -- which is
    precisely what it did, on output that had found the lines. Two charge lines are the same
    line when they name the same product, so that is what is compared; the rate is then
    scored separately, because finding a line and reading its price right are different
    failures.
    """
    by_file: dict[str, list] = {}
    for row in produced:
        if row.get("sheet") != "Commercials":
            continue
        by_file.setdefault(G.normalise_name(row["file"]), []).append(row)

    wanted = [r for r in gold.commercials if not r.id.endswith(("S", "F"))]
    found = correct = 0
    missing, wrong = [], []
    for record in wanted:
        key = _charge_key(record.field)
        hit = None
        for candidate in by_file.get(G.normalise_name(record.file), []):
            other = _charge_key(candidate.get("field"))
            if key and other and (key in other or other in key):
                hit = candidate
                break
        if hit is None:
            missing.append(record)
            continue
        found += 1
        if _rate_matches(hit, record):
            correct += 1
        else:
            wrong.append((record, hit.get("value")))
    return found, correct, len(wanted), missing, wrong


def extraction_report(config, gold: G.GoldenSet) -> None:
    """Compare S4+S5 output against the golden values. Needs an LLM run to have happened."""
    path = config.paths.cache / "qa" / "extracted.json"
    if not path.is_file():
        print(f"\n{'S4 EXTRACTION':=^78}")
        print(f"  no {path.name} yet -- run the pipeline through S4 first.")
        return

    produced = json.loads(path.read_text(encoding="utf-8"))
    index = _summary_index(produced)

    matched = nulls_right = same_fact = 0
    summary = [r for r in gold.records if r.sheet != "Commercials"
               and r.field not in NOT_A_VALUE]
    expected_nulls = [r for r in summary if r.is_null]
    exact_rows = [r for r in summary if not r.is_null and r.field not in PARAPHRASED]
    text_rows = [r for r in summary if not r.is_null and r.field in PARAPHRASED]
    misses, null_misses, text_misses = [], [], []

    for record in exact_rows:
        got = index.get((G.normalise_name(record.file), record.field))
        if got is None:
            misses.append((record, "NOT EXTRACTED"))
        elif _same_value(got, record):
            matched += 1
        else:
            misses.append((record, f"got {got.get('value')!r}"))

    for record in text_rows:
        got = index.get((G.normalise_name(record.file), record.field))
        if got is not None and _same_fact(got.get("value"), record.value):
            same_fact += 1
        else:
            text_misses.append((record, None if got is None else got.get("value")))

    for record in expected_nulls:
        got = index.get((G.normalise_name(record.file), record.field))
        # A field the pipeline never emits is not evidence that it correctly said "absent".
        if got is None:
            null_misses.append((record, "NOT EXTRACTED -- cannot credit as a null"))
        elif got.get("value") in (None, "", "null"):
            nulls_right += 1
        else:
            null_misses.append((record, f"invented {got.get('value')!r}"))

    found, correct, wanted, missing, wrong = charge_recall(produced, gold)
    print(f"\n{'S4 EXTRACTION':=^78}")
    print(f"  exact fields    : {matched}/{len(exact_rows)} "
          f"({matched / (len(exact_rows) or 1):.1%})   dates, names, status -- target 100%")
    print(f"  paraphrased     : {same_fact}/{len(text_rows)} "
          f"({same_fact / (len(text_rows) or 1):.1%})   same fact, wording may differ")
    print(f"  null accuracy   : {nulls_right}/{len(expected_nulls)} "
          f"({nulls_right / (len(expected_nulls) or 1):.1%})   target 100%")
    print(f"  charge lines    : {found}/{wanted} "
          f"({found / (wanted or 1):.1%})   target 100%")
    print(f"  rate correct    : {correct}/{wanted} "
          f"({correct / (wanted or 1):.1%})   target 100%")

    s_found, s_correct, s_swapped, s_wanted, s_misses = standard_rate_report(produced, gold)
    print(f"  standard rates  : {s_correct}/{s_wanted} "
          f"({s_correct / (s_wanted or 1):.1%})   captured {s_found}"
          + (f", SWAPPED with the client rate {s_swapped}" if s_swapped else ""))

    if misses:
        print(f"\n  {len(misses)} exact-field miss(es):")
        for record, why in misses[:25]:
            print(f"    {record.id:<8} {record.field[:28]:<28} "
                  f"want {str(record.value)[:22]!r} {why[:40]}")
    if text_misses:
        print(f"\n  {len(text_misses)} paraphrased field(s) a human should judge:")
        for record, got in text_misses[:15]:
            print(f"    {record.id:<8} {record.field[:24]:<24}")
            print(f"{'':14}golden {str(record.value)[:54]!r}")
            print(f"{'':14}ours   {str(got)[:54]!r}")
    if null_misses:
        print(f"\n  {len(null_misses)} null miss(es):")
        for record, why in null_misses[:10]:
            print(f"    {record.id:<8} {record.field[:28]:<28} {why[:58]}")
    if missing:
        print(f"\n  {len(missing)} charge line(s) not found:")
        for record in missing[:20]:
            print(f"    {record.id:<8} {record.file[:24]:<24} {record.field[:40]}")
    if wrong:
        print(f"\n  {len(wrong)} charge line(s) found with the wrong rate:")
        for record, got in wrong[:20]:
            print(f"    {record.id:<8} {record.field[:28]:<28} "
                  f"want {str(record.value)[:14]!r} got {str(got)[:14]!r}")
    if s_misses:
        print(f"\n  {len(s_misses)} standard-rate problem(s):")
        for record, why in s_misses[:15]:
            print(f"    {record.id:<9} {record.field[:30]:<30} "
                  f"want {str(record.value)[:10]!r} -- {why[:52]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--grounding", action="store_true", help="S5 only")
    args = parser.parse_args(argv)

    config = config_module.load(args.config)
    gold = G.load(config.paths.golden)

    problems = G.check(gold)
    if problems:
        print("The golden set itself is inconsistent; fix it before measuring:")
        for problem in problems[:10]:
            print(f"  - {problem}")
        return 2

    passed, total = grounding_report(config, gold)
    if not args.grounding:
        extraction_report(config, gold)

    print()
    return 0 if passed / total >= 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(main())
