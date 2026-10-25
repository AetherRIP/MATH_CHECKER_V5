"""Mark-scheme validation, manual override and real recheck (Part F).

Locks the behaviour that stops an incomplete extraction (e.g. 25 questions
detected when the paper has 33) from silently shrinking the denominator:
  * deterministic structure inspection (gaps / duplicates / totals),
  * a recheck that actually RE-EXTRACTS and compares passes,
  * a validated manual override the teacher can save.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def _questions(n, *, start=1, marks=1):
    return [
        {"question_number": str(start + i), "max_marks": marks,
         "criteria": "M1 A1"}
        for i in range(n)
    ]


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, texts):
        self._texts = list(texts)

    def generate_content(self, **kwargs):
        text = self._texts.pop(0) if self._texts else "{}"
        return _FakeResponse(text)


class _FakeClient:
    """Returns a different extraction on each call (to simulate disagreement)."""

    def __init__(self, texts):
        self.models = _FakeModels(texts)

    def upload_file(self, path):
        return "remote-blueprint"

    def files_delete(self, remote):
        pass


class TestStructureReport:
    def test_counts_and_totals(self):
        from app.engine import blueprint as bp
        report = bp.structure_report({"questions": _questions(3, marks=5)})
        assert report["question_count"] == 3
        assert report["total_marks"] == 15
        assert report["complete"] is True

    def test_detects_missing_question_numbers(self):
        from app.engine import blueprint as bp
        questions = _questions(25)
        # Numbers 1..25 but 26, 27, 28 jumped to 29, 30 -> gaps internals
        questions += [
            {"question_number": "29", "max_marks": 2, "criteria": ""},
            {"question_number": "30", "max_marks": 2, "criteria": ""},
        ]
        report = bp.structure_report({"questions": questions})
        assert report["gaps"] == ["26", "27", "28"]
        assert report["has_gaps"] is True
        assert report["complete"] is False

    def test_detects_duplicates_and_missing_marks(self):
        from app.engine import blueprint as bp
        questions = [
            {"question_number": "1", "max_marks": 3, "criteria": ""},
            {"question_number": "1", "max_marks": 3, "criteria": ""},
            {"question_number": "2", "max_marks": 0, "criteria": ""},
        ]
        report = bp.structure_report({"questions": questions})
        assert report["duplicate_numbers"] == ["1"]
        assert report["missing_max_marks"] == ["2"]

    def test_warnings_report_expected_vs_found(self):
        from app.engine import blueprint as bp
        report = bp.structure_report({"questions": _questions(25)})
        warnings = bp.validation_warnings(report, expected_count=33,
                                          expected_total=75)
        joined = " ".join(warnings)
        assert "25 question" in joined
        assert "33 were expected" in joined


class TestManualOverride:
    def test_override_persists_and_is_read_back(self, fresh_state):
        from app.engine import blueprint as bp
        path = fresh_state / "scheme.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic")
        sha = bp.file_hash(path)
        saved = bp.save_manual_blueprint(
            sha, _questions(33, marks=2), total_marks=66, note="teacher fixed")
        assert saved["manual_override"] is True
        loaded, meta = bp.blueprint_for_mark_scheme(path)
        assert meta["manual_override"] is True
        assert len(loaded["questions"]) == 33
        assert loaded["total_marks"] == 66

    def test_override_rejects_inconsistent_total(self, fresh_state):
        from app.engine import blueprint as bp
        path = fresh_state / "scheme2.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic")
        sha = bp.file_hash(path)
        with pytest.raises(ValueError):
            bp.save_manual_blueprint(sha, _questions(3, marks=5), total_marks=99)

    def test_override_rejects_duplicate_numbers(self, fresh_state):
        from app.engine import blueprint as bp
        path = fresh_state / "scheme3.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic")
        sha = bp.file_hash(path)
        with pytest.raises(ValueError):
            bp.save_manual_blueprint(
                sha, [{"question_number": "1", "max_marks": 1, "criteria": ""},
                      {"question_number": "1", "max_marks": 1, "criteria": ""}])


class TestRecheck:
    def test_recheck_reextracts_and_reports_disagreement(self, fresh_state, monkeypatch):
        from app.config import get_config
        from app.engine import blueprint as bp

        get_config().mock_mode = False
        path = fresh_state / "recheck.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic")

        # Pass 1 returns 25 questions, pass 2 returns 33 (the reported bug).
        pass_one = json.dumps({"paper": "P1", "total_marks": 25,
                               "questions": _questions(25)})
        pass_two = json.dumps({"paper": "P1", "total_marks": 33,
                               "questions": _questions(33)})
        fake = _FakeClient([pass_one, pass_two])

        outcome = bp.recheck_blueprint(fake, path, passes=2)
        assert outcome["ok"] is True
        assert outcome["disagreement"] is True
        # The most complete pass is kept.
        assert outcome["validation"]["question_count"] == 33
        assert outcome["previous"]["question_count"] == 0  # nothing cached before
        assert any("disagreed" in w.lower() for w in outcome["warnings"])


class TestValidationApi:
    def _seed_scheme(self, coursework_id, data_dir):
        from app.engine import blueprint as bp
        scheme_dir = data_dir / "mark_schemes"
        scheme_dir.mkdir(parents=True, exist_ok=True)
        path = scheme_dir / f"{coursework_id}.pdf"
        path.write_bytes(b"%PDF-1.4 synthetic")
        bp.save_cached(bp.file_hash(path),
                       {"questions": _questions(3, marks=4), "total_marks": 12},
                       model="test")
        return path

    def test_no_mark_scheme_reported(self, client):
        data = client.get("/api/assignments/cw-x/mark-scheme").json()
        assert data["has_mark_scheme"] is False
        assert data["processed"] is False
        # Nothing is attached, so there is no file to name — not an empty
        # string that a page would render as a scheme called "".
        assert data["file_name"] is None
        assert data["pages"] is None

    def test_the_attached_file_is_named_with_its_page_count(self, client, fresh_state):
        """The Classroom card could only offer an upload button, so a teacher
        could not tell which scheme (if any) an assignment already had.

        The endpoint now reports the file that is actually on disk. The page
        count is read from the PDF, not guessed from the upload.
        """
        from fixtures_mark_scheme import table_mark_scheme

        path = fresh_state / "mark_schemes" / "cw-pages.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        table_mark_scheme(path)

        data = client.get("/api/assignments/cw-pages/mark-scheme").json()
        assert data["has_mark_scheme"] is True
        assert data["file_name"] == "cw-pages.pdf"
        assert data["pages"] >= 1

    def test_cached_structure_is_validated(self, client, fresh_state):
        self._seed_scheme("cw-1", fresh_state)
        data = client.get("/api/assignments/cw-1/mark-scheme").json()
        assert data["processed"] is True
        assert data["validation"]["question_count"] == 3
        assert data["validation"]["total_marks"] == 12

    def test_manual_override_endpoint_saves(self, client, fresh_state):
        self._seed_scheme("cw-2", fresh_state)
        body = {"questions": _questions(5, marks=2), "total_marks": 10,
                "note": "put back missing questions"}
        response = client.post("/api/assignments/cw-2/mark-scheme/override",
                               json=body)
        assert response.status_code == 200
        data = response.json()
        assert data["manual_override"] is True
        assert data["validation"]["question_count"] == 5

    def test_manual_override_rejects_bad_structure(self, client, fresh_state):
        self._seed_scheme("cw-3", fresh_state)
        response = client.post(
            "/api/assignments/cw-3/mark-scheme/override",
            json={"questions": [], "total_marks": 0})
        assert response.status_code == 400
