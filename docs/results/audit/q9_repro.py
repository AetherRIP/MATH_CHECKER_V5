"""Reproduce the live Q9 disagreement (0 then 2) against the REAL engine code,
then demonstrate the fix.

Run from `backend/`. Two "runs" of the same paper differ only in whether the
model returned a row for Q3:
  * run A omits Q3   -> the pipeline inserts a technical-gap placeholder
  * run B returns Q3 -> a normal mark
and the two totals differ by exactly the omitted question's marks.

Before the fix the omission could not be healed without a cached mark-scheme
file, so the placeholder 0 was persisted as if it were a score. After the fix
the narrow retry builds from the in-memory blueprint and the two runs agree.

Exit 0 = the two runs now agree.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.getcwd())

from app.engine import blueprint as bp          # noqa: E402
from app.engine import legacy_marking as legacy  # noqa: E402
from app.engine import recover_missing           # noqa: E402
from app.engine.question_inventory import narrow_blueprint  # noqa: E402

BLUEPRINT = {
    "questions": [
        {"question_number": "1", "max_marks": 3},
        {"question_number": "2", "max_marks": 2},
        {"question_number": "3", "max_marks": 2},
    ]
}


def _row(number, awarded, maximum):
    return {
        "question_number": number, "max_marks": maximum,
        "awarded_marks": awarded, "topic": "t", "student_answer": "a",
        "working_summary": "w", "loss_reason": "" if awarded == maximum else "slip",
        "mark_breakdown": [], "correct_method": "c", "full_mark_solution": "f",
    }


def _run(include_q3: bool):
    """One marking run's model payload, normalised and reconciled."""
    rows = [_row("1", 3, 3), _row("2", 2, 2)]
    if include_q3:
        rows.append(_row("3", 2, 2))
    payload = json.dumps({
        "paper_info": {"qualification": "Q", "paper": "P", "session": "S"},
        "questions": rows,
    })
    result = legacy.normalise_result(json.loads(payload))
    result, report = bp.apply_blueprint(result, BLUEPRINT)
    return result, report


def main() -> int:
    print("=" * 72)
    print("RUN A: the model omitted Q3 (the observed run-1 behaviour)")
    result_a, report_a = _run(include_q3=False)
    q3a = next(q for q in result_a["questions"] if q["question_number"] == "3")
    earned_a, avail_a, _ = legacy.calculate_totals(result_a)
    print(f"  Q3 row -> awarded={q3a['awarded_marks']} max={q3a['max_marks']} "
          f"status={q3a['status']!r}")
    print(f"  Q3 loss_reason   = {str(q3a['loss_reason'])[:58]!r}")
    print(f"  missing_questions = {report_a['missing_questions']}")
    print(f"  TOTAL = {earned_a}/{avail_a}")

    print("=" * 72)
    print("RUN B: the model returned Q3 (the observed run-2 behaviour)")
    result_b, report_b = _run(include_q3=True)
    earned_b, avail_b, _ = legacy.calculate_totals(result_b)
    print(f"  missing_questions = {report_b['missing_questions']}")
    print(f"  TOTAL = {earned_b}/{avail_b}")

    print("=" * 72)
    print(f"DISAGREEMENT: Q3 0 then 2   (totals {earned_a} vs {earned_b})")
    print("(identical input; the only difference is whether the model emitted Q3)")

    print("=" * 72)
    print("BEFORE THE FIX: the targeted retry needs a narrow prompt")
    print(f"  narrow_blueprint(None, ['3'])       -> {narrow_blueprint(None, ['3'])!r}")

    class DummyClient:
        records_usage = True

        class models:  # noqa: N801
            @staticmethod
            def generate_content(**_kw):
                raise AssertionError("must not reach the model")

    refused = recover_missing.run_recovery_round(
        client=DummyClient(), missing=["3"],
        student_remote="s", mark_scheme_remote="m", mark_scheme_path=None)
    print(f"  run_recovery_round(mark_scheme_path=None) -> ok={refused.ok} "
          f"still_missing={refused.still_missing}")
    print(f"     error: {refused.error}")
    print("  => without a cached scheme FILE the placeholder 0 was persisted "
          "as a mark.")

    print("=" * 72)
    print("AFTER THE FIX")
    built = narrow_blueprint(None, ["3"], blueprint=BLUEPRINT)
    print(f"  narrow_blueprint(None, ['3'], blueprint=<in-memory>) -> "
          f"{'built' if built else 'None'}")
    built2 = recover_missing.build_narrow_blueprint(None, ["3"],
                                                    blueprint=BLUEPRINT)
    print(f"  build_narrow_blueprint(None, ['3'], blueprint=...) -> "
          f"{'built' if built2 else 'None'}")

    class AnsweringClient:
        records_usage = True

        class models:  # noqa: N801
            @staticmethod
            def generate_content(**_kw):
                payload = {"questions": [_row("3", 2, 2)]}

                class _Resp:
                    text = json.dumps(payload)

                return _Resp()

    healed = recover_missing.run_recovery_round(
        client=AnsweringClient(), missing=["3"],
        student_remote="s", mark_scheme_remote="m",
        mark_scheme_path=None, blueprint=BLUEPRINT)
    print(f"  run_recovery_round(no path, in-memory blueprint) -> ok={healed.ok} "
          f"recovered={sorted(healed.recovered)}")

    # Merge it the way the worker child now does, then re-total.
    for q in result_a["questions"]:
        if q["question_number"] == "3":
            q.clear()
            q.update(healed.recovered["3"])
    result_a, _ = bp.apply_blueprint(result_a, BLUEPRINT)
    earned_a2, avail_a2, _ = legacy.calculate_totals(result_a)
    print(f"  healed TOTAL = {earned_a2}/{avail_a2}   (run B was {earned_b}/{avail_b})")
    same = (earned_a2, avail_a2) == (earned_b, avail_b)
    print()
    print(f"  => VERDICT: the two runs now AGREE: {same}")
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
