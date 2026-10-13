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
import time
from pathlib import Path
from typing import Any

from ..config import get_config
from . import legacy_marking as legacy

log = logging.getLogger("engine.blueprint")

# Statuses attached to every question after reconciliation.
STATUS_VALID = "VALID"      # marked normally, backed by the mark scheme
STATUS_REVIEW = "REVIEW"    # needs a human look (gap/extra/ambiguity)

#: States that must NEVER be laundered into a clean VALID row by a later
#: reconciliation pass (see ``apply_blueprint``): the missing-result placeholder
#: an earlier pass inserted, a split-question flag, an explicit unmarked row.
UNRESOLVED_STATUSES = (STATUS_REVIEW, "NEEDS_REVIEW", "UNMARKED")

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
    """Render the blueprint as an instruction block for the marking prompt."""
    lines = [PROMPT_HEADER, "total_marks: %s" % blueprint.get("total_marks", "?")]
    lines.append("questions:")
    for q in blueprint.get("questions", []):
        lines.append(
            '  - question_number=%s max_marks=%s criteria=%s'
            % (q.get("question_number"), q.get("max_marks"),
               (q.get("criteria") or "").replace("\n", " ")[:400])
        )
    lines.append("")
    lines.append(
        "Rules:\n"
        "- Return EXACTLY one entry per blueprint question, using the SAME\n"
        "  question_number string and the SAME max_marks value shown above.\n"
        "- Do not invent, split, merge, rename or omit questions, even if the\n"
        "  student left one blank — blank work is still an entry with\n"
        "  awarded_marks 0.\n"
        "- Award marks ONLY against the criteria above and the mark scheme;\n"
        "  never against your own sense of what looks reasonable.\n"
        "- Independently verify the answer. If the mark scheme's reference\n"
        "  answer is demonstrably wrong or incomplete, award the marks the\n"
        "  mathematics justifies and record the disagreement in scheme_check;\n"
        "  never award zero merely because a correct answer differs from a\n"
        "  broken reference answer. Preserve every existing M/A/B rule.\n"
        "- Use the mark_breakdown codes (M/A/B...) from the criteria as the\n"
        "  evidence for each awarded or withheld mark.\n"
    )
    return "\n".join(lines)


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
    payload = {
        "sha256": sha,
        "created_at": time.time(),
        "model": model,
        "blueprint": blueprint,
    }
    try:
        _cache_path(sha).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # caching is best-effort; never fail the job
        log.warning("could not cache blueprint %s: %s", sha[:12], exc)


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
        number = str(q.get("question_number", "")).strip()
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
    numbers = [str(q.get("question_number")) for q in questions]
    seq = [n for n in (_leading_number(x) for x in numbers) if n is not None]
    unique = sorted(set(seq))
    gaps: list[str] = []
    if unique:
        present = set(unique)
        gaps = [str(n) for n in range(unique[0], unique[-1] + 1) if n not in present]
    duplicate_numbers = sorted({n for n in numbers if numbers.count(n) > 1})
    missing_marks = [str(q.get("question_number")) for q in questions
                     if int(q.get("max_marks", 0) or 0) <= 0]
    return {
        "question_count": len(questions),
        "total_marks": sum(int(q.get("max_marks", 0) or 0) for q in questions),
        "numbering": numbers,
        "first": unique[0] if unique else None,
        "last": unique[-1] if unique else None,
        "gaps": gaps,
        "duplicate_numbers": duplicate_numbers,
        "missing_max_marks": missing_marks,
        "has_gaps": bool(gaps),
        "complete": bool(questions) and not gaps and not duplicate_numbers \
                    and not missing_marks,
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
    if report.get("missing_max_marks"):
        reasons.append("Questions with no mark allocation: "
                       + ", ".join(report["missing_max_marks"]) + ".")
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
    if report.get("missing_max_marks"):
        warnings.append("Questions with no mark allocation: "
                        + ", ".join(report["missing_max_marks"]) + ".")
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

    cached = load_cached(sha)
    if cached and not force:
        meta["source"] = "cache"
        meta["total_marks"] = sum(int(q["max_marks"]) for q in cached["questions"])
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
    validation = _msv.cross_validate(structure, evidence)
    meta["validation"] = {
        "verified": validation["verified"],
        "structural_confidence": validation["structural_confidence"],
        "anomalies": validation["anomalies"],
        "checks": validation["checks"],
        "extraction_mode": validation["extraction_mode"],
    }
    action, detail = _msv.prefer(cached, structure, None, validation)
    meta["structure_decision"] = {"action": action, **detail}

    if action == "keep_existing" and cached:
        meta["source"] = "cache"
        meta["total_marks"] = sum(int(q["max_marks"]) for q in cached["questions"])
        meta["note"] = detail.get("reason", "existing verified structure preserved")
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

    save_cached(sha, blueprint, model=model_name)
    meta.update(source="built", total_marks=total, model=model_name)
    log.info("mark-scheme verification: %d questions / %d marks (confidence %.2f, "
             "%d anomaly/anomalies, evidence=%s)",
             len(questions), total, validation["structural_confidence"],
             len(validation["anomalies"]), validation["extraction_mode"])
    return blueprint, meta


def _clean_questions(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
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
        cleaned.append({
            "question_number": number,
            "max_marks": max_marks,
            "criteria": str(item.get("criteria", "")).strip(),
        })
    return cleaned


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
    from .batching import _question_sort_key
    canonical_ids: dict[str, str] = {}
    for bpn in bp_by_number:
        canonical_ids[canonical_question_id(bpn)] = bpn

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
            can = canonical_question_id(number)
            parent = can
            while parent and parent not in canonical_ids and "a" <= parent[-1:] <= "z":
                parent = parent[:-1]
            target = canonical_ids.get(parent) or canonical_ids.get(can)
        if target is None:
            extras.append(q)
        else:
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
                "loss_reason": "",
                "mark_breakdown": [],
                "correct_method": "",
                "full_mark_solution": "",
                "status": STATUS_REVIEW,
                "review_reason": (
                    "The marker did not return an answer for this question. "
                    "It is UNMARKED — do not treat this 0 as a confirmed "
                    "score; review it manually."
                ),
            })
            report["missing_questions"].append(number)
            continue

        if len(parts) > 1:
            report["merged_questions"].append(number)

        awarded = sum(int(p.get("awarded_marks", 0)) for p in parts)
        awarded = max(0, min(awarded, max_marks))

        old_max = parts[0].get("max_marks")
        if len(parts) == 1 and int(old_max or max_marks) != max_marks:
            report["max_marks_corrected"].append(
                {"question": number, "model": int(old_max or 0),
                 "mark_scheme": max_marks})

        primary = parts[0]
        primary["question_number"] = number   # blueprint numbering is official
        primary["max_marks"] = max_marks
        primary["awarded_marks"] = awarded
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
    return cached, {
        "source": "cache",
        "sha256": sha,
        "manual_override": bool(cached.get("manual_override")),
        "total_marks": sum(int(q["max_marks"]) for q in cached["questions"]),
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
        number = str(item.get("question_number", "")).strip()
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


def recheck_blueprint(client, mark_scheme_path: str | Path, *, passes: int = 2,
                      status_callback=None) -> dict:
    """REPROCESS a mark scheme and compare independent extraction passes.

    This is not a re-count of the cached list: each pass re-extracts the
    structure from the PDF. When passes disagree on the number of questions or
    the total marks, the disagreement is reported so an incomplete extraction
    is visible. The most complete pass (most questions, then most marks) is
    saved, and the previous structure is reported for comparison.
    """
    previous, previous_meta = blueprint_for_mark_scheme(mark_scheme_path)
    passes = max(1, min(int(passes or 2), 4))
    results: list[dict] = []
    metas: list[dict] = []
    for index in range(passes):
        if status_callback:
            status_callback(f"Rechecking mark scheme (pass {index + 1}/{passes})…")
        bp, meta = get_blueprint(client, mark_scheme_path, force=True,
                                 status_callback=status_callback)
        metas.append(meta)
        if bp:
            results.append(bp)
    if not results:
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
    disagreement = len(counts) > 1 or len(totals) > 1
    validation = structure_report(best)
    warnings = validation_warnings(validation)
    if disagreement:
        warnings.insert(0, "Independent extraction passes disagreed: "
                        + ", ".join(f"{r['question_count']} questions / "
                                    f"{r['total_marks']} marks" for r in reports)
                        + ". The most complete pass was kept.")
    return {
        "ok": True,
        "blueprint": best,
        "previous": structure_report(previous),
        "validation": validation,
        "warnings": warnings,
        "passes": reports,
        "disagreement": disagreement,
        "meta": metas[-1] if metas else {},
    }
