"""Demo Mode — synthetic data only, never a real Classroom account.

Demo Mode exists so a teacher can show Math Checker to someone without any
risk of exposing real students. It is enforced structurally, not by asking
politely:

  * it runs against its OWN data directory (``<base>/demo``), so it can never
    read or write the real database, config or credentials;
  * Google Classroom is hard-disabled — connect attempts are refused and the
    auth state reports not-configured, so it cannot inherit a real token;
  * the AI engine runs in mock mode, so no key is required and nothing is
    uploaded;
  * the data it shows is entirely seeded synthetic content (Student A, …).

Activation: ``python main.py --demo`` (or ``MC5_DEMO=1``).
"""

from __future__ import annotations

import os
import time

DEMO_ENV = "MC5_DEMO"
_DEMO_META = "demo_seeded"
_DEMO_VERSION = "2"


def is_demo() -> bool:
    return os.environ.get(DEMO_ENV) == "1"


def activate(base_data_dir: str) -> str:
    """Point the process at an isolated demo data dir and flag demo mode.

    MUST be called BEFORE ``app.config`` is imported (DATA_DIR is read at
    import time). Returns the demo data directory.

    IDEMPOTENT. The on-demand supervisor starts the backend as a child process
    that INHERITS demo mode (``MC5_DEMO=1``) and the already-demo ``MC5_DATA_DIR``
    (``<base>/demo``) and then runs this function again. Appending ``"demo"`` a
    second time moved the app to ``<base>/demo/demo`` while the launcher,
    ``--stop`` and a teacher looking in the folder all use ``<base>/demo`` - the
    demo database (and mock mode) were effectively invisible. Reusing the demo
    directory we are already in keeps the two processes on the same data.
    """
    from pathlib import Path
    base = Path(base_data_dir)
    if is_demo() and base.name == "demo":
        os.environ["MC5_DATA_DIR"] = str(base)
        return str(base)
    demo_dir = base / "demo"
    os.environ["MC5_DATA_DIR"] = str(demo_dir)
    os.environ[DEMO_ENV] = "1"
    return str(demo_dir)


# --------------------------------------------------------------- seeding

SYNTHETIC_CLASSES = [
    ("demo-course-1", "Demo — Year 10 Mathematics", "10A"),
    ("demo-course-2", "Demo — Year 11 Mathematics", "11B"),
]

SYNTHETIC_ASSIGNMENTS = [
    ("demo-work-1", "demo-course-1", "Demo — Algebra Homework", 20, 3),
    ("demo-work-2", "demo-course-1", "Demo — Quadratics Paper", 30, 5),
    ("demo-work-3", "demo-course-2", "Demo — Trigonometry HW", 15, 1),
]

SYNTHETIC_STUDENTS = [
    ("demo-stu-1", "Student A"),
    ("demo-stu-2", "Student B"),
    ("demo-stu-3", "Student C"),
    ("demo-stu-4", "Student D"),
    ("demo-stu-5", "Student E"),
]

# Per-question layout for the synthetic corrections. It exists so Demo Mode
# can show the same question-level feedback a real correction carries (marks,
# topics, what was lost) instead of an empty breakdown. Marks are split by the
# weights below and always sum to the stored total.
_QUESTION_PLANS = {
    "demo-work-1": [("Q1", "Solving linear equations", 8),
                    ("Q2", "Expanding brackets", 6),
                    ("Q3", "Factorising", 6)],
    "demo-work-2": [("Q1", "Completing the square", 12),
                    ("Q2", "Quadratic formula", 10),
                    ("Q3", "Graphs of quadratics", 8)],
    "demo-work-3": [("Q1", "SOHCAHTOA", 8),
                    ("Q2", "Sine and cosine rules", 7)],
}


def _demo_questions(work_id: str, earned: int, available: int) -> list[dict]:
    """Split a synthetic score across the assignment's questions.

    The split is proportional and rounded so the question marks add up to the
    stored total exactly — a demo profile must never show a breakdown that
    contradicts its own score.
    """
    plan = _QUESTION_PLANS.get(work_id)
    if not plan:
        return []
    weight = sum(marks for _label, _topic, marks in plan) or available or 1
    raw = [earned * marks / weight for _label, _topic, marks in plan]
    awards = [int(value) for value in raw]
    remainder = max(0, earned - sum(awards))
    order = sorted(range(len(raw)), key=lambda i: raw[i] - awards[i], reverse=True)
    for index in order[:remainder]:
        awards[index] += 1

    questions = []
    for (label, topic, max_marks), awarded in zip(plan, awards):
        lost = max_marks - awarded
        questions.append({
            "question_number": label,
            "max_marks": max_marks,
            "awarded_marks": awarded,
            "topic": topic,
            "status": "VALID",
            "loss_reason": (
                "Method shown, mark lost in the final step." if lost
                else "Full marks."),
            "mark_breakdown": [
                {"code": "M1", "awarded": True,
                 "reason": "Correct method selected"},
                {"code": "A1", "awarded": lost == 0,
                 "reason": "Correct final answer" if lost == 0
                 else "Final answer not correct"},
            ],
        })
    return questions


def _demo_verification(count: int) -> str:
    import json
    return json.dumps({
        "status": "VALID",
        "verified": True,
        "anomalies": [],
        "warnings": [],
        "review_questions": [],
        "coverage": {
            "expected": count,
            "marked": count,
            "requires_review": 0,
            "unanswered": 0,
            "verified": True,
        },
    })


# Which synthetic students submitted each assignment (state, late, earned).
_SUBMISSIONS = {
    "demo-work-1": [("TURNED_IN", False, (16, 20)), ("TURNED_IN", False, (18, 20)),
                    ("TURNED_IN", True, (12, 20)), ("CREATED", False, None)],
    "demo-work-2": [("TURNED_IN", False, (27, 30)), ("RECLAIMED_BY_STUDENT", False, None),
                    ("TURNED_IN", False, (21, 30))],
    "demo-work-3": [("TURNED_IN", False, (13, 15)), ("TURNED_IN", False, (11, 15)),
                    ("TURNED_IN", False, (9, 15))],
}


def seed() -> dict:
    """Insert synthetic classes/assignments/students/results (idempotent)."""
    from . import db

    if db.get_meta(_DEMO_META) == _DEMO_VERSION:
        return {"seeded": False, "reason": "already seeded"}

    now = time.time()
    with db.transaction() as conn:
        for cid, name, section in SYNTHETIC_CLASSES:
            conn.execute(
                "INSERT INTO courses(id, name, section, state, update_time, fetched_at) "
                "VALUES(?,?,?, 'ACTIVE', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (cid, name, section, "2026-10-04T00:00:00Z", now),
            )
        for wid, cid, title, max_points, _lesson in SYNTHETIC_ASSIGNMENTS:
            conn.execute(
                "INSERT INTO coursework(id, course_id, title, max_points, state, "
                "due_date, allowlisted, description, update_time, fetched_at) "
                "VALUES(?,?,?,?, 'PUBLISHED', ?, 1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title",
                (wid, cid, title, max_points, "2026-10-10 20:00",
                 "Synthetic demo assignment — no real class data.",
                 "2026-10-04T00:00:00Z", now),
            )
        for sid, name in SYNTHETIC_STUDENTS:
            conn.execute(
                "INSERT INTO students(id, name, email, fetched_at) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (sid, name, f"{name.lower().replace(' ', '.')}@demo.invalid", now),
            )

        result_index = 0
        for wid, cid, _title, _max_points, _lesson in SYNTHETIC_ASSIGNMENTS:
            entries = _SUBMISSIONS.get(wid, [])
            for idx, (state, late, score) in enumerate(entries):
                sid = SYNTHETIC_STUDENTS[idx][0]
                name = SYNTHETIC_STUDENTS[idx][1]
                sub_id = f"{wid}:{sid}"
                attachments = ([] if state != "TURNED_IN"
                               else [{"kind": "drive", "id": f"demo-file-{idx}",
                                      "title": f"{name} - demo.pdf"}])
                import json
                conn.execute(
                    "INSERT INTO classroom_submissions(submission_id, course_id, "
                    "coursework_id, student_id, student_name, student_email, "
                    "current_state, late, creation_time, update_time, "
                    "attachments_json, assigned_grade, data_quality, last_synced_at, "
                    "first_seen_at, sync_version) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(submission_id) DO UPDATE SET "
                    "current_state=excluded.current_state",
                    (sub_id, cid, wid, sid, name,
                     f"{name.lower().replace(' ', '.')}@demo.invalid", state,
                     int(late), "2026-10-04T10:00:00Z", "2026-10-04T11:00:00Z",
                     json.dumps(attachments), None,
                     "VERIFIED" if attachments else "PARTIAL", now, now),
                )
                if score is None:
                    continue
                earned, available = score
                result_index += 1
                job_id = f"demo-job-{result_index}"
                result_id = f"demo-result-{result_index}"
                conn.execute(
                    "INSERT OR IGNORE INTO correction_jobs(id, submission_id, "
                    "coursework_id, course_id, student_id, student_name, status, "
                    "progress, attempts, max_attempts, created_at, finished_at, "
                    "updated_at, result_id) "
                    "VALUES(?,?,?,?,?,?, 'COMPLETED', 1, 1, 3, ?, ?, ?, ?)",
                    (job_id, sub_id, wid, cid, sid, name, now - 3600, now - 3500,
                     now - 3500, result_id),
                )
                questions = _demo_questions(wid, earned, available)
                conn.execute(
                    "INSERT INTO results(id, job_id, coursework_id, "
                    "submission_id, student_id, student_name, earned, available, "
                    "percentage, provisional_grade, marking_model, confidence, "
                    "confidence_reasons, review_required, manual_override, "
                    "result_json, verification_json, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?) "
                    # An older demo seed stored an empty breakdown; upgrading it
                    # keeps the synthetic scores but fills in the questions.
                    "ON CONFLICT(id) DO UPDATE SET "
                    "result_json=excluded.result_json, "
                    "verification_json=excluded.verification_json",
                    (result_id, job_id, wid, sub_id, sid, name, earned, available,
                     100.0 * earned / available, _grade(100.0 * earned / available),
                     "demo-model", "HIGH", "[]",
                     json.dumps({"questions": questions}),
                     _demo_verification(len(questions)), now - 3500),
                )

    db.set_meta(_DEMO_META, _DEMO_VERSION)
    return {"seeded": True, "results": result_index}


def _grade(percentage: float) -> str:
    for minimum, grade in ((90, "9"), (80, "8"), (70, "7"), (60, "6"),
                           (50, "5"), (40, "4"), (0, "3")):
        if percentage >= minimum:
            return grade
    return "3"


class DemoClassroomBlocked(RuntimeError):
    """Raised when a real Classroom action is attempted in Demo Mode."""


def guard_classroom() -> None:
    """Refuse any real Classroom operation while in Demo Mode."""
    if is_demo():
        raise DemoClassroomBlocked(
            "Demo Mode is active. It uses synthetic data only and cannot "
            "connect to a real Google Classroom account. Restart Math Checker "
            "normally (without --demo) to use your own account."
        )
