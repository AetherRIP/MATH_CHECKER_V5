"""Every export keeps the subquestion layer, not just the database.

The engine writes per-question ``subquestions`` (marks, status, scheme answers,
marking points). A report that printed only the parent total forced the reader
to guess which part lost the marks, so these tests pin the hierarchy into the
Markdown, CSV and PDF exports.
"""

from __future__ import annotations

import io

import pytest

from app.engine.legacy_marking import build_csv_rows, build_markdown_report

QUESTION = {
    "question_number": "7",
    "max_marks": 5,
    "awarded_marks": 2,
    "topic": "Percentage Increase",
    "loss_reason": "Part (b) was left blank.",
    "correct_method": "15000 * 1.075 = 16125, then apply the second increase",
    "full_mark_solution": "16125 and 1944",
    "subquestions": [
        {
            "id": "7a",
            "label": "7a",
            "parent_id": "7",
            "max_marks": 2,
            "awarded_marks": 2,
            "status": "MARKED",
            "answers": ["16125"],
            "mark_codes": ["M1", "A1"],
        },
        {
            "id": "7b",
            "label": "7b",
            "parent_id": "7",
            "max_marks": 3,
            "awarded_marks": 0,
            "status": "UNMARKED",
            "answers": [],
            "mark_codes": [],
        },
    ],
}

PLAIN_QUESTION = {
    "question_number": "8",
    "max_marks": 3,
    "awarded_marks": 3,
    "topic": "Angles",
    "loss_reason": None,
    "correct_method": "angles on a straight line",
    "full_mark_solution": "x = 40",
}

RESULT = {
    "paper_info": {"paper": "Paper 2", "session": "June 2017"},
    "overall_notes": {"summary": "Two parts, one left blank.", "strengths": [],
                      "revision_areas": []},
    "questions": [QUESTION, PLAIN_QUESTION],
}

TOTALS = {"earned": 5, "available": 8, "percentage": 62.5,
          "provisional_grade": "5"}


def test_markdown_keeps_the_parts_and_the_unmarked_state():
    report = build_markdown_report("TEST 2", RESULT, TOTALS)

    assert "### Question 7 — 2 / 5" in report
    # Each part carries its OWN mark, indented under its question.
    assert "  - 7a: 2 / 2" in report
    assert "Scheme answer: 16125" in report
    assert "Marking points: M1, A1" in report
    # An unmarked part is never printed as a confirmed zero.
    assert "  - 7b: UNMARKED (of 3)" in report
    # A question with no parts adds none.
    assert "8:" not in report.split("### Question 8")[1]


def test_csv_rows_keep_one_row_per_question_and_name_the_parts():
    rows = build_csv_rows("TEST 2", RESULT)

    assert [row["Question"] for row in rows] == ["7", "8"]
    # One cell, so a spreadsheet total cannot count the paper twice.
    assert rows[0]["Parts"] == "7a 2/2; 7b unmarked"
    assert rows[1]["Parts"] == ""
    assert (rows[0]["Awarded"], rows[0]["Max"]) == (2, 5)


def test_the_pdf_report_prints_each_subquestion():
    pytest.importorskip("pypdf")
    from pypdf import PdfReader

    from app.api.report_routes import render_result_pdf

    row = {
        "id": "result-sub-1",
        "student_name": "TEST 2",
        "display_name": "TEST 2",
        "class_label": "10C",
        "earned": 2,
        "available": 5,
        "percentage": 40.0,
        "created_at": 1792941724.0,
        "result_json": RESULT,
        "verification_json": {},
    }

    buf = render_result_pdf(row, detail="detailed")
    reader = PdfReader(io.BytesIO(buf.getvalue()))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert "SUBQUESTION 7A" in text.upper()
    assert "2 / 2 marks" in text
    assert "UNMARKED" in text.upper()
    assert "Scheme answer" in text.lower() or "scheme: 16125" in text.lower()


def test_a_subquestion_without_status_is_unmarked_not_zero():
    from app.api.report_routes import _subquestion_detail

    detail = _subquestion_detail({"label": "7b", "max_marks": 3,
                                  "awarded_marks": 0, "status": None})

    assert detail.startswith("UNMARKED")
    assert "0 / 3" not in detail
