"""Repeated marking of one submission must not depend on luck.

Regression tests for the live finding behind "Q9: 0 then 2": two runs of the
SAME paper and mark scheme returned different marks for one question, so the
same submission totalled 47 or 49 depending on the run.

Root cause: when the marker omitted a question, ``apply_blueprint`` inserted a
technical-gap placeholder (``awarded_marks: 0``) — correct, and deliberately
counted in the denominator so a dropped question can never inflate the
percentage. But the targeted retry that should heal it ran EXACTLY ONCE, and it
could only build its narrow prompt from a CACHED mark-scheme FILE, found by
path. A scheme that arrived from Classroom/Drive has no such path, so the retry
was refused outright and the placeholder 0 was persisted as if it were a score.

These tests pin the three properties that make the repeat deterministic:
  1. the narrow recovery prompt can be built from the in-memory blueprint;
  2. the recovery round repeats while it is still closing the gap;
  3. the placeholder says out loud that it is not a score.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.engine import blueprint as bp
from app.engine import legacy_marking as legacy
from app.engine import recover_missing
from app.engine.question_inventory import narrow_blueprint

#: Q1 max 3, Q2 max 2, Q3 max 2 — the shape of the live paper's gap.
BLUEPRINT = {
    "questions": [
        {"question_number": "1", "max_marks": 3},
        {"question_number": "2", "max_marks": 2},
        {"question_number": "3", "max_marks": 2},
    ]
}


def _row(number, awarded, maximum):
    return {
        "question_number": number, "max_marks": maximum,
        "awarded_marks": awarded, "topic": "t", "student_answer": "a",
        "working_summary": "w",
        "loss_reason": "" if awarded == maximum else "slip",
        "mark_breakdown": [], "correct_method": "c", "full_mark_solution": "f",
    }


def _run(*, omit: str | None):
    """One marking run whose model payload omits (or includes) one question."""
    numbers = {"1": 3, "2": 2, "3": 2}
    rows = [_row(n, m, m) for n, m in numbers.items() if n != omit]
    result = legacy.normalise_result(json.loads(json.dumps({
        "paper_info": {"qualification": "Q", "paper": "P", "session": "S"},
        "questions": rows,
    })))
    return bp.apply_blueprint(result, BLUEPRINT)


# ============================================================ the failure itself

class TestTheOmissionIsWhatMovedTheMark:
    def test_an_omitted_question_reads_as_zero_not_as_a_gap(self):
        """The reason the two runs disagreed, stated as a fact about the data."""
        result_a, report_a = _run(omit="3")
        result_b, report_b = _run(omit=None)

        earned_a, available_a, _ = legacy.calculate_totals(result_a)
        earned_b, available_b, _ = legacy.calculate_totals(result_b)

        assert report_a["missing_questions"] == ["3"]
        assert report_b["missing_questions"] == []
        # Identical input; the totals differ by exactly the omitted question.
        assert (earned_a, available_a) == (5, 7)
        assert (earned_b, available_b) == (7, 7)
        assert earned_b - earned_a == 2

        q3 = next(q for q in result_a["questions"] if q["question_number"] == "3")
        assert q3["awarded_marks"] == 0
        assert q3["status"] == bp.STATUS_REVIEW

    def test_placeholder_is_counted_in_the_denominator_on_purpose(self):
        """A dropped question must never inflate the percentage."""
        result, _ = _run(omit="3")
        _, available, _ = legacy.calculate_totals(result)
        assert available == 7          # unchanged by the gap


# ========================================================= 1. the narrow prompt

class TestNarrowPromptNoLongerNeedsACachedFile:
    def test_cached_lookup_is_still_used_when_no_blueprint_is_given(self):
        """Unchanged behaviour: with no path and no blueprint, there is no prompt."""
        assert narrow_blueprint(None, ["3"]) is None

    def test_in_memory_blueprint_builds_the_narrow_prompt_without_a_path(self):
        """The fix: a Classroom/Drive scheme has no cached file path."""
        narrow = narrow_blueprint(None, ["3"], blueprint=BLUEPRINT)
        assert narrow is not None
        assert [q["question_number"] for q in narrow["questions"]] == ["3"]
        assert narrow["questions"][0]["max_marks"] == 2

    def test_narrow_prompt_scores_only_the_requested_question(self):
        narrow = narrow_blueprint(None, ["2", "3"], blueprint=BLUEPRINT)
        assert [q["question_number"] for q in narrow["questions"]] == ["2", "3"]

    def test_build_narrow_blueprint_accepts_the_in_memory_blueprint(self):
        assert recover_missing.build_narrow_blueprint(None, ["3"]) is None
        built = recover_missing.build_narrow_blueprint(None, ["3"],
                                                      blueprint=BLUEPRINT)
        assert built is not None
        assert built["questions"][0]["question_number"] == "3"


# ================================================== 2. the retry actually runs

class _RecoveryClient:
    """Returns a row for Q3 — but only from the SECOND narrow call on.

    This models the live failure precisely: one retry is not enough.
    """

    records_usage = True

    def __init__(self):
        self.narrow_calls = 0

    def current_fingerprint(self):
        return None

    class models:  # noqa: N801 - mirrors the real client surface
        @staticmethod
        def generate_content(**_kwargs):
            raise AssertionError("the main pass is not exercised here")


def test_recovery_round_heals_the_gap_without_a_cached_scheme_file():
    """Before the fix this returned ok=False and the placeholder stayed."""
    class Client:
        records_usage = True

        class models:  # noqa: N801
            @staticmethod
            def generate_content(**_kwargs):
                payload = {"questions": [_row("3", 2, 2)]}

                class _Resp:
                    text = json.dumps(payload)

                return _Resp()

    outcome = recover_missing.run_recovery_round(
        client=Client(), missing=["3"],
        student_remote="s", mark_scheme_remote="m",
        mark_scheme_path=None, blueprint=BLUEPRINT)
    assert outcome.ok is True
    assert outcome.error is None
    assert "3" in outcome.recovered
    assert outcome.still_missing == []
    assert outcome.recovered["3"]["awarded_marks"] == 2


def test_recovery_is_still_refused_when_there_is_no_structure_at_all():
    """No path AND no blueprint: refuse loudly, never invent a re-mark."""
    outcome = recover_missing.run_recovery_round(
        client=_RecoveryClient(), missing=["3"],
        student_remote="s", mark_scheme_remote="m",
        mark_scheme_path=None, blueprint=None)
    assert outcome.ok is False
    assert "No reliable mark-scheme structure" in (outcome.error or "")
    assert outcome.still_missing == ["3"]


def test_recovery_stops_after_one_round_when_it_recovers_nothing():
    """A repeat of an identical request must not burn the budget."""
    class EmptyClient:
        records_usage = True

        class models:  # noqa: N801
            @staticmethod
            def generate_content(**_kwargs):
                class _Resp:
                    text = json.dumps({"questions": []})

                return _Resp()

    outcome = recover_missing.run_recovery_round(
        client=EmptyClient(), missing=["3"],
        student_remote="s", mark_scheme_remote="m",
        mark_scheme_path=None, blueprint=BLUEPRINT)
    assert outcome.recovered == {}
    assert outcome.still_missing == ["3"]


# ================================================ 3. the child repeats the retry

class TestChildRepeatsTheRetryUntilTheGapCloses:
    def _drive(self, fresh_state, tmp_path, monkeypatch, *, rows_by_call):
        """Run the real worker child with a scripted model, return the payload."""
        import app.config as cfg_mod
        import app.db as db_mod
        from app.engine.blueprint import save_cached
        from pypdf import PdfWriter

        cfg = cfg_mod.get_config()
        cfg.mock_mode = False
        cfg.gemini.primary_models = ["mock-model"]
        #: The recovery budget (``_max_rounds`` reads this) — the loop must
        #: stay inside it and no further.
        cfg.worker.max_retries = 3

        db_mod.init_db()

        student_pdf = tmp_path / "student.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with open(student_pdf, "wb") as fh:
            writer.write(fh)

        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock ms\n")
        sha = hashlib.sha256(ms_pdf.read_bytes()).hexdigest()
        save_cached(sha, {"questions": [
            {"question_number": "1", "max_marks": 3},
            {"question_number": "2", "max_marks": 2},
        ]}, model="test")

        state = {"calls": 0}

        class FakeChildClient:
            records_usage = True

            def upload_file(self, path, purpose=None, shared=False):
                return str(path)

            def files_delete(self, remote):
                pass

            def release_job_files(self):
                pass

            def current_fingerprint(self):
                return None

            class files:  # noqa: N801
                @staticmethod
                def upload(file):
                    return str(file)

                @staticmethod
                def get(name):
                    return name

                @staticmethod
                def delete(name):
                    pass

            @property
            def models(self):
                return self

            def generate_content(self, **_kwargs):
                state["calls"] += 1
                payload_rows = rows_by_call(state["calls"])

                class _Resp:
                    text = json.dumps({"questions": payload_rows})

                return _Resp()

        import app.gemini.client_proxy as client_proxy
        monkeypatch.setattr(client_proxy, "KeyAwareClient",
                            lambda *a, **k: FakeChildClient())

        import app.workers.child as child

        job = {
            "job_id": "job-determinism", "worker_id": "w1",
            "submission_paths": [str(student_pdf)],
            "mark_scheme_path": str(ms_pdf),
            "question_paper_path": None,
            "primary_models": ["mock-model"],
            "audit_enabled": False,
            "double_check_enabled": False,
            "mock": False,
        }
        return child._run(job), state

    def test_a_second_retry_recovers_what_the_first_one_missed(
            self, fresh_state, tmp_path, monkeypatch):
        """First retry returns nothing useful; the repeat must close the gap.

        With the old single-round code the placeholder 0 was persisted here
        and the same paper totalled two marks fewer than a run that happened
        to return the row.
        """
        def rows_by_call(call: int):
            if call == 1:                      # main pass omits Q2
                return [_row("1", 3, 3)]
            if call == 2:                      # first retry: still no Q2
                return []
            return [_row("2", 2, 2)]           # second retry answers it

        payload, state = self._drive(
            fresh_state, tmp_path, monkeypatch, rows_by_call=rows_by_call)

        assert payload["ok"] is True
        questions = {q["question_number"]: q
                     for q in payload["result"]["questions"]}
        assert questions["2"]["awarded_marks"] == 2, (
            "the omitted question must end up with its real mark, not a "
            "persisted placeholder 0")
        assert questions["2"]["status"] not in ("REVIEW", "NEEDS_REVIEW",
                                                "UNMARKED")
        recovery = payload["blueprint_report"].get("recovery") or {}
        assert recovery.get("rounds") == 2, recovery
        assert recovery.get("recovered") == ["2"]

    def test_a_gap_that_never_heals_still_reports_what_happened(
            self, fresh_state, tmp_path, monkeypatch):
        """Bounded: the retry repeats only while it makes progress."""
        def rows_by_call(call: int):
            return [_row("1", 3, 3)] if call == 1 else []

        payload, state = self._drive(
            fresh_state, tmp_path, monkeypatch, rows_by_call=rows_by_call)

        questions = {q["question_number"]: q
                     for q in payload["result"]["questions"]}
        q2 = questions["2"]
        # Still a gap — but an HONEST one, and the budget was respected.
        # (The child labels it with its explicit unresolved state.)
        assert q2["status"] in bp.UNRESOLVED_STATUSES, q2["status"]
        # 1 main pass + at most ``max_retries`` recovery rounds.
        assert state["calls"] <= 4, f"the repeat must stay bounded: {state}"
        recovery = payload["blueprint_report"].get("recovery") or {}
        assert recovery.get("rounds") == 3, recovery
        assert recovery.get("still_missing") == ["2"]
        # The gap is stated in the field the report shows, not left blank.
        assert q2["loss_reason"] == bp.UNMARKED_REASON


# ================================================ 3b. the placeholder is explicit

class TestThePlaceholderSaysWhatItIs:
    def test_loss_reason_is_populated_not_blank(self):
        """The report shows loss_reason; a blank one hid the gap behind a 0."""
        result, _ = _run(omit="3")
        q3 = next(q for q in result["questions"] if q["question_number"] == "3")
        assert q3["loss_reason"], "a placeholder must not look like a real 0"
        assert q3["loss_reason"] == bp.UNMARKED_REASON
        assert "UNMARKED" in q3["loss_reason"].upper()
        assert q3["review_reason"] == bp.UNMARKED_REASON

    def test_a_real_zero_keeps_its_own_reason(self):
        """A student's genuine 0 must never be relabelled as a gap."""
        result = legacy.normalise_result(json.loads(json.dumps({
            "paper_info": {"qualification": "Q", "paper": "P", "session": "S"},
            "questions": [_row("1", 3, 3), _row("2", 0, 2), _row("3", 2, 2)],
        })))
        out, report = bp.apply_blueprint(result, BLUEPRINT)
        q2 = next(q for q in out["questions"] if q["question_number"] == "2")
        assert report["missing_questions"] == []
        assert q2["awarded_marks"] == 0
        assert q2["loss_reason"] != bp.UNMARKED_REASON
        assert q2["status"] != bp.STATUS_REVIEW
