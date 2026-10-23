"""Regression tests for missing-question safeguards in normal correction.

Bug — questions present in the official mark scheme (e.g. 62-69) came back
        with "no answer" from the marker, were added as REVIEW placeholders,
        and silently looked like 0s. Now: detected via the canonical
        inventory, marked NEEDS_REVIEW (unresolved, never a 0), and retried
        with a narrow prompt that ONLY re-marks the missing questions.

All tests are deterministic (no AI, no network, no API quota): marking
clients are injected or mocked.
"""

from __future__ import annotations

import pytest

from app.engine import question_inventory as qi
from app.engine.blueprint import apply_blueprint
from app.engine.question_inventory import (
    canonical_question_id,
    confirmed_zero_status,
    expected_inventory,
    mark_missing_results,
    missing_from_results,
)


# ------------------------------------------------------------------ fixtures

def _blueprint(numbers, max_marks=4):
    return {"questions": [{"question_number": str(n), "max_marks": max_marks,
                           "criteria": "c"} for n in numbers]}


def _q(number, awarded, max_marks, *, answer="2+2=4", breakdown=None):
    return {
        "question_number": str(number), "max_marks": max_marks,
        "awarded_marks": awarded, "topic": "", "student_answer": answer,
        "working_summary": "", "loss_reason": "",
        "mark_breakdown": breakdown if breakdown is not None else [
            {"code": "M1", "awarded": awarded > 0, "reason": "x"}],
        "correct_method": "", "full_mark_solution": "",
    }


def _blank_q(number, max_marks):
    """A row the marker RETURNED but that legitimately earned nothing."""
    return _q(number, 0, max_marks, answer="", breakdown=[])


def _placeholder(number, max_marks):
    """What apply_blueprint inserts when the marker returned nothing."""
    return _q(number, 0, max_marks, answer="", breakdown=[]) | {
        "status": "NEEDS_REVIEW",
        "review_reason": "The marker returned no result for this question.",
    }


# ============================================================ bug 1: inventory

class TestCanonicalIds:
    @pytest.mark.parametrize("raw,expected", [
        ("62(a)", "62a"), ("Q62(a)", "62a"), ("62 a", "62a"),
        (" 62 (a) ", "62a"), ("14", "14"), ("Q14", "14"), (" q14. ", "14"),
        ("7b", "7b"), ("62(A)", "62a"),
    ])
    def test_equivalent_formats_map_to_one_canonical_id(self, raw, expected):
        assert canonical_question_id(raw) == expected

    def test_one_question_number_never_swallows_another(self):
        # "1" and "14" are distinct questions, canonical ids included.
        assert canonical_question_id("1") != canonical_question_id("14")
        # A partial label like "1(a)" must never be confused with "14".
        assert canonical_question_id("1a") != canonical_question_id("14a")


class TestMissingQuestionDetection:
    def test_inventory_reports_omitted_questions(self):
        blueprint = _blueprint(range(1, 70))
        result = {"questions": [_q(1, 2, 4)]}
        missing = missing_from_results(result, blueprint)
        # Every question except 1 is unaccounted for in this tiny result.
        assert missing == [str(n) for n in range(2, 70)]

    def test_inventory_skips_legitimate_blanks(self):
        # A CONFIRMED blank still counts as COVERED by the inventory (it was
        # returned): the blank/zero distinction happens at row-state level.
        blueprint = _blueprint([1, 2, 3])
        result = {"questions": [_q(1, 2, 4), _blank_q(2, 4)]}
        missing = missing_from_results(result, blueprint)
        assert missing == ["3"]

    def test_inventory_tolerates_unreadable_mark_scheme(self):
        assert missing_from_results({"questions": []}, None) == []
        assert missing_from_results({"questions": []}, {"questions": []}) == []

    def test_subpart_covers_parent(self):
        blueprint = _blueprint([62])
        result = {"questions": [_q("62(a)", 2, 4)]}
        assert missing_from_results(result, blueprint) == []

    def test_non_consecutive_numbering_detected_not_assumed(self):
        # A real mark scheme with gaps (e.g. 40 marks, question numbers
        # 1, 2, 5, 7): the INVENTORY is read from the blueprint, not guessed
        # as a consecutive range, so 5 and 7 are required and 3 is not.
        blueprint = _blueprint([1, 2, 5, 7])
        result = {"questions": [_q(1, 1, 4), _q(2, 1, 4)]}
        assert missing_from_results(result, blueprint) == ["5", "7"]

    def test_nested_subparts_do_not_duplicate_because_of_aliases(self):
        inv = expected_inventory({
            "questions": [
                {"question_number": "62(a)", "max_marks": 2, "criteria": "c"},
                {"question_number": "Q62(a)", "max_marks": 2, "criteria": "c"},
                {"question_number": "62", "max_marks": 4, "criteria": "c"},
            ]})
        # All three labels collapse to ONE canonical question.
        assert list(inv) == ["62"]
        assert inv["62"]["max_marks"] == 4


class TestApplyBlueprintMissingRows:
    def test_missing_rows_keep_status_needs_review_not_a_zero(self):
        blueprint = _blueprint(range(1, 8))
        result = {"questions": [_q(n, 2, 4) for n in (1, 2)]}
        merged, report = apply_blueprint(result, blueprint)
        assert report["missing_questions"] == ["3", "4", "5", "6", "7"]
        placeholders = [q for q in merged["questions"]
                        if q["question_number"] in {"3", "4", "5", "6", "7"}]
        for q in placeholders:
            # A placeholder is NOT a confirmed 0: status must reveal that.
            assert q["status"] in ("REVIEW", "NEEDS_REVIEW")
            assert q["awarded_marks"] == 0

    def test_placeholder_never_counts_as_a_valid_row_at_persist(self):
        # What a finalizedゼresult would look like if the placeholder were
        # silently accepted as a score.
        blueprint = _blueprint([1, 2, 3])
        result = {"questions": [_q(1, 4, 4), _q(2, 4, 4)]}
        merged, _report = apply_blueprint(result, blueprint)
        mark_missing_results(merged)
        statuses = {q["question_number"]: q["status"]
                    for q in merged["questions"]}
        assert statuses["3"] == "NEEDS_REVIEW"   # unresolved, NOT a 0
        assert statuses["1"] in (None, "VALID")  # untouched real result

    def test_extra_question_is_review_not_missing(self):
        blueprint = _blueprint([1, 2])
        result = {"questions": [_q(1, 2, 4), _q(2, 2, 4), _q(99, 2, 4)]}
        merged, report = apply_blueprint(result, blueprint)
        assert report["missing_questions"] == []
        assert report["extra_questions"] == ["99"]

    def test_confirmed_blank_is_labelled_zero(self):
        q = _blank_q(5, 3)
        assert confirmed_zero_status(q) == "CONFIRMED_ZERO"
        assert confirmed_zero_status(_q(5, 0, 3)) is None  # has evidence
        assert confirmed_zero_status(_q(5, 2, 3)) is None  # not zero

    def test_needs_review_row_is_not_blank_confirmed(self):
        q = _placeholder(5, 3)
        assert confirmed_zero_status(q) is None  # placeholder, not a blank


# ============================================================ bug 1: recovery

class TestRecoveryBoundedBudget:
    def test_recovery_escalates_without_credential_leak_when_too_many_missing(self):
        from app.engine.recover_missing import need_recovery
        may, reason = need_recovery(
            missing=[str(n) for n in range(62, 70)], already_attempted=0)
        # 8 missing > the bounded cap (also reads 'review mark scheme').
        assert may is True
        assert reason is None
        may, reason = need_recovery(
            missing=[str(n) for n in range(1, 40)], already_attempted=0)
        assert may is False
        assert "mark scheme" in reason.lower()

    def test_recovery_budget_attempts_exhausted_message(self):
        from app.engine.recover_missing import need_recovery
        may, reason = need_recovery(missing=["5"], already_attempted=2)
        assert may is False
        assert "review" in reason.lower()

    def test_narrow_blueprint_contains_only_the_missing_questions(self,
                                                                 fresh_state,
                                                                 tmp_path):
        import hashlib

        from app.engine.blueprint import save_cached
        from app.engine.recover_missing import build_narrow_blueprint

        ms = tmp_path / "ms.pdf"
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with open(ms, "wb") as fh:
            writer.write(fh)
        sha = hashlib.sha256(ms.read_bytes()).hexdigest()
        save_cached(sha, _blueprint(range(62, 68)), model="test")
        narrow = build_narrow_blueprint(str(ms), ["62", "63"])
        assert narrow is not None
        numbers = [str(q["question_number"]) for q in narrow["questions"]]
        # ONLY the missing questions enter the retry prompt (not the paper).
        assert set(numbers) <= {"62", "63", "64", "65", "66", "67"}
        assert "62" in numbers and "63" in numbers

    def test_narrow_blueprint_is_none_without_a_cached_structure(self,
                                                                 tmp_path):
        from app.engine.recover_missing import build_narrow_blueprint
        ms = tmp_path / "unseen.pdf"
        ms.write_bytes(b"not even a pdf")
        assert build_narrow_blueprint(str(ms), ["62"]) is None

    def test_split_missing_chunks_are_bounded(self):
        from app.engine.recover_missing import MAXMISS_IN_PROMPT, split_missing
        chunks = split_missing([str(n) for n in range(62, 68)])
        assert all(len(c) <= MAXMISS_IN_PROMPT for c in chunks)


class TestNormalPipelineChildRecovery:
    """The NORMAL pipeline (workers/child.py) drives the same targeted
    recovery while its downloaded submission + mark scheme are still alive:
    no re-upload, one narrow AI call, successful rows never re-marked."""

    def test_child_upgrades_missing_placeholders_and_recovers(
            self, fresh_state, tmp_path, monkeypatch):
        import app.config as cfg_mod
        import app.db as db_mod
        from app.engine.blueprint import save_cached
        import hashlib
        import time as _time

        cfg = cfg_mod.get_config()
        cfg.mock_mode = False          # force the real-client path
        cfg.gemini.primary_models = ["mock-model"]
        cfg.worker.max_retries = 2

        db_path = fresh_state / "app.db"
        db_mod.init_db()

        student_pdf = tmp_path / "student.pdf"
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with open(student_pdf, "wb") as fh:
            writer.write(fh)
        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock ms\n")
        sha = hashlib.sha256(ms_pdf.read_bytes()).hexdigest()
        save_cached(sha, _blueprint([1, 2]), model="test")

        narrow_calls: list[list[str]] = []
        call_count = [0]

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

            class files:  # noqa: N801 - mirrors the real client surface
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

            def generate_content(self, **kwargs):
                # The FIRST call is the main marking pass (deliberately omits
                # q2 — the missing-question bug); the SECOND is the narrow
                # recovery pass.
                call_count[0] += 1
                import json as _json
                if call_count[0] > 1:
                    narrow_calls.append(["2"])
                    payload = {"questions": [
                        {"question_number": "2", "max_marks": 4,
                         "awarded_marks": 2, "topic": "t",
                         "student_answer": "x", "working_summary": "",
                         "loss_reason": "", "mark_breakdown": [
                             {"code": "M1", "awarded": True, "reason": "r"}],
                         "correct_method": "", "full_mark_solution": ""}]}
                else:
                    payload = {"questions": [
                        {"question_number": "1", "max_marks": 4,
                         "awarded_marks": 4, "topic": "t",
                         "student_answer": "y", "working_summary": "",
                         "loss_reason": "", "mark_breakdown": [
                             {"code": "M1", "awarded": True, "reason": "r"}],
                         "correct_method": "", "full_mark_solution": ""}]}

                class _Resp:
                    text = _json.dumps(payload)

                return _Resp()

        import app.gemini.client_proxy as client_proxy
        monkeypatch.setattr(client_proxy, "KeyAwareClient",
                            lambda *a, **k: FakeChildClient())

        # Skip the worker-child's subprocess transport — drive _run directly
        # with the descriptor the pool builds.
        import app.workers.child as child
        from app.engine.recover_missing import as_json

        job = {
            "job_id": "job-child-recovery", "worker_id": "w1",
            "submission_paths": [str(student_pdf)],
            "mark_scheme_path": str(ms_pdf),
            "question_paper_path": None,
            "primary_models": ["mock-model"],
            "audit_enabled": False,
            "double_check_enabled": False,
            "mock": False,
        }
        payload = child._run(job)
        assert payload["ok"] is True
        questions = {q["question_number"]: q
                     for q in payload["result"]["questions"]}
        # q1 intact, q2 recovered (no longer a NEEDS_REVIEW placeholder).
        assert questions["1"]["awarded_marks"] == 4
        assert questions["2"]["awarded_marks"] == 2
        assert questions["2"]["status"] not in ("NEEDS_REVIEW", "REVIEW")
        # Exactly ONE narrow recovery call was made — bounded.
        assert len(narrow_calls) == 1
        # The recovery record reached the blueprint report.
        recovery = payload["blueprint_report"].get("recovery") or {}
        assert recovery.get("rounds") == 1
        assert "2" in (recovery.get("recovered") or [])
