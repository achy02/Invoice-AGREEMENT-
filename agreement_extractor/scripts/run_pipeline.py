"""Run the agreement extraction pipeline.

    python -m scripts.run_pipeline --check                 verify the environment only
    python -m scripts.run_pipeline --input <folder>        run end to end
    python -m scripts.run_pipeline --stages S0,S1,S2       run part of it
    python -m scripts.run_pipeline --no-resume             ignore the cache

Stages are S0 inventory, S1 render, S2 text/layout, S3 llm_input, S4 extract, S5 ground,
S6 validate, S7 export. Each caches its output per file SHA-256, so a stopped run resumes
where it left off and a re-run with unchanged settings is nearly free.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src import config as config_module  # noqa: E402
from src import logging_setup  # noqa: E402
from src.preflight import report as preflight_report  # noqa: E402

STAGES = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="path to config.yaml")
    parser.add_argument("--input", type=Path, help="override paths.input")
    parser.add_argument("--check", action="store_true",
                        help="report whether OCR and the LLM endpoint are usable, then exit")
    parser.add_argument("--stages", default=",".join(STAGES),
                        help=f"comma-separated subset of {','.join(STAGES)}")
    parser.add_argument("--limit", type=int, help="process at most this many documents")
    parser.add_argument("--no-resume", action="store_true", help="ignore cached stage output")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging_setup.setup(config.logging.level,
                        config.paths.output / Path(config.logging.file).name)

    if args.check:
        return 0 if preflight_report(config) else 1

    unknown = [s for s in args.stages.split(",") if s.strip() and s.strip() not in STAGES]
    if unknown:
        print(f"Unknown stage(s): {unknown}. Valid stages are {STAGES}.", file=sys.stderr)
        return 2
    wanted = {s.strip() for s in args.stages.split(",") if s.strip()}

    from src import pipeline

    run_log = logging_setup.RunLog()
    documents = pipeline.run(config, root=args.input, limit=args.limit,
                             resume=not args.no_resume, run_log=run_log)

    # Under cache/, not output/. These are the OCR dumps that scripts/eval.py measures
    # against the golden set -- developer machinery, not a deliverable. output/ holds the
    # workbook and the run log, and nothing a person has to be told to ignore.
    out_dir = config.paths.cache / "qa" / "pages"
    pipeline.dump_pages(documents, out_dir)

    print(f"\n{'document':<48} {'pages':>5}  {'text':>5} {'ocr':>5} {'none':>5}  source")
    print("-" * 92)
    for document in documents:
        counts = {"text_layer": 0, "paddle_ocr": 0, "none": 0}
        for page in document.pages:
            counts[page.source] = counts.get(page.source, 0) + 1
        note = document.notes[0] if document.notes and not document.pages else ""
        print(f"{document.file_name[:48]:<48} {document.page_count:>5}  "
              f"{counts['text_layer']:>5} {counts['paddle_ocr']:>5} {counts['none']:>5}  {note}")

    print(f"\nOCR dump (for eval.py) written to {out_dir}")

    if wanted & {"S3", "S4", "S5", "S6", "S7"}:
        from src import extract as extract_stage
        from src import export

        ok, why = extract_stage.available(config)
        if not ok:
            print(f"\nS3-S7 skipped: {why}")
            print(f"  -> install Ollama, then: ollama pull {config.llm.model}")
            print(f"\n{run_log.summary()}")
            return 1

        results = pipeline.run_extraction(
            documents, config, run_log=run_log, resume=not args.no_resume)

        print(f"\n{'document':<48} {'fields':>6} {'charges':>8} {'signed':<10} review")
        print("-" * 92)
        for result in results:
            print(f"{result.file_name[:48]:<48} {len(result.fields):>6} "
                  f"{len(result.charges):>8} {result.signed_status:<10} "
                  f"{'YES' if result.review_required else 'no'}")

        # What eval.py compares against: one row per extracted cell, before the workbook's
        # formatting gets in the way.
        rows = []
        for result in results:
            # Signed status is graded by S6 from collected evidence rather than answered by
            # the model, but it is still an extracted value and the golden set labels it, so
            # it belongs in the record dump like any other field.
            rows.append({
                "record_id": "", "sheet": "Summary", "file": result.file_name,
                "field": "signed_status", "value": result.signed_status,
                "normalized_value": result.signed_status, "evidence_text": "",
                "page": None, "value_bbox": None, "status": "OK",
                "match_score": None, "charge": {},
            })
            for item in result.fields + result.charges:
                rows.append({
                    "record_id": getattr(item, "record_id", ""),
                    "sheet": getattr(item, "sheet", "Summary"),
                    "file": result.file_name, "field": item.field, "value": item.value,
                    "normalized_value": item.normalized_value,
                    "evidence_text": item.evidence_text, "page": item.page,
                    "value_bbox": list(item.value_bbox) if item.value_bbox else None,
                    "status": item.status, "match_score": item.match_score,
                    "charge": getattr(item, "charge", {}),
                })
        extracted = config.paths.cache / "qa" / "extracted.json"
        extracted.parent.mkdir(parents=True, exist_ok=True)
        extracted.write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str),
                             encoding="utf-8")

        workbook = export.write_workbook(results, config, run_log)
        print(f"\nworkbook : {workbook}")
        print(f"eval data: {extracted}")

    print(f"\n{run_log.summary()}")
    for failure in run_log.failures():
        print(f"  FAILED {failure.stage} {failure.file_name}: {failure.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
