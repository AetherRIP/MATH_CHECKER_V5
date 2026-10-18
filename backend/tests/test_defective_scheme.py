"""Defective and contradictory mark schemes must never read as verified.

The browser workflow harness had to SKIP its "broken mark scheme" case in demo
mode, because the mock engine never opens the file: a COMPLETED job proved only
that the stub ran. The property it wanted to assert is decidable, though, at the
deterministic engine layer — no AI, no network, no keys — which is where these
tests assert it:

  * a corrupt / renamed PDF yields no usable structure, and no confident one;
  * a mark scheme whose own stated total contradicts its questions is flagged;
  * a structure the parser cannot trust is never reported HIGH;
  * a clean scheme stays HIGH (the false-positive guard); and
  * the API surface a teacher reads says the same thing.

Two of these started as genuine defects, both from ``str(None)`` being treated
as a real question number: ``validate()`` accepted ``question_number=None`` as
the string "None", and ``structure_report()`` then dropped it from the numbering
sequence, so a scheme whose questions were ALL unnumbered reported
``complete: True`` with confidence HIGH while ``validate()`` called the very same
blueprint unusable. The reassuring gate won. They are pinned below.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.engine import blueprint as bp


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True          # proves none of this needs an AI model
    save_config(cfg)
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def _q(number, marks):
    return {"question_number": number, "max_marks": marks}


#: A clean, internally consistent 6-mark paper used as the control.
CLEAN = [_q("1a", 1), _q("1b", 1), _q("1c", 1), _q("2", 3)]


def _minimal_pdf() -> bytes:
    """A real one-page PDF, so a refusal can be shown to be about content."""
    import io

    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(72, 720, "Question 1 (2 marks): 2 + 2")
    page.showPage()
    page.save()
    return buffer.getvalue()


def _confidence(questions, declared=None):
    report = bp.structure_report({"questions": questions})
    return report, bp.mark_scheme_confidence(report, declared_total=declared)


# --------------------------------------------------------------- the control

class TestACleanSchemeIsNotFlagged:
    """The guard against crying wolf: a good scheme must stay HIGH, and the
    confidence check must not spend an API call on it."""

    def test_clean_and_consistent_is_high_confidence(self):
        report, conf = _confidence(CLEAN, declared=6)
        assert report["total_marks"] == 6
        assert report["complete"] is True
        assert bp.validate({"questions": CLEAN}) is True
        assert conf["confidence"] == bp.CONFIDENCE_HIGH
        assert conf["requires_verification"] is False
        assert conf["reasons"] == []


# ------------------------------------------------------------- corrupt file

class TestACorruptSchemeIsRefused:

    def test_the_class_validator_refuses_a_renamed_non_pdf(self, tmp_path):
        """The same bytes the UI harness uploads, judged where it is decidable.

        ``validate_and_normalize`` REPORTS rather than raises; the worker is the
        caller that turns an all-invalid attachment set into
        ``InvalidSubmissionError``. So the assertion is on its verdict.
        """
        from app.classroom.submission_validation import validate_and_normalize
        result = validate_and_normalize("mark-scheme.pdf",
                                        b"this is not really a PDF\n",
                                        "application/pdf", tmp_path)
        assert result["ok"] is False
        assert "renamed" in result["reason"] or "corrupted" in result["reason"]

    def test_a_mislabeled_scheme_and_a_real_pdf_are_told_apart(self, tmp_path):
        """The refusal above must be about the bytes, not the file name."""
        from app.classroom.submission_validation import validate_and_normalize
        assert validate_and_normalize("scheme.pdf", _minimal_pdf(),
                                      "application/pdf", tmp_path)["ok"] is True

    def test_a_corrupt_scheme_yields_no_usable_structure(self, tmp_path):
        broken = tmp_path / "broken-scheme.pdf"
        broken.write_bytes(b"this is not really a PDF\n")
        blueprint, meta = bp.blueprint_for_mark_scheme(broken)
        assert blueprint is None
        assert meta.get("source") in ("not_processed", "unavailable")

    def test_no_structure_is_never_confident(self):
        report = bp.structure_report(None)
        conf = bp.mark_scheme_confidence(report)
        assert report["question_count"] == 0
        assert report["complete"] is False
        assert conf["confidence"] == bp.CONFIDENCE_NEEDS_REVIEW
        assert conf["requires_verification"] is True
        assert any("No questions" in r for r in conf["reasons"])


# ------------------------------------------------------- contradictory totals

class TestContradictoryTotalsAreFlagged:
    """The scheme states 7 but its questions add up to 6 (paper B's deliberate
    defect). Following it silently would shift every student's denominator."""

    def test_a_stated_total_that_disagrees_is_flagged(self):
        report, conf = _confidence(CLEAN, declared=7)
        assert report["total_marks"] == 6
        assert conf["confidence"] == bp.CONFIDENCE_NEEDS_REVIEW
        assert conf["requires_verification"] is True
        assert any("does not match" in r and "7" in r for r in conf["reasons"])

    def test_the_agreeing_total_is_not_flagged(self):
        _, conf = _confidence(CLEAN, declared=6)
        assert conf["requires_verification"] is False

    def test_no_stated_total_cannot_contradict_anything(self):
        _, conf = _confidence(CLEAN, declared=None)
        assert conf["requires_verification"] is False


# --------------------------------------------------- defective structures

#: (label, questions, declared total, structurally_defective)
#:
#: ``structurally_defective`` is the subset whose NUMBERING cannot be trusted,
#: which is what ``complete`` describes. The single-question/20-mark case is a
#: "too few questions" heuristic: its numbering is perfectly fine, so it is
#: flagged for verification without being called incomplete. Asserting
#: ``complete is False`` for that one would be testing my assumption rather than
#: the code.
DEFECTIVE = [
    ("no questions at all", [], None, True),
    ("zero marks", [_q("1", 0)], None, True),
    ("a gap in the numbering", [_q("1", 2), _q("3", 2)], None, True),
    ("duplicate question numbers", [_q("1", 2), _q("1", 2)], None, True),
    ("one question across a 20-mark paper", [_q("1", 20)], None, False),
    ("a question with no number key", [{"max_marks": 2}], None, True),
    ("a question numbered None", [{"question_number": None, "max_marks": 2}],
     None, True),
    ("a question numbered with blanks", [{"question_number": "   ",
                                          "max_marks": 2}], None, True),
    ("mixed numbered and unnumbered", [_q("1", 2), {"max_marks": 2}], None, True),
    ("marks missing entirely", [{"question_number": "1"}], None, True),
]


class TestDefectiveStructuresAreNeverHighConfidence:

    @pytest.mark.parametrize("label,questions,declared,structural", DEFECTIVE,
                             ids=[c[0] for c in DEFECTIVE])
    def test_never_high_and_always_worth_a_check(self, label, questions,
                                                 declared, structural):
        report, conf = _confidence(questions, declared=declared)
        assert conf["confidence"] != bp.CONFIDENCE_HIGH, label
        assert conf["requires_verification"] is True, label
        assert conf["reasons"], label
        if structural:
            # A structure whose numbering cannot be checked is never "complete".
            assert report["complete"] is False, label

    def test_the_unnumbered_regression(self):
        """``str(None)`` used to become the question number "None": validate()
        accepted it, and structure_report() then reported the structure as
        complete with confidence HIGH."""
        questions = [{"question_number": None, "max_marks": 2}]
        report, conf = _confidence(questions)
        assert report["unnumbered_questions"] == 1
        assert "None" not in report["numbering"]
        assert report["complete"] is False
        assert conf["confidence"] == bp.CONFIDENCE_NEEDS_REVIEW
        assert bp.validate({"questions": questions}) is False

    def test_unnumbered_questions_are_counted_and_described(self):
        report = bp.structure_report({"questions": [_q("1", 2), {"max_marks": 2}]})
        assert report["unnumbered_questions"] == 1
        warnings = bp.validation_warnings(report)
        assert any("no question number" in w for w in warnings)

    def test_a_number_that_is_not_a_number_is_still_unnumbered(self):
        report = bp.structure_report(
            {"questions": [{"question_number": "   ", "max_marks": 2}]})
        assert report["unnumbered_questions"] == 1
        assert bp.validate({"questions": [{"question_number": "   ",
                                           "max_marks": 2}]}) is False


# ------------------------------------------------------------- API surface

class TestTheApiAgreesWithTheEngine:
    """The screen a teacher reads must not say "verified" about a broken scheme.

    Deterministic and offline, so it holds in demo mode — which is what makes
    the browser harness's skipped case assertable here.
    """

    def _write_scheme(self, data_dir, coursework_id, content):
        schemes = data_dir / "mark_schemes"
        schemes.mkdir(parents=True, exist_ok=True)
        path = schemes / f"{coursework_id}.pdf"
        path.write_bytes(content)
        return path

    def test_a_corrupt_scheme_is_not_reported_as_markable(self, client, fresh_state):
        self._write_scheme(fresh_state, "cw-broken", b"this is not really a PDF\n")
        data = client.get("/api/assignments/cw-broken/mark-scheme")
        assert data.status_code == 200
        body = data.json()
        assert body["has_mark_scheme"] is True
        assert body["processed"] is False
        assert body["validation"]["question_count"] == 0
        assert not body.get("questions")
        assert body.get("confidence") != bp.CONFIDENCE_HIGH
        assert any("not been processed" in w for w in body["warnings"])

    def test_an_absent_scheme_is_not_reported_as_markable(self, client):
        body = client.get("/api/assignments/cw-none/mark-scheme").json()
        assert body["has_mark_scheme"] is False
        assert body["processed"] is False
        assert body["validation"]["question_count"] == 0
        assert any("No mark scheme" in w for w in body["warnings"])

    def test_a_defective_manual_override_is_rejected(self, client, fresh_state):
        """The teacher's own repair path must not accept a structure the engine
        would refuse — otherwise the gate can be bypassed by typing into it."""
        self._write_scheme(fresh_state, "cw-fix", b"%PDF-1.4 not a real body\n")
        response = client.post(
            "/api/assignments/cw-fix/mark-scheme/override",
            json={"questions": [{"question_number": None, "max_marks": 2}]})
        assert response.status_code == 400
        assert response.json()["code"] == "bad_override"

    def test_a_sound_manual_override_is_accepted(self, client, fresh_state):
        """The rejection above must be about the defect, not about overrides."""
        self._write_scheme(fresh_state, "cw-good", b"%PDF-1.4 not a real body\n")
        response = client.post(
            "/api/assignments/cw-good/mark-scheme/override",
            json={"questions": [_q("1", 3), _q("2", 4)], "total_marks": 7})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["processed"] is True
        assert body["validation"]["question_count"] == 2
        assert body["totals_agree"] is True
