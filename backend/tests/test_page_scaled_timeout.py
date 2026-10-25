"""The request timeout must grow with the paper, not stay fixed.

A marking request sends every page as an image, so its duration is bounded by
how long the model takes to READ the submission — not by the prompt. Measured
on 2026-10-25 with a 15-page scan and a 26-page mark scheme (41 page images):
230-440 s for the call. The configured ``request_timeout_ms`` is therefore a
floor for small papers: a 70-page booklet would otherwise inherit a budget
sized for 15 pages and die with ``504 DEADLINE_EXCEEDED`` mid-answer.

No network and no quota: these are the pure sizing rules plus the wiring that
lets a worker tell the client how many pages it will send.
"""

from __future__ import annotations

import io

import pytest

from app.engine.pdf_inspect import page_images
from app.gemini import client_proxy as cp

CONFIGURED = 600_000     # the shipped floor (10 minutes)


def test_small_paper_keeps_the_configured_floor():
    # 15 pages: 120 s + 15 s/page = 345 s, below the configured 10 minutes.
    assert cp.timeout_for_pages(15, CONFIGURED) == CONFIGURED


def test_measured_paper_gets_more_than_the_floor():
    # The paper this was measured on: 15-page scan + 26-page scheme.
    assert cp.timeout_for_pages(41, CONFIGURED) == 120_000 + 41 * 15_000


def test_a_bigger_homework_gets_proportionally_more():
    small = cp.timeout_for_pages(20, CONFIGURED)
    big = cp.timeout_for_pages(70, CONFIGURED)
    assert big > small
    assert big == 120_000 + 70 * 15_000


def test_a_large_configured_value_still_wins():
    """A teacher who raised the setting keeps the larger budget."""
    assert cp.timeout_for_pages(10, 1_200_000) == 1_200_000


def test_huge_paper_is_capped_so_a_worker_is_never_held_forever():
    assert cp.timeout_for_pages(10_000, CONFIGURED) == cp.MAX_REQUEST_TIMEOUT_MS


@pytest.mark.parametrize("pages", [0, None, -5, "not a number"])
def test_unknown_page_count_falls_back_to_the_configured_value(pages):
    assert cp.timeout_for_pages(pages, CONFIGURED) == CONFIGURED


def _pdf(pages: int) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for index in range(pages):
        pdf.drawString(72, 720, f"page {index + 1}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def test_page_images_counts_a_pdf(tmp_path):
    path = tmp_path / "script.pdf"
    path.write_bytes(_pdf(3))
    assert page_images(path) == 3


def test_page_images_counts_an_image_as_one_page(tmp_path):
    path = tmp_path / "page.png"
    path.write_bytes(b"not really a png, the suffix is what matters here")
    assert page_images(path) == 1


def test_page_images_is_zero_for_a_missing_or_unreadable_file(tmp_path):
    assert page_images(None) == 0
    assert page_images(tmp_path / "missing.pdf") == 0
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    assert page_images(broken) == 0


def test_client_carries_the_page_count_it_was_given():
    """The worker hands the client the size of the request it will make."""
    from app.gemini.key_manager import KeyManager

    client = cp.KeyAwareClient(KeyManager(), pages=70)
    assert client.pages == 70
    assert cp.KeyAwareClient(KeyManager()).pages == 0
