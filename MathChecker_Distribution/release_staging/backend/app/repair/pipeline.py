"""Repair pipeline logic: inventory, comparison and deterministic validation.

Marking itself runs elsewhere (isolated data dir, real pipeline). This module
holds the parts that must be pure and testable:

* :func:`assignment_inventory` — every submission and existing correction for
  an assignment, including what could not be accessed (never silently dropped);
* :func:`compare_rows` — per-question diff between the ORIGINAL stored
  correction and the REPAIRED one, with the evidence and criteria for each
  change;
* :func:`validate_repair` — deterministic checks that must pass before a
  repaired result may be called verified (coverage, maxima, totals, silent
  zeros, unresolved questions).

The validation layer is deliberately independent of the AI's opinion of its
own work: it checks structure and accounting only.
"""

from __future__ import annotations

from .audit import (
    UNRESOLVED_STATUSES, Finding, audit_result, blueprint_maxima,
)

#: Per-question comparison verdicts.
MATCH = "MATCH"                      # second pass agrees on the mark
ADDED = "ADDED"                      # original had no mark; repair produced one
CHANGED = "CHANGED"                  # two passes disagree on the mark
UNRESOLVED = "UNRESOLVED"            # still no mark after the repair
DETERMINISTIC_FIX = "DETERMINISTIC_FIX"  # accounting fixed, mark unchanged
REMOVED = "REMOVED"                  # repair dropped a phantom row

#: How much a verdict may be trusted without a teacher.
SECOND_PASS_AGREES = "SECOND_PASS_AGREES (consistency evidence only)"
TEACHER_CONFIRM = "TEACHER_CONFIRMATION_REQUIRED"
DETERMINISTIC = "DETERMINISTICALLY_VERIFIED (accounting)"
UNRESOLVED_LABEL = "UNRESOLVED (explicit, not a zero)"


def assignment_inventory(coursework_id: str) -> dict:
    """Read-only inventory of every submission and correction for an
    assignment. Submissions that cannot be processed are listed explicitly."""
    import json

    from .. import db

    subs = [db.row_to_dict(r) for r in db.query(
        "SELECT * FROM classroom_submissions WHERE coursework_id = ?",
        (coursework_id,))]
    results = [db.row_to_dict(r) for r in db.query(
        "SELECT * FROM results WHERE coursework_id = ?", (coursework_id,))]
    jobs = [db.row_to_dict(r) for r in db.query(
        "SELECT id, student_name, student_id, status, error, result_id, "
        "created_at FROM large_pdf_jobs WHERE coursework_id = ?",
        (coursework_id,))]
    by_student_result = {str(r["student_id"]): r for r in results}

    rows = []
    for sub in subs:
        attachments = db.loads(sub.get("attachments_json") or "[]") or []
        result = by_student_result.get(str(sub["student_id"]))
        state = str(sub.get("current_state") or "")
        submitted = state in ("TURNED_IN", "RETURNED", "GRADED")
        kinds = []
        for att in attachments:
            kind = str(att.get("kind") or "").lower()
            title = str(att.get("title") or att.get("filename") or "")
            link = str(att.get("linkUrl") or att.get("url") or "")
            urlish = str(att.get("id") or "")
            drive = att.get("driveFile") or {}
            is_link = (kind == "link" or
                       (link + urlish).startswith("http") and not drive.get("id"))
            if is_link:
                kinds.append("link")
            elif title.lower().endswith(".zip"):
                kinds.append("zip")
            elif kind == "drive" or drive.get("id") or \
                    title.lower().endswith(".pdf"):
                kinds.append("pdf")
            else:
                kinds.append("other")
        if not submitted:
            accessible, note = False, "not submitted"
        elif "zip" in kinds and "pdf" not in kinds:
            accessible, note = False, ("invalid submission type (archive) — "
                                       "no readable student work")
        elif "pdf" not in kinds and "link" in kinds and "other" not in kinds:
            accessible, note = False, ("link-only attachment — no file the "
                                       "pipeline can read")
        elif not attachments:
            accessible, note = False, "no attachment"
        else:
            accessible, note = True, ""
        rows.append({
            "submission_id": sub.get("submission_id"),
            "student_id": sub.get("student_id"),
            "student_name": sub.get("student_name"),
            "state": state,
            "submitted": submitted,
            "attachment_kinds": kinds,
            "attachment_count": len(attachments),
            "attachments": [{"kind": a.get("kind"), "id": a.get("id"),
                             "title": a.get("title")}
                            for a in attachments],
            "has_correction": bool(result),
            "result_id": result.get("id") if result else None,
            "stored_earned": result.get("earned") if result else None,
            "stored_available": result.get("available") if result else None,
            "stored_percentage": result.get("percentage") if result else None,
            "accessible": accessible,
            "note": note,
        })
    rows.sort(key=lambda r: (not r["submitted"], str(r["student_name"] or "")))
    corrected = [r for r in rows if r["has_correction"]]
    return {
        "coursework_id": coursework_id,
        "submissions": rows,
        "counts": {
            "total": len(rows),
            "submitted": sum(1 for r in rows if r["submitted"]),
            "corrected": len(corrected),
            "uncorrected": sum(1 for r in rows
                               if r["submitted"] and not r["has_correction"]),
            "not_submitted": sum(1 for r in rows if not r["submitted"]),
            "accessible": sum(1 for r in rows if r["accessible"]),
            "inaccessible": sum(1 for r in rows
                                if r["submitted"] and not r["accessible"]),
        },
        "jobs": jobs,
    }


def compare_rows(*, original: list[dict] | None,
                 repaired: list[dict] | None,
                 blueprint: dict) -> list[dict]:
    """Per-question diff original vs repaired, with evidence and criteria."""
    maxima = blueprint_maxima(blueprint)
    criteria = {str(q["question_number"]): str(q.get("criteria") or "")
                for q in (blueprint.get("questions") or [])}
    orig = {str(q.get("question_number")): q for q in (original or [])}
    rep = {str(q.get("question_number")): q for q in (repaired or [])}
    diff: list[dict] = []
    for number in maxima:
        before = orig.get(number)
        after = rep.get(number)
        b_marks = (None if before is None
                   else float(before.get("awarded_marks") or 0))
        a_marks = (None if after is None
                   else float(after.get("awarded_marks") or 0))
        b_status = str((before or {}).get("status") or "").upper()
        a_status = str((after or {}).get("status") or "").upper()
        b_unresolved = before is None or b_status in UNRESOLVED_STATUSES
        a_unresolved = after is None or a_status in UNRESOLVED_STATUSES

        if a_unresolved:
            verdict, trust = UNRESOLVED, UNRESOLVED_LABEL
            reason = ((after or {}).get("review_reason")
                      or "the repaired run still produced no mark")
        elif b_unresolved:
            verdict, trust = ADDED, TEACHER_CONFIRM
            reason = ("the original correction had NO mark for this question "
                      "(unmarked is not 0); the repair marked it from the "
                      "student's actual work")
        elif b_marks != a_marks:
            verdict, trust = CHANGED, TEACHER_CONFIRM
            reason = (f"the two passes disagree ({b_marks:g} vs {a_marks:g} "
                      f"of {maxima[number]}) — both versions are preserved; "
                      f"the teacher decides")
        elif before is not None and int(before.get("max_marks") or 0) != \
                int(maxima[number]):
            verdict, trust = DETERMINISTIC_FIX, DETERMINISTIC
            reason = (f"the mark ({a_marks:g}) is unchanged, but the stored "
                      f"maximum was {before.get('max_marks')} instead of the "
                      f"official {maxima[number]} — the accounting was fixed "
                      f"to the mark scheme")
        else:
            verdict, trust = MATCH, SECOND_PASS_AGREES
            reason = "both passes agree on this mark"

        evidence = ""
        if after is not None:
            evidence = " | ".join(str(after.get(k) or "").strip() for k in
                                  ("student_answer", "working_summary",
                                   "loss_reason") if str(after.get(k) or "").strip())
        diff.append({
            "question": number,
            "max_marks": maxima[number],
            "original_awarded": b_marks,
            "repaired_awarded": a_marks,
            "original_status": b_status or ("MISSING" if before is None else "MARKED"),
            "repaired_status": a_status or ("MISSING" if after is None else "MARKED"),
            "change": verdict,
            "validation": trust,
            "reason": reason,
            "evidence": evidence[:600],
            "criteria": criteria.get(number, "")[:600],
            "original_pages": (before or {}).get("source_pages")
                              or (before or {}).get("pages"),
            "repaired_pages": (after or {}).get("source_pages")
                              or (after or {}).get("pages"),
        })
    # Rows the repair introduced that the mark scheme does not declare.
    for number in rep:
        if number not in maxima:
            diff.append({
                "question": number,
                "max_marks": None,
                "original_awarded": None,
                "repaired_awarded": float(rep[number].get("awarded_marks") or 0),
                "change": REMOVED,
                "validation": DETERMINISTIC,
                "reason": ("the mark scheme does not declare this question; "
                           "phantom rows are excluded from every total"),
                "evidence": "", "criteria": "",
            })
    return diff


def validate_repair(*, questions: list[dict], earned, available,
                    blueprint: dict, score_basis: str | None = None,
                    unresolved_marks: float | None = None) -> dict:
    """Deterministic validation of a REPAIRED result.

    ``ok`` is True only when every check passes and no question is left
    unresolved — a repaired result with unresolved questions is provisional,
    never 'fully verified'.
    """
    findings = audit_result(
        questions=questions, earned=earned, available=available,
        blueprint=blueprint, score_basis=score_basis,
        unresolved_marks=unresolved_marks)
    # A repaired result may legitimately contain NEEDS_REVIEW rows with an
    # explicit reason — those count as unresolved but not as silent zeros.
    rows = {str(q.get("question_number")): q for q in (questions or [])}
    maxima = blueprint_maxima(blueprint)
    unresolved = [n for n, q in rows.items()
                  if str(q.get("status") or "").upper() in UNRESOLVED_STATUSES]
    missing = [n for n in maxima if n not in rows]
    unmarked_without_reason = [
        n for n in unresolved if not str(rows[n].get("review_reason") or "").strip()]

    checks = {
        "inventory_complete": not missing,
        "no_duplicates": len(rows) == len(questions or []),
        "maxima_match_scheme": all(
            int(rows[n].get("max_marks") or 0) == m
            for n, m in maxima.items() if n in rows),
        "totals_consistent": (
            earned is not None
            and abs(sum(float(q.get("awarded_marks") or 0)
                        for q in (questions or [])) - float(earned)) < 1e-9),
        "denominator_is_scheme_total": (
            available is not None and float(available) == sum(maxima.values())),
        "no_marks_over_maximum": all(
            float(rows[n].get("awarded_marks") or 0) <= m
            for n, m in maxima.items() if n in rows),
        "zeros_have_evidence": not any(
            float(q.get("awarded_marks") or 0) == 0
            and str(q.get("status") or "").upper() not in UNRESOLVED_STATUSES
            and not any(str(q.get(k) or "").strip()
                        for k in ("student_answer", "working_summary",
                                  "feedback", "loss_reason", "mark_breakdown"))
            for q in (questions or [])),
        "unresolved_are_explicit": not unmarked_without_reason,
        "no_unresolved_questions": not unresolved and not missing,
    }
    blocking = [f for f in findings
                if f.severity == "high" and f.code != "UNMARKED_AS_ZERO"]
    ok = all(checks.values()) and not blocking
    return {
        "ok": ok,
        "checks": checks,
        "unresolved_questions": sorted(unresolved, key=_qid),
        "missing_questions": sorted(missing, key=_qid),
        "findings": [{"code": f.code, "severity": f.severity,
                      "question": f.question, "detail": f.detail}
                     for f in findings],
    }


def item_status(diff: list[dict], validation: dict,
                error: str | None = None) -> str:
    """Overall verdict for a repaired submission.

    HONEST LABELS: "MATCH" means the corrected second pass agrees with the
    stored marks (consistency evidence), "REPAIRED" means the repair produced
    marks/accounting the original lacked — every changed question still asks
    for teacher confirmation, and anything contradictory or unresolved is
    NEEDS_REVIEW / UNRESOLVED, never a quiet success.
    """
    if error:
        return "FAILED"
    changes = {d["change"] for d in diff}
    if "UNRESOLVED" in changes or validation.get("missing_questions"):
        return "UNRESOLVED"
    if "CHANGED" in changes:
        return "NEEDS_REVIEW"
    if "ADDED" in changes:
        without = [d for d in diff if d["change"] == "ADDED"
                   and not str(d.get("evidence") or "").strip()]
        return "NEEDS_REVIEW" if without else "REPAIRED"
    if not validation.get("ok"):
        return "NEEDS_REVIEW"
    return "MATCH"


def _qid(value: str):
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))


def audit_stored_result(result_row: dict, blueprint: dict) -> list[Finding]:
    """Audit a ``results`` row as stored today."""
    import json

    data = result_row.get("result_json")
    if isinstance(data, str):
        data = json.loads(data or "{}")
    data = data or {}
    totals = data.get("totals") or {}
    return audit_result(
        questions=data.get("questions") or [],
        earned=result_row.get("earned"),
        available=result_row.get("available"),
        blueprint=blueprint,
        score_basis=totals.get("score_basis"),
        unresolved_marks=totals.get("unresolved_marks"),
    )
