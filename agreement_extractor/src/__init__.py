"""Local agreement-extraction pipeline: PaddleOCR reads the pages, Qwen3 maps what was read
onto the fields we need, code does the arithmetic and the checking. The output is one Excel
workbook, and nothing else.

Stages, each caching JSON per file SHA-256 (see `cache.StageCache`):

    S0 inventory   list PDFs, hash, page count, per-page text-layer detection, duplicates
    S1 render      every page to PNG at render.dpi, for OCR to read
    S2 text/layout lines, tokens, tables, seals and signature evidence -> one Page per page
    S3 llm_input   reading-order text with line ids, tables as markdown with cell ids
    S4 extract     two Qwen3 calls per document; values + verbatim evidence, never boxes
    S5 ground      find each evidence quote in the page text; a quote that is not there
                   means the value was invented, and the value is withheld
    S6 validate    arithmetic, dates, signed status, contradictions, placeholders -> flags
    S7 export      the workbook

S5 computes coordinates on the way to its answer and no longer reports them. The page
images it measured against are build material for OCR, kept under cache/ and never shown.
What survives into the workbook is the part a person can use: the page number, the quoted
words, and whether the quote was found.

The division of labour is the point: the model is asked only for what it is good at
(reading prose and naming what it found), and every number, comparison and verdict is
produced by code that can be tested.
"""
