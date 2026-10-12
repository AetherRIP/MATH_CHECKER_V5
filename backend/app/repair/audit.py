"""Deterministic audit of a STORED correction against the mark-scheme blueprint.

This layer is deliberately independent of the AI: it checks accounting,
coverage and structure — the classes of defect actually found on the live
C1L13 - Sets corrections (per-question maxima leaking from the model, an
unmarked question charged to the student as a 0, a wrong denominator):

* ``MISSING_QUESTION``      mark-scheme question with no result row at all
* ``DUPLICATE_QUESTION``    the same question answered twice (double-counted)
* ``MAX_MISMATCH``          row maxima differ from the official mark scheme
* ``DENOMINATOR_MISMATCH``  the stored total is not the scheme's total
* ``OVER_MAX``              awarded marks exceed the official maximum
* ``SUM_MISMATCH``          stored total != sum of the accepted question marks
* ``UNMARKED_AS_ZERO``      an unresolved question is counted as a scored 0
* ``SUSPICIOUS_ZERO``       a 0 with no evidence the answer was actually read
* ``PERFECT_SCORE``         full marks — must be independently validated

Every finding names the question (where applicable) and the exact numbers,
so a teacher can trace it. Nothing here changes any mark.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Row statuses that mean "no mark was produced" (never a confirmed 0).
UNRESOLVED_STATUSES = {"REVIEW", "NEEDS_REVIEW", "UNMARKED", "PROCESSING_ERROR"}


@dataclass
class Finding:
    code: str
    severity: str                 # high | medium | low | info
    detail: str
    question: str | None = None
    numbers: dict = field(default_factory=dict)


def blueprint_maxima(blueprint: dict) -> dict[str, int]:
    return {str(q["question_number"]): int(q.get("max_marks") or 0)
            for q in (blueprint.get("questions") or [])}


def _evidence_present(row: dict) -> bool:
    """Did anything actually evaluate this row (as opposed to a bare 0)?"""
    for key in ("student_answer", "working_summary", "feedback", "loss_reason"):
        if str(row.get(key) or "").strip():
            return True
    if row.get("mark_breakdown"):
        return True
    return False


def audit_result(*, questions: list[dict], earned, available,
                 blueprint: dict, score_basis: str | None = None,
                 unresolved_marks: float | None = None,
                 unresolved_count: int | None = None) -> list[Finding]:
    """Audit one stored correction. Pure function; changes nothing."""
    findings: list[Finding] = []
    maxima = blueprint_maxima(blueprint)
    scheme_total = sum(maxima.values())
    rows = [q for q in (questions or [])]
    by_number: dict[str, list[dict]] = {}
    for row in rows:
        by_number.setdefault(str(row.get("question_number")), []).append(row)

    # --- coverage ----------------------------------------------------------
    missing = [n for n in maxima if n not in by_number]
    for number in missing:
        findings.append(Finding(
            "MISSING_QUESTION", "high",
            f"Question {number} ({maxima[number]} marks) is required by the "
            f"official mark scheme but has NO result row — it must be marked "
            f"NEEDS_REVIEW, never silently dropped or scored 0.",
            question=number, numbers={"max_marks": maxima[number]}))
    for number, group in by_number.items():
        if len(group) > 1:
            findings.append(Finding(
                "DUPLICATE_QUESTION", "high",
                f"Question {number} appears {len(group)} times in the stored "
                f"result — duplicate rows can double-count marks.",
                question=number, numbers={"rows": len(group)}))

    # --- per-question maxima / bounds --------------------------------------
    max_sum = 0
    awarded_sum = 0.0
    for number, group in by_number.items():
        row = group[0]
        row_max = int(row.get("max_marks") or 0)
        awarded = float(row.get("awarded_marks") or 0)
        max_sum += row_max
        awarded_sum += awarded
        expected = maxima.get(number)
        if expected is not None and row_max != expected:
            findings.append(Finding(
                "MAX_MISMATCH", "high",
                f"Question {number} is worth {expected} marks in the official "
                f"mark scheme but the stored row says {row_max} — the model's "
                f"own maximum leaked into the result.",
                question=number,
                numbers={"scheme_max": expected, "row_max": row_max}))
        cap = expected if expected is not None else row_max
        if cap and awarded > cap:
            findings.append(Finding(
                "OVER_MAX", "high",
                f"Question {number} was awarded {awarded:g} marks, more than "
                f"its {cap}-mark maximum.",
                question=number, numbers={"awarded": awarded, "max": cap}))

        status = str(row.get("status") or "").upper()
        if status in UNRESOLVED_STATUSES:
            # Unresolved questions are the marker's failure — they must not be
            # charged to the student inside a "verified" total.
            findings.append(Finding(
                "UNMARKED_AS_ZERO", "high",
                f"Question {number} is {status} (the marker produced no mark) "
                f"but its {maxima.get(number, row_max)} marks must not be "
                f"counted as the student's 0. Score only the marked questions "
                f"and name the unresolved remainder.",
                question=number,
                numbers={"max_marks": maxima.get(number, row_max)}))
        elif awarded == 0 and not _evidence_present(row):
            findings.append(Finding(
                "SUSPICIOUS_ZERO", "medium",
                f"Question {number} was awarded 0 with NO evidence recorded "
                f"(no student answer, working summary or loss reason) — a 0 "
                f"must be justified by the student's actual work, and a "
                f"missing response must never become a silent 0.",
                question=number))

    # --- totals ------------------------------------------------------------
    if earned is not None and abs(awarded_sum - float(earned)) > 1e-9:
        findings.append(Finding(
            "SUM_MISMATCH", "high",
            f"The stored total ({earned:g}) does not equal the sum of the "
            f"accepted question marks ({awarded_sum:g}).",
            numbers={"stored": float(earned), "sum": awarded_sum}))
    if scheme_total and available is not None and float(available) != scheme_total:
        findings.append(Finding(
            "DENOMINATOR_MISMATCH", "high",
            f"The stored total is out of {available:g} marks, but the official "
            f"mark scheme is worth {scheme_total}. A percentage out of the "
            f"wrong denominator is not the student's percentage.",
            numbers={"stored_available": float(available),
                     "scheme_total": scheme_total}))
    if max_sum != scheme_total and by_number and not missing:
        # Covered per-question above, but keep the aggregate visible too.
        findings.append(Finding(
            "DENOMINATOR_MISMATCH", "high",
            f"The stored rows add up to {max_sum} marks of question maxima; "
            f"the official mark scheme is worth {scheme_total}.",
            numbers={"row_max_sum": max_sum, "scheme_total": scheme_total}))

    # An unresolved question counted inside a COMPLETE/verified total is the
    # exact Alia defect: 32 unmarked questions divided into 266.
    unresolved_rows = [n for n, g in by_number.items()
                       if str(g[0].get("status") or "").upper()
                       in UNRESOLVED_STATUSES]
    if unresolved_rows and (score_basis in (None, "", "COMPLETE")):
        if not any(f.code == "UNMARKED_AS_ZERO" for f in findings):
            findings.append(Finding(
                "UNMARKED_AS_ZERO", "high",
                f"{len(unresolved_rows)} question(s) have no mark, yet the "
                f"total is presented as complete."))

    # --- flag perfect scores for independent validation ---------------------
    if earned is not None and scheme_total and float(earned) >= scheme_total:
        findings.append(Finding(
            "PERFECT_SCORE", "info",
            f"{earned:g}/{scheme_total} — a full-mark correction must be "
            f"independently validated against the student's work before it is "
            f"trusted (false 100% results have occurred)."))

    return findings


def summarise(findings: list[Finding]) -> dict:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.code] = counts.get(finding.code, 0) + 1
    return {
        "counts": counts,
        "total": len(findings),
        "high": sum(1 for f in findings if f.severity == "high"),
    }
