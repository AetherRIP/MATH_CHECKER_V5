"""Gemini Files API registry (bandwidth §6/§9).

The Gemini Files API stores uploads server-side for ~48h and scopes each file
to the API key that uploaded it. That means the app can upload an input ONCE
and reuse the returned file reference for every later job/retry on the same
key — the dominant correction bandwidth sink is otherwise the mark scheme PDF
being re-uploaded for every one of a class's 75 students.

This registry persists ``(key fingerprint, content SHA-256, purpose) ->
remote file`` in SQLite so both the API process and worker child processes
share it. A lookup is only a hint: the caller must still verify the remote is
alive (``files.get``) before reusing it, because the provider can expire or
delete files at any time. Every function is best-effort — accounting/registry
failures must never break a correction.

Permanent storage note: this stores file IDENTIFIERS, never file bytes.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time

from .. import db

# Provider-side retention is ~48h; stop trusting a reference well before that.
_DEFAULT_TTL_SECONDS = 40 * 60 * 60

_lock = threading.Lock()
_sha_cache: dict[tuple[str, int, int], str] = {}


def content_sha256(path: str) -> str:
    """SHA-256 of a local file, memoized on (path, size, mtime).

    Prepared inputs are immutable for a job's lifetime, so hashing a 10 MB
    mark scheme once per worker process is enough.
    """
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_size, int(st.st_mtime))
    with _lock:
        cached = _sha_cache.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    with _lock:
        if len(_sha_cache) > 256:
            _sha_cache.clear()
        _sha_cache[key] = value
    return value


def lookup(fingerprint: str, sha256: str, purpose: str) -> dict | None:
    """Return the live registry row for this key+content, or None."""
    if not (fingerprint and sha256 and purpose):
        return None
    try:
        row = db.query_one(
            "SELECT * FROM gemini_remote_files "
            "WHERE fingerprint = ? AND sha256 = ? AND purpose = ?",
            (fingerprint, sha256, purpose),
        )
        if row is None:
            return None
        if float(row["expires_at"]) <= time.time():
            return None
        return dict(row)
    except Exception:
        return None


def store(fingerprint: str, sha256: str, purpose: str, remote_name: str,
          size_bytes: int, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
    now = time.time()
    try:
        db.execute(
            "INSERT INTO gemini_remote_files(fingerprint, sha256, purpose, "
            "remote_name, size_bytes, uploaded_at, expires_at, last_used_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(fingerprint, sha256, purpose) DO UPDATE SET "
            "remote_name = excluded.remote_name, "
            "size_bytes = excluded.size_bytes, "
            "uploaded_at = excluded.uploaded_at, "
            "expires_at = excluded.expires_at, "
            "last_used_at = excluded.last_used_at",
            (fingerprint, sha256, purpose, remote_name, int(size_bytes),
             now, now + max(60, ttl_seconds), now),
        )
    except Exception:
        pass


def touch(fingerprint: str, sha256: str, purpose: str) -> None:
    try:
        db.execute(
            "UPDATE gemini_remote_files SET last_used_at = ? "
            "WHERE fingerprint = ? AND sha256 = ? AND purpose = ?",
            (time.time(), fingerprint, sha256, purpose),
        )
    except Exception:
        pass


def forget(fingerprint: str, sha256: str, purpose: str) -> None:
    try:
        db.execute(
            "DELETE FROM gemini_remote_files "
            "WHERE fingerprint = ? AND sha256 = ? AND purpose = ?",
            (fingerprint, sha256, purpose),
        )
    except Exception:
        pass


def record_job_file(job_id: str | None, fingerprint: str, remote_name: str,
                    purpose: str) -> None:
    """Remember that a job owns a provider-side file (deleted when the job
    reaches a terminal state — never mid-retry)."""
    if not job_id or not remote_name:
        return
    try:
        db.execute(
            "INSERT INTO job_remote_files(job_id, fingerprint, remote_name, "
            "purpose, created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(job_id, remote_name) DO NOTHING",
            (job_id, fingerprint, remote_name, purpose, time.time()),
        )
    except Exception:
        pass


def job_files(job_id: str) -> list[dict]:
    try:
        return [dict(r) for r in db.query(
            "SELECT job_id, fingerprint, remote_name, purpose "
            "FROM job_remote_files WHERE job_id = ?", (job_id,))]
    except Exception:
        return []


def clear_job_files(job_id: str) -> None:
    try:
        db.execute("DELETE FROM job_remote_files WHERE job_id = ?", (job_id,))
    except Exception:
        pass


def purge_expired(now_ts: float | None = None) -> int:
    """Drop expired rows so the registry stays small. Returns rows removed."""
    now_ts = time.time() if now_ts is None else now_ts
    try:
        with db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM gemini_remote_files WHERE expires_at <= ?",
                (now_ts,),
            )
            return cur.rowcount or 0
    except Exception:
        return 0
