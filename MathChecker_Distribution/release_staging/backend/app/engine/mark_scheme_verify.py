"""Stage B: AI verification over reconstructed evidence.

Stage A (:mod:`app.engine.mark_scheme`) reconstructs what can be known about a
mark scheme locally and for free. This module is the reasoning layer on top of
it: it decides what to ask the model, canonicalises whatever comes back,
cross-checks it against independent sources, and — critically — refuses to let a
worse result replace a structure that was already verified.

The governing rule from the brief:

    Do not make Gemini better at guessing a broken PDF.
    Make Math Correct better at reconstructing the document,
    then use Gemini to verify and resolve ambiguity.

So the model's answer is never authoritative. Every number it reports is
recomputed, re-ordered, and compared against at least one other source before
it is allowed anywhere near the marking engine.
"""

from __future__ import annotations

import logging
from typing import Any

from . import mark_scheme as ms

log = logging.getLogger("engine.mark_scheme_verify")

# --------------------------------------------------------------- the request

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper": {"type": "string"},
        "printed_total": {"type": "integer"},
        "total_marks": {"type": "integer"},
        "confidence": {"type": "number"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_number": {"type": "string"},
                    "max_marks": {"type": "integer"},
                    "criteria": {"type": "string"},
                    "answers": {"type": "array", "items": {"type": "string"}},
                    "page": {"type": "integer"},
                    "parts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "number": {"type": "string"},
                                "max_marks": {"type": "integer"},
                                # Per-subquestion evidence. Without these the
                                # sub-part's own answer and marking points never
                                # reach the marker or the checker (§2/§4/§5).
                                "criteria": {"type": "string"},
                                "answers": {"type": "array",
                                            "items": {"type": "string"}},
                                "page": {"type": "integer"},
                            },
                            "required": ["number", "max_marks"],
                        },
                    },
                },
                "required": ["question_number", "max_marks"],
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["questions"],
}

VERIFY_SYSTEM = """
You are verifying the STRUCTURE of an official mathematics mark scheme.

You are NOT marking student work and must never look for it.

Decide what each number in the evidence ACTUALLY is, using its context:
  "22" in a header/footer band, or beside "Page"/"of" -> PAGE NUMBER, never a
      question.
  "5" beside the words "marks" or "max" -> a MARK VALUE, not a question.
  "22" opening a row that also carries an answer and an allocation -> Question 22.
  "1(a)" -> a SUB-PART of question 1, not a new top-level question.

Rules:
- List every top-level question exactly once with the marks the scheme
  allocates to it. If the scheme splits a question into labelled parts, put
  them in "parts"; the parent max_marks will be recomputed as their sum.
- EVERY labelled sub-part must be its own entry in "parts" (a, b, c, and the
  nested i, ii, iii), spelled as the scheme prints it, with its own max_marks
  and its own criteria. Never merge (b)(i) and (b)(ii), and never move an
  answer or a mark from one part to another.
- Give each part's own accepted answers in "answers" (one string per accepted
  alternative) and its own marking points in "criteria" (the M/A/B codes and
  the working they reward). Put the parent's answers in the parent's
  "answers" only when the scheme states them for the question as a whole.
- Set "page" to the page number the row was read from when it is visible.
- Never invent a question that only appears as a page number or mark value.
- The evidence preserves page and block order. For a two-column page the blocks
  are already in reading order (left column first) - do not re-interleave them.
- A question starting on one page and continuing on the next is ONE question.
- Set "printed_total" to the total the mark scheme itself states, and
  "total_marks" to the sum of your question maxima.
- Set "confidence" (0-1) and list anything you were unsure about in "warnings".

Return ONLY the requested JSON structure.
"""


# --------------------------------------------------------------- failure taxonomy

FAILURE_MALFORMED = "malformed_ai_response"
FAILURE_TIMEOUT = "timeout"
FAILURE_RATE_LIMIT = "rate_limited"
FAILURE_OVERLOAD = "overload"
FAILURE_AUTH = "authentication"
FAILURE_INVALID_REQUEST = "invalid_request"
FAILURE_EXTRACTION = "extraction_failure"
FAILURE_STRUCTURAL = "structural_inconsistency"

#: Failures that mean "ask again later". None of them is evidence that the
#: mark scheme is broken, so none may invalidate a verified structure.
TRANSIENT_FAILURES = frozenset({
    FAILURE_TIMEOUT, FAILURE_RATE_LIMIT, FAILURE_OVERLOAD, FAILURE_AUTH,
})


def classify_failure(exc: BaseException) -> str:
    """Map an exception onto the failure taxonomy.

    Conservative by design: an unrecognised error keeps its own bucket rather
    than being reported as a structural problem, so a transient provider
    glitch is never shown to the teacher as "your mark scheme is broken".
    """
    text = str(exc or "").lower()
    if any(k in text for k in ("timeout", "timed out", "deadline")):
        return FAILURE_TIMEOUT
    if any(k in text for k in ("429", "rate limit", "quota", "resource_exhausted")):
        return FAILURE_RATE_LIMIT
    if any(k in text for k in ("503", "overloaded", "unavailable")):
        return FAILURE_OVERLOAD
    if any(k in text for k in ("401", "403", "unauthenticated", "permission")):
        return FAILURE_AUTH
    if any(k in text for k in ("400", "invalid argument", "invalid_request")):
        return FAILURE_INVALID_REQUEST
    if any(k in text for k in ("json", "decode", "parse", "schema", "expected")):
        return FAILURE_MALFORMED
    return FAILURE_EXTRACTION


# ------------------------------------------------------------------ canonicalisation

def canonicalize(parsed: Any) -> dict | None:
    """Turn a raw model response into a canonical, ordered, checked structure.

    Recomputed rather than trusted:

    * duplicate question numbers are dropped (first wins) and recorded;
    * questions are sorted NUMERICALLY — Q2 before Q10, never string order;
    * sub-parts are ordered too, and the parent total becomes the sum of its
      own parts rather than a number the model guessed;
    * ``calculated_total`` is summed from the structure itself and kept
      separate from whatever total the model claimed.

    ``None`` means "nothing usable came back" — the caller must then keep any
    structure it already had rather than replacing it with nothing.
    """
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("questions")
    if not isinstance(raw, list) or not raw:
        return None

    by_number: dict[str, dict] = {}
    duplicates: list[str] = []
    rejected: list[str] = []
    parts_disagreements: list[str] = []

    for item in raw:
        if not isinstance(item, dict):
            continue
        number = str(item.get("question_number", "")).strip()
        try:
            max_marks = int(item.get("max_marks"))
        except (TypeError, ValueError):
            rejected.append(number or "<unnamed>")
            continue
        if not number or max_marks <= 0:
            rejected.append(number or "<unnamed>")
            continue

        parts: list[dict] = []
        for p in (item.get("parts") or []):
            if not isinstance(p, dict):
                continue
            try:
                pnum = str(p.get("number", "")).strip()
                pm = int(p.get("max_marks"))
            except (TypeError, ValueError):
                continue
            if pnum and pm > 0:
                part: dict[str, Any] = {"number": pnum, "max_marks": pm}
                # The sub-part's OWN evidence travels with it. Dropping it here
                # is what forced every part's answer and M/A/B rules into the
                # parent's one free-text field.
                part_criteria = str(p.get("criteria", "") or "").strip()
                if part_criteria:
                    part["criteria"] = part_criteria
                part_answers = [str(a).strip()
                                for a in (p.get("answers") or [])
                                if str(a).strip()]
                if part_answers:
                    part["answers"] = part_answers
                part_page = _as_int(p.get("page"))
                if part_page:
                    part["page"] = part_page
                parts.append(part)
        parts.sort(key=lambda p: ms.order_key(p["number"]))
        parts_stated = max_marks
        if parts:
            max_marks = sum(p["max_marks"] for p in parts)

        if number in by_number:
            duplicates.append(number)
            continue
        entry: dict[str, Any] = {
            "question_number": number,
            "max_marks": max_marks,
            "criteria": str(item.get("criteria", "") or "").strip(),
        }
        answers = [str(a).strip() for a in (item.get("answers") or [])
                   if str(a).strip()]
        if answers:
            entry["answers"] = answers
        page = _as_int(item.get("page"))
        if page:
            entry["page"] = page
        if parts:
            entry["parts"] = parts
            if parts_stated and parts_stated != max_marks:
                # The statement and its own parts disagree. The parts win (they
                # are the finer evidence) but the disagreement is recorded: an
                # incomplete parts list silently shrinking a question is how a
                # denominator goes missing.
                entry["parts_disagreement"] = {
                    "stated": parts_stated, "summed_from_parts": max_marks}
                parts_disagreements.append(
                    f"{number}: stated {parts_stated}, parts sum to {max_marks}")
        by_number[number] = entry

    if not by_number:
        return None

    ordered = [by_number[n] for n in sorted(by_number, key=ms.order_key)]
    return {
        "paper": str(parsed.get("paper") or "").strip(),
        "questions": ordered,
        "calculated_total": sum(q["max_marks"] for q in ordered),
        "printed_total": _as_int(parsed.get("printed_total")),
        "model_stated_total": _as_int(parsed.get("total_marks")),
        "model_confidence": _as_confidence(parsed.get("confidence")),
        "warnings": [str(w) for w in (parsed.get("warnings") or [])
                     if isinstance(w, (str, int, float))],
        "duplicates_rejected": sorted(set(duplicates)),
        "invalid_rejected": sorted(set(rejected)),
        "parts_disagreements": parts_disagreements,
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_confidence(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if 0.0 <= out <= 1.0 else None


# ----------------------------------------------------------------- cross-validation

def cross_validate(structure: dict | None, evidence: dict | None = None) -> dict:
    """Check the structure against every independent source available.

    "The model said 33" is not evidence. Agreement between sources is. Compared
    here: the structure itself, the total the mark scheme prints, the total the
    model stated separately, and the question candidates the local document
    reconstruction produced.

    A missing/empty structure is a reportable state ("nothing usable came
    back"), never an error \u2014 this runs on the read-only diagnostics path, which
    must answer with a verdict rather than a 500.
    """
    evidence = evidence or {}
    structure = structure or {}
    questions = structure.get("questions") or []
    calculated = int(structure.get("calculated_total") or 0)
    printed = structure.get("printed_total")
    model_stated = structure.get("model_stated_total")

    numbers = [q["question_number"] for q in questions]
    seq = ms.sequence_report(numbers)
    local = ms.candidate_question_numbers(evidence) if evidence else []

    anomalies: list[str] = []
    checks: dict[str, Any] = {
        "calculated_total": calculated,
        "printed_total": printed,
        "model_stated_total": model_stated,
        "question_count": seq["count"],
        "missing_questions": seq["missing"],
        "duplicates_rejected": structure.get("duplicates_rejected") or [],
    }

    # --- subquestion structure (§2/§3/§8) ---------------------------------
    # Only meaningful when the scheme actually declares sub-parts: a flat
    # scheme of 33 whole questions has nothing to say here, and inventing
    # findings for it would bury the real ones.
    sub_report: dict[str, Any] = {"anomalies": [], "counts": {}}
    if any(q.get("parts") for q in questions):
        from . import scheme_structure as st
        built = st.build_structure(questions)
        sub_report = st.validate_structure(built["questions"])
        checks["subquestion_counts"] = sub_report["counts"]
        checks["subquestions"] = [
            {"id": s["id"], "label": s["label"], "parent_id": s["parent_id"],
             "max_marks": s["max_marks"], "answers": s["answers"],
             "mark_codes": (s.get("marks") or {}).get("codes") or [],
             "validation": s.get("validation")}
            for s in built["subquestions"]]
        for issue in sub_report["anomalies"]:
            if issue.get("severity") == st.SEVERITY_REVIEW:
                anomalies.append(issue["message"])
    for disagreement in (structure.get("parts_disagreements") or []):
        anomalies.append(
            f"The stated marks and the sub-part marks disagree for question "
            f"{disagreement} — a sub-part may be missing from the scheme.")

    if not questions:
        anomalies.append("No questions were reconstructed from the mark scheme.")

    # --- totals must agree, and must add up -------------------------------
    if printed is not None and printed != calculated:
        anomalies.append(
            f"Question marks add up to {calculated} but the mark scheme states "
            f"{printed} — {abs(printed - calculated)} mark(s) unaccounted for.")
    if model_stated is not None and model_stated != calculated:
        anomalies.append(
            f"The reported total ({model_stated}) disagrees with the marks "
            f"summed from the reconstructed questions ({calculated}).")

    # --- the question sequence --------------------------------------------
    if seq["missing"]:
        anomalies.append(
            "Missing expected question number(s): "
            + ", ".join(str(n) for n in seq["missing"][:12])
            + ". The extraction may have skipped a question, or the numbering "
              "is intentionally non-sequential.")
    if seq["duplicates"]:
        anomalies.append("Duplicate question number(s) rejected: "
                         + ", ".join(seq["duplicates"][:12]) + ".")
    if seq["largest_gap"] > 3:
        anomalies.append(
            f"A gap of {seq['largest_gap']} question numbers suggests content "
            "was lost rather than intentionally skipped.")

    # --- independent local evidence ---------------------------------------
    evidence_agrees: bool | None = None
    if local:
        local_set, found_set = set(local), set(seq["numbers"])
        only_ai = sorted(found_set - local_set, key=ms.order_key)
        only_local = sorted(local_set - found_set, key=ms.order_key)
        checks["local_candidate_count"] = len(local)
        checks["local_candidates"] = local
        if only_local and not only_ai:
            anomalies.append(
                "The local document reconstruction found question number(s) "
                + ", ".join(only_local[:12])
                + " that are missing from the reconstructed structure — "
                  "extraction may have dropped blocks.")
        elif only_ai and len(only_ai) > max(2, len(seq["numbers"]) * 0.25):
            anomalies.append(
                f"The reconstructed structure contains {len(only_ai)} "
                "question(s) the local text evidence does not support — these "
                "may be page numbers or mark values mistaken for questions.")
        evidence_agrees = not (only_ai or only_local)
    else:
        checks["local_candidate_count"] = 0

    # --- what kind of document is this? -----------------------------------
    mode = evidence.get("mode")
    if mode == ms.MODE_IMAGE_ONLY:
        anomalies.append(
            "This mark scheme has no text layer (image-only), so its structure "
            "was reconstructed visually and cannot be cross-checked against "
            "extracted text.")
    elif mode == ms.MODE_NONE and evidence:
        anomalies.append(
            "The mark scheme could not be read locally, so no independent "
            "structural evidence is available.")

    confidence = structure.get("model_confidence")
    if confidence is None:
        confidence = 1.0 if evidence_agrees else 0.6
    if anomalies:
        confidence = min(confidence, 0.5)

    return {
        "checks": checks,
        "anomalies": anomalies,
        "sequence": seq,
        "evidence_agrees": evidence_agrees,
        "extraction_mode": mode,
        "structural_confidence": round(float(confidence), 3),
        "verified": not anomalies,
    }


def likely_cause(verification: dict | None) -> str | None:
    """Turn a list of anomalies into one actionable sentence for the teacher.

    "AI verification failed" tells the teacher nothing. This names the most
    probable *cause* so they know whether to look at the PDF pages, fix the
    question list by hand, or simply re-verify. Returns ``None`` when there is
    nothing to explain.
    """
    verification = verification or {}
    anomalies = [str(a) for a in (verification.get("anomalies") or [])]
    if not anomalies:
        return None
    joined = " ".join(anomalies).lower()

    # Ordered: the first rule that matches names the dominant cause.
    rules = (
        (lambda: verification.get("extraction_mode") == ms.MODE_IMAGE_ONLY,
         "This PDF has no text layer \u2014 the pages are images. The structure was "
         "rebuilt visually, so it cannot be cross-checked against extracted text. "
         "Check the question list against the page images in the mark scheme."),
        (lambda: verification.get("extraction_mode") == ms.MODE_NONE,
         "The mark scheme could not be read locally, so there is no independent "
         "evidence to check the structure against. Re-verify with a model, or "
         "enter the structure by hand."),
        (lambda: "page numbers or mark values mistaken" in joined,
         "Numbers that are not questions (page numbers, mark values, answer "
         "values) were promoted to question numbers. Review the question list "
         "and remove any entry that is really page furniture or a mark value."),
        (lambda: "duplicate question number" in joined,
         "The same question number appears more than once \u2014 usually a page "
         "header or duplicated extraction being read as a question. De-duplicate "
         "in the question list."),
        (lambda: "may have dropped blocks" in joined
                or "missing expected question number" in joined,
         "PDF text extraction appears to have lost question blocks: the "
         "structure is missing question number(s) that the document really "
         "contains. Check the pages where those questions should be and add "
         "them to the question list."),
        (lambda: "unaccounted for" in joined or "disagrees with the marks" in joined,
         "The per-question marks do not add up to the paper total, so some marks "
         "were not attached to a question (or a question is missing its "
         "allocation). Check the mark allocation for each question."),
        (lambda: "gap of" in joined,
         "A large jump in the question numbering suggests content was lost "
         "between those questions rather than intentionally skipped. Check the "
         "missing pages."),
    )
    for matches, cause in rules:
        if matches():
            return cause
    return ("The reconstructed structure could not be confirmed against the "
            "independent evidence listed above. Inspect the question list and "
            "verify again.")


# ------------------------------------------------------------------ protection

def quality(structure: dict | None, validation: dict | None) -> float:
    """Comparable quality score deciding which structure survives.

    Rewards exactly what a marking engine depends on: question coverage,
    cross-source agreement, and a total that adds up. This is what stops a
    "22 questions / 74 marks" re-read from displacing an existing verified
    "33 questions / 100 marks".
    """
    if not structure or not structure.get("questions"):
        return 0.0
    validation = validation or {}
    score = min(1.0, len(structure["questions"]) / 20.0) * 40.0
    score += float(validation.get("structural_confidence") or 0.0) * 40.0
    if validation.get("evidence_agrees") is True:
        score += 10.0
    if not validation.get("anomalies"):
        score += 10.0
    return round(score, 2)


def prefer(existing: dict | None, candidate: dict | None,
           existing_validation: dict | None = None,
           candidate_validation: dict | None = None) -> tuple[str, dict]:
    """May this candidate replace the existing verified structure?

    Returns ``(action, detail)`` where action is ``adopt`` (nothing to lose),
    ``replace`` (the candidate is at least as good), or ``keep_existing``.
    """
    if existing and not existing.get("questions"):
        return "replace", {"reason": "the existing structure had no questions"}
    if not candidate or not candidate.get("questions"):
        return "keep_existing", {
            "reason": "the new verification produced no usable questions; "
                      "the existing verified structure has been preserved."}
    if not existing:
        return "adopt", {"reason": "first verified structure for this mark scheme"}

    new_score = quality(candidate, candidate_validation)
    old_score = quality(existing, existing_validation)
    if new_score >= old_score:
        return "replace", {"reason": f"new structure scores {new_score} "
                                     f"(previous {old_score})"}
    return "keep_existing", {
        "reason": f"the new result is worse ({new_score}) than the existing "
                  f"verified structure ({old_score}); the verified structure "
                  f"has been preserved.",
        "new_score": new_score, "existing_score": old_score}


def should_verify(existing_validation: dict | None, *,
                  teacher_requested: bool = False) -> tuple[bool, str]:
    """Is an AI verification call actually justified?

    Reusing a valid, unchanged structure must cost nothing. Verification is
    worth an API request only when something is wrong, uncertain, or the
    teacher explicitly asked.
    """
    if teacher_requested:
        return True, "the teacher asked to verify this mark scheme."
    if existing_validation is None:
        return True, "this mark scheme has not been verified yet."
    if existing_validation.get("anomalies"):
        return True, (f"{len(existing_validation['anomalies'])} structural "
                      "anomaly/anomalies were found previously.")
    if not existing_validation.get("verified", True):
        return True, "the previous verification did not fully pass."
    return False, "the existing structure verified cleanly and is unchanged."


# --------------------------------------------------------------------- evidence prompt

def format_evidence(evidence: dict | None, *, max_pages: int = 40,
                    max_chars: int = 24000) -> str:
    """Render reconstructed evidence with page boundaries preserved.

    Never flattens the document into one wall of text — the model sees
    ``Page N`` / ``[block id col=]`` / text so it can reason about layout. The
    budget is bounded so verification stays ONE small request.
    """
    if not evidence or evidence.get("mode") == ms.MODE_NONE:
        return "(no local text evidence available for this PDF)"
    pages = evidence.get("pages") or []
    if not any(p.get("blocks") for p in pages):
        return ("(this PDF has no text layer - it is image-only. Use the page "
                "images you can see and say so in your confidence.)")

    out: list[str] = []
    used = 0
    for page in pages[:max_pages]:
        header = (f"\n--- Page {page['page']} ({page['columns']} column(s), "
                  f"{page['block_count']} block(s))")
        if used + len(header) > max_chars:
            out.append("\n[truncated: remaining pages omitted for budget]")
            break
        out.append(header)
        used += len(header)
        for block in page["blocks"]:
            line = (f"  [{block['id']} col={block['column']}"
                    f"{' HEADER/FOOTER' if block.get('is_header_or_footer') else ''}] "
                    f"{block['text'][:400]}").rstrip()
            if used + len(line) > max_chars:
                out.append("  [truncated]")
                break
            out.append(line)
            used += len(line)
    diag = evidence.get("diagnostics") or {}
    if diag:
        out.append("\nLOCAL STRUCTURE DIAGNOSTICS: " + ", ".join(
            f"{k}={v}" for k, v in diag.items() if k != "candidate_numbers"))
    return "\n".join(out)


def evidence_summary(evidence: dict | None) -> dict:
    """The diagnostic block recorded in logs and shown to the teacher."""
    if not evidence:
        return {"extraction_mode": ms.MODE_NONE}
    diag = evidence.get("diagnostics") or {}
    return {
        "extraction_mode": evidence.get("mode"),
        "pages": diag.get("pages"),
        "pages_with_text": diag.get("pages_with_text"),
        "blocks": diag.get("blocks"),
        "two_column_pages": diag.get("two_column_pages"),
        "image_count": diag.get("image_count"),
        "question_candidates": diag.get("question_candidates"),
        "page_number_candidates": diag.get("page_number_candidates"),
        "printed_total": evidence.get("printed_total"),
        "error": evidence.get("error"),
    }