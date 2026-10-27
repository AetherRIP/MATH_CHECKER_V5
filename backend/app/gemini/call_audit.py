"""One ledger per correction: every provider request, and why it was sent.

The engine used to have no idea how many requests a paper cost. That made
"too many AI calls" unmeasurable, so it could neither be reported nor fixed:
``job_bandwidth.ai_attempts`` stayed 0 because it only counted SUCCESSFUL
generations, and the 3,922 failed attempts measured in ``data/logs/app.log``
(the storm around an overloaded provider) were invisible.

The ledger is deliberately small and process-local. A correction runs in one
worker child, so a module singleton is the whole job; the pool persists the
final snapshot onto ``job_bandwidth`` and the desktop reads it from there.

A process does not only host corrections: the backend sends provider requests
of its own (mark-scheme recheck and extraction, the developer diagnostics)
where no correction is running. ``begin()`` therefore ARMS the ledger, and only
requests that say which job they belong to (``job_id``) are counted and charged
— see :func:`_counts_toward_ledger`.

Kinds counted (each one is an HTTP request to the provider):

* ``generate`` — a marking / extraction / audit generation attempt.
* ``upload``   — a Files API upload.
* ``file_get`` — a Files API lookup (used to validate a reused upload).

The budget is a SAFEGUARD, never a way to skip marking: it is high enough for
the largest legitimate paper (measured: 1 request per paper, 2 with the
independent second pass, +2 targeted recovery rounds) and only stops a request
storm that is already failing.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time

#: A correction that needs more than this many GENERATION attempts is not
#: marking any more — it is retrying a provider that is refusing. Healthy
#: papers measured 1 (single pass), 2 (independent second check) and at most
#: 4 (both recovery rounds). 12 leaves room for the mark-scheme extraction
#: (1-2) and a key failover without ever truncating real marking work.
DEFAULT_MAX_REQUESTS = 12

STAGE_OTHER = "other"
STAGE_UPLOAD = "upload"
STAGE_MARK_SCHEME = "mark_scheme"
STAGE_MARKING = "marking"
STAGE_RECOVERY = "recovery"
STAGE_AUDIT = "audit"
STAGE_DOUBLE_CHECK = "double_check"

KIND_GENERATE = "generate"
KIND_UPLOAD = "upload"
KIND_FILE_GET = "file_get"


class ProviderBudgetExceeded(RuntimeError):
    """The per-correction request budget is spent; nothing more is sent."""


_stage: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ai_call_stage", default=STAGE_OTHER)
_lock = threading.Lock()

_job_id: str | None = None
_started_at: float = 0.0
_sent = 0
_succeeded = 0
_failed = 0
_skipped = 0
_by_kind: dict[str, int] = {}
_by_stage: dict[str, int] = {}
_by_model: dict[str, int] = {}
_last_error: str | None = None


def begin(job_id: str | None = None) -> None:
    """Start a fresh ledger for one correction job."""
    global _job_id, _started_at, _sent, _succeeded, _failed, _skipped
    global _last_error
    with _lock:
        _job_id = job_id
        _started_at = time.time()
        _sent = _succeeded = _failed = _skipped = 0
        _by_kind.clear()
        _by_stage.clear()
        _by_model.clear()
        _last_error = None


def current_stage() -> str:
    return _stage.get()


@contextlib.contextmanager
def stage(name: str):
    """Attribute the requests made inside this block to one pipeline stage."""
    token = _stage.set(name or STAGE_OTHER)
    try:
        yield
    finally:
        _stage.reset(token)


def max_requests() -> int:
    """The generation-attempt budget for one correction (0 = unlimited)."""
    try:
        from ..config import get_config
        value = int(getattr(get_config().gemini, "max_requests_per_job",
                            DEFAULT_MAX_REQUESTS) or 0)
    except Exception:
        value = DEFAULT_MAX_REQUESTS
    return value


def remaining() -> int | None:
    budget = max_requests()
    if budget <= 0:
        return None
    return max(0, budget - _sent)


def _counts_toward_ledger(job_id: str | None) -> bool:
    """True when a request belongs to the correction this ledger describes.

    The ledger is a process-local singleton while the budget is per
    CORRECTION, so a request may only be charged when the caller says it is
    part of the job that was begun. Two cases are deliberately NOT charged:

    * nothing has been begun in this process (``_job_id is None``) — both the
      backend and the API process it hosts send provider requests with no
      correction in flight (mark-scheme recheck, mark-scheme extraction, the
      developer diagnostics), and a budget that belongs to no job must not
      stop them; and
    * the caller NAMES a different job than the ledger's.

    A caller that names no job at all (``None``) is still charged while a
    ledger is active, so an ad-hoc script — or a test — that begins a ledger
    keeps the protection it asked for.
    """
    if _job_id is None:
        return False
    return job_id is None or job_id == _job_id


def note_send(kind: str = KIND_GENERATE, model: str | None = None,
              job_id: str | None = None) -> None:
    """Count one request about to be sent, enforcing the generation budget.

    ``job_id`` is the job the caller belongs to (the worker child's client
    passes it) so the per-correction budget cannot be charged to another job's
    requests. A request outside the ledger's job is not counted here at all.
    """
    global _sent
    with _lock:
        if not _counts_toward_ledger(job_id):
            return
        if kind == KIND_GENERATE:
            budget = max_requests()
            if budget > 0 and _sent >= budget:
                raise ProviderBudgetExceeded(
                    f"This correction has already used its budget of {budget} "
                    "AI requests. Remaining questions are not being marked by "
                    "more retries — re-run the paper once the provider is "
                    "healthy, or mark the affected questions by hand.")
            _sent += 1
        _by_kind[kind] = _by_kind.get(kind, 0) + 1
        # Stages are about MARKING work: an upload is not "the marking stage",
        # and mixing the two made the breakdown disagree with the request count
        # (1 generation + 1 upload + 1 lookup read as "3 requests" but
        # "marking 1").
        if kind == KIND_GENERATE:
            _by_stage[_stage.get()] = _by_stage.get(_stage.get(), 0) + 1
        if model:
            _by_model[model] = _by_model.get(model, 0) + 1


def note_result(*, ok: bool, error: str | None = None) -> None:
    global _succeeded, _failed, _last_error
    with _lock:
        if ok:
            _succeeded += 1
        else:
            _failed += 1
            if error:
                _last_error = str(error)[:300]


def note_skip(reason: str = "cooling down",
              job_id: str | None = None) -> None:
    """A request that was NOT sent because the model/provider was cooling."""
    global _skipped
    with _lock:
        if not _counts_toward_ledger(job_id):
            return
        _skipped += 1


def snapshot() -> dict:
    """What this correction cost, by stage and kind (safe to persist)."""
    with _lock:
        budget = max_requests()
        return {
            "job_id": _job_id,
            # Generation attempts: the number the budget governs.
            "requests": _sent,
            # Every provider round-trip, including uploads and lookups.
            "total_requests": sum(_by_kind.values()),
            "succeeded": _succeeded,
            "failed": _failed,
            "skipped": _skipped,
            "budget": budget,
            "budget_exceeded": bool(budget > 0 and _sent >= budget),
            "by_kind": dict(_by_kind),
            "by_stage": dict(_by_stage),
            "by_model": dict(_by_model),
            "seconds": round(time.time() - _started_at, 1) if _started_at else 0,
            "last_error": _last_error,
        }


def reset() -> None:
    """Testing helper: forget the current job."""
    begin(None)
