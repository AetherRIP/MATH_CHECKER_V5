"""TESTING ONLY mode: nothing may be published while the marker is audited.

The interface can hide a button; these tests prove the BACKEND refuses the
call, so a retry, a scheduled job, a background worker or a direct API request
cannot publish a grade, upload or attach a report, or return work.

Read-only access is deliberately NOT blocked — testing must still be able to
see the real Classroom submissions and mark schemes.
"""

from __future__ import annotations

import pytest

import app.config as config
from app.testing_guard import (
    PublicationBlocked,
    assert_publication_allowed,
    blocked_reason,
    is_mutating,
    state,
)


@pytest.fixture
def testing_only(fresh_state, monkeypatch):
    cfg = config.get_config()
    cfg.testing.only = True
    config.save_config(cfg)
    # The guard must block regardless of the rest of the configuration.
    cfg.classroom.enabled = True
    cfg.automation.enabled = True
    return cfg


@pytest.fixture
def testing_off(fresh_state, monkeypatch):
    cfg = config.get_config()
    cfg.testing.only = False
    config.save_config(cfg)
    return cfg


class TestGuardState:
    def test_defaults_off(self):
        assert config.AppConfig().testing.only is False

    def test_round_trips_through_config_toml(self, fresh_state):
        cfg = config.AppConfig()
        cfg.teacher.onboarded = True
        cfg.testing.only = True
        config.save_config(cfg)
        assert config.get_config(reload=True).testing.only is True
        cfg2 = config.AppConfig()
        cfg2.teacher.onboarded = True
        cfg2.testing.only = False
        config.save_config(cfg2)
        assert config.get_config(reload=True).testing.only is False

    def test_only_mutating_methods_are_guarded(self):
        assert is_mutating("POST") is True
        assert is_mutating("PATCH") is True
        assert is_mutating("DELETE") is True
        assert is_mutating("GET") is False
        assert is_mutating("HEAD") is False
        assert is_mutating("OPTIONS") is False

    def test_reason_and_state_are_explicit(self, testing_only):
        assert state().only is True
        assert "Testing only" in (blocked_reason() or "")


class TestAssertion:
    def test_write_is_blocked(self, testing_only):
        with pytest.raises(PublicationBlocked) as exc:
            assert_publication_allowed("write a draft grade")
        # The message must be actionable, not a stack trace.
        assert "TESTING ONLY" in str(exc.value)
        assert "Settings" in str(exc.value)

    def test_read_is_never_blocked(self, testing_only):
        assert_publication_allowed("list courses", method="GET")
        assert_publication_allowed("download a submission", method="HEAD")

    def test_writes_allowed_when_testing_is_off(self, testing_off):
        assert blocked_reason() is None
        assert_publication_allowed("write a draft grade")  # must not raise


class TestClassroomWritePaths:
    """Every publishing chokepoint must refuse while testing is on."""

    def test_mutating_classroom_request_is_blocked_before_it_is_sent(
            self, testing_only, monkeypatch):
        from app.classroom import client as gc

        sent: list[str] = []
        monkeypatch.setattr(gc.requests, "request",
                            lambda *a, **k: sent.append("sent"))
        with pytest.raises(PublicationBlocked):
            gc.classroom_request("PATCH", f"{gc.CLASSROOM_BASE}/courses/1", "tok",
                                 json={"draftGrade": 9})
        assert sent == [], "no HTTP request may reach Google in testing mode"

    def test_readonly_classroom_request_still_works(self, testing_only,
                                                    monkeypatch):
        from app.classroom import client as gc

        class _Resp:
            status_code = 200
            content = b'{"courses": []}'

            @staticmethod
            def json():
                return {"courses": []}

        monkeypatch.setattr(gc.requests, "request", lambda *a, **k: _Resp())
        assert gc.classroom_request("GET", f"{gc.CLASSROOM_BASE}/courses",
                                    "tok") == {"courses": []}

    @pytest.mark.parametrize("func,args", [
        ("upload_report_to_drive", ("tok", "report.pdf", b"%PDF-1.4")),
        ("replace_drive_file_content", ("tok", "file-id", b"%PDF-1.4")),
        ("share_drive_file", ("tok", "file-id", "student@example.com")),
        ("delete_drive_file", ("tok", "file-id")),
        ("modify_submission_attachments", ("tok", "c", "w", "s")),
        ("patch_draft_grade", ("tok", "c", "w", "s", 9)),
    ])
    def test_drive_and_grade_helpers_are_blocked(self, testing_only, func, args):
        from app.classroom import client as gc

        with pytest.raises(PublicationBlocked):
            getattr(gc, func)(*args)

        # ...and the same helpers work again once testing is switched off.
        config.get_config().testing.only = False
        assert blocked_reason() is None


class TestTestingModeIsSwitchableFromSettings:
    """The block message says "turn it off in Settings" — so that must work.

    A guard whose escape hatch does not exist sends the teacher to a screen
    with no switch and no way to publish: the block becomes permanent and the
    message becomes a lie. These tests pin the switch to the SAME state the
    guard reads, so the two can never disagree.
    """

    @pytest.fixture
    def api_client(self):
        import os

        os.environ.setdefault("MC5_LOGIN_REQUIRED", "0")
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            yield client

    def test_settings_reports_the_real_state(self, api_client, testing_off):
        body = api_client.get("/api/settings").json()
        assert body["testing"]["only"] is False
        assert body["testing"]["reason"] == ""

    def test_settings_reports_on_with_the_reason(self, api_client, testing_only):
        body = api_client.get("/api/settings").json()
        assert body["testing"]["only"] is True
        assert "Testing only" in body["testing"]["reason"]

    def test_switching_it_off_unblocks_the_very_next_google_write(
            self, api_client, testing_only):
        from app.classroom import client as gc

        with pytest.raises(PublicationBlocked):
            gc.upload_report_to_drive("tok", "report.pdf", b"%PDF-1.4")

        response = api_client.post("/api/settings",
                                   json={"testing": {"only": False}})
        assert response.status_code == 200, response.text
        assert response.json()["testing"]["only"] is False

        # No restart, and it survives a reload from config.toml.
        assert blocked_reason() is None
        assert config.get_config(reload=True).testing.only is False
        assert state().only is False

    def test_switching_it_on_blocks_again(self, api_client, testing_off):
        response = api_client.post("/api/settings",
                                   json={"testing": {"only": True}})
        assert response.status_code == 200
        assert blocked_reason() is not None
        from app.classroom import client as gc
        with pytest.raises(PublicationBlocked):
            gc.patch_draft_grade("tok", "c", "w", "s", 9)

    def test_a_payload_that_does_not_mention_testing_leaves_it_alone(
            self, api_client, testing_only):
        assert api_client.post("/api/settings",
                               json={"debug": True}).status_code == 200
        assert state().only is True

    def test_a_malformed_testing_payload_is_ignored_not_fatal(
            self, api_client, testing_off):
        response = api_client.post("/api/settings", json={"testing": "yes"})
        assert response.status_code == 200
        assert state().only is False


class TestAuditApiIsReadOnlyAndPrivate:
    def test_summary_endpoint_is_registered(self):
        import os

        os.environ.setdefault("MC5_LOGIN_REQUIRED", "0")
        from app.main import app

        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/api/audit/summary" in paths
        assert "/api/audit/reports" in paths
        assert "/api/audit/reports/{name}" in paths

    def test_report_endpoint_refuses_path_traversal(self, fresh_state):
        import os

        os.environ.setdefault("MC5_LOGIN_REQUIRED", "0")
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        for bad in ("..%2Fapp.db", ".hidden.pdf", "sub%2Fdir.pdf"):
            response = client.get(f"/api/audit/reports/{bad}")
            # Either the auth gate or the name check refuses it — never a file.
            assert response.status_code in (400, 401, 403, 404)
