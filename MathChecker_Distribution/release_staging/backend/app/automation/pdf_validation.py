"""PDF validation before upload + Drive file verification after upload.

Nothing is ever uploaded to Drive or attached to a Classroom submission
merely because the generator returned without an exception (§26, §27, §28).

Two independent checks live here:

* :func:`validate_report_pdf` — proves a freshly rendered report is a real,
  complete, parseable PDF carrying the expected content, BEFORE it is sent
  anywhere (§25).
* :func:`verify_drive_report` — after upload, proves the Drive file really
  exists, is a non-empty ``application/pdf`` of the expected size that can be
  downloaded byte-for-byte (§28).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re

from ..classroom import client as gc

log = logging.getLogger("automation.pdf_validation")

PDF_MAGIC = b"%PDF-"
PDF_EOF = b"%%EOF"

#: A report smaller than this is almost certainly truncated/empty.
MIN_REASONABLE_BYTES = 800
#: Anything above this is implausible for a single correction report.
MAX_REASONABLE_BYTES = 50 * 1024 * 1024


def _normalise(text: str) -> str:
    """Fold the text the way a READER would compare it.

    PDF text extraction reproduces the line breaks the report used, so a name
    that wrapped onto two lines (``"Yahia Mohamed\nNoureldin"``) was reported
    as missing even though it is plainly printed on the page. Case and
    whitespace must never decide whether a report is accepted.
    """
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def validate_report_pdf(content: bytes, *, expected_text: list[str] | None = None,
                        expected_any: list[str] | None = None,
                        min_pages: int = 1) -> dict:
    """Validate generated PDF bytes (§25, §27).

    ``expected_text`` entries must ALL appear (e.g. the assignment title);
    ``expected_any`` needs AT LEAST ONE to appear, which is the honest rule for
    a student's NAME: the report prints the canonical display name while the
    Classroom roster, the stored snapshot and a nickname are all the same
    student, so demanding one specific spelling rejected correct reports.

    Returns ``{ok, reason, size, pages, checks}``. ``reason`` is a short,
    human-readable explanation when ``ok`` is False; the caller must NOT
    upload a report that fails here.
    """
    checks: dict[str, bool] = {}
    if not content:
        return {"ok": False, "reason": "the generated report is empty",
                "size": 0, "pages": 0, "checks": {"non_empty": False}}
    size = len(content)
    checks["non_empty"] = size > 0
    checks["pdf_header"] = content.startswith(PDF_MAGIC)
    # A truncated PDF loses its trailer; require the EOF marker in the tail.
    checks["pdf_eof"] = PDF_EOF in content[-2048:]
    checks["reasonable_size"] = MIN_REASONABLE_BYTES <= size <= MAX_REASONABLE_BYTES

    pages = 0
    missing: list[str] = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = len(reader.pages)
        checks["parseable"] = pages > 0
        wanted = [str(t) for t in (expected_text or []) if str(t).strip()]
        wanted_any = [str(t) for t in (expected_any or []) if str(t).strip()]
        if wanted or wanted_any:
            haystack = ""
            for page in reader.pages[:3]:
                try:
                    haystack += (page.extract_text() or "") + "\n"
                except Exception:  # noqa: BLE001 — extraction is best-effort
                    pass
            lowered = _normalise(haystack)
            missing = [t for t in wanted if _normalise(t) not in lowered]
            if wanted_any and not any(_normalise(t) in lowered
                                      for t in wanted_any):
                missing.append("the student name (any of "
                               + " / ".join(wanted_any) + ")")
            checks["expected_content"] = not missing
    except Exception as exc:  # noqa: BLE001
        checks["parseable"] = False
        return {"ok": False,
                "reason": f"the report could not be parsed as a PDF ({exc})",
                "size": size, "pages": 0, "checks": checks}

    if not checks.get("pdf_header"):
        return {"ok": False, "reason": "the report does not start with a PDF header",
                "size": size, "pages": pages, "checks": checks}
    if not checks.get("pdf_eof"):
        return {"ok": False, "reason": "the report is truncated (no PDF trailer)",
                "size": size, "pages": pages, "checks": checks}
    if pages < max(1, min_pages):
        return {"ok": False, "reason": "the report has no readable pages",
                "size": size, "pages": pages, "checks": checks}
    if not checks.get("reasonable_size"):
        return {"ok": False,
                "reason": f"the report size ({size} bytes) is not plausible",
                "size": size, "pages": pages, "checks": checks}
    if missing:
        return {"ok": False,
                "reason": "the report is missing expected content "
                          f"({', '.join(missing)})",
                "size": size, "pages": pages, "checks": checks,
                "missing": missing}
    return {"ok": True, "reason": None, "size": size, "pages": pages,
            "checks": checks}


def verify_drive_report(token: str, file_id: str, *,
                        expected_bytes: bytes | None = None,
                        expected_name: str | None = None) -> dict:
    """Verify an uploaded report file actually exists in Drive (§28).

    Checks: exists, correct id, ``application/pdf`` mime type, non-zero and
    plausible size, and (when provided) that its bytes match what we generated.
    """
    result = {"ok": False, "reason": None, "file_id": file_id}
    try:
        meta = gc.get_drive_file(token, file_id)
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"could not read the Drive file ({exc})"
        return result
    if meta is None:
        result["reason"] = "the Drive file does not exist"
        return result
    result["mime_type"] = meta.get("mimeType")
    result["name"] = meta.get("name")
    result["size"] = _int_or_none(meta.get("size"))
    result["trashed"] = bool(meta.get("trashed"))

    if meta.get("id") != file_id:
        result["reason"] = "Drive returned a different file id"
        return result
    if meta.get("mimeType") != "application/pdf":
        result["reason"] = ("the Drive file is not a PDF "
                            f"(mime {meta.get('mimeType')!r})")
        return result
    if meta.get("trashed"):
        result["reason"] = "the Drive file is in the trash"
        return result
    size = result["size"]
    if size is not None and size <= 0:
        result["reason"] = "the Drive file is zero bytes"
        return result

    if expected_bytes is not None:
        try:
            _, data, _ = gc.download_drive_file(token, file_id)
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"the Drive file could not be downloaded ({exc})"
            return result
        if len(data) <= 0:
            result["reason"] = "the downloaded Drive file is empty"
            return result
        if hashlib.sha256(data).hexdigest() != hashlib.sha256(expected_bytes).hexdigest():
            result["reason"] = "the Drive file content does not match the report"
            return result
        result["content_verified"] = True

    result["ok"] = True
    return result


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
