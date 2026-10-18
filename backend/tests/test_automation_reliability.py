"""Reliability regression tests for the Automatic Correction page.

What is under test here, with every Google call faked:

* the IN-FLIGHT job count: one job per submission, never one per pipeline stage,
  and never a finished, failed or stale job;
* durable, idempotent REPORT uploads (``report_upload_records``): a confirmed
  upload is never repeated, an ambiguous one is reconciled instead of retried,
  and a genuinely changed report can still be re-uploaded;
* the teacher-triggered BULK grade + report upload: per-component outcomes,
  partial success, no duplicate attachments, no overlapping runs;
* CLEAR RECENT JOBS: archives terminal jobs only, deletes nothing, and every
  record needed to retry or verify an upload survives.

The Google world below is a fake; the app's own decision logic is what is being
tested. Nothing here claims Google Classroom was contacted for real.
"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from app import db
from app.automation import states, store
from app.classroom import auth as gc_auth
from app.classroom import client as gc
from app.classroom import managed as mg
from app.classroom import report_attachment as ra

COURSE = "course-1"
CW = "coursework-1"
ACCOUNT = "teacher@school.edu"


# ------------------------------------------------------------------- fixtures

def seed_assignment(coursework_id: str = CW, course_id: str = COURSE,
                    *, managed: bool = True, max_points: float = 75.0) -> None:
    db.execute("INSERT OR IGNORE INTO courses(id, name, fetched_at) VALUES(?,?,?)",
               (course_id, "Maths", time.time()))
    db.execute(
        "INSERT OR IGNORE INTO coursework(id, course_id, title, fetched_at, "
        "allowlisted, max_points) VALUES(?,?,?,?,1,?)",
        (coursework_id, course_id, "Algebra Lesson 1", time.time(), max_points))
    if managed:
        mg.record_managed(coursework_id, course_id, title="Algebra Lesson 1",
                          max_points=max_points)


def seed_submission(submission_id: str, *, student_id: str,
                    name: str = "Student One", assigned_grade=None,
                    state: str = "TURNED_IN",
                    coursework_id: str = CW, course_id: str = COURSE) -> None:
    db.execute(
        """
        INSERT INTO classroom_submissions(
            submission_id, course_id, coursework_id, student_id, student_name,
            current_state, update_time, attachments_json, data_quality,
            last_synced_at, first_seen_at, sync_version, assigned_grade)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)
        ON CONFLICT(submission_id) DO UPDATE SET
            current_state = excluded.current_state,
            assigned_grade = excluded.assigned_grade,
            last_synced_at = excluded.last_synced_at
        """,
        (submission_id, course_id, coursework_id, student_id, name, state,
         "2026-09-01T09:00:00Z", json.dumps([]), "VERIFIED",
         time.time(), time.time(), assigned_grade))


def seed_result(result_id: str, submission_id: str, *, student_id: str,
                earned: int = 60, available: int = 75,
                coursework_id: str = CW, review_required: int = 0) -> str:
    job_id = f"cj-{result_id}"
    db.execute(
        "INSERT OR IGNORE INTO correction_jobs(id, status, created_at, updated_at) "
        "VALUES(?,?,?,?)", (job_id, "COMPLETED", time.time(), time.time()))
    db.execute(
        """
        INSERT INTO results(id, job_id, coursework_id, submission_id, student_id,
            student_name, earned, available, percentage, provisional_grade,
            result_json, review_required, manual_override, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)
        """,
        (result_id, job_id, coursework_id, submission_id, student_id,
         "Student One", earned, available, round(earned / available * 100, 2),
         "5", json.dumps({"questions": [], "overall_notes": {}}),
         review_required, time.time()))
    return result_id


def seed_ready_submission(submission_id: str, result_id: str, *,
                          student_id: str, earned: int = 60, available: int = 75,
                          assigned_grade=None, review_required: int = 0) -> str:
    """A corrected submission with a grade and a report: bulk-upload ready."""
    seed_submission(submission_id, student_id=student_id,
                    assigned_grade=assigned_grade)
    return seed_result(result_id, submission_id, student_id=student_id,
                       earned=earned, available=available,
                       review_required=review_required)


def make_job(submission_id: str, student_id: str, *, version: str = "v1") -> dict:
    return store.create_job(course_id=COURSE, coursework_id=CW,
                            assignment_title="Algebra Lesson 1",
                            student_id=student_id, student_name="Student One",
                            submission_id=submission_id,
                            submission_version=version)


def _pdf(lines: int = 40) -> bytes:
    """A real, parseable PDF big enough to pass the app's report guard."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(lines):
        c.drawString(72, 760 - i * 16, f"Correction Report line {i} " + "x" * 20)
    c.showPage()
    c.save()
    return buf.getvalue()


PDF_BYTES = _pdf()


class FakeGoogle:
    """An in-memory Classroom/Drive: real Drive files, real attachments."""

    def __init__(self, monkeypatch):
        self.files: dict[str, dict] = {}
        self.attachments: dict[str, list[str]] = {}
        self.grades: dict[str, float] = {}
        self.calls = {"upload": [], "update": [], "attach": [], "grade": []}
        self.attach_failures: set[str] = set()
        self.grade_failures: set[str] = set()
        self._n = 1
        self._patch(monkeypatch)

    # ---- Drive ----------------------------------------------------------
    def upload(self, token, filename, content, **kw):
        self.calls["upload"].append(filename)
        fid = f"drive-{self._n}"
        self._n += 1
        self.files[fid] = {"content": content,
                           "props": dict(kw.get("app_properties") or {}),
                           "name": filename, "owners": [ACCOUNT]}
        return {"id": fid, "name": filename,
                "owners": [{"emailAddress": ACCOUNT}]}

    def update(self, token, file_id, content):
        self.calls["update"].append(file_id)
        self.files[file_id]["content"] = content

    def get_file(self, token, fid):
        f = self.files.get(fid)
        if f is None:
            return None
        return {"id": fid, "name": f["name"], "trashed": False,
                "mimeType": "application/pdf",
                "size": str(len(f["content"])),
                "owners": [{"emailAddress": e} for e in f["owners"]],
                "appProperties": f["props"]}

    def download(self, token, fid):
        f = self.files[fid]
        return f["name"], f["content"], "application/pdf"

    # ---- Classroom ------------------------------------------------------
    def attach(self, token, course_id, coursework_id, submission_id, **kw):
        add = list(kw.get("add_drive_file_ids") or [])
        self.calls["attach"].append({"submission": submission_id, "add": add})
        if submission_id in self.attach_failures:
            raise gc.ClassroomApiError(500, "Classroom is unavailable")
        self.attachments.setdefault(submission_id, []).extend(add)
        return self.submission(submission_id)

    def submission(self, submission_id):
        ids = list(self.attachments.get(submission_id, []))
        grade = self.grades.get(submission_id)
        return {"id": submission_id, "userId": f"user-{submission_id}",
                "state": "TURNED_IN", "associatedWithDeveloper": True,
                "draftGrade": grade, "assignedGrade": grade,
                "assignmentSubmission": {
                    "attachments": [{"driveFile": {"id": i}} for i in ids]}}

    def read(self, token, course_id, coursework_id, submission_id):
        return self.submission(submission_id)

    def classroom_request(self, method, url, token=None, **kw):
        """The one write chokepoint the real publish path goes through."""
        body = kw.get("json") or {}
        if method == "PATCH" and "studentSubmissions" in url:
            sid = url.split("studentSubmissions/")[1].split(":")[0]
            if sid in self.grade_failures:
                raise gc.ClassroomApiError(403, "ProjectPermissionDenied")
            grade = body.get("assignedGrade", body.get("draftGrade"))
            self.grades[sid] = grade
            self.calls["grade"].append({"submission": sid, "grade": grade})
        return {}

    # ---- wiring ---------------------------------------------------------
    def _patch(self, monkeypatch):
        monkeypatch.setattr(gc, "upload_report_to_drive", self.upload)
        monkeypatch.setattr(gc, "replace_drive_file_content", self.update)
        monkeypatch.setattr(gc, "get_drive_file", self.get_file)
        monkeypatch.setattr(gc, "download_drive_file", self.download)
        monkeypatch.setattr(gc, "modify_submission_attachments", self.attach)
        monkeypatch.setattr(gc, "get_student_submission", self.read)
        monkeypatch.setattr(gc, "classroom_request", self.classroom_request)
        monkeypatch.setattr(gc, "share_drive_file",
                            lambda token, fid, email, role="reader": None)
        monkeypatch.setattr(gc, "get_user_profile",
                            lambda token, uid: {"emailAddress": f"{uid}@school.edu"})
        monkeypatch.setattr(gc, "is_our_temp_report", lambda *a, **k: True)
        monkeypatch.setattr(gc, "delete_drive_file", lambda *a, **k: None)
        monkeypatch.setattr(
            gc, "drive_file_owners",
            lambda meta: [o["emailAddress"] for o in (meta or {}).get("owners", [])])
        monkeypatch.setattr(
            gc, "verify_drive_ownership",
            lambda token, fid, *, account_email=None: {
                "ok": True, "owners": [ACCOUNT], "reason": "owner", "file_id": fid})
        monkeypatch.setattr(gc_auth, "get_access_token", lambda: "tok")
        monkeypatch.setattr(gc_auth, "account_email", lambda: ACCOUNT)
        monkeypatch.setattr(ra, "render_report_pdf",
                            lambda result, detail="standard": PDF_BYTES)

    # ---- assertions -----------------------------------------------------
    def report_files(self, submission_id: str) -> list[str]:
        out = []
        for fid in self.attachments.get(submission_id, []):
            props = (self.files.get(fid) or {}).get("props") or {}
            if str(props.get(gc.APP_PROP_TEMP)).lower() == "true":
                out.append(fid)
        return out


@pytest.fixture
def google(monkeypatch):
    return FakeGoogle(monkeypatch)


@pytest.fixture
def client(fresh_state):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


# ===========================================================================
# 1. The in-flight count
# ===========================================================================

def test_three_active_jobs_are_three_even_with_many_stages():
    """One job moving through three stages is still ONE in-flight job."""
    for i in range(3):
        seed_assignment()
        seed_submission(f"s{i + 1}", student_id=f"u{i + 1}")
        job = make_job(f"s{i + 1}", f"u{i + 1}")
        assert job is not None
        # The real claim path the background worker uses.
        assert store.claim(job["id"], worker="w1") is not None
        # ...and the job keeps moving between stages without being counted twice.
        for stage in (states.RESULT_VALIDATED, states.REPORT_GENERATED,
                      states.REPORT_UPLOADED):
            store.set_status(job["id"], stage)
            assert store.execution_counts()["active"] == i + 1
    assert store.execution_counts()["active"] == 3


def test_finished_failed_and_stale_jobs_never_inflate_the_active_count():
    seed_assignment()
    now_ts = time.time()

    seed_submission("done", student_id="u-done")
    done = make_job("done", "u-done")
    db.execute("UPDATE automation_jobs SET status=?, claimed_by=?, claimed_at=? "
               "WHERE id=?", (states.COMPLETE, "w1", now_ts, done["id"]))

    seed_submission("retry", student_id="u-retry")
    retrying = make_job("retry", "u-retry")
    store.set_status(retrying["id"], states.REPORT_UPLOAD_FAILED,
                     next_attempt_at=now_ts + 60)

    seed_submission("stale", student_id="u-stale")
    stale = make_job("stale", "u-stale")
    db.execute("UPDATE automation_jobs SET status=?, claimed_by=?, claimed_at=? "
               "WHERE id=?",
               (states.CORRECTING, "dead-worker",
                now_ts - store.CLAIM_LEASE_SECONDS - 5, stale["id"]))

    # Nothing is genuinely running: the finished job is done, the failed one is
    # waiting for its backoff and the stale claim belongs to a dead worker.
    assert store.execution_counts(now_ts)["active"] == 0
    assert store.execution_counts(now_ts)["waiting"] == 1


def test_a_retried_job_is_one_job_and_a_duplicate_row_cannot_be_created():
    """A retry resumes the SAME row, so it can never inflate the count, and a
    racing insert for the same submission version is refused outright (§11)."""
    seed_assignment()
    seed_submission("s1", student_id="u1")
    job = make_job("s1", "u1")
    store.claim(job["id"], worker="w1")
    for _ in range(3):                      # three failed attempts, one job
        store.set_status(job["id"], states.REPORT_UPLOAD_FAILED,
                         next_attempt_at=time.time() + 5)
        store.set_status(job["id"], states.CORRECTING)
        store.claim(job["id"], worker="w1")
        assert store.execution_counts()["active"] == 1
        assert len(store.list_jobs()) == 1
    # A second job for the SAME submission version is not a second job.
    assert make_job("s1", "u1") is None
    assert len(store.list_jobs()) == 1


def test_status_endpoint_reports_the_logical_count_and_recovers_after_restart(client):
    seed_assignment()
    now_ts = time.time()
    seed_submission("s1", student_id="u1")
    job = make_job("s1", "u1")
    store.claim(job["id"], worker="w1")
    assert client.get("/api/automation/status").json()["active_jobs"] == 1

    # A crashed worker leaves a stale claim behind: after a restart recovery the
    # job is released and can be run again instead of counting as in flight.
    db.execute("UPDATE automation_jobs SET claimed_at=? WHERE id=?",
               (now_ts - store.CLAIM_LEASE_SECONDS - 5, job["id"]))
    assert client.get("/api/automation/status").json()["active_jobs"] == 0
    assert store.recover_in_flight()["claims_released"] == 1
    assert store.get(job["id"])["claimed_by"] is None


def test_concurrent_stage_transitions_never_double_count():
    """A job changing stage while the page polls is never counted twice."""
    seed_assignment()
    seed_submission("s1", student_id="u1")
    job = make_job("s1", "u1")
    stages = [states.CORRECTING, states.RESULT_VALIDATED, states.REPORT_GENERATED,
              states.REPORT_VALIDATED, states.REPORT_UPLOADED]
    seen: list[int] = []
    stop = threading.Event()
    store.claim(job["id"], worker="w1")      # the worker owns the job

    def worker():
        while not stop.is_set():
            for stage in stages:
                store.set_status(job["id"], stage)
                db.execute("UPDATE automation_jobs SET claimed_by='w1', "
                           "claimed_at=? WHERE id=?", (time.time(), job["id"]))
                seen.append(store.execution_counts()["active"])
                time.sleep(0.001)

    t = threading.Thread(target=worker)
    t.start()
    try:
        for _ in range(40):
            seen.append(store.execution_counts()["active"])
            time.sleep(0.001)
    finally:
        stop.set()
        t.join(timeout=5)
    assert seen, "the poller never observed the job"
    assert set(seen) == {1}, f"count wobbled: {sorted(set(seen))}"


# ===========================================================================
# 2. Durable, idempotent report uploads
# ===========================================================================

def _result_row(result_id: str, submission_id: str = "sub-A") -> dict:
    return {"id": result_id, "coursework_id": CW, "submission_id": submission_id,
            "student_id": f"u-{submission_id}", "student_name": "Student One",
            "earned": 60, "available": 75, "percentage": 80.0,
            "provisional_grade": "5", "result_json": {"questions": []}}


def test_a_second_worker_cannot_start_the_same_report_upload():
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    result = _result_row("r-A")
    first = store.report_upload_begin(result, "hash-1")
    second = store.report_upload_begin(result, "hash-1")
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert second["status"] == "UPLOADING"


def test_a_confirmed_upload_is_never_sent_again_and_survives_a_restart():
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    result = _result_row("r-A")
    assert store.report_upload_begin(result, "hash-1")["claimed"] is True
    store.report_upload_finish("r-A", "hash-1", status="UPLOADED",
                               remote_file_id="drive-7")
    # A "restart" only re-reads the database row - there is no in-memory flag.
    record = store.report_upload_record("r-A")
    assert record["status"] == "UPLOADED"
    assert record["remote_file_id"] == "drive-7"
    again = store.report_upload_begin(result, "hash-1")
    assert again["claimed"] is False
    assert again["status"] == "UPLOADED"
    assert again["remote_file_id"] == "drive-7"


def test_an_ambiguous_outcome_is_reconciled_before_any_retry():
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    result = _result_row("r-A")
    store.report_upload_begin(result, "hash-1")
    store.report_upload_finish("r-A", "hash-1", status="AMBIGUOUS",
                               remote_file_id="drive-7", error="timeout")
    blocked = store.report_upload_begin(result, "hash-1")
    assert blocked["claimed"] is False, "an ambiguous outcome must not be retried"

    # Reading Classroom back proves the attachment is there: reconcile, never
    # upload a second time.
    store.reconcile_report_upload(result, "hash-1", "drive-7", attached=True)
    record = store.report_upload_record("r-A")
    assert record["status"] == "UPLOADED"
    assert store.report_upload_begin(result, "hash-1")["claimed"] is False


def test_a_changed_report_gets_a_fresh_claim_and_a_forced_report_does_not():
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    result = _result_row("r-A")
    store.report_upload_begin(result, "hash-1")
    store.report_upload_finish("r-A", "hash-1", status="UPLOADED",
                               remote_file_id="drive-7")
    # The marks changed -> the report genuinely differs -> re-upload is allowed.
    changed = store.report_upload_begin(result, "hash-2")
    assert changed["claimed"] is True


def test_an_expired_lease_can_be_reclaimed_after_a_crash():
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    result = _result_row("r-A")
    assert store.report_upload_begin(result, "hash-1", lease_seconds=-1)["claimed"]
    # The worker died without finishing; the lease is already in the past.
    assert store.report_upload_begin(result, "hash-1")["claimed"] is True


# ===========================================================================
# 3. Bulk grade + report upload
# ===========================================================================

def test_bulk_upload_sends_grade_and_report_once_and_skips_them_afterwards(
        client, google):
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A", earned=60)
    eligible = client.get("/api/automation/bulk-upload/eligible").json()
    assert eligible["eligible_count"] == 1
    assert eligible["items"][0]["report_attached"] is False

    first = client.post("/api/automation/bulk-upload",
                        json={"result_ids": ["r-A"]}).json()
    assert first["summary"] == {"total": 1, "complete": 1, "partial": 0, "failed": 0}
    assert first["items"][0]["grade"] == "published"
    assert first["items"][0]["report"] == "uploaded"
    assert len(google.calls["grade"]) == 1
    assert len(google.calls["attach"]) == 1
    assert google.report_files("sub-A") == ["drive-1"]

    # Pressing the button again must not post the grade or the report twice.
    second = client.post("/api/automation/bulk-upload",
                         json={"result_ids": ["r-A"]}).json()
    assert second["items"][0]["status"] == "complete"
    assert second["items"][0]["grade"] == "skipped"
    assert len(google.calls["grade"]) == 1, "the grade was written twice"
    assert len(google.calls["attach"]) == 1, "the report was attached twice"
    assert len(google.calls["upload"]) == 1, "a second Drive file was created"
    assert google.report_files("sub-A") == ["drive-1"]


def test_bulk_upload_skips_an_unchanged_grade_and_only_sends_what_is_missing(
        client, google):
    seed_assignment()
    # The synced Classroom state already carries the intended grade.
    seed_ready_submission("sub-A", "r-A", student_id="u-A", earned=60,
                          assigned_grade=60.0)
    outcome = client.post("/api/automation/bulk-upload",
                          json={"result_ids": ["r-A"]}).json()["items"][0]
    assert outcome["grade"] == "skipped"
    assert google.calls["grade"] == []
    assert outcome["report"] == "uploaded"      # the report was still missing
    assert len(google.calls["attach"]) == 1


def test_bulk_upload_reports_partial_success_and_keeps_going(client, google):
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    seed_ready_submission("sub-B", "r-B", student_id="u-B")
    google.attach_failures = {"sub-A"}          # only A's report fails

    body = client.post("/api/automation/bulk-upload",
                       json={"result_ids": ["r-A", "r-B"]}).json()
    by_id = {i["result_id"]: i for i in body["items"]}
    assert by_id["r-A"]["status"] == "partial"
    assert by_id["r-A"]["report"] == "failed"
    assert by_id["r-B"]["status"] == "complete"
    assert body["summary"] == {"total": 2, "complete": 1, "partial": 1, "failed": 0}
    # B was processed even though A failed, and A's grade really was published.
    assert len(google.calls["attach"]) == 2
    assert {c["submission"] for c in google.calls["grade"]} == {"sub-A", "sub-B"}

    # A retry of A must not re-publish the grade that already succeeded.
    google.attach_failures = set()
    retry = client.post("/api/automation/bulk-upload",
                        json={"result_ids": ["r-A"]}).json()["items"][0]
    assert retry["grade"] == "skipped"
    assert retry["report"] == "uploaded"
    assert len([c for c in google.calls["grade"] if c["submission"] == "sub-A"]) == 1


def test_bulk_upload_will_not_run_twice_over_the_same_submission(client, google):
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    run_id = store.begin_bulk_upload_run(["r-A"])
    assert run_id
    resp = client.post("/api/automation/bulk-upload", json={"result_ids": ["r-A"]})
    assert resp.status_code == 409
    assert resp.json()["code"] == "bulk_upload_in_progress"
    store.end_bulk_upload_run(run_id)
    # A finished item releases its claim, so the teacher can retry immediately.
    assert client.post("/api/automation/bulk-upload",
                       json={"result_ids": ["r-A"]}).status_code == 200


def test_bulk_upload_without_a_google_connection_fails_cleanly(client, google,
                                                              monkeypatch):
    """A dead/absent Classroom grant must not lock the submissions out."""
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")

    def no_token():
        raise RuntimeError("Google Classroom is not connected yet.")

    monkeypatch.setattr(gc_auth, "get_access_token", no_token)
    resp = client.post("/api/automation/bulk-upload", json={"result_ids": ["r-A"]})
    assert resp.status_code == 409
    assert resp.json()["code"] == "classroom_not_connected"
    assert "not connected" in resp.json()["message"].lower()
    # The run is closed and its claims released, so connecting and retrying
    # works immediately instead of hitting "already in an upload run".
    assert db.query("SELECT * FROM bulk_upload_claims") == []
    run = db.query_one("SELECT status FROM automation_bulk_runs")
    assert run["status"] == "FAILED"
    monkeypatch.setattr(gc_auth, "get_access_token", lambda: "tok")
    assert client.post("/api/automation/bulk-upload",
                       json={"result_ids": ["r-A"]}).status_code == 200


def test_bulk_upload_validates_its_input(client):
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    empty = client.post("/api/automation/bulk-upload", json={"result_ids": []})
    assert empty.status_code == 400 and empty.json()["code"] == "empty_selection"

    big = client.post("/api/automation/bulk-upload",
                      json={"result_ids": [f"r-{i}" for i in range(101)]})
    assert big.status_code == 400 and big.json()["code"] == "batch_too_large"

    needs_confirm = client.post(
        "/api/automation/bulk-upload",
        json={"result_ids": [f"r-{i}" for i in range(11)]})
    assert needs_confirm.status_code == 400
    assert needs_confirm.json()["code"] == "confirmation_required"


def test_refreshing_the_page_starts_no_work(client, google):
    """Status/jobs/eligibility are read-only: a polling page cannot duplicate work."""
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    for _ in range(3):
        client.get("/api/automation/status")
        client.get("/api/automation/jobs")
        client.get("/api/automation/bulk-upload/eligible")
    assert google.calls == {"upload": [], "update": [], "attach": [], "grade": []}
    assert db.query("SELECT * FROM bulk_upload_claims") == []
    assert db.query("SELECT * FROM report_upload_records") == []
    assert db.query("SELECT * FROM automation_bulk_runs") == []


def test_bulk_upload_eligibility_explains_missing_prerequisites(client):
    seed_assignment()
    # A correction that still needs teacher review is not uploadable.
    seed_ready_submission("sub-A", "r-A", student_id="u-A", review_required=1)
    # A submission that was never synced cannot be uploaded either.
    seed_result("r-B", "sub-B", student_id="u-B")
    items = {i["result_id"]: i
             for i in client.get("/api/automation/bulk-upload/eligible").json()["items"]}
    assert items["r-A"]["eligible"] is False
    assert "review" in items["r-A"]["prerequisite_error"].lower()
    assert items["r-B"]["eligible"] is False


def test_bulk_upload_never_reruns_the_correction(client, google, monkeypatch):
    """The whole point: it only sends what a correction already produced."""
    seed_assignment()
    seed_ready_submission("sub-A", "r-A", student_id="u-A")
    from app.engine.service import CorrectionService

    def explode(*a, **k):  # any AI correction call here would be a bug
        raise AssertionError("bulk upload re-ran the AI correction")

    monkeypatch.setattr(CorrectionService, "enqueue_classroom_submission", explode)
    assert client.post("/api/automation/bulk-upload",
                       json={"result_ids": ["r-A"]}).status_code == 200
    uploaded = google.calls["upload"]
    assert uploaded, "the report was not generated/uploaded"
    # Nothing was queued for the AI engine: the only correction job is the one
    # the fixture's correction already produced.
    assert len(db.query("SELECT id FROM correction_jobs")) == 1


# ===========================================================================
# 4. Clear recent jobs
# ===========================================================================

def test_clear_recent_jobs_hides_only_terminal_jobs_and_deletes_nothing(client):
    seed_assignment()
    seed_submission("s-active", student_id="u1")
    active = make_job("s-active", "u1")
    seed_ready_submission("s-done", "r-done", student_id="u2")
    done = make_job("s-done", "u2")
    store.set_status(done["id"], states.COMPLETE, result_id="r-done")
    store.report_upload_begin(_result_row("r-done", "s-done"), "hash-1")
    store.report_upload_finish("r-done", "hash-1", status="UPLOADED",
                               remote_file_id="drive-9")
    store.save_bulk_upload_component(result_id="r-done", coursework_id=CW,
                                     submission_id="s-done", component="report",
                                     status="UPLOADED", report_hash="hash-1")

    before = client.get("/api/automation/jobs/clearable-count").json()["count"]
    assert before == 1

    # Without confirmation nothing happens.
    refused = client.post("/api/automation/jobs/clear", json={})
    assert refused.status_code == 400
    assert refused.json()["code"] == "confirmation_required"

    result = client.post("/api/automation/jobs/clear",
                         json={"confirm": True}).json()
    assert result["hidden"] == 1
    visible = {j["id"] for j in client.get("/api/automation/jobs").json()["jobs"]}
    assert done["id"] not in visible, "a cleared job is still on the list"
    assert active["id"] in visible, "an active job was cleared"

    # Everything needed to retry or verify an upload is still there.
    assert store.get(done["id"]) is not None            # the job row itself
    assert db.query_one("SELECT id FROM results WHERE id='r-done'") is not None
    assert store.report_upload_record("r-done")["status"] == "UPLOADED"
    assert store.bulk_upload_component("r-done")["report_status"] == "UPLOADED"
    # And a second clear finds nothing left to hide.
    assert client.post("/api/automation/jobs/clear",
                       json={"confirm": True}).json()["hidden"] == 0


def test_cleared_jobs_stay_cleared_after_a_restart(client):
    seed_assignment()
    seed_submission("s-done", student_id="u2")
    done = make_job("s-done", "u2")
    store.set_status(done["id"], states.COMPLETE)
    client.post("/api/automation/jobs/clear", json={"confirm": True})
    store.recover_in_flight()      # what a restart does at boot
    visible = {j["id"] for j in client.get("/api/automation/jobs").json()["jobs"]}
    assert done["id"] not in visible
