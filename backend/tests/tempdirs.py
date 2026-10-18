"""Temporary test directories that are actually deleted on Windows.

``tempfile.mkdtemp`` has no owner and no teardown: every backend test session
used to leave its data directory behind, and after a few weeks the temp folder
held more than a thousand ``mc5_*`` trees. Creating the directory here instead
means it is registered, and :func:`remove_all` deletes it when the session ends.

Deleting is the part Windows makes hard. A directory that a process still has
open cannot be removed (``WinError 32: The process cannot access the file
because it is being used by another process``), and a test run always has two
such handles:

* ``logs/app.log`` — ``app.logging_setup.setup_logging`` attaches a
  ``FileHandler`` to the root logger, and that handler stays open for the whole
  session;
* ``app.db`` (+ ``-wal``/``-shm``) — the SQLite connections ``app.db`` caches
  per thread.

Deleting first and hoping is exactly how a "cleanup" step ends up doing
nothing, so :func:`remove` closes every handler whose file lives in the
directory, closes the application's cached database connections
(``app.db.close_all()``), then retries the delete a few times with backoff for
the locks Windows releases asynchronously. Whatever still cannot be deleted is
RETURNED, never swallowed: the session hook prints it so a leak is visible
instead of silent.

Safety: :func:`remove` refuses any path that is not inside the system temp
directory. A bug that passes the wrong path must not be able to delete the
teacher's ``data/`` folder.
"""
from __future__ import annotations

import gc
import logging
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

#: Directories created by this session, in creation order.
_REGISTRY: dict[Path, str] = {}
#: How many delete attempts before a directory is reported as leaked.
ATTEMPTS = 6


def make(prefix: str) -> Path:
    """Create a registered temp directory; it is deleted at session end."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _REGISTRY[path] = prefix
    return path


def registered() -> list[Path]:
    """Directories this session created and has not yet removed."""
    return list(_REGISTRY)


def forget(path: Path | str) -> None:
    """Stop tracking a directory (after deleting it by other means)."""
    _REGISTRY.pop(Path(path), None)


def remove(path: Path | str, *, attempts: int = ATTEMPTS) -> list[str]:
    """Delete ``path``; return what is left behind (empty list = gone).

    Releases the application's open database and log handles first, then retries
    with backoff. The return value is the reason this function exists: a caller
    that ignores it is back to silent leaks, so the session hook prints it.
    """
    target = Path(path)
    if not _inside_temp(target):
        return [f"{target} (refused: not inside {_temp_root()})"]

    _release_handles(target)
    leftovers: list[str] = []
    for attempt in range(attempts):
        leftovers = _rmtree(target)
        if not leftovers:
            forget(target)
            return []
        # Handles can appear between the release and the delete (a worker
        # thread reopening its connection, an antivirus scanner): release again
        # once, then let the remaining attempts ride out a transient lock.
        if attempt == 1:
            _release_handles(target)
        gc.collect()
        time.sleep(0.15 * (2 ** attempt))
    return leftovers


def remove_all(paths=None) -> dict[Path, list[str]]:
    """Delete every directory this session created; return the failures.

    The returned mapping is empty when everything was cleaned up. Each value is
    the list of paths that are still on disk, which names the file whose handle
    was still open - the one piece of information needed to fix a leak.

    ``paths`` restricts the sweep to a subset, which the tests use so they do not
    delete the session's own data directory mid-run.
    """
    targets = registered() if paths is None else [Path(p) for p in paths]
    failures: dict[Path, list[str]] = {}
    for path in targets:
        leftovers = remove(path)
        if leftovers:
            failures[path] = leftovers
    return failures


def _temp_root() -> Path:
    try:
        return Path(tempfile.gettempdir()).resolve()
    except OSError:  # pragma: no cover - the temp root always resolves
        return Path(tempfile.gettempdir())


def _inside_temp(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    root = _temp_root()
    if resolved == root:
        return False
    return resolved.is_relative_to(root)


def _release_handles(path: Path) -> None:
    """Close every application handle that could be holding something in ``path``.

    Imports are lazy and guarded: this runs at session end, possibly before the
    application was imported at all (a collection error, a run of unrelated
    tests), and a cleanup step must never raise.

    Handlers are closed individually rather than through
    ``logging_setup.reset_logging()`` on purpose: that function clears the ROOT
    logger, and at session end the root also carries pytest's own capture
    handler - a cleanup step must not disturb the reporter that is about to
    print the result.
    """
    # Any handler still holding a file inside the directory, wherever it was
    # attached (a test that configured its own logger, a library handler).
    # Each one is DETACHED as well as closed: a closed handler left attached
    # turns any later log record into a "--- Logging error ---" traceback.
    loggers = [logging.getLogger()]
    loggers.extend(v for v in logging.Logger.manager.loggerDict.values()
                   if isinstance(v, logging.Logger))
    for logger in loggers:
        for handler in list(logger.handlers):
            if _close_if_inside(handler, path):
                logger.removeHandler(handler)

    try:
        from app import db

        db.close_all()
    except Exception:  # noqa: BLE001 - same reason as above
        pass


def _close_if_inside(handler: logging.Handler, path: Path) -> bool:
    """Close ``handler`` when its file lives inside ``path``; True if it did."""
    filename = getattr(handler, "baseFilename", None)
    if not filename:
        return False
    try:
        inside = Path(filename).resolve().is_relative_to(path.resolve())
    except OSError:
        return False
    if not inside:
        return False
    try:
        handler.flush()
        handler.close()
    except Exception:  # noqa: BLE001 - closing must never fail the cleanup
        pass
    return True


def _rmtree(path: Path) -> list[str]:
    """One delete attempt; returns the paths still present afterwards."""

    def _force(func, failed_path, _exc):
        # Read-only files are common in SQLite/Windows backups; clearing the
        # attribute is safe inside the directory being deleted.
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except Exception:  # noqa: BLE001 - report via the leftover list instead
            pass

    if not path.exists():
        return []
    shutil.rmtree(path, onerror=_force)
    if not path.exists():
        return []
    remaining: list[str] = []
    for root, _dirs, files in os.walk(path):
        for name in files:
            remaining.append(str(Path(root) / name))
            if len(remaining) >= 5:
                return remaining
    return remaining or [str(path)]
