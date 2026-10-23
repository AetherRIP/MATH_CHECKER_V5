"""Mock correction isolation: offline, key-free and inert outside the database.

The audit's rule is that AI work may only be exercised in a way that cannot
reach the live classroom or a production credential. Mock mode is that
environment, and it is only trustworthy if it is provably isolated:

* zero Gemini keys configured — a real call is impossible, not merely unused;
* no classroom row, automation job or automation log entry is created;
* the stored result says it is a demo, so it can never be mistaken for a real
  mark in a report or a screenshot;
* the marking still exercises the real pipeline: parsing, per-question
  partial credit, verification and the result guard.

No network access, no classroom call and no credential is involved anywhere in
this file.
"""

from __future__ import annotations

import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import db


def _pdf_bytes(pages: int = 1) -> bytes:
    """A small readable PDF (synthetic content, no student data)."""
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    for page in range(pages):
        pdf.setFont("Helvetica", 12)
        pdf.drawString(60, 760, f"Synthetic answer sheet — page {page + 1}")
        pdf.drawString(60, 730, "Q1  x^2 + 5x + 6 = (x + 2)(x + 3)")
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


WAIT_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


@pytest.fixture
def client(fresh_state):
    """The real app in mock mode with NO Gemini keys at all."""
    from app.config import get_config, get_secrets, save_config
    cfg = get_config()
    cfg.mock_mode = True
    cfg.classroom.enabled = False
    cfg.automation.enabled = False
    save_config(cfg)
    assert get_secrets().gemini_keys() == []

    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def run_correction(client) -> dict:
    response = client.post(
        "/api/corrections/manual",
        files={
            "submission": ("synthetic.pdf", io.BytesIO(_pdf_bytes()), "application/pdf"),
            "mark_scheme": ("synthetic-scheme.pdf", io.BytesIO(_pdf_bytes()), "application/pdf"),
        },
        data={"student_name": "Synthetic Candidate"},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]

    deadline = time.time() + 40
    job = response.json()
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in WAIT_TERMINAL:
            break
        time.sleep(0.2)
    assert job["status"] == "COMPLETED", job
    return client.get(f"/api/results/{job['result_id']}").json()


def test_a_mock_correction_needs_no_key_and_is_clearly_labelled(client):
    result = run_correction(client)

    payload = result["result_json"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    flattened = json.dumps(payload).lower()

    # The demo identity travels with the stored result, not only with a banner
    # in one client, so an exported report cannot pretend to be real marking.
    assert "mock" in flattened or "demo" in flattened
    assert result["earned"] == 9 and result["available"] == 12
    assert result["percentage"] == pytest.approx(75.0, abs=0.1)


def test_a_mock_correction_keeps_per_question_partial_credit(client):
    result = run_correction(client)

    payload = result["result_json"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    questions = payload["questions"]
    marks = [(q["question_number"], q["awarded_marks"], q["max_marks"]) for q in questions]

    assert marks == [("1", 2, 3), ("2", 4, 4), ("3", 3, 5)]
    assert sum(awarded for _, awarded, _ in marks) == result["earned"]
    assert sum(total for _, _, total in marks) == result["available"]
    # Partial credit is justified, never just a number.
    assert all(q.get("loss_reason") is not None for q in questions)
    assert any(q["loss_reason"] for q in questions)


def test_verification_runs_on_the_mock_result(client):
    result = run_correction(client)

    verdict = result["verification_json"]
    if isinstance(verdict, str):
        verdict = json.loads(verdict)
    assert verdict["verified"] is True


def test_a_mock_correction_writes_nothing_outside_its_own_database(client):
    run_correction(client)

    assert db.query("SELECT * FROM classroom_submissions") == []
    assert db.query("SELECT * FROM automation_jobs") == []
    assert db.query("SELECT * FROM automation_log") == []
    assert db.query("SELECT * FROM automation_operations") == []
    # The staged upload is removed when the job is terminal (no orphan copies).
    assert db.query("SELECT * FROM pending_manual_files") == []


def test_the_mock_result_is_readable_through_the_viewer_contract(client):
    """The desk asks for pages; a mock result must answer honestly, not throw."""
    result = run_correction(client)

    body = client.get(f"/api/results/{result['id']}/pages").json()

    assert "available" in body
    assert body["result_id"] == result["id"]
