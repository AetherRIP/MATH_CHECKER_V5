"""Submission attachment validation + harmless normalization (§8).

Before a downloaded Classroom/Drive attachment is handed to the marking
engine we:
  * classify the attachment type (PDF / image / unsupported),
  * reject things that must NOT go near the model as zero marks: empty
    files, corrupted/renamed PDFs, encrypted PDFs, zero-page PDFs and
    unsupported mime types,
  * normalize harmless orientation problems into a TEMPORARY copy:
    images get EXIF orientation applied (phone photos of homework that
    arrive sideways/upside-down become upright) and PDFs are re-encoded
    with their page rotation preserved/applied so downstream rendering is
    upright.

The original Google file is never modified — every change is written to a
fresh file in the worker's private staging directory.

One invalid attachment never blocks the class: the caller collects
per-file reasons and only fails a submission when NO attachment is usable
(via :class:`InvalidSubmissionError`, which maps to the distinct
``INVALID``/“Invalid submission” tracking state rather than a generic
correction failure).

ZIP archives: phone scanners (CamScanner, Adobe Scan, …) upload a student's
pages as a .zip. Treating that as "unsupported" cost the student their whole
correction — they appeared in the class list and then silently got no mark at
all. A ZIP is therefore EXPANDED into its supported members, each member is
validated/normalised with exactly the rules above, and the members are marked
in archive (natural) order. Expansion is bounded (entry count, per-entry and
total uncompressed size, one level of nesting) so a hostile or accidental
archive cannot exhaust the disk or memory.
"""

from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path

from ..logging_setup import log_event

log = logging.getLogger("classroom.submission_validation")

SUPPORTED_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp",
}
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
#: Phone-scanner archives arrive under a few different mime labels.
ZIP_MIMES = {
    "application/zip", "application/x-zip-compressed",
    "application/zip-compressed", "multipart/x-zip",
}
SUPPORTED_SUFFIXES = {".pdf"} | SUPPORTED_IMAGE_SUFFIXES

_PDF_MAGIC = b"%PDF-"

#: ZIP local-file/empty-archive/spanned markers (a renamed .zip is rejected).
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# Bounded expansion: enough for a scanned homework set, small enough that a
# zip bomb cannot fill the disk.
MAX_ARCHIVE_ENTRIES = 200
MAX_ARCHIVE_ENTRY_BYTES = 40 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 200 * 1024 * 1024
#: A ZIP inside a ZIP is not a student submission.
MAX_ARCHIVE_DEPTH = 1

#: Entries macOS/most scanners add that are never the student's work.
_ARCHIVE_JUNK_PREFIXES = ("__MACOSX/",)


def _natural_key(name: str) -> list:
    """Sort scanned pages the way a human orders them: page2 before page10."""
    import re

    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", name)]


class InvalidSubmissionError(RuntimeError):
    """A submission has no usable attachment — maps to the INVALID state."""

    def __init__(self, reasons: list[str] | str):
        self.reasons = reasons if isinstance(reasons, list) else [reasons]
        super().__init__("Invalid submission: " + "; ".join(self.reasons))


def classify(filename: str, data: bytes, mime: str) -> dict:
    """Best-effort type detection from mime + filename suffix."""
    name = (filename or "").lower()
    if mime == "application/pdf" or name.endswith(".pdf"):
        return {"kind": "pdf"}
    if mime in SUPPORTED_IMAGE_MIMES or Path(name).suffix in SUPPORTED_IMAGE_SUFFIXES:
        return {"kind": "image"}
    if mime in ZIP_MIMES or Path(name).suffix == ".zip":
        return {"kind": "archive"}
    # Docs exported by the client already arrive as application/pdf; anything
    # else here is genuinely unexpected for a marking submission.
    return {"kind": "unsupported", "mime": mime, "filename": filename}


def _reason_check(filename: str, kind: str, data: bytes, mime: str) -> str | None:
    """Return a reason string if the file is obviously unusable, else None."""
    if not data:
        return "empty file"
    if kind == "pdf":
        if not data.startswith(_PDF_MAGIC):
            # A file with a PDF name/suffix that is not actually PDF bytes is
            # either corrupted or a mis-labelled (renamed) file.
            return "not a valid PDF (file appears corrupted or renamed)"
        return None
    if kind == "image":
        if not (mime.startswith("image/") or Path(filename.lower()).suffix in SUPPORTED_IMAGE_SUFFIXES):
            return f"unsupported file type ({mime or 'unknown'})"
        return None
    if kind == "archive":
        if not any(data.startswith(magic) for magic in _ZIP_MAGICS):
            return "not a valid ZIP archive (file appears corrupted or renamed)"
        return None
    return f"unsupported file type ({mime or 'unknown'})"


def _normalize_pdf(data: bytes, work_dir: Path) -> tuple[str | None, list[str]]:
    """Re-encode the PDF so pages render upright; returns (relative_name, warnings)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        return None, ["encrypted PDF is not supported"]
    pages = list(reader.pages)
    if not pages:
        return None, ["PDF contains no pages"]

    writer = PdfWriter()
    warnings: list[str] = []
    for page in pages:
        try:
            mb = page.mediabox
            width = float(getattr(mb, "width", 0) or 0)
            height = float(getattr(mb, "height", 0) or 0)
        except Exception:
            width = height = 0.0
        rotation = int(getattr(page, "rotation", 0) or 0) % 360
        if width and height and width > height and rotation % 180 == 0:
            warnings.append("landscape page detected (kept as-is)")
        writer.add_page(page)

    name = f"normalized_{uuid.uuid4().hex[:8]}.pdf"
    out_path = work_dir / name
    with out_path.open("wb") as handle:
        writer.write(handle)
    return name, warnings



def _normalize_image(data: bytes, mime: str, work_dir: Path) -> tuple[str | None, list[str]]:
    """Apply EXIF orientation so phone-photo homework is upright."""
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            # Re-encode to a canonical format based on source type.
            fmt = "PNG" if mime in ("image/png", "image/gif", "image/bmp") else "JPEG"
            ext = "png" if fmt == "PNG" else "jpg"
            out_name = f"normalized_{uuid.uuid4().hex[:8]}.{ext}"
            out_path = work_dir / out_name
            if fmt == "JPEG":
                img = img.convert("RGB")
            img.save(out_path, fmt)
            return out_name, []
    except Exception as exc:
        log.warning("image normalization failed (%s); keeping original", exc)
        img_name = f"normalized_{uuid.uuid4().hex[:8]}.img"
        out_path = work_dir / img_name
        out_path.write_bytes(data)
        return img_name, ["could not fully verify image (kept original)"]


def validate_and_normalize(filename: str, data: bytes, mime: str,
                           work_dir: Path, *, depth: int = 0) -> dict:
    """Validate one attachment and write normalized copy(ies) into ``work_dir``.

    Returns either
    ``{"ok": True, "kind": ..., "path": "stage_name", "paths": [...], "warnings": [...]}``
    or ``{"ok": False, "kind": ..., "reason": "..."}``.

    ``path`` is always the FIRST produced file (backwards compatible);
    ``paths`` is the complete list, which is longer than one only for an
    EXPANDED ZIP archive. Never touches the original bytes — every output is a
    fresh file in ``work_dir``.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    detection = classify(filename, data, mime)
    kind = detection.get("kind", "unsupported")
    reason = _reason_check(filename, kind, data, mime)
    if reason is not None:
        return {"ok": False, "kind": kind, "reason": reason}

    if kind == "archive":
        return _expand_archive(filename, data, work_dir, depth=depth)

    warnings: list[str] = []
    try:
        if kind == "pdf":
            name, w = _normalize_pdf(data, work_dir)
        else:
            name, w = _normalize_image(data, mime, work_dir)
        warnings = w
    except Exception as exc:  # noqa: BLE001
        log.warning("validation/normalization error for %s: %s", filename, exc)
        return {"ok": False, "kind": kind,
                "reason": "could not be processed (corrupted or unsupported)"}

    if not name:
        return {"ok": False, "kind": kind,
                "reason": warnings[0] if warnings else "could not be processed"}

    return {"ok": True, "kind": kind, "path": name, "paths": [name],
            "warnings": warnings}


def _expand_archive(filename: str, data: bytes, work_dir: Path, *,
                    depth: int = 0) -> dict:
    """Expand a student's .zip into its supported, normalized members.

    Members keep the archive's NATURAL order (page2 before page10) so the
    marked PDF reads in the order the student scanned it. Every member goes
    through the same validation/normalization as a direct attachment, so an
    unreadable page inside a ZIP is reported rather than silently dropped, and
    ``ok`` is only False when NOTHING usable is inside.
    """
    import zipfile

    if depth >= MAX_ARCHIVE_DEPTH:
        return {"ok": False, "kind": "archive",
                "reason": "nested archives are not supported"}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = [i for i in archive.infolist() if not i.is_dir()]
            infos = [i for i in infos
                     if not i.filename.startswith(_ARCHIVE_JUNK_PREFIXES)
                     and not Path(i.filename).name.startswith(".")]
            infos.sort(key=lambda i: _natural_key(i.filename))
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                return {"ok": False, "kind": "archive",
                        "reason": (f"archive contains {len(infos)} files "
                                   f"(more than the {MAX_ARCHIVE_ENTRIES} supported)")}

            produced: list[str] = []
            warnings: list[str] = []
            problems: list[str] = []
            total = 0
            for info in infos:
                size = int(info.file_size or 0)
                if size > MAX_ARCHIVE_ENTRY_BYTES:
                    problems.append(f"{info.filename}: file too large")
                    continue
                total += size
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    problems.append("archive is larger than the supported "
                                    "uncompressed size")
                    break
                try:
                    payload = archive.read(info)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{info.filename}: could not be read ({exc})")
                    continue
                member = Path(info.filename).name
                check = validate_and_normalize(member, payload, "", work_dir,
                                               depth=depth + 1)
                if not check.get("ok"):
                    problems.append(f"{info.filename}: {check.get('reason')}")
                    continue
                produced.extend(check.get("paths") or [check.get("path")])
                warnings.extend(check.get("warnings") or [])
    except zipfile.BadZipFile:
        return {"ok": False, "kind": "archive",
                "reason": "not a valid ZIP archive (corrupted or renamed)"}

    if not produced:
        return {"ok": False, "kind": "archive",
                "reason": "; ".join(problems[:4]) or "archive contains no "
                          "readable PDF or image pages"}
    if problems:
        warnings.append(
            f"{len(problems)} file(s) inside the archive could not be used: "
            + "; ".join(problems[:4]))
    log_event("classroom.validation", "archive expanded",
              entries=len(infos), usable=len(produced))
    return {"ok": True, "kind": "archive", "path": produced[0],
            "paths": produced, "warnings": warnings}
