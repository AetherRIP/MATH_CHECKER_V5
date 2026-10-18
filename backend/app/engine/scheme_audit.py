"""Mark-scheme error detection, notification and approval-gated bulk correction.

The marking engine (``legacy_marking``) is excellent at following a *correct*
mark scheme. The failure this module exists to fix is the opposite case: the AI
recognises that a student's answer is mathematically correct but still awards
zero because the supplied mark scheme gives a different, incorrect answer.

The governing principle (from the brief):

    The mark scheme is an important reference, not an infallible source of
    mathematical truth.

How this module fits in
----------------------

* The marking PROMPT was changed (see ``legacy_marking``) so the model
  independently verifies each answer and reports a per-question ``scheme_check``
  whenever it has a defensible mathematical reason to disagree with the
  supplied mark scheme. It awards marks justified by mathematics and the
  applicable marking rules - never zero merely because a correct answer differs
  from a broken reference answer.
* :func:`detect` turns those per-question checks into structured *candidates*.
  It is deliberately conservative: a disagreement alone is NOT an error. A
  defensible reason, an affected student mark and a confidence level are
  required before a candidate is even recorded.
* :func:`record_from_result` persists the candidates (idempotently, one live
  record per class + assignment + question + type) and preserves any human
  decision already on record.
* :func:`build_proposal` stages the correction for OTHER, already-graded
  submissions. Staging NEVER changes a grade.
* :func:`approve` is the ONLY function that changes previously graded marks,
  and it requires an explicit human approval on a staged proposal. It is
  idempotent, records the original marks for auditing, and detects partial
  failure so the whole batch can never be reported as successful.

Nothing here is a prompt-only change: the decision logic, persistence, the
notification feed and the approval workflow are all real code paths.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from .. import db

log = logging.getLogger("engine.scheme_audit")

# ---------------------------------------------------------------- states (section 5)

STATE_CONFIRMED = "CONFIRMED_DISCREPANCY"
STATE_POSSIBLE = "POSSIBLE_DISCREPANCY"
STATE_VALID_ALTERNATIVE = "VALID_ALTERNATIVE"
STATE_UNCERTAIN = "UNCERTAIN"
STATE_HUMAN_REVIEWED = "HUMAN_REVIEWED"

REVIEW_AWAITING = "AWAITING_REVIEW"
REVIEW_COMPLETED = "REVIEW_COMPLETED"

# ------------------------------------------------------------- discrepancy types (section 3)

TYPE_INCORRECT = "incorrect_answer"
TYPE_ARITHMETIC = "arithmetic_error"
TYPE_SIGN = "sign_error"
TYPE_EQUIVALENT = "equivalent_expression"
TYPE_ALTERNATIVE_METHOD = "alternative_method"
TYPE_QUESTION_MISMATCH = "question_scheme_mismatch"
TYPE_INTERNAL_INCONSISTENCY = "internal_inconsistency"
TYPE_AMBIGUOUS = "ambiguous_question"
TYPE_OTHER = "other"

ALL_TYPES = frozenset({
    TYPE_INCORRECT, TYPE_ARITHMETIC, TYPE_SIGN, TYPE_EQUIVALENT,
    TYPE_ALTERNATIVE_METHOD, TYPE_QUESTION_MISMATCH,
    TYPE_INTERNAL_INCONSISTENCY, TYPE_AMBIGUOUS, TYPE_OTHER,
})

#: Types that are a *mathematical* claim about the scheme being wrong, as
#: opposed to a format/method difference that is not an error at all.
MATH_ERROR_TYPES = frozenset({
    TYPE_INCORRECT, TYPE_ARITHMETIC, TYPE_SIGN, TYPE_QUESTION_MISMATCH,
    TYPE_INTERNAL_INCONSISTENCY,
})

#: Types that mean the student is right and the scheme simply did not list
#: their (equivalent) form or (valid alternative) method.
VALID_ALTERNATIVE_TYPES = frozenset({TYPE_EQUIVALENT, TYPE_ALTERNATIVE_METHOD})

# -------------------------------------------------------------- human decisions (section 8)

DECISION_CONFIRM_SCHEME_WRONG = "CONFIRM_SCHEME_INCORRECT"
DECISION_ACCEPT_ALTERNATIVE = "ACCEPT_ALTERNATIVE_ANSWER"
DECISION_AWARD_PARTIAL = "AWARD_PARTIAL_CREDIT"
DECISION_CONFIRM_SCHEME_CORRECT = "CONFIRM_SCHEME_CORRECT"
DECISION_REJECT_BULK = "REJECT_BULK_CORRECTION"
DECISION_REQUEST_INVESTIGATION = "REQUEST_FURTHER_INVESTIGATION"

DECISIONS = (
    DECISION_CONFIRM_SCHEME_WRONG,
    DECISION_ACCEPT_ALTERNATIVE,
    DECISION_AWARD_PARTIAL,
    DECISION_CONFIRM_SCHEME_CORRECT,
    DECISION_REJECT_BULK,
    DECISION_REQUEST_INVESTIGATION,
)

PROPOSAL_PROPOSED = "PROPOSED"
PROPOSAL_APPROVED = "APPROVED"
PROPOSAL_APPLIED = "APPLIED"
PROPOSAL_PARTIAL = "PARTIAL"
PROPOSAL_REJECTED = "REJECTED"

ITEM_PENDING = "PENDING"
ITEM_APPLIED = "APPLIED"
ITEM_FAILED = "FAILED"
ITEM_SKIPPED = "SKIPPED"

DEFAULT_CONFIRM_CONFIDENCE = 0.85


# --------------------------------------------------------------------- config

def enabled() -> bool:
    try:
        return bool(get_config().scheme_audit.enabled)
    except AttributeError:
        return True


def confirm_confidence() -> float:
    try:
        value = float(get_config().scheme_audit.confirm_confidence)
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_CONFIRM_CONFIDENCE
    return min(1.0, max(0.0, value))


def get_config():
    from ..config import get_config as _get
    return _get()


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: Any, maximum: int) -> int | None:
    parsed = _as_int(value)
    if parsed is None:
        return None
    return max(0, min(parsed, max(0, maximum)))


def _normalise_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "wrong_answer": TYPE_INCORRECT,
        "incorrect": TYPE_INCORRECT,
        "wrong": TYPE_INCORRECT,
        "arithmetic": TYPE_ARITHMETIC,
        "sign": TYPE_SIGN,
        "equivalent": TYPE_EQUIVALENT,
        "equivalence": TYPE_EQUIVALENT,
        "alternative": TYPE_ALTERNATIVE_METHOD,
        "alternative_solution": TYPE_ALTERNATIVE_METHOD,
        "mismatch": TYPE_QUESTION_MISMATCH,
        "inconsistent": TYPE_INTERNAL_INCONSISTENCY,
        "inconsistency": TYPE_INTERNAL_INCONSISTENCY,
        "ambiguous": TYPE_AMBIGUOUS,
        "ambiguity": TYPE_AMBIGUOUS,
    }
    if text in ALL_TYPES:
        return text
    return aliases.get(text, TYPE_OTHER)


# ----------------------------------------------------------------- classification (sections 2/3)

def classify(check: Any, *, max_marks: int, awarded: int) -> dict | None:
    """Decide the discrepancy state for ONE question's ``scheme_check``.

    Returns ``None`` when there is nothing to record: no disagreement, a
    malformed check, or a disagreement the model could not substantiate. This
    is what stops the engine from treating *every* difference between its own
    answer and the reference answer as a mark-scheme error.
    """
    if not isinstance(check, dict):
        return None
    if not check.get("disagrees_with_scheme"):
        return None

    dtype = _normalise_type(check.get("discrepancy_type"))
    confidence = min(1.0, max(0.0, _as_float(check.get("confidence"), 0.5)))
    valid_student = bool(check.get("student_answer_valid"))
    reason = str(check.get("evidence") or check.get("reasoning") or "").strip()

    threshold = confirm_confidence()

    if valid_student and dtype in VALID_ALTERNATIVE_TYPES:
        # The student is right; the scheme merely did not list their form or
        # method. This is not a mark-scheme *error* claim, but it is a
        # disagreement worth surfacing (and it must not cost the student marks).
        state, recommended = STATE_VALID_ALTERNATIVE, True
    elif valid_student and dtype == TYPE_AMBIGUOUS:
        # Ambiguous question: do not confidently declare either answer correct.
        state, recommended = STATE_UNCERTAIN, True
    elif valid_student and dtype in MATH_ERROR_TYPES:
        if confidence >= threshold and reason:
            # Self-reported model confidence is not independent proof. Keep
            # the mathematical recommendation, but reserve confirmation for a
            # recorded human decision rather than laundering prose into fact.
            state, recommended = STATE_POSSIBLE, True
        else:
            # A defensible-looking disagreement that is not yet strong enough
            # to call confirmed. Human verification is requested.
            state, recommended = STATE_POSSIBLE, True
    elif valid_student:
        state, recommended = STATE_POSSIBLE, confidence >= 0.6
    else:
        # The student really is wrong. The scheme may still be wrong, but no
        # student mark is at stake here; record it without forcing review
        # unless the evidence is strong and mathematical.
        state = STATE_POSSIBLE if (dtype in MATH_ERROR_TYPES
                                   and confidence >= threshold and reason) \
            else STATE_UNCERTAIN
        recommended = dtype in MATH_ERROR_TYPES and confidence >= threshold

    justified = _clamp(check.get("justified_marks"), max_marks)

    return {
        "type": dtype,
        "state": state,
        "confidence": round(confidence, 3),
        "student_answer_valid": valid_student,
        "review_recommended": recommended,
        "affects_marks": bool(valid_student and awarded < max_marks),
        "scheme_answer": str(check.get("scheme_answer") or "").strip(),
        "verified_answer": str(check.get("independently_verified_answer")
                               or check.get("verified_answer") or "").strip(),
        "reasoning": reason,
        "justified_marks": justified,
    }


def detect(result: dict | None) -> list[dict]:
    """Every substantiated mark-scheme disagreement in a normalised result.

    ``result`` is the legacy engine's normalised result (each question may
    carry an optional ``scheme_check``). Questions without a check, or whose
    check does not represent a substantiated disagreement, are ignored - so an
    ordinary result produces no records at all and normal marking is untouched.
    """
    if not isinstance(result, dict):
        return []
    out: list[dict] = []
    for q in result.get("questions") or []:
        if not isinstance(q, dict):
            continue
        max_marks = _as_int(q.get("max_marks"), 0) or 0
        awarded = _as_int(q.get("awarded_marks"), 0) or 0
        info = classify(q.get("scheme_check"), max_marks=max_marks,
                        awarded=awarded)
        if not info:
            continue
        out.append({
            "question_number": str(q.get("question_number", "")).strip(),
            "max_marks": max_marks,
            "awarded_marks": awarded,
            "student_answer": str(q.get("student_answer") or "").strip(),
            "loss_reason": str(q.get("loss_reason") or "").strip(),
            **info,
        })
    return out


# ----------------------------------------------------------------- persistence (section 5)

def _assignment_title(conn, coursework_id: str | None) -> str | None:
    if not coursework_id:
        return None
    row = conn.execute(
        "SELECT COALESCE(custom_title, classroom_title, title) AS title "
        "FROM coursework WHERE id = ?", (coursework_id,)).fetchone()
    return row["title"] if row else None


_UPSERT_SQL = """
INSERT INTO scheme_discrepancies(
    id, coursework_id, course_id, assignment_title, question_number,
    discrepancy_type, state, scheme_answer, verified_answer, verified_reasoning,
    evidence, suspicion, confidence, max_marks, student_result_id, submission_id,
    student_id, student_name, awarded_marks, justified_marks, review_recommended,
    review_required, review_status, created_at, updated_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_UPDATE_SQL = """
UPDATE scheme_discrepancies SET
    course_id = ?, assignment_title = ?, state = ?, scheme_answer = ?,
    verified_answer = ?, verified_reasoning = ?, evidence = ?, suspicion = ?,
    confidence = ?, max_marks = ?, student_result_id = COALESCE(?, student_result_id),
    submission_id = COALESCE(?, submission_id),
    student_id = COALESCE(?, student_id),
    student_name = COALESCE(?, student_name),
    awarded_marks = ?, justified_marks = ?, review_recommended = ?,
    review_required = ?, updated_at = ?
WHERE id = ?
"""


def record_from_result(conn, *, job: dict, result_id: str, result: dict,
                       now_ts: float | None = None) -> list[dict]:
    """Persist the disagreements in one correction. Idempotent.

    Called inside the caller's transaction from BOTH save paths. Re-detecting
    the same (class + assignment + question + type) updates the existing row -
    it never creates a duplicate notification - and a record a human has
    already reviewed keeps its human outcome.
    """
    if not enabled():
        return []
    candidates = detect(result)
    if not candidates:
        return []

    now_ts = now_ts or time.time()
    coursework_id = job.get("coursework_id")
    course_id = job.get("course_id")
    title = _assignment_title(conn, coursework_id)
    stored: list[dict] = []

    for c in candidates:
        if not c["question_number"]:
            continue
        existing = conn.execute(
            "SELECT * FROM scheme_discrepancies WHERE coursework_id IS ? "
            "AND question_number = ? AND discrepancy_type = ?",
            (coursework_id, c["question_number"], c["type"])).fetchone()
        suspicion = _suspicion(c)
        if existing:
            # Preserve a completed human review: only refresh evidence.
            conn.execute(_UPDATE_SQL, (
                course_id, title,
                existing["state"] if existing["review_status"] == REVIEW_COMPLETED else c["state"],
                c["scheme_answer"],
                c["verified_answer"], c["reasoning"], c["reasoning"], suspicion,
                c["confidence"], c["max_marks"], result_id,
                job.get("submission_id"), job.get("student_id"),
                job.get("student_name"), c["awarded_marks"],
                c["justified_marks"], 1 if c["review_recommended"] else 0,
                0 if existing["review_status"] == REVIEW_COMPLETED
                else (1 if c["review_recommended"] else 0), now_ts,
                existing["id"]))
            stored.append({"id": existing["id"], **c, "suspicion": suspicion,
                           "review_status": existing["review_status"]})
        else:
            new_id = uuid.uuid4().hex
            conn.execute(_UPSERT_SQL, (
                new_id, coursework_id, course_id, title, c["question_number"],
                c["type"], c["state"], c["scheme_answer"], c["verified_answer"],
                c["reasoning"], c["reasoning"], suspicion, c["confidence"],
                c["max_marks"], result_id, job.get("submission_id"),
                job.get("student_id"), job.get("student_name"),
                c["awarded_marks"], c["justified_marks"],
                1 if c["review_recommended"] else 0,
                1 if c["review_recommended"] else 0, REVIEW_AWAITING,
                now_ts, now_ts))
            stored.append({"id": new_id, **c, "suspicion": suspicion,
                           "review_status": REVIEW_AWAITING})
    return stored


def _suspicion(c: dict) -> str:
    """One neutral sentence for the notification, never over-claiming."""
    scheme = c.get("scheme_answer") or "the reference answer"
    verified = c.get("verified_answer") or "an independent solution"
    if c["state"] == STATE_CONFIRMED:
        return (f"Supplied mark-scheme answer ({scheme}) appears incorrect; "
                f"independent verification gives {verified}.")
    if c["state"] == STATE_VALID_ALTERNATIVE:
        return ("The student's answer is a valid equivalent/alternative and "
                "should not be penalised for its form or method.")
    if c["state"] == STATE_UNCERTAIN:
        return "The evidence is insufficient to decide; human review required."
    return (f"Possible mark-scheme error: reference says {scheme}, independent "
            f"verification suggests {verified}.")


# ------------------------------------------------------------------- reads (section 9)

def list_discrepancies(*, coursework_id: str | None = None,
                       review_status: str | None = None,
                       limit: int = 200) -> list[dict]:
    clauses, params = [], []
    if coursework_id:
        clauses.append("coursework_id = ?")
        params.append(coursework_id)
    if review_status:
        clauses.append("review_status = ?")
        params.append(review_status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.query(
        f"SELECT * FROM scheme_discrepancies{where} "
        "ORDER BY review_required DESC, updated_at DESC LIMIT ?",
        (*params, limit))
    return [_decorate(dict(r)) for r in rows]


def get_discrepancy(discrepancy_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM scheme_discrepancies WHERE id = ?",
                       (discrepancy_id,))
    return _decorate(dict(row)) if row else None


def _decorate(row: dict) -> dict:
    row["review_required"] = bool(row.get("review_required"))
    row["review_recommended"] = bool(row.get("review_recommended"))
    row["evidence"] = {
        "scheme_answer": row.get("scheme_answer"),
        "verified_answer": row.get("verified_answer"),
        "reasoning": row.get("verified_reasoning"),
        "confidence": row.get("confidence"),
    }
    return row


#: Lower rank = more severe. Used to pick the headline state of a group.
_STATE_RANK = {STATE_CONFIRMED: 0, STATE_POSSIBLE: 1, STATE_UNCERTAIN: 2,
               STATE_VALID_ALTERNATIVE: 3}


def notifications(*, limit: int = 50) -> list[dict]:
    """Open discrepancies as notification items, grouped per assignment+question.

    Duplicate notifications are structurally impossible at two levels:

    * re-detecting the same (class + assignment + question + type) UPDATES the
      existing row instead of adding another, and a completed human review
      removes the row from this feed; and
    * several records for the SAME assignment and question (e.g. the marker
      labelled one student's case ``incorrect_answer`` and another's
      ``arithmetic_error``) fold into ONE notification, so the teacher is asked
      to review a question once - not once per discrepancy label.
    """
    if not enabled():
        return []
    # Pull more rows than ``limit`` because several records per question can
    # collapse into a single notification.
    rows = db.query(
        "SELECT * FROM scheme_discrepancies WHERE review_status = ? "
        "AND review_required = 1 ORDER BY updated_at DESC LIMIT ?",
        (REVIEW_AWAITING, max(limit * 10, 200)))
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for r in rows:
        row = dict(r)
        key = (row.get("course_id"), row.get("coursework_id") or row.get("student_result_id"),
               str(row.get("question_number")))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    items: list[dict] = []
    for key in order[:limit]:
        group = groups[key]
        rep = min(group, key=lambda g: _STATE_RANK.get(g["state"], 9))
        confirmed = rep["state"] == STATE_CONFIRMED
        others = len(group) - 1
        detail = f"Q{rep['question_number']}: {rep.get('suspicion') or ''}"
        if others:
            detail += (f" ({others} more related record(s) on this "
                       "question)")
        items.append({
            "type": "mark_scheme_discrepancy",
            "tone": "err" if confirmed else "warn",
            "icon": "alert",
            "title": ("Confirmed mark-scheme error" if confirmed
                      else "Possible mark-scheme error")
                     + f" - {rep.get('assignment_title') or 'assignment'}",
            "detail": detail[:200],
            "route": f"#/scheme-review/{rep['id']}",
            "count": 1,
            "discrepancy_id": rep["id"],
            "state": rep["state"],
            "related_count": others,
        })
    return items


# ------------------------------------------------ bulk proposals (section 7, approval-gated)

def build_proposal(discrepancy_id: str) -> dict:
    """Stage a bulk correction for OTHER already-graded submissions.

    Computes the affected submissions and each one's proposed marks from the
    evidence already stored on their result rows. It NEVER changes a grade and
    NEVER overwrites the mark scheme. Re-staging an already-proposed
    discrepancy returns the existing proposal instead of duplicating it.
    """
    discrepancy = get_discrepancy(discrepancy_id)
    if discrepancy is None:
        raise LookupError("That discrepancy does not exist.")
    # Re-staging returns the SAME proposal - including one already applied (or
    # mid-recovery), so a second staging can never produce a duplicate batch
    # that could be approved twice.
    existing = db.query_one(
        "SELECT * FROM bulk_correction_proposals WHERE discrepancy_id = ? "
        "AND status IN (?,?,?,?) ORDER BY created_at DESC LIMIT 1",
        (discrepancy_id, PROPOSAL_PROPOSED, PROPOSAL_APPROVED, PROPOSAL_PARTIAL,
         PROPOSAL_APPLIED))
    if existing:
        return proposal_detail(existing["id"])

    coursework_id = discrepancy.get("coursework_id")
    question_number = discrepancy["question_number"]
    results = db.query(
        "SELECT * FROM results WHERE coursework_id = ? "
        "AND submission_id IS NOT NULL", (coursework_id,)) if coursework_id else []

    items: list[dict] = []
    for r in results:
        r = dict(r)
        payload = db.loads(r["result_json"], {}) or {}
        question = next((q for q in (payload.get("questions") or [])
                         if str(q.get("question_number")) == question_number),
                        None)
        if question is None:
            continue
        awarded = _as_int(question.get("awarded_marks"), 0) or 0
        max_marks = _as_int(question.get("max_marks"), 0) or 0
        check = question.get("scheme_check")
        assessment = classify(check, max_marks=max_marks, awarded=awarded)
        proposed = assessment["justified_marks"] if assessment and \
            assessment["student_answer_valid"] and assessment["reasoning"] and \
            assessment["verified_answer"] and assessment["confidence"] >= confirm_confidence() and \
            assessment["state"] != STATE_UNCERTAIN else None
        # A submission the teacher already decided by hand holds a deliberate
        # human mark ("teacher override wins"): NEVER overwrite it from a bulk
        # batch. It stays visible as requiring an individual re-mark instead.
        if int(r.get("manual_override") or 0):
            proposed = None
        # No independently justified mark for THIS submission => no automatic
        # change is proposed. It is shown to the teacher as requiring a re-mark,
        # never silently adjusted.
        items.append({
            "result_id": r["id"],
            "submission_id": r["submission_id"],
            "student_id": r["student_id"],
            "student_name": r.get("student_name"),
            "original_earned": r["earned"],
            "proposed_earned": None if proposed is None
                else (r["earned"] - awarded + proposed),
            "delta_points": None if proposed is None else (proposed - awarded),
            "max_marks": max_marks,
            "question_awarded_before": awarded,
            "question_awarded_after": proposed,
        })

    changed = [i for i in items
               if i["question_awarded_after"] is not None
               and i["delta_points"] not in (0, None)]
    proposal_id = uuid.uuid4().hex
    now_ts = time.time()
    reason = _suspicion(discrepancy)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO bulk_correction_proposals(
                id, coursework_id, course_id, discrepancy_id, question_number,
                scheme_answer, verified_answer, reason, status, created_by,
                created_at, affected_count, changed_count)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (proposal_id, coursework_id, discrepancy.get("course_id"),
             discrepancy_id, question_number, discrepancy.get("scheme_answer"),
             discrepancy.get("verified_answer"), reason, PROPOSAL_PROPOSED,
             "system", now_ts, len(items), len(changed)))
        for item in items:
            conn.execute(
                """
                INSERT INTO bulk_correction_items(
                    id, proposal_id, result_id, submission_id, student_id,
                    student_name, original_earned, proposed_earned, delta_points,
                    max_marks, question_awarded_before, question_awarded_after,
                    status, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (uuid.uuid4().hex, proposal_id, item["result_id"],
                 item["submission_id"], item["student_id"], item["student_name"],
                 item["original_earned"], item["proposed_earned"],
                 item["delta_points"], item["max_marks"],
                 item["question_awarded_before"], item["question_awarded_after"],
                 ITEM_PENDING, now_ts))
    return proposal_detail(proposal_id)


def list_proposals(*, coursework_id: str | None = None,
                   status: str | None = None, limit: int = 100) -> list[dict]:
    clauses, params = [], []
    if coursework_id:
        clauses.append("coursework_id = ?")
        params.append(coursework_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.query(
        f"SELECT * FROM bulk_correction_proposals{where} ORDER BY created_at DESC "
        "LIMIT ?", (*params, limit))
    return [dict(r) for r in rows]


def proposal_detail(proposal_id: str) -> dict:
    row = db.query_one("SELECT * FROM bulk_correction_proposals WHERE id = ?",
                       (proposal_id,))
    if not row:
        raise LookupError("That proposal does not exist.")
    items = db.query(
        "SELECT * FROM bulk_correction_items WHERE proposal_id = ? "
        "ORDER BY student_name", (proposal_id,))
    return {**dict(row), "items": [dict(i) for i in items]}


def apply_approved(proposal_id: str, *, approved_by: str,
                   note: str = "", publish: bool = False,
                   publish_fn: Callable[[list[dict]], dict] | None = None,
                   retry_failed: bool = False) -> dict:
    """Apply a staged proposal AFTER explicit human approval.

    Safety properties (all tested):

    * refuses any proposal that has not been left in a pending state - an
      already approved/applied/partial proposal is returned unchanged, so the
      same approved change can never be applied twice;
    * a rejected proposal can never be applied;
    * EVERY original mark is snapshotted into ``result_versions`` and the item
      row before the results row is updated;
    * each item is applied independently: one failure never hides the others
      and the batch is reported ``PARTIAL`` (never "successful") if any failed;
    * the mark scheme itself is never touched.
    """
    from . import legacy_marking as legacy
    from . import versions

    now_ts = time.time()
    applied = failed = skipped = 0
    with db.transaction() as conn:
        # Read and claim under BEGIN IMMEDIATE, not before the transaction:
        # concurrent approvals cannot both apply the same pending item.
        found = conn.execute("SELECT * FROM bulk_correction_proposals WHERE id = ?",
                             (proposal_id,)).fetchone()
        if not found:
            raise LookupError("That proposal does not exist.")
        row = dict(found)
        status = row["status"]
        if status in (PROPOSAL_APPLIED, PROPOSAL_APPROVED) or \
                (status == PROPOSAL_PARTIAL and not retry_failed):
            return proposal_detail(proposal_id)
        if status == PROPOSAL_REJECTED:
            raise ValueError("This proposal was rejected and cannot be applied.")
        if retry_failed and (status != PROPOSAL_PARTIAL or not row.get("decided_at")):
            raise ValueError("Only failed items of an explicitly approved batch may be retried.")
        items = conn.execute("SELECT * FROM bulk_correction_items WHERE proposal_id = ?",
                             (proposal_id,)).fetchall()
        if not retry_failed:
            conn.execute(
                "UPDATE bulk_correction_proposals SET status = ?, decided_by = ?, "
                "decided_at = ?, note = ? WHERE id = ?",
                (PROPOSAL_APPROVED, approved_by, now_ts, note[:500], proposal_id))
        for item in items:
            item = dict(item)
            if item["status"] != (ITEM_FAILED if retry_failed else ITEM_PENDING):
                continue
            after = item.get("question_awarded_after")
            if after is None or item.get("delta_points") in (0, None):
                conn.execute(
                    "UPDATE bulk_correction_items SET status = ? WHERE id = ?",
                    (ITEM_SKIPPED, item["id"]))
                skipped += 1
                continue
            conn.execute("SAVEPOINT scheme_item")
            try:
                _apply_item(conn, item, after, now_ts, legacy, versions,
                            row.get("decided_by") if retry_failed else approved_by,
                            proposal_id, row["question_number"])
                conn.execute("RELEASE scheme_item")
                applied += 1
            except Exception as exc:  # noqa: BLE001 - per-item, must be recorded
                conn.execute("ROLLBACK TO scheme_item")
                conn.execute("RELEASE scheme_item")
                conn.execute(
                    "UPDATE bulk_correction_items SET status = ?, error = ?, "
                    "applied_at = ? WHERE id = ?",
                    (ITEM_FAILED, str(exc)[:500], now_ts, item["id"]))
                failed += 1
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM bulk_correction_items WHERE proposal_id = ? GROUP BY status",
            (proposal_id,)).fetchall()}
        applied, failed = counts.get(ITEM_APPLIED, 0), counts.get(ITEM_FAILED, 0)
        final_status = (PROPOSAL_APPLIED if failed == 0 else PROPOSAL_PARTIAL)
        conn.execute(
            "UPDATE bulk_correction_proposals SET status = ?, applied_at = ?, "
            "applied_count = ?, failed_count = ?, note = ? WHERE id = ?",
            (final_status, now_ts, applied, failed, note[:500], proposal_id))
        # Approval and recovery are distinct audit events.
        conn.execute(
            "INSERT INTO scheme_discrepancy_reviews(discrepancy_id, decision, "
            "decided_by, note, ai_recommendation, created_at) VALUES(?,?,?,?,?,?)",
            (row["discrepancy_id"], "RETRY_APPROVED_FAILED_ITEMS" if retry_failed else "APPROVE_BULK_CORRECTION", approved_by,
             note[:500], row.get("reason"), now_ts))
        conn.execute(
            "UPDATE scheme_discrepancies SET review_status = ?, updated_at = ? "
            "WHERE id = ?", (REVIEW_COMPLETED, now_ts, row["discrepancy_id"]))

    result = proposal_detail(proposal_id)
    if publish and publish_fn is not None:
        publishable = [i for i in result["items"] if i["status"] == ITEM_APPLIED]
        try:
            report = publish_fn(publishable) or {}
        except Exception as exc:  # noqa: BLE001 - must be reported, never hidden
            report = {"error": str(exc)[:500]}
        state = classroom_publish_state(report)
        db.execute(
            "UPDATE bulk_correction_proposals SET classroom_publish_state = ? "
            "WHERE id = ?", (state, proposal_id))
        result["classroom_publish"] = report
        result["classroom_publish_state"] = state
    return result


def classroom_publish_state(report: Any) -> str:
    """Map a Classroom write-back report to an honest state string.

    Accepts the normalised report the route builds AND a raw provider report
    with a nested ``summary`` so a partial (or fully failed) provider response
    can never be displayed as a clean success. Never invents success.
    """
    if not isinstance(report, dict):
        return "failed"
    if report.get("error"):
        return "failed"
    inner = report.get("summary")
    summary = inner if isinstance(inner, dict) else report
    failed = _as_int(summary.get("failed"), 0) or 0
    auth = _as_int(summary.get("requires_auth"), 0) or 0
    unsupported = _as_int(summary.get("not_supported"), 0) or 0
    published = _as_int(summary.get("published"), 0) or 0
    total = _as_int(summary.get("total"), 0) or 0
    if failed:
        return "partial" if published else "failed"
    if auth:
        return "requires_auth"
    if unsupported:
        return "not_supported"
    if published == 0 and total <= 0:
        return "nothing_to_publish"
    return "published" if published and (not total or published == total) else "failed"


def _apply_item(conn, item: dict, after: int, now_ts: float, legacy, versions,
                approved_by: str, proposal_id: str, question_number: str) -> bool:
    """Update ONE result row from a proposal item. Returns True if it changed."""
    import json
    from ..automation.grading import compute_grade

    row = conn.execute("SELECT * FROM results WHERE id = ?",
                       (item["result_id"],)).fetchone()
    if row is None:
        raise RuntimeError("The stored result no longer exists.")
    payload = db.loads(row["result_json"], {}) or {}
    target = next((q for q in (payload.get("questions") or [])
                   if str(q.get("question_number")) == question_number), None)
    if target is None:
        raise RuntimeError("The question is missing from the stored result.")
    before_q = _as_int(target.get("awarded_marks"), 0) or 0
    max_marks = _as_int(target.get("max_marks"), 0) or 0
    if int(row["manual_override"] or 0):
        raise RuntimeError("A teacher override was recorded after staging. Prepare a new individual review.")
    if (before_q != item["question_awarded_before"] or max_marks != item["max_marks"]
            or row["earned"] != item["original_earned"]):
        raise RuntimeError("The result changed after staging. Review the new marks before applying.")
    if not 0 <= int(after) <= max_marks:
        raise RuntimeError("The proposed mark exceeds the current question maximum.")
    target["awarded_marks"] = int(after)

    earned, available, percentage = legacy.calculate_totals(payload)
    # Preserve the ORIGINAL marks for auditing BEFORE overwriting the row.
    versions.record(
        conn, result_id=row["id"], earned=row["earned"],
        available=row["available"], percentage=row["percentage"],
        provisional_grade=row["provisional_grade"],
        marking_model=row["marking_model"], key_fingerprint=row["key_fingerprint"],
        confidence=row["confidence"], confidence_reasons=row["confidence_reasons"],
        verification=row["verification_json"], result=row["result_json"],
        created_at=now_ts,
        note=(f"Original marks preserved before approved mark-scheme correction "
              f"(proposal {proposal_id}, Q{question_number})"))
    conn.execute(
        """
        UPDATE results SET earned = ?, available = ?, percentage = ?,
            provisional_grade = ?, result_json = ?, manual_override = 1,
            review_required = 0, confidence_reasons = ? WHERE id = ?
        """,
        (earned, available, percentage,
         compute_grade(
             earned, available, coursework_id=row["coursework_id"]).grade,
         json.dumps(payload, ensure_ascii=False),
         json.dumps([f"Approved mark-scheme correction by {approved_by} "
                     f"(proposal {proposal_id})"], ensure_ascii=False),
         row["id"]))
    current = conn.execute("SELECT * FROM results WHERE id = ?", (row["id"],)).fetchone()
    versions.record(
        conn, result_id=current["id"], earned=current["earned"], available=current["available"],
        percentage=current["percentage"], provisional_grade=current["provisional_grade"],
        marking_model=current["marking_model"], key_fingerprint=current["key_fingerprint"],
        confidence=current["confidence"], confidence_reasons=current["confidence_reasons"],
        verification=current["verification_json"], result=current["result_json"], created_at=now_ts,
        note=f"Approved bulk correction {proposal_id} by {approved_by}")
    conn.execute(
        "UPDATE bulk_correction_items SET status = ?, error = NULL, applied_at = ?, "
        "question_awarded_before = ?, question_awarded_after = ? WHERE id = ?",
        (ITEM_APPLIED, now_ts, before_q, target["awarded_marks"], item["id"]))
    return before_q != target["awarded_marks"]


def reject_proposal(proposal_id: str, *, rejected_by: str, note: str = "") -> dict:
    """Reject a staged proposal. The grade and the discrepancy record survive."""
    found = db.query_one("SELECT * FROM bulk_correction_proposals WHERE id = ?",
                         (proposal_id,))
    if not found:
        raise LookupError("That proposal does not exist.")
    row = dict(found)
    if row["status"] in (PROPOSAL_APPLIED, PROPOSAL_PARTIAL):
        raise ValueError("This proposal has already been applied.")
    now_ts = time.time()
    with db.transaction() as conn:
        current = conn.execute("SELECT status FROM bulk_correction_proposals WHERE id=?",
                               (proposal_id,)).fetchone()
        if current["status"] in (PROPOSAL_APPLIED, PROPOSAL_PARTIAL, PROPOSAL_APPROVED):
            raise ValueError("This proposal has already been approved or applied.")
        conn.execute(
            "UPDATE bulk_correction_proposals SET status = ?, decided_by = ?, "
            "decided_at = ?, note = ? WHERE id = ?",
            (PROPOSAL_REJECTED, rejected_by, now_ts, note[:500], proposal_id))
        conn.execute(
            "INSERT INTO scheme_discrepancy_reviews(discrepancy_id, decision, "
            "decided_by, note, ai_recommendation, created_at) VALUES(?,?,?,?,?,?)",
            (row["discrepancy_id"], DECISION_REJECT_BULK, rejected_by,
             note[:500], row.get("reason"), now_ts))
        # Keep the discrepancy record; it is simply no longer an open bulk item.
        conn.execute(
            "UPDATE scheme_discrepancies SET review_status = ?, review_required "
            "= 0, updated_at = ? WHERE id = ?",
            (REVIEW_COMPLETED, now_ts, row["discrepancy_id"]))
    return proposal_detail(proposal_id)


def record_decision(discrepancy_id: str, *, decision: str, decided_by: str,
                    note: str = "") -> dict:
    """Record an authorised reviewer's outcome on a discrepancy (section 8)."""
    decision = (decision or "").upper()
    if decision not in DECISIONS:
        raise ValueError(f"Unknown decision '{decision}'.")
    row = db.query_one("SELECT * FROM scheme_discrepancies WHERE id = ?",
                       (discrepancy_id,))
    if row is None:
        raise LookupError("That discrepancy does not exist.")
    now_ts = time.time()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO scheme_discrepancy_reviews(discrepancy_id, decision, "
            "decided_by, note, ai_recommendation, created_at) VALUES(?,?,?,?,?,?)",
            (discrepancy_id, decision, decided_by, note[:500], row["state"],
             now_ts))
        # The AI's classification stays in the review row; the live row records
        # that a human has now decided.
        conn.execute(
            "UPDATE scheme_discrepancies SET review_status = ?, review_required "
            "= 0, updated_at = ? WHERE id = ?",
            (REVIEW_AWAITING if decision == DECISION_REQUEST_INVESTIGATION else REVIEW_COMPLETED,
             now_ts, discrepancy_id))
        conn.execute("UPDATE scheme_discrepancies SET review_required = ?, state = ? WHERE id = ?",
                     (1 if decision == DECISION_REQUEST_INVESTIGATION else 0,
                      STATE_CONFIRMED if decision == DECISION_CONFIRM_SCHEME_WRONG else
                      (row["state"] if decision == DECISION_REQUEST_INVESTIGATION else STATE_HUMAN_REVIEWED),
                      discrepancy_id))
    return get_discrepancy(discrepancy_id) or {}


def review_history(discrepancy_id: str) -> list[dict]:
    rows = db.query(
        "SELECT * FROM scheme_discrepancy_reviews WHERE discrepancy_id = ? "
        "ORDER BY created_at DESC", (discrepancy_id,))
    return [dict(r) for r in rows]
