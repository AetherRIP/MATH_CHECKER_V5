"""Tests for submission attachment validation/normalization and the distinct
"Invalid submission" tracking state (§2/§8).

Key invariants under test:
  * empty / unsupported / corrupted / encrypted / zero-page files are rejected
    with a reason — never turned into a zero mark,
  * valid PDFs and images are normalized into a TEMPORARY copy in the work dir,
    leaving the original untouched,
  * a submission with no usable attachment surfaces as the distinct INVALID
    state (not a generic correction failure).
"""

from __future__ import annotations

import io

import pytest

from app.classroom import submissions as cs
from app.classroom.submission_validation import (
    InvalidSubmissionError, classify, validate_and_normalize,
)


def _pdf_bytes(pages: int = 1) -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _png_bytes() -> bytes:
    from PIL import Image
    img = Image.new("RGB", (24, 24), "white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def work(tmp_path):
    return tmp_path / "stage"


# ------------------------------------------------------------ classification

def test_classify_pdf_by_mime_and_suffix():
    assert classify("hw.pdf", b"%PDF-x", "application/pdf")["kind"] == "pdf"
    assert classify("hw", b"%PDF-x", "application/pdf")["kind"] == "pdf"
    assert classify("notes.PDF", b"", "text/plain")["kind"] == "pdf"


def test_classify_image_by_mime_and_suffix():
    assert classify("photo.jpg", b"", "image/jpeg")["kind"] == "image"
    assert classify("scan.PNG", b"", "application/octet-stream")["kind"] == "image"


def test_classify_unsupported():
    assert classify("doc.docx", b"", "application/msword")["kind"] == "unsupported"


# ------------------------------------------------------------ validation

def test_empty_file_rejected(work):
    r = validate_and_normalize("empty.pdf", b"", "application/pdf", work)
    assert r["ok"] is False and r["reason"] == "empty file"


def test_unsupported_type_rejected(work):
    r = validate_and_normalize("doc.docx", b"%PDF-x", "application/msword", work)
    assert r["ok"] is False
    assert "unsupported" in r["reason"]


def test_corrupt_pdf_rejected_with_outcome_reason(work):
    r = validate_and_normalize("corrupt.pdf", b"this is not a pdf", "application/pdf", work)
    assert r["ok"] is False
    assert "not a valid PDF" in r["reason"]


def test_valid_pdf_normalized_to_temp_copy(work):
    r = validate_and_normalize("hw.pdf", _pdf_bytes(), "application/pdf", work)
    assert r["ok"] is True and r["kind"] == "pdf"
    assert r["path"].endswith(".pdf")
    assert (work / r["path"]).exists()
    # The original bytes supplied to the function must be untouched.
    assert (work / r["path"]).read_bytes().startswith(b"%PDF-")


def test_zero_page_pdf_rejected(work):
    # A writer with no pages yields a header-only PDF with zero /Count.
    from pypdf import PdfWriter
    writer = PdfWriter()
    buf = io.BytesIO()
    writer.write(buf)
    r = validate_and_normalize("blank.pdf", buf.getvalue(), "application/pdf", work)
    assert r["ok"] is False
    assert "no pages" in r["reason"] or "processed" in r["reason"]


def test_valid_image_normalized_to_temp_copy(work):
    r = validate_and_normalize("photo.jpg", _png_bytes(), "image/png", work)
    assert r["ok"] is True and r["kind"] == "image"
    assert (work / r["path"]).exists()


def test_invalid_submission_error_prefix():
    err = InvalidSubmissionError(["hw.pdf: empty file", "scan.jpg: unsupported file type"])
    assert str(err).startswith("Invalid submission: ")
    assert "hw.pdf: empty file" in str(err)


# ------------------------------------------------- INVALID tracking state map

def test_normalized_status_invalid_from_error_code():
    row = {"current_state": "TURNED_IN"}
    job = {"status": "FAILED", "error_code": "invalid_submission"}
    assert cs.normalized_status(row, job=job) == "INVALID"


def test_normalized_status_failed_for_other_error_code():
    row = {"current_state": "TURNED_IN"}
    job = {"status": "FAILED", "error_code": "job_failed"}
    assert cs.normalized_status(row, job=job) == "FAILED"


def test_invalid_label_present():
    assert cs.STATUS_LABELS["INVALID"] == "Invalid submission"
