"""Render short and long sample reports so the design can be reviewed by eye.

Not a test - a design review aid, like ``preview_class_report.py``. It builds
the SAME model the API builds and hands it to the SAME renderer, so what lands
on the page is what a teacher would get. Every figure is synthetic review
data: no real student, mark or submission is read or invented here.

    python scripts/preview_sample_reports.py            # into dev_local/reports/_pdf_preview
    python scripts/preview_sample_reports.py out/dir

Produces four documents, because a design that only holds up on one page is
not a design:

    missing_short.pdf   the formal missing-homework report, a few students
    missing_long.pdf    ~75 students, 14 assignments - forces real page
                        breaks, repeated table headers and running headers
    class_full.pdf      the full class report, every section
    correction.pdf      a single correction - score summary, question-level
                        results, feedback and marking detail
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.api import report_routes  # noqa: E402
from app.reporting import class_report as CR  # noqa: E402
from app.reporting import design as D  # noqa: E402

OUT = (pathlib.Path(__file__).resolve().parents[2]
       / "dev_local" / "reports" / "_pdf_preview")

FIRST = ["Yassin", "Malak", "Kenda", "Karam", "Mostafa", "Seif", "Taha",
         "Eyad", "Lara", "Moaz", "Rokaya", "Hazem", "Mariam", "Abdulrahman",
         "Alaa", "Dania", "Deema", "Jory", "Youssef", "Nour", "Ziad", "Hana",
         "Omar", "Farida", "Adham", "Lina", "Sara", "Talia", "Yehia", "Rana"]
LAST = ["Shady", "Ahmed", "Mohamed", "Fawzy", "Ozo", "Ihab", "Sameh",
        "Ibrahim", "Mahmoud", "El Abyad", "Yasier", "Fahmy", "Essam"]


def _codes(count: int) -> list[CR.AssignmentCode]:
    return [CR.AssignmentCode(
        code=f"L{lesson}", chapter=str((lesson - 1) // 4 + 1),
        lesson=str(lesson), topic=f"Topic {lesson}",
        title=f"Chapter {(lesson - 1) // 4 + 1} Lesson {lesson}",
        coursework_id=f"cw-l{lesson}", max_points=20)
        for lesson in range(1, count + 1)]


def build_data(student_count: int, lesson_count: int, seed: int = 7) -> CR.ClassReportData:
    """A plausible class: every student gets a real mix of outcomes."""
    codes = _codes(lesson_count)
    groups = []
    for class_index, label in enumerate(("10A", "10B", "10C")):
        size = max(3, student_count // 3
                   + (1 if class_index < student_count % 3 else 0))
        group = CR.Group(class_label=label)
        for n in range(size):
            name = (f"{FIRST[(seed + n * 3 + class_index) % len(FIRST)]} "
                    f"{LAST[(seed + n + class_index) % len(LAST)]}")
            grades, missing = [], []
            for position, code in enumerate(codes):
                pick = (seed + n * 5 + position * 3 + class_index) % 7
                entry = {"code": code.code, "chapter": code.chapter,
                         "lesson": code.lesson, "label": code.label}
                if pick in (0, 1):
                    missing.append({**entry, "status_label": "Missing"})
                elif pick == 2:
                    grades.append({**entry, "grade": "1", "grade_label": "1",
                                   "submitted": True, "is_draft": False})
                elif pick == 3:
                    grades.append({**entry, "grade": "3", "grade_label": "3",
                                   "submitted": True, "is_draft": False})
                else:
                    grade = str(6 + pick % 3)
                    grades.append({**entry, "grade": grade, "grade_label": grade,
                                   "submitted": True, "is_draft": False})
            group.students.append({"student_id": f"s{label}{n}",
                                   "display_name": name, "grades": grades})
            if missing:
                group.missing.append({"student_id": f"s{label}{n}",
                                      "display_name": name, "class_label": label,
                                      "assignments": missing})
        groups.append(group)
    return CR.ClassReportData(
        course={"id": "course-review-0001"}, groups=groups, codes=codes,
        generated_at=time.time(), threshold=CR.DEFAULT_LOW_THRESHOLD)


def render(data: CR.ClassReportData, *, report_type: str, detailed: bool,
           title: str) -> bytes:
    """The SAME renderer the API hands a finished model to."""
    return CR.render_pdf(data, D.load_branding(), title=title,
                         report_type=report_type, detailed=detailed).getvalue()


def correction_sample() -> bytes:
    """One correction: score summary, per-question results and feedback."""
    marks = [(8, 8, "Fully correct working, method clearly set out."),
             (5, 7, "Correct approach; sign slip in the factorisation."),
             (10, 10, "Correct, and the units are carried through."),
             (0, 6, "Formula not recalled; no attempt at substitution."),
             (3, 4, "Right method, one arithmetic slip in the final line."),
             (6, 6, "Correct, fully justified."),
             (2, 6, "Partial: first two marks for method only."),
             (7, 8, "Correct answer, working not fully shown.")]
    questions = [{
        "question_number": number, "awarded_marks": awarded,
        "max_marks": maximum,
        "loss_reason": comment if awarded < maximum else "",
        "correct_method": "Method per the mark scheme.",
        "student_answer": "Student working as submitted.",
        "mark_breakdown": [{"code": f"{number}a", "awarded": True,
                            "reason": "Correct start"},
                           {"code": f"{number}b", "awarded": awarded >= maximum,
                            "reason": "Final answer"}],
        "full_mark_solution": "Full solution per the mark scheme."}
        for number, (awarded, maximum, comment) in enumerate(marks, 1)]
    earned, available = (sum(q[0] for q in marks), sum(q[1] for q in marks))
    result = {
        "id": "res-0011aa22", "display_name": "Yassin Shady",
        "class_label": "10A", "created_at": time.time(),
        "marking_model": "gemini-2.5-flash",
        "earned": earned, "available": available,
        "percentage": round(100.0 * earned / available, 1),
        "result_json": {"questions": questions, "overall_notes": {
            "summary": "Solid paper with two avoidable losses.",
            "strengths": ["Method is clear throughout",
                          "Full marks on the algebra questions"],
            "revision_areas": ["Recall of standard formulae",
                               "Carrying working through to the final line"]}},
        "verification_json": {"status": "OK"},
    }
    return report_routes.render_result_pdf(result, detail="detailed").getvalue()


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else OUT
    out.mkdir(parents=True, exist_ok=True)

    documents = {
        "missing_short.pdf": render(
            build_data(student_count=9, lesson_count=4),
            report_type="missing", detailed=True,
            title="MISSING HOMEWORK REPORT"),
        "missing_long.pdf": render(
            build_data(student_count=75, lesson_count=14, seed=3),
            report_type="missing", detailed=False,
            title="MISSING HOMEWORK REPORT"),
        "class_full.pdf": render(
            build_data(student_count=30, lesson_count=8, seed=11),
            report_type="class", detailed=True, title="CLASS REPORT"),
        "correction.pdf": correction_sample(),
    }

    import pymupdf
    for name, data in documents.items():
        path = out / name
        path.write_bytes(data)
        pages = pymupdf.open(path).page_count
        print(f"wrote {path} ({pages} page(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
