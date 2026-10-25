"""Completeness of the FULL Classroom sync (spec §2, §5, §6, §7).

These tests exist because "the sync stops halfway" has exactly three possible
homes, and two of them are asserted here:

  * pagination — a listing that stops at the first page is a SILENTLY
    incomplete sync (assignments past the cap never appear, students past the
    cap are never marked);
  * completion checks — a partial run must be reported as partial, and the
    failing part must name the assignment so it can be retried alone instead of
    re-running the whole class sync;
  * duplicate jobs — two clicks must join ONE run, never start a second.

The third home (the desktop client abandoning the run after 1.5 s) is covered
by desktop/tests/MathChecker.Desktop.Tests/ClassroomSyncTests.cs.
"""

from __future__ import annotations

import json
import time

import pytest

from test_classroom_tracking import _sub, client, seeded_roster  # noqa: F401


def _events(body: str) -> list[dict]:
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return out


def _stream(client, url: str) -> tuple[int, str]:
    with client.stream("GET", url) as resp:
        body = b"".join(resp.iter_bytes()).decode()
    return resp.status_code, body


@pytest.fixture
def two_courses(monkeypatch):
    """Courses + coursework persisted exactly like the real sync does."""
    from app import db
    from app.classroom import sync as csync

    courses = [{"id": "c1", "name": "Algebra A"}, {"id": "c2", "name": "Physics B"}]

    def fake_courses(force=False):
        for c in courses:
            db.execute(
                "INSERT INTO courses(id, name, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (c["id"], c["name"], 1_700_000_000))
        return [dict(r) for r in db.query("SELECT * FROM courses ORDER BY name")]

    def fake_coursework(course_id, force=False):
        items = ([{"id": "w1", "title": "Algebra Homework 5"}]
                 if course_id == "c1" else
                 [{"id": "w9", "title": "Forces Homework"}])
        for it in items:
            db.execute(
                "INSERT INTO coursework(id, course_id, title, fetched_at) "
                "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title",
                (it["id"], course_id, it["title"], 1_700_000_000))
        return items

    monkeypatch.setattr(csync, "sync_courses", fake_courses)
    monkeypatch.setattr(csync, "sync_coursework", fake_coursework)
    return csync


def test_pagination_reads_every_page_and_stops_on_a_repeated_token(monkeypatch):
    """``_paginate`` must read to the last page (Google caps a page at 100),
    and a server echoing the same nextPageToken forever must not spin."""
    from app.classroom import client as gc

    requested: list[str | None] = []

    def fake_request(method, url, token, params=None, **kwargs):
        cursor = (params or {}).get("pageToken")
        requested.append(cursor)
        if cursor is None:
            return {"courses": [{"id": "c1"}], "nextPageToken": "t1"}
        if cursor == "t1":
            return {"courses": [{"id": "c2"}], "nextPageToken": "t2"}
        if cursor == "t2":
            # A misbehaving server: same token back again.
            return {"courses": [{"id": "c3"}], "nextPageToken": "t2"}
        raise AssertionError("unexpected cursor " + str(cursor))

    monkeypatch.setattr(gc, "classroom_request", fake_request)

    items = gc._paginate("tok", "https://example/courses", {}, "courses")

    assert [i["id"] for i in items] == ["c1", "c2", "c3"]
    assert requested == [None, "t1", "t2"]   # stopped instead of looping


def test_full_sync_stores_every_submission_page(client, seeded_roster, monkeypatch,
                                                two_courses):
    """A class bigger than one API page must land completely: the summary and
    the database both count every page, not just the first."""
    from app import db
    from app.classroom import client as gc

    chunks = [
        [_sub(f"p{i}", "TURNED_IN", f"u{i}") for i in range(1, 5)],
        [_sub(f"p{i}", "TURNED_IN", f"u{i}") for i in range(5, 9)],
        [_sub(f"p{i}", "RETURNED", f"u{i}") for i in range(9, 13)],
    ]

    def paged(token, course_id, coursework_id, page_token=None):
        index = int(page_token or 0)
        if index >= len(chunks):
            return [], None
        next_token = str(index + 1) if index + 1 < len(chunks) else None
        return list(chunks[index]), next_token

    monkeypatch.setattr(gc, "list_student_submissions", paged)

    status, body = _stream(client, "/api/classroom/sync")
    assert status == 200
    events = _events(body)
    done = events[-1]
    assert done["type"] == "done", done
    assert done["summary"]["partial"] is False

    stored = db.query_one(
        "SELECT COUNT(*) AS n FROM classroom_submissions WHERE coursework_id='w1' "
        "AND submission_id LIKE 'p%'")["n"]
    assert stored == 12
    assert done["summary"]["submissions"] == 24   # 12 per coursework


def test_second_sync_click_joins_the_running_job(client, seeded_roster, monkeypatch,
                                                 two_courses):
    """Two starts while one is running must NOT queue a second full sync: the
    backend answers `started: false` and names the run already in flight."""
    import threading

    from app.classroom import client as gc
    from app.classroom import sync_runs

    sync_runs.reset_for_tests()
    release = threading.Event()

    def slow_page(token, course_id, coursework_id, page_token=None):
        release.wait(5)
        return [_sub("s1", "TURNED_IN", "u1")], None

    monkeypatch.setattr(gc, "list_student_submissions", slow_page)

    first = client.post("/api/classroom/sync/start").json()
    second = client.post("/api/classroom/sync/start").json()

    assert first["started"] is True
    assert second["started"] is False           # joined, not duplicated
    assert second["snapshot"]["id"] == first["snapshot"]["id"]

    release.set()
    deadline = time.time() + 20
    while time.time() < deadline:
        snapshot = client.get("/api/classroom/sync/progress").json()
        if not snapshot["running"]:
            break
        time.sleep(0.05)
    assert snapshot["running"] is False
    assert snapshot["summary"] is not None
    sync_runs.reset_for_tests()


def test_partial_sync_names_the_assignment_so_it_can_be_retried(client, seeded_roster,
                                                               monkeypatch, two_courses):
    """An unrecoverable assignment failure must (a) not abort the run, (b) mark
    it partial, and (c) carry the ids needed to re-sync just that assignment."""
    from app import db
    from app.classroom import client as gc

    state = {"w9_fails": True}

    def flaky(token, course_id, coursework_id, page_token=None):
        if coursework_id == "w9" and state["w9_fails"]:
            raise gc.ClassroomApiError(503, "backend quota exceeded")
        return [_sub(f"s-{coursework_id}", "TURNED_IN", "u1")], None

    monkeypatch.setattr(gc, "list_student_submissions", flaky)

    status, body = _stream(client, "/api/classroom/sync")
    assert status == 200
    done = _events(body)[-1]
    assert done["type"] == "done"
    assert done["summary"]["partial"] is True

    problem = done["summary"]["errors"][0]
    assert problem["part"].startswith("submissions")
    assert problem["course_id"] == "c2"
    assert problem["coursework_id"] == "w9"

    # The healthy course was still stored — a partial run keeps its good work.
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM classroom_submissions WHERE coursework_id='w1'"
    )["n"] >= 1

    # ---- retry ONLY the failed assignment (no second full sync) -------------
    state["w9_fails"] = False
    started = client.post(
        "/api/classroom/sync/start?course_id=c2&coursework_id=w9").json()
    assert started["started"] is True
    deadline = time.time() + 20
    while time.time() < deadline:
        snapshot = client.get("/api/classroom/sync/progress").json()
        if not snapshot["running"]:
            break
        time.sleep(0.05)
    assert snapshot["error"] is None
    assert snapshot["summary"]["coursework_id"] == "w9"
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM classroom_submissions WHERE submission_id='s-w9'"
    )["n"] == 1
