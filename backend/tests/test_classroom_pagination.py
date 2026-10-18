"""Pagination of the Google Classroom listings the marker depends on.

Regression cover for a real defect: ``list_teacher_courses``,
``list_assignments`` and ``list_turned_in_submissions`` each asked Google for one
page (``pageSize=100``) and returned whatever came back, ignoring
``nextPageToken``. Google truncates a response at the page size and expects the
caller to ask for the rest, so past 100 courses / assignments / submissions the
app silently saw only the first page. Nothing raised, nothing was logged — the
missing assignments were simply never offered for correction and the missing
submissions were never marked.

The live read-only harness (``backend/scripts/verify_classroom_readonly.py``)
proves the fix against real data where a nextPageToken really appears; these
tests prove it against a synthetic listing large enough to truncate, which the
teacher's own classes are not.
"""

from __future__ import annotations

import pytest

from app.classroom import client as gc

def _fake_api(pages: list[dict], *, repeat_token: str | None = None):
    """A ``classroom_request`` that serves ``pages`` in order, then stops.

    Records the params of every call so a test can assert the caller actually
    sent the previous ``nextPageToken`` rather than re-reading page one.
    """
    calls: list[dict] = []

    def request(method, url, token, **kwargs):
        params = dict(kwargs.get("params") or {})
        calls.append({"method": method, "url": url, "params": params})
        index = len(calls) - 1
        if repeat_token is not None:
            # Every page hands back the SAME token: a server loop.
            return {"nextPageToken": repeat_token, **pages[0]}
        if index < len(pages):
            return pages[index]
        return {}

    request.calls = calls  # type: ignore[attr-defined]
    return request


def test_courses_follows_next_page_token(monkeypatch):
    """150 courses across 3 pages must all come back, not the first 100."""
    pages = [
        {"courses": [{"id": f"c{i}"} for i in range(100)],
         "nextPageToken": "T1"},
        {"courses": [{"id": f"c{i}"} for i in range(100, 140)],
         "nextPageToken": "T2"},
        {"courses": [{"id": f"c{i}"} for i in range(140, 150)]},
    ]
    request = _fake_api(pages)
    monkeypatch.setattr(gc, "classroom_request", request)

    courses = gc.list_teacher_courses("token")

    assert len(courses) == 150, (
        "the first page alone is 100 — anything less than 150 means the "
        "listing truncated")
    assert [c["id"] for c in courses] == [f"c{i}" for i in range(150)]
    # The second and third calls must carry the token they were handed.
    assert request.calls[0]["params"].get("pageToken") is None
    assert request.calls[1]["params"]["pageToken"] == "T1"
    assert request.calls[2]["params"]["pageToken"] == "T2"


def test_assignments_follow_pagination_and_keep_the_type_filter(monkeypatch):
    """The ASSIGNMENT filter must survive across pages, and materials dropped."""
    pages = [
        {"courseWork": [{"id": "a1", "workType": "ASSIGNMENT"},
                        {"id": "m1", "workType": "SHORT_ANSWER_QUESTION"}],
         "nextPageToken": "T1"},
        {"courseWork": [{"id": "a2", "workType": "ASSIGNMENT"}]},
    ]
    request = _fake_api(pages)
    monkeypatch.setattr(gc, "classroom_request", request)

    assignments = gc.list_assignments("token", "course-1")

    assert [a["id"] for a in assignments] == ["a1", "a2"], (
        "the assignment on page 2 was lost")
    assert request.calls[0]["url"].endswith("/courses/course-1/courseWork")
    assert request.calls[0]["params"]["courseWorkStates"] == "PUBLISHED"


def test_turned_in_submissions_follow_pagination(monkeypatch):
    pages = [
        {"studentSubmissions": [{"id": f"s{i}"} for i in range(100)],
         "nextPageToken": "P2"},
        {"studentSubmissions": [{"id": f"s{i}"} for i in range(100, 130)]},
    ]
    request = _fake_api(pages)
    monkeypatch.setattr(gc, "classroom_request", request)

    submissions = gc.list_turned_in_submissions("token", "c1", "w1")

    assert len(submissions) == 130, (
        "30 students' work sat on page two and was never marked")
    assert request.calls[0]["params"]["states"] == "TURNED_IN"
    assert request.calls[1]["params"]["pageToken"] == "P2"


def test_roster_still_follows_pagination(monkeypatch):
    """The roster already paginated; hold that behaviour still."""
    pages = [
        {"students": [{"userId": f"u{i}"} for i in range(30)],
         "nextPageToken": "R2"},
        {"students": [{"userId": f"u{i}"} for i in range(30, 78)]},
    ]
    monkeypatch.setattr(gc, "classroom_request", _fake_api(pages))

    assert len(gc.list_course_students("token", "c1")) == 78


def test_a_repeated_page_token_cannot_hang_the_listing(monkeypatch):
    """A server that echoes the same token forever must terminate, not spin.

    Two calls, not one: the first page is read, its (repeated) token is used
    once, and the walk stops as soon as the same token comes back — which is
    what keeps a broken server from becoming an infinite loop.
    """
    request = _fake_api([{"courses": [{"id": "c0"}]}], repeat_token="SAME")
    monkeypatch.setattr(gc, "classroom_request", request)

    courses = gc.list_teacher_courses("token")

    assert len(request.calls) == 2, (
        f"the repeated token must be visited once and then abandoned; "
        f"made {len(request.calls)} calls")
    assert len(courses) == 2, "(one page per call, and nothing more)"


def test_page_walk_is_bounded(monkeypatch):
    """Even distinct tokens are capped, so a long listing cannot spin forever."""
    def endless(method, url, token, **kwargs):
        params = kwargs.get("params") or {}
        index = int(params.get("pageToken") or 0)
        return {"courses": [{"id": f"c{index}"}],
                "nextPageToken": str(index + 1)}

    monkeypatch.setattr(gc, "classroom_request", endless)
    monkeypatch.setattr(gc, "MAX_PAGES", 5)

    courses = gc.list_teacher_courses("token")

    assert len(courses) == 5, f"expected the cap to stop at 5, got {len(courses)}"


def test_a_page_size_is_always_requested(monkeypatch):
    """Without pageSize Google picks its own small default; never omit it."""
    request = _fake_api([{"courses": []}])
    monkeypatch.setattr(gc, "classroom_request", request)

    gc._paginate("token", "https://example.invalid/courses", {}, "courses")

    assert request.calls[0]["params"]["pageSize"] == 100


def test_an_empty_listing_is_not_an_error(monkeypatch):
    monkeypatch.setattr(gc, "classroom_request", _fake_api([{}]))

    assert gc.list_teacher_courses("token") == []
    assert gc.list_assignments("token", "c1") == []
    assert gc.list_turned_in_submissions("token", "c1", "w1") == []


def test_only_get_requests_are_issued(monkeypatch):
    """The listings are read-only by construction."""
    request = _fake_api([{"courses": [{"id": "c"}], "nextPageToken": None}])
    monkeypatch.setattr(gc, "classroom_request", request)

    gc.list_teacher_courses("token")

    assert {call["method"] for call in request.calls} == {"GET"}
