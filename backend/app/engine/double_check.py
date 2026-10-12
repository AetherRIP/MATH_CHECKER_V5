"""High-score double-check (§1-§21) — independent verification of suspiciously
high AI corrections.

The AI correction engine occasionally produces suspiciously high results
(many 100%s). This module decides WHEN a second, independent correction must
run and HOW the two results combine:

    percentage = raw / max × 100  (unrounded)
    percentage >= threshold (default 95.0)  ->  run a second correction

The second correction is a genuine INDEPENDENT evaluation (the existing
engine re-run on the original submission + mark scheme); it never receives
the first score, reasoning, report or confidence as input, so it cannot be
biased toward confirming the first result.

Decision rules (§5/§6):

* second confirms first (second >= first)      -> final = first, COMPLETE
* second lower, within disagreement threshold  -> final = second, COMPLETE
  (the check was triggered precisely to catch a false high score, so the
  independent second correction is authoritative for small disagreements)
* second lower by MORE than the disagreement
  threshold                                    -> REVIEW_REQUIRED, final
  stays the first result, both preserved, no silent choice
* second correction failed                     -> first preserved,
  DOUBLE_CHECK_FAILED, review required

Nothing here changes the deterministic 1-9 grading (§7): the AI still only
produces raw marks, and the grade is still computed from the FINAL marks by
``app.automation.grading``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..config import get_config


def _dump(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)

# double_check_status values (persisted on the results row).
STATUS_NOT_TRIGGERED = "NOT_TRIGGERED"
STATUS_COMPLETE = "COMPLETE"
STATUS_FAILED = "DOUBLE_CHECK_FAILED"
STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"

# disagreement_status values (persisted on the results row).
DISAGREE_NONE = "NONE"                      # no double-check ran
DISAGREE_MATCHING = "MATCHING"              # the two passes agree on the verdict
DISAGREE_SECOND_LOWER = "SECOND_LOWER"      # second < first within threshold
DISAGREE_SECOND_HIGHER = "SECOND_HIGHER"    # low-score check: second > first within threshold
DISAGREE_MAJOR = "MAJOR_DISAGREEMENT"       # the two passes differ far more than allowed
DISAGREE_UNKNOWN = "UNKNOWN"                # second correction failed

# Which suspicious end of the scale triggered the check (§7).
DIRECTION_HIGH = "HIGH"   # first looked suspiciously high (catch inflated marks)
DIRECTION_LOW = "LOW"     # first looked suspiciously low (catch unfairly withheld marks)

# Generation parameters for the SECOND (independent) pass. The engine's
# first pass samples deterministically (temperature 0, seed 42); re-running
# with the very same seed would reproduce the first mark bit-for-bit and
# verify nothing. A distinct seed gives an independent sampling path while
# keeping temperature 0 (no randomness in style, only tie-breaking) and the
# marking prompt, schema, model chain and clamping EXACTLY the same.
SECOND_PASS_SAMPLING: dict = {"temperature": 0.0, "seed": 20260101}


def second_pass_models(model_chain: list[str], first_model: str | None) -> list[str]:
    """Model chain for the SECOND pass, led by a DIFFERENT model when possible.

    Why: at temperature 0 the same model given the same inputs tends to return
    the same marks, which would make the double-check a confirmation by
    construction instead of an independent evaluation (the observed "recheck
    always agrees" failure). Rotating to the next healthy model in the chain
    gives a genuinely different evaluation of the same evidence; if every other
    model fails, the chain still falls back to the first pass's model (a weak
    check is better than none, and the disagreement logic still applies).
    """
    models = [m for m in (model_chain or []) if m]
    if not models:
        return models
    if first_model and first_model in models and len(models) > 1:
        idx = models.index(first_model)
        rotated = models[idx + 1:] + models[:idx + 1]
        # The first-pass model stays in the chain as the LAST resort.
        return rotated
    return models


def threshold() -> float:
    """Percentage (unrounded) at or above which a double-check runs (§2/§12)."""
    try:
        value = float(get_config().double_check.threshold_percentage)
    except (TypeError, ValueError, AttributeError):
        return 95.0
    return value


def low_threshold() -> float:
    """Percentage (unrounded) at or below which a LOW-score double-check runs
    (§7). ``0`` disables the low branch."""
    try:
        value = float(get_config().double_check.low_threshold_percentage)
    except (TypeError, ValueError, AttributeError):
        return 10.0
    return max(0.0, value)


def disagreement_points() -> float:
    """Percentage-point gap between the two corrections above which NEITHER
    result is trusted blindly and the result is flagged REVIEW_REQUIRED (§6)."""
    try:
        value = float(get_config().double_check.disagreement_points)
    except (TypeError, ValueError, AttributeError):
        return 15.0
    return max(0.0, value)


def enabled() -> bool:
    try:
        return bool(get_config().double_check.enabled)
    except AttributeError:
        return True


def was_completed(result_row) -> bool:
    """True when the double-check for THIS result already completed (§17):
    used at restart so a completed check is never accidentally re-run."""
    try:
        return bool(result_row.get("double_check_triggered")) and \
            result_row.get("double_check_status") in (STATUS_COMPLETE,
                                                      STATUS_REVIEW_REQUIRED)
    except AttributeError:
        return False


def differences_of(result_row) -> list[dict]:
    """Decode the persisted question-level differences (§9)."""
    raw = result_row.get("double_check_differences") if result_row else None
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return raw if isinstance(raw, list) else []


def percentage_of(earned, available) -> float:
    """Unrounded percentage (§2: never trigger from a rounded display value)."""
    try:
        earned = float(earned or 0)
        available = float(available or 0)
    except (TypeError, ValueError):
        return 0.0
    return (earned / available * 100.0) if available else 0.0


def direction_of(earned, available) -> str | None:
    """Which independent check (if any) this result needs: ``HIGH``, ``LOW``
    or ``None``. The unrounded percentage decides; ``None`` means "no check".

    Exactly at the boundary counts: 95.0 triggers the high check and 10.0 the
    low check (§12)."""
    if not enabled() or not available:
        return None
    pct = percentage_of(earned, available)
    if pct >= threshold():
        return DIRECTION_HIGH
    low = low_threshold()
    if low > 0 and pct <= low:
        return DIRECTION_LOW
    return None


def should_trigger(earned, available) -> bool:
    """True when a completed first correction needs an independent re-check.

    Uses the ACTUAL unrounded percentage: ``>=`` the high threshold (§12:
    exactly 95% triggers; 94.99% does not) OR ``<=`` the low threshold (§7: a
    suspiciously low score gets an independent second look too)."""
    return direction_of(earned, available) is not None


@dataclass
class Decision:
    """Outcome of comparing the two independent corrections."""
    final_earned: int
    final_available: int
    final_percentage: float
    status: str                       # STATUS_* constant
    disagreement_status: str          # DISAGREE_* constant
    review_required: bool
    reason: str
    #: Percentage-point difference between the passes; 0 when not applicable.
    difference_points: float = 0.0
    #: Question-level differences supported by the correction evidence (§9).
    question_differences: list[dict] = field(default_factory=list)


def decide(*, first_earned: int, first_available: int, first_percentage: float,
           second_earned: int, second_available: int,
           second_percentage: float,
           first_result: dict | None = None,
           second_result: dict | None = None,
           direction: str = DIRECTION_HIGH) -> Decision:
    """Combine the first and second corrections into the final result (§5/§6).

    ``direction`` is which end of the scale triggered the check. For
    ``DIRECTION_LOW`` a HIGHER independent result is the interesting case (the
    first pass may have unfairly withheld marks); for ``DIRECTION_HIGH`` a
    LOWER independent result is (the first pass may have inflated marks).

    The deterministic grade is computed LATER from the returned final marks —
    this function never picks a grade.
    """
    if direction == DIRECTION_LOW:
        return _decide_low(
            first_earned, first_available, first_percentage,
            second_earned, second_available, second_percentage,
            first_result, second_result)

    gap = first_percentage - second_percentage

    if second_percentage >= first_percentage:
        # Confirmed (or the independent run scored higher — keep the first,
        # lower result: the double-check exists to catch inflated scores).
        return Decision(
            final_earned=int(first_earned),
            final_available=int(first_available),
            final_percentage=first_percentage,
            status=STATUS_COMPLETE,
            disagreement_status=DISAGREE_MATCHING,
            review_required=False,
            reason=("The independent second correction confirmed the first "
                    f"result ({_fmt(first_percentage)}%)."),
            difference_points=max(0.0, gap),
            question_differences=_question_differences(first_result,
                                                       second_result),
        )

    limit = disagreement_points()
    if gap <= limit:
        return Decision(
            final_earned=int(second_earned),
            final_available=int(second_available),
            final_percentage=second_percentage,
            status=STATUS_COMPLETE,
            disagreement_status=DISAGREE_SECOND_LOWER,
            review_required=False,
            reason=(
                "The independent second correction found missed errors the "
                f"first pass overlooked ({_fmt(first_percentage)}% -> "
                f"{_fmt(second_percentage)}%, {gap:.2f} points, within the "
                f"{limit:g}-point disagreement threshold), so its result is "
                "final."),
            difference_points=gap,
            question_differences=_question_differences(first_result,
                                                       second_result),
        )

    # Large disagreement: do not blindly trust either result (§6).
    return Decision(
        final_earned=int(first_earned),
        final_available=int(first_available),
        final_percentage=first_percentage,
        status=STATUS_REVIEW_REQUIRED,
        disagreement_status=DISAGREE_MAJOR,
        review_required=True,
        reason=(
            "The two independent corrections disagree by "
            f"{gap:.2f} percentage points ({_fmt(first_percentage)}% vs "
            f"{_fmt(second_percentage)}%), which exceeds the "
            f"{limit:g}-point threshold. Both results are preserved and a "
            "teacher must review before the grade is used."),
        difference_points=gap,
        question_differences=_question_differences(first_result,
                                                   second_result),
    )


def _decide_low(first_earned: int, first_available: int, first_percentage: float,
                second_earned: int, second_available: int,
                second_percentage: float,
                first_result: dict | None,
                second_result: dict | None) -> Decision:
    """Low-score check (§7): the first result looked suspiciously LOW.

    * second <= first  -> both passes agree it is low; keep the first result.
    * second  > first, within the disagreement limit -> the independent pass
      found marks the first missed, so the SECOND becomes final.
    * second  > first, beyond the limit -> neither is trusted blindly;
      REVIEW_REQUIRED with the first result left in place.
    """
    if second_percentage <= first_percentage:
        return Decision(
            final_earned=int(first_earned),
            final_available=int(first_available),
            final_percentage=first_percentage,
            status=STATUS_COMPLETE,
            disagreement_status=DISAGREE_MATCHING,
            review_required=False,
            reason=("The independent second correction confirmed the low first "
                    f"result ({_fmt(first_percentage)}%)."),
            difference_points=max(0.0, first_percentage - second_percentage),
            question_differences=_question_differences(first_result,
                                                       second_result),
        )

    rise = second_percentage - first_percentage
    limit = disagreement_points()
    if rise <= limit:
        return Decision(
            final_earned=int(second_earned),
            final_available=int(second_available),
            final_percentage=second_percentage,
            status=STATUS_COMPLETE,
            disagreement_status=DISAGREE_SECOND_HIGHER,
            review_required=False,
            reason=(
                "The independent second correction found marks the first pass "
                f"withheld ({_fmt(first_percentage)}% -> "
                f"{_fmt(second_percentage)}%, +{rise:.2f} points, within the "
                f"{limit:g}-point disagreement threshold), so its result is "
                "final."),
            difference_points=rise,
            question_differences=_question_differences(first_result,
                                                       second_result),
        )

    return Decision(
        final_earned=int(first_earned),
        final_available=int(first_available),
        final_percentage=first_percentage,
        status=STATUS_REVIEW_REQUIRED,
        disagreement_status=DISAGREE_MAJOR,
        review_required=True,
        reason=(
            "The two independent corrections disagree by "
            f"{rise:.2f} percentage points ({_fmt(first_percentage)}% vs "
            f"{_fmt(second_percentage)}%), which exceeds the "
            f"{limit:g}-point threshold. Both results are preserved and a "
            "teacher must review before the grade is used."),
        difference_points=rise,
        question_differences=_question_differences(first_result,
                                                   second_result),
    )


def combine(first_result: dict, second_result: dict | None,
            second_marking_model: str | None = None,
            error: str | None = None) -> dict:
    """Shared persistence-side helper: combine a first correction with the
    outcome of its independent double-check into the state to persist.

    Used by BOTH save paths (the worker pool and the legacy service) so they
    can never drift apart. Returns a plain dict with the first/second/final
    mark triples, the status/disagreement/review flags, the decision reason,
    the evidence-backed question differences, and the result dict whose marks
    are final (``final_result``).
    """
    from . import legacy_marking as legacy

    earned, available, percentage = legacy.calculate_totals(first_result)
    out: dict = {
        "triggered": True,
        "status": STATUS_FAILED,
        "disagreement_status": DISAGREE_UNKNOWN,
        "review_required": True,
        "reason": None,
        "first": (earned, available, percentage),
        "second": None,
        "second_result": None,
        "second_marking_model": second_marking_model,
        "final_result": first_result,
        "final": (earned, available, percentage),
        "error": error,
        "question_differences": [],
    }
    if second_result is None:
        out["reason"] = (
            "The independent high-score double-check failed, so the first "
            "result was preserved unverified: " + (error or "unknown error"))
        return out

    s_e, s_a, s_p = legacy.calculate_totals(second_result)
    low = low_threshold()
    direction = DIRECTION_LOW if (available and low > 0 and percentage <= low) \
        else DIRECTION_HIGH
    decision = decide(
        first_earned=earned, first_available=available,
        first_percentage=percentage,
        second_earned=s_e, second_available=s_a, second_percentage=s_p,
        first_result=first_result, second_result=second_result,
        direction=direction)
    final_is_second = decision.disagreement_status in (
        DISAGREE_SECOND_LOWER, DISAGREE_SECOND_HIGHER)
    out.update(
        status=decision.status,
        disagreement_status=decision.disagreement_status,
        review_required=decision.review_required,
        reason=decision.reason,
        second=(s_e, s_a, s_p),
        second_result=second_result,
        final_result=second_result if final_is_second else first_result,
        final=(decision.final_earned, decision.final_available,
               decision.final_percentage),
        error=None,
        question_differences=decision.question_differences,
    )
    return out


def record_persistence(conn, *, result_id: str, first_result: dict,
                       first_verification, dc: dict, marking_model,
                       second_marking_model, key_fingerprint, confidence,
                       confidence_reasons, now_ts: float,
                       second_verification=None) -> tuple[str | None, str | None]:
    """Persist the double-check outcome (§4/§16), shared by BOTH save paths.

    Inside the caller's transaction this:
      1. records the FIRST correction snapshot in the append-only history,
      2. records the SECOND (independent) correction snapshot,
      3. sets the double-check columns on the results row (the row already
         holds the FINAL result in earned/available/percentage).

    The caller records the FINAL snapshot afterwards, so the current version
    is always the final result. Returns the result_versions row ids for the
    first and second corrections (first/second_correction_id).
    """
    from . import legacy_marking as legacy
    from . import versions

    f_e, f_a, f_p = dc["first"]
    versions.record(
        conn, result_id=result_id, earned=f_e, available=f_a, percentage=f_p,
        provisional_grade=legacy.percentage_band_grade(f_p),
        marking_model=marking_model, key_fingerprint=key_fingerprint,
        confidence=confidence, confidence_reasons=confidence_reasons,
        verification=first_verification, result=first_result,
        created_at=now_ts,
        note="First correction (high-score double-check triggered)")
    first_id = _last_version_row_id(conn, result_id)

    second_id = None
    if dc["second_result"] is not None:
        s_e, s_a, s_p = dc["second"]
        versions.record(
            conn, result_id=result_id, earned=s_e, available=s_a, percentage=s_p,
            provisional_grade=legacy.percentage_band_grade(s_p),
            marking_model=second_marking_model or marking_model,
            key_fingerprint=key_fingerprint, confidence=confidence,
            confidence_reasons=confidence_reasons,
            verification=second_verification, result=dc["second_result"],
            created_at=now_ts,
            note="Independent double-check correction")
        second_id = _last_version_row_id(conn, result_id)

    conn.execute(
        """
        UPDATE results SET
            double_check_triggered = 1, double_check_status = ?,
            first_earned = ?, first_available = ?, first_percentage = ?,
            second_earned = ?, second_available = ?, second_percentage = ?,
            final_earned = ?, final_available = ?, final_percentage = ?,
            disagreement_status = ?, first_correction_id = ?,
            second_correction_id = ?, double_check_reason = ?,
            double_check_differences = ?
        WHERE id = ?
        """,
        (dc["status"], f_e, f_a, f_p,
         (dc["second"][0] if dc["second"] else None),
         (dc["second"][1] if dc["second"] else None),
         (dc["second"][2] if dc["second"] else None),
         dc["final"][0], dc["final"][1], dc["final"][2],
         dc["disagreement_status"], first_id, second_id,
         (dc["reason"] or "")[:500],
         _dump(dc["question_differences"]), result_id),
    )
    return first_id, second_id


def _last_version_row_id(conn, result_id: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM result_versions WHERE result_id = ? "
        "ORDER BY version DESC LIMIT 1", (result_id,)).fetchone()
    return row["id"] if row else None


def _fmt(pct: float) -> str:
    return f"{pct:.2f}%".rstrip("0").rstrip(".").replace(".%", "%")


def _question_differences(first: dict | None, second: dict | None) -> list[dict]:
    """Question-level evidence for WHY the scores differ (§9).

    Only differences the two corrections actually recorded are reported:
    question number, both awarded marks, and each pass's own loss reason.
    Nothing is invented — a question both passes marked identically is never
    listed.
    """
    if not isinstance(first, dict) or not isinstance(second, dict):
        return []
    by_number: dict[str, dict] = {}
    for q in first.get("questions") or []:
        if isinstance(q, dict) and q.get("question_number") is not None:
            by_number[str(q["question_number"])] = q
    diffs: list[dict] = []
    for q in second.get("questions") or []:
        if not isinstance(q, dict) or q.get("question_number") is None:
            continue
        counterpart = by_number.get(str(q["question_number"]))
        if counterpart is None:
            continue
        try:
            a = int(counterpart.get("awarded_marks") or 0)
            b = int(q.get("awarded_marks") or 0)
        except (TypeError, ValueError):
            continue
        if a == b:
            continue
        diffs.append({
            "question_number": str(q["question_number"]),
            "first_awarded": a,
            "second_awarded": b,
            "max_marks": int(q.get("max_marks") or 0),
            "first_loss_reason": str(counterpart.get("loss_reason") or ""),
            "second_loss_reason": str(q.get("loss_reason") or ""),
        })
    diffs.sort(key=lambda d: d["question_number"])
    return diffs
