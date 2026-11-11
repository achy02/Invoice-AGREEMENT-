import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.batch_processor import BatchProcessor  # noqa: E402
from app.invoice_field_extractor import InvoiceFieldExtractor  # noqa: E402
from app.invoice_validator import validate_record  # noqa: E402
from app.pdf_text_extractor import extract_document  # noqa: E402


@pytest.fixture
def extractor():
    return InvoiceFieldExtractor(vendor_rules=[])


@pytest.fixture
def extract(extractor):
    """Extract and validate a single PDF, returning the InvoiceRecord."""

    def _extract(path):
        record = extractor.extract(extract_document(path))
        return validate_record(record)

    return _extract


@pytest.fixture
def processor(tmp_path):
    return BatchProcessor(upload_dir=tmp_path / "uploads", output_dir=tmp_path / "outputs",
                          extractor=InvoiceFieldExtractor(vendor_rules=[]))


def issue_texts(record, field=None):
    return [i.description for i in record.issues if field is None or i.field == field]
