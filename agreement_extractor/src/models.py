"""The data contract every stage reads and writes.

One rule shapes all of it: **the LLM never produces coordinates.** It returns values and
verbatim evidence quotes; S5 finds the boxes by matching those quotes against the tokens
recorded here. So every token, line and cell carries its own box from the moment it is read,
whether it came from the PDF's text layer or from OCR.

Boxes are always `[x0, y0, x1, y1]` in **pixels on the page image rendered at `render.dpi`**,
origin top-left. The normalised 0-1000 form is derived, never stored as the primary -- it is
the Qwen-VL grounding convention and is resolution independent, but pixels are what the
overlays and the golden annotations use.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

BBox = tuple[float, float, float, float]

Source = Literal["text_layer", "paddle_ocr"]


def normalise_bbox(bbox: BBox, width: float, height: float) -> tuple[int, int, int, int]:
    """Scale a pixel box to 0-1000 on each axis (the Qwen2.5-VL / Qwen3-VL convention)."""
    if width <= 0 or height <= 0:
        raise ValueError(f"page size must be positive, got {width}x{height}")
    x0, y0, x1, y1 = bbox
    return (round(x0 / width * 1000), round(y0 / height * 1000),
            round(x1 / width * 1000), round(y1 / height * 1000))


def union(boxes: list[BBox]) -> BBox:
    """The smallest box containing all of `boxes` -- how a multi-token value is grounded."""
    if not boxes:
        raise ValueError("cannot union an empty list of boxes")
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


@dataclass
class Token:
    """A word (OCR) or character run (text layer), with its own box.

    Grounding works at this level: a quote is matched against a window of tokens and the
    matched tokens' boxes are unioned. Line boxes alone are too coarse -- they would place
    a rate's box across the whole row.
    """

    text: str
    bbox: BBox
    conf: float = 1.0


@dataclass
class Line:
    """One line of text in reading order.

    `line_id` is what the LLM cites, formatted `p<page>:L<nnn>` (e.g. `p3:L042`). It is
    stable for a given page JSON, which is why the page JSON is cached rather than rebuilt.
    """

    line_id: str
    text: str
    bbox: BBox
    source: Source
    conf: float = 1.0
    tokens: list[Token] = field(default_factory=list)


@dataclass
class Cell:
    """One table cell. Cited by the LLM as `p<page>:T<table>:R<row>C<col>`."""

    cell_id: str
    row: int
    col: int
    text: str
    bbox: BBox
    row_span: int = 1
    col_span: int = 1


@dataclass
class Table:
    table_id: str
    bbox: BBox
    n_rows: int
    n_cols: int
    cells: list[Cell] = field(default_factory=list)

    def to_markdown(self) -> str:
        """Render for the LLM prompt, one cell id per cell so evidence can point at a cell."""
        grid: dict[tuple[int, int], Cell] = {(c.row, c.col): c for c in self.cells}
        lines = []
        for r in range(self.n_rows):
            row = []
            for c in range(self.n_cols):
                cell = grid.get((r, c))
                row.append(f"{cell.text} <{cell.cell_id}>" if cell else "")
            lines.append("| " + " | ".join(row) + " |")
            if r == 0:
                lines.append("|" + "---|" * self.n_cols)
        return "\n".join(lines)


@dataclass
class SealText:
    """Text recognised inside a seal or stamp image -- often the only place a client's
    signatory title appears, because the stamp is an image and not in the text layer."""

    text: str
    bbox: BBox
    conf: float = 1.0


@dataclass
class SignatureEvidence:
    """What was seen near a signatory caption, graded rather than decided.

    `kind` is one of:
      `pdf_signature_field` -- a signature widget in the PDF structure
      `envelope_id`         -- a DocuSign/e-sign envelope stamped on the page
      `digitally_signed_by` -- that phrase in page text
      `seal`                -- a detected company seal or stamp
      `ink`                 -- a handwriting/ink region near an "Authorised Signatory" caption
      `typed_name`          -- a name printed under the caption
    """

    kind: str
    text: str = ""
    bbox: BBox | None = None
    page: int = 0


@dataclass
class Page:
    """Everything known about one page, cached as JSON and never recomputed."""

    number: int
    width: int                      # of the rendered image, in pixels
    height: int
    dpi: int
    image_path: str
    source: Source | Literal["none"]
    lines: list[Line] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    seals: list[SealText] = field(default_factory=list)
    signatures: list[SignatureEvidence] = field(default_factory=list)
    mean_conf: float = 1.0
    note: str = ""

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def usable(self) -> bool:
        return self.source != "none" and bool(self.lines)


@dataclass
class Document:
    """One agreement: its identity, and its pages once S2 has run."""

    file_name: str
    path: str
    sha256: str
    page_count: int
    pages: list[Page] = field(default_factory=list)
    duplicate_of: str | None = None   # sha256 of the first file seen with these bytes
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- extraction results --------------------------------------------------------------------

#: A field the LLM could not find. Recorded explicitly rather than omitted, so that
#: "not present" is distinguishable from "never looked for" -- the golden set keeps Stamp
#: Date and Effective Date as null examples for exactly this reason.
NOT_PRESENT = None


@dataclass
class Extracted:
    """One extracted value, before grounding.

    `evidence_text` must be copied verbatim from the page. S5 looks for it; if it cannot be
    found the value is marked UNGROUNDED and goes to the review queue rather than into the
    workbook. That check is what stops a fluent model from inventing a plausible rate.
    """

    field: str
    value: Any
    normalized_value: Any = None
    evidence_text: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    page: int | None = None
    section_heading: str = ""
    key_text: str = ""


@dataclass
class Grounded(Extracted):
    """An Extracted value after S5 has located it on the page."""

    value_bbox: BBox | None = None
    key_bbox: BBox | None = None
    match_score: float = 0.0
    locate_method: str = ""        # e.g. "text layer (char-level)", "PaddleOCR (word-level)"
    multi_line: bool = False
    status: str = "OK"             # OK | UNGROUNDED | REVIEW
    flags: list[str] = field(default_factory=list)
