"""Developer-only Gemini prompt inspection/editor page.

Locks down three promises of the page:

  1. it is Developer-role only (read AND write),
  2. it shows the engine's OWN prompt texts — proven by asserting the API
     payload equals the very constants the engine sends, and by capturing the
     actual generate_content request,
  3. an override saved on the page is what ``grade_with_model`` REALLY sends
     (captured from a fake client), survives a config reload (so the worker
     child — a separate interpreter — marks with the same prompt), and
     clearing it restores the built-in legacy prompts byte-for-byte.
"""

from __future__ import annotations

import pytest

from app import config as cfg_mod
from app.config import get_config, save_config
from app.engine import legacy_marking as legacy


@pytest.fixture
def api(fresh_state):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as client:
        yield client


def _as(role: str) -> None:
    cfg = get_config()
    cfg.role = role
    save_config(cfg)


class _CapturingClient:
    """Minimal client stand-in that records the EXACT request the engine sends."""

    class _Resp:
        text = '{"ok": true}'

    def __init__(self):
        class Models:
            def __init__(self):
                self.calls = []

            def generate_content(self, *, model, contents, config=None, **kw):
                self.calls.append({"model": model, "contents": contents,
                                   "config": config})
                return _CapturingClient._Resp()

        self.models = Models()


@pytest.fixture(autouse=True)
def _clean_model_health():
    legacy.MODEL_HEALTH.reset()
    yield
    legacy.MODEL_HEALTH.reset()


class TestPromptPageIsDeveloperOnly:
    def test_a_teacher_cannot_read_the_page(self, api):
        _as("teacher")
        r = api.get("/api/dev/prompts")
        assert r.status_code == 403
        assert r.json()["code"] == "developer_only"

    def test_a_teacher_cannot_write_prompts(self, api):
        _as("teacher")
        r = api.post("/api/dev/prompts",
                     json={"system_prompt_override": "hijack"})
        assert r.status_code == 403
        assert r.json()["code"] == "developer_only"
        assert legacy.effective_system_prompt() == legacy.MARKING_SYSTEM


class TestPageShowsTheActualPrompts:
    def test_texts_are_the_engines_own_constants(self, api):
        _as("developer")
        body = api.get("/api/dev/prompts").json()
        prompts = {p["id"]: p for p in body["prompts"]}
        # Exactly what the engine holds — no hand-copied duplicate anywhere.
        assert prompts["marking_system"]["text"] == legacy.MARKING_SYSTEM
        assert prompts["marking_system"]["builtin"] == legacy.MARKING_SYSTEM
        assert prompts["marking_user"]["text"] == legacy.marking_user_prompt()
        assert prompts["marking_user"]["builtin"] == legacy._MARKING_USER_BASE
        assert "question_number=1" in prompts["blueprint_block"]["text"]
        assert "FIRST-PASS REPORT" in prompts["audit_user"]["text"]
        assert body["response_schema"] == legacy.OUTPUT_SCHEMA
        assert body["models"]["chain"] == list(
            get_config().gemini.primary_models)
        assert body["overrides"]["active"] is False

    def test_the_engine_actually_sends_what_the_page_shows(self, api):
        _as("developer")
        shown = {p["id"]: p["text"]
                 for p in api.get("/api/dev/prompts").json()["prompts"]}
        client = _CapturingClient()
        legacy.grade_with_model(client, ["m1"], ["student-file"], "ms-file")
        call = client.models.calls[0]
        # The page is truthful: same system instruction, same final text part.
        assert call["config"]["system_instruction"] == shown["marking_system"]
        assert call["contents"][-1] == shown["marking_user"]


class TestSessionRoleGate:
    """The app-wide "developer mode" alone must never expose the prompt page
    to a teacher's SESSION.

    Found live: with the machine in developer mode, a teacher account's
    session received 200 from /api/dev/prompts. The gate must check the
    SIGNED-IN account too.
    """

    @pytest.fixture
    def gated_api(self, fresh_state, monkeypatch):
        monkeypatch.setenv("MC5_LOGIN_REQUIRED", "1")
        from fastapi.testclient import TestClient

        from app.distributed import accounts
        from app.main import app

        # fresh_state points the DB at a NEW file; the process-wide
        # "tables ready" flag must be cleared or the tables are never
        # created for this test's database.
        monkeypatch.setattr(accounts, "_TABLES_READY", False)
        accounts.ensure_tables_ready()
        accounts.create_account(username="dev", display_name="Dev",
                                password="dev-password-12345",
                                role="DEVELOPER")
        accounts.create_account(username="teach", display_name="Teach",
                                password="teach-password-12345",
                                role="TEACHER")
        with TestClient(app) as client:
            yield client

    @staticmethod
    def _login(api, username: str, password: str) -> str:
        r = api.post("/api/auth/login",
                     json={"username": username, "password": password})
        assert r.status_code == 204, r.text
        return r.headers.get("X-MC5-CSRF", "")

    def test_teacher_session_is_forbidden_even_in_developer_mode(self,
                                                                gated_api):
        _as("developer")          # the machine is in developer mode
        csrf = self._login(gated_api, "teach", "teach-password-12345")
        assert csrf
        r = gated_api.get("/api/dev/prompts")
        assert r.status_code == 403
        assert r.json()["code"] == "developer_only"
        r2 = gated_api.post("/api/dev/prompts",
                            json={"system_prompt_override": "hijack"},
                            headers={"x-mc5-csrf": csrf})
        assert r2.status_code == 403
        assert legacy.effective_system_prompt() == legacy.MARKING_SYSTEM

    def test_developer_session_still_reads_and_writes_the_page(self, gated_api):
        _as("developer")
        csrf = self._login(gated_api, "dev", "dev-password-12345")
        assert gated_api.get("/api/dev/prompts").status_code == 200
        r = gated_api.post("/api/dev/prompts",
                           json={"system_prompt_override": "DEV OK"},
                           headers={"x-mc5-csrf": csrf})
        assert r.status_code == 200
        assert r.json()["overrides"]["system_prompt_override"] == "DEV OK"


class TestOverrideRoundTrip:
    def test_override_reaches_the_engine_and_persists(self, api):
        _as("developer")
        r = api.post("/api/dev/prompts", json={
            "system_prompt_override": "CUSTOM SYSTEM PROMPT",
            "user_prompt_override": "CUSTOM USER PROMPT"})
        assert r.status_code == 200
        body = r.json()
        assert body["overrides"]["active"] is True
        assert body["prompts"][0]["edited"] is True

        # Every caller reads these functions — they see the override at once.
        assert legacy.effective_system_prompt() == "CUSTOM SYSTEM PROMPT"
        assert legacy.marking_user_prompt().startswith("CUSTOM USER PROMPT")

        # ...and grade_with_model REALLY sends it (captured request).
        client = _CapturingClient()
        legacy.grade_with_model(client, ["m1"], ["sf"], "ms")
        call = client.models.calls[0]
        assert call["config"]["system_instruction"] == "CUSTOM SYSTEM PROMPT"
        assert call["contents"][-1] == "CUSTOM USER PROMPT"

        # Persisted: the worker child re-reads config.toml, so it marks with
        # the same prompt as the parent process.
        from app import config as cfg_mod
        fresh = cfg_mod.get_config(reload=True)
        assert fresh.gemini.system_prompt_override == "CUSTOM SYSTEM PROMPT"
        assert fresh.gemini.user_prompt_override == "CUSTOM USER PROMPT"
        # The TOML writer must never stringify None into a fake prompt.
        assert 'prompt_override = "None"' not in cfg_mod.CONFIG_PATH.read_text(
            encoding="utf-8")

    def test_clearing_restores_the_built_in_prompts_exactly(self, api):
        _as("developer")
        api.post("/api/dev/prompts",
                 json={"system_prompt_override": "X",
                       "user_prompt_override": "Y"})
        r = api.post("/api/dev/prompts",
                     json={"system_prompt_override": "",
                           "user_prompt_override": ""})
        assert r.status_code == 200
        assert r.json()["overrides"]["active"] is False
        assert legacy.effective_system_prompt() == legacy.MARKING_SYSTEM
        assert legacy.marking_user_prompt() == legacy._MARKING_USER_BASE
        # Cleared from disk too — the key is absent, not an empty string.
        assert "prompt_override" not in cfg_mod.CONFIG_PATH.read_text(
            encoding="utf-8")
