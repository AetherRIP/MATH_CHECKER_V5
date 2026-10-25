"""Mark-scheme BLUEPRINT: extract the official question structure once, reuse it.

Why this exists
---------------
Before this module, every correction run asked the model to "assign max marks
from the mark scheme" from scratch. Because the model also decided *how to
segment the paper* on each run, the same submission + same mark scheme could
come back as:

    run A: 31 questions, Q14 = 6 marks,      total 103 -> 74/103
    run B: 36 questions, Q14(a)+Q14(b),      total 106 -> 78/106
    run C: 31 questions, Q20 max 3 (wrong),  total 103 -> 72/103
    run D: 28 questions (3 silently dropped) total  87 -> 87/87 = 100%

The variation was NOT the student's work — it was our pipeline re-inventing
question segmentation, numbering and maximum marks on every call.

The blueprint fixes that: the official mark scheme is parsed ONCE per
mark-scheme file (keyed by the file's SHA-256, cached on disk), and every
marking run for that mark scheme then sees the same fixed question list,
numbering and maximum marks. The model's job shrinks to what an LLM is actually
needed for: judging the student's answers against those criteria.

Guarantees provided here
------------------------
* The mark scheme (blueprint) controls max marks — a model that inflates or
  shrinks a question's maximum is corrected back to the official value.
* A question present in the mark scheme but MISSING from the model's answer is
  added back with status=REVIEW and an explicit reason. It is NEVER silently
  dropped from the totals, and the UI can tell "unmarked because of a technical
  gap" apart from "the student scored 0".
* Missing internal metadata (no mark_breakdown, no evidence codes) never
  changes any mark — see verify.py, which only ever reports, never rewrites.
* Blueprint extraction is best-effort: if it fails, the pipeline falls back to
  the legacy free-form marking (blueprint=None) instead of blocking work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..config import get_config
from . import legacy_marking as legacy
from . import mark_scheme_verify as _msv

log = logging.getLogger("engine.blueprint")

# Statuses attached to every question after reconciliation.
STATUS_VALID = "VALID"      # marked normally, backed by the mark scheme
STATUS_REVIEW = "REVIEW"    # needs a human look (gap/extra/ambiguity)

#: States that must NEVER be laundered into a clean VALID row by a later
#: reconciliation pass (see ``apply_blueprint``): the missing-result placeholder
#: an earlier pass inserted, a split-question flag, an explicit unmarked row.
UNRESOLVED_STATUSES = (STATUS_REVIEW, "NEEDS_REVIEW", "UNMARKED")

#: The explanation attached to a question the marker never answered (the
#: technical-gap placeholder). It is written to BOTH ``review_reason`` and
#: ``loss_reason``: the placeholder stores ``awarded_marks: 0`` so that the gap
#: still counts in the denominator (a dropped question must never inflate the
#: percentage), which means any reader that only looks at the mark sees a 0.
#: Leaving ``loss_reason`` blank made that indistinguishable from "the student
#: scored 0 and there is nothing to say" in the report, the CSV and the audit
#: harness — so the placeholder now says what it is in the field those readers
#: actually display.
UNMARKED_REASON = (
    "The marker did not return an answer for this question. It is UNMARKED - "
    "do not treat this 0 as a confirmed score; review it manually.")

BLUEPRINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper": {"type": "string"},
        "total_marks": {"type": "integer"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_number": {"type": "string"},
                    "max_marks": {"type": "integer"},
                    "criteria": {"type": "string"},
                },
                "required": ["question_number", "max_marks", "criteria"],
            },
        },
    },
    "required": ["questions"],
}

BLUEPRINT_SYSTEM = """
You extract the structure of an official mathematics mark scheme.

You are given ONLY the mark scheme PDF (and optionally the question paper for
context). You are NOT given, and must NOT look for, any student work.

Produce a complete, ordered list of every question in the mark scheme:
- question_number: exactly as the mark scheme labels it (e.g. "1", "5",
  "14(a)" — if the mark scheme labels parts, keep the parts separate; if it
  labels one question with sub-parts under one mark total, use the single
  question number)
- max_marks: the TOTAL marks available for that question according to the
  mark scheme (the number that would appear in the mark-scheme mark allocation
  column). Never guess: use what the mark scheme states.
- criteria: a compact summary of how marks are earned (M/A/B codes, ECF/ISW
  notes, acceptable answers) — enough for a marker to award marks without
  re-reading the whole scheme.

Also set total_marks to the sum of all question max_marks, and paper to the
paper title/code if visible.

Return ONLY the requested JSON structure.
"""

PROMPT_HEADER = (
    "OFFICIAL MARK SCHEME BLUEPRINT (extracted from the uploaded mark "
    "scheme, cached and identical for every run):\n"
)


def prompt_block(blueprint: dict) -> str:
    """Render the blueprint as an instruction block for the marking prompt.

    Delegates to :mod:`app.engine.scheme_structure`, which prints every
    SUBQUESTION on its own line with its own id, accepted answers and M/A/B
    marking points. The old rendering squeezed a whole question into one line
    and cut the criteria at 400 characters *silently*; the structured rendering
    gives each subquestion its own budget and marks a truncation explicitly.
    """
    from . import scheme_structure as st
    questions = blueprint.get("questions") or []
    total = blueprint.get("total_marks")
    if total is None:
        total = sum(int(q.get("max_marks", 0) or 0) for q in questions)
    return st.prompt_block(questions, total_marks=total)


# ---------------------------------------------------------------- caching

def blueprint_dir() -> Path:
    # Resolved per call so tests (and a relocated data dir) stay correct.
    from ..config import DATA_DIR
    path = DATA_DIR / "blueprints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_path(sha: str) -> Path:
    return blueprint_dir() / f"{sha}.json"


def load_cached(sha: str) -> dict | None:
    path = _cache_path(sha)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    blueprint = payload.get("blueprint")
    if validate(blueprint):
        return blueprint
    return None


def save_cached(sha: str, blueprint: dict, model: str) -> None:
    save_record(sha, blueprint, model=model)


def load_record(sha: str) -> dict | None:
    """The WHOLE cache entry for a mark scheme (blueprint + its verdict).

    ``load_cached`` answers "what is the structure"; this answers "what do we
    know about it" — the page-scoped inventory, the completeness verdict and the
    verification state are persisted next to the question list so a later read
    cannot quietly lose them.
    """
    path = _cache_path(sha)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_record(sha: str, blueprint: dict, *, model: str = "",
                inventory: dict | None = None,
                completeness: dict | None = None,
                verification: dict | None = None,
                reconciliation: dict | None = None,
                anomalies: list[dict] | None = None,
                evidence_kind: str | None = None,
                confidence_basis: dict | None = None) -> None:
    """Persist a structure together with the verdict about it.

    Keys whose value is ``None`` keep whatever the existing record already had:
    a single-request extraction that runs afterwards must not erase the
    page-scoped inventory (and its completeness verdict) that an earlier
    verification established.
    """
    existing = load_record(sha) or {}
    payload = dict(existing)
    fingerprint = document_fingerprint(sha, blueprint)
    previous_fingerprint = (document_fingerprint(sha, existing.get("blueprint"))
                            if existing.get("blueprint") else None)
    structure_changed = bool(previous_fingerprint) and previous_fingerprint != fingerprint
    payload.update({"sha256": sha, "blueprint": blueprint})
    if model:
        payload["model"] = model
    payload.setdefault("created_at", time.time())
    payload["updated_at"] = time.time()
    for key, value in (("inventory", inventory),
                       ("completeness", completeness),
                       ("verification", verification),
                       ("reconciliation", reconciliation),
                       ("anomalies", anomalies),
                       ("evidence_kind", evidence_kind),
                       ("confidence_basis", confidence_basis)):
        if value is not None:
            payload[key] = value
    payload["fingerprint"] = fingerprint
    if verification is not None:
        # A verdict is only ever about the structure it was computed for.
        payload["verified_fingerprint"] = fingerprint
    elif structure_changed and verification_status(existing) in TERMINAL_STATUSES:
        # Editing the structure by hand (or replacing it) must not leave a
        # VERIFIED badge on something no verification ever looked at.
        payload["verification"] = {
            **(existing.get("verification") or {}),
            "state": STATUS_NOT_VERIFIED,
            "invalidated_by": "the stored structure changed after it was verified",
            "updated_at": time.time(),
        }
    try:
        _cache_path(sha).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # caching is best-effort; never fail the job
        log.warning("could not cache blueprint %s: %s", sha[:12], exc)


# ------------------------------------------- verification state + fingerprint

#: No verification has been attempted yet.
STATUS_NOT_VERIFIED = "NOT_VERIFIED"
#: A verification is running right now (persisted so a restart is visible).
STATUS_VERIFYING = "VERIFYING"
#: Verified: every independent source agreed and the inventory was complete.
STATUS_VERIFIED = "VERIFIED"
#: Something is wrong and a repair attempt is available.
STATUS_NEEDS_REPAIR = "NEEDS_REPAIR"
#: A repair (re-read / page-scoped pass) is running.
STATUS_REPAIRING = "REPAIRING"
#: A repaired structure is being re-checked.
STATUS_REVERIFYING = "REVERIFYING"
#: Nothing more can be done automatically. The teacher must decide.
STATUS_NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"

#: States that end a verification cycle. Re-running one of these for an
#: unchanged document is the pointless repetition this module exists to stop.
TERMINAL_STATUSES = frozenset({STATUS_VERIFIED, STATUS_NEEDS_MANUAL_REVIEW})


def document_fingerprint(sha: str, blueprint: dict | None) -> str:
    """A stable identity for "this document as this structure".

    Verification is worth an API request only when something changed. The
    fingerprint combines the mark-scheme file's content hash with the canonical
    question list, so re-uploading the same file, editing a question by hand, or
    replacing the scheme all produce different identities — while merely opening
    the screen again does not.
    """
    digest = hashlib.sha256()
    digest.update(str(sha or "").encode("utf-8"))
    rows = []
    for question in ((blueprint or {}).get("questions") or []):
        if not isinstance(question, dict):
            continue
        rows.append([str(question.get("question_number", "")).strip(),
                     int(question.get("max_marks") or 0),
                     [str(p.get("number", "")) + ":" + str(p.get("max_marks", 0))
                      for p in (question.get("parts") or [])
                      if isinstance(p, dict)]])
    digest.update(json.dumps(rows, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()[:32]


def verification_status(record: dict | None) -> str:
    """The persisted verification state, defaulting to NOT_VERIFIED."""
    block = (record or {}).get("verification") or {}
    state = str(block.get("state") or "").upper()
    return state if state in (STATUS_NOT_VERIFIED, STATUS_VERIFYING,
                              STATUS_VERIFIED, STATUS_NEEDS_REPAIR,
                              STATUS_REPAIRING, STATUS_REVERIFYING,
                              STATUS_NEEDS_MANUAL_REVIEW) else STATUS_NOT_VERIFIED


def completeness_of(mark_scheme_path: str | Path) -> dict:
    """The persisted completeness verdict for a mark-scheme file (no AI).

    Returns ``{}`` when nothing has established one yet — an empty verdict is
    not the same as a clean one, and callers must not treat it as complete.
    """
    try:
        sha = file_hash(mark_scheme_path)
    except OSError:
        return {}
    record = load_record(sha) or {}
    completeness = record.get("completeness")
    if not isinstance(completeness, dict):
        return {}
    return dict(completeness)


def review_requirement(mark_scheme_path: str | Path) -> dict:
    """Whether a stored structure must be treated as unverified, and why.

    Used by the marking path: marking itself is never blocked (existing correct
    behaviour is preserved), but a result marked against an incomplete or
    unreviewed structure carries an explicit warning instead of looking routine.
    """
    verdict = completeness_of(mark_scheme_path)
    if not verdict:
        return {"known": False, "incomplete": False, "warnings": []}
    warnings: list[str] = []
    if verdict.get("missing_indices"):
        warnings.append(
            "The mark-scheme structure is missing question index/indices "
            + ", ".join(str(i) for i in verdict["missing_indices"][:12])
            + ", which the document prints. Marks for this assignment are "
              "therefore not verified against a complete mark scheme.")
    if verdict.get("failed_chunks"):
        warnings.append("Page range(s) "
                        + ", ".join(str(c) for c in verdict["failed_chunks"][:12])
                        + " of the mark scheme could not be read.")
    if verdict.get("conflicting_indices"):
        warnings.append("Overlapping reads disagree about question index/indices "
                        + ", ".join(str(i) for i in verdict["conflicting_indices"][:12])
                        + ".")
    if str(verdict.get("verdict") or "").startswith("UNESTABLISHED"):
        warnings.append("The mark scheme's own numbering does not establish how "
                        "many questions it contains.")
    return {
        "known": True,
        "incomplete": bool(verdict.get("unresolved")),
        "state": verdict.get("state") or verdict.get("verdict"),
        "missing_indices": verdict.get("missing_indices") or [],
        "warnings": warnings,
    }


def _question_number(question: Any) -> str:
    """A question's number as a USABLE string, or "" when it has none.

    ``None`` and whitespace are not question numbers, but ``str(None)`` is the
    perfectly good string "None", and that is what this module used to compute
    with. The consequences were not cosmetic:

    * ``validate()`` accepted ``{"question_number": None}`` ("None" is a
      non-empty string), so an unnumbered blueprint could pass the gate that
      decides whether a structure may be used at all;
    * ``structure_report()`` fed the same "None" into the numbering sequence,
      where ``_leading_number`` correctly found no digit and dropped it, so a
      structure whose questions were ALL unnumbered produced no gaps and no
      duplicates, reported ``complete: True`` and returned confidence HIGH -
      telling the teacher "no need to verify" about the exact incomplete
      extraction this module exists to expose.

    Two gates disagreeing about the same data, with the reassuring one winning,
    is the failure mode. This function is the single answer to "what number does
    this question have", so both gates agree.
    """
    if not isinstance(question, dict):
        return ""
    value = question.get("question_number")
    if value is None:
        return ""
    return str(value).strip()


def validate(blueprint: Any) -> bool:
    """A blueprint is usable only with a non-empty list of sane questions."""
    if not isinstance(blueprint, dict):
        return False
    questions = blueprint.get("questions")
    if not isinstance(questions, list) or not questions:
        return False
    seen: set[str] = set()
    for q in questions:
        if not isinstance(q, dict):
            return False
        number = _question_number(q)
        try:
            max_marks = int(q.get("max_marks"))
        except (TypeError, ValueError):
            return False
        if not number or max_marks <= 0 or number in seen:
            return False
        seen.add(number)
    return True


# ---------------------------------------------------------------- extraction

# Deterministic structural validation (Part F). The extraction is best-effort;
# these checks make an incomplete mark scheme VISIBLE instead of silently
# producing a smaller denominator (the "25 questions when the paper has 33"
# class of bug). They never change a mark — they only describe the structure.
_LEADING_INT = re.compile(r"\s*(\d+)")


def _leading_number(question_number: str) -> int | None:
    match = _LEADING_INT.match(str(question_number))
    return int(match.group(1)) if match else None


def structure_report(blueprint: dict | None) -> dict:
    """Describe an extracted mark-scheme structure deterministically.

    Returns question count, total marks, the numbering sequence, any missing
    numbers in the sequence (a strong incomplete-extraction signal), and
    per-question sanity problems. No AI, no network — safe to call anywhere.
    """
    questions = (blueprint or {}).get("questions") or []
    # Numbers come from ONE helper so an unnumbered question is reported as
    # unnumbered instead of as the string "None" (see ``_question_number``).
    numbers = [_question_number(q) for q in questions]
    unnumbered = [n for n in numbers if not n]
    seq = [n for n in (_leading_number(x) for x in numbers) if n is not None]
    unique = sorted(set(seq))
    gaps: list[str] = []
    if unique:
        present = set(unique)
        gaps = [str(n) for n in range(unique[0], unique[-1] + 1) if n not in present]
    duplicate_numbers = sorted({n for n in numbers
                                if n and numbers.count(n) > 1})
    missing_marks = [numbers[idx] or f"#{(idx + 1)}"
                     for idx, q in enumerate(questions)
                     if int(q.get("max_marks", 0) or 0) <= 0]

    # Subquestion structure (§2/§8). Only reported when the scheme actually
    # declares sub-parts: a flat scheme of whole questions has nothing to say
    # here, and blanket findings would bury the real ones.
    from . import scheme_structure as st
    struct = st.build_structure(questions)
    sub_report = st.validate_structure(struct["questions"])
    sub_anomalies = [a for a in sub_report["anomalies"]
                     if a.get("severity") == st.SEVERITY_REVIEW]
    descends = bool(struct["subquestions"])
    return {
        "question_count": len(questions),
        "total_marks": sum(int(q.get("max_marks", 0) or 0) for q in questions),
        "numbering": numbers,
        "first": unique[0] if unique else None,
        "last": unique[-1] if unique else None,
        "gaps": gaps,
        "duplicate_numbers": duplicate_numbers,
        "missing_max_marks": missing_marks,
        #: How many questions carry no usable number at all. Non-zero means the
        #: numbering cannot be checked, which is itself an extraction defect.
        "unnumbered_questions": len(unnumbered),
        "has_gaps": bool(gaps),
        "complete": bool(questions) and not gaps and not duplicate_numbers \
                    and not missing_marks and not unnumbered,
        # --- subquestion structure ---------------------------------------
        "has_subquestions": descends,
        "subquestion_count": len(struct["subquestions"]),
        "subquestions": [{
            "id": s["id"], "label": s["label"],
            "question": s["parent_id"], "max_marks": s["max_marks"],
            "answers": s["answers"],
            "mark_codes": (s.get("marks") or {}).get("codes") or [],
            "validation": s.get("validation"),
            "confidence": s.get("confidence"),
        } for s in struct["subquestions"]],
        "subquestion_anomalies": sub_anomalies if descends else [],
        "subquestions_complete": (not sub_anomalies) if descends else None,
    }


CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_NEEDS_REVIEW = "NEEDS_REVIEW"


def mark_scheme_confidence(report: dict, *, declared_total: int | None = None,
                           disagreement: bool = False) -> dict:
    """How much to trust an extracted structure, and should we stop and check?

    Implements the §26/§27 suspicion list deterministically - no AI, no network,
    no API spend - so an obviously broken extraction is caught the moment it is
    parsed rather than after a class has been marked against it:

    * no questions at all, or a zero total,
    * a single question extracted from a multi-mark paper,
    * a total far too small to be a real paper,
    * missing / duplicate question numbers, questions with no mark allocation,
    * a detected total that disagrees with the mark scheme's own stated total,
    * two verification passes that disagreed.

    ``requires_verification`` is the signal for the UI to offer
    "Verify Mark Scheme". It is deliberately conservative: when the parser is
    clean and the totals agree, no Gemini request is made.
    """
    reasons: list[str] = []
    questions = report.get("question_count") or 0
    total = report.get("total_marks") or 0

    if not questions:
        reasons.append("No questions were detected in the mark scheme.")
    if total <= 0:
        reasons.append("Total marks are zero or missing — nothing can be graded.")
    elif questions <= 1 and total > 10:
        reasons.append(
            f"Only {questions} question was detected across {total} marks — "
            "the extraction is almost certainly incomplete.")
    elif questions > 1 and total < questions:
        reasons.append(
            f"Total marks ({total}) is smaller than the question count "
            f"({questions}) — impossible.")
    if report.get("gaps"):
        reasons.append("Missing question numbers: " + ", ".join(report["gaps"]) + ".")
    if report.get("duplicate_numbers"):
        reasons.append("Duplicate question numbers: "
                       + ", ".join(report["duplicate_numbers"]) + ".")
    if report.get("unnumbered_questions"):
        reasons.append(
            f"{report['unnumbered_questions']} question(s) carry no question "
            "number — the numbering cannot be checked and the paper is "
            "probably incomplete.")
    if report.get("missing_max_marks"):
        reasons.append("Questions with no mark allocation: "
                       + ", ".join(report["missing_max_marks"]) + ".")
    # Subquestion-level findings (§2/§8): a subquestion with no answer, two
    # marking points sharing one identity, or sub-part marks that do not add up
    # all make the extraction unreliable for the affected question only.
    for issue in (report.get("subquestion_anomalies") or []):
        reasons.append(str(issue.get("message") or issue))
    if declared_total is not None and int(declared_total or 0) > 0 \
            and int(declared_total) != total:
        reasons.append(
            f"Detected question total {total} does not match the mark scheme's "
            f"stated total of {declared_total}.")
    if disagreement:
        reasons.append("Independent verification passes disagreed with each other.")

    return {
        "confidence": CONFIDENCE_HIGH if not reasons else CONFIDENCE_NEEDS_REVIEW,
        "reasons": reasons,
        "requires_verification": bool(reasons),
    }


def validation_warnings(report: dict, *, expected_count: int | None = None,
                        expected_total: int | None = None) -> list[str]:
    """Human-readable warnings for a structure report (Part F)."""
    warnings: list[str] = []
    if not report.get("question_count"):
        warnings.append("No questions were extracted from the mark scheme.")
        return warnings
    if report.get("gaps"):
        warnings.append(
            "Missing question numbers in the mark-scheme numbering: "
            + ", ".join(report["gaps"]) + ". Possible incomplete extraction.")
    if report.get("duplicate_numbers"):
        warnings.append("Duplicate question numbers: "
                        + ", ".join(report["duplicate_numbers"]) + ".")
    if report.get("unnumbered_questions"):
        warnings.append(
            f"{report['unnumbered_questions']} question(s) have no question "
            "number, so the numbering cannot be checked. The extraction is "
            "probably incomplete.")
    if report.get("missing_max_marks"):
        warnings.append("Questions with no mark allocation: "
                        + ", ".join(report["missing_max_marks"]) + ".")
    for issue in (report.get("subquestion_anomalies") or []):
        warnings.append(str(issue.get("message") or issue))
    if expected_count is not None and report["question_count"] != expected_count:
        warnings.append(
            f"The mark scheme has {report['question_count']} question(s) but "
            f"{expected_count} were expected — check for missing questions.")
    if expected_total is not None and report["total_marks"] != expected_total:
        warnings.append(
            f"Extracted marks total {report['total_marks']} but {expected_total} "
            f"were expected — {abs(expected_total - report['total_marks'])} mark(s) "
            f"may be missing.")
    return warnings


class _LastError(Exception):
    """Adapter so a stored error string can be classified by kind."""

    def __init__(self, message: str):
        super().__init__(message)


def get_blueprint(client, mark_scheme_path: str | Path, *,
                  status_callback=None, force: bool = False) -> tuple[dict | None, dict]:
    """Return ``(blueprint_or_None, meta)`` for a local mark-scheme file.

    ``meta`` always describes what happened (``source``:
    ``cache`` / ``built`` / ``unavailable``) so diagnostics can show where the
    question structure came from. Extraction is best-effort: any failure
    returns ``(None, {...})`` and the caller falls back to legacy free-form
    marking — never blocks a correction.

    ``force=True`` ignores a cached blueprint and re-extracts (used by the
    teacher's "Recheck mark scheme" action). If the re-extraction fails, the
    previously cached blueprint is kept rather than discarding working state.
    """
    meta: dict[str, Any] = {"source": "unavailable", "sha256": None}
    try:
        sha = file_hash(mark_scheme_path)
    except OSError as exc:
        meta["error"] = f"mark scheme unreadable: {exc}"
        return None, meta
    meta["sha256"] = sha

    record = load_record(sha) or {}
    cached = load_cached(sha)
    if cached and not force:
        meta["source"] = "cache"
        meta["total_marks"] = sum(int(q["max_marks"]) for q in cached["questions"])
        # A cached structure carries its verdict with it: the completeness the
        # page-scoped read established, the verification state, and whether a
        # human still has to look. Reading the structure must never look the
        # same whether or not the mark scheme was ever fully read.
        meta["completeness"] = record.get("completeness") or {}
        meta["verification_state"] = verification_status(record)
        meta["inventory"] = record.get("inventory") or {}
        meta["anomalies"] = record.get("anomalies") or []
        meta["review"] = review_requirement(mark_scheme_path)
        return cached, meta

    # ---- Stage A: reconstruct the document locally, before asking any model.
    # Free, deterministic, and it tells us whether there is even a text layer
    # to reason about. A cached structure is reused without this cost.
    from . import mark_scheme as _ms
    from . import mark_scheme_verify as _msv
    try:
        evidence = _ms.analyse_document(str(mark_scheme_path))
    except Exception as exc:  # noqa: BLE001 - never block a correction
        evidence = {"mode": _ms.MODE_NONE, "error": str(exc)[:200],
                    "pages": [], "candidates": [], "diagnostics": {}}
    meta["evidence"] = _msv.evidence_summary(evidence)

    if get_config().mock_mode:
        if cached:
            meta["source"] = "cache"
            meta["note"] = "mock mode: kept the cached mark-scheme structure"
            return cached, meta
        meta["error"] = "mock mode: blueprint extraction skipped"
        return None, meta

    if status_callback:
        status_callback("📘 Reading the mark scheme structure…")
    try:
        remote = client.upload_file(str(mark_scheme_path))
    except Exception as exc:
        meta["error"] = f"mark scheme upload failed: {exc}"
        return None, meta

    try:
        from . import model_health_store as mhs
        chain = mhs.ordered_models(list(get_config().gemini.primary_models))
        candidates = legacy.MODEL_HEALTH.usable_models(chain) or chain
        errors: list[str] = []
        parsed: dict | None = None
        model_name: str | None = None

        for name in candidates:
            for attempt in range(2):
                try:
                    response = client.models.generate_content(
                        model=name,
                        contents=[remote,
                                  _msv.VERIFY_SYSTEM + "\n\nRECONSTRUCTED DOCUMENT "
                                  "EVIDENCE (page/block structure, in reading "
                                  "order):\n"
                                  + _msv.format_evidence(evidence)],
                        config={
                            "system_instruction": _msv.VERIFY_SYSTEM,
                            "response_mime_type": "application/json",
                            "response_schema": _msv.VERIFY_SCHEMA,
                            **legacy.DETERMINISTIC_SAMPLING,
                        },
                    )
                    raw = response.text if response else None
                    parsed = legacy.extract_json(raw or "")
                    model_name = name
                    legacy.MODEL_HEALTH.record_success(name)
                    mhs.record_success(name, chain)
                    break
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name}: {exc}")
                    if legacy.is_timeout_error(exc):
                        legacy.MODEL_HEALTH.record_failure(name, exc)
                        break  # do not burn another full timeout
                    if legacy.is_temporary_model_error(exc) and attempt == 0:
                        time.sleep(2)
                        continue
                    legacy.MODEL_HEALTH.record_failure(name, exc)
                    break
            if parsed is not None:
                break

        if parsed is None or model_name is None:
            # Classify WHY it failed. A provider outage is not a broken mark
            # scheme, and must never invalidate a structure we already trust.
            kinds = [_msv.classify_failure(_LastError(e)) for e in errors]
            transient = any(k in _msv.TRANSIENT_FAILURES for k in kinds)
            meta["failure"] = kinds[0] if kinds else _msv.FAILURE_EXTRACTION
            meta["transient"] = transient
            meta["error"] = (
                "blueprint extraction failed: " + (" | ".join(errors) or "no models"))
            log.warning("blueprint extraction failed (%s): %s",
                        meta["failure"], meta["error"])
            if cached:
                meta["source"] = "cache"
                meta["note"] = ("verification failed; the existing verified "
                                "structure has been preserved")
                return cached, meta
            return None, meta
    except Exception as exc:
        meta["error"] = f"blueprint extraction failed: {exc}"
        log.warning("blueprint extraction failed: %s", exc)
        if cached:
            meta["source"] = "cache"
            return cached, meta
        return None, meta
    finally:
        try:
            client.files_delete(remote)
        except Exception:
            pass

    questions = _clean_questions(parsed.get("questions") if isinstance(parsed, dict) else None)
    blueprint = {"questions": questions}
    if not validate(blueprint):
        meta["failure"] = _msv.FAILURE_MALFORMED
        meta["error"] = "blueprint extraction returned an unusable structure"
        if cached:
            meta["source"] = "cache"
            return cached, meta
        return None, meta

    # ---- Stage B: canonicalise, cross-check, and decide whether this result
    # is allowed to replace what we already have.
    structure = _msv.canonicalize(parsed)
    # A completeness verdict established by an earlier page-scoped read is an
    # INPUT to this check, not something a cheaper single-request extraction is
    # allowed to forget.
    inventory = record.get("inventory") or None
    validation = _msv.cross_validate(structure, evidence, inventory)
    meta["validation"] = {
        "verified": validation["verified"],
        "structural_confidence": validation["structural_confidence"],
        "anomalies": validation["anomalies"],
        "checks": validation["checks"],
        "extraction_mode": validation["extraction_mode"],
        "evidence_kind": validation["evidence_kind"],
        "confidence_basis": validation["confidence_basis"],
        "anomaly_records": validation["anomaly_records"],
    }
    action, detail = _msv.prefer(cached, structure, None, validation)
    meta["structure_decision"] = {"action": action, **detail}

    if action == "keep_existing" and cached:
        meta["source"] = "cache"
        meta["total_marks"] = sum(int(q["max_marks"]) for q in cached["questions"])
        meta["note"] = detail.get("reason", "existing verified structure preserved")
        meta["completeness"] = record.get("completeness") or {}
        meta["verification_state"] = verification_status(record)
        meta["anomalies"] = record.get("anomalies") or []
        meta["review"] = review_requirement(mark_scheme_path)
        log.info("mark-scheme verification: keeping existing structure (%s)",
                 meta["note"])
        return cached, meta

    # The canonical (numerically ordered, deduplicated) question list wins.
    questions = _clean_questions(structure["questions"])
    blueprint = {"questions": questions}
    if not validate(blueprint):
        meta["failure"] = _msv.FAILURE_MALFORMED
        meta["error"] = "canonicalised structure was unusable"
        if cached:
            meta["source"] = "cache"
            return cached, meta
        return None, meta

    # The total is SUMMED from the structure, never taken on trust. When it
    # disagrees with the printed total that is reported, not silently dropped.
    total = sum(int(q["max_marks"]) for q in questions)
    paper = str(parsed.get("paper") or "").strip() if isinstance(parsed, dict) else ""
    blueprint["total_marks"] = total
    blueprint["paper"] = paper

    # The verdict travels with the structure. ``verified`` is only true when
    # every independent source agreed AND no completeness problem is known.
    completeness = record.get("completeness") or {}
    complete = bool(completeness) and not completeness.get("unresolved")
    state = (STATUS_VERIFIED if (validation["verified"] and complete)
             else STATUS_NEEDS_MANUAL_REVIEW if not complete and completeness
             else STATUS_NEEDS_REPAIR)
    save_record(
        sha, blueprint, model=model_name,
        anomalies=validation["anomaly_records"],
        evidence_kind=validation["evidence_kind"],
        confidence_basis=validation["confidence_basis"],
        verification={
            "state": state,
            "updated_at": time.time(),
            "anomalies": len(validation["anomalies"]),
            "verified": bool(validation["verified"] and complete),
            "attempts": int(((record.get("verification") or {}).get("attempts") or 0)) + 1,
            "basis": validation["confidence_basis"],
        })
    meta.update(source="built", total_marks=total, model=model_name,
                completeness=completeness, verification_state=state,
                anomalies=validation["anomaly_records"],
                review=review_requirement(mark_scheme_path))
    # Diagnostics, no secrets: the model, the evidence kind, the counts and the
    # finish shape are what make "why was this structure believed?" answerable.
    log.info("mark-scheme verification: %d questions / %d marks (confidence %.2f, "
             "%d anomaly/anomalies, evidence=%s, kind=%s, model=%s, "
             "inventory=%s)",
             len(questions), total, validation["structural_confidence"],
             len(validation["anomalies"]), validation["extraction_mode"],
             validation["evidence_kind"], model_name,
             (completeness.get("verdict") or "not-established"))
    return blueprint, meta


def _clean_parts(raw: Any) -> list[dict]:
    """Normalise a question's sub-parts, keeping each part's OWN evidence.

    The extraction already asks the model for per-part criteria and answers,
    but this function used to drop the whole ``parts`` field on the floor —
    which is how every sub-part's answer, marking points and allocation were
    left with nowhere to live except the parent's one free-text string.
    """
    if not isinstance(raw, list):
        return []
    parts: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = str(item.get("number") or item.get("question_number")
                     or item.get("label") or "").strip()
        try:
            max_marks = int(item.get("max_marks"))
        except (TypeError, ValueError):
            continue
        if not number or max_marks <= 0:
            continue
        part: dict[str, Any] = {"number": number, "max_marks": max_marks}
        criteria = str(item.get("criteria", "") or "").strip()
        if criteria:
            part["criteria"] = criteria
        answers = [str(a).strip() for a in (item.get("answers") or [])
                   if str(a).strip()]
        if answers:
            part["answers"] = answers
        try:
            page = int(item.get("page"))
        except (TypeError, ValueError):
            page = 0
        if page > 0:
            part["page"] = page
        if str(item.get("validation") or ""):
            part["validation"] = str(item["validation"])
        parts.append(part)
    return parts


def _declared_subquestion_for(label: str, target: str,
                              sub_by_parent: dict[str, list[dict]]) -> tuple[str, bool]:
    """Which declared subquestion of ``target`` a returned row label names.

    Returns ``(subquestion_id, foreign)``. ``subquestion_id`` is the declared
    marking point the label identifies — the identity is the FULL nested label,
    which is what keeps ``4(b)(ii)`` from being read as ``4(b)(i)``. It is
    ``""`` when the label names the question itself, when the scheme declares
    no subquestions, or when the label is an ambiguous GROUPING label
    (``4(b)`` while ``(b)(i)`` and ``(b)(ii)`` are declared).

    ``foreign`` is True only when the label names something the question does
    not have at all (``4(c)(iv)`` under a scheme with no (c)): that row is
    flagged for review, never attached to a neighbouring marking point and
    never silently summed into one.
    """
    from . import scheme_structure as st
    declared = [str(s.get("id") or "") for s in sub_by_parent.get(target) or []]
    if not declared:
        return "", False
    full_id = st.subquestion_id(label)
    if not full_id or full_id == str(target):
        return "", False
    if full_id in declared:
        return full_id, False
    walked = st.parent_id(full_id)
    while walked:
        if walked in declared:
            return walked, False
        walked = st.parent_id(walked)
    if any(d.startswith(full_id) or full_id.startswith(d) for d in declared):
        return "", False      # ancestor / grouping label: ambiguous, not foreign
    return "", True


def _award_of(value: Any) -> int:
    """A returned row's award as a whole number (0 when unreadable).

    The batch validator reports an unreadable mark as a problem before this
    point, so reconciliation must not also raise on it: the question is held
    for review by the validator's verdict, and no invented mark is credited
    here.
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return 0
        return int(number) if number.is_integer() else 0


def _claims_whole_question(row: dict, max_marks: int) -> bool:
    """Is this returned row a mark for the WHOLE question?

    True only when the marker's own maximum equals the scheme's maximum for the
    question (it read the same allocation) and the award is within range. A row
    that claims a smaller maximum is a fragment and stays ambiguous.
    """
    try:
        claimed_max = int(row.get("max_marks") or 0)
        awarded = int(row.get("awarded_marks") or 0)
    except (TypeError, ValueError):
        return False
    return claimed_max == int(max_marks) and 0 <= awarded <= claimed_max


def _part_canonical(owner_number: str, part_number: Any) -> str:
    """The canonical id of a part, whether it is printed locally or in full."""
    from . import scheme_structure as st
    text = str(part_number or "").strip()
    if not text:
        return ""
    if st.parse_label(text)[0] is None:
        return st.local_subquestion(owner_number, text)[0]
    return st.subquestion_id(text)


def _clean_questions(raw: Any) -> list[dict]:
    """Normalise the extracted questions, keeping subquestion structure.

    A row that labels a SUB-PART of a question the same structure already
    declares is folded into that question as a sub-part rather than kept as a
    second scored row: it stays available as evidence (its own criteria,
    answers and allocation) without its marks being counted twice.
    """
    if not isinstance(raw, list):
        return []
    from . import scheme_structure as st
    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = str(item.get("question_number", "")).strip()
        try:
            max_marks = int(item.get("max_marks"))
        except (TypeError, ValueError):
            continue
        if not number or max_marks <= 0 or number in seen:
            continue
        seen.add(number)
        entry: dict[str, Any] = {
            "question_number": number,
            "max_marks": max_marks,
            "criteria": str(item.get("criteria", "")).strip(),
        }
        parts = _clean_parts(item.get("parts") or item.get("subquestions"))
        if parts:
            entry["parts"] = parts
        answers = [str(a).strip() for a in (item.get("answers") or [])
                   if str(a).strip()]
        if answers:
            entry["answers"] = answers
        try:
            page = int(item.get("page"))
        except (TypeError, ValueError):
            page = 0
        if page > 0:
            entry["page"] = page
        cleaned.append(entry)

    # Fold a sub-part row into the question it belongs to when that question is
    # declared: "4(a)" next to "4" is 4's part (a), not a second question.
    by_number = {str(q["question_number"]): q for q in cleaned}
    folded: list[dict] = []
    for entry in cleaned:
        canonical = st.subquestion_id(entry["question_number"])
        parent = st.parent_id(canonical)
        owner = None
        while parent:
            for candidate in by_number.values():
                if st.subquestion_id(candidate["question_number"]) == parent:
                    owner = candidate
                    break
            if owner is not None:
                break
            parent = st.parent_id(parent)
        if owner is None or owner is entry:
            folded.append(entry)
            continue
        part = {"number": entry["question_number"],
                "max_marks": entry["max_marks"]}
        if entry.get("criteria"):
            part["criteria"] = entry["criteria"]
        if entry.get("answers"):
            part["answers"] = entry["answers"]
        # Compared by IDENTITY, not by spelling: "4(a)" and "a" are the same
        # subquestion, and an alias must not become a second copy of it.
        known = {_part_canonical(str(owner["question_number"]), p.get("number"))
                 for p in owner.get("parts") or []}
        if st.subquestion_id(entry["question_number"]) not in known:
            owner.setdefault("parts", []).append(part)
        owner.setdefault("folded_rows", []).append(entry["question_number"])
    return folded


# ---------------------------------------------------------------- reconciliation

def _matches(model_number: str, bp_number: str) -> bool:
    """True when a model-produced number belongs to a blueprint question.

    Handles sub-part outputs ("14(a)", "7b") mapping onto a single blueprint
    question ("14", "7") WITHOUT letting "1" swallow "14".
    """
    if model_number == bp_number:
        return True
    if model_number.startswith(bp_number) and len(model_number) > len(bp_number):
        rest = model_number[len(bp_number)]
        return rest in "(.-_abcdefghijklmnopqrstuvwxyz"
    return False


def apply_blueprint(result: dict, blueprint: dict | None) -> tuple[dict, dict]:
    """Reconcile a normalised marking result against the official blueprint.

    Returns ``(result, report)``. Never raises, never invents marks, never
    turns an existing mark into 0 because metadata was missing. Questions the
    model failed to return are added with ``status=REVIEW`` so the score stays
    honest about the gap instead of silently dropping the question's marks
    from the denominator (which previously inflated totals to fake 100%s).
    """
    report = {
        "applied": False,
        "source": None,
        "blueprint_total": None,
        "missing_questions": [],
        "extra_questions": [],
        "merged_questions": [],
        "max_marks_corrected": [],
        #: Subquestions the scheme declares that the marker returned nothing
        #: for. Distinct from ``missing_questions``: the question itself came
        #: back, one of its marking points did not.
        "missing_subquestions": [],
        #: Two returned rows claiming the SAME subquestion — never resolved by
        #: guessing which one is real.
        "subquestion_conflicts": [],
        #: Returned rows whose identifier the scheme does not declare.
        "unmatched_rows": [],
        #: Rows whose awarded marks had to be clamped to the question maximum.
        "award_clamped": [],
        #: Questions marked as ONE row for the whole question while the scheme
        #: declares sub-parts: the marks are complete, the per-sub-part
        #: attribution is unavailable (reported, never guessed).
        "question_level_marks": [],
        #: Returned rows that named no subquestion while the question declares
        #: some: which marking point they cover cannot be established.
        "unattributed_rows": [],
        #: Rows that exist but are still unresolved (a placeholder kept from an
        #: earlier pass, a split-question flag, a conflict). Distinct from
        #: ``missing_questions`` (no row at all) so callers can hold the
        #: correction for review in both cases.
        "still_unresolved": [],
    }
    if not blueprint or not validate(blueprint):
        for q in result.get("questions", []):
            q.setdefault("status", STATUS_VALID)
        return result, report

    bp_by_number = {str(q["question_number"]): q for q in blueprint["questions"]}
    report.update(
        applied=True,
        source="mark_scheme",
        blueprint_total=sum(int(q["max_marks"]) for q in bp_by_number.values()),
    )

    # -- group the model's questions onto blueprint questions -----------------
    # Canonical matching (question_inventory): "62(a)", "Q62(a)", "62 a" all
    # map onto the blueprint row the pipeline treats as question 62's result,
    # without letting "1" ever swallow "14". Structure-derived extra rows
    # (paper headers, stray marks values) are still treated as extras.
    from .question_inventory import canonical_question_id
    from . import scheme_structure as st
    canonical_ids: dict[str, str] = {}
    for bpn in bp_by_number:
        canonical_ids[canonical_question_id(bpn)] = bpn

    # The declared subquestions, keyed both ways: which question owns each
    # subquestion id, and which subquestions each question declares.
    structure = st.build_structure(blueprint["questions"])
    owner: dict[str, str] = {}
    sub_by_parent: dict[str, list[dict]] = {}
    for parent_row in structure["questions"]:
        pnum = str(parent_row["question_number"])
        owner[str(parent_row["id"])] = pnum
        sub_by_parent[pnum] = parent_row.get("subquestions") or []
        for sub in sub_by_parent[pnum]:
            owner[str(sub["id"])] = pnum

    grouped: dict[str, list[dict]] = {}
    extras: list[dict] = []
    for q in result.get("questions", []):
        number = str(q.get("question_number", ""))
        target = None
        if number in bp_by_number:
            target = number
        else:
            target = next(
                (bpn for bpn in bp_by_number if _matches(number, bpn)), None)
        if target is None:
            # Walk the label's own nesting to the deepest DECLARED node
            # ("4(b)(ii)" -> its own subquestion, or its question).
            walked = st.subquestion_id(number)
            while walked:
                if walked in owner:
                    target = owner[walked]
                    break
                walked = st.parent_id(walked)
        if target is None:
            # Alias forms ("Q62(a)", " q14. ") through the canonical id.
            can = canonical_question_id(number)
            parent = can
            while parent and parent not in canonical_ids \
                    and "a" <= parent[-1:] <= "z":
                parent = parent[:-1]
            target = canonical_ids.get(parent) or canonical_ids.get(can)

        if target is None:
            # Names nothing the scheme declares at all: the model's judgement is
            # kept (a mark is never deleted) but it is flagged and can never
            # count against a real question.
            if st.subquestion_id(number) not in owner:
                report["unmatched_rows"].append(
                    {"question_number": number,
                     "reason": "the mark scheme declares no such subquestion"})
            extras.append(q)
            continue

        # Attribute the row to the DECLARED subquestion it names. A row that
        # names a marking point the question does not have stays with the
        # question as an unattributed row and is reported — never silently
        # summed into whichever subquestion is processed first.
        sub_id, foreign = _declared_subquestion_for(number, target,
                                                    sub_by_parent)
        q["_subquestion_id"] = sub_id
        if foreign:
            report["unmatched_rows"].append(
                {"question_number": number,
                 "reason": "the mark scheme declares no such subquestion"})
        grouped.setdefault(target, []).append(q)

    merged_questions: list[dict] = []
    reconciled: list[dict] = []

    for bp in blueprint["questions"]:
        number = str(bp["question_number"])
        max_marks = int(bp["max_marks"])
        parts = grouped.get(number, [])

        if not parts:
            # Present in the official mark scheme, absent from the model's
            # answer. This is a TECHNICAL GAP, not a student score of 0 —
            # it is flagged loudly and counted separately in verification.
            reconciled.append({
                "question_number": number,
                "max_marks": max_marks,
                "awarded_marks": 0,
                "topic": "",
                "student_answer": "",
                "working_summary": "",
                "loss_reason": UNMARKED_REASON,
                "mark_breakdown": [],
                "correct_method": "",
                "full_mark_solution": "",
                "status": STATUS_REVIEW,
                "review_reason": UNMARKED_REASON,
            })
            report["missing_questions"].append(number)
            continue

        if len(parts) > 1:
            report["merged_questions"].append(number)

        # ---- SUBQUESTION attribution (§4) -------------------------------
        # Each returned row is attributed to the subquestion it NAMES. A row
        # that names none is the question as a whole; with subquestions
        # declared that is not enough to say which marking point it covers, so
        # it is reported rather than guessed at.
        declared_subs = sub_by_parent.get(number) or []
        per_sub: dict[str, list[dict]] = {}
        for row in parts:
            key = str(row.pop("_subquestion_id", "") or "")
            per_sub.setdefault(key, []).append(row)
        unattributed = len(per_sub.pop("", []))
        if declared_subs and unattributed:
            report["unattributed_rows"].append(
                {"question": number, "rows": unattributed})

        breakdown: list[dict] = []
        missing_subs: list[dict] = []
        conflicted = 0
        for sub in declared_subs:
            rows_for_sub = per_sub.get(sub["id"], [])
            sub_max = int(sub.get("max_marks") or 0)
            got = sum(_award_of(r.get("awarded_marks")) for r in rows_for_sub)
            if rows_for_sub and sub_max:
                got = max(0, min(got, sub_max))
            if len(rows_for_sub) > 1:
                conflicted += 1
                report["subquestion_conflicts"].append(
                    {"question": number, "subquestion": sub["id"],
                     "rows": [str(r.get("question_number")) for r in rows_for_sub]})
            if not rows_for_sub:
                missing_subs.append({"question": number,
                                     "subquestion": sub["id"],
                                     "label": sub.get("label") or sub["id"]})
            breakdown.append({
                "id": sub["id"],
                "label": sub.get("label") or sub["id"],
                "parent_id": sub.get("parent_id") or number,
                "max_marks": sub_max,
                "awarded_marks": got,
                "status": "MARKED" if rows_for_sub else "UNMARKED",
                "rows": [str(r.get("question_number")) for r in rows_for_sub],
                "answers": sub.get("answers") or [],
                "mark_codes": (sub.get("marks") or {}).get("codes") or [],
            })

        # A question whose scheme declares sub-parts can still come back as ONE
        # row for the whole question ("1", maximum 5, award 5). That is a
        # complete question-level mark, not an ambiguous fragment: the row
        # claims the scheme's own maximum, no sub-part row competes with it and
        # nothing was claimed twice. Treating it as "unattributed" is what
        # parked 60 of 67 questions in teacher review on a real 50-page paper
        # and collapsed the reported denominator to the 22 marks of the seven
        # rows that survived. The marks are kept whole and the attribution gap
        # is REPORTED, never guessed.
        question_level = (
            bool(declared_subs) and unattributed == 1 and not per_sub
            and not conflicted and len(parts) == 1
            and _claims_whole_question(parts[0], max_marks)
        )
        if question_level:
            report["question_level_marks"].append(
                {"question": number, "max_marks": max_marks,
                 "awarded": _award_of(parts[0].get("awarded_marks"))})
        else:
            report["missing_subquestions"].extend(missing_subs)

        raw_awarded = sum(_award_of(p.get("awarded_marks")) for p in parts)
        awarded = max(0, min(raw_awarded, max_marks))
        if awarded != raw_awarded:
            report["award_clamped"].append(
                {"question": number, "awarded": raw_awarded, "max": max_marks})

        old_max = parts[0].get("max_marks")
        claimed_max = _award_of(old_max) if old_max is not None else max_marks
        if len(parts) == 1 and claimed_max != max_marks:
            report["max_marks_corrected"].append(
                {"question": number, "model": claimed_max,
                 "mark_scheme": max_marks})

        primary = parts[0]
        primary["question_number"] = number   # blueprint numbering is official
        primary["max_marks"] = max_marks
        primary["awarded_marks"] = awarded
        if breakdown and not question_level:
            # The per-subquestion result travels WITH the row, so a sub-part's
            # marks are never only an unwritten part of a summed total.
            primary["subquestions"] = breakdown
            primary["subquestion_marks"] = sum(
                int(b["awarded_marks"]) for b in breakdown)
            if missing_subs:
                primary["unmarked_subquestions"] = [
                    s["label"] for s in missing_subs]
        if question_level:
            primary["subquestion_attribution"] = "question_level"
        if (declared_subs and (missing_subs or unattributed or conflicted)
                and not question_level
                and str(primary.get("status") or "").upper()
                not in UNRESOLVED_STATUSES):
            # A question whose sub-parts were only PARTLY assessed — or whose
            # sub-part was claimed twice — must not read as complete: one
            # subquestion's marks cannot stand in for another marking point that
            # was never marked, and an ambiguous row cannot be trusted by
            # picking whichever row happens to be first.
            unresolved_because: list[str] = []
            if missing_subs:
                unresolved_because.append(
                    "the marker returned nothing for "
                    + ", ".join(s["label"] for s in missing_subs))
            if unattributed:
                unresolved_because.append(
                    f"{unattributed} returned row(s) name no subquestion")
            if conflicted:
                unresolved_because.append(
                    f"{conflicted} subquestion(s) were claimed by more than "
                    "one returned row")
            primary["status"] = STATUS_REVIEW
            primary["review_reason"] = (
                "This question's subquestions were not resolved cleanly ("
                + "; ".join(unresolved_because)
                + "). The missing or ambiguous marking points are UNMARKED, "
                  "not a confirmed zero — review them before the grade is "
                  "used.")
            report["still_unresolved"].append(number)
            reconciled.append(primary)
            continue
        if str(primary.get("status") or "").upper() in UNRESOLVED_STATUSES:
            # An UNRESOLVED row (the missing-result placeholder this function
            # itself inserted on an earlier pass, a split-question flag from the
            # merge layer, a conflict) must keep its state AND its reason.
            # Reconciling previously rewrote such a row to status=VALID and
            # deleted its review_reason, which laundered "the marker produced
            # no mark" into a clean, scored 0 — the failure mode where a
            # partially recovered paper silently read as complete.
            report["still_unresolved"].append(number)
            if not str(primary.get("student_answer") or "").strip() and \
                    not (primary.get("mark_breakdown") or []):
                report["missing_questions"].append(number)
            reconciled.append(primary)
            continue
        primary["status"] = STATUS_VALID
        primary.pop("review_reason", None)
        if len(parts) > 1:
            primary["student_answer"] = " | ".join(
                str(p.get("student_answer", "")).strip()
                for p in parts if str(p.get("student_answer", "")).strip())
        reconciled.append(primary)

    for q in extras:
        # Graded by the model but not in the mark scheme: keep the model's
        # judgement (never delete a mark) but flag for human review, and make
        # sure it cannot double-count against a blueprint question.
        q["status"] = STATUS_REVIEW
        q["review_reason"] = (
            "This question number does not appear in the mark scheme "
            "blueprint — verify it belongs to this paper before trusting it."
        )
        report["extra_questions"].append(str(q.get("question_number")))
        reconciled.append(q)

    report["merged_questions"] = sorted(set(report["merged_questions"]))
    result["questions"] = reconciled
    result["blueprint"] = {
        "total_marks": report["blueprint_total"],
        "questions": len(bp_by_number),
        "paper": blueprint.get("paper", ""),
    }
    return result, report


def blueprint_for_mark_scheme(mark_scheme_path: str | Path) -> tuple[dict | None, dict]:
    """Read the CACHED blueprint for a mark-scheme file without any AI call.

    Used by the mark-scheme validation surface so the teacher can inspect the
    extracted structure offline (historical access, Part M) — this never
    triggers a new extraction.
    """
    try:
        sha = file_hash(mark_scheme_path)
    except OSError as exc:
        return None, {"source": "unavailable", "error": f"mark scheme unreadable: {exc}"}
    cached = load_cached(sha)
    if cached is None:
        return None, {"source": "not_processed", "sha256": sha}
    # The verdict travels WITH the structure. The marking path reads this, so
    # leaving it out here is what let a correction proceed against a structure
    # whose completeness was never established.
    record = load_record(sha) or {}
    return cached, {
        "source": "cache",
        "sha256": sha,
        "manual_override": bool(cached.get("manual_override")),
        "total_marks": sum(int(q["max_marks"]) for q in cached["questions"]),
        "completeness": record.get("completeness") or {},
        "verification_state": verification_status(record),
        "inventory": record.get("inventory") or {},
        "anomalies": record.get("anomalies") or [],
        "review": review_requirement(mark_scheme_path),
    }


def save_manual_blueprint(sha: str, questions: list[dict], *, total_marks: int | None = None,
                          note: str = "") -> dict:
    """Persist a teacher-edited mark-scheme structure (Part F manual override).

    Validates the structure first: an override that would leave the mark
    scheme unusable is rejected rather than saved. Never touches any stored
    result — it only fixes the structure used by FUTURE corrections.
    """
    if not isinstance(questions, list) or not questions:
        raise ValueError("Provide at least one question for the mark scheme.")
    # Validate the teacher's input EXPLICITLY (never silently drop/merge a
    # question they typed — the whole point is to correct the extraction).
    seen: set[str] = set()
    cleaned: list[dict] = []
    for item in questions:
        if not isinstance(item, dict):
            raise ValueError("Each question must be an object.")
        # ``str(None)`` is the string "None", so this used to ACCEPT a question
        # whose number was null and save it as question "None" — through the
        # very gate that exists to reject an unusable structure. Same rule as
        # every other reader now.
        number = _question_number(item)
        if not number:
            raise ValueError("Every question needs a question number.")
        try:
            max_marks = int(item.get("max_marks"))
        except (TypeError, ValueError):
            raise ValueError(f"Question {number} needs a whole-number maximum mark.")
        if max_marks <= 0:
            raise ValueError(f"Question {number} must have a positive maximum mark.")
        if number in seen:
            raise ValueError(f"Duplicate question number: {number}.")
        seen.add(number)
        cleaned.append({"question_number": number, "max_marks": max_marks,
                        "criteria": str(item.get("criteria", "")).strip()})
    blueprint: dict[str, Any] = {"questions": cleaned}
    if not validate(blueprint):
        raise ValueError(
            "The edited mark scheme needs at least one valid question.")
    total = total_marks if total_marks is not None else sum(
        int(q["max_marks"]) for q in cleaned)
    if int(total) != sum(int(q["max_marks"]) for q in cleaned):
        raise ValueError(
            f"The stated total ({total}) does not match the question marks "
            f"({sum(int(q['max_marks']) for q in cleaned)}).")
    blueprint["total_marks"] = int(total)
    blueprint["manual_override"] = True
    blueprint["manual_note"] = str(note)[:500]
    save_cached(sha, blueprint, model="teacher_override")
    return blueprint


#: Rechecks running right now, keyed by document content hash. A second
#: request for a document already being verified must not start a second
#: extraction: the logs showed four identical passes running at once because
#: two UI buttons (and an impatient second click) hit the same endpoint.
_INFLIGHT: dict[str, float] = {}
_INFLIGHT_LOCK = threading.Lock()


def _begin_recheck(sha: str) -> bool:
    """Claim the verification slot for a document. False when already taken."""
    with _INFLIGHT_LOCK:
        if sha in _INFLIGHT:
            return False
        _INFLIGHT[sha] = time.time()
        return True


def _end_recheck(sha: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.pop(sha, None)


def recheck_decision(mark_scheme_path: str | Path) -> tuple[bool, str, str]:
    """Should this mark scheme be re-verified at all? ``(run, reason, state)``.

    Re-verification is only justified when the input changed or the previous
    attempt left something unresolved that a fresh read could settle. An
    unchanged document whose verification already ended in a terminal state is
    returned as-is: repeating the identical request cannot change the answer,
    which is exactly what the old two-blind-passes recheck did every time.
    """
    try:
        sha = file_hash(mark_scheme_path)
    except OSError as exc:
        return False, f"mark scheme unreadable: {exc}", STATUS_NOT_VERIFIED
    record = load_record(sha) or {}
    state = verification_status(record)
    blueprint = record.get("blueprint") or {}
    # The identity the verdict was established against, not merely the identity
    # of the last write: an edit that keeps the file identical still invalidates
    # a verification it was never part of.
    fingerprint = record.get("verified_fingerprint") or record.get("fingerprint")
    current = document_fingerprint(sha, blueprint)

    if not blueprint:
        return True, "this mark scheme has no stored structure yet", state
    if fingerprint is None:
        # A record written before verification state existed: the structure is
        # there, but nothing has ever checked it page by page.
        return True, "this mark scheme has no stored verification yet", state
    if fingerprint != current:
        return True, "the stored structure changed since it was last verified", state
    if state == STATUS_VERIFIED:
        return False, ("the structure verified cleanly and the document is "
                       "unchanged"), state
    if state == STATUS_NEEDS_MANUAL_REVIEW:
        return False, ("a verification already ended in NEEDS_MANUAL_REVIEW "
                       "for this unchanged document; re-running it cannot "
                       "change the outcome — a person must decide"), state
    if state in (STATUS_VERIFYING, STATUS_REPAIRING, STATUS_REVERIFYING):
        return False, "a verification of this mark scheme is already running", state
    return True, "the previous verification did not finish cleanly", state


def _inventory_is_authoritative(path: str | Path) -> bool:
    """Is there already a page-scoped completeness verdict for this document?"""
    try:
        sha = file_hash(path)
    except OSError:
        return False
    record = load_record(sha) or {}
    inventory = record.get("inventory") or {}
    return bool(inventory) and inventory.get("expected_count") is not None


def verify_with_inventory(client, mark_scheme_path: str | Path, *,
                          status_callback=None, dpi: int | None = None,
                          window: int | None = None,
                          overlap: int | None = None) -> dict:
    """Read the mark scheme PAGE BY PAGE and persist the completeness verdict.

    This is the expensive, authoritative check: every page is read by at least
    one bounded request, each item must carry the index the document prints,
    and the merge reports gaps, failures and disagreements instead of a tidy
    count. It replaces the structure only when the page-scoped read is at least
    as complete as what is stored.
    """
    from . import scheme_inventory as si

    path = Path(mark_scheme_path)
    sha = file_hash(path)
    record = load_record(sha) or {}
    previous = load_cached(sha)
    if not _begin_recheck(sha):
        return {"ok": False, "busy": True,
                "error": "a verification of this mark scheme is already running",
                "validation": structure_report(previous), "previous": structure_report(previous)}
    try:
        save_record(sha, previous or {}, model=record.get("model") or "",
                    verification={"state": STATUS_VERIFYING,
                                  "updated_at": time.time(),
                                  "attempts": int(((record.get("verification") or {}).get("attempts") or 0)) + 1})
        if status_callback:
            status_callback("Reading the mark scheme page by page…")
        # Deterministic sampling for the page-scoped read as well: this read is
        # AUTHORITATIVE (it can replace the stored structure), and at the
        # provider's default temperature the same scheme was read differently
        # by every request. Eight simultaneous corrections of one paper then
        # produced four different totals (76, 76, 75, 76, 76, 72, 73, 76) even
        # though the marking call itself is reproducible. The extraction stage
        # already pins temperature 0 + seed; the verification read now matches
        # it, so two jobs reading the same scheme read the same pages.
        outcome = si.extract_inventory(
            client, path,
            window=int(window or si.CHUNK_WINDOW_PAGES),
            overlap=int(overlap if overlap is not None else si.CHUNK_OVERLAP_PAGES),
            dpi=int(dpi or si.PAGE_DPI),
            config_extra=dict(legacy.DETERMINISTIC_SAMPLING),
            on_chunk=(lambda chunk_result: status_callback(_chunk_message(chunk_result))
                      if status_callback else None))
    except Exception as exc:  # noqa: BLE001 — nothing is destroyed by a failure
        save_record(sha, previous or {}, model=record.get("model") or "",
                    verification={"state": STATUS_NEEDS_MANUAL_REVIEW,
                                  "updated_at": time.time(),
                                  "error": str(exc)[:300]})
        return {"ok": False, "error": f"page-scoped verification failed: {exc}",
                "validation": structure_report(previous), "previous": structure_report(previous)}
    finally:
        _end_recheck(sha)

    report = outcome["report"]
    questions = _questions_from_inventory(outcome["items"])
    candidate = {"questions": questions} if questions else None
    validation = structure_report(candidate or previous)

    # Which structure wins is decided by completeness first, then by marks:
    # a stored structure that contains a question the page-scoped read did not
    # see is NOT thrown away (the read may have missed it), and vice versa.
    replaced = False
    if candidate and validate(candidate):
        previous_ids = {q["question_number"] for q in (previous or {}).get("questions", [])}
        missing_from_candidate = previous_ids - {q["question_number"] for q in questions}
        if not missing_from_candidate or report.get("unresolved"):
            candidate["total_marks"] = sum(int(q["max_marks"]) for q in questions)
            candidate["paper"] = (previous or {}).get("paper", "")
            replaced = True

    completeness = {
        "detected_count": report["detected_count"],
        "expected_count": report["expected_count"],
        "expected_basis": report["expected_basis"],
        "observed_last_index": report.get("observed_last_index"),
        "missing_indices": report["missing_indices"],
        "conflicting_indices": report["conflicting_indices"],
        "label_variant_indices": report.get("label_variant_indices") or [],
        "part_label_variant_indices": report.get("part_label_variant_indices") or [],
        "failed_chunks": report["failed_chunks"],
        "pages_without_items": report["pages_without_items"],
        "unkeyed_items": report["unkeyed_items"],
        "marks_missing_indices": report["marks_missing_indices"],
        "items_total_marks": report["items_total_marks"],
        "unresolved": report["unresolved"],
        "verdict": report["verdict"],
        "sha256": outcome["sha256"],
        "checked_at": time.time(),
    }
    state = STATUS_VERIFIED if not report["unresolved"] else STATUS_NEEDS_MANUAL_REVIEW
    anomalies = _msv_anomalies(validation, outcome)
    # The per-item evidence is persisted with the report so the review viewer
    # can map a question to its source page (and back) without another read.
    inventory_payload = dict(report)
    inventory_payload["items"] = outcome["items"]
    inventory_payload["page_count"] = outcome["page_count"]
    # BOTH readings of a disputed index are kept with the report: "flagged for
    # review" is only honest if the reviewer can actually see the disagreement.
    inventory_payload["conflicts"] = (outcome.get("merge") or {}).get("conflicts") or []
    inventory_payload["label_variants"] = (
        outcome.get("merge") or {}).get("label_variants") or []
    save_record(sha, candidate if replaced else (previous or {}),
                model=record.get("model") or "",
                inventory=inventory_payload, completeness=completeness,
                reconciliation=reconcile_marks(
                    (candidate if replaced else previous) or {}, report),
                anomalies=anomalies, evidence_kind=_msv.EVIDENCE_VISUAL,
                verification={
                    "state": state,
                    "updated_at": time.time(),
                    "verified": state == STATUS_VERIFIED,
                    "anomalies": len(anomalies),
                    "attempts": int(((record.get("verification") or {}).get("attempts") or 0)) + 1,
                    "replaced_structure": replaced,
                })
    log.info("mark-scheme inventory verification: %d/%s questions, %d marks, "
             "state=%s (%s)",
             completeness["detected_count"], completeness["expected_count"],
             completeness["items_total_marks"], state, completeness["verdict"])
    return {
        "ok": True,
        "state": state,
        "replaced": replaced,
        "previous": structure_report(previous),
        "validation": validation,
        "inventory": inventory_payload,
        "completeness": completeness,
        "warnings": validation_warnings(validation),
        "disagreement": bool(report["conflicting_indices"]),
        "passes": [structure_report(previous)] + ([validation] if replaced else []),
    }


def _chunk_message(chunk_result: dict) -> str:
    """A one-line progress message from a chunk result (no secrets)."""
    chunk = chunk_result.get("chunk") or {}
    return (f"Read pages {chunk.get('pages')}: "
            f"{len(chunk_result.get('items') or [])} question(s)"
            + (f" — {chunk_result['error']}" if chunk_result.get("error") else ""))


def _questions_from_inventory(items: list[dict]) -> list[dict]:
    """Blueprint rows built from the page-scoped inventory (printed index kept)."""
    rows: list[dict] = []
    for item in items or []:
        label = str(item.get("label") or "").strip()
        index = item.get("source_index")
        row: dict[str, Any] = {
            # The document's printed INDEX is the question's number: it is the
            # identity the document itself uses, and — unlike a descriptive
            # header ("Jan 12/3H/Q6") — it is what sub-part nesting, marking and
            # the question list can be keyed on. The descriptive label travels
            # alongside it, so nothing is lost from the source.
            "question_number": str(index) if index is not None else label,
            "max_marks": int(item.get("max_marks") or 0),
            "criteria": str(item.get("criteria") or ""),
        }
        if label and label != row["question_number"]:
            row["printed_label"] = label
        if index is not None:
            row["source_index"] = int(index)
        if item.get("page"):
            row["page"] = int(item["page"])
        parts = item.get("parts") or []
        if parts:
            row["parts"] = [
                {"number": str(p.get("number") or ""),
                 "max_marks": int(p.get("max_marks") or 0),
                 **(({"criteria": str(p["criteria"])}) if p.get("criteria") else {}),
                 **(({"page": int(p["page"])}) if p.get("page") else {})}
                for p in parts]
        rows.append(row)
    return rows


def _msv_anomalies(validation: dict, outcome: dict) -> list[dict]:
    """The structured anomalies a page-scoped verification established."""
    from . import mark_scheme_verify as _v
    combined = dict(validation or {})
    combined.setdefault("checks", {})["inventory"] = outcome.get("report") or {}
    return _v.anomaly_records(combined)


def reconcile_marks(blueprint: dict | None, inventory: dict | None = None) -> dict:
    """Reconcile the mark total at question and document level.

    What it refuses to do is double-count: a parent's total is only summed from
    its own sub-parts when the scheme actually declares them, each printed index
    is counted once, and marks that could not be read are reported instead of
    being treated as zero. ``unresolved`` is true whenever the total cannot be
    established from the source, and the caller must show that rather than a
    confident number.
    """
    questions = [q for q in ((blueprint or {}).get("questions") or [])
                 if isinstance(q, dict)]
    per_question: list[dict] = []
    seen_indices: dict[str, int] = {}
    documents_total = 0
    duplicates: list[str] = []
    unread: list[str] = []
    for question in questions:
        number = str(question.get("question_number") or "").strip()
        parts = question.get("parts") or []
        if parts:
            total = sum(int(p.get("max_marks") or 0) for p in parts if isinstance(p, dict))
        else:
            total = int(question.get("max_marks") or 0)
        if total <= 0:
            unread.append(number)
        key = str(question.get("source_index") if question.get("source_index") is not None
                  else number)
        if key in seen_indices:
            duplicates.append(key)
        seen_indices[key] = seen_indices.get(key, 0) + total
        documents_total += total
        per_question.append({"question_number": number, "total": total,
                             "summed_from_parts": bool(parts),
                             "parts": len(parts)})
    inventory = inventory or {}
    source_total = inventory.get("items_total_marks")
    unresolved = bool(unread or duplicates)
    if source_total is not None and int(source_total) != documents_total:
        unresolved = True
    return {
        "per_question": per_question,
        "document_total": documents_total,
        "inventory_total": source_total,
        "duplicate_keys": duplicates,
        "unread_marks": unread,
        "unresolved": unresolved,
        "basis": ("summed once per declared index; a question with parts is "
                  "summed from its parts, never added to them"),
    }


def recheck_blueprint(client, mark_scheme_path: str | Path, *, passes: int = 1,
                      status_callback=None, force: bool = False) -> dict:
    """Re-verify a mark scheme — but only when that can actually change anything.

    Order of work:

    1. the fingerprint gate: an unchanged document whose verification already
       ended cleanly (or already asked for a human) is returned as-is, with no
       API request at all;
    2. one bounded extraction pass — not two identical ones;
    3. the page-scoped inventory pass, which is what can establish whether the
       question list is complete (and therefore which is the authority here);
    4. the outcome is persisted with a state, so a second click cannot repeat a
       verification that already concluded.
    """
    previous, previous_meta = blueprint_for_mark_scheme(mark_scheme_path)
    sha = file_hash(mark_scheme_path)
    run, reason, state = recheck_decision(mark_scheme_path)
    if not run:
        log.info("mark-scheme recheck skipped (%s): %s", state, reason)
        return {
            "ok": True,
            "skipped": True,
            "reason": reason,
            "state": state,
            "blueprint": previous,
            "previous": structure_report(previous),
            "validation": structure_report(previous),
            "warnings": [],
            "passes": [],
            "disagreement": False,
            "meta": previous_meta,
        }

    if not _begin_recheck(sha):
        log.info("mark-scheme recheck refused: already running")
        return {
            "ok": False,
            "busy": True,
            "error": "a verification of this mark scheme is already running",
            "previous": structure_report(previous),
            "validation": structure_report(previous),
            "passes": [],
            "disagreement": False,
        }

    results: list[dict] = []
    metas: list[dict] = []
    warnings: list[str] = []
    try:
        # ``passes`` counts passes for an EXPLICIT comparison request (the
        # caller asked for the disagreement between two independent reads).
        # The number of passes is a property of the request, never a retry
        # loop: the default is one pass, so the click that used to cost two
        # identical extractions now costs one.
        for index in range(max(1, min(int(passes or 1), 4))):
            if status_callback:
                status_callback(f"Rechecking mark scheme (pass {index + 1})…")
            bp, meta = get_blueprint(client, mark_scheme_path, force=True,
                                     status_callback=status_callback)
            metas.append(meta)
            if bp:
                results.append(bp)
    finally:
        _end_recheck(sha)

    if not results:
        save_record(sha, previous or {}, model=previous_meta.get("model") or "",
                    verification={"state": STATUS_NEEDS_MANUAL_REVIEW,
                                  "updated_at": time.time(),
                                  "error": (metas[-1].get("error") if metas else "recheck failed")})
        return {
            "ok": False,
            "error": (metas[-1].get("error") if metas else "recheck failed"),
            "previous": structure_report(previous),
            "validation": structure_report(previous),
            "passes": [],
            "disagreement": False,
        }

    results.sort(key=lambda b: (len(b.get("questions", [])),
                                sum(int(q["max_marks"]) for q in b["questions"])),
                 reverse=True)
    best = results[0]
    reports = [structure_report(bp) for bp in results]
    counts = {r["question_count"] for r in reports}
    totals = {r["total_marks"] for r in reports}
    passes_disagreed = len(counts) > 1 or len(totals) > 1
    validation = structure_report(best)
    warnings.extend(validation_warnings(validation))
    if passes_disagreed:
        warnings.insert(0, "Independent extraction passes disagreed: "
                        + ", ".join(f"{r['question_count']} questions / "
                                    f"{r['total_marks']} marks" for r in reports)
                        + ". The most complete pass was kept.")

    # ---- the completeness pass, when it is the authority -------------------
    # It runs when the stored structure is not already backed by an inventory, or
    # when the extraction reported something it could not settle itself. That is
    # the whole point of chunking: pay for the page-scoped read when it can
    # actually answer the question, not on every click.
    inventory_result: dict | None = None
    needs_completeness = (not _inventory_is_authoritative(mark_scheme_path)
                          or bool(validation.get("gaps"))
                          or validation.get("complete") is False)
    if needs_completeness:
        inventory_result = verify_with_inventory(
            client, mark_scheme_path, status_callback=status_callback)
        if inventory_result.get("ok"):
            best = (load_cached(sha) or best)
            validation = structure_report(best)
        elif inventory_result.get("error"):
            warnings.append(str(inventory_result["error"]))

    record = load_record(sha) or {}
    completeness = record.get("completeness") or {}
    state = verification_status(record)
    inventory_conflict = bool((inventory_result or {}).get("disagreement"))
    if inventory_conflict:
        warnings.insert(0, "Overlapping page reads disagreed about at least one "
                          "question index; both readings were kept for review.")
    return {
        "ok": True,
        "blueprint": best,
        "previous": structure_report(previous),
        "validation": validation,
        "warnings": warnings,
        "passes": reports,
        "disagreement": bool(passes_disagreed or inventory_conflict),
        "state": state,
        "inventory": record.get("inventory") or {},
        "completeness": completeness,
        "meta": metas[-1] if metas else {},
    }
