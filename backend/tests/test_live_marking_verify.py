"""The opt-in live-marking verifier: opt-in guard, verdict logic, honesty.

No network and no Gemini key here: what is under test is the machinery that
decides a verdict and the claim the script makes about itself. The live request
itself is exercised by running ``verify_live_marking.py --live`` deliberately.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "MathChecker_Distribution"
FIXTURE_SOURCE = ROOT / "backend" / "scripts" / "make_fixtures.py"


@pytest.fixture(scope="module")
def live():
    sys.path.insert(0, str(DIST / "scripts"))
    try:
        import verify_live_marking as module
        yield module
    finally:
        sys.path.pop(0)
        for name in ("verify_live_marking", "verify_portable"):
            sys.modules.pop(name, None)


def outcome(live, awarded: dict[str, int], maxed: dict[str, int] | None = None,
            reasons: dict[str, str] | None = None, earned: float | None = None,
            available: float | None = None):
    marks = maxed or {"1": 3, "2": 2, "3": 3, "4": 2}
    return live.Outcome(
        awarded=awarded, maximum=marks, reasons=reasons or {},
        earned=float(sum(awarded.values()) if earned is None else earned),
        available=float(sum(marks.values()) if available is None else available),
    )


# ------------------------------------------------------------------ opt-in

def test_without_live_nothing_is_started_or_sent(live, monkeypatch, capsys):
    """The flag is the consent: no flag, no request, no app launch."""
    def explode(*args, **kwargs):        # pragma: no cover - must not run
        raise AssertionError("the verifier started something without --live")

    monkeypatch.setattr(live.subprocess, "Popen", explode)
    monkeypatch.setattr(sys, "argv", ["verify_live_marking.py"])
    assert live.main() == 2
    printed = capsys.readouterr().out
    assert "OPT-IN" in printed and "--live" in printed


def test_a_missing_key_refuses_instead_of_marking_in_mock_mode(
        live, tmp_path, monkeypatch, capsys):
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("# app\n", encoding="utf-8")
    student = tmp_path / "student.pdf"
    scheme = tmp_path / "scheme.pdf"
    for path in (student, scheme):
        path.write_bytes(b"%PDF-1.4\n")

    def explode(*args, **kwargs):        # pragma: no cover - must not run
        raise AssertionError("the verifier marked without a key")

    monkeypatch.setattr(live.subprocess, "Popen", explode)
    monkeypatch.setattr(sys, "argv", [
        "verify_live_marking.py", "--live", "--app", str(app),
        "--student-pdf", str(student), "--scheme-pdf", str(scheme),
        "--data-dir", str(tmp_path / "no-such-data")])
    assert live.main() == 2
    assert "No Gemini key configured" in capsys.readouterr().out


def test_an_unusable_expected_file_is_refused(live, tmp_path, monkeypatch, capsys):
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("# app\n", encoding="utf-8")
    (tmp_path / "key.txt").write_text("fake-key-not-a-real-credential\n",
                                      encoding="utf-8")
    (tmp_path / "empty.json").write_text("{}", encoding="utf-8")
    for name in ("student.pdf", "scheme.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(sys, "argv", [
        "verify_live_marking.py", "--live", "--app", str(app),
        "--student-pdf", str(tmp_path / "student.pdf"),
        "--scheme-pdf", str(tmp_path / "scheme.pdf"),
        "--key-file", str(tmp_path / "key.txt"),
        "--expected", str(tmp_path / "empty.json")])
    assert live.main() == 2
    assert "unusable" in capsys.readouterr().out


# ------------------------------------------------------------------ verdicts

def test_exact_match_certifies(live):
    key = {"questions": {"1": 3, "2": 2, "3": 2, "4": 2}, "total": 9,
           "total_marks": 10}
    verdict = live.compare_marks(
        outcome(live, {"1": 3, "2": 2, "3": 2, "4": 2}), key, 0)
    assert verdict.status == "CERTIFIED" and verdict.certified
    assert verdict.mismatches == 0


def test_one_mark_of_disagreement_is_not_certified_and_is_named(live):
    # Q3 is the question where the fixture student is wrong: awarding it in
    # full wrongly ALSO lifts the total, and both are reported.
    verdict = live.compare_marks(
        outcome(live, {"1": 3, "2": 2, "3": 3, "4": 2},
                reasons={"3": "A1 awarded for n = 14"}),
        {"questions": {"1": 3, "2": 2, "3": 2, "4": 2}, "total": 9}, 0)
    assert verdict.status == "NOT CERTIFIED"
    assert verdict.mismatches == 2
    joined = " ".join(verdict.lines)
    assert "Q3: expected 2, model awarded 3" in joined
    assert "A1 awarded for n = 14" in joined     # the model's own reasoning
    assert "total: expected 9, model awarded 10" in joined


def test_tolerance_weakens_the_claim_and_says_so(live):
    key = {"questions": {"1": 3, "2": 2, "3": 2}, "total": 7}
    verdict = live.compare_marks(outcome(live, {"1": 3, "2": 2, "3": 3}), key, 1)
    assert verdict.certified
    assert any("weakens this certification" in line for line in verdict.lines)


def test_a_question_the_key_does_not_have_is_a_mismatch(live):
    """A hallucinated question 5 must not slip through as an unmatched row."""
    verdict = live.compare_marks(
        outcome(live, {"1": 3, "5": 2}, {"1": 3, "5": 2}),
        {"questions": {"1": 3}, "total": 3}, 0)
    assert verdict.status == "NOT CERTIFIED"
    assert any("not in the answer key" in line for line in verdict.lines)


def test_a_missing_question_is_a_mismatch(live):
    """Marking four questions but reporting three is not a certified paper."""
    key = {"questions": {"1": 3, "2": 2, "3": 3, "4": 2}, "total": 10}
    verdict = live.compare_marks(
        outcome(live, {"1": 3, "2": 2}, {"1": 3, "2": 2, "3": 3, "4": 2}),
        key, 0)
    assert verdict.status == "NOT CERTIFIED"
    missing = [line for line in verdict.lines
               if "returned no mark for this question" in line]
    assert len(missing) == 2 and any("Q3" in line for line in missing) \
        and any("Q4" in line for line in missing)
    # The two missing questions are also two missing marks, so the total
    # disagrees as well.
    assert verdict.mismatches == 3


def test_a_total_only_key_still_decides(live):
    marks = {"1": 3, "2": 2, "3": 2, "4": 2}
    ok = live.compare_marks(outcome(live, marks), {"total": 9}, 0)
    assert ok.certified
    bad = live.compare_marks(outcome(live, marks), {"total": 8}, 0)
    assert bad.status == "NOT CERTIFIED"
    assert any("total: expected 8" in line for line in bad.lines)


def test_a_wrongly_read_mark_scheme_fails_even_when_the_marks_agree(live):
    """If the app reads 12 marks from a 10-mark scheme, nothing is certified."""
    verdict = live.compare_marks(
        outcome(live, {"1": 3, "2": 2, "3": 2, "4": 2},
                available=12),
        {"questions": {"1": 3, "2": 2, "3": 2, "4": 2}, "total": 9,
         "total_marks": 10}, 0)
    assert verdict.status == "NOT CERTIFIED"
    assert any("marks available" in line for line in verdict.lines)


# ------------------------------------------------------- the built-in key

def test_the_built_in_key_applies_only_to_the_bundled_fixture(live):
    bundled = live.fixture_key_for(Path("student_submission.pdf"),
                                   Path("mark_scheme.pdf"))
    assert bundled is not None and bundled["total"] == 9
    # Any other paper must supply its own truth: a different quiz cannot be
    # "certified" against another paper's answer key.
    assert live.fixture_key_for(Path("Year 10 mock.pdf"), Path("key.pdf")) is None


def test_the_built_in_key_matches_the_scripted_fixture(live):
    """The answer key must follow from the fixture source, not from a model.

    Re-derives the marks straight from ``backend/scripts/make_fixtures.py``:
    four questions worth 3+2+3+2, three answered correctly, and question 3
    carrying a wrong final answer (n = 13.33 instead of 14), which is worth
    two of its three marks.
    """
    text = FIXTURE_SOURCE.read_text(encoding="utf-8")
    scheme = text.split("SCHEME = [", 1)[1].split("]", 1)[0]
    student = text.split("STUDENT = [", 1)[1].split("]", 1)[0]

    questions = re.findall(r"Question (\d+) \((\d+) marks?\)", scheme)
    assert [number for number, _ in questions] == ["1", "2", "3", "4"]
    max_marks = [int(marks) for _, marks in questions]
    assert max_marks == [3, 2, 3, 2], max_marks

    key = live.FIXTURE_ANSWER_KEY
    assert key["total_marks"] == sum(max_marks) == 10
    assert {int(k): int(v) for k, v in key["questions"].items()} == {
        1: 3, 2: 2, 3: 2, 4: 2}
    assert key["total"] == sum(int(v) for v in key["questions"].values()) == 9
    # The fixture student's question 3 answer is the wrong one ...
    assert "n = 13.33" in student
    # ... so the key must withhold a mark there and award the rest in full.
    assert key["questions"]["3"] < max_marks[2]
    for number in ("1", "2", "4"):
        assert key["questions"][number] == max_marks[int(number) - 1]


# --------------------------------------------------------- run preparation

def test_mock_mode_is_forced_off_and_testing_only_forced_on(live):
    config = ("[app]\nrole = \"teacher\"\nmock_mode = true\n"
              "[worker]\nconcurrency = 2\n")
    config = live._force_setting(config, "app", "mock_mode", "false")
    config = live._force_setting(config, "testing", "only", "true")
    assert "mock_mode = false" in config
    assert "mock_mode = true" not in config
    assert "[testing]\nonly = true" in config
    assert "concurrency = 2" in config          # the teacher's settings survive


def test_the_isolated_copy_carries_the_key_but_the_report_never_does(
        live, tmp_path):
    secret = "AIzaSyFAKE-not-a-real-credential-000000"
    data = live.prepare_isolated_data(None, tmp_path, secret)
    assert secret in (data / "secrets.toml").read_text(encoding="utf-8")

    lines = "\n".join(live._report_lines(
        outcome(live, {"1": 3}), live.Verdict(status="CERTIFIED"),
        {"questions": {"1": 3}}, live.fingerprint(secret),
        Path("student_submission.pdf"), Path("mark_scheme.pdf")))
    assert secret not in lines
    assert live.fingerprint(secret) in lines     # only the fingerprint


def test_the_source_isolation_hashes_before_and_after(live, tmp_path):
    """``snapshot_files`` is what proves the real install was never written."""
    source = tmp_path / "real"
    source.mkdir()
    (source / "secrets.toml").write_text("[gemini]\napi_keys = [\"x\"]\n",
                                         encoding="utf-8")
    before = live.snapshot_files(source, ("config.toml", "secrets.toml"))
    assert before["secrets.toml"] != "absent" and before["config.toml"] == "absent"
    (source / "secrets.toml").write_text("[gemini]\napi_keys = [\"y\"]\n",
                                         encoding="utf-8")
    assert live.snapshot_files(source, ("secrets.toml",)) != \
        {"secrets.toml": before["secrets.toml"]}


def test_evidence_survives_a_cp1252_console(tmp_path):
    """A real Windows console cannot encode an arrow.

    Reproduces the defect the first live run hit: the model had already marked
    the paper, and printing the evidence crashed the run with
    UnicodeEncodeError before it could report anything. The evidence must
    degrade, never take the certification down with it.
    """
    script = (
        "import sys; sys.path.insert(0, %r); import verify_live_marking as m; "
        "report = m.Report(None); "
        "report.check('the live model completes the correction', True, "
        "'job 1 \u2192 COMPLETED'); "
        "report.note('\u00b10 mark(s) per question'); "
        "print('reached the end')" % str(DIST / "scripts"))
    proc = subprocess.run([sys.executable, "-c", script],
                          env={**os.environ, "PYTHONIOENCODING": "cp1252"},
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "reached the end" in proc.stdout
    assert "UnicodeEncodeError" not in proc.stderr


def test_expected_files_need_something_to_compare(live, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"total": 9}), encoding="utf-8")
    assert live.load_expected(good)["total"] == 9
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"source": "nothing to check"}), encoding="utf-8")
    with pytest.raises(ValueError):
        live.load_expected(bad)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        live.load_expected(broken)
