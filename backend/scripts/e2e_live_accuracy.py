"""Real end-to-end live-accuracy test (required by the live-audit task).

Runs the REAL pipeline on the REAL fixture submission:
    original submission preserved -> validate -> AI correction #1
    -> structured question marks -> validate -> (>=95% -> independent
    correction #2) -> deterministic score -> deterministic 1-9 grade
    -> generate report -> validate report (NO upload, NO Classroom grade)
and prints the full trace plus a REVIEW-MODE hold confirmation.

The student's original file is opened read-only (it is never rewritten).
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
DATA = BACKEND.parent / "data"


def main() -> int:
    real_dir = DATA / "fixtures" / "real"
    student = real_dir / "Student Answer.pdf"
    scheme = real_dir / "Answer Sheet.pdf"
    question = real_dir / "Question paper.pdf"
    for p in (student, scheme, question):
        if not p.exists():
            print(f"MISSING fixture: {p}")
            return 1

    # ---- 1. ORIGINAL SUBMISSION PRESERVED: fingerprint before/after ----------
    original_sha = hashlib.sha256(student.read_bytes()).hexdigest()
    original_size = student.stat().st_size

    from app.config import get_config
    cfg = get_config()
    review_mode_note = cfg.automation.review_mode
    print("REVIEW MODE (grade hold):", "ENABLED" if review_mode_note else "off")
    print("Double-check threshold:", cfg.double_check.threshold_percentage)

    from app.db import init_db
    init_db()

    from app.engine import blueprint as bp
    from app.engine import legacy_marking as legacy
    from app.engine import double_check as dc
    from app.engine.verify import verify_result
    from app.engine.submission_validation import assess_submission_quality
    from app.engine.result_guard import assess_confidence
    from app.engine.grading import grade_for_totals, describe_boundaries
    from app.gemini.client_proxy import KeyAwareClient
    from app.gemini.key_manager import KeyManager

    km = KeyManager()
    client = KeyAwareClient(km, job_id="e2e-live-test")

    # ---- 2. mark-scheme structure (blueprint) --------------------------------
    blueprint, bp_meta = bp.get_blueprint(client, scheme)
    struct = bp.structure_report(blueprint)
    bp_conf = bp.mark_scheme_confidence(struct)
    print("\n== MARK SCHEME STRUCTURE ==")
    print("source:", bp_meta.get("source"),
          "| sha:", (bp_meta.get("sha256") or "")[:12])
    print("questions:", struct["question_count"], "total:", struct["total_marks"])
    print("complete:", struct["complete"], "| confidence:", bp_conf["confidence"])
    if bp_conf["reasons"]:
        print("structure reasons:", bp_conf["reasons"])
    if blueprint is None:
        print("STOP: no trustworthy mark-scheme structure (§8: never guess max).")
        return 1

    # ---- 3. AI correction #1 (original evidence only) ------------------------
    remote_students = [client.upload_file(str(student))]
    remote_scheme = client.upload_file(str(scheme))
    remote_question = client.upload_file(str(question))
    try:
        t0 = time.time()
        raw1, model1 = legacy.grade_with_model(
            client=client,
            model_names=list(cfg.gemini.primary_models),
            student_file=remote_students,
            mark_scheme_file=remote_scheme,
            question_file=remote_question,
            blueprint=blueprint,
        )
        result1 = legacy.normalise_result(raw1)
        result1, report1 = bp.apply_blueprint(result1, blueprint)
        e1, a1, p1 = legacy.calculate_totals(result1)
        v1 = verify_result(result1, report1)
        print(f"\n== FIRST CORRECTION == {e1}/{a1} = {p1:.4f}% (model {model1}, "
              f"{time.time()-t0:.0f}s)  status: {v1['status']}")
        for q in result1["questions"]:
            print(f"  Q{q['question_number']}: {q['awarded_marks']}/{q['max_marks']}"
                  + (f"  [{q.get('loss_reason') or ''}]"[:90]))

        # ---- 4. submission-quality validation ---------------------------------
        quality = assess_submission_quality(result1, v1)
        print("\n== SUBMISSION QUALITY ==", quality["quality"])
        for r in quality["reasons"]:
            print("  -", r)

        # ---- 5. independent correction #2 (>=95% unrounded) -------------------
        second = None
        triggered = dc.should_trigger(e1, a1)
        print("\n== HIGH-SCORE DOUBLE-CHECK ==", "TRIGGERED" if triggered
              else "NOT triggered")
        if triggered:
            raw2, model2 = legacy.grade_with_model(
                client=client,
                model_names=list(cfg.gemini.primary_models),
                student_file=remote_students,
                mark_scheme_file=remote_scheme,
                question_file=remote_question,
                blueprint=blueprint,
                sampling=dc.SECOND_PASS_SAMPLING,
            )
            second = legacy.normalise_result(raw2)
            second, report2 = bp.apply_blueprint(second, blueprint)
            e2, a2, p2 = legacy.calculate_totals(second)
            print(f"== SECOND CORRECTION == {e2}/{a2} = {p2:.4f}% (model {model2})")
            diffs = dc.combine(result1, second)["question_differences"]
            if diffs:
                print("== QUESTION-LEVEL DIFFERENCES ==")
                for d in diffs:
                    print(f"  Q{d['question_number']}: {d['first_awarded']}"
                          f" vs {d['second_awarded']} (max {d['max_marks']})"
                          f" | first: {d['first_loss_reason'][:60]}"
                          f" | second: {d['second_loss_reason'][:60]}")
            else:
                print("  (identical per-question marks)")
            decision = dc.combine(result1, second)
            print("== DECISION ==", decision["status"],
                  "|", decision["reason"])
            final_result = decision["final_result"]
        else:
            final_result = result1

        eF, aF, pF = legacy.calculate_totals(final_result)
        vF = verify_result(final_result)
        confidence, reasons = assess_confidence(final_result, vF, eF, aF)
        if quality["review"]:
            reasons = reasons + [
                f"Submission quality ({quality['quality']}): {r}"
                for r in quality["reasons"]]
        print("\n== FINAL PROPOSED SCORE ==", f"{eF}/{aF} = {pF:.4f}%")

        # ---- 6. deterministic 1-9 grade ------------------------------------
        grade, pct = grade_for_totals(eF, aF)
        print("== DETERMINISTIC GRADE ==", grade,
              "| boundaries:", describe_boundaries())
        print("== CONFIDENCE ==", confidence)
        for r in reasons:
            print("  -", r)

        # ---- 7. report generation + validation (no upload) --------------------
        try:
            from app.classroom.report_attachment import render_report_pdf
            from app.automation.pdf_validation import validate_report_pdf
            fake_row = {
                "student_name": "E2E Live Test Student",
                "earned": eF, "available": aF, "percentage": pF,
                "provisional_grade": grade,
                "result_json": final_result,
                "created_at": time.time(),
            }
            pdf_bytes = render_report_pdf(fake_row, detail="standard")
            validation = validate_report_pdf(
                pdf_bytes, expected_text=["E2E Live Test Student"])
            print("\n== REPORT ==", len(pdf_bytes), "bytes | valid:",
                  validation.get("ok"), "| pages:", validation.get("pages"))
        except Exception as exc:  # noqa: BLE001
            print("report step (offline check) error:", exc)

    finally:
        for remote in [r for r in
                       [*remote_students, remote_scheme, remote_question]
                       if r is not None]:
            try:
                client.files_delete(remote)
            except Exception:
                pass

    # ---- 8. original must be untouched ---------------------------------------
    after_sha = hashlib.sha256(student.read_bytes()).hexdigest()
    print("\n== ORIGINAL SUBMISSION PRESERVED ==",
          after_sha == original_sha and student.stat().st_size == original_size)
    print("\nNOTE: no Classroom grade was written and no report was uploaded —"
          " the proposed result above is a DRAFT for teacher review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
