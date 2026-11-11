"""Entry point.

    python main.py                         start the web app at http://127.0.0.1:8000
    python main.py extract <pdfs or folders> [-o OUTPUT_DIR]
                                           process PDFs from the command line and write the Excel workbook
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def run_server() -> None:
    import uvicorn

    from app import config

    print(f"Open http://{config.HOST}:{config.PORT} in your browser (Ctrl+C to stop).")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)


def run_extract(inputs: list[str], output_dir: str | None) -> int:
    from app import config
    from app.batch_processor import BatchError, BatchProcessor

    paths: list[Path] = []
    labels: list[str] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            # Recurse into sub-folders, so a whole invoice archive can be given as one path.
            found = sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() == ".pdf")
            paths += found
            # Name each file by its path inside the folder, so same-named files stay distinct.
            labels += [f.relative_to(p).as_posix() for f in found]
        else:
            paths.append(p)
            labels.append(p.name)
    if not paths:
        print("No PDF files found in: " + ", ".join(inputs), file=sys.stderr)
        return 2
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print("Not found: " + ", ".join(map(str, missing)), file=sys.stderr)
        return 2
    print(f"Found {len(paths)} PDF file(s).")
    processor = BatchProcessor(output_dir=Path(output_dir) if output_dir else config.OUTPUT_DIR)
    try:
        batch = processor.process_paths(paths, labels)
    except BatchError as exc:
        print(exc, file=sys.stderr)
        return 2
    for f in batch.files:
        print(f"{f.status.value:<28} {f.file_name}" + (f"  ({'; '.join(f.messages)})" if f.messages else ""))
    workbook = processor.export(batch.batch_id)
    print(f"\nWorkbook written: {workbook}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bulk PDF invoice extraction to Excel (no OCR).")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="start the web app (default)")
    extract = sub.add_parser("extract", help="process PDF files or folders from the command line")
    extract.add_argument("inputs", nargs="+", help="PDF files and/or folders containing PDFs")
    extract.add_argument("-o", "--output-dir", help="where to write the Excel workbook (default: outputs/)")
    args = parser.parse_args(argv)
    if args.command == "extract":
        return run_extract(args.inputs, args.output_dir)
    run_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
