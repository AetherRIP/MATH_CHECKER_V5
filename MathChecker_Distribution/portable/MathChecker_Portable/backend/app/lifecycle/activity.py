"""In-process registry of critical operations that must not be interrupted.

"Nobody has a tab open" is not on its own a safe reason to stop: a database
transaction may be mid-write, a report may be being written to disk, a sync may
be committing results. Those are short operations, but they are not *nothing*,
and stopping the process in the middle of one is exactly the failure this
module exists to prevent.

Every entry is a ``with`` block, so the count is correct even if the operation
raises::

    with activity.critical("report_write"):
        ...

:mod:`app.db` marks every write transaction here, which covers the whole
storage layer for free. Longer file/report operations can add their own label.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_lock = threading.Lock()
_counts: dict[str, int] = {}


def begin(label: str) -> None:
    """Mark the start of a critical operation (must be paired with ``end``)."""
    name = label or "operation"
    with _lock:
        _counts[name] = _counts.get(name, 0) + 1


def end(label: str) -> None:
    """Mark the end of a critical operation (never goes below zero)."""
    name = label or "operation"
    with _lock:
        left = _counts.get(name, 0) - 1
        if left > 0:
            _counts[name] = left
        else:
            _counts.pop(name, None)


@contextmanager
def critical(label: str) -> Iterator[None]:
    begin(label)
    try:
        yield
    finally:
        end(label)


def busy() -> list[str]:
    """Labels with at least one operation in flight, sorted for stable output."""
    with _lock:
        return sorted(name for name, count in _counts.items() if count > 0)


def in_flight() -> int:
    """Total number of in-flight critical operations."""
    with _lock:
        return sum(count for count in _counts.values() if count > 0)


def reset() -> None:
    """Clear all counts. Tests only — a live process never needs this."""
    with _lock:
        _counts.clear()
