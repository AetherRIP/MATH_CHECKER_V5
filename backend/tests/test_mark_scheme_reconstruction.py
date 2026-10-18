"""Mark-scheme reconstruction and verification (§19 scenarios 1-12).

Every test here corresponds to a scenario the brief names explicitly. The PDF
fixtures in :mod:`fixtures_mark_scheme` are generated locally with reportlab, so
the failure modes are real: two-column layouts whose naive extraction order
interleaves, page numbers that look exactly like questions, tables full of
numbers, questions split across a page break, and out-of-order model output.

No network, no API key, no student data.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.engine import blueprint as bp
from app.engine import mark_scheme as ms
from app.engine import mark_scheme_verify as msv

from fixtures_mark_scheme import (
    image_only_mark_scheme, page_break_mark_scheme, page_numbers_mark_scheme,
    table_mark_scheme, two_column_mark_scheme,
)


@pytest.fixture
def client(fresh_state, monkeypatch):
    """Same offline client the other mark-scheme tests use."""
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def pdfs(tmp_path):
    return {
        "two_column": str(two_column_mark_scheme(tmp_path / "tc.pdf")),
        "page_numbers": str(page_numbers_mark_scheme(tmp_path / "pn.pdf")),
        "table": str(table_mark_scheme(tmp_path / "tb.pdf")),
        "page_break": str(page_break_mark_scheme(tmp_path / "pb.pdf")),
        "image_only": str(image_only_mark_scheme(tmp_path / "io.pdf")),
    }


# --- Test 2 / 3 / 5 / 6: local document reconstruction ------------------

class TestDocumentReconstruction:
    def test_two_column_pages_are_not_interleaved(self, pdfs):
        """Test 2: a two-column scheme must not read 1, 7, 2, 8, 3, 9."""
        evidence = ms.analyse_document(pdfs["two_column"])
        assert evidence["mode"] == ms.MODE_TEXT
        assert evidence["diagnostics"]["two_column_pages"] == 1
        found = ms.candidate_question_numbers(evidence)
        assert found == [str(n) for n in range(1, 13)]

        page2 = next(p for p in evidence["pages"] if p["page"] == 2)
        assert page2["columns"] == 2
        order = [b["text"].split()[0] for b in page2["blocks"]
                 if not b["is_header_or_footer"]]
        # Reading order runs down the left column, then the right one.
        assert order == [str(n) for n in range(1, 13)]

    def test_page_numbers_are_never_questions(self, pdfs):
        """Test 3: 'Page 2' / '2 of 3' must not become Question 2."""
        evidence = ms.analyse_document(pdfs["page_numbers"])
        assert ms.candidate_question_numbers(evidence) == ["1", "2", "3"]
        assert evidence["diagnostics"]["page_number_candidates"] >= 3
        page_numbers = {c["value"] for c in evidence["candidates"]
                        if c["type"] == ms.CAND_PAGE_NUMBER}
        assert {"2", "3"} <= page_numbers

    def test_a_question_split_across_pages_stays_one_question(self, pdfs):
        """Test 5: Q5 continues onto page 2 and must not become two questions."""
        evidence = ms.analyse_document(pdfs["page_break"])
        found = ms.candidate_question_numbers(evidence)
        # The requirement is that the continuation is not counted as a NEW
        # question; seeing Q5's label on two pages is expected and is collapsed
        # to a single candidate.
        assert found == ["5", "6"]
        pages_seen = {c["page"] for c in evidence["candidates"]
                      if c["value"] == "5" and c["type"] == ms.CAND_TOP_LEVEL}
        assert pages_seen == {1, 2}, "the question does genuinely span both pages"
        assert len([n for n in found if n == "5"]) == 1

    def test_table_numbers_are_not_promoted_to_questions(self, pdfs):
        """Test 6: mark allocations in a table are not question numbers."""
        evidence = ms.analyse_document(pdfs["table"])
        found = ms.candidate_question_numbers(evidence)
        assert found == ["1", "2", "3", "4", "5"]
        # The lone mark cells are reported UNRESOLVED rather than guessed.
        unresolved = [c for c in evidence["candidates"]
                      if c["type"] == ms.CAND_OTHER]
        assert unresolved, "ambiguous table cells must be surfaced, not invented"

    def test_image_only_mark_scheme_is_detected_truthfully(self, pdfs):
        """A PDF with no text layer must say so instead of pretending."""
        evidence = ms.analyse_document(pdfs["image_only"])
        assert evidence["mode"] == ms.MODE_IMAGE_ONLY
        assert evidence["diagnostics"]["blocks"] == 0
        assert ms.candidate_question_numbers(evidence) == []

    def test_unreadable_file_degrades_instead_of_raising(self, tmp_path):
        bad = tmp_path / "broken.pdf"
        bad.write_bytes(b"not a pdf at all")
        evidence = ms.analyse_document(str(bad))
        assert evidence["mode"] in (ms.MODE_NONE, ms.MODE_IMAGE_ONLY)
        assert evidence["error"]


# --- Test 4 / 18: canonical numeric ordering ----------------------------

class TestCanonicalOrdering:
    def test_random_input_is_sorted_numerically(self):
        """Test 4: Q10 appearing before Q2 must still order as 1..10."""
        assert ms.sort_numbers(["10", "2", "1", "9", "3"]) == \
            ["1", "2", "3", "9", "10"]

    def test_ordering_is_not_alphabetical(self):
        ordered = ms.sort_numbers([str(n) for n in (11, 2, 10, 1)])
        assert ordered == ["1", "2", "10", "11"]

    def test_sub_parts_order_within_their_question(self):
        ordered = ms.sort_numbers(["2(b)", "1(ii)", "1(a)", "10", "2(a)"])
        assert ordered == ["1(a)", "1(ii)", "2(a)", "2(b)", "10"]

    def test_sequence_report_finds_a_missing_question(self):
        """Test 7: 1..6 with 4 absent must be reported, not invented."""
        report = ms.sequence_report(["1", "2", "3", "5", "6", "7"])
        assert report["missing"] == [4]
        assert report["sequential"] is False

    def test_sequence_report_flags_duplicates(self):
        report = ms.sequence_report(["1", "1", "2"])
        assert report["duplicates"] == ["1"]


# --- Test 1 / 10 / 11 / 12: canonicalisation of the model's answer ------

class TestCanonicalisation:
    def test_a_normal_scheme_yields_all_questions(self):
        """Test 1: a 33-question scheme must produce 33 questions."""
        parsed = {"questions": [
            {"question_number": str(n), "max_marks": 3, "criteria": "M"}
            for n in range(1, 34)
        ], "printed_total": 99, "confidence": 0.97}
        structure = msv.canonicalize(parsed)
        assert len(structure["questions"]) == 33
        assert structure["calculated_total"] == 99
        assert structure["questions"][0]["question_number"] == "1"
        assert structure["questions"][-1]["question_number"] == "33"

    def test_random_order_from_the_model_is_normalised(self):
        """Test 10: shuffled output must become 1..10."""
        parsed = {"questions": [
            {"question_number": str(n), "max_marks": 2} for n in
            (7, 3, 10, 1, 5, 2, 9, 4, 8, 6)
        ]}
        structure = msv.canonicalize(parsed)
        assert [q["question_number"] for q in structure["questions"]] == \
            [str(n) for n in range(1, 11)]

    def test_duplicate_question_numbers_are_rejected_and_flagged(self):
        """Test 11: duplicates must not silently become extra questions."""
        parsed = {"questions": [
            {"question_number": "1", "max_marks": 5},
            {"question_number": "1", "max_marks": 5},
            {"question_number": "2", "max_marks": 5},
        ]}
        structure = msv.canonicalize(parsed)
        assert [q["question_number"] for q in structure["questions"]] == ["1", "2"]
        assert structure["duplicates_rejected"] == ["1"]

    def test_malformed_json_yields_nothing_and_is_not_destructive(self):
        """Test 12: unusable model output must never replace a good structure."""
        assert msv.canonicalize({"questions": "not a list"}) is None
        assert msv.canonicalize("total garbage") is None
        assert msv.canonicalize({}) is None
        assert msv.canonicalize(None) is None
        action, detail = msv.prefer(
            {"questions": [{"question_number": "1", "max_marks": 5}]},
            None, None, None)
        assert action == "keep_existing"
        assert "preserved" in detail["reason"]

    def test_sub_parts_are_ordered_and_summed_into_the_parent(self):
        parsed = {"questions": [{
            "question_number": "15", "max_marks": 99,
            "parts": [{"number": "b", "max_marks": 2},
                      {"number": "a", "max_marks": 3}],
        }]}
        structure = msv.canonicalize(parsed)
        q = structure["questions"][0]
        assert [p["number"] for p in q["parts"]] == ["a", "b"]
        # The parent's allocation is computed, not taken from the model.
        assert q["max_marks"] == 5
        assert structure["calculated_total"] == 5


# --- Test 7 / 9: cross-validation and anomaly detection -----------------

class TestCrossValidation:
    def test_total_mismatch_is_reported(self):
        """Test 9: questions sum to 100 but the parser claimed 98."""
        parsed = {
            "questions": [{"question_number": str(n), "max_marks": 10}
                          for n in range(1, 11)],
            "total_marks": 98, "printed_total": 100, "confidence": 0.99,
        }
        structure = msv.canonicalize(parsed)
        result = msv.cross_validate(structure, None)
        assert result["verified"] is False
        assert any("98" in a and "100" in a for a in result["anomalies"])

    def test_a_fully_consistent_scheme_verifies(self):
        parsed = {
            "questions": [{"question_number": str(n), "max_marks": 5}
                          for n in range(1, 11)],
            "printed_total": 50, "total_marks": 50, "confidence": 0.96,
        }
        structure = msv.canonicalize(parsed)
        result = msv.cross_validate(structure, None)
        assert result["anomalies"] == []
        assert result["verified"] is True
        assert result["structural_confidence"] == pytest.approx(0.96)

    def test_missing_question_is_an_anomaly_not_an_invention(self):
        parsed = {"questions": [
            {"question_number": str(n), "max_marks": 2} for n in range(1, 9)
            if n != 4], "printed_total": 16, "total_marks": 16}
        structure = msv.canonicalize(parsed)
        result = msv.cross_validate(structure, None)
        assert result["checks"]["missing_questions"] == [4]
        assert any("Missing expected question" in a for a in result["anomalies"])
        # Crucially, the question was NOT invented into the structure.
        assert "4" not in [q["question_number"] for q in structure["questions"]]

    def test_image_only_source_is_flagged_as_uncross_checkable(self, pdfs):
        evidence = ms.analyse_document(pdfs["image_only"])
        parsed = {"questions": [{"question_number": "1", "max_marks": 5}],
                  "printed_total": 5, "total_marks": 5, "confidence": 0.9}
        result = msv.cross_validate(msv.canonicalize(parsed), evidence)
        assert result["evidence_agrees"] is None
        assert any("no text layer" in a for a in result["anomalies"])


# --- Test 8 / 16: a verified structure is never overwritten by a worse one

class TestExistingStructureIsProtected:
    def _good(self):
        return {"questions": [{"question_number": str(n), "max_marks": 3}
                              for n in range(1, 34)],
                "calculated_total": 99}

    def _bad(self):
        return {"questions": [{"question_number": str(n), "max_marks": 3}
                              for n in range(1, 23)],
                "calculated_total": 66}

    def test_a_worse_result_cannot_replace_a_verified_structure(self):
        """Test 8: existing 33 questions must survive a new 22-question result."""
        good, good_val = self._good(), {"structural_confidence": 1.0,
                                        "anomalies": [], "evidence_agrees": True}
        bad, bad_val = self._bad(), {"structural_confidence": 0.3,
                                     "anomalies": ["..."], "evidence_agrees": False}
        action, detail = msv.prefer(good, bad, good_val, bad_val)
        assert action == "keep_existing"
        assert "preserved" in detail["reason"]

    def test_a_better_result_does_replace(self):
        good, bad = self._good(), self._bad()
        action, _ = msv.prefer(
            bad, good, {"structural_confidence": 0.3, "anomalies": ["x"]},
            {"structural_confidence": 1.0, "anomalies": []})
        assert action == "replace"

    def test_first_structure_is_adopted(self):
        action, _ = msv.prefer(None, self._good(), None, None)
        assert action == "adopt"


# --- Test 11 / 21: verification is targeted, failures are classified ----

class TestTargetedVerification:
    def test_a_clean_structure_is_not_re_verified(self):
        verify, reason = msv.should_verify({"verified": True, "anomalies": []})
        assert verify is False
        assert "unchanged" in reason

    def test_an_anomaly_triggers_verification(self):
        verify, reason = msv.should_verify(
            {"verified": False, "anomalies": ["missing 7"]})
        assert verify is True

    def test_the_teacher_can_always_force_it(self):
        verify, _ = msv.should_verify({"verified": True, "anomalies": []},
                                      teacher_requested=True)
        assert verify is True

    @pytest.mark.parametrize("text,kind", [
        ("Deadline exceeded", msv.FAILURE_TIMEOUT),
        ("429 Too Many Requests quota", msv.FAILURE_RATE_LIMIT),
        ("503 Service Unavailable overloaded", msv.FAILURE_OVERLOAD),
        ("401 unauthenticated", msv.FAILURE_AUTH),
        ("400 invalid argument", msv.FAILURE_INVALID_REQUEST),
        ("failed to decode JSON", msv.FAILURE_MALFORMED),
    ])
    def test_failures_are_classified_separately(self, text, kind):
        assert msv.classify_failure(RuntimeError(text)) == kind

    def test_a_provider_outage_is_never_a_structural_problem(self):
        kind = msv.classify_failure(RuntimeError("503 overloaded"))
        assert kind in msv.TRANSIENT_FAILURES
        assert kind != msv.FAILURE_STRUCTURAL


# --- evidence formatting keeps page structure ---------------------------

class TestEvidencePrompt:
    def test_page_boundaries_are_preserved(self, pdfs):
        evidence = ms.analyse_document(pdfs["two_column"])
        rendered = msv.format_evidence(evidence)
        assert "--- Page 1" in rendered and "--- Page 2" in rendered
        assert "[p2b0 col=0]" in rendered
        assert "LOCAL STRUCTURE DIAGNOSTICS" in rendered

    def test_image_only_says_so_instead_of_sending_nothing(self, pdfs):
        evidence = ms.analyse_document(pdfs["image_only"])
        rendered = msv.format_evidence(evidence)
        assert "no text layer" in rendered

    def test_evidence_is_bounded(self, pdfs):
        evidence = ms.analyse_document(pdfs["two_column"])
        rendered = msv.format_evidence(evidence, max_chars=500)
        assert len(rendered) < 1200
        assert "truncated" in rendered


# --- the reason must be actionable, not "AI verification failed" -------

class TestLikelyCause:
    """§17: show the probable cause, not a generic failure message."""

    def test_a_clean_verification_has_nothing_to_explain(self):
        assert msv.likely_cause({"anomalies": [], "extraction_mode": ms.MODE_TEXT}) is None

    def test_image_only_names_the_missing_text_layer(self, pdfs):
        evidence = ms.analyse_document(pdfs["image_only"])
        structure = msv.canonicalize({"questions": [{"question_number": "1", "max_marks": 5}]})
        # WITH a discrepancy to explain: the absence of a text layer is the
        # probable cause of it.
        inventory = {"expected_count": 5, "detected_count": 1,
                     "missing_indices": [2, 3, 4, 5], "unresolved": True}
        result = msv.cross_validate(structure, evidence, inventory)
        assert result["discrepancies"]
        cause = msv.likely_cause(result)
        assert cause and "no text layer" in cause
        assert "AI verification failed" not in cause

    def test_a_clean_visual_read_has_nothing_to_explain(self, pdfs):
        """Nothing contradicts the structure, so there is no cause to name —
        "no text layer" is how it was read, not what went wrong."""
        evidence = ms.analyse_document(pdfs["image_only"])
        structure = msv.canonicalize({"questions": [{"question_number": "1", "max_marks": 5}]})
        clean = msv.cross_validate(structure, evidence, {
            "expected_count": 1, "detected_count": 1, "missing_indices": [],
            "unresolved": False})
        assert clean["verified"] is True
        assert msv.likely_cause(clean) is None

    def test_a_lost_block_is_blamed_on_extraction(self):
        cause = msv.likely_cause({
            "anomalies": ["The local document reconstruction found question number(s) "
                          "7 that are missing from the reconstructed structure — "
                          "extraction may have dropped blocks."],
            "extraction_mode": ms.MODE_TEXT})
        assert cause and "extraction" in cause and "lost question blocks" in cause

    def test_page_numbers_mistaken_for_questions_are_named(self):
        cause = msv.likely_cause({
            "anomalies": ["The reconstructed structure contains 6 question(s) the local "
                          "text evidence does not support — these may be page numbers "
                          "or mark values mistaken for questions."],
            "extraction_mode": ms.MODE_TEXT})
        assert cause and "page numbers" in cause

    def test_a_total_mismatch_blames_the_mark_allocation(self):
        cause = msv.likely_cause({
            "anomalies": ["Question marks add up to 20 but the mark scheme states 25 "
                          "— 5 mark(s) unaccounted for."],
            "extraction_mode": ms.MODE_TEXT})
        assert cause and "mark allocation" in cause


# --- a missing structure is a verdict, never a crash --------------------

class TestNoUsableStructure:
    """canonicalize() returns None when nothing usable came back. The
    diagnostics endpoint must still answer with a verdict, not a 500."""

    @pytest.mark.parametrize("value", [None, msv.canonicalize({"questions": []})])
    def test_cross_validate_survives_a_missing_structure(self, value):
        result = msv.cross_validate(value, {"mode": ms.MODE_TEXT})
        assert result["verified"] is False
        assert result["structural_confidence"] <= 0.5
        assert any("No questions were reconstructed" in a for a in result["anomalies"])

    def test_cross_validate_survives_missing_evidence(self):
        result = msv.cross_validate(None, None)
        assert result["verified"] is False
        assert result["evidence_agrees"] is None

    def test_the_endpoint_answers_even_with_an_empty_structure(self, client,
                                                              fresh_state):
        from app.engine import blueprint as bp
        scheme_dir = fresh_state / "mark_schemes"
        scheme_dir.mkdir(parents=True, exist_ok=True)
        path = scheme_dir / "cw-empty.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic")
        # A stored structure with no usable rows — e.g. a legacy cache written
        # before canonicalization existed. The endpoint must explain itself,
        # not 500 and not claim the structure was verified.
        bp.save_cached(bp.file_hash(path), {"questions": []}, model="test")

        response = client.get("/api/assignments/cw-empty/mark-scheme")
        assert response.status_code == 200
        data = response.json()
        assert data["has_mark_scheme"] is True
        assert data["processed"] is False
        assert data["warnings"], "the teacher must be told what to do next"
        assert data.get("verification", {}).get("verified") is not True