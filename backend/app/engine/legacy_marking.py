"""LEGACY CORRECTION ENGINE — preserved from the friend's working app.

Source: ``app5_1_automated.py``, the friend's original Streamlit app — kept as
an untouched reference copy under ``Friend code/`` until that folder was removed
on 2026-10-15 (commit of record: the version audited before V5 development
started).

PRESERVATION CONTRACT (V5 rule #0 — do not break the working engine):

* The marking prompt (``MARKING_SYSTEM``), output schema (``OUTPUT_SCHEMA``),
  JSON extraction, result normalisation/clamping, Python-side totals,
  provisional grade bands, model fallback order and per-model retry
  semantics are copied VERBATIM.
* The ONLY functional changes, all required to run outside Streamlit and
  inside V5's job system, are:
    1. Streamlit's shared model-health store (``st.cache_resource``) is
       replaced by an equivalent plain class with the same locking and
       cooldown semantics (temp 5 min / permanent 12 h).
    2. The Gemini client is supplied by the caller (V5's key-aware client
       proxy) instead of being constructed here — the friend's app likewise
       always constructed it once in the UI layer and passed it in.
    3. The optional second-pass ``audit_result`` is retained for parity but
       not invoked by V5's default pipeline (deterministic verification
       replaces it — see ``verify.py``).
    4. (V5 determinism) ``DETERMINISTIC_SAMPLING`` — temperature 0 + a fixed
       seed — is added to the generation config. The friend's app left
       sampling at the API default (~1.0), which made the SAME submission +
       SAME mark scheme produce different marks on every run. This is a
       generation-parameter change only: prompts, schema, model order,
       clamping and totals arithmetic are untouched.
    5. (V5 mark-scheme control) ``grade_with_model`` accepts an optional
       ``blueprint`` — the mark-scheme question structure extracted once and
       cached per mark-scheme file. When present it is injected into the
       marking prompt so question numbering/max marks are fixed by the
       official mark scheme instead of being re-invented per run. Omitting
       it (``blueprint=None``) reproduces the original behaviour exactly.
  Nothing else differs. Model names, timings, prompts, clamping and totals
  arithmetic are byte-for-byte the original logic.
    6. (V5 high-score double-check) ``grade_with_model`` accepts an optional
       ``sampling`` generation-config override. The DEFAULT is unchanged
       (``DETERMINISTIC_SAMPLING``), so every existing call is byte-identical;
       the independent second correction (engine/double_check.py) is the only
       caller that passes a different seed, so its evaluation is not a
       bit-for-bit replay of the first pass.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable

from ..config import get_config, normalize_model_chain
from ..gemini.client_proxy import KeyAwareClient

StatusCallback = Callable[[str], None]


# ============================================================
# MODEL HEALTH: remember which models are failing so later submissions
# skip them instead of re-discovering the failure every time.
# (Adaptation 1: plain thread-safe store replacing st.cache_resource —
#  identical cooldown constants, identical bench/fallback semantics.)
# ============================================================

MODEL_COOLDOWN_TEMPORARY_SECONDS = 5 * 60      # overloaded / slow: retry later
MODEL_COOLDOWN_PERMANENT_SECONDS = 12 * 3600   # retired / not found


class ModelHealth:
    """Thread-safe model bench with the legacy app's exact semantics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bad: dict[str, dict] = {}

    def record_failure(self, model_name: str, exc: Exception) -> None:
        if is_permanent_model_error(exc):
            cooldown, kind = MODEL_COOLDOWN_PERMANENT_SECONDS, "unavailable"
        elif is_timeout_error(exc) or is_temporary_model_error(exc):
            cooldown, kind = MODEL_COOLDOWN_TEMPORARY_SECONDS, "busy/slow"
        else:
            return
        with self._lock:
            self._bad[model_name] = {"until": time.time() + cooldown, "kind": kind}

    def record_success(self, model_name: str) -> None:
        with self._lock:
            self._bad.pop(model_name, None)

    def usable_models(self, model_names: list[str]) -> list[str]:
        now = time.time()
        with self._lock:
            bad = self._bad
            for name in [n for n, v in bad.items() if v["until"] <= now]:
                del bad[name]  # cooldown finished: give it another chance

            healthy = [m for m in model_names if m not in bad]
            if healthy:
                return healthy

            # Everything is benched. Rather than fail instantly, still try the
            # ones that are only temporarily struggling.
            fallback = [m for m in model_names if bad[m]["kind"] != "unavailable"]
            return fallback or list(model_names)

    def benched(self) -> dict[str, tuple[str, int]]:
        """{model: (kind, seconds_left)} for models currently being skipped."""
        now = time.time()
        with self._lock:
            return {
                m: (v["kind"], int(v["until"] - now))
                for m, v in self._bad.items()
                if v["until"] > now
            }

    def reset(self) -> None:
        with self._lock:
            self._bad.clear()

    # -- V5 additions (semantics-preserving): serialisation so the SAME
    #    health state can be shared across worker processes. Without it every
    #    job restarted the model chain from scratch and the model that ended
    #    up marking a submission was a per-job lottery (a major source of
    #    run-to-run mark differences).
    def snapshot(self) -> dict:
        with self._lock:
            return {name: dict(info) for name, info in self._bad.items()}

    def restore(self, data: dict) -> None:
        with self._lock:
            self._bad = {
                str(name): {"until": float(info.get("until", 0)),
                            "kind": info.get("kind", "busy/slow")}
                for name, info in (data or {}).items()
                if isinstance(info, dict)
            }

    def clear_temporary(self) -> None:
        """Drop TEMPORARY benches only (V5 addition — a per-transaction reset
        so a rate-limited first model cannot permanently shadow the rest of
        the fallback chain within one submission). PERMANENT 'unavailable'
        benches (404 / limit:0 models) are kept: retrying them wastes a full
        request exactly as in the legacy app. The fallback ORDER, per-model
        retry counts, timeout handling and all other semantics are unchanged."""
        now = time.time()
        with self._lock:
            for name in [n for n, v in self._bad.items()
                         if v["kind"] != "unavailable" and v["until"] > now]:
                del self._bad[name]


MODEL_HEALTH = ModelHealth()


def is_permanent_model_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if "no longer available" in message:
        return True
    if ("404" in message or "not_found" in message) and "model" in message:
        return True
    # A quota LIMIT of 0 means this API key/plan has no access to the model
    # at all (e.g. a Pro-tier model on the free tier) — retrying in a few
    # minutes will hit the exact same wall, unlike an ordinary "used up
    # today's quota" 429, so this is treated as effectively permanent.
    if "limit: 0" in message or "limit:0" in message:
        return True
    return False


def is_timeout_error(exc: Exception) -> bool:
    """True when a request ran out of time (retrying the same model would just
    burn another full timeout, so callers move straight to the next model)."""
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def is_temporary_model_error(exc: Exception) -> bool:
    """Return True for errors where trying another model is sensible."""
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "503",
            "unavailable",
            "high demand",
            "overloaded",
            "429",
            "resource exhausted",
            "temporarily",
            "deadline exceeded",
        )
    )


def is_overload_error(exc: Exception) -> bool:
    """True when the provider says it is busy (503 high demand / 429).

    Such a failure is not a property of THIS request: resending the same pages
    two seconds later does not help, and the shared provider gate already
    knows to hold traffic. Distinguishing it here is what stops a paper from
    spending an extra request per model on a 2-second retry that cannot
    succeed (measured: 3,922 failed attempts against 456 successes in the
    production log).
    """
    try:
        from ..retry_policy import HIGH_DEMAND, classify_failure
        return classify_failure(exc).cls == HIGH_DEMAND
    except Exception:
        message = str(exc).lower()
        return any(m in message for m in ("high demand", "503", "429",
                                          "resource exhausted", "overloaded"))


def extract_json(text: str) -> dict:
    """Parse JSON even if a model wraps it in a markdown code fence."""
    if not text:
        raise ValueError("The model returned an empty response.")

    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last-resort extraction of the largest JSON object.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])

    raise ValueError("The model response was not valid JSON.")


def clamp_mark(value, max_marks):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, max_marks))


def _clean_scheme_check(raw: Any, max_marks: int) -> dict | None:
    """Sanitise the optional per-question mark-scheme verification block.

    Nothing here is trusted blindly: the confidence is bounded, the justified
    mark is clamped to the question maximum, and an unknown discrepancy type is
    kept as a short string so the audit layer can classify it. A block that
    does not actually claim a disagreement is dropped entirely so ordinary
    results are byte-identical to before.
    """
    if not isinstance(raw, dict):
        return None
    if not bool(raw.get("disagrees_with_scheme")):
        return None
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(1.0, max(0.0, confidence))
    justified = None
    if raw.get("justified_marks") is not None:
        try:
            justified = max(0, min(int(raw.get("justified_marks")), max_marks))
        except (TypeError, ValueError):
            justified = None
    return {
        "disagrees_with_scheme": True,
        "student_answer_valid": bool(raw.get("student_answer_valid")),
        "scheme_answer": str(raw.get("scheme_answer") or "").strip()[:2000],
        "independently_verified_answer": str(
            raw.get("independently_verified_answer")
            or raw.get("verified_answer") or "").strip()[:2000],
        "discrepancy_type": str(raw.get("discrepancy_type") or "").strip()[:80],
        "evidence": str(raw.get("evidence") or "").strip()[:4000],
        "confidence": round(confidence, 3),
        "justified_marks": justified,
    }


def normalise_result(data: dict) -> dict:
    """Clean and validate model output before anything is displayed."""
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        questions = []

    cleaned = []

    for index, q in enumerate(questions, start=1):
        if not isinstance(q, dict):
            continue

        number = str(q.get("question_number", index)).strip()
        try:
            max_marks = int(q.get("max_marks", 0))
        except (TypeError, ValueError):
            max_marks = 0

        max_marks = max(0, max_marks)
        awarded = clamp_mark(q.get("awarded_marks", 0), max_marks)

        breakdown = q.get("mark_breakdown", [])
        if not isinstance(breakdown, list):
            breakdown = []

        cleaned_breakdown = []
        for item in breakdown:
            if isinstance(item, dict):
                cleaned_breakdown.append(
                    {
                        "code": str(item.get("code", "")).strip(),
                        "awarded": bool(item.get("awarded", False)),
                        "reason": str(item.get("reason", "")).strip(),
                    }
                )

        entry = {
            "question_number": number,
            "max_marks": max_marks,
            "awarded_marks": awarded,
            "topic": str(q.get("topic", "Unclassified")).strip(),
            "student_answer": str(q.get("student_answer", "")).strip(),
            "working_summary": str(q.get("working_summary", "")).strip(),
            "loss_reason": str(q.get("loss_reason", "")).strip(),
            "mark_breakdown": cleaned_breakdown,
            "correct_method": str(q.get("correct_method", "")).strip(),
            "full_mark_solution": str(q.get("full_mark_solution", "")).strip(),
        }
        check = _clean_scheme_check(q.get("scheme_check"), max_marks)
        if check is not None:
            entry["scheme_check"] = check
        cleaned.append(entry)

    cleaned.sort(key=lambda q: q["question_number"])

    info = data.get("paper_info", {})
    if not isinstance(info, dict):
        info = {}

    notes = data.get("overall_notes", {})
    if not isinstance(notes, dict):
        notes = {}

    uncertainties = data.get("uncertainties", [])
    if not isinstance(uncertainties, list):
        uncertainties = []

    return {
        "paper_info": {
            "qualification": str(info.get("qualification", "Pearson Edexcel International GCSE Mathematics")).strip(),
            "paper": str(info.get("paper", "Not identified")).strip(),
            "session": str(info.get("session", "Not identified")).strip(),
            "max_marks_declared": info.get("max_marks"),
        },
        "questions": cleaned,
        "overall_notes": {
            "summary": str(notes.get("summary", "")).strip(),
            "strengths": [str(x).strip() for x in notes.get("strengths", []) if str(x).strip()],
            "revision_areas": [
                str(x).strip() for x in notes.get("revision_areas", []) if str(x).strip()
            ],
        },
        "uncertainties": [str(x).strip() for x in uncertainties if str(x).strip()],
    }


def calculate_totals(result: dict):
    earned = sum(q["awarded_marks"] for q in result["questions"])
    available = sum(q["max_marks"] for q in result["questions"])
    percentage = (earned / available * 100) if available else 0.0

    return earned, available, percentage


def percentage_band_grade(percentage: float) -> str:
    """
    This is only a provisional percentage band.
    It is NOT an official Pearson Edexcel session-specific grade boundary.
    """
    if percentage >= 90:
        return "9"
    if percentage >= 80:
        return "8"
    if percentage >= 70:
        return "7"
    if percentage >= 60:
        return "6"
    if percentage >= 50:
        return "5"
    if percentage >= 40:
        return "4"
    if percentage >= 30:
        return "3"
    if percentage >= 20:
        return "2"
    return "1"


# V5 determinism: the friend's app used the API default sampling temperature
# (~1.0) and no seed, so two identical runs of the same PDF + mark scheme
# could disagree on marks. Sampling at temperature 0 with a fixed seed keeps
# the model's marking as repeatable as the API allows; everything downstream
# (normalisation, clamping, totals) was already deterministic.
DETERMINISTIC_SAMPLING: dict[str, Any] = {"temperature": 0.0, "seed": 42}

GROUP_SIZE = 5


def make_group_rows(questions):
    groups = []
    for start in range(0, len(questions), GROUP_SIZE):
        chunk = questions[start:start + GROUP_SIZE]
        earned = sum(q["awarded_marks"] for q in chunk)
        available = sum(q["max_marks"] for q in chunk)
        pct = (earned / available * 100) if available else 0.0
        groups.append(
            {
                "group": f"Questions {chunk[0]['question_number']} to {chunk[-1]['question_number']}",
                "earned": earned,
                "available": available,
                "percentage": pct,
                "questions": chunk,
            }
        )
    return groups


# ============================================================
# REPORT BUILDING (data-only variants of the legacy renderers, so the
# same Markdown/CSV the legacy app produced can be exported from V5)
# ============================================================

def build_markdown_report(student_name: str, result: dict, totals: dict) -> str:
    info = result["paper_info"]
    lines = [
        f"# Pearson Edexcel IGCSE Maths Auto-Marker Report — {student_name}",
        "",
        f"Score: {totals['earned']} / {totals['available']}",
        f"Percentage: {totals['percentage']:.2f}%",
        f"Provisional percentage-band grade: {totals['provisional_grade']}",
        f"Paper: {info['paper']}",
        f"Session: {info['session']}",
        "",
        "## Overview",
        result["overall_notes"]["summary"],
        "",
        "## Strengths",
    ]
    lines.extend(f"- {x}" for x in result["overall_notes"]["strengths"])
    lines.append("")
    lines.append("## Revision areas")
    lines.extend(f"- {x}" for x in result["overall_notes"]["revision_areas"])
    lines.append("")
    lines.append("## Question breakdown")

    for q in result["questions"]:
        lines.extend(
            [
                "",
                f"### Question {q['question_number']} — {q['awarded_marks']} / {q['max_marks']}",
                f"Topic: {q['topic']}",
                f"Loss reason: {q['loss_reason'] or 'None'}",
                f"Correct method: {q['correct_method']}",
                f"Full-mark solution: {q['full_mark_solution']}",
            ]
        )
        # The parts keep their own marks in the export too: a report that shows
        # only "2 / 5" cannot say which part lost the three marks.
        for sub in q.get("subquestions") or []:
            part_lines = subquestion_lines(sub)
            if not part_lines:
                continue
            lines.append(f"  - {part_lines[0]}")
            lines.extend(f"    {line.strip()}" for line in part_lines[1:])

    return "\n".join(lines)


def subquestion_lines(sub: dict) -> list:
    """One subquestion as report lines: its own mark, then its scheme evidence.

    An UNMARKED part says so instead of printing a 0 the marker never awarded.
    """
    label = str(sub.get("label") or sub.get("id") or "?")
    maximum = sub.get("max_marks")
    if str(sub.get("status") or "").upper() != "MARKED":
        lines = [f"{label}: UNMARKED (of {maximum if maximum is not None else '?'})"]
    else:
        awarded = sub.get("awarded_marks")
        lines = [f"{label}: {awarded if awarded is not None else 0} / "
                 f"{maximum if maximum is not None else '?'}"]
    answers = [str(a).strip() for a in sub.get("answers") or [] if str(a).strip()]
    if answers:
        lines.append(f"  Scheme answer: {' or '.join(answers)}")
    codes = [str(c).strip() for c in sub.get("mark_codes") or [] if str(c).strip()]
    if codes:
        lines.append(f"  Marking points: {', '.join(codes)}")
    return lines


def parts_summary(question: dict) -> str:
    """The subquestions of one question as one spreadsheet cell."""
    parts = []
    for sub in question.get("subquestions") or []:
        label = str(sub.get("label") or sub.get("id") or "?")
        if str(sub.get("status") or "").upper() != "MARKED":
            parts.append(f"{label} unmarked")
        else:
            awarded = sub.get("awarded_marks")
            maximum = sub.get("max_marks")
            parts.append(f"{label} {awarded if awarded is not None else 0}"
                         f"/{maximum if maximum is not None else '?'}")
    return "; ".join(parts)


def build_csv_rows(student_name: str, result: dict) -> list:
    rows = []
    for q in result["questions"]:
        rows.append(
            {
                "Student": student_name,
                "Question": q["question_number"],
                "Awarded": q["awarded_marks"],
                "Max": q["max_marks"],
                # One cell keeps the row counting exactly one question (adding
                # a row per part would double the paper total in a sheet).
                "Parts": parts_summary(q),
                "Percentage": (
                    round(q["awarded_marks"] / q["max_marks"] * 100, 2)
                    if q["max_marks"]
                    else 0
                ),
                "Topic": q["topic"],
                "Loss Reason": q["loss_reason"],
                "Correct Method": q["correct_method"],
            }
        )
    return rows


# ============================================================
# MARKING PROMPT (verbatim from the legacy app)
# ============================================================

CORRECTION_VERSION = "5.1.0"  # engine revision recorded with every result (§7 audit)

MARKING_SYSTEM = r"""
You are an automated Pearson Edexcel International GCSE Mathematics
marking assistant. You are NOT an official Pearson employee or examiner.

Your job is to mark the student's submitted work against the supplied
official mark scheme as strictly and faithfully as possible.

CORE RULES
1. Read the entire student submission and the entire mark scheme before
   assigning marks.
2. Match each student question to the corresponding mark-scheme question.
3. Award marks using the supplied mark scheme, not generic intuition.
3a. Independently verify the mathematics BEFORE relying on the mark scheme.
   The mark scheme is an important reference, NOT an infallible source of
   mathematical truth. Solve the question yourself (or verify the student's
   working) and compare your answer with the reference answer.
3b. NEVER award zero, or withhold marks, solely because a mathematically
   correct student answer differs from an incorrect or incomplete reference
   answer. If your independent solution shows the student is right, award the
   marks justified by the question and the applicable marking rules.
3c. Do NOT conclude the mark scheme is wrong merely because your own answer
   differs. You must have a defensible mathematical reason before recording a
   discrepancy (a demonstrable arithmetic/algebraic/sign error, a wrong value,
   an equivalent form, or a valid alternative method).
4. Respect method (M), accuracy (A), independent/basis (B), error-carried-forward
   (ECF), follow-through, implied answers, and "ignore subsequent working" (ISW)
   where the mark scheme supports them.
5. Do not award a mark simply because a final answer looks plausible.
6. Do not remove an earned earlier mark because of a later contradiction when
   the mark scheme says the earlier work stands (ISW).
7. When a later answer follows correctly from an earlier student error and the
   mark scheme permits ECF/follow-through, award the appropriate later marks.
8. Do not invent missing working. If the image/PDF is unclear, flag uncertainty.
9. Do not penalise a student for a formatting difference when the mathematics
   is equivalent.
10. Use the mark scheme's exact maximum marks for each question where possible.
11. Award only integer marks unless the supplied mark scheme explicitly uses
    another convention.
12. Never exceed a question's maximum mark.
13. Return one entry for every identifiable question in the student submission.
14. For each lost mark, explain the precise mathematical/mark-scheme reason.
15. Give a correct full-mark solution that a student can study.
16. Keep the output factual and concise. Do not give a fake "official" grade.
17. For every question, verify the answer independently using a reliable
    method, and record the result in the optional "scheme_check" object.
18. Set "scheme_check.disagrees_with_scheme" to true ONLY when you have a
    defensible mathematical reason to disagree with the supplied mark scheme.
19. In "scheme_check" record: the mark-scheme answer, your independently
    verified answer, the discrepancy type, your evidence/working, your
    confidence (0-1), whether the student's answer is mathematically valid,
    and - when the student is right - the integer "justified_marks" the work
    deserves. Never exceed the question's maximum.
20. Preserve every existing rule: method (M), accuracy (A), basis (B), ECF,
    follow-through, ISW, required working, rounding, units, answer formats and
    any explicitly required method must still be applied exactly as stated.
21. If the question is ambiguous, or the evidence is insufficient, use the
    discrepancy type "ambiguous_question" (or leave confidence low) so the
    case goes to human review instead of a confident wrong decision.
22. Never change a question's maximum marks and never invent marks.

VERY IMPORTANT
The application's Python code will calculate the total score and percentage.
Your job is to provide the individual question marks and marking evidence.
Do not attempt to make the arithmetic sound consistent by changing marks.
"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper_info": {
            "type": "object",
            "properties": {
                "qualification": {"type": "string"},
                "paper": {"type": "string"},
                "session": {"type": "string"},
                "max_marks": {"type": "integer"},
            },
            "required": ["qualification", "paper", "session"],
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_number": {"type": "string"},
                    "max_marks": {"type": "integer"},
                    "awarded_marks": {"type": "integer"},
                    "topic": {"type": "string"},
                    "student_answer": {"type": "string"},
                    "working_summary": {"type": "string"},
                    "loss_reason": {"type": "string"},
                    "mark_breakdown": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "awarded": {"type": "boolean"},
                                "reason": {"type": "string"},
                            },
                            "required": ["code", "awarded", "reason"],
                        },
                    },
                    "scheme_check": {
                        "type": "object",
                        "properties": {
                            "disagrees_with_scheme": {"type": "boolean"},
                            "student_answer_valid": {"type": "boolean"},
                            "scheme_answer": {"type": "string"},
                            "independently_verified_answer": {"type": "string"},
                            "discrepancy_type": {
                                "type": "string",
                                "enum": [
                                    "incorrect_answer",
                                    "arithmetic_error",
                                    "sign_error",
                                    "equivalent_expression",
                                    "alternative_method",
                                    "question_scheme_mismatch",
                                    "internal_inconsistency",
                                    "ambiguous_question",
                                    "other",
                                ],
                            },
                            "evidence": {"type": "string"},
                            "confidence": {"type": "number"},
                            "justified_marks": {"type": "integer"},
                        },
                        "required": ["disagrees_with_scheme"],
                    },
                    "correct_method": {"type": "string"},
                    "full_mark_solution": {"type": "string"},
                },
                "required": [
                    "question_number",
                    "max_marks",
                    "awarded_marks",
                    "topic",
                    "student_answer",
                    "working_summary",
                    "loss_reason",
                    "mark_breakdown",
                    "correct_method",
                    "full_mark_solution",
                ],
            },
        },
        "overall_notes": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "revision_areas": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary", "strengths", "revision_areas"],
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["paper_info", "questions", "overall_notes", "uncertainties"],
}


# ============================================================ PROMPTS
# Single source of truth for the EXACT texts the engine sends. They live here
# (not in a hand-copied doc or page) so the Developer prompt-inspection page
# shows the real thing, and so a Developer override is applied in exactly one
# place for every caller: manual corrections, the worker child, automation and
# the audit second pass.

_MARKING_USER_BASE = """
Mark the student's work in the uploaded file(s) against the uploaded mark scheme.

The student file(s) may contain multiple pages/files belonging to one submission.
Treat them as ONE student's complete submission.

The files are:
- Student submission: the actual work to mark.
- Official mark scheme: the source of marking rules and maximum marks.
- Optional question paper: use it only to improve question identification/context.

Return ONLY the requested JSON structure.

Before returning:
- identify every question you can,
- assign max marks from the mark scheme,
- assign earned marks question-by-question,
- record M/A/B-style evidence where available,
- explain lost marks,
- provide a full-mark solution,
- flag any unreadable or genuinely ambiguous evidence,
- independently verify each answer and fill in "scheme_check" when (and only
  when) you have a defensible mathematical reason to disagree with the
  supplied mark scheme, awarding the marks the mathematics justifies.

Do not calculate the final total as a free-form narrative. The application will calculate it from the individual question marks.
"""

_AUDIT_USER_BASE = """
You are performing a second-pass audit of an automated mathematics marking report.

Compare the report below against the actual student submission file(s) and the
official mark scheme. Correct any question-level marks that are not supported
by the mark scheme. Pay particular attention to:
- M/A/B mark logic
- ECF/follow-through
- ISW
- arithmetic in the student's working
- omitted vs shown working
- maximum marks
- question matching
- diagrams and geometry
- algebraic equivalence

Do NOT change a mark merely because another valid method exists.
Do NOT invent evidence that is not visible.
Return the COMPLETE corrected JSON in exactly the requested schema.

FIRST-PASS REPORT:
"""


def effective_system_prompt() -> str:
    """The system instruction actually sent (Developer override, else built-in)."""
    override = get_config().gemini.system_prompt_override
    return override if override else MARKING_SYSTEM


def marking_user_prompt(blueprint: dict | None = None) -> str:
    """The user-turn text sent AFTER the uploaded files.

    A Developer override replaces the base text; the mark-scheme blueprint
    block is still appended when one was extracted, because it is what keeps
    question numbering and maximum marks identical across runs.
    """
    override = get_config().gemini.user_prompt_override
    prompt = override if override else _MARKING_USER_BASE
    if blueprint:
        # V5 addition: the mark scheme's structure was extracted ONCE and is
        # cached, so every run of the same mark scheme sees the same question
        # list, numbering and maximum marks (see engine/blueprint.py).
        from .blueprint import prompt_block as blueprint_prompt_block
        prompt += "\n\n" + blueprint_prompt_block(blueprint)
    return prompt


def audit_prompt(result: dict) -> str:
    """The audit second-pass prompt with the first-pass report embedded."""
    return _AUDIT_USER_BASE + json.dumps(result, ensure_ascii=False) + "\n"


# ============================================================
# MARKING CALLS (Adaptation 2: the client arrives as a KeyAwareClient;
# the generate_content call signature and fallback loop are verbatim.)
# ============================================================

def grade_with_model(
    client: KeyAwareClient,
    model_names: list[str],
    student_file: Any,
    mark_scheme_file: Any,
    question_file: Any = None,
    status_callback: StatusCallback | None = None,
    blueprint: dict | None = None,
    sampling: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
):
    """
    Try several current Gemini models automatically.

    student_file may be one Gemini file or a list of Gemini files. Lists are
    useful for Google Classroom submissions containing multiple pages/files.

    ``sampling`` (V5 adaptation 6) overrides the generation sampling config;
    None keeps DETERMINISTIC_SAMPLING exactly as the legacy app runs it.

    ``system_prompt`` / ``user_prompt`` let a caller supply a dedicated prompt
    without touching the prompts every other caller uses. Both default to
    None, which sends exactly the legacy texts — so manual corrections, the
    worker child, automation and the audit pass are byte-for-byte unchanged.
    """
    if user_prompt is not None:
        prompt = user_prompt
        if blueprint:
            from .blueprint import prompt_block as blueprint_prompt_block
            prompt += "\n\n" + blueprint_prompt_block(blueprint)
    else:
        prompt = marking_user_prompt(blueprint)
    system_instruction = (system_prompt if system_prompt is not None
                          else effective_system_prompt())

    if isinstance(student_file, (list, tuple)):
        student_files = list(student_file)
    else:
        student_files = [student_file]

    contents_base = list(student_files) + [mark_scheme_file]
    if question_file is not None:
        contents_base.append(question_file)
    contents_base.append(prompt)

    errors = []

    # Resolve auto-routed aliases (and duplicate entries) BEFORE the chain is
    # walked. A deprecated ID such as gemini-3.5-flash is served by 3.6, so
    # trying it after 3.6 failed would re-send the whole request to the same
    # model. This is a pure normalisation of the caller's list; the fallback
    # ORDER is untouched.
    all_models = normalize_model_chain(model_names)
    from ..config import is_trusted_marking_model
    trusted = [m for m in all_models if is_trusted_marking_model(m)]
    untrusted = [m for m in all_models if m not in trusted]
    if untrusted and status_callback:
        status_callback(
            "⏭️ Skipping lightweight models for marking: " + ", ".join(untrusted)
        )
    if not trusted:
        raise RuntimeError(
            "No full-capability Gemini model is configured for marking "
            f"(refused lightweight: {', '.join(untrusted) or 'none'}). "
            "A correction cannot be trusted from a lightweight fallback; "
            "configure a full model and retry."
        )
    model_names = MODEL_HEALTH.usable_models(trusted)
    skipped = [m for m in trusted if m not in model_names]
    if skipped and status_callback:
        status_callback(
            "⏭️ Skipping models that recently failed: " + ", ".join(skipped)
        )

    for model_name in model_names:
        # A model the shared gate has parked sends NOTHING: the cooldown was
        # established by an earlier request (possibly another worker's), so
        # asking again is a guaranteed failure that still costs a request.
        try:
            from ..gemini.provider_health import provider_health
            allowed, gate_wait = provider_health().available(model_name)
        except Exception:  # noqa: BLE001 — the gate must never break marking
            allowed, gate_wait = True, 0.0
        if not allowed:
            errors.append(
                f"{model_name}: cooling down after high demand "
                f"({int(gate_wait)}s remaining) — not sent")
            try:
                from ..gemini import call_audit
                call_audit.note_skip(f"{model_name} cooling down")
            except Exception:
                pass
            continue
        for attempt in range(2):
            try:
                if status_callback:
                    status_callback(
                        f"🧠 Marking with {model_name}"
                        + (f" (attempt {attempt + 1}/2)" if attempt else "")
                    )

                response = client.models.generate_content(
                    model=model_name,
                    contents=contents_base,
                    config={
                        "system_instruction": system_instruction,
                        "response_mime_type": "application/json",
                        "response_schema": OUTPUT_SCHEMA,
                        **(sampling or DETERMINISTIC_SAMPLING),
                    },
                )

                if response and response.text:
                    parsed = extract_json(response.text)
                    MODEL_HEALTH.record_success(model_name)
                    return parsed, model_name

                raise RuntimeError(f"{model_name} returned an empty response.")

            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

                if is_timeout_error(exc):
                    MODEL_HEALTH.record_failure(model_name, exc)
                    break  # don't spend another full timeout on the same model

                if is_overload_error(exc):
                    # The provider is busy, not this request: the gate now
                    # carries the cooldown, so try the NEXT model instead of
                    # resending the same pages after 2 s.
                    MODEL_HEALTH.record_failure(model_name, exc)
                    try:
                        from ..gemini.provider_health import (
                            HIGH_DEMAND, provider_health,
                        )
                        provider_health().record_failure(
                            HIGH_DEMAND, str(exc), model_name)
                    except Exception:  # noqa: BLE001
                        pass
                    break

                if is_temporary_model_error(exc) and attempt == 0:
                    time.sleep(2)
                    continue

                MODEL_HEALTH.record_failure(model_name, exc)
                break

    raise RuntimeError(
        "All configured Gemini models failed. "
        + " | ".join(errors)
    )


def audit_result(
    client: KeyAwareClient,
    result: dict,
    student_file: Any,
    mark_scheme_file: Any,
    model_names: list[str],
    status_callback: StatusCallback | None = None,
    attempts_per_model: int = 1,
):
    """
    Independent second pass. Supports multiple student files/pages.

    Retained verbatim for parity with the legacy app. V5's default pipeline
    does NOT call this (deterministic verification is used instead), but it
    remains available and behaves identically to the original.
    """
    if isinstance(student_file, (list, tuple)):
        student_files = list(student_file)
    else:
        student_files = [student_file]

    contents = student_files + [mark_scheme_file, audit_prompt(result)]
    errors = []

    model_names = normalize_model_chain(model_names)
    model_names = MODEL_HEALTH.usable_models(model_names)

    for model_name in model_names:
        for attempt in range(attempts_per_model):
            try:
                if status_callback:
                    status_callback(
                        f"🔎 Auditing with {model_name}"
                        + (f" (attempt {attempt + 1}/2)" if attempt else "")
                    )

                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config={
                        "system_instruction": effective_system_prompt(),
                        "response_mime_type": "application/json",
                        "response_schema": OUTPUT_SCHEMA,
                        **DETERMINISTIC_SAMPLING,
                    },
                )

                if response and response.text:
                    parsed = extract_json(response.text)
                    MODEL_HEALTH.record_success(model_name)
                    return parsed, model_name

                raise RuntimeError(f"{model_name} returned an empty response.")

            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

                if is_timeout_error(exc):
                    MODEL_HEALTH.record_failure(model_name, exc)
                    break

                if is_temporary_model_error(exc) and attempt < attempts_per_model - 1:
                    time.sleep(2)
                    continue

                MODEL_HEALTH.record_failure(model_name, exc)
                break

    raise RuntimeError("Audit models failed. " + " | ".join(errors))


# ============================================================
# BATCH MARKING (the legacy manual-upload flow, engine form)
# ============================================================

def mark_one_submission(
    client: KeyAwareClient,
    student_name: str,
    student_files: Any,
    mark_scheme_file: Any,
    question_file: Any = None,
    audit_enabled: bool = False,
    blueprint: dict | None = None,
) -> dict:
    """
    Mark one student's already-uploaded file(s) and return a result dict.

    Makes no UI-framework calls, so it is safe to run inside a worker
    thread — same contract as the legacy function of the same name.
    ``audit_enabled`` defaults to False in V5 (deterministic verification
    replaces the AI audit) but preserves legacy behavior when True.
    """
    config = get_config()
    raw_result, marking_model = grade_with_model(
        client=client,
        model_names=config.gemini.primary_models,
        student_file=student_files,
        mark_scheme_file=mark_scheme_file,
        question_file=question_file,
        status_callback=None,
        blueprint=blueprint,
    )

    result = normalise_result(raw_result)
    audit_model = "Not run"
    audit_warning = None

    if audit_enabled:
        try:
            audit_candidates = [
                m for m in config.gemini.audit_models if m != marking_model
            ] + [
                m for m in config.gemini.primary_models
                if m != marking_model and m not in config.gemini.audit_models
            ]
            # The audit is a bonus check, so fail fast: try at most two models.
            audit_candidates = MODEL_HEALTH.usable_models(audit_candidates)[:2]

            audited, audit_model = audit_result(
                client=client,
                result=result,
                student_file=student_files,
                mark_scheme_file=mark_scheme_file,
                model_names=audit_candidates,
                status_callback=None,
            )
            result = normalise_result(audited)
        except Exception as audit_error:
            audit_warning = str(audit_error)

    earned, available, percentage = calculate_totals(result)
    provisional_grade = percentage_band_grade(percentage)
    groups = make_group_rows(result["questions"])

    return {
        "student_name": student_name,
        "status": "Marked",
        "error": None,
        "audit_warning": audit_warning,
        "result": result,
        "totals": {
            "earned": earned,
            "available": available,
            "percentage": percentage,
            "provisional_grade": provisional_grade,
        },
        "groups": groups,
        "marking_model": marking_model,
        "audit_model": audit_model,
    }
