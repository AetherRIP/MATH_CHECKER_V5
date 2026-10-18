"""The temp-directory cleanup itself: it must delete, and say when it cannot.

This is the regression test for a leak nobody noticed for weeks: every backend
session created ``%TEMP%/mc5_test_*`` with no owner and never removed it, so
thousands of scratch trees accumulated. The interesting cases are the Windows
ones - a directory that still has ``logs/app.log`` or ``app.db`` open cannot be
deleted at all (``WinError 32``), so a cleanup that only calls ``rmtree`` would
"work" on Linux and leave everything behind on the teacher's machine.
"""
from __future__ import annotations

import logging
import os
import tempfile
import types
from pathlib import Path

import pytest

import tempdirs

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_make_registers_the_directory_and_remove_deletes_it():
    path = tempdirs.make("mc5_cleanup_probe_")
    try:
        assert path.is_dir()
        assert path in tempdirs.registered()

        assert tempdirs.remove(path) == []
        assert not path.exists()
        assert path not in tempdirs.registered()
    finally:
        tempdirs.remove(path)


def test_an_open_log_file_does_not_block_deletion():
    """The exact handle that leaked: the app's FileHandler on logs/app.log."""
    path = tempdirs.make("mc5_cleanup_log_")
    (path / "logs").mkdir()
    handler = logging.FileHandler(path / "logs" / "app.log", encoding="utf-8")
    logger = logging.getLogger("tempdirs.cleanup-probe")
    logger.addHandler(handler)
    try:
        logger.warning("holding the file open, as the real app does")
        # On Windows this handle makes a plain rmtree raise WinError 32.
        assert getattr(handler, "stream", None) is not None

        assert tempdirs.remove(path) == []
        assert not path.exists()
        assert handler not in logger.handlers, \
            "a handler whose file was deleted must be detached as well as closed"
        # Logging after the cleanup must not turn into a "--- Logging error ---".
        logger.warning("after cleanup")
    finally:
        logger.removeHandler(handler)
        handler.close()
        tempdirs.remove(path)


def test_an_open_database_connection_does_not_block_deletion(monkeypatch):
    """The second handle: app.db's cached SQLite connection (plus -wal/-shm)."""
    from app import db

    path = tempdirs.make("mc5_cleanup_db_")
    monkeypatch.setattr(db, "DB_PATH", path / "app.db")
    db.close_all()
    db._local.conn = None
    try:
        db.init_db()
        with db.transaction() as conn:
            conn.execute("INSERT INTO app_meta(key, value) VALUES('probe','1')"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        assert (path / "app.db").is_file()
        assert getattr(db._local, "conn", None) is not None, \
            "the connection must be cached, or this is not the real case"

        assert tempdirs.remove(path) == []
        assert not path.exists(), "app.db and its -wal/-shm must go too"
        assert getattr(db._local, "conn", None) is None, \
            "the cleanup must close the connection it removed"
    finally:
        db.close_all()
        tempdirs.remove(path)


def test_a_directory_it_cannot_delete_is_reported_not_swallowed():
    """A leak must be visible: the caller gets the files that are still open."""
    path = tempdirs.make("mc5_cleanup_locked_")
    held = open(path / "still-open.txt", "w", encoding="utf-8")
    try:
        held.write("holding this file without FILE_SHARE_DELETE\n")
        held.flush()

        leftovers = tempdirs.remove(path, attempts=1)

        if os.name == "nt":
            assert leftovers, "a file held open must be reported, not hidden"
            assert any("still-open.txt" in item for item in leftovers), leftovers
            assert path.exists()
        else:
            # POSIX unlinks an open file happily; the reporting contract is
            # identical, there is simply nothing to report.
            assert leftovers == []
    finally:
        held.close()
        tempdirs.remove(path)


def test_remove_refuses_anything_outside_the_temp_directory():
    """A bug that passes the wrong path must not delete the teacher's data."""
    target = REPO_ROOT / "backend"

    leftovers = tempdirs.remove(target)

    assert leftovers and "refused" in leftovers[0]
    assert target.is_dir(), "a refused path is left untouched"
    assert (target / "app").is_dir()


def test_remove_all_clears_the_registry():
    # Deliberately a SUBSET: sweeping everything would delete the session's own
    # data directory while the suite is still running.
    first = tempdirs.make("mc5_cleanup_many_")
    second = tempdirs.make("mc5_cleanup_many_")
    try:
        failures = tempdirs.remove_all([first, second])
        assert failures == {}
        assert not first.exists() and not second.exists()
        assert first not in tempdirs.registered()
        assert second not in tempdirs.registered()
    finally:
        tempdirs.remove(first)
        tempdirs.remove(second)


def test_every_fixture_directory_is_created_through_the_registry():
    """The session directory must be registered, or the sweep cannot see it.

    Read from the conftest module rather than ``MC5_DATA_DIR``: other tests
    legitimately delete that variable (demo/portability tests), so the
    environment is not a stable record of what this session created.
    """
    import conftest

    session_dir = conftest._TMP
    assert session_dir in tempdirs.registered()
    assert session_dir.parent.resolve() == Path(tempfile.gettempdir()).resolve()
    assert tempdirs.registered().count(session_dir) == 1, "registered exactly once"


def _fake_session(written: list[str]):
    """A session whose terminal reporter records what the hook prints."""

    class _Reporter:
        def write_line(self, text, **_kwargs):
            written.append(text)

    class _PluginManager:
        def getplugin(self, _name):
            return _Reporter()

    return types.SimpleNamespace(
        config=types.SimpleNamespace(pluginmanager=_PluginManager()))


def test_the_session_hook_reports_a_leak_instead_of_hiding_it(monkeypatch):
    """The two hooks that make this a fix rather than a silent no-op."""
    import conftest

    leaked = Path(tempfile.gettempdir()) / "mc5_test_left_behind"
    monkeypatch.setattr(tempdirs, "remove_all",
                        lambda *a, **k: {leaked: [str(leaked / "logs" / "app.log")]})
    written: list[str] = []

    conftest.pytest_sessionfinish(_fake_session(written), 0)

    assert written, "a directory that survived must be reported"
    assert "WARNING" in written[0]
    assert str(leaked) in written[0]
    assert "app.log" in written[0], "the still-open file is what names the bug"


def test_the_session_hook_is_silent_when_everything_was_deleted(monkeypatch):
    import conftest

    monkeypatch.setattr(tempdirs, "remove_all", lambda *a, **k: {})
    written: list[str] = []

    conftest.pytest_sessionfinish(_fake_session(written), 0)

    assert written == [], "a clean run must not print noise"


@pytest.mark.parametrize("prefix", ["mc5_test_", "mc5_child_", "mc5_ws_"])
def test_the_prefixes_the_suite_used_to_leak_are_handled(prefix):
    path = tempdirs.make(prefix)
    try:
        assert tempdirs.remove(path) == []
        assert not path.exists()
    finally:
        tempdirs.remove(path)
