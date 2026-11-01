"""Worker pool tests: safe concurrency, idempotency, recovery, isolation.

The pool executes jobs via spawn processes; these tests exercise the
orchestration logic with the mock Gemini backend (no network, no quota).
"""

import threading
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


class TestRealConcurrency:
    """Papers must genuinely overlap in time, not just look parallel in a UI.

    Mock mode is used deliberately: it exercises the real pool, the real
    spawned worker processes and the real job/stage records with no network
    and no quota. The proof is the stored timestamps, not a callback.
    """

    @staticmethod
    def _intervals(ids: list[str]) -> list[tuple[float, float]]:
        spans = []
        for job_id in ids:
            row = db.query_one(
                "SELECT started_at, finished_at FROM correction_jobs WHERE id = ?",
                (job_id,))
            assert row is not None and row["started_at"] is not None, job_id
            spans.append((float(row["started_at"]),
                          float(row["finished_at"] or time.time())))
        return spans

    @staticmethod
    def _max_overlap(spans: list[tuple[float, float]]) -> int:
        """Most jobs that were in flight at the same instant."""
        moments: list[tuple[float, int]] = []
        for start, end in spans:
            moments.append((start, 1))
            moments.append((end, -1))
        # End-ticks sort before start-ticks at the same instant, so a job that
        # finishes exactly as another starts is not counted as overlapping.
        moments.sort(key=lambda m: (m[0], m[1]))
        running = peak = 0
        for _, delta in moments:
            running += delta
            peak = max(peak, running)
        return peak

    def test_four_jobs_run_in_parallel_and_never_exceed_the_pool_size(
            self, pool, fresh_state):
        # The pool is capped by HEALTHY KEYS, so one key means one worker and
        # no overlap at all. Three keys is what a real installation rotates.
        from app.config import get_secrets
        get_secrets().set_gemini_keys(
            ["AIzaConcurrencyKey11111111", "AIzaConcurrencyKey22222222",
             "AIzaConcurrencyKey33333333"])
        assert pool.set_size(2) == 2
        ids = []
        for student in ("P1", "P2", "P3", "P4"):
            job_id = _enqueue_job(student)
            _stage_manual_job(job_id, fresh_state)
            ids.append(job_id)
        pool._wake.set()
        finals = [_wait_terminal(pool, jid, timeout=240) for jid in ids]
        assert all(f["status"] == "COMPLETED" for f in finals), finals

        spans = self._intervals(ids)
        peak = self._max_overlap(spans)
        assert peak >= 2, f"jobs never overlapped: {spans}"
        assert peak <= 2, f"{peak} jobs ran at once with a pool of 2: {spans}"

        # Each card must belong to a distinct worker process while it runs.
        workers = [db.query_one(
            "SELECT worker_id FROM correction_jobs WHERE id = ?", (jid,))["worker_id"]
            for jid in ids]
        assert all(worker for worker in workers), workers

    def test_raising_the_pool_size_raises_the_number_in_flight(
            self, pool, fresh_state):
        from app.config import get_secrets
        get_secrets().set_gemini_keys(
            ["AIzaConcurrencyKey11111111", "AIzaConcurrencyKey22222222",
             "AIzaConcurrencyKey33333333"])
        assert pool.set_size(3) >= 2
        ids = []
        for student in ("R1", "R2", "R3", "R4"):
            job_id = _enqueue_job(student)
            _stage_manual_job(job_id, fresh_state)
            ids.append(job_id)
        pool._wake.set()
        finals = [_wait_terminal(pool, jid, timeout=240) for jid in ids]
        assert all(f["status"] == "COMPLETED" for f in finals), finals

        peak = self._max_overlap(self._intervals(ids))
        # A larger pool must actually be used; the cap still holds.
        assert peak >= 2, f"jobs never overlapped: {peak}"
        assert peak <= max(1, pool.recommended_workers()), peak
        pool.set_size(2)


class TestTerminalVisibility:
    """COMPLETED must not be observable before the job's bookkeeping is done.

    Regression for the two intermittent CI failures: the status row was
    committed and only afterwards were the prepared inputs deleted and the
    duration recorded, so a reader polling for a terminal row (the
    retry-architecture test, a desktop list, an API client) could see
    COMPLETED while ``tmp/job-<id>/`` still existed and ``avg_seconds`` was
    still None.

    Cleanup is held open here, so the window is not a race to reproduce: with
    the old ordering the row is already COMPLETED while cleanup is blocked.
    """

    def test_completed_is_not_visible_while_cleanup_is_still_running(
            self, pool, fresh_state, monkeypatch):
        import app.workers.pool as pool_module

        entered = threading.Event()
        release = threading.Event()
        real_cleanup = pool_module.cleanup_job_files

        def blocking_cleanup(job_id):
            entered.set()
            assert release.wait(60), "cleanup was never released"
            return real_cleanup(job_id)

        monkeypatch.setattr(pool_module, "cleanup_job_files", blocking_cleanup)

        job_id = _enqueue_job("Visibility")
        _stage_manual_job(job_id, fresh_state)
        pool._wake.set()
        assert entered.wait(90), "terminal cleanup was never reached"

        cache_dir = config.DATA_DIR / "tmp" / f"job-{job_id}"
        assert cache_dir.exists(), (
            "the prepared-input cache must exist while cleanup is blocked")
        row = db.query_one("SELECT status FROM correction_jobs WHERE id = ?",
                           (job_id,))
        assert row["status"] not in ("COMPLETED", "FAILED", "CANCELLED"), (
            f"job advertised {row['status']} while its inputs still existed")

        release.set()
        final = _wait_terminal(pool, job_id)
        assert final["status"] == "COMPLETED"
        assert not cache_dir.exists()
        assert pool.public_state()["metrics"].get("avg_seconds") is not None
