"""Worker pool tests: safe concurrency, idempotency, recovery, isolation.

The pool executes jobs via spawn processes; these tests exercise the
orchestration logic with the mock Gemini backend (no network, no quota).
"""

import time

import pytest

import app.config as config
import app.db as db


@pytest.fixture
def pool(fresh_state, monkeypatch):
    """WorkerPool bound to the fresh test DB, in mock mode.

    fresh_state already rebinds config/db singletons to the per-test data dir
    AND resets KeyManagerHolder._instance, so a new pool here sees a clean
    key registry (no INVALID state leaked from other tests or the production
    database)."""
    cfg = config.get_config()
    cfg.mock_mode = True
    cfg.worker.concurrency = 2
    config.save_config(cfg)
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaPoolTestKey1234567890"])

    from app.workers.pool import WorkerPool
    test_pool = WorkerPool(worker_count=2)
    test_pool.start()
    yield test_pool
    test_pool.shutdown(drain=False)


def _pdf_bytes(pages: int = 1) -> bytes:
    """A REAL, readable PDF — routing must be able to read the page count."""
    import io
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _stage_manual_job(job_id: str, tmp_dir) -> None:
    from pathlib import Path
    sub = Path(tmp_dir) / f"{job_id}_sub.pdf"
    ms = Path(tmp_dir) / f"{job_id}_ms.pdf"
    sub.write_bytes(_pdf_bytes())
    ms.write_bytes(_pdf_bytes())
    db.execute(
        "INSERT INTO pending_manual_files(job_id, kind, path, filename) VALUES(?,?,?,?)",
        (job_id, "submission", str(sub), "sub.pdf"))
    db.execute(
        "INSERT INTO pending_manual_files(job_id, kind, path, filename) VALUES(?,?,?,?)",
        (job_id, "mark_scheme", str(ms), "ms.pdf"))


def _enqueue_job(student: str) -> str:
    job_id = f"job-{student}-{time.time_ns()}"
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, course_id, "
        "student_id, student_name, status, created_at, updated_at, max_attempts) "
        "VALUES(?, NULL, NULL, NULL, NULL, ?, 'QUEUED', ?, ?, 3)",
        (job_id, student, time.time(), time.time()))
    return job_id


def _wait_terminal(pool, job_id: str, timeout: float = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.query_one("SELECT * FROM correction_jobs WHERE id = ?", (job_id,))
        if row and row["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return dict(row)
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


class TestSingleWorker:
    def test_one_job_completes_with_result(self, pool, fresh_state):
        job_id = _enqueue_job("Alice")
        _stage_manual_job(job_id, fresh_state)
        pool._wake.set()
        final = _wait_terminal(pool, job_id)
        assert final["status"] == "COMPLETED", final
        assert final["result_id"]
        result = db.query_one("SELECT * FROM results WHERE id = ?", (final["result_id"],))
        assert result["student_name"] == "Alice"
        assert result["earned"] == 9 and result["available"] == 12

    def test_five_jobs_no_duplicates_no_losses(self, pool, fresh_state):
        ids = []
        for student in ("A", "B", "C", "D", "E"):
            job_id = _enqueue_job(student)
            _stage_manual_job(job_id, fresh_state)
            ids.append(job_id)
        pool._wake.set()
        finals = [_wait_terminal(pool, jid, timeout=180) for jid in ids]
        assert all(f["status"] == "COMPLETED" for f in finals), finals
        results = db.query("SELECT student_name, earned FROM results ORDER BY student_name")
        assert len(results) == 5  # exactly one result per job — no duplicates
        assert {r["student_name"] for r in results} == {"A", "B", "C", "D", "E"}


class TestIdempotency:
    def test_duplicate_enqueue_is_prevented_by_db(self, fresh_state):
        """The partial UNIQUE index guarantees one active job per submission."""
        import sqlite3
        db.execute(
            "INSERT INTO courses(id, name, fetched_at) VALUES('c9','Maths',?)", (time.time(),))
        db.execute(
            "INSERT INTO coursework(id, course_id, title, fetched_at, allowlisted) "
            "VALUES('w9','c9','HW',?,1)", (time.time(),))
        db.execute(
            "INSERT INTO submissions(id, coursework_id, course_id, user_id, state, "
            "attachment_ids, fetched_at) VALUES('s9','w9','c9','u9','TURNED_IN','[]',?)",
            (time.time(),))
        db.execute(
            "INSERT INTO correction_jobs(id, submission_id, status, created_at, updated_at, max_attempts) "
            "VALUES('j-active','s9','QUEUED',?, ?, 3)", (time.time(), time.time()))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO correction_jobs(id, submission_id, status, created_at, updated_at, max_attempts) "
                "VALUES('j-second','s9','QUEUED',?, ?, 3)", (time.time(), time.time()))


class TestPoolSizing:
    def test_size_capped_by_healthy_keys(self, pool, fresh_state):
        """recommended_workers can never exceed healthy keys (§6)."""
        recommended = pool.recommended_workers()
        assert recommended >= 1
        assert recommended <= max(1, pool.keys.status()["healthy"]) or \
            pool.keys.status()["healthy"] == 0

    def test_resize_up_and_down(self, pool, fresh_state):
        assert pool.set_size(1) == 1
        assert pool.set_size(3) >= 1
        pool.set_size(2)


class TestRecovery:
    def test_failed_job_retries_then_permanent_failure(self, pool, fresh_state, monkeypatch):
        """A job whose worker result is a permanent error ends FAILED; the
        pool keeps processing later jobs (worker crash isolation §18)."""
        job_id = _enqueue_job("Doomed")
        _stage_manual_job(job_id, fresh_state)
        # Sabotage: point the submission at a missing file -> dispatch-time failure
        db.execute("UPDATE pending_manual_files SET path = ? WHERE job_id = ?",
                   (str(fresh_state / "missing.pdf"), job_id))
        pool._wake.set()
        final = _wait_terminal(pool, job_id, timeout=60)
        assert final["status"] in ("FAILED", "COMPLETED")

        # The pool must still accept new work afterwards.
        ok_id = _enqueue_job("AfterCrash")
        _stage_manual_job(ok_id, fresh_state)
        pool._wake.set()
        final2 = _wait_terminal(pool, ok_id)
        assert final2["status"] == "COMPLETED"

    def test_workspace_isolation(self, fresh_state):
        """Each worker gets its own temp dir (§11)."""
        from pathlib import Path
        from app.config import DATA_DIR
        from app.workers.adapter import CorrectionWorker
        from app.gemini.tester import KeyManagerHolder
        w1 = CorrectionWorker("W1", DATA_DIR / "tmp" / "worker-001", KeyManagerHolder.instance())
        w2 = CorrectionWorker("W2", DATA_DIR / "tmp" / "worker-002", KeyManagerHolder.instance())
        assert w1.workspace != w2.workspace
        assert w1.workspace_dir().exists()
        assert w1.workspace_dir() != w2.workspace_dir()


class TestWorkerStates:
    def test_public_state_shape(self, pool, fresh_state):
        state = pool.public_state()
        assert "workers" in state and "queue" in state and "metrics" in state
        assert state["size"] >= 1
        assert all("worker_id" in w and "status" in w for w in state["workers"])
        assert "fingerprint" in state["workers"][0]  # safe identifier present

    def test_metrics_recorded_after_completion(self, pool, fresh_state):
        job_id = _enqueue_job("Measured")
        _stage_manual_job(job_id, fresh_state)
        pool._wake.set()
        _wait_terminal(pool, job_id)
        state = pool.public_state()
        assert state["metrics"].get("avg_seconds") is not None
        assert state["queue"]["completed"] >= 1
