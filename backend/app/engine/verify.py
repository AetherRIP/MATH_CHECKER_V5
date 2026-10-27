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


#: Reason codes. A review that cannot say WHY it exists is indistinguishable
#: from a bug, so every escalation carries one of these instead of only prose.
R_NEGATIVE_MARK = "NEGATIVE_MARK"
R_MARK_ABOVE_MAXIMUM = "MARK_ABOVE_MAXIMUM"
R_STORED_TOTAL_MISMATCH = "STORED_TOTAL_MISMATCH"
R_OVER_AVAILABLE = "OVER_AVAILABLE"
R_QUESTION_UNMARKED = "QUESTION_UNMARKED"
R_SUBQUESTION_UNMARKED = "SUBQUESTION_UNMARKED"
R_SUBQUESTION_CONFLICT = "SUBQUESTION_CONFLICT"
R_QUESTION_FLAGGED = "QUESTION_FLAGGED"
R_SCHEME_DISAGREEMENT = "SCHEME_DISAGREEMENT"
R_SCHEME_CONTRADICTED = "MARK_SCHEME_CONTRADICTED"
A_SCHEME_UNVERIFIED = "MARK_SCHEME_UNVERIFIED"
A_EXTRA_QUESTION = "EXTRA_QUESTION"
A_MAX_MARKS_CORRECTED = "MAX_MARKS_CORRECTED"
A_UNATTRIBUTED_ROWS = "UNATTRIBUTED_ROWS"
A_UNMATCHED_ROW = "UNMATCHED_ROW"
A_AWARD_CLAMPED = "AWARD_CLAMPED"
A_MISSING_BREAKDOWN = "MISSING_BREAKDOWN"
A_ZERO_MAX_MARKS = "ZERO_MAX_MARKS"
A_MARKER_UNCERTAINTY = "MARKER_UNCERTAINTY"
A_CODE_COUNT = "CODE_COUNT_OVER_MAX"

#: Which pipeline stage produced the signal. The review screen groups by this,
#: so "the marker was unsure" and "the extraction lost a question" never read
#: as the same kind of problem.
STAGE_EXTRACTION = "extraction"
STAGE_MAPPING = "mapping"
STAGE_MARKING = "marking"
STAGE_VALIDATION = "validation"
STAGE_SCHEME = "mark_scheme"

SEVERITY_REVIEW = "review"
SEVERITY_ADVISORY = "advisory"


def _flag(reasons: list[dict], code: str, stage: str, detail: str, *,
          question=None, severity: str = SEVERITY_REVIEW,
          action: str | None = None) -> None:
    """Record one coded reason: what happened, where, and what to do about it."""
    entry = {
        "code": code,
        "stage": stage,
        "severity": severity,
        "question": None if question is None else str(question),
        "detail": detail,
        "action": action,
    }
    if entry not in reasons:
        reasons.append(entry)


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
    reasons: list[dict] = []

    questions = result.get("questions", [])

    # 1. Structural sanity.
    if not questions:
        anomalies.append("No questions were detected in the marking result.")
        _flag(reasons, "NO_QUESTIONS", STAGE_EXTRACTION,
              "The marking run returned no questions at all.",
              action="Check the uploaded PDF pages, then correct the paper again.")

    for q in questions:
        number = q.get("question_number", "?")
        max_marks = q.get("max_marks", 0) or 0
        awarded = q.get("awarded_marks", 0) or 0
        if max_marks <= 0:
            warnings.append(
                f"Question {number} has max_marks = 0; it contributes nothing to the total."
            )
            _flag(reasons, A_ZERO_MAX_MARKS, STAGE_MAPPING,
                  f"Question {number} has no maximum marks, so it cannot affect the total.",
                  question=number, severity=SEVERITY_ADVISORY,
                  action="Check the mark scheme declares a mark allocation for it.")
        # §5 question-level validation: 0 <= awarded <= max. A violation is a
        # REVIEW signal (never a silent clamp) so a broken model response can
        # never fabricate or destroy marks.
        if awarded < 0:
            anomalies.append(
                f"Question {number} has a negative awarded mark ({awarded}); "
                f"marks must be between 0 and {max_marks}.")
            _flag(reasons, R_NEGATIVE_MARK, STAGE_VALIDATION,
                  f"Question {number} returned a negative mark ({awarded}).",
                  question=number,
                  action="Set the correct mark by hand; the stored mark is unchanged.")
        elif max_marks > 0 and awarded > max_marks:
            anomalies.append(
                f"Question {number} awarded {awarded} marks but its maximum is "
                f"{max_marks} — the per-question marks do not add up. "
                f"A teacher must review before the grade is used.")
            _flag(reasons, R_MARK_ABOVE_MAXIMUM, STAGE_VALIDATION,
                  f"Question {number} awarded {awarded} of {max_marks} marks.",
                  question=number,
                  action="Award within the question's maximum.")

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
        _flag(reasons, R_STORED_TOTAL_MISMATCH, STAGE_VALIDATION,
              f"The stored total ({stored_earned}) differed from the sum of the "
              f"questions ({earned}); the sum is used.",
              action="No action needed unless the question marks look wrong.")

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
            _flag(reasons, A_CODE_COUNT, STAGE_MARKING,
                  f"Question {q.get('question_number')} lists {awarded_flags} awarded "
                  f"marking points for {max_marks} marks.",
                  question=q.get("question_number"), severity=SEVERITY_ADVISORY)
    if without_evidence:
        warnings.append(
            f"{without_evidence} question(s) have no M/A/B breakdown metadata "
            f"(marks unchanged — metadata is informational only)."
        )
        _flag(reasons, A_MISSING_BREAKDOWN, STAGE_MARKING,
              f"{without_evidence} question(s) carry no marking-point breakdown; "
              f"the marks are unaffected.",
              severity=SEVERITY_ADVISORY,
              action="Nothing to do — the breakdown is informational only.")

    # 4. Blueprint reconciliation report (mark-scheme control).
    review_questions: list[str] = []
    if blueprint_report:
        for number in blueprint_report.get("missing_questions", []):
            anomalies.append(
                f"Question {number} is in the official mark scheme but the marker "
                f"returned no answer for it — it is UNMARKED (not a confirmed 0)."
            )
            _flag(reasons, R_QUESTION_UNMARKED, STAGE_MAPPING,
                  f"Question {number} is in the mark scheme but the marking run "
                  f"returned nothing for it.",
                  question=number,
                  action="Re-correct the paper; if it persists, check the scan "
                         "covers that page.")
        for number in blueprint_report.get("extra_questions", []):
            warnings.append(
                f"Question {number} was marked but is not in the mark scheme "
                f"blueprint — verify before trusting it."
            )
            _flag(reasons, A_EXTRA_QUESTION, STAGE_MAPPING,
                  f"Question {number} was marked but the mark scheme does not "
                  f"declare it.",
                  question=number, severity=SEVERITY_ADVISORY,
                  action="Check the question number against the printed paper.")
        for entry in blueprint_report.get("max_marks_corrected", []):
            warnings.append(
                f"Question {entry['question']} max marks corrected to the mark "
                f"scheme value {entry['mark_scheme']} (model said {entry['model']})."
            )
            _flag(reasons, A_MAX_MARKS_CORRECTED, STAGE_MAPPING,
                  f"Question {entry['question']} maximum corrected to the mark "
                  f"scheme value {entry['mark_scheme']} (the marker said "
                  f"{entry['model']}).",
                  question=entry.get("question"), severity=SEVERITY_ADVISORY)
        # SUBQUESTION-level gaps (§4/§8/§9): the question came back, one of its
        # marking points did not. That is NOT a zero, and it is not complete
        # either — it needs a human look at THAT subquestion, not the paper.
        for entry in blueprint_report.get("missing_subquestions", []):
            label = entry.get("label") or entry.get("subquestion")
            anomalies.append(
                f"Subquestion {label} "
                f"of question {entry.get('question')} is declared by the mark "
                f"scheme but the marker returned nothing for it — it is "
                f"UNMARKED (not a confirmed 0).")
            _flag(reasons, R_SUBQUESTION_UNMARKED, STAGE_MAPPING,
                  f"Subquestion {label} of question {entry.get('question')} is in "
                  f"the mark scheme but was not marked.",
                  question=entry.get("question"),
                  action="Re-correct the paper; if it persists, check the scan "
                         "covers that part of the question.")
        for entry in blueprint_report.get("subquestion_conflicts", []):
            anomalies.append(
                f"{len(entry.get('rows') or [])} returned rows claim "
                f"subquestion {entry.get('subquestion')} of question "
                f"{entry.get('question')} "
                f"({', '.join(str(r) for r in entry.get('rows') or [])}). "
                f"Which row is real cannot be established from the result; "
                f"review it rather than trusting either.")
            _flag(reasons, R_SUBQUESTION_CONFLICT, STAGE_MAPPING,
                  f"Several returned rows claim subquestion "
                  f"{entry.get('subquestion')} of question {entry.get('question')} "
                  f"({', '.join(str(r) for r in entry.get('rows') or [])}).",
                  question=entry.get("question"),
                  action="Decide which row is the real answer, then set that mark.")
        for entry in blueprint_report.get("unattributed_rows", []):
            warnings.append(
                f"Question {entry.get('question')}: {entry.get('rows')} "
                f"returned row(s) name no subquestion although the mark scheme "
                f"declares them — the marking point each one covers is unknown.")
            _flag(reasons, A_UNATTRIBUTED_ROWS, STAGE_MAPPING,
                  f"Question {entry.get('question')}: rows "
                  f"{entry.get('rows')} name no subquestion, although the scheme "
                  f"declares them.",
                  question=entry.get("question"), severity=SEVERITY_ADVISORY)
        for entry in blueprint_report.get("unmatched_rows", []):
            warnings.append(
                f"The marker returned "
                f"'{entry.get('question_number')}' but "
                f"{entry.get('reason', 'the mark scheme does not declare it')}.")
            _flag(reasons, A_UNMATCHED_ROW, STAGE_MAPPING,
                  f"The marker returned '{entry.get('question_number')}' but "
                  f"{entry.get('reason', 'the mark scheme does not declare it')}.",
                  question=entry.get("question_number"),
                  severity=SEVERITY_ADVISORY)
        for entry in blueprint_report.get("award_clamped", []):
            warnings.append(
                f"Awarded marks were clamped to the maximum for "
                f"{entry.get('subquestion') or entry.get('question')} "
                f"({entry.get('awarded')} > {entry.get('max')}).")
            _flag(reasons, A_AWARD_CLAMPED, STAGE_VALIDATION,
                  f"Awarded marks for "
                  f"{entry.get('subquestion') or entry.get('question')} were "
                  f"clamped to its maximum ({entry.get('awarded')} > "
                  f"{entry.get('max')}).",
                  question=entry.get("question"), severity=SEVERITY_ADVISORY)

    # 5. Per-question status aggregation (absent status = legacy result = valid).
    for q in questions:
        q_status = q.get("status")
        if q_status and q_status != STATUS_VALID:
            review_questions.append(str(q.get("question_number")))
            reason = q.get("review_reason")
            if reason:
                warnings.append(f"Question {q.get('question_number')}: {reason}")
            _flag(reasons, R_QUESTION_FLAGGED, STAGE_MARKING,
                  reason or (f"Question {q.get('question_number')} was flagged by the "
                             f"marking stage."),
                  question=q.get("question_number"),
                  action="Check this question against the scan and set the mark by "
                         "hand if it is wrong.")

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
            _flag(reasons, R_SCHEME_DISAGREEMENT, STAGE_SCHEME,
                  f"Question {q.get('question_number')}: the marking disagrees with "
                  f"the mark scheme's own answer "
                  f"({check.get('scheme_answer') or 'not extracted'} vs "
                  f"{check.get('verified_answer') or 'no independent answer'}).",
                  question=q.get("question_number"),
                  action="Decide which answer is correct, then record the decision "
                         "in Mark scheme review.")

    # 6. Legacy uncertainties surface as warnings, not anomalies.
    for note in result.get("uncertainties", []) or []:
        warnings.append(f"Marker flagged: {note}")
        _flag(reasons, A_MARKER_UNCERTAINTY, STAGE_MARKING,
              f"The marker recorded an uncertainty: {note}",
              severity=SEVERITY_ADVISORY,
              action="Read the note; it does not change any mark by itself.")

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
    #
    # The verdict is split in two, because the two cases are not the same
    # question:
    #
    # * CONTRADICTED — the scheme (or this marking against it) produced an
    #   actual structural fault: a declared question nobody marked, conflicting
    #   subquestion rows, a printed total that disagrees with its parts. The
    #   marks may be wrong, so a human decides. This is review-level.
    # * UNVERIFIED — the structure was never independently confirmed (the
    #   visual inventory pass was not run for this PDF). That is a fact about
    #   the SCHEME, not evidence that this paper is wrong. Escalating every
    #   paper forever because a scheme was read once without the second pass is
    #   what turned 14 of 15 stored corrections into reviews. It is reported as
    #   an advisory on the assignment's scheme, and the marking itself stands.
    scheme_review_required = False
    if blueprint_report:
        scheme_state = str(blueprint_report.get("verification_state")
                           or "").upper()
        completeness = blueprint_report.get("completeness") or {}
        structural_signals = (
            len(blueprint_report.get("missing_questions") or [])
            + len(blueprint_report.get("missing_subquestions") or [])
            + len(blueprint_report.get("subquestion_conflicts") or [])
            + len(blueprint_report.get("unmatched_rows") or [])
            + len(blueprint_report.get("extra_questions") or [])
        )
        contradicted = bool(
            scheme_state in ("NEEDS_REPAIR", "NEEDS_MANUAL_REVIEW",
                             "REPAIRING", "REVERIFYING")
            and (completeness.get("unresolved") or structural_signals))
        if contradicted:
            scheme_review_required = True
            detail = ""
            verdict_text = str(completeness.get("verdict") or "").strip()
            if completeness.get("unresolved") and verdict_text:
                detail = f" ({verdict_text})"
            message = (
                f"The mark scheme structure this paper was marked against is "
                f"{scheme_state}{detail}: the app's own check did not confirm "
                f"the scheme. The marks are unchanged, but verify the scheme "
                f"before the grade is used.")
            _add_warning(warnings, message)
            _flag(reasons, R_SCHEME_CONTRADICTED, STAGE_SCHEME, message,
                  action="Check the mark scheme for this assignment in Mark "
                         "scheme review, then correct the paper again if it is "
                         "wrong.")
        elif scheme_state in ("NEEDS_REPAIR", "NEEDS_MANUAL_REVIEW",
                              "REPAIRING", "REVERIFYING"):
            message = (
                f"The mark scheme structure this paper was marked against is "
                f"{scheme_state}: it was read from the PDF but never "
                f"independently confirmed. No structural fault was found in "
                f"this paper's marking, so its marks stand; confirm the scheme "
                f"once to stop this note.")
            _add_warning(warnings, message)
            _flag(reasons, A_SCHEME_UNVERIFIED, STAGE_SCHEME, message,
                  severity=SEVERITY_ADVISORY,
                  action="Open the assignment's mark scheme and use "
                         "\"Recheck mark scheme\" once; a scheme that verifies "
                         "clears this note.")

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
        message = (
            f"The awarded total ({earned:g}) exceeds the {available:g} marks "
            f"available by {over_available:g}"
            + (f" — {', '.join(offenders)}" if offenders else "")
            + ". No mark has been changed: this correction needs a human "
              "decision before the score is used.")
        _add_warning(warnings, message)
        _flag(reasons, R_OVER_AVAILABLE, STAGE_VALIDATION, message,
              action="Lower the offending question's mark to its maximum.")

    review_level = any(r["severity"] == SEVERITY_REVIEW for r in reasons)
    status = (STATUS_VALID if not anomalies and not review_questions
              and not scheme_review_required and not review_level
              else STATUS_REVIEW)

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
        # Coded, ordered reasons: what happened, which stage produced it, the
        # question it concerns, whether it needs a human, and what to do. The
        # review screen and the diagnostics read THIS instead of parsing prose.
        "review_reasons": reasons,
        "review_reason_codes": [r["code"] for r in reasons],
        "advisory_count": sum(1 for r in reasons
                              if r["severity"] == SEVERITY_ADVISORY),
        "blueprint": blueprint_report or None,
    }
