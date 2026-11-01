"""Secure-at-rest store for credentials and peer key material.

The store exists because a device credential, a device private key and a
department root key must not sit in a plain world-readable file. On Windows it
uses DPAPI (``CryptProtectData``), which scopes the ciphertext to the *current
user*: even an administrator copying the file to another machine cannot read it.

What changed and why (R-08)
---------------------------
The first version of this module degraded to plaintext whenever DPAPI was
unavailable, and ``save`` returned normally. A caller therefore could not tell
"protected" from "written in the clear", and the department root key — the one
secret whose loss invalidates every certificate a department ever issued —
could be written unencrypted while every status surface reported success.

Now a write either encrypts or fails:

* :func:`save` requires encryption by default and raises
  :class:`SecureStoreUnavailable` when DPAPI cannot be used. Nothing is written
  in that case, and a file already at the path is left untouched.
* A caller that genuinely wants the documented plaintext mode (a development
  machine, or the probe in :mod:`app.peer.service` that *reports* the outcome)
  has to ask for it explicitly with ``require_encryption=False`` and receives
  the mode it actually got back.
* :func:`load` raises :class:`SecureStoreCorrupt` when a DPAPI-protected file
  cannot be decrypted. It never returns ``None`` for a file that exists and is
  unreadable, because ``None`` means "absent" to every caller — and a broken
  store read as absent is how a fresh device key silently replaces one that
  still has live certificates.

Compatibility
-------------
Files are unchanged on disk: a one-byte scheme marker (``MC1D`` / ``MC1P``)
precedes the payload, and a file with no marker is still read as plain JSON.
Existing data therefore keeps working — including a plaintext file written by
the old code — and no migration is required. What changed is that a *new*
unmarked or plaintext file is never produced without the caller saying so.

Every OS call is still guarded: DPAPI failure is reported, never swallowed into
an except-anywhere clause.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

_MARKER_DPAPI = b"MC1D"
_MARKER_PLAIN = b"MC1P"

#: What a file on disk actually is.
MODE_DPAPI = "dpapi"
MODE_PLAINTEXT = "plaintext"
MODE_LEGACY = "legacy"      # no marker: written by something else/an older build
MODE_ABSENT = "absent"


class SecureStoreError(OSError):
    """Base class for "this store could not do what was asked"."""


class SecureStoreUnavailable(SecureStoreError):
    """Encryption is required and this process cannot provide it.

    Raised *before* anything is written, so a failure cannot leave a downgraded
    file behind and cannot destroy the protected one already there.
    """


class SecureStoreCorrupt(SecureStoreError):
    """A file exists, but its contents cannot be used."""


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
    """True only where the DPAPI entry points exist for this process."""
    return os.name == "nt" and hasattr(ctypes, "windll")


def dpapi_available() -> bool:
    """Whether a real encrypt/decrypt round trip succeeds *right now*.

    A capability question, not a platform question: ``_can_use_dpapi`` is true on
    any Windows box, but a service account with no user profile, or one running
    in a context whose DPAPI scope cannot be reached, fails at call time. This
    performs the round trip so ``save`` can fail closed with a specific message
    instead of discovering it halfway through a write.
    """
    if not _can_use_dpapi():
        return False
    try:
        return _dpapi(_dpapi(b"probe", protect=True), protect=False) == b"probe"
    except OSError:
        return False


def status(path: str | Path) -> str:
    """What is on disk at ``path``: one of the ``MODE_*`` constants.

    Reads four bytes. Used by the startup probe and by the tests that have to
    state plainly that a file is *not* protected.
    """
    path = Path(path)
    if not path.exists():
        return MODE_ABSENT
    try:
        head = path.read_bytes()[:4]
    except OSError:
        return MODE_ABSENT
    if head == _MARKER_DPAPI:
        return MODE_DPAPI
    if head == _MARKER_PLAIN:
        return MODE_PLAINTEXT
    return MODE_LEGACY


def save(path: str | Path, obj: dict, *,
         require_encryption: bool = True) -> str:
    """Write ``obj`` to ``path`` atomically; return the mode actually written.

    ``require_encryption`` defaults to True and is the safe choice: when DPAPI
    cannot protect the payload this raises :class:`SecureStoreUnavailable` and
    writes nothing at all — not a temporary file, not a downgraded copy, and the
    existing file at ``path`` is left exactly as it was.

    Passing ``require_encryption=False`` is a deliberate, per-call decision to
    accept the documented plaintext mode (``chmod 600``, same user only). It is
    never inferred from a failed DPAPI call.
    """
    path = Path(path)
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")

    payload: bytes | None = None
    mode = MODE_PLAINTEXT
    if _can_use_dpapi():
        try:
            payload = _MARKER_DPAPI + _dpapi(raw, protect=True)
            mode = MODE_DPAPI
        except OSError as exc:
            if require_encryption:
                raise SecureStoreUnavailable(
                    f"the secure store could not encrypt {path.name!r}: {exc}. "
                    "DPAPI is scoped to the signed-in Windows account, so it "
                    "fails for a service running as another account or with no "
                    "loaded user profile. Nothing was written. Run the peer "
                    "service inside the owner's own session, or re-run this "
                    "action while signed in as that user."
                ) from exc
    elif require_encryption:
        if os.name == "nt":
            raise SecureStoreUnavailable(
                f"the secure store could not encrypt {path.name!r}: DPAPI is not "
                "available to this process. It is scoped to the signed-in Windows "
                "account, so it fails for a service running as another account or "
                "with no loaded user profile. Nothing was written; any file "
                "already at that path is unchanged."
            )
        raise SecureStoreUnavailable(
            f"the secure store cannot encrypt {path.name!r} on this platform "
            f"({os.name}); DPAPI is Windows-only. Nothing was written. This "
            "protected file must be created on the Windows machine that owns it."
        )

    if payload is None:                     # explicit, requested plaintext mode
        payload = _MARKER_PLAIN + raw
        mode = MODE_PLAINTEXT

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(payload)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SecureStoreError(
            f"the secure store could not write {path.name!r}: {exc}"
        ) from exc
    return mode


def load(path: str | Path) -> dict | None:
    """Read a file written by :func:`save`.

    ``None`` means the file is **absent** — nothing else. A file that exists but
    cannot be decrypted raises :class:`SecureStoreCorrupt`, because treating it
    as absent is how a device silently mints a replacement key while its old
    certificates are still live.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SecureStoreCorrupt(f"{path.name!r} could not be read: {exc}") from exc

    if payload.startswith(_MARKER_DPAPI):
        try:
            raw = _dpapi(payload[len(_MARKER_DPAPI):], protect=False)
        except OSError as exc:
            raise SecureStoreCorrupt(
                f"{path.name!r} is DPAPI-protected but cannot be decrypted for "
                "this Windows account. The file was written for a different "
                "user or machine profile; it has not been replaced or deleted. "
                "Restore it under the original account, or re-invite this device "
                "if the key is genuinely gone."
            ) from exc
    elif payload.startswith(_MARKER_PLAIN):
        raw = payload[len(_MARKER_PLAIN):]
    else:
        raw = payload                        # legacy/foreign file: plain JSON

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SecureStoreCorrupt(
            f"{path.name!r} is not readable JSON; it is either corrupt or was "
            "written by an incompatible version. The file is intact on disk."
        ) from exc
    if not isinstance(data, dict):
        raise SecureStoreCorrupt(
            f"{path.name!r} does not contain a JSON object.")
    return data
