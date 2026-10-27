"""Regression: a SQLite connection must not outlive the thread that opened it.

Measured defect (before the fix): every correction spawned one short-lived
``<worker>-stdout`` reader thread (``app.workers.adapter``) and that thread's
first query opened a thread-local connection which ``db._connections`` then kept
for the life of the process — that registry is only ever emptied by
``close_all()`` (factory reset). A finished thread's connection was therefore
never closed: 60 consecutive jobs left 62 open connections, +199 file handles
and +46 MB RSS, growing linearly with no plateau.

These tests use plain threads (the same shape as the adapter's reader thread)
so they stay fast and deterministic; the end-to-end growth is measured
separately in the audit report.
"""

import gc
import sqlite3
import threading
import time

import pytest

from app import db


def _settle(predicate, timeout: float = 5.0) -> bool:
    """Give the collector a chance to run the pending thread finalizers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        gc.collect()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_a_connection_is_closed_when_its_owning_thread_is_gone():
    box: dict = {}

    def work():
        box["conn"] = db.get_connection()   # the thread's first DB use

    thread = threading.Thread(target=work, name="transient-job")
    thread.start()
    thread.join()
    conn = box["conn"]

    assert conn in db._connections, \
        "a connection in use must stay registered so close_all() can reach it"

    del thread    # exactly what the worker adapter does after joining it

    assert _settle(lambda: conn not in db._connections), (
        "the connection opened by a finished thread is still registered: it "
        "would never be closed and its file handles would leak")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_a_live_thread_keeps_a_usable_connection():
    """The fix must not close a connection whose thread is still running."""
    ready = threading.Event()
    release = threading.Event()
    box: dict = {}

    def work():
        box["conn"] = db.get_connection()
        box["first"] = db.query("SELECT 1 AS n")[0]["n"]
        ready.set()
        release.wait(timeout=10)
        box["second"] = db.query("SELECT 2 AS n")[0]["n"]

    thread = threading.Thread(target=work, name="long-lived")
    thread.start()
    try:
        assert ready.wait(timeout=10)
        gc.collect()
        assert box["conn"] in db._connections
        assert box["conn"].execute("SELECT 1").fetchone()[0] == 1
    finally:
        release.set()
        thread.join()

    assert box["first"] == 1 and box["second"] == 2, \
        "a live thread must keep reusing its cached connection"
    del thread
    assert _settle(lambda: box["conn"] not in db._connections)


def test_many_transient_threads_do_not_accumulate_open_connections():
    """The leak shape: N short-lived threads (one per job) must not leave N open
    connections behind once they have finished."""
    opened: list[sqlite3.Connection] = []

    def work():
        opened.append(db.get_connection())

    threads = [threading.Thread(target=work, name=f"transient-{i}")
               for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    del thread

    assert len(opened) == 20
    assert all(conn in db._connections for conn in opened), \
        "each thread must have really opened its own connection"

    threads.clear()   # drop the finished Thread objects

    assert _settle(lambda: not any(c in db._connections for c in opened)), (
        f"{sum(c in db._connections for c in opened)} of 20 finished threads "
        "still hold an open connection")


def test_close_all_still_closes_every_live_connection():
    """The factory-reset contract this registry exists for is unchanged: a
    connection on ANOTHER live thread is closed by close_all() and that thread
    transparently reopens on its next query."""
    ready = threading.Event()
    proceed = threading.Event()
    box: dict = {}

    def work():
        box["conn"] = db.get_connection()
        box["value"] = db.query("SELECT 1 AS n")[0]["n"]
        ready.set()
        proceed.wait(timeout=10)
        box["after_reset"] = db.query("SELECT 2 AS n")[0]["n"]

    thread = threading.Thread(target=work, name="post-reset")
    thread.start()
    try:
        assert ready.wait(timeout=10)
        db.close_all()
        with pytest.raises(sqlite3.ProgrammingError):
            box["conn"].execute("SELECT 1")
    finally:
        proceed.set()
        thread.join()

    assert box["value"] == 1 and box["after_reset"] == 2
