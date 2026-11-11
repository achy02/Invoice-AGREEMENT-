"""Load and verify the hand-labelled golden set.

This is the definition of "correct" for the whole pipeline, so it is loaded through one
place and checked on load rather than trusted. A silent mismatch here -- a filename that
resolves to nothing, a normalised box computed against the wrong page size -- would show up
later as an accuracy figure that means nothing.

Contents of `tests/golden/`:

    source_map_annotations.json     111 records: value, page, section, key/value boxes
    TBX_Offer_Letters_Extraction.xlsx   the workbook whose shape S7 must reproduce
    TBX_pages/*.png                 the 200 DPI page images every box refers to
    qa_overlays/*.jpg               those boxes drawn, for eyeballing
    paddleocr_Label.txt             PPOCRLabel detection/recognition labels
    paddleocr_kie_xfund_style.txt   KIE labels: question/answer + linking
    *.pdf                           the four source documents

One wrinkle worth knowing about: the annotations name files with underscores
("Signed_Bajaj_Finance_Limited_-_Offer_letter_-_01_Aug_2024.pdf") while the PDFs on disk
use spaces. Matching therefore goes through `normalise_name`, not string equality.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BBox = tuple[float, float, float, float]

#: Fields the golden set deliberately records as absent. They are the null examples: a model
#: that invents an Effective Date is wrong even though the document "looks like" it has one.
NULL_METHOD = "Not present in document"


def normalise_name(name: str) -> str:
    """Compare file names across the underscore/space difference, and ignore case."""
    return re.sub(r"[\s_]+", " ", str(name)).strip().casefold()


@dataclass
class Record:
    """One hand-verified extracted cell."""

    id: str
    client: str
    sheet: str                 # Summary | Commercials
    field: str
    value: Any
    file: str
    page: int | None           # None where the field is not present in the document
    section: str
    key_text: str
    key_bbox: BBox | None
    key_norm: tuple[int, int, int, int] | None
    value_text: str
    value_bbox: BBox | None
    value_norm: tuple[int, int, int, int] | None
    raw_text: str
    method: str
    score: float | None
    image: str
    page_size: tuple[int, int] | None
    multi_line: bool
    note: str

    @property
    def is_null(self) -> bool:
        """A deliberate "not present" example rather than a missed extraction."""
        return self.method == NULL_METHOD

    @property
    def row_key(self) -> tuple[str, str]:
        return (self.client, self.field)


def _bbox(value) -> BBox | None:
    if not value:
        return None
    x0, y0, x1, y1 = value
    return (float(x0), float(y0), float(x1), float(y1))


def _page_size(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", str(text or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def load_records(path: Path | str) -> list[Record]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    records = []
    for row in data:
        page = row.get("page")
        records.append(Record(
            id=row["id"], client=row["client"], sheet=row["sheet"], field=row["field"],
            value=row.get("value"), file=row["file"],
            page=page if isinstance(page, int) else None,
            section=row.get("section", ""), key_text=row.get("key_gt", "") or "",
            key_bbox=_bbox(row.get("key_bb")), key_norm=_bbox(row.get("key_n")),
            value_text=row.get("val_gt", "") or "", value_bbox=_bbox(row.get("val_bb")),
            value_norm=_bbox(row.get("val_n")), raw_text=row.get("raw", "") or "",
            method=row.get("method", ""), score=row.get("score"),
            image=row.get("img", "") or "", page_size=_page_size(row.get("wh")),
            multi_line=str(row.get("multiline", "")).strip().lower() == "yes",
            note=row.get("note", "") or "",
        ))
    return records


@dataclass
class GoldenSet:
    root: Path
    records: list[Record]
    pdfs: dict[str, Path] = field(default_factory=dict)      # normalised name -> path
    images: dict[str, Path] = field(default_factory=dict)    # image file name -> path

    @property
    def commercials(self) -> list[Record]:
        return [r for r in self.records if r.sheet == "Commercials"]

    @property
    def summary(self) -> list[Record]:
        return [r for r in self.records if r.sheet == "Summary"]

    @property
    def nulls(self) -> list[Record]:
        return [r for r in self.records if r.is_null]

    @property
    def clients(self) -> list[str]:
        seen: list[str] = []
        for record in self.records:
            if record.client not in seen:
                seen.append(record.client)
        return seen

    def pdf_for(self, record: Record) -> Path | None:
        return self.pdfs.get(normalise_name(record.file))

    def image_for(self, record: Record) -> Path | None:
        return self.images.get(record.image) if record.image else None


def load(root: Path | str) -> GoldenSet:
    root = Path(root)
    records = load_records(root / "source_map_annotations.json")
    pdfs = {normalise_name(p.name): p for p in root.glob("*.pdf")}
    images = {p.name: p for p in (root / "TBX_pages").glob("*.png")}
    return GoldenSet(root, records, pdfs, images)


# -- verification --------------------------------------------------------------------------

def check(golden: GoldenSet, tolerance: int = 1) -> list[str]:
    """Return a list of problems with the golden set itself. Empty means it is sound.

    The normalised-box check is the one that matters: `val_n` must be `val_bb` scaled against
    the page size in `wh`. If it is not, either the boxes were drawn on a differently sized
    render or the two coordinate systems disagree, and every IoU measured later is wrong.
    """
    problems: list[str] = []

    for record in golden.records:
        where = f"{record.id} ({record.field})"

        if golden.pdf_for(record) is None:
            problems.append(f"{where}: no PDF on disk for {record.file!r}")

        if record.is_null:
            if record.value_bbox or record.page is not None:
                problems.append(f"{where}: marked not-present but carries a page or box")
            continue

        if record.page is None:
            problems.append(f"{where}: has a box but no page number")
        if record.image and golden.image_for(record) is None:
            problems.append(f"{where}: page image {record.image!r} is missing")
        if record.page_size is None:
            problems.append(f"{where}: no page size recorded, cannot verify its boxes")
            continue

        width, height = record.page_size
        for label, bbox, norm in (("value", record.value_bbox, record.value_norm),
                                  ("key", record.key_bbox, record.key_norm)):
            if bbox is None or norm is None:
                continue
            expected = (round(bbox[0] / width * 1000), round(bbox[1] / height * 1000),
                        round(bbox[2] / width * 1000), round(bbox[3] / height * 1000))
            if any(abs(a - b) > tolerance for a, b in zip(expected, norm)):
                problems.append(
                    f"{where}: {label} normalised box {tuple(int(v) for v in norm)} "
                    f"does not match {bbox} scaled by {width}x{height} (expected {expected})")
            if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
                problems.append(f"{where}: {label} box is inverted: {bbox}")
            if bbox[2] > width + tolerance or bbox[3] > height + tolerance:
                problems.append(f"{where}: {label} box {bbox} falls outside the {width}x{height} page")

    ids = [r.id for r in golden.records]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        problems.append(f"duplicate record id(s): {sorted(duplicates)}")

    return problems
