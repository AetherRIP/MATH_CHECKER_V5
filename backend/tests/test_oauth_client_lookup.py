"""The OAuth-client lookup must not walk the install tree on every request.

Measured defect (audit, live backend): ``/api/dashboard`` took ~156 ms per call
under no load and ~1 s with six concurrent clients, because each request ran
``resolve_oauth_client_path()`` three times and each call recursively walked the
project tree (3075 ``os.scandir`` + 51 810 ``pathlib.match`` calls) looking for
a teacher-placed ``client_secret_*.json``. The dashboard is polled every 10-15 s
by several UI pages, so the cost was continuous.

The guard that was supposed to make this cheap (``_OAUTH_SEARCH_SKIP``) skips
``node_modules``/``.venv`` but not the bundled portable Python runtime the
release ships (thousands of directories), so the walk's cost grows with the
install — exactly the shape of thing that must be bounded by design, not by
luck.

Behaviour that must NOT change: the canonical ``data/secrets/google_oauth_client.json``
still wins; a teacher-placed file is still auto-detected; monkeypatching
``config.OAUTH_CLIENT_PATH`` is still honoured per call (the suite and the
setup wizard both rely on it).
"""

from __future__ import annotations

import pytest

import app.config as config


@pytest.fixture
def scan_counter(monkeypatch):
    """Count project-tree scans and control what they find."""
    state = {"scans": 0, "found": None}

    def fake_iter(prefix):
        state["scans"] += 1
        if state["found"] is not None:
            yield state["found"]

    monkeypatch.setattr(config, "_iter_project_json_files", fake_iter)
    monkeypatch.setattr(config, "_oauth_scan_cache", {})
    return state


@pytest.fixture
def no_canonical(monkeypatch, tmp_path):
    """No canonical OAuth client yet -> the resolver must go looking."""
    path = tmp_path / "data" / "secrets" / "google_oauth_client.json"
    monkeypatch.setattr(config, "OAUTH_CLIENT_PATH", path)
    return path


class TestLookupIsBounded:
    def test_repeated_lookups_do_not_re_walk_the_project_tree(
            self, scan_counter, no_canonical):
        """10 lookups (one dashboard request's worth, several times over) must
        not mean 10 recursive walks of the install."""
        for _ in range(10):
            config.resolve_oauth_client_path()
        assert scan_counter["scans"] <= 2, (
            f"{scan_counter['scans']} project-tree scans for 10 lookups: the "
            "resolver is re-walking the install on every call")

    def test_a_real_client_config_pays_for_at_most_one_walk(
            self, scan_counter, no_canonical, tmp_path):
        """The path a client_configured()/auth_state() request takes."""
        found = tmp_path / "client_secret_teacher.json"
        found.write_text('{"installed": {"client_id": "x"}}', encoding="utf-8")
        scan_counter["found"] = found

        resolved = [config.resolve_oauth_client_path() for _ in range(6)]
        assert all(p == found for p in resolved)
        assert scan_counter["scans"] <= 2, scan_counter["scans"]

    def test_the_canonical_path_needs_no_walk_at_all(
            self, scan_counter, monkeypatch, tmp_path):
        canonical = tmp_path / "google_oauth_client.json"
        canonical.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config, "OAUTH_CLIENT_PATH", canonical)

        for _ in range(5):
            assert config.resolve_oauth_client_path() == canonical
        assert scan_counter["scans"] == 0


class TestConcurrentLookupsDoNotStampede:
    def test_simultaneous_cold_lookups_walk_the_tree_once(
            self, monkeypatch, no_canonical, tmp_path):
        """Six pages polling on the same refresh tick must not each walk.

        Measured before the walk was serialized: p95 691 ms for six concurrent
        dashboard clients on the refresh tick, against 45 ms warm.
        """
        import threading
        import time as _time

        scans = {"n": 0}
        lock = threading.Lock()

        def slow_iter(prefix):
            with lock:
                scans["n"] += 1
            _time.sleep(0.05)          # a walk that takes real time
            return iter(())

        monkeypatch.setattr(config, "_iter_project_json_files", slow_iter)
        monkeypatch.setattr(config, "_oauth_scan_cache", {})

        started = threading.Barrier(8)

        def look_up():
            started.wait(timeout=10)
            config.resolve_oauth_client_path()

        threads = [threading.Thread(target=look_up) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert scans["n"] == 1, (
            f"{scans['n']} simultaneous walks: the cold-cache stampede is back")


class TestLookupStaysCorrect:
    def test_a_client_json_placed_later_is_still_found(
            self, scan_counter, no_canonical, monkeypatch, tmp_path):
        """The cache must expire: dropping the file in later still works."""
        assert config.resolve_oauth_client_path() == no_canonical  # nothing found

        found = tmp_path / "client_secret_later.json"
        found.write_text('{"installed": {"client_id": "x"}}', encoding="utf-8")
        scan_counter["found"] = found
        monkeypatch.setattr(config, "_OAUTH_SCAN_TTL_SECONDS", 0.0)  # window elapsed

        assert config.resolve_oauth_client_path() == found

    def test_a_cached_path_that_disappeared_is_not_returned(
            self, scan_counter, no_canonical, monkeypatch, tmp_path):
        found = tmp_path / "client_secret_gone.json"
        found.write_text("{}", encoding="utf-8")
        scan_counter["found"] = found
        assert config.resolve_oauth_client_path() == found

        found.unlink()
        monkeypatch.setattr(config, "_OAUTH_SCAN_TTL_SECONDS", 0.0)
        got = config.resolve_oauth_client_path()
        assert got == no_canonical, "a deleted client JSON must not be reported as present"

    def test_a_new_data_directory_does_not_reuse_the_old_answer(
            self, scan_counter, no_canonical, monkeypatch, tmp_path):
        """Frozen at import time would break tests, which rebind DATA_DIR."""
        config.resolve_oauth_client_path()
        before = scan_counter["scans"]
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "other-data")
        config.resolve_oauth_client_path()
        assert scan_counter["scans"] > before, (
            "the cached answer must be keyed on the data directory")

    def test_monkeypatched_canonical_path_is_honoured_per_call(
            self, scan_counter, monkeypatch, tmp_path):
        """The documented contract: OAUTH_CLIENT_PATH is read at call time."""
        first = tmp_path / "a.json"
        first.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config, "OAUTH_CLIENT_PATH", first)
        assert config.resolve_oauth_client_path() == first

        second = tmp_path / "b.json"
        second.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config, "OAUTH_CLIENT_PATH", second)
        assert config.resolve_oauth_client_path() == second
