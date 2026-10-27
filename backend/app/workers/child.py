"""Worker child process bootstrap.

Run as:  python <backend>/app/workers/child.py      (a script, by path)
         python -m app.workers.child                (also works)

The orchestrator launches it by PATH on purpose: the packaged application runs
on CPython's embeddable distribution, whose ``python313._pth`` file makes
CPython ignore PYTHONPATH, so a ``-m`` module launch cannot find the ``app``
package there and the child dies before it reads its job.

Reads one JSON job descriptor from stdin, executes the trusted correction
engine, writes one JSON result to stdout. This avoids multiprocessing.spawn's
``__main__`` re-import entirely (which breaks under pytest and frozen apps)
while keeping full process isolation — the child has its own memory, its own
interpreter state, and only stdin/stdout as the IPC channel.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Make the backend package importable no matter the cwd or how this file was
# started: ``app`` lives one level ABOVE app/workers, so the backend root goes
# on sys.path (this is what ``from app.… import …`` below needs).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


class _UsageModels:
    """Record one generation round-trip per call (response exact, request
    overhead estimated). Accounting is best-effort and never breaks marking."""

    def __init__(self, inner, job_id=None):
        self._inner = inner
        self._job_id = job_id

    def generate_content(self, *args, **kwargs):
        from app.net_usage import GENERATION_REQUEST_OVERHEAD_BYTES, record
        response = self._inner.generate_content(*args, **kwargs)
        try:
            text = getattr(response, "text", "") or ""
            record("gemini_generate", GENERATION_REQUEST_OVERHEAD_BYTES, len(text))
            # ``ai_attempts`` is NOT incremented here: only successful calls
            # reach this line, so a paper that burned 12 failed attempts
            # reported 0 (measured on every stored job). gemini.call_audit
            # counts every attempt where it is sent, and its snapshot is what
            # the pool persists.
        except Exception:
            pass
        return response

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _UsageFiles:
    """Record exact uploaded file bytes (the dominant correction traffic)."""

    def __init__(self, inner):
        self._inner = inner

    def upload(self, *args, **kwargs):
        from app.net_usage import record
        path = kwargs.get("file") or (args[0] if args else None)
        remote = self._inner.upload(*args, **kwargs)
        try:
            size = os.path.getsize(path) if path else 0
            record("gemini_upload", size, 0)
        except Exception:
            pass
        return remote

    def get(self, *args, **kwargs):
        return self._inner.get(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._inner.delete(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _UsageRecordingClient:
    """Transparent wrapper: identical client surface, plus usage accounting.
    The trusted engine is unaware of it."""

    def __init__(self, inner, job_id=None):
        object.__setattr__(self, "_inner_client", inner)
        self.models = _UsageModels(inner.models, job_id)
        # KeyAwareClient records uploads itself (exact bytes, once). Wrapping
        # it again would double-count the dominant correction traffic.
        self.files = inner.files if getattr(inner, "records_usage", False) \
            else _UsageFiles(inner.files)

    def __getattr__(self, name):
        return getattr(self._inner_client, name)


def _emit(message: str | None = None, progress: float | None = None,
          stage: str | None = None) -> None:
    """Write one progress event to stdout (the result is the last line).

    The orchestrator reads these as they are produced and records them on the
    job row, so the desktop shows the stage the engine is really in rather than
    a single "starting" state for the whole run. Flushed immediately: a
    buffered event would arrive only after the work it describes.
    """
    event: dict = {"type": "stage"}
    if message:
        event["message"] = message
    if stage:
        event["stage"] = stage
    if progress is not None:
        event["progress"] = progress
    try:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — progress must never break a correction
        pass


def _run(job: dict) -> dict:
    started = time.time()
    # Every provider request this correction makes is counted here, by stage,
    # so "how much AI did this paper cost" is measured instead of guessed.
    from app.gemini import call_audit
    call_audit.begin(job.get("job_id"))
    from app.engine.legacy_marking import (  # trusted engine (verbatim)
        audit_result, calculate_totals, grade_with_model, make_group_rows,
        normalise_result, percentage_band_grade,
    )
    from app.engine import double_check
    from app.engine.blueprint import apply_blueprint, get_blueprint
    from app.engine import scheme_exclusions
    from app.engine import model_health_store as mhs
    from app.config import get_config

    # Restore shared model health + the pinned marking model so the model
    # chain does not restart from scratch in every worker process (otherwise
    # the model that marks a submission is a per-job lottery).
    mhs.load()

    submission_paths = job["submission_paths"]
    mark_scheme_path = job.get("mark_scheme_path")
    question_paper_path = job.get("question_paper_path")

    # How many page images this marking request will carry. The request
    # timeout grows with it (client_proxy.timeout_for_pages): a 41-page paper
    # needs minutes, and a 70-page booklet must not inherit a budget sized for
    # 15 pages. Counting is reading the document index, not the pages.
    from app.engine.pdf_inspect import page_images as _page_images

    request_pages = (sum(_page_images(p) for p in submission_paths)
                     + _page_images(mark_scheme_path)
                     + _page_images(question_paper_path))

    # Mock/demo mode travels with the job because this child is a separate
    # interpreter that re-reads config.toml (which does not record the
    # in-memory demo flag). Applying it here keeps blueprint extraction and
    # the client selection consistent with the orchestrator.
    if job.get("mock"):
        get_config().mock_mode = True

    if get_config().mock_mode:
        from app.gemini.mock import MockGeminiClient
        client = MockGeminiClient()
        inner_client = None
    else:
        # Multi-key rotation INSIDE the job: the pool leases one key for this
        # job (preferred_fp), but a mid-job 429/invalid key now rotates to
        # another healthy key instead of forcing the model-fallback chain onto
        # a different model (a major source of run-to-run mark differences).
        # Outcomes are recorded through the KeyManager, so the KEY_n stats and
        # cooldowns stay truthful across processes (WAL SQLite).
        from app.gemini.client_proxy import KeyAwareClient
        from app.gemini.key_manager import KeyManager
        inner_client = KeyAwareClient(
            KeyManager(), preferred_fp=job.get("leased_fingerprint"),
            job_id=job.get("job_id"), pages=request_pages)
        client = _UsageRecordingClient(inner_client, job.get("job_id"))

    def _upload(path, purpose: str = "submission"):
        if not path:
            return None
        # upload_file waits for the remote file to become ACTIVE (legacy
        # PROCESSING poll loop lives in the key-aware client) and reuses a
        # registered remote when one is still alive (§6/§9). Mark schemes are
        # SHARED: uploaded once, then reused by every sibling job.
        if inner_client is not None:
            return inner_client.upload_file(
                path, purpose=purpose, shared=(purpose == "mark_scheme"))
        return client.files.upload(file=path)

    _emit("Uploading the paper to the AI service", progress=0.15)
    with call_audit.stage(call_audit.STAGE_UPLOAD):
        remote_students = [_upload(p, "submission") for p in submission_paths]
        remote_scheme = _upload(mark_scheme_path, "mark_scheme")
        remote_question = _upload(question_paper_path, "question")
    _emit("Paper uploaded — reading the mark scheme", progress=0.2)

    try:
        # Mark-scheme blueprint: official question structure, extracted once
        # per mark-scheme file and cached — question numbering and maximum
        # marks therefore do not change between runs of the same mark scheme.
        blueprint, blueprint_meta = (None, {"source": "unavailable"})
        if mark_scheme_path:
            with call_audit.stage(call_audit.STAGE_MARK_SCHEME):
                blueprint, blueprint_meta = get_blueprint(
                    client, mark_scheme_path,
                    status_callback=lambda message: _emit(message))

        if blueprint:
            _emit(f"Mark scheme read: {len(blueprint.get('questions') or [])} "
                  "questions", progress=0.35)
        else:
            _emit("Mark scheme structure unavailable — marking from the PDFs",
                  progress=0.35)

        # Questions the teacher took OUT of this assignment never reach the
        # prompt, the reconciliation or the total. The mark-scheme PDF is
        # untouched; removing the exclusion restores the question.
        blueprint, excluded_questions = scheme_exclusions.filter_blueprint(
            blueprint, job.get("coursework_id"))
        if excluded_questions:
            names = ", ".join(str(q["question_number"]) for q in excluded_questions)
            _emit(f"Excluded from marking: {names}", progress=0.36)

        model_chain = mhs.ordered_models(job["primary_models"])
        with call_audit.stage(call_audit.STAGE_MARKING):
            raw, marking_model = grade_with_model(
                client=client,
                model_names=model_chain,
                student_file=remote_students,
                mark_scheme_file=remote_scheme,
                question_file=remote_question,
                # "Marking with <model>" while the single request runs; the pool
                # appends the elapsed time, so a long wait is visibly alive.
                status_callback=lambda message: _emit(message),
                blueprint=blueprint,
            )
        _emit(f"First correction received from {marking_model}", progress=0.7)
        mhs.record_success(marking_model, model_chain)
        mhs.save()
        result = normalise_result(raw)
        result, blueprint_report = apply_blueprint(result, blueprint)
        blueprint_report["source"] = blueprint_meta.get("source", "unavailable")
        if blueprint_meta.get("error"):
            blueprint_report["note"] = blueprint_meta["error"]
        # §9: marking against an INCOMPLETE or unreviewed structure must say so.
        # Grading is unchanged — the marks are still produced exactly as before —
        # but the result carries the verdict so it cannot look routine.
        blueprint_report["completeness"] = blueprint_meta.get("completeness") or {}
        blueprint_report["verification_state"] = blueprint_meta.get(
            "verification_state")
        review = blueprint_meta.get("review") or {}
        if review.get("warnings"):
            blueprint_report["review_required"] = True
            blueprint_report["review_warnings"] = list(review["warnings"])
            result["mark_scheme_review_required"] = True
            result["mark_scheme_warnings"] = list(review["warnings"])
            result.setdefault("warnings", [])
            if isinstance(result["warnings"], list):
                result["warnings"].extend(review["warnings"])

        # ---- explicit per-question states ----------------------------------
        # Missing mark-scheme questions become NEEDS_REVIEW (not a silent 0)
        # and evidence-backed zeros are labelled CONFIRMED_ZERO. The pool may
        # then recover the gap narrowly.
        from app.engine.question_inventory import (
            confirmed_zero_status, mark_missing_results,
        )
        mark_missing_results(result)
        for q in result.get("questions", []):
            if confirmed_zero_status(q) == "CONFIRMED_ZERO" and not q.get("status"):
                q["status"] = "CONFIRMED_ZERO"

        # ---- targeted recovery of the missing questions ---------------------
        # Before anything is persisted: the remote files are still alive here,
        # so re-marking ONLY the missing questions costs no re-upload and no
        # re-marking of the successful rows. Bounded by the configured retry
        # budget, and only for a gap small enough to be a marker omission (a
        # large gap means the mark-scheme blueprint itself is incomplete, and
        # the questions stay NEEDS_REVIEW for the teacher).
        #
        # The round REPEATS while it is still closing the gap. It used to run
        # exactly once: if that single retry came back without the question —
        # or could not build its narrow prompt at all — the placeholder
        # survived, and the same paper could then total 47 or 49 depending on
        # whether the model happened to emit the row that run. A transient
        # omission now heals instead of being persisted as a mark.
        recovery_record = None
        try:
            from app.engine import recover_missing
            import app.engine.question_inventory as qi
            missing_reported = list(blueprint_report.get("missing_questions") or [])
            missing_here = list(missing_reported)
            rounds_run = 0
            recovered_rows: dict = {}
            last_error = None
            still_missing = list(missing_here)
            while missing_here:
                may_retry, block_reason = recover_missing.need_recovery(
                    missing=missing_here, already_attempted=rounds_run)
                if not may_retry:
                    last_error = block_reason
                    break
                _emit("Checking questions the first pass missed")
                with call_audit.stage(call_audit.STAGE_RECOVERY):
                    outcome = recover_missing.run_recovery_round(
                        client=client, missing=missing_here,
                        student_remote=remote_students,
                        mark_scheme_remote=remote_scheme,
                        question_remote=remote_question,
                        mark_scheme_path=mark_scheme_path,
                        # The reconciled structure this run ALREADY holds, so the
                        # narrow prompt never depends on a cached mark-scheme
                        # FILE (a scheme that arrived from Classroom/Drive has
                        # none). Without it the retry was refused outright and
                        # the omitted question kept its placeholder 0 for good.
                        blueprint=blueprint,
                    )
                rounds_run += 1
                still_missing = list(outcome.still_missing)
                if outcome.error:
                    last_error = outcome.error
                recovered_rows.update(outcome.recovered)
                missing_here = list(still_missing)
                # A round that returns nothing is retried (the next iteration
                # is still inside the bounded budget): the live finding is that
                # the omission itself is flaky, so a second attempt is exactly
                # the cheap chance that closes the gap. ``need_recovery`` above
                # refuses once the configured budget is spent, which is what
                # keeps this loop finite.

            if rounds_run or last_error:
                recovery_record = {
                    "rounds": rounds_run,
                    "missing_reported": missing_reported,
                    "recovered": sorted(recovered_rows),
                    "still_missing": list(still_missing),
                    "error": last_error,
                }

            if recovered_rows:
                by_canon = {
                    qi.canonical_question_id(
                        str(q.get("question_number", ""))): q
                    for q in result.get("questions", [])
                }
                for canon, row in recovered_rows.items():
                    placeholder = by_canon.get(canon)
                    if placeholder is not None and \
                            str(placeholder.get("status", "")).upper() in (
                                "REVIEW", "NEEDS_REVIEW", "UNMARKED"):
                        placeholder.clear()
                        placeholder.update(row)
                result, blueprint_report = apply_blueprint(result, blueprint)
                blueprint_report["source"] = blueprint_meta.get(
                    "source", "unavailable")
                mark_missing_results(result)
                for q in result.get("questions", []):
                    if confirmed_zero_status(q) == "CONFIRMED_ZERO" \
                            and not q.get("status"):
                        q["status"] = "CONFIRMED_ZERO"

            if recovery_record is not None:
                from app.logging_setup import log_event as _log
                _log("workers.child", "targeted recovery finished",
                     run_id=job["job_id"],
                     recovered=recovery_record["recovered"],
                     still_missing=recovery_record["still_missing"],
                     error=recovery_record["error"])
        except Exception as recovery_exc:  # noqa: BLE001 — never lose the job
            recovery_record = {"rounds": 0, "error": str(recovery_exc)[:500]}
        if recovery_record is not None:
            blueprint_report["recovery"] = recovery_record

        audit_model = "Not run"
        audit_warning = None
        if job.get("audit_enabled"):
            try:
                with call_audit.stage(call_audit.STAGE_AUDIT):
                    audited, audit_model = audit_result(
                        client=client, result=result,
                        student_file=remote_students,
                        mark_scheme_file=remote_scheme,
                        model_names=job["primary_models"][:2],
                    )
                result = normalise_result(audited)
                result, blueprint_report = apply_blueprint(result, blueprint)
            except Exception as audit_error:
                audit_warning = str(audit_error)

        # Evidence consistency (deterministic, no API call): a mark that the
        # result itself says has no student answer behind it is withdrawn, and
        # contradictory full/partial awards are sent to review.  This runs
        # AFTER blueprint reconciliation, which normalises every question to
        # VALID, so the verdict survives to persistence.
        # Drop the excluded questions one last time: the audit and the
        # recovery round both re-apply the blueprint and could reintroduce a
        # row for a question that is deliberately not being marked.
        result = scheme_exclusions.drop_questions(result, excluded_questions)
        if excluded_questions:
            blueprint_report["excluded_questions"] = excluded_questions

        from app.engine.mark_integrity import enforce_evidence
        result, integrity_report = enforce_evidence(result)
        if integrity_report["changed"] or integrity_report["review_questions"]:
            blueprint_report["integrity"] = integrity_report
            blueprint_report["review_required"] = True
            blueprint_report["review_warnings"] = (
                list(blueprint_report.get("review_warnings") or [])
                + list(integrity_report["warnings"]))

        earned, available, percentage = calculate_totals(result)
        totals = {
            "earned": earned,
            "available": available,
            "percentage": percentage,
            "provisional_grade": percentage_band_grade(percentage),
        }

        # ---- High-score double-check (independent second correction) ------
        # Triggered by the FIRST correction's unrounded percentage only (§2).
        # The second pass re-runs the SAME trusted engine on the SAME original
        # submission + mark scheme — it never sees the first score, reasoning,
        # report or confidence, so it is a genuinely independent evaluation
        # (§3/§10). A distinct sampling seed prevents a deterministic replay
        # of the first pass. The remote files are still uploaded, so the
        # second pass costs generation requests only (§14: it never runs for
        # scores below the threshold).
        second_result = None
        second_marking_model = None
        dc_triggered = False
        dc_error = None
        if job.get("double_check_enabled", True) and \
                double_check.should_trigger(earned, available):
            dc_triggered = True
            _emit("Running an independent second correction (high score)",
                  progress=0.85)
            try:
                with call_audit.stage(call_audit.STAGE_DOUBLE_CHECK):
                    raw2, second_marking_model = grade_with_model(
                        client=client,
                        # A different model leads the second pass when one is
                        # available: at temperature 0 the same model tends to
                        # replay the first pass's marks, which would make the
                        # check a confirmation by construction (§4).
                        model_names=double_check.second_pass_models(
                            model_chain, marking_model),
                        student_file=remote_students,
                        mark_scheme_file=remote_scheme,
                        question_file=remote_question,
                        status_callback=None,
                        blueprint=blueprint,
                        sampling=double_check.SECOND_PASS_SAMPLING,
                    )
                second_result = normalise_result(raw2)
                second_result, _ = apply_blueprint(second_result, blueprint)
                second_result = scheme_exclusions.drop_questions(
                    second_result, excluded_questions)
                second_result, _integrity2 = enforce_evidence(second_result)
            except Exception as exc:  # noqa: BLE001 — a failed check must not
                # discard the valid first correction (§13).
                dc_error = str(exc)[:800]
                second_result = None
            from app.logging_setup import log_event as _log
            _log("workers.child", "double-check finished",
                 run_id=job["job_id"],
                 first=earned, available=available,
                 second=(calculate_totals(second_result)[0]
                         if second_result is not None else None),
                 status=("COMPLETE" if second_result is not None
                         else "FAILED"),
                 triggered=dc_triggered)

        groups = make_group_rows(result["questions"])

        _emit("Saving the result", progress=0.95)

        key_fp = inner_client.current_fingerprint() if inner_client else None
        if inner_client is not None:
            from app.logging_setup import log_event
            log_event("workers.child", "correction finished",
                      run_id=job["job_id"], model=marking_model,
                      fingerprint=key_fp,
                      blueprint_source=blueprint_report.get("source"),
                      earned=earned, available=available,
                      processing_s=round(time.time() - started, 1))

        return {
            "job_id": job["job_id"], "worker_id": job["worker_id"],
            "status": "COMPLETED", "ok": True, "result": result,
            "totals": totals, "groups": groups,
            "marking_model": marking_model, "audit_model": audit_model,
            "audit_warning": audit_warning,
            "blueprint_report": blueprint_report,
            "key_fingerprint": key_fp,
            "key_recorded": inner_client is not None,
            "processing_seconds": round(time.time() - started, 2),
            "double_check_triggered": dc_triggered,
            "second_result": second_result,
            "second_marking_model": second_marking_model,
            "double_check_error": dc_error,
            "ai_usage": call_audit.snapshot(),
        }
    finally:
        if inner_client is not None:
            # Do NOT delete the uploads: an AI failure retried by the pool must
            # reuse them instead of re-uploading the student's submission.
            # The pool deletes them at the job's terminal state.
            try:
                inner_client.release_job_files()
            except Exception:
                pass
        else:
            for remote in [*remote_students, remote_scheme, remote_question]:
                if remote is not None:
                    try:
                        client.files.delete(name=remote.name)
                    except Exception:
                        pass


def main() -> int:
    # Structured logs (run id / model / safe key fingerprint / blueprint
    # source) go to data/logs/app.log — credentials are redacted by the
    # logging filter; stderr is discarded by the worker pool.
    try:
        from app.logging_setup import setup_logging
        setup_logging()
    except Exception:
        pass

    raw = sys.stdin.read()
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as exc:
        json.dump({"ok": False, "status": "FAILED",
                   "error": f"Bad job descriptor: {exc}"}, sys.stdout)
        return 1

    try:
        payload = _run(job)
    except KeyboardInterrupt:
        payload = {"job_id": job.get("job_id"), "worker_id": job.get("worker_id"),
                   "status": "CANCELLED", "ok": False, "error": "Cancelled"}
    except Exception as exc:
        from app.errors import classify_gemini_error
        payload = {"job_id": job.get("job_id"), "worker_id": job.get("worker_id"),
                   "status": "FAILED", "ok": False,
                   "error": str(exc)[:800],
                   "error_kind": classify_gemini_error(exc)["kind"]}
    # Even a failed correction reports what it spent: a paper that burned ten
    # requests on an outage is exactly what this field exists to expose.
    from app.gemini import call_audit as _audit
    payload.setdefault("ai_usage", _audit.snapshot())

    json.dump(payload, sys.stdout, ensure_ascii=False, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
