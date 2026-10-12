"""Regression tests for the REPORT-DELIVERY defects found in ``data/logs``.

Every test here reproduces something that really happened in the production
log and would silently come back without a guard:

1. ``REPORT_FAILED / report_invalid`` — "the report is missing expected
   content (Yahia Mohamed Noureldin)".  The automation worker rendered the RAW
   result row while every other surface renders the DECORATED identity, so the
   automated report showed a different student name than the one the file was
   validated against.  4 jobs parked in REPORT_FAILED, 1 246 log lines.
2. A name that WRAPPED across two lines was reported as missing, because the
   comparison used the raw extracted text including its line breaks.
3. ``REPORT_UPLOAD_FAILED / report_upload_failed`` = "Google Drive upload
   failed: TESTING ONLY: ...".  The local stop-the-line guard was recorded as a
   Drive failure, retried three times and parked as a Drive problem.
4. "workflow step failed permanently" x50 973 on ONE job (median gap 0.17 s):
   a permanently failing job must stay parked, never be re-run every tick.
"""

from __future__ import annotations

import io

import pytest

from app.automation import pdf_validation, runner as runner_mod, states, store


# --------------------------------------------------------------- helpers

def _pdf(lines: list[str]) -> bytes:
    """A genuinely parseable one-page PDF with the given lines."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def _seed_student(student_id: str = "u1", name: str = "Yahia Mohamed Noureldin",
                  nickname: str = "Yehia Nour") -> None:
    from app import db
    import time
    db.execute(
        "INSERT OR IGNORE INTO students(id, name, nickname, fetched_at) "
        "VALUES(?,?,?,?)",
        (student_id, name, nickname, time.time()))
    row = db.query_one("SELECT nickname FROM students WHERE id = ?", (student_id,))
    assert row is not None and (row["nickname"] or "") == nickname


def _seed_coursework(coursework_id: str = "w1", title: str = "Ch2L1") -> None:
    from app import db
    import time
    db.execute("INSERT OR IGNORE INTO courses(id, name, fetched_at) VALUES(?,?,?)",
               ("c1", "Maths", time.time()))
    db.execute(
        "INSERT OR IGNORE INTO coursework(id, course_id, title, fetched_at, "
        "allowlisted) VALUES(?,?,?,?,1)",
        (coursework_id, "c1", title, time.time()))


def _seed_tracking(submission_id: str = "s1", student_id: str = "u1",
                   name: str = "Yahia Mohamed Noureldin") -> None:
    from app import db
    import json
    import time
    db.execute(
        """
        INSERT INTO classroom_submissions(
            submission_id, course_id, coursework_id, student_id, student_name,
            current_state, update_time, attachments_json, data_quality,
            last_synced_at, first_seen_at, sync_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
        """,
        (submission_id, "c1", "w1", student_id, name, "TURNED_IN",
         "2026-09-01T09:00:00Z",
         json.dumps([{"kind": "drive", "id": "f1", "title": "hw.pdf"}]),
         "VERIFIED", time.time(), time.time()))


def _seed_result(result_id: str = "r1", *, snapshot_name: str = "Yehia Nour",
                 student_id: str = "u1") -> None:
    """The row the real database held: snapshot name != Classroom identity."""
    from app import db
    import json
    import time
    db.execute(
        "INSERT OR IGNORE INTO correction_jobs(id, submission_id, coursework_id,"
        " course_id, student_id, status, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        ("cj1", "s1", "w1", "c1", student_id, "COMPLETED", time.time(),
         time.time()))
    db.execute(
        """
        INSERT OR REPLACE INTO results(
            id, job_id, coursework_id, submission_id, student_id, student_name,
            earned, available, percentage, provisional_grade, result_json,
            review_required, manual_override, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0,?)
        """,
        (result_id, "cj1", "w1", "s1", student_id, snapshot_name, 38, 47, 80.85,
         "8", json.dumps({"questions": [], "overall_notes": {}}), time.time()))


def _make_job(*, student_name: str = "Yahia Mohamed Noureldin",
              assignment_title: str = "Ch2L1", version: str = "v1") -> dict:
    job = store.create_job(
        course_id="c1", coursework_id="w1", assignment_title=assignment_title,
        student_id="u1", student_name=student_name, submission_id="s1",
        submission_version=version)
    return job


class _Runner:
    """A WorkflowRunner with only the report steps exercised."""

    def __init__(self, *, rendered: list, validated: list, attach_status=None,
                 attach=None):
        from app.config import AutomationConfig
        from app.classroom import report_attachment as ra
        from types import SimpleNamespace

        class _Service:
            def get_result(self, result_id):
                from app import db
                row = db.query_one("SELECT * FROM results WHERE id = ?",
                                   (result_id,))
                if not row:
                    return None
                import json
                data = dict(row)
                for key in ("result_json", "verification_json"):
                    if isinstance(data.get(key), str):
                        data[key] = json.loads(data[key])
                return data

            def enqueue_classroom_submission(self, **kw):     # pragma: no cover
                return {"id": "cj1"}

            def get_correction_status(self, job_id):          # pragma: no cover
                return {"id": job_id, "status": "COMPLETED", "result_id": "r1"}

        def render(result, **kw):
            rendered.append(result)
            # A stand-in for the real report: it prints the canonical name and
            # the assignment title, exactly like the production renderer.
            from app import db
            row = db.query_one("SELECT title FROM coursework WHERE id = ?",
                               (result.get("coursework_id"),))
            return _pdf([str(result.get("display_name")
                             or result.get("student_name") or ""),
                         f"Assignment: {row['title'] if row else ''}"])

        self.validated = validated

        def validate(content, **kw):
            validated.append(kw)
            return pdf_validation.validate_report_pdf(content, **kw)

        attach_fn = attach
        if attach_fn is None:
            def attach_fn(token, result, **kw):
                rendered.append(result)
                return SimpleNamespace(
                    status=attach_status or ra.STATUS_VERIFIED,
                    message="testing only" if attach_status
                            == ra.STATUS_TESTING_BLOCKED else "ok",
                    extra={"drive_file_id": "drive-1",
                           "report_filename": "r.pdf"},
                    to_dict=lambda: {"status": attach_status})

        self.runner = runner_mod.WorkflowRunner(
            correction_service=_Service(), attach=attach_fn,
            publish=lambda *a, **k: {"results": [{"status": "published",
                                                  "http_status": 200}]},
            render_pdf=render, pdf_validate=validate,
            drive_verify=lambda *a, **k: {"ok": True},
            submission_reader=lambda job: {
                "draftGrade": "8", "assignedGrade": None, "state": "RETURNED",
                "updateTime": "2026-09-03T00:00:00Z"},
            account_email="t@school.org",
            cleanup=lambda *a, **k: {"ok": True}, token_fn=lambda: "tok",
            config=AutomationConfig(review_mode=False))


# --------------------------------------- 1. name matching (raw PDF payload)

def test_a_name_that_wrapped_onto_two_lines_is_not_reported_as_missing():
    """PDF extraction reproduces the report's line breaks.

    The production failure was a name that wrapped in a key/value cell, so the
    extracted text contained a newline inside it and the substring test failed
    even though the name was plainly printed.
    """
    pdf = _pdf(["Student", "Yahia Mohamed", "Noureldin"])
    out = pdf_validation.validate_report_pdf(
        pdf, expected_text=["Yahia Mohamed Noureldin"])
    assert out["ok"] is True, out


def test_any_alias_of_the_same_student_is_enough():
    pdf = _pdf(["Student", "Yehia Nour"])
    assert pdf_validation.validate_report_pdf(
        pdf, expected_any=["Yahia Mohamed Noureldin", "Yehia Nour"])["ok"] is True
    assert pdf_validation.validate_report_pdf(
        pdf, expected_any=["Somebody Else", "Nobody"])["ok"] is False


def test_expected_text_is_still_required_in_full_and_rejected_when_absent():
    pdf = _pdf(["Student", "Yehia Nour", "Assignment: Ch2L1"])
    assert pdf_validation.validate_report_pdf(
        pdf, expected_text=["Ch2L1"],
        expected_any=["Yehia Nour"])["ok"] is True
    other = _pdf(["Student", "Yehia Nour", "Assignment: Ch9Z9"])
    out = pdf_validation.validate_report_pdf(
        other, expected_text=["Ch2L1"], expected_any=["Yehia Nour"])
    assert out["ok"] is False
    assert "Ch2L1" in out["reason"]


def test_case_and_spacing_do_not_decide_acceptance():
    pdf = _pdf(["Student", "  YAhiA   Mohamed Noureldin "])
    assert pdf_validation.validate_report_pdf(
        pdf, expected_text=["yahia mohamed noureldin"])["ok"] is True


# ------------------------- 2. the automation render must use ONE identity

def test_the_report_step_renders_the_canonical_identity():
    """The regression that produced REPORT_FAILED / report_invalid ×1 246.

    The stored snapshot name ('Yehia Nour') differs from the Classroom roster
    name on the job ('Yahia Mohamed Noureldin').  The report the automation
    generates must carry the SAME canonical display name every other surface
    shows, and validation must accept it via the alias group.
    """
    _seed_student()
    _seed_coursework()
    _seed_tracking()
    _seed_result()
    job = _make_job()
    rendered: list = []
    validated: list = []
    r = _Runner(rendered=rendered, validated=validated)
    assert r.runner.advance(job["id"])["status"] in (
        states.REPORT_ATTACHED, states.ATTACHMENT_VERIFIED, states.COMPLETE)
    assert rendered, "the report was never rendered"
    rendered_row = rendered[0]
    # The canonical identity is what the PDF prints - not the raw snapshot.
    assert rendered_row.get("display_name") == "Yehia Nour"
    assert rendered_row.get("classroom_name") == "Yahia Mohamed Noureldin"
    kwargs = validated[0]
    # The roster name and the snapshot name are BOTH acceptable spellings.
    assert "Yahia Mohamed Noureldin" in kwargs["expected_any"]
    assert "Yehia Nour" in kwargs["expected_any"]
    # The assignment title the renderer prints is still demanded in full.
    assert kwargs["expected_text"] == ["Ch2L1"]


def test_the_automated_report_and_the_teacher_download_agree():
    """Same result, same identity: the two surfaces can never drift again."""
    from app import student_identity
    _seed_student()
    _seed_coursework()
    _seed_tracking()
    _seed_result()
    from app import db
    row = db.query_one("SELECT * FROM results WHERE id = 'r1'")
    import json
    data = dict(row)
    for key in ("result_json", "verification_json"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key]) if data.get(key) else {}
    decorated = student_identity.decorate(data)
    rendered: list = []
    r = _Runner(rendered=rendered, validated=[])
    r.runner._render_report({"id": "j1", "result_id": "r1"})
    assert rendered[0]["display_name"] == decorated["display_name"]
    assert rendered[0]["display_name"] == "Yehia Nour"


# ------------------------------------- 3. testing-only is not a Drive failure

def test_testing_only_block_is_its_own_non_retryable_attention_state():
    from app.classroom import report_attachment as ra
    _seed_student()
    _seed_coursework()
    _seed_tracking()
    _seed_result()
    job = _make_job()
    r = _Runner(rendered=[], validated=[],
                attach_status=ra.STATUS_TESTING_BLOCKED)
    final = r.runner.advance(job["id"])
    assert final["status"] == states.ATTENTION_REQUIRED
    assert final["error_code"] == "testing_only_blocked"
    # NOT the Drive-failure state, and no retry budget was burned.
    assert final["status"] != states.REPORT_UPLOAD_FAILED
    assert final["attempts"] == 1
    # A human must act: the job is parked, not due on the next tick.
    assert (final["next_attempt_at"] or 0) - __import__("time").time() > 3600


def test_a_testing_only_hold_can_be_retried_at_the_report_stage():
    """Turning the flag off and retrying resumes at the report stage — the
    AI correction and the grade are never recomputed."""
    from app.classroom import report_attachment as ra
    _seed_student()
    _seed_coursework()
    _seed_tracking()
    _seed_result()
    job = _make_job()
    r = _Runner(rendered=[], validated=[],
                attach_status=ra.STATUS_TESTING_BLOCKED)
    held = r.runner.advance(job["id"])
    resume = states.retry_step(held)
    assert resume in (states.REPORT_VALIDATED, states.RESULT_VALIDATED)
    assert resume != states.NEW


# ------------------------------------------- 4. parked failures never flood

def test_a_permanently_failed_job_stays_parked_and_is_not_rerun():
    """Production logged 50 973 'failed permanently' lines on ONE job.

    A permanent failure must park the job far into the future AND survive the
    boot-recovery pass, which is what stops the scheduler re-running the same
    failing step on every tick.
    """
    import time
    from app.automation import runner as rm
    _seed_student()
    _seed_coursework()
    _seed_tracking()
    _seed_result()
    job = _make_job()
    r = _Runner(rendered=[], validated=[])
    # A permanent failure raised from the report upload step.
    r.runner._record_failure(
        store.get(job["id"]),
        runner_mod.StepFailure("Google authorization expired.", transient=False,
                               state=states.CORRECTION_FAILED,
                               code="requires_auth"))
    parked = store.get(job["id"])
    assert parked["status"] == states.CORRECTION_FAILED
    assert parked["next_attempt_at"] - time.time() > 3600
    # Boot recovery must NOT make a parked job due again.
    store.recover_in_flight()
    assert store.get(job["id"])["next_attempt_at"] - time.time() > 3600
    assert store.next_runnable() is None
    # ...and a manual retry still re-arms it.
    r.runner.retry(job["id"])
    assert (store.get(job["id"])["next_attempt_at"] or 0) <= time.time()
