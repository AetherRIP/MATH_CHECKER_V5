import os
import sys
import warnings
from pathlib import Path

import pytest

# ``tempdirs`` lives next to this file. It owns every temp directory the suite
# creates, so a run does not leave scratch trees in %TEMP% (it is imported by
# path because the tests directory is not a package).
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
import tempdirs  # noqa: E402  (after the path setup it needs)

# Every test gets an isolated data dir before app modules are imported. It is
# REGISTERED, not just created: ``pytest_sessionfinish`` below deletes it, and on
# Windows deleting works only after the log/database handles inside it are closed
# (see tempdirs.remove - deleting first is the bug this replaced).
_TMP = tempdirs.make("mc5_test_")
os.environ["MC5_DATA_DIR"] = str(_TMP)

# The production default requires a sign-in session for local use too (the
# main login page). The general API suite runs in the documented no-login
# local mode so tests exercise features instead of the gate; the gate itself
# is covered by the tests that set MC5_LOGIN_REQUIRED=1 and by
# test_public_access_security.py (public mode).
os.environ.setdefault("MC5_LOGIN_REQUIRED", "0")

# The batch preflight asks Gemini one tiny question before a multi-paper start.
# The suite must never touch the network, so it is switched off here; the
# preflight module itself is covered by tests that mock the provider client.
os.environ.setdefault("MC5_DISABLE_PREFLIGHT", "1")

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

    # The adaptive concurrency controller is process-global state as well. A test
    # that simulates a 429 halves it, and without this the NEXT test's pool would
    # inherit that reduction and refuse to run the parallelism it asked for —
    # which is exactly how one test's simulated outage became another test's
    # "flake". A fresh controller per test keeps the assertion honest.
    from app.workers import concurrency as _concurrency
    monkeypatch.setattr(_concurrency, "_control",
                        _concurrency.ConcurrencyControl())

    # The per-correction request ledger is process-global too, and unlike the
    # other singletons it is NOT recreated per job inside a test process: a test
    # that calls ``call_audit.begin(...)`` leaves its job id and its send count
    # behind, and every provider call the NEXT test makes is then charged to
    # that finished correction. Once the account reached 12 the ledger started
    # raising ProviderBudgetExceeded inside unrelated tests — an order-dependent
    # failure with nothing wrong in the code under test. Each test now starts
    # with no correction in flight.
    from app.gemini import call_audit as _call_audit
    _call_audit.reset()

    # The provider circuit breaker is process-global for the same reason: a test
    # that simulates a 503 benches the model for every later test in the run, so
    # a following test would find the model "cooling down" and send nothing.
    from app.gemini import provider_health as _health
    monkeypatch.setattr(_health, "_provider", _health.ProviderHealth())

    # login throttling is process-global in-memory state; clear it so one test
    # cannot rate-limit another (every TestClient shares the same client host).
    from app import auth_gate as _ag
    _ag.reset_throttle()

    # The tiny-agent ON/OFF state file is written under
    # ``agent_control.agent_dir()``, which defaults to the REPOSITORY's
    # ``agent/`` folder — the same folder the standalone ``agent.py`` and its
    # ``device_id`` live in. Any test that exercised ``/api/agent/state`` or
    # ``/api/distributed/agent/state`` therefore rewrote the tracked file
    # ``agent/agent_state.json`` with a test account id, whichever data dir the
    # test used: the suite was not hermetic and it showed up as a change to a
    # file the developer never edited. ``MC_AGENT_DIR`` is the module's own
    # override, so pointing it inside this test's temp dir confines the write.
    # A test that needs its own agent directory sets the variable again itself
    # and still wins (its monkeypatch is applied after this fixture).
    monkeypatch.setenv("MC_AGENT_DIR", str(data_dir / "agent"))

    data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db()
    yield data_dir


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest hook signature
    """Delete this session's temp data directories and REPORT any that survive.

    A silent cleanup is how the previous thousand-folder leak went unnoticed: the
    run must fail loudly (a warning with the still-open file named) rather than
    pretend the directory is gone.
    """
    failures = tempdirs.remove_all()
    if not failures:
        return
    lines = ["test temp directories could not be deleted (a file was still open):"]
    for path, leftovers in failures.items():
        lines.append(f"  {path}  <- {', '.join(leftovers)}")
    message = "\n".join(lines)
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("WARNING: " + message, yellow=True)
    else:  # pragma: no cover - only when the terminal reporter is disabled
        warnings.warn(message)
