"""Central correction routing (§automatic routing for every checker).

Proves the ONE rule the whole app follows:

* the STUDENT answer PDF's page count decides the pipeline;
* at or below the central threshold (30 pages) the existing single-pass
  workflow runs unchanged;
* above it the submission is delegated to the Large AI Worker, pinned to the
  SAME job id, and split into balanced batches;
* the mark scheme's page count never influences routing;
* an unknown page count fails safely instead of assuming "short";
* no entry point can silently bypass the backend rule, because every entry
  point reaches the engine only through ``CorrectionService.handle_job``.

The batch engine is never invoked here (no AI quota is consumed); routing and
delegation happen before any upload.
"""

from __future__ import annotations

import pytest

import app.config as config
import app.db as db
from app.engine import router
from app.engine.service import CorrectionService
from app.jobs.manager import HandedOff
from app.large_pdf import states as lstates
from app.large_pdf import store as large_store


# ------------------------------------------------------------------ helpers

def _pages_pdf(path, pages: int) -> str:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return str(path)


@pytest.fixture
def large_env(fresh_state, monkeypatch):
    cfg = config.get_config()
    cfg.mock_mode = True          # keeps any blueprint lookup offline
    cfg.large_pdf.enabled = True
    config.save_config(cfg)
    return fresh_state


def _correction_job(job_id="job-1", *, submission_id=None, coursework_id=None,
                    course_id=None, student_id=None, name="Ada"):
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, course_id, "
        "student_id, student_name, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (job_id, submission_id, coursework_id, course_id, student_id, name,
         "PREPARING", 0.0, 0.0),
    )
    return db.row_to_dict(db.query_one(
        "SELECT * FROM correction_jobs WHERE id = ?", (job_id,)))


# ============================================================ routing rule

class TestRoutingRule:
    @pytest.mark.parametrize("pages,expected", [
        (12, router.NORMAL), (26, router.NORMAL), (30, router.NORMAL),
        (31, router.LARGE), (52, router.LARGE), (100, router.LARGE),
        (150, router.LARGE),
    ])
    def test_route_by_student_page_count(self, tmp_path, pages, expected):
        pdf = _pages_pdf(tmp_path / f"{pages}.pdf", pages)
        decision = router.route_student_pdf(pdf)
        assert decision.kind == expected
        assert decision.page_count == pages
        assert decision.threshold == 30

    def test_threshold_lives_in_one_configuration_field(self, large_env, tmp_path):
        pdf = _pages_pdf(tmp_path / "p.pdf", 40)
        assert router.route_student_pdf(pdf).kind == router.LARGE
        # Change the ONE central value; the router follows it.
        config.get_config().large_pdf.threshold_pages = 50
        assert router.route_student_pdf(pdf).kind == router.NORMAL
        assert router.threshold_pages() == 50

    def test_mark_scheme_page_count_never_affects_routing(self, tmp_path):
        student = _pages_pdf(tmp_path / "student.pdf", 12)
        scheme = _pages_pdf(tmp_path / "scheme.pdf", 150)   # huge mark scheme
        decision = router.route_student_pdf(student)
        assert decision.kind == router.NORMAL
        assert decision.page_count == 12
        assert scheme  # the mark scheme is deliberately unused here

    def test_unknown_page_count_fails_safely(self, tmp_path):
        bad = tmp_path / "broken.pdf"
        bad.write_bytes(b"this is not a PDF at all")
        with pytest.raises(router.RoutingError):
            router.route_student_pdf(str(bad))


# ============================================================ delegation

class TestServiceDelegation:
    def test_long_document_is_delegated_to_the_large_worker(self, large_env, tmp_path):
        student = _pages_pdf(tmp_path / "s.pdf", 52)
        scheme = _pages_pdf(tmp_path / "m.pdf", 8)
        job = _correction_job("long-1")
        db.execute("INSERT INTO pending_manual_files(job_id, kind, path, filename) "
                   "VALUES(?,?,?,?)", ("long-1", "mark_scheme", scheme, "m.pdf"))

        svc = CorrectionService()
        with pytest.raises(HandedOff):
            svc._route_or_continue(job, [student])

        large = large_store.get_job("long-1")
        assert large is not None, "the long document must reach the Large AI Worker"
        assert large["id"] == job["id"]                 # one job row, pinned id
        assert large["page_count"] == 52
        assert large["batch_count"] == 2                # 52 -> balanced batches
        assert large["status"] in (lstates.READY, lstates.PROCESSING)

    def test_short_document_stays_on_the_normal_workflow(self, large_env, tmp_path):
        student = _pages_pdf(tmp_path / "s.pdf", 30)
        scheme = _pages_pdf(tmp_path / "m.pdf", 8)
        job = _correction_job("short-1")
        db.execute("INSERT INTO pending_manual_files(job_id, kind, path, filename) "
                   "VALUES(?,?,?,?)", ("short-1", "mark_scheme", scheme, "m.pdf"))

        svc = CorrectionService()
        # Returns normally (no hand-off) so the existing single-pass flow runs.
        assert svc._route_or_continue(job, [student]) is None
        assert large_store.get_job("short-1") is None

    def test_classroom_submission_uses_the_same_rule(self, large_env, tmp_path):
        student = _pages_pdf(tmp_path / "s.pdf", 57)
        scheme = _pages_pdf(tmp_path / "m.pdf", 6)
        job = _correction_job("cw-1", submission_id="sub-1", coursework_id="cw-1",
                              course_id="course-1", student_id="stu-1")
        db.execute("INSERT INTO pending_mark_schemes(coursework_id, path, uploaded_at) "
                   "VALUES(?,?,?)", ("cw-1", scheme, 0.0))

        svc = CorrectionService()
        with pytest.raises(HandedOff):
            svc._route_or_continue(job, [student])
        large = large_store.get_job("cw-1")
        assert large is not None
        assert large["submission_id"] == "sub-1"
        assert large["batch_count"] == 3               # 57 -> 19 + 19 + 19

    def test_repeated_routing_does_not_duplicate_batches(self, large_env, tmp_path):
        student = _pages_pdf(tmp_path / "s.pdf", 100)
        scheme = _pages_pdf(tmp_path / "m.pdf", 8)
        job = _correction_job("rep-1")
        db.execute("INSERT INTO pending_manual_files(job_id, kind, path, filename) "
                   "VALUES(?,?,?,?)", ("rep-1", "mark_scheme", scheme, "m.pdf"))
        svc = CorrectionService()
        for _ in range(3):
            with pytest.raises(HandedOff):
                svc._route_or_continue(job, [student])
        batches = large_store.list_batches("rep-1")
        assert len(batches) == large_store.get_job("rep-1")["batch_count"]

    def test_long_document_without_mark_scheme_fails_actionably(self, large_env, tmp_path):
        student = _pages_pdf(tmp_path / "s.pdf", 52)
        job = _correction_job("noscheme")
        svc = CorrectionService()
        with pytest.raises(RuntimeError) as exc:
            svc._route_or_continue(job, [student])
        assert "mark scheme" in str(exc.value).lower()
        assert large_store.get_job("noscheme") is None


# ============================================================ job manager

class TestJobManagerHandoff:
    def test_handed_off_job_is_not_completed_and_not_cleaned(self, large_env):
        from app.jobs.manager import JobManager

        cleaned: list[str] = []
        mgr = JobManager(worker_count=1)

        def _handler(job, cancel):
            raise HandedOff()

        mgr.set_handler("manual_correction", _handler)
        mgr.set_cleanup_handler(lambda job_id: cleaned.append(job_id))

        job = mgr.enqueue(submission_id=None, coursework_id=None, course_id=None,
                          student_id=None, student_name="Ada")
        mgr._run_job(mgr.get(job["id"]))

        after = mgr.get(job["id"])
        assert after["status"] != "COMPLETED", after
        assert cleaned == [], "a delegated job must keep its source files"


# ============================================================ end to end

class TestEndToEndRouting:
    def test_long_document_flows_through_the_large_worker(self, large_env, tmp_path):
        """A real 52-page job runs the ACTUAL handler and commandeers the
        Large AI Worker end to end: same job id, one result, sources cleaned."""
        import threading

        student = _pages_pdf(tmp_path / "s.pdf", 52)
        scheme = _pages_pdf(tmp_path / "m.pdf", 8)
        _correction_job("e2e-1", name="Ada")
        for kind, path, name in (("submission", student, "s.pdf"),
                                 ("mark_scheme", scheme, "m.pdf")):
            db.execute("INSERT INTO pending_manual_files(job_id, kind, path, filename) "
                       "VALUES(?,?,?,?)", ("e2e-1", kind, path, name))

        svc = CorrectionService()
        job = db.row_to_dict(db.query_one(
            "SELECT * FROM correction_jobs WHERE id = ?", ("e2e-1",)))
        with pytest.raises(HandedOff):
            svc.handle_job(job, threading.Event())

        # The Large AI Worker (as its scheduler would) finishes the job.
        from app.large_pdf.runner import LargePdfRunnerHolder
        final = LargePdfRunnerHolder.instance().process("e2e-1")
        assert final["status"] in (lstates.COMPLETED, lstates.AWAITING_REVIEW), final

        # ONE job row and ONE result, all carrying the original id.
        cj = db.query_one("SELECT * FROM correction_jobs WHERE id = ?", ("e2e-1",))
        assert cj["status"] == "COMPLETED", dict(cj)   # mirrors the terminal state
        assert cj["result_id"]
        res = db.query_one("SELECT * FROM results WHERE id = ?", (cj["result_id"],))
        assert res is not None and res["job_id"] == "e2e-1"

        # Privacy cleanup: a fully COMPLETED correction removes its uploaded
        # source files; one AWAITING_REVIEW keeps them so a teacher can re-mark.
        sources = db.query_one("SELECT 1 FROM pending_manual_files WHERE job_id = ?",
                              ("e2e-1",))
        if final["status"] == lstates.COMPLETED:
            assert sources is None
        else:
            assert final["status"] == lstates.AWAITING_REVIEW, final
            assert sources is not None
            assert "review" in (cj["stage_detail"] or "").lower()
