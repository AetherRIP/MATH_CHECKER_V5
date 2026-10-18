"""Result guard — deterministic protection against bad corrections (§7/§8/§23).

This module sits between "a model returned a result" and "a grade is stored".
It answers two questions, both with plain Python (no AI, no randomness):

1. **How trustworthy is this result?**  -> a confidence level
   ``HIGH`` / ``MEDIUM`` / ``LOW`` / ``VALIDATION_FAILED`` plus human-readable
   reasons. Validation happens even when the JSON is perfectly well-formed:
   a model returning valid JSON does not mean the *marks* are correct.

2. **May it replace an existing stored result?** -> an overwrite decision.

The hard rule (§7): a valid stored result is NEVER silently replaced by a
suspicious one. The motivating bug: a valid 67/103 being overwritten by a
later 0/103. Deterministic triggers for "suspicious" include a zero where the
previous run awarded marks, or a drop larger than ``LARGE_CHANGE_POINTS``.
In those cases the strongest validated result is kept and the job is flagged
for human review instead.

A teacher's manual override outranks any automated result and is only
replaced when the caller explicitly forces it (§23).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A drop of this many percentage points (or more) between runs is treated as
# a suspicious score change that requires human review rather than a silent
# overwrite. Deliberately conservative: legitimate re-marks rarely move more
# than this, and a false "keep existing" is far cheaper than losing a real
# mark to a bad model run.
LARGE_CHANGE_POINTS = 30.0

# Confidence levels (also the review-queue buckets, §23).
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
VALIDATION_FAILED = "VALIDATION_FAILED"
LARGE_SCORE_CHANGE = "LARGE_SCORE_CHANGE"
#: Submission itself is suspect (blank / unreadable / wrong assignment /) —
#: deterministic signal from engine.submission_validation (§9). Never trusted
#: without a human: only a teacher override can clear it.
SUBMISSION_REVIEW_REQUIRED = "SUBMISSION_REVIEW_REQUIRED"

# Overwrite actions.
ACTION_INSERT = "insert"                    # nothing stored yet — always store
ACTION_UPDATE = "update"                    # safe to replace the stored result
ACTION_KEEP_EXISTING = "keep_existing"      # keep the strongest validated result
ACTION_BLOCKED = "blocked"                  # untrustworthy AND nothing to keep


@dataclass
class OverwriteDecision:
    action: str
    reason: str
    confidence: str
    confidence_reasons: list[str] = field(default_factory=list)

    @property
    def review_required(self) -> bool:
        return self.action in (ACTION_KEEP_EXISTING, ACTION_BLOCKED) or \
            self.confidence in (LOW, VALIDATION_FAILED, LARGE_SCORE_CHANGE,
                                SUBMISSION_REVIEW_REQUIRED)


def percentage_of(earned, available) -> float:
    """Unrounded percentage, the one definition used across the engine.

    A missing/invalid value counts as 0 rather than raising: callers classify
    an untrustworthy result, they do not crash on one. ``available == 0`` has
    no defined percentage and returns 0.0 (a grade cannot be computed from it
    anyway — the caller reports that separately).
    """
    try:
        earned = float(earned or 0)
        available = float(available or 0)
    except (TypeError, ValueError):
        return 0.0
    return (earned / available * 100.0) if available else 0.0


def assess_confidence(result: dict, verification: dict, earned: int,
                      available: int) -> tuple[str, list[str]]:
    """Return ``(level, reasons)`` for one freshly produced result.

    Deterministic validation of the AI output *before* it is trusted:
    impossible values, missing structure, and structural review signals from
    the verification layer all reduce confidence. This never changes a mark —
    it only decides how much a human should look at it.
    """
    reasons: list[str] = []

    # --- Hard validation failures: the result cannot be trusted at all. ------
    failed = False
    if available is None or available <= 0:
        reasons.append("Maximum marks are zero or missing — a grade cannot be computed.")
        failed = True
    if earned is None or earned < 0:
        reasons.append("Awarded marks are negative or missing (impossible value).")
        failed = True
    if earned is not None and available and earned > available:
        reasons.append(f"Awarded marks ({earned}) exceed the maximum ({available}) — impossible.")
        failed = True
    questions = result.get("questions") if isinstance(result, dict) else None
    if not questions:
        reasons.append("No questions were returned — nothing to grade.")
        failed = True
    if verification.get("status") == "BLOCKED":
        reasons.append("The verification layer marked this as blocked (no trustworthy result).")
        failed = True

    if failed:
        return VALIDATION_FAILED, reasons

    # --- Structural review signals (do not invalidate, but need a glance). ---
    review_questions = verification.get("review_questions") or []
    if verification.get("status") and verification["status"] != "VALID":
        reasons.append("Verification requested review for this correction.")
    if review_questions:
        reasons.append(f"{len(review_questions)} question(s) flagged for review "
                       f"(e.g. {', '.join(str(q) for q in review_questions[:5])}).")
    anomalies = verification.get("anomalies") or []
    if anomalies:
        reasons.append(f"{len(anomalies)} structural anomaly(ies) reported.")

    if verification.get("status") != "VALID" or anomalies:
        return LOW, reasons

    warnings = verification.get("warnings") or []
    if warnings:
        reasons.append(f"{len(warnings)} warning(s) — marks are valid but worth a check.")
        return MEDIUM, reasons

    reasons.append("All checks passed: totals consistent, no structural warnings.")
    return HIGH, reasons


def evaluate_overwrite(*, existing: dict | None, earned: int, available: int,
                       verification: dict, confidence: str,
                       confidence_reasons: list[str],
                       force: bool = False) -> OverwriteDecision:
    """Decide whether a new result may replace the stored one.

    ``existing`` is the currently stored result row (or None). ``force`` is a
    deliberate teacher override ("reprocess anyway") and is the ONLY way a
    suspicious result is allowed to replace a valid one.
    """
    if existing is None:
        return OverwriteDecision(
            action=ACTION_INSERT,
            reason="No previous result stored — saving this correction.",
            confidence=confidence, confidence_reasons=confidence_reasons)

    # A teacher's manual override is authoritative (§23).
    manual = bool(existing.get("manual_override"))
    if manual and not force:
        return OverwriteDecision(
            action=ACTION_KEEP_EXISTING,
            reason=("A teacher manually overrode this result. The automated "
                    "re-run was NOT applied — use 'Reprocess anyway' to replace it."),
            confidence=confidence, confidence_reasons=confidence_reasons)

    if force:
        return OverwriteDecision(
            action=ACTION_UPDATE,
            reason="Forced reprocessing was explicitly requested — replacing the stored result.",
            confidence=confidence, confidence_reasons=confidence_reasons)

    # Never replace a stored result with one that failed validation (§7).
    if confidence == VALIDATION_FAILED:
        return OverwriteDecision(
            action=ACTION_KEEP_EXISTING,
            reason=("The new result failed validation, so the existing result was "
                    "kept. " + " ".join(confidence_reasons[:2])).strip(),
            confidence=confidence, confidence_reasons=confidence_reasons)

    existing_pct = percentage_of(existing.get("earned"), existing.get("available"))
    new_pct = percentage_of(earned, available)
    existing_earned = existing.get("earned") or 0

    # The motivating bug: 67/103 replaced by 0/103.
    if new_pct == 0.0 and existing_earned > 0:
        return OverwriteDecision(
            action=ACTION_KEEP_EXISTING,
            reason=(f"The new run scored 0 where the stored result awarded "
                    f"{existing_earned} — this is almost always a failed marking "
                    f"run, so the stronger stored result was kept for review."),
            confidence=LARGE_SCORE_CHANGE, confidence_reasons=confidence_reasons)

    drop = existing_pct - new_pct
    if drop >= LARGE_CHANGE_POINTS:
        return OverwriteDecision(
            action=ACTION_KEEP_EXISTING,
            reason=(f"The new run scored {new_pct:.1f}% against a stored "
                    f"{existing_pct:.1f}% (a {drop:.1f} point drop). A change this "
                    f"large needs human review, so the stored result was kept."),
            confidence=LARGE_SCORE_CHANGE, confidence_reasons=confidence_reasons)

    return OverwriteDecision(
        action=ACTION_UPDATE,
        reason="The new result is consistent with the stored one — updating it.",
        confidence=confidence, confidence_reasons=confidence_reasons)
