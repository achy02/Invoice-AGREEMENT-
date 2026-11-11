# Sample agreements

Drop real client agreement PDFs here (sub-folders are fine), then probe them:

```
.venv\Scripts\python -m tools.inspect_agreement sample_agreements --limit 30
```

The probe reports, per document: page count, how many pages produced text without OCR,
whether the quality gate passes, which retrieval anchors were found and on which page,
whether the PDF carries real signature fields, and whether rate tables are machine-readable.

Add `--ocr` to OCR the pages whose text layer fails (requires PaddleOCR — see
`requirements.txt`). Add `--text N` to print page N, to see exactly what was fetched.

PDFs in this folder are git-ignored.
