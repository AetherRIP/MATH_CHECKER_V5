"""Deterministic post-verification — replaces the legacy AI audit pass.

The question marks themselves always come from the legacy Gemini pipeline
untouched. This module then re-checks the arithmetic and structure in pure
Python: totals recomputation, mark clamping, missing-question detection and
consistency flags — so the teacher sees verified numbers plus any anomalies
worth a human glance.

HARD RULES (V5):
* This module NEVER changes a mark. It reports; it does not rewrite.
* Missing internal metadata (mark_breakdown absent, evidence codes absent,
  blueprint absent) is a REVIEW signal, never a zero. A technical gap must
  never be able to turn a correct answer into 0.
* The three different situations stay separate:
    1. the student failed to satisfy the mark scheme  -> marks say so
    2. the model failed to provide evidence           -> warnings + REVIEW
    3. internal metadata/structure is missing         -> anomalies + REVIEW
"""

from __future__ import annotations

from . import legacy_marking as legacy

STATUS_VALID = "VALID"
STATUS_REVIEW = "REVIEW"
STATUS_BLOCKED = "BLOCKED"   # no trustworthy result at all (job failed)


def partial_summary(verification: dict | None) -> dict | None:
    """A compact "this correction is INCOMPLETE" block for list surfaces.

    Returns ``None`` for a complete correction, so callers can simply test the
    value. A partial correction has no publishable score: the UI must show
    which questions are missing instead of a percentage that reads as a mark.
    """
    if not isinstance(verification, dict):
        return None
    # Rows written before the explicit flag existed are still partial when the
    # score was computed on a marked subset or marks are unresolved.
    if not verification.get("partial"):
        unresolved_marks = int(verification.get("unresolved_marks") or 0)
        if (verification.get("score_basis") != "MARKED_ONLY"
                and unresolved_marks <= 0):
            return None
    coverage = verification.get("coverage") or {}
    marked = coverage.get("marked")
    expected = coverage.get("expected")
    unresolved_q = coverage.get("requires_review")
    if marked is None:
        # Legacy partial row: derive the counts from what it did record.
        expected = int(verification.get("verified_questions") or 0) or None
        unresolved_q = len(verification.get("unresolved_questions") or [])
        marked = (max(0, expected - unresolved_q)
                  if expected else 0)
    return {
        "marked_questions": int(marked or 0),
        "expected_questions": int(expected or 0),
        "unresolved_questions": int(unresolved_q or 0),
        "paper_marks": verification.get("paper_total_marks"),
        "unresolved_marks": int(verification.get("unresolved_marks") or 0),
        "earned": int(verification.get("earned") or 0),
        "marked_available": int(verification.get("available") or 0),
    }


def _over(question: dict) -> bool:
    """Does this row claim more marks than its own maximum?"""
    try:
        return float(question.get("awarded_marks") or 0) > \
            float(question.get("max_marks") or 0)
    except (TypeError, ValueError):
        return False


def _add_warning(warnings: list[str], text: str) -> None:
    """Append a warning once. The same condition can be reached twice (a
    question-level reason and a blueprint-level one); reporting it twice reads
    as two separate problems."""
    if text and text not in warnings:
        warnings.append(text)


def verify_result(result: dict, blueprint_report: dict | None = None) -> dict:
    """Return the legacy result plus a ``verification`` report.

    Recomputes totals with the legacy ``calculate_totals`` (which reads
    per-question awarded/max marks) and compares against any totals the
    pipeline stored; never alters question-level marks.

    ``earned`` is the sum the questions actually claim, even when that exceeds
    ``available`` (a broken model response can award 5 marks out of 4). It is
    NOT clamped here — silently lowering a mark is exactly what this module
    must never do — so a score above the paper's maximum stays visible and is
    reported as ``over_available`` with an explicit warning instead.
    """
    anomalies: list[str] = []
    warnings: list[str] = []

    questions = result.get("questions", [])

    # 1. Structural sanity.
    if not questions:
        anomalies.append("No questions were detected in the marking result.")

    for q in questions:
        number = q.get("question_number", "?")
        max_marks = q.get("max_marks", 0) or 0
        awarded = q.get("awarded_marks", 0) or 0
        if max_marks <= 0:
            warnings.append(
                f"Question {number} has max_marks = 0; it contributes nothing to the total."
            )
        # §5 question-level validation: 0 <= awarded <= max. A violation is a
        # REVIEW signal (never a silent clamp) so a broken model response can
        # never fabricate or destroy marks.
        if awarded < 0:
            anomalies.append(
                f"Question {number} has a negative awarded mark ({awarded}); "
                f"marks must be between 0 and {max_marks}.")
        elif max_marks > 0 and awarded > max_marks:
            anomalies.append(
                f"Question {number} awarded {awarded} marks but its maximum is "
                f"{max_marks} — the per-question marks do not add up. "
                f"A teacher must review before the grade is used.")

    # 2. Recompute totals exactly as the legacy engine does.
    earned, available, percentage = legacy.calculate_totals(result)
    provisional_grade = legacy.percentage_band_grade(percentage)

    stored = result.get("totals") or {}
    stored_earned = stored.get("earned")
    if stored_earned is not None and stored_earned != earned:
        anomalies.append(
            f"Stored total {stored_earned} disagreed with recomputed total {earned}; "
            f"the recomputed value is authoritative."
        )

    # 3. Consistency of per-question breakdowns. Absence of a breakdown is
    #    NOT an error — it is simply "no M/A/B metadata", which must never
    #    affect any mark (it only shows up as an informational note).
    without_evidence = 0
    for q in questions:
        breakdown = q.get("mark_breakdown") or []
        if not breakdown:
            without_evidence += 1
            continue
        awarded_flags = sum(1 for item in breakdown if item.get("awarded"))
        max_marks = q.get("max_marks", 0)
        if max_marks and awarded_flags > max_marks:
            warnings.append(
                f"Question {q.get('question_number')} has more awarded M/A/B codes "
                f"({awarded_flags}) than max marks ({max_marks})."
            )
    if without_evidence:
        warnings.append(
            f"{without_evidence} question(s) have no M/A/B breakdown metadata "
            f"(marks unchanged — metadata is informational only)."
        )

    # 4. Blueprint reconciliation report (mark-scheme control).
    review_questions: list[str] = []
    if blueprint_report:
        for number in blueprint_report.get("missing_questions", []):
            anomalies.append(
                f"Question {number} is in the official mark scheme but the marker "
                f"returned no answer for it — it is UNMARKED (not a confirmed 0)."
            )
        for number in blueprint_report.get("extra_questions", []):
            warnings.append(
                f"Question {number} was marked but is not in the mark scheme "
                f"blueprint — verify before trusting it."
            )
        for entry in blueprint_report.get("max_marks_corrected", []):
            warnings.append(
                f"Question {entry['question']} max marks corrected to the mark "
                f"scheme value {entry['mark_scheme']} (model said {entry['model']})."
            )
        # SUBQUESTION-level gaps (§4/§8/§9): the question came back, one of its
        # marking points did not. That is NOT a zero, and it is not complete
        # either — it needs a human look at THAT subquestion, not the paper.
        for entry in blueprint_report.get("missing_subquestions", []):
            anomalies.append(
                f"Subquestion {entry.get('label') or entry.get('subquestion')} "
                f"of question {entry.get('question')} is declared by the mark "
                f"scheme but the marker returned nothing for it — it is "
                f"UNMARKED (not a confirmed 0).")
        for entry in blueprint_report.get("subquestion_conflicts", []):
            anomalies.append(
                f"{len(entry.get('rows') or [])} returned rows claim "
                f"subquestion {entry.get('subquestion')} of question "
                f"{entry.get('question')} "
                f"({', '.join(str(r) for r in entry.get('rows') or [])}). "
                f"Which row is real cannot be established from the result; "
                f"review it rather than trusting either.")
        for entry in blueprint_report.get("unattributed_rows", []):
            warnings.append(
                f"Question {entry.get('question')}: {entry.get('rows')} "
                f"returned row(s) name no subquestion although the mark scheme "
                f"declares them — the marking point each one covers is unknown.")
        for entry in blueprint_report.get("unmatched_rows", []):
            warnings.append(
                f"The marker returned "
                f"'{entry.get('question_number')}' but "
                f"{entry.get('reason', 'the mark scheme does not declare it')}.")
        for entry in blueprint_report.get("award_clamped", []):
            warnings.append(
                f"Awarded marks were clamped to the maximum for "
                f"{entry.get('subquestion') or entry.get('question')} "
                f"({entry.get('awarded')} > {entry.get('max')}).")

    # 5. Per-question status aggregation (absent status = legacy result = valid).
    for q in questions:
        q_status = q.get("status")
        if q_status and q_status != STATUS_VALID:
            review_questions.append(str(q.get("question_number")))
            reason = q.get("review_reason")
            if reason:
                warnings.append(f"Question {q.get('question_number')}: {reason}")

    # A disagreement whose recommendation contradicts the actual award, or
    # whose evidence is ambiguous, must reach the existing review gate. Do not
    # silently rewrite marks from a self-reported confidence value.
    from .scheme_audit import classify, STATE_UNCERTAIN
    for q in questions:
        check = classify(q.get("scheme_check"), max_marks=q.get("max_marks", 0),
                         awarded=q.get("awarded_marks", 0))
        if check and (check["state"] == STATE_UNCERTAIN or not check["reasoning"]
                      or (check["justified_marks"] is not None and
                          check["justified_marks"] != q.get("awarded_marks"))):
            review_questions.append(str(q.get("question_number")))
            warnings.append(f"Question {q.get('question_number')}: mathematical discrepancy needs a human marking decision.")

    # 6. Legacy uncertainties surface as warnings, not anomalies.
    for note in result.get("uncertainties", []) or []:
        warnings.append(f"Marker flagged: {note}")

    # 6b. The mark scheme's OWN verification verdict travels with the result.
    #
    # A scheme the app itself could not verify (NEEDS_REPAIR / NEEDS_MANUAL_
    # REVIEW) makes the whole correction untrusted.  This used to append a
    # warning while leaving the result status VALID, so TEST 2/3 were stored
    # with review_required=0 even though the blueprint had 37 anomalies and
    # explicitly said "no expected answer" for every question.
    #
    # The audit that decides whether the extracted structure is trustworthy
    # records NEEDS_REPAIR (or NEEDS_MANUAL_REVIEW) when its independent checks
    # did not confirm the scheme — a printed total that disagrees with its
    # parts, a page range that could not be read, and so on. That verdict
    # reached the stored result only as an unread field inside
    # ``verification.blueprint``, so a paper marked against a scheme the app had
    # itself refused to vouch for read as an ordinary, valid correction. It is
    # surfaced here as a warning — appended after the per-question reasons so
    # the existing warning order is unchanged — with the marks untouched.
    scheme_review_required = False
    if blueprint_report:
        scheme_state = str(blueprint_report.get("verification_state")
                           or "").upper()
        completeness = blueprint_report.get("completeness") or {}
        scheme_review_required = bool(
            blueprint_report.get("review_required")
            or blueprint_report.get("review_warnings"))
        if scheme_state in ("NEEDS_REPAIR", "NEEDS_MANUAL_REVIEW",
                            "REPAIRING", "REVERIFYING"):
            scheme_review_required = True
            detail = ""
            verdict_text = str(completeness.get("verdict") or "").strip()
            if completeness.get("unresolved") and verdict_text:
                detail = f" ({verdict_text})"
            _add_warning(
                warnings,
                f"The mark scheme structure this paper was marked against is "
                f"{scheme_state}{detail}: the app's own check did not confirm "
                f"the scheme. The marks are unchanged, but verify the scheme "
                f"before the grade is used.")

    # 7. A score above the paper's maximum can only come from a question
    #    claiming more than its own allowance. It is REPORTED, never clamped:
    #    the awarded number stays exactly what the marking produced, and the
    #    difference is named so "8 / 7" can never be read as a routine mark.
    over_available = max(0, earned - available)
    if over_available:
        offenders = [
            f"question {q.get('question_number')} "
            f"({q.get('awarded_marks')} of {q.get('max_marks', 0)})"
            for q in questions
            if _over(q)
        ]
        _add_warning(
            warnings,
            f"The awarded total ({earned:g}) exceeds the {available:g} marks "
            f"available by {over_available:g}"
            + (f" — {', '.join(offenders)}" if offenders else "")
            + ". No mark has been changed: this correction needs a human "
              "decision before the score is used.")

    status = (STATUS_VALID if not anomalies and not review_questions
              and not scheme_review_required else STATUS_REVIEW)

    return {
        "status": status,
        "review_required": status == STATUS_REVIEW,
        "verified": len(anomalies) == 0,
        # earned/available are the awarded and maximum sums as they stand;
        # over_available is the part of `earned` that has no marks behind it.
        "earned": earned,
        "available": available,
        "over_available": over_available,
        "percentage": round(percentage, 2),
        "provisional_grade": provisional_grade,
        "anomalies": anomalies,
        "warnings": warnings,
        "verified_questions": len(questions),
        "review_questions": sorted(set(review_questions)),
        "blueprint": blueprint_report or None,
    }
