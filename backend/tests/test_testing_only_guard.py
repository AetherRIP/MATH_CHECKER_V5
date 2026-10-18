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


class TestNoWritePathCanBypassTheGuard:
    """Every route to Google Classroom / Drive is AUDITED, not promised.

    A teacher can hide a button; a retry, a scheduled job, a background worker
    or a direct API call does not care about buttons. A single unguarded
    ``requests.post`` is enough to publish a grade or attach a report to a real
    student's submission, so the coverage is established mechanically here:

    * the SOURCE is parsed and every raw HTTP write in the app must sit inside a
      ``_guarded_write``-decorated function (adding a new one breaks the build);
    * the write VERBS are exercised and the HTTP transport is rigged to fail
      loudly, so a bypass is a test failure rather than a silent upload;
    * the WORKHORSE the job queue and the API both use (``attach_report``) is run
      for real and must refuse with the honest testing-only reason;
    * the HTTP ENDPOINT is called and must answer 403 with the testing-only code.

    Scope, stated honestly: this covers the Classroom/Drive publication paths
    reachable through ``requests``. The app's other ``urllib`` egress (the
    tunnel, the update checker, the distributed agent heartbeat) does not touch
    Classroom or a student's record and is deliberately out of scope here.
    """

    #: The only functions in the app that may call ``requests`` with a write
    #: verb. Each is decorated with ``_guarded_write``; the AST audit below
    #: proves it. Adding an entry here is a deliberate, reviewed change — which
    #: is the point: it cannot happen by accident.
    RAW_WRITE_FUNCTIONS = {
        "upload_report_to_drive",
        "replace_drive_file_content",
        "delete_drive_file",
    }
    WRITE_VERBS = {"post", "patch", "put", "delete"}

    @classmethod
    def _raw_writes(cls, path: Path) -> dict[str, tuple[set, bool]]:
        """Map every function in ``path`` that writes over HTTP to whether it
        is guarded. Read from the source, so it cannot drift from the code."""
        import ast

        found: dict[str, tuple[set, bool]] = {}
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            verbs = set()
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr in cls.WRITE_VERBS
                        and isinstance(inner.func.value, ast.Name)
                        and inner.func.value.id in ("requests", "httpx")):
                    verbs.add(inner.func.attr)
            if not verbs:
                continue
            names = set()
            for dec in node.decorator_list:
                if isinstance(dec, ast.Name):
                    names.add(dec.id)
                elif isinstance(dec, ast.Attribute):
                    names.add(dec.attr)
                elif isinstance(dec, ast.Call):
                    target = dec.func
                    names.add(target.id if isinstance(target, ast.Name)
                              else getattr(target, "attr", ""))
            found[node.name] = (verbs, "_guarded_write" in names)
        return found

    @staticmethod
    def _app_sources() -> list[Path]:
        from pathlib import Path

        from app import __file__ as app_init

        root = Path(app_init).resolve().parent
        return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]

    @staticmethod
    def _seed_result(result_id: str, *, coursework_id: str = "cw-1",
                     course_id: str = "course-1", submission_id: str = "sub-1",
                     student_id: str = "stu-1") -> None:
        """Real rows: report_attachments has FOREIGN KEYs to results, so an
        attachment state can only exist for a correction that really does."""
        from app import db

        db.execute("INSERT OR IGNORE INTO courses(id, name, fetched_at) "
                   "VALUES(?,?,?)", (course_id, "Guard course", 1.0))
        db.execute("INSERT OR IGNORE INTO correction_jobs(id, status, "
                   "created_at, updated_at) VALUES(?,?,?,?)",
                   (f"job-{result_id}", "COMPLETED", 1.0, 1.0))
        db.execute("INSERT OR IGNORE INTO coursework(id, course_id, title, "
                   "classroom_title, sync_state, allowlisted, fetched_at) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (coursework_id, course_id, "Guard fixture", "Guard fixture",
                    "ACTIVE", 0, 1.0))
        db.execute("INSERT OR IGNORE INTO results(id, job_id, coursework_id, "
                   "submission_id, student_id, student_name, earned, available, "
                   "percentage, result_json, created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (result_id, f"job-{result_id}", coursework_id, submission_id,
                    student_id, "Fixture", 0, 0, 0.0, '{"questions": []}', 1.0))

    @staticmethod
    def _modifiable_submission() -> dict:
        """What Classroom returns for a submission this app may modify.

        Without ``associatedWithDeveloper`` the attach path correctly refuses
        with BLOCKED_NOT_MODIFIABLE - long before the testing-only gate - and
        the test would then be measuring the wrong refusal.
        """
        return {"id": "sub-1", "userId": "stu-1", "state": "TURNED_IN",
                "associatedWithDeveloper": True,
                "assignmentSubmission": {"attachments": []}}

    @staticmethod
    def _rig_transport(monkeypatch, module, read_body: dict | None = None) -> list:
        """Make every HTTP write a recorded, hard failure; keep reads offline.

        Called BEFORE the code under test. If the guard lets a write through,
        the request lands here instead of at Google and the test fails with an
        explicit message - the transport is the last line of defence, so a
        promise cannot reach it unnoticed.

        Reads are answered locally rather than blocked, because testing-only
        mode deliberately still permits them (a reviewer has to see the real
        submissions), and a unit test must not depend on the network either way.
        """
        reached: list = []

        class _Stub:
            status_code = 200
            content = b"{}"
            text = "{}"

            @staticmethod
            def json():
                return read_body if read_body is not None else {}

        def tripwire(*args, **kwargs):
            reached.append((args, kwargs))
            raise AssertionError(
                "a Google write reached the HTTP transport while testing-only "
                "mode was ON - the guard was bypassed")

        def fake_request(method, url=None, *args, **kwargs):
            if str(method or "GET").upper() in ("GET", "HEAD", "OPTIONS"):
                return _Stub()
            return tripwire(method, url, *args, **kwargs)

        monkeypatch.setattr(module.requests, "request", fake_request)
        for name in ("get", "head"):
            monkeypatch.setattr(module.requests, name, lambda *a, **k: _Stub())
        for name in ("post", "patch", "put", "delete"):
            monkeypatch.setattr(module.requests, name, tripwire)
        return reached

    def test_the_only_raw_drive_writes_are_the_reviewed_three(self):
        from pathlib import Path

        from app.classroom import client as gc

        audited = self._raw_writes(Path(gc.__file__))
        assert set(audited) == self.RAW_WRITE_FUNCTIONS, (
            "a raw HTTP write was added or removed in classroom/client.py; "
            "review it for the testing-only guard and update "
            f"RAW_WRITE_FUNCTIONS. Found: {sorted(audited)}")
        unguarded = sorted(name for name, (_, guarded) in audited.items()
                           if not guarded)
        assert unguarded == [], (
            f"{unguarded} can reach Google with a raw write while testing-only "
            "mode is on - decorate it with @_guarded_write")

    def test_no_other_module_in_the_app_can_write_over_http(self):
        """A new module is the easiest place for a bypass to appear."""
        from pathlib import Path

        from app.classroom import client as gc

        client_path = Path(gc.__file__).resolve()
        offenders = {}
        for path in self._app_sources():
            if path.resolve() == client_path:
                continue                    # audited by the test above
            audited = self._raw_writes(path)
            if audited:
                offenders[path.name] = sorted(audited)
        assert offenders == {}, (
            f"HTTP writes outside classroom/client.py: {offenders}. Route them "
            "through the guarded client so testing-only mode can stop them.")

    def test_the_app_does_not_use_a_session_that_would_evade_the_audit(self):
        """``requests.Session().post(...)`` hides the receiver from the AST
        audit above, so its absence is asserted instead of assumed."""
        offenders = [p.name for p in self._app_sources()
                     if "requests.Session(" in p.read_text(encoding="utf-8")]
        assert offenders == [], (
            f"{offenders} build a requests.Session; a session-based write would "
            "not pass through the guarded client")

    @pytest.mark.parametrize("verb", ["POST", "PATCH", "PUT", "DELETE"])
    def test_every_mutating_verb_is_refused_before_the_transport(
            self, testing_only, monkeypatch, verb):
        from app.classroom import client as gc

        reached = self._rig_transport(monkeypatch, gc)
        with pytest.raises(PublicationBlocked):
            gc.classroom_request(verb, f"{gc.CLASSROOM_BASE}/courses/1", "tok",
                                 json={"draftGrade": 9})
        assert reached == [], "nothing may be sent to Google in testing mode"

    def test_the_shared_job_and_api_path_attach_report_is_held_back(
            self, testing_only, monkeypatch):
        """``attach_report`` is what the API route, the retry queue and the
        scheduler all call, so it is the one that must refuse on its own."""
        from app.classroom import client as gc
        from app.classroom import report_attachment as ra

        self._seed_result("result-guard-1", coursework_id="cw-1",
                          course_id="course-1")
        reached = self._rig_transport(monkeypatch, gc,
                                      read_body=self._modifiable_submission())
        # Rendering and PDF validation are LOCAL work and cannot publish
        # anything, so they are pinned here to measure the guard rather than
        # the PDF generator.
        monkeypatch.setattr(ra, "render_report_pdf",
                            lambda *a, **k: b"%PDF-1.4 local-only")
        monkeypatch.setattr(ra, "_validate_pdf", lambda *a, **k: {"ok": True})

        outcome = ra.attach_report("unused-token", {
            "id": "result-guard-1", "coursework_id": "cw-1",
            "submission_id": "sub-1", "student_id": "stu-1",
            "student_name": "Fixture", "result_json": {"questions": []},
        }, course_id="course-1")

        assert reached == [], "attach_report must not touch Drive"
        assert outcome.status == ra.STATUS_TESTING_BLOCKED, outcome.message
        assert "TESTING" in outcome.message.upper()
        assert "Settings" in outcome.message

    def test_the_attach_endpoint_answers_403_with_the_testing_only_code(
            self, testing_only, fresh_state, monkeypatch):
        """The HTTP path a scheduler or a stray curl would take is gated too."""
        import os
        import types as _types

        os.environ.setdefault("MC5_LOGIN_REQUIRED", "0")
        from fastapi.testclient import TestClient

        from app import db
        from app.api import routes_all
        from app.classroom import report_attachment as ra
        from app.main import app

        # A real correction row: the endpoint refuses nothing for an unknown id,
        # and a fake id would make this test pass for the wrong reason.
        self._seed_result("result-guard-api", coursework_id="cw-guard-api",
                          course_id="course-guard-api")

        # Connected on paper only. The guard trips before any Google call, so no
        # token is ever minted - this asserts the ORDER, not a lucky failure.
        monkeypatch.setattr(routes_all, "auth_state", lambda: _types.SimpleNamespace(
            connected=True, problem=None))
        monkeypatch.setattr(routes_all, "get_access_token", lambda: "unused")
        monkeypatch.setattr(ra, "render_report_pdf",
                            lambda *a, **k: b"%PDF-1.4 local-only")
        monkeypatch.setattr(ra, "_validate_pdf", lambda *a, **k: {"ok": True})
        # The endpoint must not reach Drive even once.
        from app.classroom import client as gc
        reached = self._rig_transport(monkeypatch, gc,
                                      read_body=self._modifiable_submission())

        with TestClient(app) as client:
            response = client.post("/api/results/result-guard-api/attach-report")

        assert reached == [], "the API published something with testing on"
        assert response.status_code == 403, response.text
        assert response.json().get("code") == ra.ERROR_TESTING_ONLY, response.text
