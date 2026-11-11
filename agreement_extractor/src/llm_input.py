"""S3 -- turn a Document into the prompt body Qwen3 reads.

Every line carries a citable id:

    [p1:L007] Date: 4th January, 2025

and tables are rendered as markdown with an id per cell (`p3:T1:R2C4`). Seal texts and
signature evidence go in as tagged lines, because a client's signatory title often exists
only inside a stamp image -- ALSD's "Director" is in the golden set as OCR-of-a-stamp for
exactly that reason -- and would otherwise be invisible to the model.

The ids are the point. The model cites them, and S5 looks there first before falling back
to the whole page, so a correct citation makes grounding both faster and less ambiguous.
A wrong one costs a little time rather than the value.

Nothing here asks the model for a coordinate, and nothing here gives it one. Positions are
code's job throughout.
"""
from __future__ import annotations

from .models import Document, Page

#: Rough characters-per-token for budgeting. Deliberately pessimistic: running out of
#: context truncates the end of a document, which is where the signatures and the Other
#: Terms charges live.
CHARS_PER_TOKEN = 3.5


def render_page(page: Page) -> str:
    """One page as cited lines, then its tables, then its non-text evidence."""
    out: list[str] = [f"--- page {page.number} ({page.source}) ---"]

    if not page.usable:
        out.append(f"[no readable text on this page: {page.note or 'unknown reason'}]")
        return "\n".join(out)

    for line in page.lines:
        out.append(f"[{line.line_id}] {line.text}")

    for table in page.tables:
        out.append(f"\n[table {table.table_id}]")
        out.append(table.to_markdown())

    for seal in page.seals:
        out.append(f"[seal text] {seal.text}")

    for evidence in page.signatures:
        detail = f": {evidence.text}" if evidence.text else ""
        out.append(f"[signature evidence: {evidence.kind}{detail}]")

    return "\n".join(out)


def render_document(document: Document, max_chars: int | None = None) -> str:
    """The whole document, page by page, in reading order."""
    blocks = [render_page(page) for page in document.pages]
    body = "\n\n".join(blocks)
    if max_chars and len(body) > max_chars:
        body = _trim_middle(blocks, max_chars)
    return body


def _trim_middle(blocks: list[str], max_chars: int) -> str:
    """Drop whole pages from the middle rather than truncating the end.

    The fields being extracted cluster at the two ends: parties, date and subject on page 1;
    commercials, terms and signatures on the last pages. A plain truncation would remove
    exactly the signature block and the Other Terms charges.
    """
    if len(blocks) <= 2:
        return "\n\n".join(blocks)[:max_chars]

    kept_head, kept_tail = [blocks[0]], [blocks[-1]]
    budget = max_chars - len(blocks[0]) - len(blocks[-1])
    head, tail = 1, len(blocks) - 2
    dropped = 0
    while head <= tail and budget > 0:
        block = blocks[tail] if (tail - head) % 2 == 0 else blocks[head]
        if len(block) > budget:
            break
        budget -= len(block)
        if block is blocks[head]:
            kept_head.append(block)
            head += 1
        else:
            kept_tail.insert(0, block)
            tail -= 1
    dropped = tail - head + 1
    middle = [f"\n[... {dropped} page(s) omitted to fit the context window ...]\n"] if dropped > 0 else []
    return "\n\n".join(kept_head + middle + kept_tail)


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


#: Context sizes worth asking for. Ollama reloads the model whenever `num_ctx` changes, so
#: asking for an exact fit per document would reload on nearly every one. A handful of
#: buckets means documents of similar size reuse a loaded model and only a real change in
#: scale pays for a reload.
WINDOWS = (8192, 16384, 32768)


def plan_window(prompt_tokens: int, max_num_ctx: int, max_output_tokens: int,
                min_output_tokens: int = 1500) -> tuple[int, int]:
    """The smallest context window this prompt can be answered in, and its output cap.

    Sizing the window for the largest document and using it for every document is the
    single most expensive habit in this pipeline. On an 8 GB card qwen3:8b runs at 30.8
    tokens/s with a 8k window and 10.8 with a 32k one -- not because the work is different,
    but because the KV cache for 32k will not fit beside the weights, so 41% of the model
    is evaluated on the CPU. A three-page offer letter asking for a 32k window is paying
    almost three times over for space it will never use.

    The output cap is scaled with the prompt for the same reason, and bounded by the same
    reasoning as the window: an extraction is a summary, so it is always a fraction of what
    it summarises. Allowing as many output tokens as there are input tokens is roughly
    double the most that has ever been needed, while still being a hard stop on a runaway.
    """
    cap = max(min_output_tokens, min(max_output_tokens, prompt_tokens))
    needed = prompt_tokens + cap
    for window in WINDOWS:
        if window >= needed and window <= max_num_ctx:
            return window, cap
    # Nothing fits: use the largest window allowed and give the answer whatever is left.
    return max_num_ctx, max(min_output_tokens, min(cap, max_num_ctx - prompt_tokens))


def build(document: Document, num_ctx: int, max_output_tokens: int = 6000,
          instruction_tokens: int = 2000) -> str:
    """Render the document to fit `num_ctx`, leaving room for the prompt *and the answer*.

    The answer is the half that is easy to forget, and forgetting it is not a degradation --
    it is a hang. A context window holds the instructions, the document and the reply at
    once. Size the body against the window alone and a long document leaves no room to
    answer in; llama-server then discards the oldest tokens to make room, which are the
    instructions and the document, and the model carries on generating from nothing with no
    stop token ever coming. An Aditya Birla MSA did exactly that for an hour.

    So the body gets what is left after both reservations, and never more. A document too
    large for that loses whole pages from its middle, which `_trim_middle` marks in the text
    it hands the model -- a visible, bounded loss, rather than an unbounded one.
    """
    budget = num_ctx - max_output_tokens - instruction_tokens
    if budget <= 0:
        raise ValueError(
            f"num_ctx {num_ctx} leaves no room for a document once {max_output_tokens} "
            f"tokens are reserved for the answer and {instruction_tokens} for the "
            f"instructions")
    return render_document(document, max_chars=int(budget * CHARS_PER_TOKEN))
