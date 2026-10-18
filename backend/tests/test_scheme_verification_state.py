"""Verification state, persistence, completeness gating and marks reconciliation.

The other suites pin the *reading* of a mark scheme (page-scoped inventory) and
the *diagnostics* of a structure. This file pins what happens to that knowledge
afterwards, which is where the product used to lie:

* a verdict that was computed and then lost (nothing persisted);
* a badge that said "verified" while the stored completeness said otherwise;
* a recheck button that re-ran the identical request on every click;
* a mark total that double-counted sub-parts or quietly dropped unread marks;
* a source-page viewer that had nothing to point at.

Everything here is offline: reportlab/PIL fixtures and a scripted client.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_config
from app.engine import blueprint as bp
from app.engine import mark_scheme as ms
from app.engine import mark_scheme_verify as msv

from fakes_gemini import FakeClient, Response, payload
from fixtures_mark_scheme import (
    compiled_image_only_mark_scheme, image_only_mark_scheme,
    page_break_mark_scheme,
)


@pytest.fixture
def client(fresh_state):
    """Offline API client, as the other mark-scheme suites build it."""
    from app.config import save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def questions(count: int, marks: int = 4) -> dict:
    return {"questions": [{"question_number": str(n), "max_marks": marks}
                          for n in range(1, count + 1)]}


def clean_completeness(count: int = 4, marks: int = 4) -> dict:
    return {
        "detected_count": count,
        "expected_count": count,
        "expected_basis": "the highest printed index",
        "observed_last_index": count,
        "missing_indices": [],
        "conflicting_indices": [],
        "failed_chunks": [],
        "pages_without_items": [],
        "unkeyed_items": 0,
        "marks_missing_indices": [],
        "items_total_marks": count * marks,
        "unresolved": False,
        "verdict": "COMPLETE",
    }


def record_verified(path, blueprint: dict | None = None,
                    completeness: dict | None = None) -> str:
    sha = bp.file_hash(path)
    bp.save_record(sha, blueprint or questions(4), model="test",
                   completeness=completeness or clean_completeness(),
                   verification={"state": bp.STATUS_VERIFIED,
                                 "updated_at": 1.0})
    return sha


# ------------------------------------------------------- the fingerprint gate

class TestNoPointlessReverification:
    """§21/§23: re-running an identical request cannot change its own answer."""

    def test_a_clean_unchanged_structure_is_not_verified_again(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        record_verified(pdf)
        run, reason, state = bp.recheck_decision(pdf)
        assert run is False
        assert state == bp.STATUS_VERIFIED
        assert "unchanged" in reason

    def test_a_manual_edit_earns_a_new_verification(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        sha = record_verified(pdf)
        edited = questions(5)
        bp.save_record(sha, edited, model="manual")
        # The badge must not survive an edit it never looked at...
        assert bp.verification_status(bp.load_record(sha)) \
            == bp.STATUS_NOT_VERIFIED
        # ...and the recheck must say why, not just "something failed".
        run, reason, _ = bp.recheck_decision(pdf)
        assert run is True, "an edited structure is not the one that was verified"
        assert "changed" in reason

    def test_a_manual_review_verdict_is_not_repeated(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        sha = record_verified(pdf)
        bp.save_record(sha, questions(4),
                       verification={"state": bp.STATUS_NEEDS_MANUAL_REVIEW})
        run, reason, state = bp.recheck_decision(pdf)
        assert run is False
        assert state == bp.STATUS_NEEDS_MANUAL_REVIEW
        assert "person must decide" in reason

    def test_a_missing_structure_is_always_worth_reading(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        run, reason, _ = bp.recheck_decision(pdf)
        assert run is True and "no stored structure" in reason

    def test_a_skipped_recheck_sends_no_request(self, tmp_path):
        """The decisive assertion: zero calls to the model."""
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        record_verified(pdf)
        client = FakeClient(responder=lambda *_: pytest.fail(
            "an unchanged verified scheme must not be re-read"))
        outcome = bp.recheck_blueprint(client, pdf)
        assert outcome["skipped"] is True
        assert outcome["state"] == bp.STATUS_VERIFIED
        assert client.models.calls == []

    def test_a_second_concurrent_recheck_is_refused(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        sha = bp.file_hash(pdf)
        assert bp._begin_recheck(sha) is True
        try:
            client = FakeClient(responder=lambda *_: pytest.fail(
                "a second identical verification must not start"))
            outcome = bp.recheck_blueprint(client, pdf)
            assert outcome["busy"] is True
            assert "already running" in outcome["error"]
            assert client.models.calls == []
        finally:
            bp._end_recheck(sha)


# -------------------------------------------------------------- persistence

class TestVerdictSurvivesALaterWrite:
    """A structure written later must not erase the knowledge about it."""

    def test_a_plain_structure_write_keeps_the_inventory(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        sha = bp.file_hash(pdf)
        bp.save_record(sha, questions(3), model="first",
                       inventory={"expected_count": 3, "items": []},
                       completeness=clean_completeness(3),
                       verification={"state": bp.STATUS_VERIFIED})
        bp.save_cached(sha, questions(3), model="second")   # the old write path
        record = bp.load_record(sha)
        assert record["completeness"]["expected_count"] == 3
        assert record["inventory"]["expected_count"] == 3
        assert bp.verification_status(record) == bp.STATUS_VERIFIED

    def test_the_fingerprint_moves_with_the_structure(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        sha = bp.file_hash(pdf)
        first = bp.document_fingerprint(sha, questions(4))
        assert first == bp.document_fingerprint(sha, questions(4))
        assert first != bp.document_fingerprint(sha, questions(5))
        assert first != bp.document_fingerprint(sha, {"questions": [
            {"question_number": "1", "max_marks": 5}]})

    def test_an_unknown_state_is_not_verified(self, tmp_path):
        assert bp.verification_status(None) == bp.STATUS_NOT_VERIFIED
        assert bp.verification_status({"verification": {"state": "nonsense"}}) \
            == bp.STATUS_NOT_VERIFIED

    def test_an_empty_verdict_is_unknown_not_complete(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        assert bp.completeness_of(pdf) == {}
        review = bp.review_requirement(pdf)
        assert review == {"known": False, "incomplete": False, "warnings": []}


# ----------------------------------------------------- completeness as a gate

class TestCompletenessGatesTheBadge:
    def test_a_missing_printed_index_is_critical(self):
        inventory = {"expected_count": 5, "observed_last_index": 5,
                     "missing_indices": [2], "detected_count": 4}
        validation = msv.cross_validate(questions(5), {"mode": ms.MODE_TEXT},
                                        inventory)
        assert validation["verified"] is False
        assert validation["structural_confidence"] <= 0.4
        assert validation["confidence_basis"]["inventory_incomplete"] is True
        assert validation["confidence_basis"]["capped_by_incompleteness"] == 0.4
        assert any("did not come back" in a for a in validation["anomalies"])
        kinds = [r["kind"] for r in validation["anomaly_records"]]
        assert "missing_question" in kinds
        missing = next(r for r in validation["anomaly_records"]
                       if r["kind"] == "missing_question")
        assert missing["severity"] == "critical"
        assert missing["needs_human"] is True
        assert missing["repaired"] is False

    def test_a_label_variant_is_information_not_a_discrepancy(self):
        inventory = {**clean_completeness(5),
                     "label_variant_indices": [4],
                     "label_variants": [{"source_index": 4,
                                         "labels": ["June 05/4H/Q4", "Q4"],
                                         "chunks": ["p1-6", "p6-11"],
                                         "page": 2}]}
        validation = msv.cross_validate(questions(5), {"mode": ms.MODE_TEXT},
                                        inventory)
        assert validation["anomalies"] == []
        assert validation["verified"] is True
        records = [r for r in validation["anomaly_records"]
                   if r["kind"] == "label_variant"]
        assert records and records[0]["severity"] == "info"
        assert records[0]["needs_human"] is False
        assert validation["checks"]["inventory"]["label_variant_indices"] == [4]

    def test_a_failed_page_range_is_reported(self):
        inventory = {"expected_count": 5, "missing_indices": [],
                     "failed_chunks": ["p6-10"], "detected_count": 5}
        validation = msv.cross_validate(questions(5), {"mode": ms.MODE_TEXT},
                                        inventory)
        assert validation["verified"] is False
        assert any("failed their read gates" in a for a in validation["anomalies"])

    def test_an_unestablished_count_is_reported(self):
        inventory = {"expected_count": None, "detected_count": 3}
        validation = msv.cross_validate(questions(3), {"mode": ms.MODE_TEXT},
                                        inventory)
        assert any("does not establish how many" in a
                   for a in validation["anomalies"])

    def test_a_complete_inventory_adds_no_anomaly(self):
        inventory = dict(clean_completeness(5))
        validation = msv.cross_validate(questions(5), {"mode": ms.MODE_TEXT},
                                        inventory)
        assert validation["anomalies"] == []
        assert validation["verified"] is True

    def test_visual_evidence_cannot_claim_text_quality(self):
        evidence = {"mode": ms.MODE_IMAGE_ONLY}
        confident = questions(3)
        confident["model_confidence"] = 0.95
        validation = msv.cross_validate(confident, evidence,
                                        clean_completeness(3))
        assert validation["evidence_kind"] == msv.EVIDENCE_VISUAL
        # A scan that only a vision model read cannot score like a text layer.
        assert validation["confidence_basis"]["capped_by_evidence"] == 0.75
        assert validation["structural_confidence"] <= 0.75

    def test_a_visual_read_is_not_a_discrepancy(self):
        """An image-only scheme has no text layer for ever. When every page was
        read and every printed index accounted for, that property must not
        veto the verdict — it caps the confidence and is stated instead."""
        evidence = {"mode": ms.MODE_IMAGE_ONLY}
        validation = msv.cross_validate(questions(4), evidence,
                                        clean_completeness(4))
        assert validation["discrepancies"] == []
        assert validation["verified"] is True
        assert validation["evidence_notes"]
        assert any("no text layer" in n for n in validation["evidence_notes"])
        # It is still visible to the teacher, and it is not a call to action.
        note = next(r for r in validation["anomaly_records"]
                    if r["kind"] == "image_only_evidence")
        assert note["needs_human"] is False
        assert validation["confidence_basis"]["evidence_notes"] == 1

    @pytest.mark.parametrize("mode,kind", [
        (ms.MODE_TEXT, msv.EVIDENCE_COMBINED),
        (ms.MODE_IMAGE_ONLY, msv.EVIDENCE_VISUAL),
        (ms.MODE_NONE, msv.EVIDENCE_NONE),
    ])
    def test_the_kind_of_evidence_is_reported_faithfully(self, mode, kind):
        assert msv.evidence_kind({"mode": mode}) == kind

    def test_a_real_discrepancy_is_still_critical_beside_a_note(self):
        evidence = {"mode": ms.MODE_IMAGE_ONLY}
        inventory = {**clean_completeness(4), "missing_indices": [3],
                     "unresolved": True}
        validation = msv.cross_validate(questions(4), evidence, inventory)
        assert validation["verified"] is False
        assert len(validation["discrepancies"]) >= 1
        assert any("did not come back" in d for d in validation["discrepancies"])

    def test_no_evidence_is_not_unreadable_evidence(self):
        assert msv.evidence_kind(None) is None


# --------------------------------------------------------- marks reconciliation

class TestMarksAreCountedOnce:
    def test_a_question_with_parts_is_summed_from_its_parts(self):
        blueprint = {"questions": [{
            "question_number": "1", "max_marks": 6,
            "parts": [{"number": "(a)", "max_marks": 2},
                      {"number": "(b)", "max_marks": 4}]}]}
        out = bp.reconcile_marks(blueprint, {"items_total_marks": 6})
        assert out["document_total"] == 6, "the parent total must not be added too"
        assert out["per_question"][0]["summed_from_parts"] is True
        assert out["unresolved"] is False

    def test_a_duplicated_printed_index_is_unresolved(self):
        blueprint = {"questions": [
            {"question_number": "1", "max_marks": 4, "source_index": 1},
            {"question_number": "1", "max_marks": 4, "source_index": 1}]}
        out = bp.reconcile_marks(blueprint)
        assert out["duplicate_keys"] == ["1"]
        assert out["unresolved"] is True

    def test_marks_that_could_not_be_read_are_not_zero(self):
        blueprint = {"questions": [{"question_number": "1", "max_marks": 0}]}
        out = bp.reconcile_marks(blueprint)
        assert out["unread_marks"] == ["1"]
        assert out["unresolved"] is True

    def test_a_total_that_disagrees_with_the_source_is_unresolved(self):
        out = bp.reconcile_marks(questions(3, 4), {"items_total_marks": 20})
        assert out["document_total"] == 12
        assert out["inventory_total"] == 20
        assert out["unresolved"] is True


# --------------------------------------------- the page-scoped pass persists

@pytest.fixture
def two_page_inventory(tmp_path):
    """A 2-page image-only scheme whose printed index runs 1..4."""
    return compiled_image_only_mark_scheme(tmp_path / "compiled.pdf",
                                           pages=2, per_page=2)


def item(index: int, page: int, marks: int = 4) -> dict:
    return {"source_index": index, "question_number": str(index),
            "page": page, "max_marks": marks,
            "criteria": "M1 method, A1 answer"}


class TestPageScopedVerificationPersists:
    def test_a_complete_read_is_verified_and_persisted(self, two_page_inventory):
        pages = [[item(1, 1), item(2, 1)], [item(3, 2), item(4, 2)]]
        calls: list[int] = []

        def responder(contents, model, config):
            calls.append(1)
            return Response(payload(pages[len(calls) - 1]))

        pdf = two_page_inventory
        outcome = bp.verify_with_inventory(FakeClient(responder=responder), pdf,
                                           window=1, overlap=0, dpi=100)
        assert outcome["ok"] is True, outcome
        assert outcome["state"] == bp.STATUS_VERIFIED
        assert outcome["completeness"]["missing_indices"] == []
        assert outcome["completeness"]["expected_count"] == 4

        record = bp.load_record(bp.file_hash(pdf))
        assert record["verification"]["state"] == bp.STATUS_VERIFIED
        assert record["completeness"]["unresolved"] is False
        assert record["evidence_kind"] == msv.EVIDENCE_VISUAL
        assert record["inventory"]["items"], "per-item evidence must be kept"
        # Both readings of any disputed index travel with the report, so
        # "flagged for review" is something the reviewer can actually see.
        assert record["inventory"]["conflicts"] == []
        assert record["inventory"]["label_variants"] == []
        assert record["reconciliation"]["document_total"] == 16
        assert record["reconciliation"]["unresolved"] is False
        # The structure is now the page-scoped one, with printed evidence.
        rows = record["blueprint"]["questions"]
        assert [r["question_number"] for r in rows] == ["1", "2", "3", "4"]
        assert all(r.get("page") for r in rows)
        assert bp.review_requirement(pdf)["warnings"] == []

    def test_a_missing_index_blocks_verified(self, two_page_inventory):
        pages = [[item(1, 1), item(2, 1)], [item(4, 2)]]
        calls: list[int] = []

        def responder(contents, model, config):
            calls.append(1)
            return Response(payload(pages[len(calls) - 1]))

        pdf = two_page_inventory
        outcome = bp.verify_with_inventory(FakeClient(responder=responder), pdf,
                                           window=1, overlap=0, dpi=100)
        assert outcome["ok"] is True
        assert outcome["state"] == bp.STATUS_NEEDS_MANUAL_REVIEW
        assert outcome["completeness"]["missing_indices"] == [3]
        assert outcome["completeness"]["unresolved"] is True
        review = bp.review_requirement(pdf)
        assert review["incomplete"] is True
        assert any("missing question index" in w for w in review["warnings"])

    def test_an_unreadable_page_range_is_not_verified(self, two_page_inventory):
        def responder(contents, model, config):
            if not calls:
                calls.append(1)
                return Response(payload([item(1, 1), item(2, 1)]))
            return Response("this is not json", "STOP")

        calls: list[int] = []
        pdf = two_page_inventory
        outcome = bp.verify_with_inventory(FakeClient(responder=responder), pdf,
                                           window=1, overlap=0, dpi=100)
        assert outcome["ok"] is True
        assert outcome["state"] == bp.STATUS_NEEDS_MANUAL_REVIEW
        assert outcome["completeness"]["failed_chunks"], \
            "a page range that failed its gates must be named"
        assert any("could not be read" in w
                   for w in bp.review_requirement(pdf)["warnings"])

    def test_a_concurrent_page_scoped_pass_is_refused(self, two_page_inventory):
        pdf = two_page_inventory
        sha = bp.file_hash(pdf)
        assert bp._begin_recheck(sha) is True
        try:
            outcome = bp.verify_with_inventory(
                FakeClient(responder=lambda *_: pytest.fail(
                    "a second page-scoped pass must not start")),
                pdf, window=1, overlap=0, dpi=100)
            assert outcome["busy"] is True
        finally:
            bp._end_recheck(sha)


# ------------------------------------------------- the marking path says so

class TestMarkingCarriesTheVerdict:
    def test_an_incomplete_structure_is_reported_to_the_marker(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        sha = bp.file_hash(pdf)
        bp.save_record(
            sha, questions(4), model="test",
            completeness={**clean_completeness(4),
                          "expected_count": 6,
                          "missing_indices": [5, 6],
                          "unresolved": True,
                          "verdict": "INCOMPLETE"},
            verification={"state": bp.STATUS_NEEDS_MANUAL_REVIEW})
        cached, meta = bp.blueprint_for_mark_scheme(pdf)
        assert cached is not None
        assert meta["source"] == "cache"
        assert meta["verification_state"] == bp.STATUS_NEEDS_MANUAL_REVIEW
        assert meta["completeness"]["missing_indices"] == [5, 6]
        assert meta["review"]["warnings"], \
            "marking against an incomplete scheme must warn"


# ----------------------------------------------------------- the API surface

class TestAFreshlyBuiltStructureKeepsTheVerdict:
    """A new extraction of the same document does not undo what was established
    about it: the completeness record and its warnings travel with the result the
    marking path reads."""

    def test_the_stored_verdict_reaches_the_result(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "scheme.pdf")
        sha = bp.file_hash(pdf)
        bp.save_record(
            sha, questions(3), model="earlier",
            completeness={**clean_completeness(3), "expected_count": 5,
                          "missing_indices": [4, 5], "unresolved": True,
                          "verdict": "INCOMPLETE"},
            verification={"state": bp.STATUS_NEEDS_MANUAL_REVIEW})
        client = FakeClient(script=[Response(payload(
            [{"question_number": "1", "max_marks": 3},
             {"question_number": "2", "max_marks": 4},
             {"question_number": "3", "max_marks": 5}]))])
        blueprint, meta = bp.get_blueprint(client, pdf, force=True)
        assert meta["source"] == "built"
        assert blueprint is not None
        assert meta["completeness"]["missing_indices"] == [4, 5]
        assert meta["verification_state"] == bp.STATUS_NEEDS_MANUAL_REVIEW
        assert meta["review"]["warnings"], \
            "the marking path must see WHY this structure is incomplete"


class TestDiagnosticsNeverOverclaims:
    def test_api_reports_the_stored_verdict(self, client, fresh_state, tmp_path):
        from app.config import get_config as cfg
        cfg().mock_mode = True
        scheme_dir = fresh_state / "mark_schemes"
        scheme_dir.mkdir(parents=True, exist_ok=True)
        source = page_break_mark_scheme(tmp_path / "src.pdf")
        path = scheme_dir / "cw-verdict.pdf"
        path.write_bytes(source.read_bytes())
        sha = bp.file_hash(path)
        bp.save_record(
            sha, questions(4), model="test",
            completeness={**clean_completeness(4), "expected_count": 5,
                          "missing_indices": [3], "unresolved": True,
                          "verdict": "INCOMPLETE"},
            verification={"state": bp.STATUS_VERIFIED})

        data = client.get("/api/assignments/cw-verdict/mark-scheme").json()
        # A stored VERIFIED that the completeness record contradicts must not be
        # shown as VERIFIED: the badge follows the evidence, not the old claim.
        assert data["verification_state"] == bp.STATUS_NEEDS_MANUAL_REVIEW
        assert data["completeness"]["missing_indices"] == [3]
        assert data["verification"]["verified"] is False
        assert data["requires_verification"] is True
        assert data["marking_warning"], "the teacher must see this before marking"
        assert any("3" in w for w in data["marking_warning"])

    def test_api_never_calls_it_verified_without_a_page_scoped_read(
            self, client, fresh_state, tmp_path):
        """A visually-read structure with no completeness verdict is exactly the
        case that used to look verified because nothing contradicted it."""
        get_config().mock_mode = True
        scheme_dir = fresh_state / "mark_schemes"
        scheme_dir.mkdir(parents=True, exist_ok=True)
        source = image_only_mark_scheme(tmp_path / "src.pdf")
        path = scheme_dir / "cw-unscanned.pdf"
        path.write_bytes(source.read_bytes())
        bp.save_record(bp.file_hash(path), questions(4), model="test")

        data = client.get("/api/assignments/cw-unscanned/mark-scheme").json()
        assert data["verification"]["verified"] is False, \
            "no page-scoped read has established the count yet"
        assert data["verification"]["evidence_kind"] == msv.EVIDENCE_VISUAL
        assert data["verification"]["evidence_notes"]
        assert data["verification"]["discrepancies"] == []
        assert data["completeness"] == {}

    def test_api_confidence_explains_itself(self, client, fresh_state, tmp_path):
        get_config().mock_mode = True
        scheme_dir = fresh_state / "mark_schemes"
        scheme_dir.mkdir(parents=True, exist_ok=True)
        source = page_break_mark_scheme(tmp_path / "src.pdf")
        path = scheme_dir / "cw-basis.pdf"
        path.write_bytes(source.read_bytes())
        sha = bp.file_hash(path)
        bp.save_record(sha, questions(4), model="test",
                       completeness=clean_completeness(4),
                       verification={"state": bp.STATUS_VERIFIED})
        data = client.get("/api/assignments/cw-basis/mark-scheme").json()
        basis = data["verification"]["confidence_basis"]
        assert "evidence_kind" in basis
        assert "anomalies" in basis
        assert data["verification"]["evidence_kind"] in (
            msv.EVIDENCE_COMBINED, msv.EVIDENCE_VISUAL, msv.EVIDENCE_NONE, None)
        assert data["reconciliation"]["document_total"] == 16


class TestSourcePageEndpoints:
    def _assignment(self, fresh_state, tmp_path):
        scheme_dir = fresh_state / "mark_schemes"
        scheme_dir.mkdir(parents=True, exist_ok=True)
        source = page_break_mark_scheme(tmp_path / "src.pdf")
        path = scheme_dir / "cw-pages.pdf"
        path.write_bytes(source.read_bytes())
        return path

    def test_pages_endpoint_lists_every_source_page(self, client, fresh_state,
                                                     tmp_path):
        get_config().mock_mode = True
        path = self._assignment(fresh_state, tmp_path)
        sha = bp.file_hash(path)
        bp.save_record(
            sha, questions(2), model="test",
            inventory={"items": [{"source_index": 5, "label": "5",
                                  "max_marks": 3, "page": 1},
                                 {"source_index": 6, "label": "6",
                                  "max_marks": 4, "page": 1}],
                       "expected_count": 2,
                       # Both readings travel to the viewer, not just the count.
                       "conflicts": [{"source_index": 6,
                                      "kind": "chunks_disagree",
                                      "chunks": ["p1-6", "p6-11"],
                                      "variants": [
                                          {"chunk": "p1-6", "max_marks": 4,
                                           "parts_detail": [["(a)", 2]], "page": 1},
                                          {"chunk": "p6-11", "max_marks": 5,
                                           "parts_detail": [["(a)", 3]], "page": 1}]}],
                       "label_variants": [{"source_index": 5, "kind": "label",
                                           "labels": ["5", "Q5"], "page": 1}]},
            completeness={**clean_completeness(2), "pages_without_items": [2],
                          "conflicting_indices": [6]},
            verification={"state": bp.STATUS_VERIFIED})
        data = client.get("/api/assignments/cw-pages/mark-scheme/pages").json()
        assert data["page_count"] == 2
        assert [p["page"] for p in data["pages"]] == [1, 2]
        assert data["pages"][0]["indices"] == [5, 6]
        assert data["pages"][0]["has_items"] is True
        # A page the read returned nothing from is the one to look at.
        assert data["pages"][1]["indices"] == []
        assert data["pages"][1]["needs_review"] is True
        assert data["page_url_template"].endswith("/page/{page}.png")
        assert data["verification_state"] == bp.STATUS_VERIFIED
        assert data["conflicts"][0]["source_index"] == 6
        assert len(data["conflicts"][0]["variants"]) == 2
        assert data["label_variants"][0]["source_index"] == 5

    def test_a_page_is_served_as_png_and_cached(self, client, fresh_state,
                                                tmp_path):
        get_config().mock_mode = True
        self._assignment(fresh_state, tmp_path)
        first = client.get("/api/assignments/cw-pages/mark-scheme/page/1.png")
        assert first.status_code == 200
        assert first.headers["content-type"] == "image/png"
        assert first.content.startswith(b"\x89PNG")
        etag = first.headers.get("etag")
        assert etag
        second = client.get("/api/assignments/cw-pages/mark-scheme/page/1.png",
                            headers={"If-None-Match": etag})
        assert second.status_code == 304, "a cached page must not be re-rendered"

    def test_a_page_that_does_not_exist_is_a_404(self, client, fresh_state,
                                                 tmp_path):
        get_config().mock_mode = True
        self._assignment(fresh_state, tmp_path)
        response = client.get("/api/assignments/cw-pages/mark-scheme/page/9.png")
        assert response.status_code == 404
        assert response.json()["code"] == "page_out_of_range"
        assert "does not exist" in response.json()["message"]


class TestRecheckEndpoint:
    def test_an_unchanged_verified_scheme_is_returned_not_reread(
            self, client, fresh_state, tmp_path):
        cfg = get_config()
        cfg.mock_mode = False           # so the recheck path is reachable at all
        scheme_dir = fresh_state / "mark_schemes"
        scheme_dir.mkdir(parents=True, exist_ok=True)
        source = page_break_mark_scheme(tmp_path / "src.pdf")
        path = scheme_dir / "cw-skip.pdf"
        path.write_bytes(source.read_bytes())
        record_verified(path)

        response = client.post("/api/assignments/cw-skip/mark-scheme/recheck")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["recheck"]["skipped"] is True
        assert data["recheck"]["state"] == bp.STATUS_VERIFIED
        assert data["recheck"]["passes"] == []
        assert "unchanged" in data["recheck"]["reason"]
