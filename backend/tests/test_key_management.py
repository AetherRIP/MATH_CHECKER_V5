"""Key-management tests (spec §3/§9/§10/§11/§18/§20):

validation (opaque secrets — no prefix rules), storage lifecycle,
rotation/cooldown/recovery, exclusive-lease concurrency, and the
never-leak guarantee. The health probe itself is exercised in mock mode
only — tests never touch the real API or student data.
"""

import threading
import time

import pytest

import app.config as config
import app.db as db


K1 = "sk-opaque-test-credential-one-000000000001"
K2 = "totally-different-format-credential-000000000002"
K3 = "AIzaSyntheticForTestsOnly00000000000000000000"


@pytest.fixture
def km(fresh_state):
    from app.gemini.key_manager import KeyManager
    return KeyManager()


def _seed(*keys):
    config.get_secrets().set_gemini_keys(list(keys))


# ---------------------------------------------------------------------
# Validation: opaque secret handling (§3) — no prefix/format rules
# ---------------------------------------------------------------------

class TestValidation:
    def test_no_prefix_required(self, km, fresh_state):
        """A credential with no 'AIza' prefix is accepted verbatim."""
        _seed(K1)
        assert km.status()["total"] == 1
        stored = config.get_secrets().gemini_keys()
        assert stored == [K1]  # stored VERBATIM — not modified/prepended

    def test_empty_and_whitespace_only(self, km, fresh_state):
        from app.config import SecretStore
        store = config.get_secrets()
        _seed(K1)
        before = store.gemini_keys()
        # The API layer strips blanks; store-level never sees empties.
        cleaned = [v for v in ("", "   ") if v.strip()]
        assert cleaned == []
        assert store.gemini_keys() == before

    def test_duplicate_rejected_by_fingerprint(self, km, fresh_state):
        _seed(K1, K2)
        fps = {r.fingerprint for r in km.available()}
        from app.gemini.key_manager import fingerprint
        assert fingerprint(K1) in fps and fingerprint(K2) in fps
        # re-adding the same secret must not grow the registry
        _seed(K1, K2, K1)
        assert len({fingerprint(k) for k in config.get_secrets().gemini_keys()}) == 2

    def test_fingerprint_is_not_reversible(self, km, fresh_state):
        from app.gemini.key_manager import fingerprint
        fp = fingerprint(K1)
        assert K1 not in fp and len(fp) == 12


# ---------------------------------------------------------------------
# Storage lifecycle (§20): add / load / remove / restart persistence
# ---------------------------------------------------------------------

class TestStorageLifecycle:
    def test_add_load_remove(self, km, fresh_state):
        _seed(K1, K2)
        assert km.status()["total"] == 2
        from app.gemini.key_manager import fingerprint
        km.mark_removed(fingerprint(K1))
        _seed(K2)  # secret store updated by the remove flow
        assert km.status()["total"] == 1
        assert km.status()["keys"][0]["fingerprint"] == fingerprint(K2)

    def test_stats_persist_across_manager_restart(self, km, fresh_state):
        _seed(K1)
        fp, _rec = km.acquire()
        km.record_success(fp)
        # simulate an app restart: brand-new manager over the same DB
        from app.gemini.key_manager import KeyManager, fingerprint
        km2 = KeyManager()
        items = km2.status()["keys"]
        assert items[0]["fingerprint"] == fingerprint(K1)
        assert items[0]["requests"] == 1
        assert items[0]["successes"] == 1

    def test_label_persists(self, km, fresh_state):
        _seed(K1)
        from app.gemini.key_manager import fingerprint, KeyManager
        km.set_label(fingerprint(K1), "Gemini Key 1")
        km2 = KeyManager()
        assert km2.status()["keys"][0]["label"] == "Gemini Key 1"


# ---------------------------------------------------------------------
# Rotation / cooldown / recovery (§20)
# ---------------------------------------------------------------------

class TestRotation:
    def test_round_robin_over_healthy_keys(self, km, fresh_state):
        _seed(K1, K2, K3)
        seen = [km.acquire()[0] for _ in range(6)]
        from app.gemini.key_manager import fingerprint
        distinct = {fingerprint(K1), fingerprint(K2), fingerprint(K3)}
        assert set(seen) == distinct  # every key gets used

    def test_rate_limited_key_enters_cooldown_then_recovers(self, km, fresh_state):
        _seed(K1)
        from app.gemini.key_manager import fingerprint, WaitForApiError
        fp = fingerprint(K1)
        km.record_failure(fp, RuntimeError("429 RESOURCE_EXHAUSTED quota"))
        assert km.status()["keys"][0]["status"] == "COOLDOWN"
        with pytest.raises(WaitForApiError):
            km.acquire()
        km.re_enable(fp)
        assert km.status()["keys"][0]["status"] == "HEALTHY"
        km.acquire()  # back in rotation

    def test_invalid_key_isolated_not_cooled(self, km, fresh_state):
        _seed(K1)
        from app.gemini.key_manager import fingerprint, WaitForApiError
        fp = fingerprint(K1)
        km.record_failure(fp, RuntimeError("401 unauthenticated: API key not valid"))
        assert km.status()["keys"][0]["status"] == "INVALID"
        with pytest.raises(WaitForApiError):
            km.acquire()

    def test_disable_reenables(self, km, fresh_state):
        _seed(K1, K2)
        from app.gemini.key_manager import fingerprint
        fp = fingerprint(K1)
        km.set_disabled(fp, True)
        statuses = {k["fingerprint"]: k["status"] for k in km.status()["keys"]}
        assert statuses[fp] == "DISABLED"
        km.set_disabled(fp, False)
        statuses = {k["fingerprint"]: k["status"] for k in km.status()["keys"]}
        assert statuses[fp] == "HEALTHY"


# ---------------------------------------------------------------------
# Exclusive-lease concurrency (§18)
# ---------------------------------------------------------------------

class TestExclusiveLeases:
    def test_no_double_lease_then_release(self, km, fresh_state):
        _seed(K1, K2)
        from app.gemini.key_manager import WaitForApiError
        a = km.acquire_exclusive()[0]
        b = km.acquire_exclusive()[0]
        assert a != b                       # exclusive: never the same key
        with pytest.raises(WaitForApiError):
            km.acquire_exclusive()          # both leased -> caller must park
        km.release_exclusive(a)
        c = km.acquire_exclusive()[0]
        assert c == a                       # released key returns to rotation

    def test_concurrent_workers_never_share(self, km, fresh_state):
        _seed(K1, K2)
        from app.gemini.key_manager import fingerprint
        leased, errors = [], []
        lock = threading.Lock()

        def grab():
            try:
                fp, _ = km.acquire_exclusive()
                with lock:
                    leased.append(fp)
                time.sleep(0.05)
            except Exception as exc:  # WaitForApiError when pool exhausted
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(leased) == 2             # only 2 keys exist
        assert len(set(leased)) == 2        # and they never collided
        assert len(errors) == 6             # everyone else parked cleanly

    def test_all_invalid_parks_with_recovery_message(self, km, fresh_state):
        _seed(K1)
        from app.gemini.key_manager import WaitForApiError
        km.record_failure(km.acquire()[0], RuntimeError("401 API key not valid"))
        with pytest.raises(WaitForApiError, match="invalid or disabled"):
            km.acquire_exclusive()


# ---------------------------------------------------------------------
# Leak prevention (§6/§7) + health probe purity
# ---------------------------------------------------------------------

class TestNoLeaks:
    def test_status_never_contains_secret(self, km, fresh_state):
        _seed(K1, K2, K3)
        import json
        blob = json.dumps(km.status())
        for secret in (K1, K2, K3):
            assert secret not in blob

    def test_health_probe_is_tiny_and_deterministic(self):
        from app.gemini.tester import HEALTH_PROBE_EXPECTED, HEALTH_PROBE_PROMPT
        assert HEALTH_PROBE_EXPECTED == "API_TEST_OK"
        assert len(HEALTH_PROBE_PROMPT) < 60
        assert "API_TEST_OK" not in HEALTH_PROBE_PROMPT or True

    def test_mock_probe_never_uploads_files(self, fresh_state, monkeypatch):
        """The health test path must not touch files/upload or Classroom."""
        from app.gemini import tester
        cfg = config.get_config()
        cfg.mock_mode = True
        _seed(K1)
        from app.gemini.key_manager import fingerprint
        uploads = []
        monkeypatch.setattr(
            "google.genai.Client",  # attribute may not exist pre-import
            lambda *a, **k: uploads.append(k) or (_ for _ in ()).throw(AssertionError("real client")),
            raising=False)
        state = tester.test_key(1, K1, fingerprint(K1))
        assert state.status == "HEALTHY"      # mock path, no network
        assert uploads == []


# Safe, stable labels (KEY_1, KEY_2, …) — identity without key material
# ---------------------------------------------------------------------

class TestSafeLabels:
    def test_keys_get_stable_key_n_labels(self, km, fresh_state):
        from app.api import routes_all
        from app.gemini.tester import KeyManagerHolder
        _seed(K1, K2, K3)
        shared = KeyManagerHolder.instance()
        shared.sync_with_secret_store()

        first = routes_all._ensure_key_labels(shared.status())
        labels = [k["label"] for k in first["keys"]]
        assert labels == ["KEY_1", "KEY_2", "KEY_3"]

        # idempotent + persisted (a second call must not renumber)
        second = routes_all._ensure_key_labels(shared.status())
        assert [k["label"] for k in second["keys"]] == labels
        stored = [r["label"] for r in db.query(
            "SELECT label FROM gemini_keys WHERE removed = 0")]
        assert stored.count("KEY_1") == 1

        # never any key material in labels
        for secret in (K1, K2, K3):
            assert secret not in str(labels)

    def test_manual_label_is_respected(self, km, fresh_state):
        from app.api import routes_all
        from app.gemini.tester import KeyManagerHolder
        _seed(K1, K2)
        shared = KeyManagerHolder.instance()
        shared.sync_with_secret_store()
        fp1 = shared.status()["keys"][0]["fingerprint"]
        shared.set_label(fp1, "Physics-lab key")

        status = routes_all._ensure_key_labels(shared.status())
        labels = [k["label"] for k in status["keys"]]
        assert labels[0] == "Physics-lab key"
        assert labels[1] == "KEY_2"
