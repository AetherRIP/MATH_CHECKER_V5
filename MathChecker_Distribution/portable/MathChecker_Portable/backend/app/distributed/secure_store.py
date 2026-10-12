"""Small secure-at-rest store for the device credential (§8).

The device credential must not sit in a plain world-readable file. Where the OS
offers per-user encryption we use it; elsewhere we fall back to owner-only file
permissions. The two are interoperable: a file carries a one-byte scheme marker
so a machine that gains or loses DPAPI never loses its credential.

* Windows  -> DPAPI (``CryptProtectData`` scoped to the current user), so the
  file is unreadable to any other account on the machine, even an admin copying
  it to another PC.
* Other    -> plaintext JSON with ``chmod 600``.

Every OS call is guarded: a failure degrades to the fallback rather than
preventing the agent from starting.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

_MARKER_DPAPI = b"MC1D"
_MARKER_PLAIN = b"MC1P"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob = _blob(data)
    out_blob = _DataBlob()
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None,
                                      None, 0x01, ctypes.byref(out_blob))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None,
                                        None, 0x01, ctypes.byref(out_blob))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _can_use_dpapi() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def save(path: str | Path, obj: dict) -> None:
    """Write ``obj`` to ``path`` atomically, encrypted where possible."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    payload = _MARKER_PLAIN + raw
    if _can_use_dpapi():
        try:
            payload = _MARKER_DPAPI + _dpapi(raw, protect=True)
        except OSError:
            payload = _MARKER_PLAIN + raw
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load(path: str | Path) -> dict | None:
    """Read a file written by :func:`save`; None when absent or unreadable."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = path.read_bytes()
        if payload.startswith(_MARKER_DPAPI):
            raw = _dpapi(payload[len(_MARKER_DPAPI):], protect=False)
        elif payload.startswith(_MARKER_PLAIN):
            raw = payload[len(_MARKER_PLAIN):]
        else:                       # legacy/foreign file: treat as plain JSON
            raw = payload
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
