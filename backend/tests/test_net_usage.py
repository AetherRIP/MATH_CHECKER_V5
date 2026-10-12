"""Internet usage accounting + rolling-limit tests (network budget §8).

Covers: hour-bucket accounting and accumulation, rolling limits and
unlimited mode, the pool dispatch gate, Classroom/Gemini probe/child-proxy
instrumentation, and settings round-trip for the limits.
"""

import time

import pytest

import app.config as config
import app.db as db


def _record(cat, up=0, down=0, ts=None):
    from app.net_usage import record
    record(cat, up, down, ts=ts)


@pytest.fixture(autouse=True)
def usage_cfg(fresh_state, monkeypatch):
    cfg = config.get_config()
    cfg.usage.hourly_mb = 10   # 10 MB for easy math
    cfg.usage.daily_mb = 100
    config.save_config(cfg)
    yield cfg


class TestAccounting:
    def test_record_accumulates_per_category(self, fresh_state):
        _record("gemini_upload", 1000)
        _record("gemini_upload", 500)
        _record("classroom_api", 100, 300)
        state = __import__("app.net_usage", fromlist=["usage_state"]).usage_state()
        assert state["hour"]["by_category"]["gemini_upload"]["up"] == 1500
        assert state["hour"]["by_category"]["classroom_api"]["down"] == 300
        assert state["hour"]["total"] == 1500 + 100 + 300

    def test_unknown_category_lands_in_other(self, fresh_state):
        _record("mystery", 123)
        state = __import__("app.net_usage", fromlist=["usage_state"]).usage_state()
        assert state["hour"]["by_category"]["other"]["up"] == 123

    def test_day_window_spans_hour_buckets(self, fresh_state):
        # The day window is a UTC *calendar* day, so pin `now` to 06:00 UTC to
        # keep "2 hours ago" inside the same day. Using the real clock made
        # this assertion fail in the two hours after UTC midnight.
        now = (int(time.time()) // 86400) * 86400 + 6 * 3600
        _record("gemini_upload", 1_000_000, ts=now)
        _record("gemini_upload", 1_000_000, ts=now - 7200)  # 2 hours ago
        state = __import__("app.net_usage", fromlist=["usage_state"]).usage_state(now_ts=now)
        assert state["day"]["total"] == 2_000_000
        assert state["hour"]["total"] == 1_000_000

    def test_history_includes_zero_hours(self, fresh_state):
        now = time.time()
        _record("gemini_upload", 42, ts=now - 3600)  # previous hour bucket
        from app.net_usage import history
        hist = history(hours=3, now_ts=now)
        assert len(hist) == 3
        # hist[0] = now-2h (empty), hist[1] = previous hour (has the record),
        # hist[2] = current hour (empty).
        assert hist[0]["up"] == 0
        assert hist[1]["up"] == 42
        assert hist[2]["up"] == 0

    def test_record_never_raises(self, fresh_state, monkeypatch):
        from app import net_usage
        monkeypatch.setattr(db, "execute",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db busy")))
        net_usage.record("gemini_upload", 100)  # must not raise
        monkeypatch.undo()


class TestLimits:
    def test_hourly_and_daily_over(self, fresh_state):
        from app.net_usage import daily_over, hourly_over
        now = time.time()
        _record("gemini_upload", 10 * 1024 * 1024 + 1, ts=now)  # just over 10 MB
        assert hourly_over(now_ts=now) is True
        assert daily_over(now_ts=now) is False                  # daily cap is 100 MB
        _record("gemini_upload", 100 * 1024 * 1024, ts=now)     # now past daily too
        assert daily_over(now_ts=now) is True

    def test_zero_limit_means_unlimited(self, fresh_state):
        from app.net_usage import daily_over, hourly_over
        cfg = config.get_config()
        cfg.usage.hourly_mb = 0
        cfg.usage.daily_mb = 0
        config.save_config(cfg)
        _record("gemini_upload", 500 * 1024 * 1024)
        assert hourly_over() is False
        assert daily_over() is False


class TestSettingsRoundTrip:
    def test_usage_limits_in_settings_api(self, fresh_state):
        from fastapi.testclient import TestClient
        config.get_config().mock_mode = True
        config.get_secrets().set_gemini_keys(["AIzaUsageTestKey1234567890"])
        from app.main import app
        with TestClient(app) as client:
            data = client.post("/api/settings",
                               json={"usage": {"hourly_mb": 777, "daily_mb": 8888}}).json()
            assert data["usage"] == {"hourly_mb": 777, "daily_mb": 8888}
            got = client.get("/api/settings").json()["usage"]
            assert got == {"hourly_mb": 777, "daily_mb": 8888}
            bad = client.post("/api/settings", json={"usage": {"hourly_mb": "lots"}})
            assert bad.status_code == 400

    def test_usage_endpoint_shape(self, fresh_state):
        from fastapi.testclient import TestClient
        from app.main import app
        _record("gemini_upload", 2048)
        with TestClient(app) as client:
            data = client.get("/api/usage?hours=6").json()
            assert data["hour"]["by_category"]["gemini_upload"]["up"] == 2048
            assert len(data["history"]) == 6
            assert set(data["limits"]) >= {"hourly_mb", "daily_mb"}


class TestInstrumentation:
    def test_classroom_request_records(self, fresh_state, monkeypatch):
        from app.classroom import client as cc
        class FakeResponse:
            status_code = 200
            content = b'{"courses": [1, 2, 3]}'
            text = "{}"
            def json(self): return {"courses": []}
        monkeypatch.setattr(cc.requests, "request", lambda *a, **k: FakeResponse())
        cc.classroom_request("GET", f"{cc.CLASSROOM_BASE}/courses", "tok")
        state = __import__("app.net_usage", fromlist=["usage_state"]).usage_state()
        assert state["hour"]["by_category"]["classroom_api"]["down"] == len(FakeResponse.content)

    def test_drive_download_records_exact_bytes(self, fresh_state, monkeypatch):
        from app.classroom import client as cc
        meta = {"name": "hw.pdf", "mimeType": "application/pdf",
                "capabilities": {"canDownload": True}}
        blob = b"%PDF-1.4 attachment-bytes" * 10
        monkeypatch.setattr(cc, "classroom_request", lambda *a, **k: meta)
        monkeypatch.setattr(cc.requests, "get", lambda *a, **k: type(
            "R", (), {"status_code": 200, "content": blob, "text": ""})())
        name, data, mime = cc.download_drive_file("tok", "file123")
        assert data == blob
        state = __import__("app.net_usage", fromlist=["usage_state"]).usage_state()
        assert state["hour"]["by_category"]["classroom_download"]["down"] == len(blob)

    def test_child_proxy_records_generation_and_uploads(self, fresh_state, tmp_path):
        from app.workers.child import _UsageRecordingClient

        class FakeModels:
            def generate_content(self, **kw):
                return type("R", (), {"text": "x" * 500})()

        class FakeFiles:
            def upload(self, file=None, **kw):
                return type("Remote", (), {"name": "files/x"})()

            def get(self, name=None): return type("Remote", (), {"name": name})()

            def delete(self, name=None): return None

        class FakeInner:
            def __init__(self):
                self.models = FakeModels()
                self.files = FakeFiles()

        pdf = tmp_path / "submission.pdf"
        pdf.write_bytes(b"%PDF-1.4" * 1000)  # 8000 bytes

        wrapped = _UsageRecordingClient(FakeInner())
        wrapped.models.generate_content(model="m", contents="hi")
        wrapped.files.upload(file=str(pdf))
        wrapped.files.delete(name="files/x")

        state = __import__("app.net_usage", fromlist=["usage_state"]).usage_state()
        gen = state["hour"]["by_category"]["gemini_generate"]
        up = state["hour"]["by_category"]["gemini_upload"]
        assert gen["down"] == 500
        assert gen["up"] > 0                 # estimated request overhead
        assert up["up"] == pdf.stat().st_size


class TestPoolGate:
    def test_usage_limit_blocks_new_dispatch(self, fresh_state):
        cfg = config.get_config()
        cfg.mock_mode = True
        cfg.worker.concurrency = 1
        config.save_config(cfg)
        config.get_secrets().set_gemini_keys(["AIzaUsageGateKey1234567890"])

        from app.net_usage import record
        record("gemini_generate", 20 * 1024 * 1024)  # blow past the 10 MB cap

        from app.workers.pool import WorkerPool
        pool = WorkerPool(worker_count=1)
        pool.start()
        try:
            job_id = f"job-gate-{time.time_ns()}"
            db.execute(
                "INSERT INTO correction_jobs(id, submission_id, coursework_id, course_id, "
                "student_id, student_name, status, created_at, updated_at, max_attempts) "
                "VALUES(?, NULL, NULL, NULL, NULL, 'Gated', 'QUEUED', ?, ?, 3)",
                (job_id, time.time(), time.time()))
            pool._wake.set()
            deadline = time.time() + 4
            status = None
            while time.time() < deadline:
                row = db.query_one("SELECT status FROM correction_jobs WHERE id = ?", (job_id,))
                status = row["status"]
                if status != "QUEUED":
                    break
                time.sleep(0.2)
            assert status == "QUEUED"          # never claimed while over budget
            assert pool.public_state()["usage_blocked"] is True
        finally:
            pool.shutdown(drain=False)

    def test_gate_opens_when_budget_returns(self, fresh_state):
        cfg = config.get_config()
        cfg.mock_mode = True
        config.save_config(cfg)
        config.get_secrets().set_gemini_keys(["AIzaUsageGateKey1234567890"])
        from app.workers.pool import WorkerPool
        pool = WorkerPool(worker_count=1)
        pool.start()
        try:
            assert pool.public_state()["usage_blocked"] is False
        finally:
            pool.shutdown(drain=False)
