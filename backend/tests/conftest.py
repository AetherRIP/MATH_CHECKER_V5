import os
import sys
import tempfile
from pathlib import Path

import pytest

# Every test gets an isolated data dir before app modules are imported.
_TMP = tempfile.mkdtemp(prefix="mc5_test_")
os.environ["MC5_DATA_DIR"] = _TMP

# The production default requires a sign-in session for local use too (the
# main login page). The general API suite runs in the documented no-login
# local mode so tests exercise features instead of the gate; the gate itself
# is covered by the tests that set MC5_LOGIN_REQUIRED=1 and by
# test_public_access_security.py (public mode).
os.environ.setdefault("MC5_LOGIN_REQUIRED", "0")

# Multi-account mode now DEFAULTS OFF (the shipped single-account mode), so the
# suites that exercise the accounts/roles/teams implementation run with the
# layer explicitly ON — exactly as the no-login mode above works. The disabled
# behaviour itself (default OFF, refused operations, re-enable without data
# loss) is covered by the tests that control this variable themselves
# (test_multi_account_mode.py).
os.environ.setdefault("MULTI_ACCOUNT_ENABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    """Fresh config + DB + singletons per test (no cross-test leakage)."""
    import app.config as config
    import app.db as db

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "SECRETS_PATH", data_dir / "secrets.toml")
    monkeypatch.setattr(config, "CONFIG_PATH", data_dir / "config.toml")
    monkeypatch.setattr(db, "DB_PATH", data_dir / "app.db")

    # reset config/secret singletons to stores bound to THIS test's paths
    monkeypatch.setattr(config, "_config", None)
    monkeypatch.setattr(config, "_secrets", config.SecretStore(data_dir / "secrets.toml"))

    # drop cached per-thread DB connections (they point at the old file)
    if getattr(db._local, "conn", None) is not None:
        try:
            db._local.conn.close()
        except Exception:
            pass
        db._local.conn = None

    # reset job + key-manager + worker-pool singletons
    import app.jobs.manager as jm
    monkeypatch.setattr(jm, "_manager", None)
    import app.gemini.tester as tester
    monkeypatch.setattr(tester.KeyManagerHolder, "_instance", None)
    from app.workers.pool import WorkerPoolHolder
    WorkerPoolHolder.reset()

    # login throttling is process-global in-memory state; clear it so one test
    # cannot rate-limit another (every TestClient shares the same client host).
    from app import auth_gate as _ag
    _ag.reset_throttle()

    data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db()
    yield data_dir
