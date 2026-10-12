"""Assignment repair workflow — regression tests.

Reproduces the defect classes found on the live C1L13 - Sets corrections and
locks the repair behaviour:

* per-question maxima leaking from the model (262/262 instead of 266),
* an unmarked question charged to the student as a 0 (Alia's 32 questions),
* a 0 with no evidence the answer was read (silent zero),
* duplicates, over-max marks, missing questions, wrong totals.

Plus the repair-session promises: originals are never modified, repaired
results live only in the dedicated test records, every submission is
inventoried (including the inaccessible ones), and the dashboard labels
everything TESTING ONLY.
"""

from __future__ import annotations

import json

import pytest

import app.config as config
import app.db as db
from app.engine.blueprint import save_cached
from app.repair import pipeline, store
from app.repair.audit import audit_result

CW = "cw-test-1"
COURSE = "course-test-1"


# --------------------------------------------------------------- fixtures

def _blueprint(numbers=None, maxima=None) -> dict:
    maxima = maxima or {}
    numbers = numbers or [1, 2, 3]
    return {"questions": [
        {"question_number": str(n), "max_marks": maxima.get(n, 4),
         "criteria": f"criterion for {n}"}
        for n in numbers]}


def _row(number, awarded, max_marks=4, status="VALID", **extra):
    row = {
        "question_number": str(number), "max_marks": max_marks,
        "awarded_marks": awarded, "status": status,
        "student_answer": "shows working", "working_summary": "correct method",
        "loss_reason": "" if awarded == max_marks else "arithmetic slip",
        "mark_breakdown": [{"code": "M1", "awarded": True, "reason": "x"}],
    }
    row.update(extra)
    return row


def _result_row(**over):
    questions = over.pop("questions")
    earned = over.pop("earned", sum(q["awarded_marks"] for q in questions))
    available = over.pop("available",
                         sum(q["max_marks"] for q in questions))
    row = {
        "id": "res-1", "job_id": "job-1", "coursework_id": CW,
        "submission_id": "sub-1", "student_id": "stu-1",
        "student_name": "Student One",
        "earned": earned, "available": available,
        "percentage": 100.0 * earned / available if available else 0.0,
        "result_json": json.dumps({"questions": questions,
                                   "totals": {"score_basis": "COMPLETE"}}),
        "verification_json": "{}", "review_required": 0, "created_at": 1.0,
    }
    row.update(over)
    return row


def _seed_correction(student_no=1):
    db.execute(
        "INSERT INTO correction_jobs (id, submission_id, coursework_id, "
        "course_id, student_id, student_name, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'COMPLETED', 1.0, 1.0)",
        (f"job-{student_no}", f"sub-{student_no}", CW, COURSE,
         f"stu-{student_no}", f"Student {student_no}"))


def _insert_result(row: dict) -> None:
    db.execute(
        "INSERT INTO results (id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, "
        "result_json, verification_json, review_required, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["job_id"], row["coursework_id"], row["submission_id"],
         row["student_id"], row["student_name"], row["earned"],
         row["available"], row["percentage"], row["result_json"],
         row["verification_json"], row.get("review_required", 0),
         row.get("created_at", 1.0)))


# ============================================ deterministic audit (Stage 3)

class TestDeterministicAudit:
    """The audit is independent of the AI and must see the live defect shapes."""

    def test_model_maxima_leak_is_caught(self):
        # The real Abdulrahman shape: rows say 262 marks (Q15/28/34 demoted),
        # stored as 262/262 = 100% against a 266-mark scheme.
        bp = _blueprint(range(1, 5), maxima={1: 60, 2: 60, 3: 60, 4: 86})
        questions = [_row(1, 60, 60), _row(2, 60, 60),
                     _row(3, 60, 60), _row(4, 82, 82)]
        findings = audit_result(questions=questions, earned=262, available=262,
                                blueprint=bp)
        codes = {f.code for f in findings}
        assert "MAX_MISMATCH" in codes
        assert "DENOMINATOR_MISMATCH" in codes
        detail = next(f for f in findings if f.code == "MAX_MISMATCH")
        assert detail.question == "4"
        assert detail.numbers == {"scheme_max": 86, "row_max": 82}

    def test_unmarked_question_charged_as_zero_is_caught(self):
        # The real Alia shape: 32 REVIEW rows awarded 0, total divided by 266.
        bp = _blueprint(range(1, 4))
        questions = [_row(1, 4), _row(2, 4),
                     _row(3, 0, status="REVIEW", student_answer="",
                          working_summary="", loss_reason="",
                          mark_breakdown=[])]
        findings = audit_result(questions=questions, earned=8, available=12,
                                blueprint=bp, score_basis="COMPLETE")
        hits = [f for f in findings if f.code == "UNMARKED_AS_ZERO"]
        assert hits and hits[0].question == "3"
        assert "must not be counted as the student's 0" in hits[0].detail

    def test_silent_zero_is_caught(self):
        bp = _blueprint([1, 2])
        questions = [_row(1, 4), _row(2, 0, student_answer="",
                                     working_summary="", loss_reason="",
                                     mark_breakdown=[])]
        findings = audit_result(questions=questions, earned=4, available=8,
                                blueprint=bp)
        hits = [f for f in findings if f.code == "SUSPICIOUS_ZERO"]
        assert hits and hits[0].question == "2"

    def test_duplicates_missing_overmax_and_sum_mismatch_are_caught(self):
        bp = _blueprint([1, 2, 3])
        questions = [_row(1, 4), _row(1, 4), _row(2, 9, 4)]
        findings = audit_result(questions=questions, earned=17, available=12,
                                blueprint=bp)
        codes = {f.code for f in findings}
        assert "DUPLICATE_QUESTION" in codes
        assert "MISSING_QUESTION" in codes
        assert "OVER_MAX" in codes
        assert "SUM_MISMATCH" in codes

    def test_a_clean_result_produces_no_high_findings(self):
        bp = _blueprint([1, 2])
        questions = [_row(1, 3), _row(2, 4)]
        findings = audit_result(questions=questions, earned=7, available=8,
                                blueprint=bp)
        assert not [f for f in findings if f.severity == "high"]


# ============================================ per-question diff (Stage 3)

class TestCompareRows:
    def test_unresolved_original_repaired_marks_added(self):
        bp = _blueprint([1])
        original = [_row(1, 0, status="REVIEW", student_answer="",
                        working_summary="", loss_reason="", mark_breakdown=[])]
        repaired = [_row(1, 3)]
        diff = pipeline.compare_rows(original=original, repaired=repaired,
                                     blueprint=bp)
        assert diff[0]["change"] == pipeline.ADDED
        assert diff[0]["original_awarded"] == 0
        assert diff[0]["repaired_awarded"] == 3
        assert diff[0]["validation"] == pipeline.TEACHER_CONFIRM
        assert "evidence" in diff[0]

    def test_disagreement_preserves_both_and_flags(self):
        bp = _blueprint([1])
        diff = pipeline.compare_rows(original=[_row(1, 4)],
                                     repaired=[_row(1, 2)], blueprint=bp)
        assert diff[0]["change"] == pipeline.CHANGED
        assert (diff[0]["original_awarded"], diff[0]["repaired_awarded"]) == (4, 2)
        assert diff[0]["validation"] == pipeline.TEACHER_CONFIRM

    def test_matching_marks_are_consistency_evidence_only(self):
        bp = _blueprint([1])
        diff = pipeline.compare_rows(original=[_row(1, 3)],
                                     repaired=[_row(1, 3)], blueprint=bp)
        assert diff[0]["change"] == pipeline.MATCH
        assert "consistency" in diff[0]["validation"].lower()

    def test_accounting_fix_is_deterministic(self):
        bp = _blueprint([1], maxima={1: 6})
        original = [_row(1, 2, 2)]      # stale maximum
        repaired = [_row(1, 2, 6)]
        diff = pipeline.compare_rows(original=original, repaired=repaired,
                                     blueprint=bp)
        assert diff[0]["change"] == pipeline.DETERMINISTIC_FIX
        assert diff[0]["validation"] == pipeline.DETERMINISTIC
        assert "official 6" in diff[0]["reason"]

    def test_still_unmarked_rows_are_explicitly_unresolved(self):
        bp = _blueprint([1])
        diff = pipeline.compare_rows(
            original=[_row(1, 0, status="REVIEW")],
            repaired=[_row(1, 0, status="NEEDS_REVIEW",
                          review_reason="still no mark")],
            blueprint=bp)
        assert diff[0]["change"] == pipeline.UNRESOLVED
        assert "still no mark" in diff[0]["reason"]


# ============================================ validation gates (Stage 3)

class TestValidateRepair:
    def test_complete_consistent_repair_passes(self):
        bp = _blueprint([1, 2])
        out = pipeline.validate_repair(questions=[_row(1, 3), _row(2, 4)],
                                       earned=7, available=8, blueprint=bp)
        assert out["ok"] is True
        assert out["checks"]["no_unresolved_questions"] is True

    def test_unresolved_questions_never_verify(self):
        bp = _blueprint([1, 2])
        questions = [_row(1, 3), _row(2, 0, status="NEEDS_REVIEW",
                                     review_reason="marker returned nothing")]
        out = pipeline.validate_repair(questions=questions, earned=3,
                                       available=8, blueprint=bp)
        assert out["ok"] is False
        assert out["unresolved_questions"] == ["2"]
        assert out["checks"]["no_unresolved_questions"] is False

    def test_wrong_denominator_fails_validation(self):
        bp = _blueprint([1, 2])
        out = pipeline.validate_repair(questions=[_row(1, 3), _row(2, 4)],
                                       earned=7, available=7, blueprint=bp)
        assert out["ok"] is False
        assert out["checks"]["denominator_is_scheme_total"] is False


# ============================================ item status (Stage 6 honesty)

class TestItemStatus:
    def test_error_means_failed(self):
        assert pipeline.item_status([], {}, "boom") == "FAILED"

    def test_unresolved_rows_stay_unresolved(self):
        diff = [{"change": pipeline.UNRESOLVED}]
        assert pipeline.item_status(diff, {"missing_questions": []}) == "UNRESOLVED"

    def test_disagreement_needs_review(self):
        diff = [{"change": pipeline.CHANGED, "evidence": "x"}]
        assert pipeline.item_status(diff, {"ok": True}) == "NEEDS_REVIEW"

    def test_added_marks_without_evidence_need_review(self):
        diff = [{"change": pipeline.ADDED, "evidence": ""}]
        assert pipeline.item_status(diff, {"ok": True}) == "NEEDS_REVIEW"

    def test_added_marks_with_evidence_are_repaired(self):
        diff = [{"change": pipeline.ADDED, "evidence": "student wrote x = 3"}]
        assert pipeline.item_status(diff, {"ok": True}) == "REPAIRED"

    def test_full_agreement_is_match(self):
        diff = [{"change": pipeline.MATCH, "evidence": "x"}]
        assert pipeline.item_status(diff, {"ok": True}) == "MATCH"


# ============================================ isolation (Stage 1 safety)

class TestRepairRecordsAreIsolated:
    def test_repair_records_never_touch_results(self, fresh_state):
        _seed_correction(1)
        _insert_result(_result_row(questions=[_row(1, 4)]))
        before = [dict(r) for r in db.query("SELECT * FROM results")]

        run = store.create_run(coursework_id=CW, course_id=COURSE)
        store.add_item(run["id"], {
            "coursework_id": CW, "submission_id": "sub-1", "student_id": "stu-1",
            "student_name": "Student One", "mode": "SECOND_PASS",
            "status": "REPAIRED", "original_result_id": "res-1",
            "original_earned": 4, "original_available": 4,
            "repaired_earned": 4, "repaired_available": 4,
            "questions_total": 1, "diff": [], "validation": {"ok": True},
            "artifact": {"payload": "original and repaired both kept"},
        })
        store.finish_run(run["id"], summary={"counts": {}})

        after = [dict(r) for r in db.query("SELECT * FROM results")]
        assert after == before, "a repair must never modify a stored result"

        summary = store.load_summary(CW)
        assert summary["counts"]["items"] == 1
        assert summary["items"][0]["status"] == "REPAIRED"
        path = summary["items"][0]["artifact_path"]
        payload = json.loads(open(path, encoding="utf-8").read())
        assert payload["payload"] == "original and repaired both kept"

    def test_run_banner_is_testing_only(self, fresh_state):
        run = store.create_run(coursework_id=CW, course_id=COURSE)
        loaded = store.load_run(run["id"])
        assert loaded["banner"] == "TESTING ONLY · REPAIRED RESULTS NOT PUBLISHED"


# ============================================ inventory (Stage 2)

class TestInventory:
    def _seed_submissions(self):
        rows = [
            # (submission, student, name, state, attachments_json)
            ("s1", "a1", "Ann", "TURNED_IN",
             json.dumps([{"kind": "drive", "id": "f1", "title": "work.pdf"}])),
            ("s2", "a2", "Bob", "TURNED_IN",
             json.dumps([{"kind": "drive", "id": "f2", "title": "scan.zip"}])),
            ("s3", "a3", "Cid", "TURNED_IN",
             json.dumps([{"kind": "link", "id": "http://x", "title": "http://x"}])),
            ("s4", "a4", "Dee", "CREATED", "[]"),
            ("s5", "a5", "Eve", "RETURNED",
             json.dumps([{"kind": "drive", "id": "f5", "title": "work.pdf"}])),
        ]
        for sub, sid, name, state, atts in rows:
            db.execute(
                "INSERT INTO classroom_submissions (submission_id, course_id, "
                "coursework_id, student_id, student_name, current_state, "
                "attachments_json, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1.0)",
                (sub, COURSE, CW, sid, name, state, atts))

    def test_every_submission_is_accounted_for(self, fresh_state):
        self._seed_submissions()
        _seed_correction(1)
        _insert_result(_result_row(questions=[_row(1, 4)],
                                  submission_id="s1", student_id="a1",
                                  student_name="Ann"))
        inv = pipeline.assignment_inventory(CW)

        assert inv["counts"]["total"] == 5
        assert inv["counts"]["submitted"] == 4
        assert inv["counts"]["corrected"] == 1
        assert inv["counts"]["uncorrected"] == 3
        assert inv["counts"]["not_submitted"] == 1
        assert inv["counts"]["inaccessible"] == 2     # zip + link-only

        by_name = {r["student_name"]: r for r in inv["submissions"]}
        assert by_name["Bob"]["accessible"] is False
        assert "archive" in by_name["Bob"]["note"]
        assert by_name["Cid"]["accessible"] is False
        assert "link-only" in by_name["Cid"]["note"]
        assert by_name["Dee"]["accessible"] is False
        assert by_name["Dee"]["note"] == "not submitted"
        assert by_name["Ann"]["accessible"] is True
        assert by_name["Ann"]["has_correction"] is True
        # Nothing is silently excluded: every row carries an explicit state.
        assert all(r["state"] for r in inv["submissions"])


# ============================================ API (Stage 8)

@pytest.fixture
def api(fresh_state):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as client:
        yield client


class TestRepairApi:
    def _seed(self, tmp_path):
        import hashlib

        scheme = config.DATA_DIR / "mark_schemes" / f"{CW}.pdf"
        scheme.parent.mkdir(parents=True, exist_ok=True)
        scheme.write_bytes(b"%PDF-1.4 scheme")
        sha = hashlib.sha256(scheme.read_bytes()).hexdigest()
        save_cached(sha, _blueprint([1, 2]), model="teacher")

        db.execute(
            "INSERT INTO classroom_submissions (submission_id, course_id, "
            "coursework_id, student_id, student_name, current_state, "
            "attachments_json, first_seen_at) "
            "VALUES ('sub-1', ?, ?, 'stu-1', 'Student One', 'TURNED_IN', ?, 1.0)",
            (COURSE, CW,
             json.dumps([{"kind": "drive", "id": "f1", "title": "w.pdf"}])))
        _seed_correction(1)
        _insert_result(_result_row(questions=[
            _row(1, 4), _row(2, 0, status="REVIEW", student_answer="",
                             working_summary="", loss_reason="",
                             mark_breakdown=[])]))
        run = store.create_run(coursework_id=CW, course_id=COURSE)
        store.add_item(run["id"], {
            "coursework_id": CW, "submission_id": "sub-1", "student_id": "stu-1",
            "student_name": "Student One", "mode": "SECOND_PASS",
            "status": "REPAIRED", "original_result_id": "res-1",
            "questions_total": 2, "diff": [], "validation": {"ok": True},
            "artifact": {"hello": "world"}})
        store.finish_run(run["id"], summary={"counts": {}}, status="COMPLETE")
        return run

    def test_summary_carries_banner_inventory_audit_and_repairs(
            self, api, tmp_path):
        self._seed(tmp_path)
        r = api.get(f"/api/repair/summary?coursework_id={CW}")
        assert r.status_code == 200
        body = r.json()
        assert body["banner"] == "TESTING ONLY · REPAIRED RESULTS NOT PUBLISHED"
        assert body["inventory"]["counts"]["total"] == 1
        assert body["repair"]["counts"]["items"] == 1
        codes = {f["code"] for a in body["audits"] for f in a["findings"]}
        assert "UNMARKED_AS_ZERO" in codes       # the Alia defect is visible
        assert body["scheme"] == {"questions": 2, "total_marks": 8}

    def test_item_artifact_and_report_preview(self, api, tmp_path):
        run = self._seed(tmp_path)
        item = store.load_summary(CW)["items"][0]
        r = api.get(f"/api/repair/item/{run['id']}/{item['id']}")
        assert r.status_code == 200
        assert r.json()["hello"] == "world"

        # A real repaired report PDF previews; a missing one 404s honestly.
        rep = store.repairs_dir() / run["id"] / "reports"
        rep.mkdir(parents=True, exist_ok=True)
        (rep / "ann_repaired_report.pdf").write_bytes(b"%PDF-1.4 fake")
        ok = api.get(f"/api/repair/report/{run['id']}/ann_repaired_report.pdf")
        assert ok.status_code == 200
        assert ok.headers["content-type"] == "application/pdf"
        missing = api.get(f"/api/repair/report/{run['id']}/nope.pdf")
        assert missing.status_code == 404

    def test_path_traversal_is_rejected(self, api, tmp_path):
        run = self._seed(tmp_path)
        r = api.get(f"/api/repair/report/{run['id']}/..%2F..%2Fsecrets.toml")
        assert r.status_code in (400, 404)
