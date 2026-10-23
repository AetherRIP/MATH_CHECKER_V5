"""Safe ``Content-Disposition`` headers for exported files.

A report is named after a student or a class, and those names are not ASCII:
Arabic, Chinese, accents, even marks such as "¼" appear in real exports. Two
things break when that name is dropped straight into the header:

* HTTP headers are bytes, and Starlette encodes them as latin-1, so a name with
  Arabic or Chinese characters raises ``UnicodeEncodeError`` and the export
  answers 500 instead of downloading;
* a latin-1 header that happens to encode (``ä``, ``¼``) is then decoded as
  UTF-8 by tooling and API clients — the file arrives with a mangled name.

RFC 6266/5987 solves both: send an ASCII-only ``filename`` for old clients and
the real name in ``filename*=UTF-8''…``. Every export route uses these helpers
so the behaviour cannot drift between report types.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["attachment_header", "inline_header"]

_FALLBACK = "download"


def _ascii_fallback(filename: str) -> str:
    """An ASCII-only name that keeps the extension when one exists."""
    text = filename.strip()
    if "." in text:
        base, _, extension = text.rpartition(".")
        extension = "." + "".join(c for c in extension if c.isascii() and c.isalnum())
    else:
        base, extension = text, ""
    cleaned = "".join(
        c for c in base if c.isascii() and (c.isalnum() or c in (" ", "-", "_", "."))
    ).strip()
    cleaned = " ".join(cleaned.split())
    return (cleaned or _FALLBACK) + extension


def _disposition(kind: str, filename: str) -> str:
    safe = _ascii_fallback(filename)
    encoded = quote(filename, safe="")
    return f"{kind}; filename=\"{safe}\"; filename*=UTF-8''{encoded}"


def attachment_header(filename: str) -> str:
    """``Content-Disposition`` for a downloaded file (report, CSV, export)."""
    return _disposition("attachment", filename)


def inline_header(filename: str) -> str:
    """``Content-Disposition`` for content shown in place (a paper page)."""
    return _disposition("inline", filename)
