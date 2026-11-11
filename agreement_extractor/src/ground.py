"""S5 -- find each value's box by matching its evidence quote. Pure code, no model.

This is the stage that makes the whole design safe. The LLM is never asked for a coordinate,
because a fluent model will happily produce a plausible one. It returns a verbatim quote,
and this stage goes looking for that quote in the tokens S2 recorded. If the quote is not
there, the value does not enter the workbook -- it is marked UNGROUNDED and reviewed.

Three rules earn their keep, each found by running the golden set rather than by reasoning:

1. **Normalise to alphanumerics.** Several of these PDFs have no space glyphs at all, so
   "ALSD Technologies Pvt Ltd" is "ALSDTechnologiesPvtLtd" in the text layer. Comparing
   letters and digits only makes spacing and punctuation irrelevant.

2. **" / " in a quote is a line join, not text.** The annotations write a value that wraps
   across lines as "INR 55 per stamp / (Delhi)", and a signature block spanning two columns
   as "For TransBnk Solutions Pvt Ltd / Tribhuvan Petshali". Searched literally, neither is
   ever found. Each part is located separately and the boxes unioned.

3. **Prefer the copy on the label's row.** "reimbursement basis." appears twice on
   Capitalneed page 3, "INR 12" twice in Ninjamonk's rate table. Taking the smallest or
   first match puts a rate on the wrong product, which is worse than not finding it. When
   `key_text` is known, the occurrence nearest the label's vertical centre -- and to its
   right or below -- wins.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import Config
from .models import BBox, Extracted, Grounded, Page, Token, union

log = logging.getLogger(__name__)

#: How the annotations mark a value that wraps across lines or spans two columns.
LINE_JOIN = re.compile(r"\s+/\s+")

#: Scanning more than this many tokens forward for one quote is pointless and slow; the
#: longest golden quote is a little over 20 words.
MAX_SPAN = 80


def normalise(text: str) -> str:
    """Lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _digits(text: str) -> str:
    """Just the digits, in order -- what a fuzzy match is never allowed to alter."""
    return re.sub(r"\D", "", str(text))


@dataclass
class Match:
    bbox: BBox
    score: float
    start: int
    end: int           # exclusive
    method: str
    #: True when the match begins and ends on token boundaries rather than mid-word.
    #: Normalising to alphanumerics means a short quote matches inside longer words --
    #: "To" is in "Solutions" and "Automation", "1" is in every amount on the page -- so an
    #: aligned match is preferred over an embedded one regardless of where it sits.
    aligned: bool = True


def _centre_y(bbox: BBox) -> float:
    return (bbox[1] + bbox[3]) / 2


def _trim(token: Token, from_char: int, to_char: int, length: int) -> BBox:
    """The slice of a token's box covering characters [from_char, to_char).

    The text layer gives one token per word, so this is usually the whole box. OCR gives one
    token per *line*, and without this a phrase inside a line inherits the entire line's box
    -- which is how "in addition to above Charges." ends up scored at IoU 0.27 against a
    word-level reference. Characters are assumed evenly spaced across the token, which is
    only approximately true in a proportional font but is far closer than not trimming.
    """
    if length <= 0 or (from_char <= 0 and to_char >= length):
        return token.bbox
    x0, y0, x1, y1 = token.bbox
    width = x1 - x0
    start = x0 + width * (max(from_char, 0) / length)
    end = x0 + width * (min(to_char, length) / length)
    return (start, y0, max(end, start + 1), y1)


def exact_matches(tokens: list[Token], quote: str) -> list[Match]:
    """Every window of tokens whose joined normalised text contains the quote.

    Boxes are trimmed to the characters that actually spell the quote, so a match inside a
    long token is not charged for the whole token.
    """
    target = normalise(quote)
    if not target:
        return []
    found: list[Match] = []
    for start in range(len(tokens)):
        joined = ""
        offsets: list[tuple[int, int]] = []          # (start, length) per token in the window
        for end in range(start, min(start + MAX_SPAN, len(tokens))):
            piece = normalise(tokens[end].text)
            offsets.append((len(joined), len(piece)))
            joined += piece
            at = joined.find(target)
            if at >= 0:
                until = at + len(target)
                starts = {begin for begin, length in offsets if length}
                ends = {begin + length for begin, length in offsets if length}
                found.append(Match(
                    _span_box(tokens, start, offsets, at, until),
                    1.0, start, end + 1, "exact",
                    aligned=(at in starts and until in ends)))
                break
            if len(joined) > len(target) + 40:
                break
    return found


def _span_box(tokens: list[Token], start: int, offsets: list[tuple[int, int]],
              at: int, until: int) -> BBox:
    """Union the tokens covering [at, until) in the joined string, trimming the ends."""
    boxes: list[BBox] = []
    for index, (begin, length) in enumerate(offsets):
        end = begin + length
        if end <= at or begin >= until or length == 0:
            continue
        boxes.append(_trim(tokens[start + index], at - begin, until - begin, length))
    if not boxes:
        boxes = [tokens[start].bbox]
    return union(boxes)


def fuzzy_matches(tokens: list[Token], quote: str, threshold: int) -> list[Match]:
    """Fallback for OCR slips: rapidfuzz partial ratio over the same sliding windows.

    Needed because the reference itself contains them -- Bajaj's scan reads "15 issuer" as
    "15 ixsuer" in the golden set's own raw column. An exact-only grounder would discard a
    correct value because OCR misread one letter of it.
    """
    from rapidfuzz import fuzz

    target = normalise(quote)
    if not target:
        return []
    # Fuzzy matching exists for OCR letter slips -- the golden set's own raw column has
    # Bajaj's "15 issuer" read as "15 ixsuer". It must never excuse a different NUMBER.
    # "INR 99 per transaction" scores well above threshold against "INR 2 per transaction",
    # so without this a hallucinated rate grounds successfully and lands in the workbook
    # as a located fact, which is the exact failure the whole design exists to prevent.
    target_digits = _digits(quote)
    width = max(1, len(target) // 6)
    found: list[Match] = []
    for start in range(len(tokens)):
        joined = ""
        raw = ""
        for end in range(start, min(start + max(width * 3, 12), len(tokens))):
            joined += normalise(tokens[end].text)
            raw += tokens[end].text
            if len(joined) < len(target) * 0.6:
                continue
            score = fuzz.partial_ratio(target, joined)
            if score >= threshold and target_digits and target_digits not in _digits(raw):
                score = 0
            if score >= threshold:
                found.append(Match(union([t.bbox for t in tokens[start:end + 1]]),
                                   score / 100.0, start, end + 1, "fuzzy"))
                break
            if len(joined) > len(target) * 1.8:
                break
    return found


def locate(tokens: list[Token], quote: str, threshold: int,
           anchor: BBox | None = None, tolerance: float = 20.0) -> Match | None:
    """The best single occurrence of `quote`, preferring the one on `anchor`'s row."""
    candidates = exact_matches(tokens, quote)
    if not candidates:
        candidates = fuzzy_matches(tokens, quote, threshold)
    if not candidates:
        return None
    # Several windows produce the same box; collapse them so one occurrence is one candidate.
    unique: dict[tuple, Match] = {}
    for match in candidates:
        key = (round(match.bbox[0], 1), round(match.bbox[1], 1),
               round(match.bbox[2], 1), round(match.bbox[3], 1))
        if key not in unique or (match.aligned and not unique[key].aligned):
            unique[key] = match
    tight = list(unique.values())

    # Deliberately NOT filtered by area. Area is not a proxy for correctness across font
    # sizes: on Capitalneed the digital-signature footer prints "Pulak Jain" in a smaller
    # face than the signature block, so the wrong occurrence is genuinely the smaller box
    # and an area filter discards the right answer before position is ever considered.
    if len(tight) == 1 or anchor is None:
        return min(tight, key=lambda m: (not m.aligned, _area(m.bbox)))

    # Two layouts put a value near its label, and both must be handled or one of them
    # silently picks the wrong duplicate:
    #
    #   a rate table     label ... value        same row, value to the right
    #   a signature block     label             value directly below, same column
    #                         value
    #
    # Ranking on vertical distance alone sends "Pulak Jain" to the digital-signature footer
    # instead of the signature block, because the footer is also below the caption.
    anchor_y = _centre_y(anchor)

    def rank(match: Match) -> tuple:
        dy = abs(_centre_y(match.bbox) - anchor_y)
        dx = abs(match.bbox[0] - anchor[0])
        if dy <= tolerance and match.bbox[0] >= anchor[0] - 1:
            place = (0, dx)                                        # same row, to the right
        elif match.bbox[1] >= anchor[1] - 1:
            place = (1, dy + dx * 0.5)                             # below, same column
        else:
            place = (2, dy + dx)
        return (not match.aligned, *place, _area(match.bbox))

    return min(tight, key=rank)


def _area(bbox: BBox) -> float:
    return max(bbox[2] - bbox[0], 0) * max(bbox[3] - bbox[1], 0)


def _line_height(tokens: list[Token]) -> float:
    heights = sorted(t.bbox[3] - t.bbox[1] for t in tokens if t.bbox[3] > t.bbox[1])
    return heights[len(heights) // 2] if heights else 0.0


def locate_quote(tokens: list[Token], quote: str, threshold: int,
                 anchor: BBox | None = None,
                 tolerance: float = 20.0) -> tuple[BBox, float, str] | None:
    """Locate a quote, splitting it on the line-join marker and unioning the parts."""
    parts = [p for p in LINE_JOIN.split(quote or "") if normalise(p)]
    if not parts:
        return None

    boxes: list[BBox] = []
    scores: list[float] = []
    methods: set[str] = set()
    current_anchor = anchor
    for part in parts:
        match = locate(tokens, part, threshold, current_anchor, tolerance)
        if match is None:
            if len(parts) == 1:
                return None
            continue                      # a wrapped part may be unreadable; keep the rest
        boxes.append(match.bbox)
        scores.append(match.score)
        methods.add(match.method)
        current_anchor = current_anchor or match.bbox
    if not boxes:
        return None
    method = "exact" if methods == {"exact"} else "fuzzy"
    if len(parts) > 1:
        method += "+joined"
    return union(boxes), min(scores), method


def ground_field(item: Extracted, pages: dict[int, Page], config: Config) -> Grounded:
    """Locate one extracted value, and its label, on the page it claims.

    Looks first in the tokens of the lines or cells the model cited, then across the whole
    page. Citing the right line is a hint, not a guarantee, so a wrong citation costs a
    little time rather than the value.
    """
    grounded = Grounded(**{f: getattr(item, f) for f in
                           ("field", "value", "normalized_value", "evidence_text",
                            "evidence_ids", "page", "section_heading", "key_text")})

    if item.value is None and not item.evidence_text:
        grounded.status = "OK"                      # a deliberate null needs no location
        grounded.locate_method = "not present in document"
        return grounded

    page = pages.get(item.page) if item.page else None
    if page is None:
        grounded.status = "UNGROUNDED"
        grounded.flags.append(f"no page {item.page} in the document")
        return grounded

    all_tokens = [t for line in page.lines for t in line.tokens]
    cited = _cited_tokens(page, item.evidence_ids)
    threshold = config.grounding.fuzzy_threshold
    tolerance = max(config.grounding.same_row_tolerance_px, _line_height(all_tokens) * 0.6)

    key_box = None
    if item.key_text:
        found = locate_quote(all_tokens, item.key_text, threshold)
        if found:
            key_box, key_score, _ = found
            grounded.key_bbox = key_box

    result = None
    for tokens, where in ((cited, "cited lines"), (all_tokens, "whole page")):
        if not tokens:
            continue
        result = locate_quote(tokens, item.evidence_text, threshold, key_box, tolerance)
        if result:
            grounded.flags.append(f"located in {where}")
            break

    if result is None:
        grounded.status = "UNGROUNDED"
        grounded.match_score = 0.0
        grounded.locate_method = _method_name(page)
        grounded.flags.append("evidence quote not found on the cited page")
        return grounded

    bbox, score, method = result
    grounded.value_bbox = bbox
    grounded.match_score = round(score, 4)
    grounded.locate_method = f"{_method_name(page)} [{method}]"
    grounded.multi_line = (bbox[3] - bbox[1]) > _line_height(all_tokens) * 1.6
    if score * 100 < config.grounding.min_match_score:
        grounded.status = "UNGROUNDED"
        grounded.flags.append(f"match score {score:.2f} below threshold")
    return grounded


def _cited_tokens(page: Page, ids: list[str]) -> list[Token]:
    wanted = set(ids or ())
    if not wanted:
        return []
    tokens: list[Token] = []
    for line in page.lines:
        if line.line_id in wanted:
            tokens.extend(line.tokens)
    for table in page.tables:
        for cell in table.cells:
            if cell.cell_id in wanted:
                tokens.append(Token(cell.text, cell.bbox))
    return tokens


def _method_name(page: Page) -> str:
    return ("PDF text layer (char-level)" if page.source == "text_layer"
            else "PaddleOCR (word-level)" if page.source == "paddle_ocr"
            else "no text")


def ground_document(items: list[Extracted], pages: list[Page],
                    config: Config) -> list[Grounded]:
    by_number = {page.number: page for page in pages}
    return [ground_field(item, by_number, config) for item in items]
