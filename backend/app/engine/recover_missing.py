"""TARGETED RECOVERY for missing question results.

Guards against the same failure from the last two bugs: questions the mark
scheme requires (e.g. 62-69) came back UNMARKED — never silently as 0s — and
children continued on later pages were only partially assessed.

Rules (all deterministic, never alter an existing mark):

* ONLY the missing questions are retried — successful work is never re-run,
  and the narrow prompt makes the retry's API cost proportional to the gap.
* Bounded attempts (default 2 recovery rounds), persisted on the runner's
  store / the pool's job row, so a restart never triggers repeated AI calls
  or repeats rounds that already ran.
* A round that persists but still leaves questions unresolved escalates to
  the existing teacher-review path (never finalization).
* A row promoted back by a recovery rerun NEVER overwrites an existing valid
  row, and the recovered set still gets the full verification re-run.

Recovery is generic: every production path (the worker pool /
CorrectionService) drives the same ``run_recovery_round`` through the same
``legacy_marking.grade_with_model`` engine.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from . import legacy_marking as legacy
from . import question_inventory as qi

log = logging.getLogger("engine.recover_missing")

# A single recovery round re-marks AT MOST this many questions per request.
# Recovery is a gap-filler, not a re-mark: each request is bounded so the
# retry cannot degenerate into re-marking the whole paper.
MAXMISS_IN_PROMPT = 6

#: How many bounded retry requests a CHUNKED recovery may spend on one job.
#: 8 requests x 6 questions covers a 48-question gap — more than the largest
#: genuine marker omission seen in production (32 questions), while keeping
#: the worst-case API cost of a retry strictly bounded.
DEFAULT_MAX_CHUNKS = 8

#: A gap covering this share of the paper is not a marker omission: the run
#: (or the mark-scheme structure) is broken, and re-marking it question by
#: question would just be a disguised full re-mark. Such a gap is refused and
#: the questions stay NEEDS_REVIEW for the teacher.
WHOLE_PAPER_GAP_SHARE = 0.9


@dataclass
class RecoveryOutcome:
    ok: bool
    recovered: dict[str, dict]
    still_missing: list[str]
    error: str | None = None


@dataclass
class RecoveryChunk:
    """One bounded retry request: the questions it re-marks, in order."""
    index: int
    questions: list[str]


@dataclass
class RecoveryPlan:
    """A bounded, multi-request plan for ONE gap.

    ``chunks`` are the retry requests that may run now; ``deferred`` are the
    questions the budget could not cover (they keep their explicit unresolved
    state); ``blocked_reason`` explains why NOTHING may run.
    """
    chunks: list[RecoveryChunk]
    deferred: list[str]
    blocked_reason: str | None = None

    @property
    def questions(self) -> list[str]:
        return [q for chunk in self.chunks for q in chunk.questions]


def build_narrow_blueprint(mark_scheme_path: str | None,
                           missing: list[str],
                           *, blueprint: dict | None = None) -> dict | None:
    """Blueprint for ONLY the missing questions (no AI extraction call).

    ``blueprint`` is the reconciled structure the caller already holds. Passing
    it removes the dependency on a cached mark-scheme FILE, which is what made
    the retry impossible for a scheme that arrived from Classroom/Drive: the
    omitted question kept its placeholder 0 instead of being re-marked.
    """
    if not missing:
        return None
    if blueprint is None and not mark_scheme_path:
        return None
    return qi.narrow_blueprint(mark_scheme_path, missing[:MAXMISS_IN_PROMPT],
                               blueprint=blueprint)


def run_recovery_round(*, client, missing: list[str],
                       student_remote, mark_scheme_remote,
                       question_remote=None,
                       mark_scheme_path: str | None = None,
                       blueprint: dict | None = None,
                       context_note: str = "") -> RecoveryOutcome:
    """ONE recovery pass: re-mark ONLY the still-missing questions.

    Uses the exact same marking engine (``grade_with_model``), but with a
    blueprint restricted to the missing questions so the model's job (and
    the response size) is bounded by the actual gap. The merged result is
    reconciled back through ``apply_blueprint`` against the FULL prompt
    blueprint by the caller — this function returns raw rows only.

    ``still_missing`` lists questions that remain unaccounted for, in the
    order they should be retried next round (a partial retry's progress is
    never lost).

    ``blueprint`` is the caller's reconciled in-memory structure; passing it
    lets the retry build its narrow prompt without a cached mark-scheme file
    (see :func:`build_narrow_blueprint`).
    """
    still_missing = list(missing)
    recovered: dict[str, dict] = {}
    if not missing:
        return RecoveryOutcome(ok=True, recovered=recovered,
                               still_missing=still_missing)

    narrow = build_narrow_blueprint(mark_scheme_path, missing,
                                    blueprint=blueprint)
    if narrow is None:
        # No reliable narrower (e.g. no cached blueprint for this mark
        # scheme). Bounded escalation: leave the gap UNRESOLVED rather than
        # re-running the whole paper (which previously produced the very
        # missing-question bug this guards against).
        return RecoveryOutcome(
            ok=False, recovered=recovered, still_missing=still_missing,
            error=("No reliable mark-scheme structure is available for a "
                   "targeted retry; the questions stay unmarked (not 0) "
                   "pending teacher review."))

    ids = {qi.canonical_question_id(str(q["question_number"]))
            for q in narrow["questions"]}
    try:
        raw, _model = legacy.grade_with_model(
            client=client, model_names=list(_primary_models()),
            student_file=student_remote,
            mark_scheme_file=mark_scheme_remote,
            question_file=question_remote,
            blueprint=narrow,
        )
        rerun = legacy.normalise_result(raw)
    except Exception as exc:  # noqa: BLE001 — never lose the whole job here
        return RecoveryOutcome(
            ok=False, recovered=recovered, still_missing=still_missing,
            error=str(exc)[:500])

    # Map the rerun's rows onto the canonical missing ids.
    existing = {qi.canonical_question_id(i): i for i in missing}
    for q in rerun.get("questions") or []:
        canonical = qi.canonical_question_id(str(q.get("question_number", "")))
        # Accept the result when it targets one of the missing questions
        # exactly (canonical match), so an off-by-one model row can never
        # silently replace the wrong question's placeholder.
        if canonical in existing:
            row = {
                "question_number": existing[canonical],
                **{k: v for k, v in q.items() if k != "question_number"},
            }
            recovered[canonical] = row

    still_missing = [i for i in missing
                     if qi.canonical_question_id(i) not in recovered]
    _ = ids, context_note
    return RecoveryOutcome(ok=not still_missing, recovered=recovered,
                           still_missing=still_missing)


def _primary_models() -> list[str]:
    from ..config import get_config
    return list(get_config().gemini.primary_models)


# ------------------------------------------------------------- persistence

def as_json(outcome: RecoveryOutcome) -> str:
    """Serialise outcome for a persisted ``recovery_json``-style column."""
    return json.dumps({
        "recovered_questions": sorted(outcome.recovered),
        "still_missing": list(outcome.still_missing),
        "error": outcome.error,
    }, ensure_ascii=False)


def from_json(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def split_missing(missing: list[str], per_prompt: int = MAXMISS_IN_PROMPT
                  ) -> list[list[str]]:
    """Chunk the missing list across recovery rounds (bounded per attempt)."""
    return [missing[i:i + per_prompt]
            for i in range(0, len(missing), per_prompt)]


def plan_chunked_recovery(*, missing: list[str],
                          chunk_size: int = MAXMISS_IN_PROMPT,
                          max_chunks: int = DEFAULT_MAX_CHUNKS,
                          already_attempted: int = 0,
                          scheme_questions: int | None = None,
                          ) -> RecoveryPlan:
    """Turn a gap of ANY size into bounded retry requests.

    This is the large-document path's recovery planner. A real 49-page
    submission came back with 32 mark-scheme questions (128 marks) unmarked
    because one batch answered a fraction of its pages, and the single-round
    gate used to refuse anything bigger than 12 questions — so the whole gap
    was left unresolved for no better reason than its size. The gap is now
    split into requests of ``chunk_size`` questions (the SAME narrow-prompt
    retry, never a full re-mark) and as many as ``max_chunks`` of them may
    run, with the remainder explicitly deferred.

    Still refused, deliberately:
    * a structure-less scheme (nothing to build the narrow prompt from) —
      reported by the per-chunk outcome, never by burning requests;
    * the retry budget already spent (``already_attempted``);
    * a gap covering ~the whole paper, which is a failed run or a broken
      mark-scheme extraction rather than a marker omission.
    """
    ordered = [str(q) for q in missing if str(q).strip()]
    if not ordered:
        return RecoveryPlan(chunks=[], deferred=[])
    if scheme_questions and scheme_questions > 0 and \
            len(ordered) >= max(2, int(scheme_questions * WHOLE_PAPER_GAP_SHARE)):
        return RecoveryPlan(
            chunks=[], deferred=list(ordered),
            blocked_reason=(
                f"{len(ordered)} of the paper's {scheme_questions} questions "
                f"were returned UNMARKED — that is essentially the whole paper, "
                f"so this is a failed run or an incomplete mark-scheme "
                f"extraction rather than a marker omission. A targeted retry "
                f"cannot fix it; re-run the submission or review the mark "
                f"scheme."))
    remaining = max(0, int(max_chunks) - max(0, int(already_attempted)))
    if remaining <= 0:
        return RecoveryPlan(
            chunks=[], deferred=list(ordered),
            blocked_reason=(
                f"The targeted retry already used its budget of {max_chunks} "
                f"request(s); the remaining question(s) stay unmarked (not 0) "
                f"pending manual marking."))
    size = max(1, int(chunk_size))
    pieces = split_missing(ordered, per_prompt=size)
    chunks = [RecoveryChunk(index=i, questions=piece)
              for i, piece in enumerate(pieces[:remaining])]
    deferred = [q for piece in pieces[remaining:] for q in piece]
    return RecoveryPlan(chunks=chunks, deferred=deferred)


def unresolved_reason(*, chunks_run: int, recovered: int, total: int,
                      error: str | None = None,
                      blocked_reason: str | None = None) -> str:
    """The explicit reason a question still has NO mark after retrying.

    Every question that survives recovery must say what was tried and why it
    is unresolved, so the teacher never sees a bare 0 or a silent gap.
    """
    reason = (
        "The marker returned no result for this question, although it is "
        "required by the official mark scheme. It is UNMARKED (not a "
        "confirmed 0).")
    if chunks_run:
        reason += (f" A targeted retry re-marked {chunks_run} bounded "
                   f"chunk(s) covering {total} question(s) and recovered "
                   f"{recovered} of them")
        reason += ("; this question is still unresolved and needs manual "
                   "marking or a fresh run of the submission.")
    else:
        reason += ("; no automatic retry could be run for this gap — it needs "
                   "manual marking or a fresh run of the submission.")
    if blocked_reason:
        reason += f" Why no retry ran: {str(blocked_reason)[:300]}"
    if error:
        reason += f" (last retry error: {str(error)[:200]})"
    return reason


def need_recovery(*, missing: list[str],
                  already_attempted: int) -> tuple[bool, str | None]:
    """Decide whether another recovery round may run.

    The inventory guard-rails unconditionally refuse when the missing-list
    exceeds MAX_MISSING_QUESTIONS: a gap that big is an unusable mark-scheme
    structure (blueprint problem), not a marker omission, so retrying cannot
    fix it and the questions stay NEEDS_REVIEW for the teacher.
    """
    if not missing:
        return False, None
    if len(missing) > qi.MAX_MISSING_QUESTIONS:
        return False, (
            f"{len(missing)} questions were returned UNMARKED by the marker "
            f"— this looks like an incomplete mark-scheme extraction, not a "
            f"retryable gap. A targeted retry cannot fix it; review "
            f"the mark scheme and re-run manually if needed.")
    if already_attempted >= _max_rounds():
        return False, (
            f"A targeted retry already ran {already_attempted} round(s) and "
            f"the question(s) remain unresolved — further automatic retries "
            f"are stopped. Review the affected results manually.")
    return True, None


def _max_rounds() -> int:
    from ..config import get_config
    return max(1, int(get_config().worker.max_retries or 2))
