"""Automatic correction runs the SAME canonical grading engine as manual.

Evidence chain (each link is proven by a test below):

1. ENTRY — manual uploads (``CorrectionService.enqueue_manual_pdf``) and
   Classroom/automation (``CorrectionService.enqueue_classroom_submission``)
   both funnel through the single ``JobManager.enqueue`` into the same
   ``correction_jobs`` table. The job row carries source identity only —
   no engine settings can travel on it.
2. DESCRIPTOR — ``WorkerPool._launch`` builds ONE ``WorkerJob`` descriptor
   straight from config for every job: a manual job and a Classroom job are
   handed to the worker child with identical engine parameters.
3. EXECUTION — the single worker child (``app/workers/child.py``) calls the
   canonical ``app.engine.legacy_marking.grade_with_model`` with exactly
   that descriptor (spy-verified in-process, model chain compared).
4. END-TO-END — both job kinds run through the real pool + real child
   process in mock mode and must produce identical marks, totals, grade and
   marking model for identical inputs.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.config as config
import app.db as db


def _blank_pdf() -> bytes:
    import io

    from pypdf import PdfWriter
    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def parity_env(fresh_state, monkeypatch):
    """Mock Gemini mode + one Classroom assignment whose Drive submission and
    mark scheme hold the SAME bytes, so both job kinds mark identical input.
    """
    cfg = config.get_config()
    cfg.mock_mode = True
    cfg.worker.concurrency = 2
    config.save_config(cfg)
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaParityTestKey1234567890"])

    pdf = _blank_pdf()

    import app.classroom.auth as classroom_auth
    import app.classroom.client as classroom_client
    monkeypatch.setattr(classroom_auth, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(
        classroom_client, "download_drive_file",
        lambda token, file_id: ("homework.pdf", pdf, "application/pdf"))

    now_ts = time.time()
    db.execute("INSERT INTO courses(id, name, fetched_at) VALUES('c1','Maths',?)",
               (now_ts,))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, fetched_at, allowlisted) "
        "VALUES('w1','c1','HW',?,1)", (now_ts,))
    db.execute(
        "INSERT INTO submissions(id, coursework_id, course_id, user_id, state, "
        "attachment_ids, fetched_at) VALUES('s1','w1','c1','u1','TURNED_IN',?,?)",
        (json.dumps(["f1"]), now_ts))
    scheme_dir = config.DATA_DIR / "mark_schemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    (scheme_dir / "w1.pdf").write_bytes(pdf)
    return pdf


def _stage_manual(job_id: str, pdf: bytes) -> None:
    sub = config.DATA_DIR / f"{job_id}_sub.pdf"
    ms = config.DATA_DIR / f"{job_id}_ms.pdf"
    sub.write_bytes(pdf)
    ms.write_bytes(pdf)
    for kind, p in (("submission", sub), ("mark_scheme", ms)):
        db.execute(
            "INSERT INTO pending_manual_files(job_id, kind, path, filename) "
            "VALUES(?,?,?,?)", (job_id, kind, str(p), p.name))


def _job_dicts() -> tuple[dict, dict]:
    manual = {"id": "job-parity-manual", "submission_id": None,
              "coursework_id": None, "course_id": None, "student_name": "Alice",
              "is_remark": False, "created_at": time.time()}
    classroom = {"id": "job-parity-class", "submission_id": "s1",
                 "coursework_id": "w1", "course_id": "c1",
                 "student_name": "Bob", "is_remark": False,
                 "created_at": time.time()}
    return manual, classroom


def _launch_both(parity_env) -> tuple:
    """Run the real pool launch step for both job kinds; return descriptors."""
    from app.workers.pool import WorkerPool

    manual, classroom = _job_dicts()
    _stage_manual(manual["id"], parity_env)

    captured: list = []

    class _FakeWorker:
        def __init__(self, tag: str):
            self.tag = tag
            self.state = SimpleNamespace(fingerprint=None)

        def lease_key(self):
            return f"fp-{self.tag}"

        def start_job(self, wjob, on_progress=None):
            captured.append(wjob)

    pool = WorkerPool(worker_count=1)
    pool._launch(_FakeWorker("manual"), manual)
    pool._launch(_FakeWorker("class"), classroom)
    assert len(captured) == 2, "both job kinds must reach the worker pool"
    return captured[0], captured[1]


class TestEntryParity:
    """Link 1 — one enqueue funnel, one table, no engine settings on a job."""

    def test_both_entry_points_enqueue_into_the_same_table(
            self, parity_env, monkeypatch):
        import app.jobs.manager as jm
        from app.engine.service import CorrectionService

        captured = []
        real_enqueue = jm.JobManager.enqueue

        def spy(self, **kwargs):
            captured.append(kwargs)
            return real_enqueue(self, **kwargs)

        monkeypatch.setattr(jm.JobManager, "enqueue", spy)

        svc = CorrectionService()
        manual_job = svc.enqueue_manual_pdf(student_name="Alice")
        auto_job = svc.enqueue_classroom_submission(
            submission_id="s1", coursework_id="w1", course_id="c1",
            student_id="u1", student_name="Bob")

        assert len(captured) == 2, "both paths must use JobManager.enqueue"
        m, a = captured
        source_fields = {"submission_id", "coursework_id", "course_id",
                         "student_id", "student_name"}
        for key in set(m) | set(a):
            if key in source_fields:
                continue
            assert m.get(key) == a.get(key), f"enqueue differs on {key}"
        assert m["is_remark"] is False and a["is_remark"] is False

        # One table for both kinds.
        rows = db.query("SELECT id FROM correction_jobs")
        assert {r["id"] for r in rows} == {manual_job["id"], auto_job["id"]}

        # A job row has no field that could carry engine settings.
        cols = {r["name"].lower()
                for r in db.query("PRAGMA table_info(correction_jobs)")}
        banned = ("model", "prompt", "temperature", "engine", "blueprint")
        offending = [c for c in cols if any(t in c for t in banned)]
        assert not offending, f"engine settings on job row: {offending}"


class TestDescriptorParity:
    """Link 2 — one descriptor shape built from config for every job."""

    def test_identical_engine_parameters_for_manual_and_classroom(
            self, parity_env):
        w_manual, w_class = _launch_both(parity_env)
        cfg = config.get_config()

        for field in ("api_key", "request_timeout_ms", "primary_models",
                      "audit_enabled", "mock", "double_check_enabled"):
            assert getattr(w_manual, field) == getattr(w_class, field), field

        assert w_manual.primary_models == list(cfg.gemini.primary_models)
        assert w_manual.double_check_enabled == cfg.double_check.enabled
        assert w_manual.audit_enabled is False
        assert w_manual.mock is True and w_class.mock is True

        # The ONLY differences are identity and where the bytes came from.
        def engine_view(w):
            return dataclasses.replace(
                w, job_id="", student_name="", submission_paths=[],
                mark_scheme_path=None, question_paper_path=None)

        assert engine_view(w_manual) == engine_view(w_class)

        # Both descriptors carry real, staged input files.
        for w in (w_manual, w_class):
            assert w.submission_paths and Path(w.submission_paths[0]).is_file()
            assert w.mark_scheme_path and Path(w.mark_scheme_path).is_file()


class TestCanonicalEngineExecution:
    """Link 3 — the child runs legacy_marking.grade_with_model for both."""

    def test_child_calls_canonical_engine_with_same_model_chain(
            self, parity_env, monkeypatch):
        from app.engine import legacy_marking as legacy
        from app.engine import model_health_store as mhs
        from app.workers import child

        w_manual, w_class = _launch_both(parity_env)

        calls: list[dict] = []
        real = legacy.grade_with_model

        def spy(*args, **kwargs):
            out = real(*args, **kwargs)
            calls.append(kwargs)
            return out

        monkeypatch.setattr(legacy, "grade_with_model", spy)
        cfg = config.get_config()
        expected_chain = mhs.ordered_models(list(cfg.gemini.primary_models))

        def run(w) -> tuple[list, dict]:
            calls.clear()
            job = dataclasses.asdict(w)
            job["worker_id"] = "w-parity"
            payload = child._run(job)
            return [c.get("model_names") for c in calls], payload

        chain_manual, payload_manual = run(w_manual)
        chain_class, payload_class = run(w_class)

        # The canonical engine was the one called — for BOTH job kinds.
        assert chain_manual, "manual job never reached grade_with_model"
        assert chain_class, "classroom job never reached grade_with_model"
        assert chain_manual[0] == expected_chain
        assert chain_class[0] == expected_chain
        assert chain_manual == chain_class, (
            "manual and classroom jobs must mark with the same model chain")

        assert payload_manual["ok"] is True, payload_manual
        assert payload_class["ok"] is True, payload_class
        assert payload_manual["totals"] == payload_class["totals"]
        assert payload_manual["result"] == payload_class["result"]
        assert payload_manual["marking_model"] == payload_class["marking_model"]


class TestEndToEndParity:
    """Link 4 — real pool, real child processes, identical marks."""

    def test_manual_and_classroom_jobs_complete_with_identical_marks(
            self, parity_env, monkeypatch):
        from app.engine.service import CorrectionService
        from app.workers.pool import WorkerPool

        pdf = parity_env
        svc = CorrectionService()

        # Manual upload first (needs its pending files staged after enqueue).
        manual_job = svc.enqueue_manual_pdf(student_name="Alice")
        _stage_manual(manual_job["id"], pdf)
        auto_job = svc.enqueue_classroom_submission(
            submission_id="s1", coursework_id="w1", course_id="c1",
            student_id="u1", student_name="Bob")

        pool = WorkerPool(worker_count=2)
        pool.start()
        try:
            pool._wake.set()
            deadline = time.time() + 120
            finals: dict[str, dict] = {}
            while time.time() < deadline and len(finals) < 2:
                for job in (manual_job, auto_job):
                    row = db.query_one(
                        "SELECT * FROM correction_jobs WHERE id = ?",
                        (job["id"],))
                    if row and row["status"] in ("COMPLETED", "FAILED",
                                                 "CANCELLED"):
                        finals[job["id"]] = dict(row)
                if len(finals) < 2:
                    time.sleep(0.3)
            assert len(finals) == 2, f"jobs did not finish: {finals}"
        finally:
            pool.shutdown(drain=False)

        m_final = finals[manual_job["id"]]
        a_final = finals[auto_job["id"]]
        assert m_final["status"] == "COMPLETED", m_final
        assert a_final["status"] == "COMPLETED", a_final
        assert m_final["result_id"] and a_final["result_id"]

        def result_of(job_id: str) -> dict:
            row = db.query_one("SELECT * FROM results WHERE job_id = ?",
                               (job_id,))
            assert row, f"no result for {job_id}"
            return dict(row)

        m_res = result_of(manual_job["id"])
        a_res = result_of(auto_job["id"])
        for field in ("earned", "available", "percentage",
                      "provisional_grade", "marking_model"):
            assert m_res[field] == a_res[field], (
                f"identical input produced different {field}: "
                f"{m_res[field]!r} != {a_res[field]!r}")
        # Real marks, not placeholders.
        assert m_res["available"] and m_res["earned"] is not None
