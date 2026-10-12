"""Mark-scheme upload — public-mode regression (§28, §14).

The teacher could not upload a mark scheme AT ALL. Root cause: every upload
call site in the UI hand-rolled ``fetch()`` instead of the shared ``api``
helper, so the request left WITHOUT the session's CSRF token. In public mode
the auth gate answers ``403 csrf_failed`` before the route is ever reached —
which is also why nothing was written to the server log, and why the page
reported a generic "This request could not be verified. Reload the page and
try again." that reloading could never fix.

Verified against the live public address before the fix (identical multipart
upload): 403 ``csrf_failed`` without the header, 200 ``{"stored": true}`` with
it. These tests pin that contract at the HTTP level and guard the frontend
source so no future upload can silently reintroduce a bare ``fetch()``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
COURSE = "c-ms-1"
WORK = "cw-ms-1"
PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
       b"trailer<</Root 1 0 R>>\n%%EOF\n")
URL = f"/api/classroom/courses/{COURSE}/assignments/{WORK}/mark-scheme"


@pytest.fixture
def client(fresh_state):
    """A TestClient over the REAL app (the upload route lives in routes_all)."""
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def public_client(client, monkeypatch):
    """The real app in public mode, signed in — exactly the teacher's situation.

    Yields ``(client, csrf_token)``; the token is what the UI's shared helper
    attaches to every mutating request.
    """
    from app import auth_gate
    monkeypatch.setenv("MC5_PUBLIC", "1")
    auth_gate.set_credentials("teacher", "correct-horse-battery")
    resp = client.post("/api/auth/login", json={
        "username": "teacher", "password": "correct-horse-battery"})
    assert resp.status_code == 204, resp.text

    # The session cookie is `Secure` by design whenever public mode is on, and
    # httpx will not send a Secure cookie over the TestClient's http:// scheme.
    # Carry it explicitly so these tests exercise the CSRF gate rather than
    # cookie policy (same reason curl needed an explicit cookie header).
    session = resp.cookies.get("mc5_session")
    assert session, "login must set a session cookie"
    client.headers["cookie"] = f"mc5_session={session}"
    yield client, resp.headers["x-mc5-csrf"]


def _upload(client, headers: dict | None = None, name: str = "scheme.pdf",
            content: bytes = PDF, mime: str = "application/pdf"):
    return client.post(URL, headers=headers or {},
                       files={"file": (name, content, mime)})


# ------------------------------------------------------- the reported failure

def test_upload_without_a_csrf_token_is_rejected_in_public_mode(public_client):
    """The exact request the UI used to send: session cookie, no CSRF header."""
    client, _csrf = public_client
    resp = _upload(client)
    assert resp.status_code == 403
    assert resp.json()["code"] == "csrf_failed"


def test_upload_with_the_session_csrf_token_succeeds_and_persists(
        public_client, fresh_state):
    client, csrf = public_client
    resp = _upload(client, {"x-mc5-csrf": csrf})
    assert resp.status_code == 200
    assert resp.json() == {"coursework_id": WORK, "stored": True}

    stored = fresh_state / "mark_schemes" / f"{WORK}.pdf"
    assert stored.read_bytes() == PDF

    from app import db
    row = db.query_one(
        "SELECT path, uploaded_at FROM pending_mark_schemes WHERE coursework_id = ?",
        (WORK,))
    assert row is not None
    assert row["path"] == str(stored)
    assert row["uploaded_at"] > 0


def test_reupload_replaces_the_scheme_but_keeps_the_previous_one(
        public_client, fresh_state):
    """Uploading REPLACES the active scheme — and must archive the old one.

    The route writes one file per assignment, so a wrong upload used to destroy
    the teacher's only copy. The superseded PDF is now kept under _previous/.
    """
    client, csrf = public_client
    assert _upload(client, {"x-mc5-csrf": csrf}).status_code == 200
    second = PDF + b"%% replaced\n"
    assert _upload(client, {"x-mc5-csrf": csrf}, content=second).status_code == 200

    stored = fresh_state / "mark_schemes" / f"{WORK}.pdf"
    assert stored.read_bytes() == second

    archive = fresh_state / "mark_schemes" / "_previous"
    kept = sorted(archive.glob(f"{WORK}-*.pdf"))
    assert len(kept) == 1
    assert kept[0].read_bytes() == PDF          # the recoverable first upload


def test_only_the_newest_few_superseded_schemes_are_kept(public_client,
                                                        fresh_state):
    """Recovery must not grow without bound."""
    client, csrf = public_client
    for i in range(6):
        body = PDF + f"%% version {i}\n".encode()
        assert _upload(client, {"x-mc5-csrf": csrf}, content=body).status_code == 200

    from app.api.routes_all import PREVIOUS_SCHEMES_KEPT
    kept = list((fresh_state / "mark_schemes" / "_previous").glob(f"{WORK}-*.pdf"))
    assert len(kept) == PREVIOUS_SCHEMES_KEPT


def test_non_pdf_mark_scheme_is_rejected(public_client):
    client, csrf = public_client
    resp = _upload(client, {"x-mc5-csrf": csrf}, name="notes.txt",
                   content=b"hello", mime="text/plain")
    assert resp.status_code == 400
    assert resp.json()["code"] == "bad_file_type"


# ------------------------------------------------- local mode must not change

def test_local_mode_upload_still_needs_no_csrf_token(client, monkeypatch,
                                                     fresh_state):
    """Localhost has never required the token — the fix must not add one."""
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    assert _upload(client).status_code == 200
    assert (fresh_state / "mark_schemes" / f"{WORK}.pdf").exists()


# --------------------------------------------------- frontend source guards

def _frontend_sources():
    root = ROOT / "frontend" / "src"
    return {p: p.read_text(encoding="utf-8") for p in root.rglob("*.ts*")}


def test_no_frontend_file_hand_rolls_a_mutating_fetch():
    """A bare fetch() cannot carry the CSRF token, so EVERY mutating request
    must go through api() or postForm(). A GET download (report export) is the
    only acceptable raw call outside ``api.ts``, which holds both helpers."""
    offenders = []
    for path, text in _frontend_sources().items():
        if path.name == "api.ts":      # the sanctioned helpers themselves
            continue
        for match in re.finditer(r"fetch\(", text):
            window = text[match.end():match.end() + 240]
            if re.search(r'method:\s*"(POST|PUT|PATCH|DELETE)"', window):
                offenders.append(path.name)
    assert offenders == [], (
        "these files POST/PUT/DELETE with a hand-rolled fetch(), which drops the "
        f"public-mode CSRF token and fails with 403: {sorted(set(offenders))}")


def test_every_upload_call_site_uses_the_shared_form_helper():
    sources = _frontend_sources()
    by_name = {p.name: text for p, text in sources.items()}

    # The three mark-scheme uploads plus the manual-correction upload.
    for name in ("ClassroomPage.tsx", "AssignmentsPage.tsx", "CorrectionsPage.tsx"):
        assert "postForm" in by_name[name], f"{name} must upload via postForm()"

    # postForm() itself: sends the CSRF token, and leaves Content-Type alone so
    # the browser can set the multipart boundary.
    # Read the file by PATH: a second "api.ts" (frontend/src/distributed/api.ts)
    # shares the basename and would otherwise overwrite it in by_name.
    api_src = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    assert 'headers["x-mc5-csrf"] = csrfToken;' in api_src
    body = api_src.split("export async function postForm", 1)[1]
    assert body.split("const body")[0].count("Content-Type") == 0
    assert "requestHeaders(\"POST\")" in body
    assert "method: \"POST\", body: form" in body
