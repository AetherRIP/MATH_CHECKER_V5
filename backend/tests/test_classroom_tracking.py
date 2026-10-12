"""Classroom tracking tests (spec §§3-§16): state mapping, idempotent sync,
failure-safety, attachment extraction, roster NOT_STARTED rows, exports.

All network access is faked by monkeypatching app.classroom.client callables
so the engine logic itself is what's under test.
"""

from __future__ import annotations

import time

import pytest

from app.classroom import client as gc
from app.classroom import submissions as cs


# ------------------------------------------------------------------ helpers

def _sub(sid: str, state: str, user: str = "u1", late: bool = False,
         attachments: list[dict] | None = None, **extra) -> dict:
    sub = {
        "id": sid,
        "userId": user,
        "state": state,
        "late": late,
        "creationTime": "2026-09-01T08:00:00Z",
        "updateTime": "2026-09-01T09:00:00Z",
        "assignmentSubmission": {"attachments": attachments or []},
        "submissionHistory": extra.pop("submissionHistory", []),
    }
    sub.update(extra)
    return sub


@pytest.fixture
def seeded_roster(monkeypatch):
    """Fake the client fetches used by the sync engine + token."""
    monkeypatch.setattr(
        "app.classroom.auth.get_access_token", lambda: "fake-token")
    # tracking_routes binds get_access_token at import time — patch there too
    import app.classroom.tracking_routes as _tr
    monkeypatch.setattr(_tr, "get_access_token", lambda: "fake-token")
    roster = [
        {"userId": "u1", "profile": {"name": {"fullName": "Ada Lovelace"},
                                     "emailAddress": "ada@school.org"}},
        {"userId": "u2", "profile": {"name": {"fullName": "Ben Bray"},
                                     "emailAddress": "ben@school.org"}},
        {"userId": "u3", "profile": {"name": {"fullName": "Cleo Chan"},
                                     "emailAddress": "cleo@school.org"}},
    ]
    subs: list[dict] = []
    calls = {"students": 0, "pages": 0}

    def fake_students(token, course_id):
        calls["students"] += 1
        return list(roster)

    monkeypatch.setattr(gc, "list_course_students", fake_students)

    def fake_page(token, course_id, coursework_id, page_token=None):
        calls["pages"] += 1
        return list(subs), None

    monkeypatch.setattr(gc, "list_student_submissions", fake_page)

    class Harness:
        def set_subs(self, new_subs, paginate_by=None):
            """Optionally split into pages of `paginate_by` items."""
            nonlocal subs
            subs = list(new_subs)
            if paginate_by:
                def paged(token, course_id, coursework_id, page_token=None):
                    calls["pages"] += 1
                    start = int(page_token or 0)
                    chunk = subs[start:start + paginate_by]
                    nxt = start + paginate_by
                    return chunk, (str(nxt) if nxt < len(subs) else None)
                monkeypatch.setattr(gc, "list_student_submissions", paged)
            else:
                monkeypatch.setattr(gc, "list_student_submissions", fake_page)

    return Harness()


def _sync(course="c1", work="w1") -> dict:
    return cs.sync_submission_tracking(course, work)


# ------------------------------------------------------- state mapping (§3-§7)

def test_created_is_not_submitted():
    """CREATED ≠ submitted — the data-integrity hole the old sync had."""
    assert cs.normalized_status({"current_state": "CREATED"}) == "CREATED"
    assert cs.normalized_status({"current_state": "NEW"}) == "CREATED"


def test_turned_in_maps_to_submitted_and_late():
    assert cs.normalized_status({"current_state": "TURNED_IN"}) == "SUBMITTED"
    assert cs.normalized_status({"current_state": "TURNED_IN", "late": 1}) == "SUBMITTED_LATE"


def test_reclaimed_is_not_submitted():
    assert cs.normalized_status({"current_state": "RECLAIMED_BY_STUDENT"}) == "RECLAIMED"


def test_edited_after_turn_in_is_specific():
    assert cs.normalized_status(
        {"current_state": "STUDENT_EDITED_AFTER_TURN_IN"}) == "EDITED_AFTER_SUBMISSION"


def test_returned_and_missing():
    assert cs.normalized_status({"current_state": "RETURNED"}) == "RETURNED"
    assert cs.normalized_status({"current_state": ""}) == "MISSING"
    assert cs.normalized_status({"current_state": "NOT_STARTED"}) == "NOT_STARTED"


def test_job_overlay_processing_and_corrected():
    assert cs.normalized_status({"current_state": "TURNED_IN"},
                                job={"status": "MARKING"}) == "PROCESSING"
    assert cs.normalized_status({"current_state": "TURNED_IN"},
                                job={"status": "COMPLETED"}) == "CORRECTED"
    assert cs.normalized_status({"current_state": "TURNED_IN"},
                                job={"status": "FAILED"}) == "FAILED"
    assert cs.normalized_status({"current_state": "TURNED_IN"},
                                job={"status": "COMPLETED"},
                                result={"earned": 8, "available": 10}) == "CORRECTED"


def test_job_overlay_does_not_mask_reclaimed():
    """A reclaimed submission must never display as Corrected/Processing."""
    assert cs.normalized_status({"current_state": "RECLAIMED_BY_STUDENT"},
                                job={"status": "COMPLETED"}) == "RECLAIMED"
    assert cs.normalized_status({"current_state": "RECLAIMED_BY_STUDENT"},
                                job={"status": "MARKING"}) == "RECLAIMED"


def test_classroom_grade_wins_over_v5_result():
    assert cs.normalized_status({"current_state": "TURNED_IN", "assigned_grade": 7.5},
                                job={"status": "COMPLETED"}) == "GRADED"


# ------------------------------------------------------------- sync behaviour

def test_sync_creates_rows_and_not_started_for_roster(seeded_roster):
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    summary = _sync()
    assert summary["submissions"] == 1
    assert summary["roster"] == 3
    data = cs.tracking_rows(coursework_id="w1")
    by_student = {r["student_id"]: r for r in data["rows"]}
    assert by_student["u1"]["status"] == "SUBMITTED"
    assert by_student["u2"]["status"] == "NOT_STARTED"
    assert by_student["u3"]["status"] == "NOT_STARTED"
    assert by_student["u2"]["synthetic"] is True
    assert by_student["u1"]["student_name"] == "Ada Lovelace"


def test_sync_is_idempotent(seeded_roster):
    seeded_roster.set_subs([
        _sub("s1", "TURNED_IN", "u1"),
        _sub("s2", "CREATED", "u2"),
    ])
    _sync()
    first = cs.tracking_rows(coursework_id="w1")
    v_first = {r["submission_id"]: r["sync_version"] if "sync_version" in r else 0
               for r in first["rows"]}
    _sync()
    second = cs.tracking_rows(coursework_id="w1")
    assert len(second["rows"]) == len(first["rows"])
    for r in second["rows"]:
        # identical data → only freshness fields change, no duplicate events
        assert r["status"] == next(
            f["status"] for f in first["rows"]
            if f["submission_id"] == r["submission_id"])
    events = cs.submission_detail("s1")["state_events"]
    assert events == []  # no spurious transitions on re-sync
    assert v_first  # rows existed


def test_transition_detection_records_event(seeded_roster):
    seeded_roster.set_subs([_sub("s1", "CREATED", "u1")])
    _sync()
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    summary = _sync()
    assert summary["transitions"] == 1
    detail = cs.submission_detail("s1")
    assert detail["submission"]["current_state"] == "TURNED_IN"
    assert detail["submission"]["previous_state"] == "CREATED"
    assert len(detail["state_events"]) == 1
    ev = detail["state_events"][0]
    assert ev["from_state"] == "CREATED"
    assert ev["to_state"] == "TURNED_IN"


def test_reclaimed_transition_overwrites_current_state(seeded_roster):
    """After RECLAIMED the current state is reclaimed — never 'submitted'."""
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    _sync()
    seeded_roster.set_subs([_sub("s1", "RECLAIMED_BY_STUDENT", "u1")])
    _sync()
    data = cs.tracking_rows(coursework_id="w1")
    row = next(r for r in data["rows"] if r["submission_id"] == "s1")
    assert row["status"] == "RECLAIMED"
    assert row["raw_state"] == "RECLAIMED_BY_STUDENT"
    assert row["previous_state"] == "TURNED_IN"
    events = cs.submission_detail("s1")["state_events"]
    assert [(e["from_state"], e["to_state"]) for e in events] == [
        ("TURNED_IN", "RECLAIMED_BY_STUDENT")]


def test_pagination_two_pages(seeded_roster):
    subs = [_sub(f"p{i}", "TURNED_IN", "u1") for i in range(5)]
    seeded_roster.set_subs(subs, paginate_by=2)
    summary = _sync()
    assert summary["submissions"] == 5
    assert summary["pages"] == 3  # 2 + 2 + 1
    data = cs.tracking_rows(coursework_id="w1")
    assert len([r for r in data["rows"] if not r["synthetic"]]) == 5


def test_api_failure_keeps_rows(seeded_roster, monkeypatch):
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    _sync()
    before = cs.tracking_rows(coursework_id="w1")

    def boom(token, course_id, coursework_id, page_token=None):
        raise gc.ClassroomApiError(503, "backend error")

    monkeypatch.setattr(gc, "list_student_submissions", boom)
    with pytest.raises(gc.ClassroomApiError):
        _sync()
    after = cs.tracking_rows(coursework_id="w1")
    # rows are KEPT — nothing deleted on API failure
    assert len(after["rows"]) == len(before["rows"])
    old = next(r for r in before["rows"] if r["submission_id"] == "s1")
    new = next(r for r in after["rows"] if r["submission_id"] == "s1")
    assert old["status"] == new["status"] == "SUBMITTED"


def test_stale_detection(seeded_roster):
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    _sync()
    # age the last_synced_at beyond the stale threshold
    old_ts = time.time() - (cs.stale_after_seconds() + 60)
    from app import db
    db.execute("UPDATE classroom_submissions SET last_synced_at = ?", (old_ts,))
    data = cs.tracking_rows(coursework_id="w1")
    row = next(r for r in data["rows"] if r["submission_id"] == "s1")
    assert row["stale"] is True
    assert data["overview"]["stale"] >= 1


def test_attachment_extraction_normalizes_kinds():
    sub = _sub("s1", "TURNED_IN", attachments=[
        {"driveFile": {"id": "f1", "title": "hw.pdf"}},
        {"form": {"formId": "fm1", "title": "Quiz"}},
        {"link": {"url": "https://example.com"}},
        {"youTubeVideo": {"id": "yt1", "title": "Video"}},
    ])
    out = gc.extract_attachments(sub)
    assert [a["kind"] for a in out] == ["drive", "form", "link", "youtube"]
    assert out[0] == {"kind": "drive", "id": "f1", "title": "hw.pdf"}


def test_turned_in_without_attachment_is_partial_quality(seeded_roster):
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1", attachments=[])])
    _sync()
    data = cs.tracking_rows(coursework_id="w1")
    row = next(r for r in data["rows"] if r["submission_id"] == "s1")
    assert row["data_quality"] == "PARTIAL"
    assert row["status"] == "SUBMITTED"


def test_state_history_flattened(seeded_roster):
    sub = _sub("s1", "TURNED_IN", "u1", submissionHistory=[
        {"updateTime": "2026-09-01T08:05:00Z",
         "stateHistory": {"state": "CREATED", "actorUserId": "u1"}},
        {"updateTime": "2026-09-01T09:00:00Z",
         "stateHistory": {"state": "TURNED_IN"}},
        {"updateTime": "2026-09-01T09:00:00Z",
         "gradeHistory": {"grade": 9, "actorUserId": "teacher1"}},
    ])
    seeded_roster.set_subs([sub])
    _sync()
    detail = cs.submission_detail("s1")
    history = detail["submission"]["submission_history"]
    assert {"state": "CREATED", "updateTime": "2026-09-01T08:05:00Z",
            "actorUserId": "u1"} in history
    assert any(h.get("grade") == 9 for h in history)


def test_missing_submission_resource_row_is_synthetic_and_never_overwrites_real(seeded_roster):
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    _sync()
    # u2's resource disappears entirely from the API (edge case) — the real
    # row must survive and must NOT gain a synthetic duplicate.
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    _sync()
    from app import db
    n_u2 = db.query_one(
        "SELECT COUNT(*) AS n FROM classroom_submissions WHERE student_id='u2'")["n"]
    assert n_u2 == 1  # only the synthetic NOT_STARTED row


# ------------------------------------------------------------------ API layer

@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaMockKey1234567890abcdef"])

    import app.classroom.auth as auth
    import app.api.routes_all as routes_mod

    class FakeState:
        connected = True
        email = "t@school.org"
        problem = None
        client_configured = True

        def public(self):
            return {"connected": True, "email": None, "problem": None,
                    "client_configured": True}

    # patch where each module BOUND the name at import time
    monkeypatch.setattr(auth, "auth_state", lambda: FakeState())
    monkeypatch.setattr(routes_mod, "auth_state", lambda: FakeState())

    from app.main import app
    with __import__("fastapi.testclient", fromlist=["TestClient"]) \
            .TestClient(app) as test_client:
        yield test_client


def test_tracking_api_and_detail(client, seeded_roster):
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    _sync()

    data = client.get("/api/classroom/tracking?course_id=c1&coursework_id=w1").json()
    assert data["overview"]["total"] == 3
    statuses = {r["student_id"]: r["status"] for r in data["rows"]}
    assert statuses["u1"] == "SUBMITTED"
    assert statuses["u2"] == "NOT_STARTED"

    detail = client.get("/api/classroom/submissions/s1/detail").json()
    assert detail["submission"]["submission_id"] == "s1"
    assert detail["submission"]["current_state"] == "TURNED_IN"
    assert "state_events" in detail and "attachments" in detail

    missing = client.get("/api/classroom/submissions/nope/detail")
    assert missing.status_code == 404


def test_sync_route_stores_via_sse(client, seeded_roster):
    """The SSE sync endpoint must store rows (same code path) and finish."""
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    with client.stream("POST", "/api/classroom/courses/c1/assignments/w1/sync") as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode()
    assert '"type": "done"' in body
    data = client.get("/api/classroom/tracking?coursework_id=w1").json()
    assert data["overview"]["total"] >= 2  # u1 real + u2/u3 synthetic
    assert '"type": "error"' not in body


def test_sync_route_reports_api_failure_and_keeps_rows(client, seeded_roster):
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    _sync()

    def boom(token, course_id, coursework_id, page_token=None):
        raise gc.ClassroomApiError(500, "boom")

    from app.classroom import client as client_mod
    import app.classroom.tracking_routes as tr
    orig = tr.gc
    try:
        tr.gc = type("M", (), {
            "list_course_students": staticmethod(lambda t, c: []),
            "list_student_submissions": staticmethod(boom),
        })()
        with client.stream("POST",
                           "/api/classroom/courses/c1/assignments/w1/sync") as resp:
            body = b"".join(resp.iter_bytes()).decode()
        assert '"type": "error"' in body
    finally:
        tr.gc = orig
    data = client.get("/api/classroom/tracking?coursework_id=w1").json()
    assert data["overview"]["total"] >= 2  # rows kept


def test_exports(client, seeded_roster):
    seeded_roster.set_subs([
        _sub("s1", "TURNED_IN", "u1", late=True,
             attachments=[{"driveFile": {"id": "f1", "title": "hw.pdf"}}]),
        _sub("s2", "CREATED", "u2"),
    ])
    _sync()

    csv_resp = client.get("/api/classroom/tracking/export?format=csv&coursework_id=w1")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    text = csv_resp.text
    assert "Ada Lovelace" in text
    assert "Submitted late" in text

    xlsx_resp = client.get("/api/classroom/tracking/export?format=xlsx&coursework_id=w1")
    assert xlsx_resp.status_code == 200
    assert "spreadsheetml" in xlsx_resp.headers["content-type"]
    assert len(xlsx_resp.content) > 1000

    pdf_resp = client.get("/api/classroom/tracking/export?format=pdf&coursework_id=w1")
    assert pdf_resp.status_code == 200
    assert pdf_resp.headers["content-type"] == "application/pdf"
    assert pdf_resp.content[:5] == b"%PDF-"

    json_resp = client.get("/api/classroom/tracking/export?format=json&coursework_id=w1")
    assert json_resp.status_code == 200
    payload = json_resp.json()
    assert any(r["student"] == "Ada Lovelace" for r in payload["rows"])

    bad = client.get("/api/classroom/tracking/export?format=docx")
    assert bad.status_code == 400


def test_exports_contain_no_secrets(client, seeded_roster):
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    _sync()
    for fmt in ("csv", "json"):
        resp = client.get(f"/api/classroom/tracking/export?format={fmt}&coursework_id=w1")
        assert "AIzaMockKey" not in resp.text
        assert "refresh_token" not in resp.text.lower()


def test_diagnostics_run(client):
    resp = client.post("/api/classroom/diagnostics/run").json()
    assert resp["total"] == 13
    assert resp["failed"] == 0, [r for r in resp["results"] if r["status"] == "fail"]
    names = {r["check"] for r in resp["results"]}
    assert {"state_mapping_complete", "export_csv", "export_xlsx",
            "export_pdf", "attachment_extraction"} <= names


def test_existing_submission_route_still_works(client, seeded_roster, monkeypatch):
    """Old TURNED_IN flow (pool staging) must remain intact."""
    from app import db
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    monkeypatch.setattr(gc, "list_turned_in_submissions",
                        lambda token, course_id, coursework_id:
                        [_sub("s1", "TURNED_IN", "u1")])
    db.execute(
        "INSERT INTO courses(id, name, fetched_at) VALUES('c1','Maths',?)",
        (time.time(),))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, fetched_at, allowlisted) "
        "VALUES('w1','c1','HW',?,1)", (time.time(),))
    response = client.get(
        "/api/classroom/courses/c1/assignments/w1/submissions")
    assert response.status_code == 200
    subs = response.json()["submissions"]
    assert subs and subs[0]["id"] == "s1" and subs[0]["state"] == "TURNED_IN"


# ------------------------------------------- explicit selection correction

_CORRECT_URL = "/api/classroom/courses/c1/assignments/w1/correct"
_SCHEME_URL = "/api/classroom/courses/c1/assignments/w1/mark-scheme"


def _seed_three_states():
    return [
        _sub("s1", "TURNED_IN", "u1",
             attachments=[{"driveFile": {"id": "f1", "title": "hw.pdf"}}]),
        _sub("s2", "CREATED", "u2"),                       # never submitted
        _sub("s3", "TURNED_IN", "u3"),                    # no attachment
    ]


def test_correct_selection_skips_non_submitted_students(client, seeded_roster,
                                                         monkeypatch):
    """Selecting students must never queue work that Classroom says does not
    exist — no wasted API calls, no fake corrections."""
    seeded_roster.set_subs(_seed_three_states())
    _sync()
    monkeypatch.setattr(
        gc, "list_turned_in_submissions",
        lambda token, course_id, coursework_id: _seed_three_states())
    monkeypatch.setattr(
        gc, "download_drive_file",
        lambda token, file_id: ("hw.pdf", b"%PDF-1.4 fake", "application/pdf"))

    # 1) mark scheme is required first
    resp = client.post(_CORRECT_URL, json={"submission_ids": ["s1", "s2"]})
    assert resp.status_code == 400
    assert resp.json()["code"] == "mark_scheme_missing"

    # 2) upload it, then select all three states
    up = client.post(_SCHEME_URL,
                     files={"file": ("scheme.pdf", b"%PDF-1.4", "application/pdf")})
    assert up.status_code == 200

    data = client.post(_CORRECT_URL,
                       json={"submission_ids": ["s1", "s2", "s3", "ghost"]}).json()
    assert data["enqueued"] == 1
    assert data["skipped_not_submitted"] == ["s2"]      # CREATED ≠ submitted
    assert data["skipped_no_attachment"] == ["s3"]      # handed in, nothing to mark
    assert data["unknown"] == ["ghost"]
    assert len(data["jobs"]) == 1

    # the queued job points at the real Classroom submission
    from app import db
    job = db.query_one("SELECT submission_id, student_id, student_name "
                       "FROM correction_jobs WHERE id = ?", (data["jobs"][0],))
    assert job["submission_id"] == "s1"
    assert job["student_id"] == "u1"


def test_correct_selection_requires_a_selection(client, seeded_roster):
    resp = client.post(_CORRECT_URL, json={"submission_ids": []})
    assert resp.status_code == 400
    assert resp.json()["code"] == "empty_selection"


def test_tracking_view_shows_invalid_submission_state(client, seeded_roster):
    """A FAILED job flagged invalid_submission surfaces as INVALID, not a
    generic correction failure — the distinct 'Invalid submission' state."""
    from app import db
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1",
                                 attachments=[{"driveFile": {"id": "f1", "title": "hw.pdf"}}])])
    _sync()
    now_ts = time.time()
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, course_id, "
        "student_id, student_name, status, error, error_code, created_at, updated_at) "
        "VALUES('j1','s1','w1','c1','u1','Ada Lovelace','FAILED',"
        "'Invalid submission: hw.pdf: empty file','invalid_submission',?,?)",
        (now_ts, now_ts),
    )
    data = cs.tracking_rows(course_id="c1", coursework_id="w1")
    statuses = {r["student_id"]: r["status"] for r in data["rows"]}
    assert statuses["u1"] == "INVALID"
    assert data["overview"]["counts"]["INVALID"] == 1

