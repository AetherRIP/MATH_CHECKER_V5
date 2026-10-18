#!/usr/bin/env python3
"""Verify LIVE MARKING ACCURACY — opt-in, real key, real PDF, real model.

    python verify_live_marking.py --live
    python verify_live_marking.py --live --student-pdf stu.pdf --scheme-pdf sch.pdf --expected key.json

Why this exists
---------------
verify_portable.py and verify_installer.py run the app in DEMO mode, where the
AI engine is forced into mock mode: they prove the pipeline works, never that
the marking is mathematically right. ``app/gemini/tester.py`` says the same in
its own words — "A passing test means exactly: this credential can communicate
with the configured Gemini model — nothing about marking accuracy. Correction
benchmarks are a separate, explicitly developer-triggered flow."

This is that flow. It is OPT-IN: without ``--live`` it refuses to do anything,
so it can sit next to the other verifiers without ever spending quota. When it
runs, it does the teacher's real thing:

  1. reads the Gemini key the app is already configured with (or ``--key-file``)
     and copies it into a THROWAWAY data directory — the teacher's own database,
     config and secrets are never opened for writing;
  2. starts the app against that directory with ``mock_mode`` OFF and
     Testing-only mode ON (Testing-only blocks every Classroom/Drive write at
     the HTTP chokepoint, so a certification run cannot publish anything);
  3. PROVES live is live: the running app must report ``mock_mode: false``, and
     the stored result must name a real marking model;
  4. marks a real PDF against a real mark scheme through the public API
     (``POST /api/corrections/unified``), then reads the marks back from the
     stored result — not from the job envelope;
  5. compares the model's per-question marks with GROUND TRUTH and says whether
     accuracy is certified. Two sources of truth are accepted: ``--expected``
     (a file of human-verified marks — the right way for your own papers), or
     the built-in answer key for the bundled 10-mark fixture quiz, whose correct
     marks follow from its construction (data/fixtures/*, written by
     backend/scripts/make_fixtures.py);
  6. writes the outcome — model, timings, every question's award and reason,
     verdict — to a report file, stops the app, and deletes the throwaway
     directory together with the copy of the credential in it.

Exit codes
----------
  0  accuracy CERTIFIED against ground truth
  1  the run itself failed (app would not start, job failed, result unreadable)
  2  refused: no ``--live``, no key, or a bad ``--expected`` file
  3  marked successfully but NOT certified (a question disagreed with truth)
  4  outcome reported, but there was no ground truth to certify against

Never trust exit 0 from anything else: only a comparison can certify accuracy,
so a run without ground truth can never return 0.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# The verifiers deliberately share one toolkit: same Report, same HTTP helper,
# same app-instance handling, so their evidence reads identically.
# Evidence strings carry arrows, dashes and ± signs. A Windows console is
# cp1252, and an unencodable character must not turn a live certification into
# a traceback AFTER the request has already been paid for (it did, once).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-tty streams
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_portable import (  # noqa: E402
    Report, free_port, http, listening_pids, multipart, robust_rmtree, sign_in,
    wait_health,
)

DIST = Path(__file__).resolve().parent.parent        # MathChecker_Distribution
REPO = DIST.parent                                   # the workspace root
DEFAULT_STUDENT = REPO / "data" / "fixtures" / "student_submission.pdf"
DEFAULT_SCHEME = REPO / "data" / "fixtures" / "mark_scheme.pdf"
LOGS = REPO / "docs" / "logs"

#: The bundled fixture quiz asks four questions worth 10 marks in total. Its
#: correct marks follow from how the fixture was built (backend/scripts/
#: make_fixtures.py), not from any model:
#:
#:   Q1 (3)  4x + 7 = 31  → 4x = 24 → x = 6                      3/3  correct
#:   Q2 (2)  0.36 = 36/100 = 9/25                                2/2  correct
#:   Q3 (3)  1st term 3(1) - 2 = 1 (B1) and 3n - 2 = 40 (M1) are correct,
#:           but the student then wrote 3n = 40 → n = 13.33; the answer is
#:           n = 14, so the A1 is NOT earned                         2/3
#:   Q4 (2)  (2^3)^2 = 8^2 = 64                                  2/2  correct
#:                                                          total  9/10
#:
#: Applied ONLY when the two default fixtures are the ones being marked, so a
#: regenerated or different quiz can never be certified against this key.
FIXTURE_FILES = ("student_submission.pdf", "mark_scheme.pdf")
FIXTURE_ANSWER_KEY = {
    "source": "by construction from backend/scripts/make_fixtures.py",
    "tolerance": 0,
    "questions": {"1": 3, "2": 2, "3": 2, "4": 2},
    "total": 9,
    "total_marks": 10,
}

#: Enough for a 4-question quiz on a live model; a big paper needs --timeout.
DEFAULT_JOB_TIMEOUT = 900.0


# --------------------------------------------------------------- key handling

def fingerprint(key: str) -> str:
    """Same fingerprint the app shows (app/gemini/key_manager.fingerprint)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def key_from_file(path: Path) -> str:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def key_from_secrets(path: Path) -> str:
    """First configured Gemini key in a secrets.toml (never printed)."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return ""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return ""
    keys = (data.get("gemini") or {}).get("api_keys") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    return str(keys[0]).strip() if keys else ""


def resolve_data_dir(app: Path, explicit: Path | None) -> Path | None:
    """Where the app keeps config/secrets: checkout, portable copy, install."""
    if explicit is not None:
        return explicit
    local = os.environ.get("LOCALAPPDATA")
    candidates = [app / "data", app / "Data"]
    if local:
        candidates.append(Path(local) / "MathChecker")
    for candidate in candidates:
        if (candidate / "secrets.toml").is_file():
            return candidate
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


# ---------------------------------------------------------- isolated app data

def _force_setting(text: str, section: str, key: str, value: str) -> str:
    """Set ``key = value`` inside ``section``, keeping every other setting.

    The teacher's own config is the thing being certified, so it is copied and
    only these two switches are forced. A file without the section gets it
    appended. The running app then CONFIRMS the result (GET /api/settings), so
    a botched edit fails the run instead of quietly marking in mock mode.
    """
    lines = text.splitlines()
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines)
                  if line.strip() == header), None)
    if start is None:
        return text.rstrip("\n") + f"\n\n{header}\n{key} = {value}\n"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        if lines[i].split("=")[0].strip() == key:
            lines[i] = f"{key} = {value}"
            break
    else:
        lines.insert(start + 1, f"{key} = {value}")
    return "\n".join(lines) + "\n"


def prepare_isolated_data(source: Path | None, work: Path, key: str) -> Path:
    """Build a throwaway data directory that cannot touch the teacher's data.

    Only two things are carried over from the real installation: the settings
    (so the certification marks with the teacher's own model chain) and the one
    key. Nothing is written back to the source, and the directory — including
    the copy of the key — is deleted at the end of the run.
    """
    data = work / "Data"
    data.mkdir(parents=True, exist_ok=True)

    config = (source / "config.toml").read_text(encoding="utf-8",
                                                errors="replace") \
        if source and (source / "config.toml").is_file() else ""
    config = _force_setting(config, "app", "mock_mode", "false")
    config = _force_setting(config, "testing", "only", "true")
    (data / "config.toml").write_text(config, encoding="utf-8")

    # The app reads keys from DATA_DIR/secrets.toml only, so the key has to be
    # written here. Keep nothing else from the teacher's secrets file: OAuth
    # tokens belong to the real installation and are not needed to mark.
    (data / "secrets.toml").write_text(
        "# Written by verify_live_marking.py for one certification run.\n"
        "# Deleted with this throwaway directory when the run ends.\n"
        "[gemini]\n"
        f'api_keys = ["{key}"]\n',
        encoding="utf-8")
    return data


def app_python(app: Path) -> str:
    """Prefer the app's own bundled interpreter when it ships with one."""
    for rel in ("runtime/python.exe", "runtime/bin/python3"):
        candidate = app / rel
        if candidate.is_file():
            return str(candidate)
    return sys.executable


class LiveApp:
    """The app under certification: started, and stopped again, by this test."""

    def __init__(self, app: Path, data: Path, port: int, log: Path) -> None:
        self.app = app
        self.data = data
        self.port = port
        self.log = log
        self.proc: subprocess.Popen | None = None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["MC5_DATA_DIR"] = str(self.data)
        env["MC5_NO_BROWSER"] = "1"          # a certification run opens nothing
        env["MC5_LOGIN_REQUIRED"] = "1"      # certify through the gated app
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("MC5_DEMO", None)            # demo would force mock mode
        env.pop("MC5_AUTOSTART", None)
        return env

    def start(self) -> None:
        handle = open(self.log, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [app_python(self.app), "main.py", str(self.port)],
            cwd=str(self.app), env=self._env(), stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True)

    def output(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self, timeout: float = 90.0) -> bool:
        """Stop it the way the product does, then make sure the port is free."""
        try:
            subprocess.run([app_python(self.app), "main.py", "--stop",
                            str(self.port)], cwd=str(self.app),
                           env=self._env(), capture_output=True, text=True,
                           timeout=int(timeout))
        except Exception:  # noqa: BLE001 - a failed stop must not hide a result
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not listening_pids(self.port):
                break
            time.sleep(1.0)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=20)
            except Exception:  # noqa: BLE001
                pass
        return not listening_pids(self.port)


# ------------------------------------------------------------- ground truth

@dataclass
class Outcome:
    """What the live model awarded, question by question."""

    awarded: dict[str, int] = field(default_factory=dict)
    maximum: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    earned: float = 0.0
    available: float = 0.0
    percentage: float = 0.0
    grade: str = ""
    model: str = ""
    seconds: float = 0.0
    result_id: str = ""


@dataclass
class Verdict:
    status: str                 # CERTIFIED / NOT CERTIFIED / REPORTED
    lines: list[str] = field(default_factory=list)
    mismatches: int = 0

    @property
    def certified(self) -> bool:
        return self.status == "CERTIFIED"


def load_expected(path: Path) -> dict:
    """Read a ground-truth file: human-verified marks for THIS student's paper.

        {"questions": {"1": 3, "2": 2}, "total": 9, "tolerance": 0}

    ``questions`` and ``total`` are both optional but at least one is required:
    without either there is nothing to certify against.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    if "questions" not in payload and "total" not in payload:
        raise ValueError(f"{path}: needs \"questions\" and/or \"total\"")
    return payload


def snapshot_files(root: Path | None, names: tuple[str, ...]) -> dict[str, str]:
    """Hash the real installation's own files, to prove they never changed."""
    if root is None:
        return {}
    digest = {}
    for name in names:
        path = root / name
        try:
            digest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest[name] = "absent"
    return digest


def fixture_key_for(student: Path, scheme: Path) -> dict | None:
    """The built-in answer key, only for the exact bundled fixture pair."""
    if student.name == FIXTURE_FILES[0] and scheme.name == FIXTURE_FILES[1]:
        return FIXTURE_ANSWER_KEY
    return None


def compare_marks(outcome: Outcome, expected: dict,
                  tolerance: int) -> Verdict:
    """Compare the live marks with ground truth, question by question.

    An unrecognised question is a mismatch, not a pass: a model that invents
    question 5 has not been certified, and neither has one that silently drops
    question 4 — the teacher would never see those marks. ``tolerance`` is the
    per-question slack the operator accepts, and it is reported, because every
    mark of slack weakens the claim.
    """
    verdict = Verdict(status="NOT CERTIFIED")
    wanted = {str(k): int(v) for k, v in (expected.get("questions") or {}).items()}
    slack = int(expected.get("tolerance", tolerance) or 0)

    if wanted:
        for number in sorted(set(wanted) | set(outcome.awarded),
                             key=lambda n: (len(n), n)):
            if number not in outcome.awarded:
                verdict.mismatches += 1
                verdict.lines.append(
                    f"Q{number}: expected {wanted[number]} but the model "
                    f"returned no mark for this question")
                continue
            got = outcome.awarded[number]
            if number not in wanted:
                verdict.mismatches += 1
                verdict.lines.append(
                    f"Q{number}: the model marked a question that is not in "
                    f"the answer key (awarded {got})")
                continue
            delta = abs(got - wanted[number])
            if delta > slack:
                verdict.mismatches += 1
                verdict.lines.append(
                    f"Q{number}: expected {wanted[number]}, model awarded {got}"
                    + (f" — {outcome.reasons.get(number, '')[:120]}"
                       if outcome.reasons.get(number) else ""))
        if slack:
            verdict.lines.append(
                f"tolerance ±{slack} mark(s) per question: a mark of slack "
                f"weakens this certification")

    if "total" in expected and expected["total"] is not None:
        want_total = float(expected["total"])
        if abs(outcome.earned - want_total) > slack:
            verdict.mismatches += 1
            verdict.lines.append(
                f"total: expected {want_total:g}, model awarded "
                f"{outcome.earned:g}")

    if "total_marks" in expected and expected["total_marks"] is not None:
        want_max = float(expected["total_marks"])
        if abs(outcome.available - want_max) > 0:
            verdict.mismatches += 1
            verdict.lines.append(
                f"marks available: expected {want_max:g}, the app read "
                f"{outcome.available:g} from the mark scheme")

    verdict.status = "CERTIFIED" if not verdict.mismatches else "NOT CERTIFIED"
    return verdict


# ------------------------------------------------------------------ marking

def _find(node, key):
    """Depth-first lookup for a key in the job detail envelope."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if key in current:
                return current[key]
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return None


def mark_once(app: LiveApp, session: tuple[str, str], student: Path,
              scheme: Path, report: Report,
              timeout: float) -> tuple[Outcome | None, dict]:
    """One real correction: upload, mark, read the stored result back."""
    cookie, csrf = session
    body, content_type = multipart(
        {"student_name": "Live Marking Verification", "pipeline": "auto"},
        {"submission": (student.name, student.read_bytes()),
         "mark_scheme": (scheme.name, scheme.read_bytes())})
    started = time.time()
    status, _, raw = http(
        "POST", f"http://127.0.0.1:{app.port}/api/corrections/unified",
        data=body, timeout=300,
        headers={"Content-Type": content_type, "Cookie": cookie,
                 "X-MC5-CSRF": csrf,
                 "Origin": f"http://127.0.0.1:{app.port}"})
    if status != 200:
        report.check("the app accepts a real PDF and mark scheme", False,
                     f"HTTP {status}: {raw[:200]!r}")
        return None, {}
    job = json.loads(raw)
    job_id = job.get("job_id") or job.get("large_pdf_job_id")
    report.check("the app accepts a real PDF and mark scheme", True,
                 f"pipeline={job.get('applied_pipeline')} "
                 f"pages={job.get('page_count')} job={job_id}")

    detail: dict = {}
    deadline = time.time() + timeout
    state = ""
    while time.time() < deadline:
        status, _, raw = http("GET", f"http://127.0.0.1:{app.port}/api/jobs/{job_id}",
                              headers={"Cookie": cookie})
        if status == 200:
            detail = json.loads(raw)
            state = str(detail.get("status") or detail.get("state") or "").upper()
            if state in ("COMPLETED", "FAILED", "CANCELLED", "ERROR"):
                break
        time.sleep(2.0)
    elapsed = time.time() - started
    reason = _find(detail, "error") or detail.get("error_kind") or ""
    if state != "COMPLETED":
        report.check("the live model completes the correction", False,
                     f"job {job_id} → {state or 'no answer after ' + str(int(timeout)) + 's'}"
                     + (f" ({str(reason)[:200]})" if reason else ""))
        return None, detail
    report.check("the live model completes the correction", True,
                 f"job {job_id} → COMPLETED in {elapsed:.0f}s")

    result_id = _find(detail, "result_id")
    if not result_id:
        status, _, raw = http(
            "GET", f"http://127.0.0.1:{app.port}/api/results"
                   "?q=Live%20Marking%20Verification&limit=5",
            headers={"Cookie": cookie})
        rows = json.loads(raw).get("results") or [] if status == 200 else []
        if rows:
            result_id = rows[0].get("result_id") or rows[0].get("id")
    if not result_id:
        report.check("the stored result is readable", False,
                     "no result_id in the job detail")
        return None, detail

    status, _, raw = http("GET",
                          f"http://127.0.0.1:{app.port}/api/results/{result_id}",
                          headers={"Cookie": cookie})
    if status != 200:
        report.check("the stored result is readable", False, f"HTTP {status}")
        return None, detail
    stored = json.loads(raw)
    report.check("the stored result is readable", True, f"result {result_id}")

    outcome = Outcome(result_id=str(result_id), seconds=elapsed)
    payload = stored.get("result_json") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    for question in (payload.get("questions") or []):
        number = str(question.get("question_number") or "?")
        try:
            outcome.awarded[number] = int(float(question.get("awarded_marks") or 0))
            outcome.maximum[number] = int(float(question.get("max_marks") or 0))
        except (TypeError, ValueError):
            continue
        note = question.get("loss_reason") or question.get("summary") or ""
        if note:
            outcome.reasons[number] = str(note)
    try:
        outcome.earned = float(stored.get("earned") or 0)
        outcome.available = float(stored.get("available") or 0)
        outcome.percentage = float(stored.get("percentage") or 0)
    except (TypeError, ValueError):
        pass
    outcome.grade = str(stored.get("provisional_grade") or "")
    outcome.model = str(stored.get("marking_model") or "")
    return outcome, stored


# --------------------------------------------------------------------- main

def _report_lines(outcome: Outcome, verdict: Verdict, expected: dict,
                  key_fp: str, student: Path, scheme: Path) -> list[str]:
    lines = ["", "== live marking outcome =="]
    lines.append(f"  student PDF : {student}")
    lines.append(f"  mark scheme : {scheme}")
    lines.append(f"  key         : {key_fp} (fingerprint only; the key is never written)")
    lines.append(f"  marking     : {outcome.model or 'model not recorded'} "
                 f"| {outcome.seconds:.0f}s | result {outcome.result_id}")
    lines.append("  marks       : " + ", ".join(
        f"Q{number} {outcome.awarded.get(number, '?')}/{outcome.maximum.get(number, '?')}"
        for number in sorted(outcome.maximum, key=lambda n: (len(n), n))))
    lines.append(f"  total       : {outcome.earned:g}/{outcome.available:g} "
                 f"= {outcome.percentage:g}% | provisional grade {outcome.grade}")
    for number in sorted(outcome.reasons, key=lambda n: (len(n), n)):
        lines.append(f"    Q{number}: {outcome.reasons[number][:200]}")
    lines.append("")
    lines.append("== accuracy against ground truth ==")
    lines.append(f"  source      : {expected.get('source') or 'supplied with --expected'}")
    if "questions" in expected:
        lines.append("  expected    : " + ", ".join(
            f"Q{k} {v}" for k, v in sorted(expected["questions"].items(),
                                           key=lambda kv: (len(kv[0]), kv[0]))))
    if "total" in expected:
        lines.append(f"  expected total: {expected['total']}")
    lines.append(f"  verdict     : {verdict.status}")
    for line in verdict.lines:
        lines.append(f"    - {line}")
    lines.append("")
    lines.append("Local evidence only: the per-question reasons are the model's "
                 "own words about this paper.")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mark a real PDF with the configured Gemini key and report "
                    "whether the marks are accurate (opt-in).")
    parser.add_argument("--live", action="store_true",
                        help="required: actually send a real (paid) request")
    parser.add_argument("--app", default=str(REPO),
                        help="app folder to certify (default: this workspace)")
    parser.add_argument("--data-dir", default=None,
                        help="folder holding the app's config.toml/secrets.toml "
                             "(default: the app's own)")
    parser.add_argument("--key-file", default=None,
                        help="file whose first non-empty line is the Gemini key "
                             "(default: the key the app is configured with)")
    parser.add_argument("--student-pdf", default=str(DEFAULT_STUDENT))
    parser.add_argument("--scheme-pdf", default=str(DEFAULT_SCHEME))
    parser.add_argument("--expected", default=None,
                        help="ground-truth JSON (default: the built-in answer "
                             "key for the bundled fixture quiz)")
    parser.add_argument("--tolerance", type=int, default=0,
                        help="per-question marks of slack (default 0 = exact)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=DEFAULT_JOB_TIMEOUT,
                        help="seconds to wait for the marking job")
    parser.add_argument("--work", default=None,
                        help="throwaway folder (default: %%LOCALAPPDATA%%/MC5-Live-Marking)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the app folder and the throwaway data")
    parser.add_argument("--report", default=None,
                        help="write the evidence report here")
    parser.add_argument("--outcome-json", default=None,
                        help="write the machine-readable outcome here")
    args = parser.parse_args()

    # ---- opt-in ---------------------------------------------------------
    if not args.live:
        print("This verification is OPT-IN: it sends a real correction to "
              "Gemini, using the operator's own quota.\n"
              "Nothing was sent. Re-run with --live to certify live marking "
              "accuracy, e.g.\n\n"
              "    python verify_live_marking.py --live\n")
        return 2

    app = Path(args.app).resolve()
    student = Path(args.student_pdf).resolve()
    scheme = Path(args.scheme_pdf).resolve()
    if not (app / "main.py").is_file():
        print(f"not an app folder (no main.py): {app}")
        return 2
    for path in (student, scheme):
        if not path.is_file():
            print(f"missing PDF: {path}")
            return 2

    source_data = resolve_data_dir(app, Path(args.data_dir).resolve()
                                   if args.data_dir else None)
    if args.key_file:
        key = key_from_file(Path(args.key_file).expanduser())
        origin = f"--key-file {args.key_file}"
    elif source_data:
        key = key_from_secrets(source_data / "secrets.toml")
        origin = f"{source_data / 'secrets.toml'}"
    else:
        key, origin = "", "nowhere"
    if not key:
        print(f"No Gemini key configured ({origin}).\n"
              "Add one in the app (AI → API & providers) or pass --key-file; "
              "certifying live marking without a key is not possible.")
        return 2

    expected: dict | None = None
    expected_source = ""
    if args.expected:
        try:
            expected = load_expected(Path(args.expected).resolve())
        except ValueError as exc:
            print(f"--expected is unusable: {exc}")
            return 2
        expected_source = f"--expected {args.expected}"
    else:
        expected = fixture_key_for(student, scheme)
        expected_source = ("the built-in answer key for the bundled fixture "
                           "(backend/scripts/make_fixtures.py)") if expected else ""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = Path(args.work).resolve() if args.work else \
        Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".")
    work = work / f"MC5-Live-Marking-{stamp}"
    if work.exists():
        robust_rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    # Never inside ``work``: that folder is deleted at the end of the run, and
    # the evidence has to survive it.
    report_path = Path(args.report).resolve() if args.report else \
        (LOGS if LOGS.is_dir() else DIST) / f"LIVE_MARKING_{stamp}.txt"
    outcome_path = Path(args.outcome_json).resolve() if args.outcome_json else \
        report_path.with_suffix(".json")
    report = Report(report_path)

    report.note("=" * 72)
    report.note("LIVE MARKING VERIFICATION — opt-in, real key, real PDF, real model")
    report.note("=" * 72)
    report.note(f"app        : {app}")
    report.note(f"key        : {fingerprint(key)} (from {origin})")
    report.note(f"student PDF: {student}")
    report.note(f"mark scheme: {scheme}")
    report.note(f"ground truth: {expected_source or 'NONE — accuracy cannot be certified'}")
    report.note(f"tolerance  : ±{args.tolerance} mark(s) per question")
    report.note(f"report     : {report_path}")
    report.note("")

    # ---- isolate --------------------------------------------------------
    report.note("== isolated run ==")
    real_files = snapshot_files(source_data, ("config.toml", "secrets.toml"))
    data = prepare_isolated_data(source_data, work, key)
    report.check("the run works in its own throwaway data directory",
                 data == work / "Data" and (data / "secrets.toml").is_file(),
                 f"{data}" + (f" (settings copied from {source_data})"
                              if source_data else " (fresh settings, no key file)"))
    report.check("Testing-only mode is forced ON for the run",
                 "only = true" in (data / "config.toml").read_text(
                     encoding="utf-8"),
                 "every Classroom/Drive write is blocked while certifying")

    port = args.port or free_port()
    app_instance = LiveApp(app, data, port, work / "app.log")
    code = 1
    try:
        report.note("\n== start the app (live, not demo) ==")
        app_instance.start()
        if not wait_health(port):
            report.check("the app starts and answers /api/health", False,
                         f"nothing on port {port}; see {app_instance.log}\n"
                         + "\n".join(app_instance.output().splitlines()[-12:]))
            return 1
        report.check("the app starts and answers /api/health", True,
                     f"http://127.0.0.1:{port}")

        # The gate is on (MC5_LOGIN_REQUIRED=1), so the settings that PROVE
        # the engine is live can only be read with a session: sign in first,
        # then verify — never the other way round, or an unauthenticated read
        # would look like "mock_mode is false" when it was simply refused.
        session = sign_in(port, data, label="live-marking")
        report.check("the copy signs in", session is not None,
                     "signed in through /api/auth/login" if session else
                     "sign-in failed")
        if not session:
            return 1

        cookie, _ = session
        status, _, raw = http("GET", f"http://127.0.0.1:{port}/api/settings",
                              headers={"Cookie": cookie})
        settings = json.loads(raw) if status == 200 else {}
        mock = settings.get("mock_mode")
        report.check("the app reports its engine state", status == 200,
                     "GET /api/settings → 200" if status == 200 else
                     f"GET /api/settings → HTTP {status}: {raw[:120]!r}")
        report.check("the app is NOT in mock mode — the marking is live",
                     mock is False,
                     f"mock_mode={mock!r}, models="
                     f"{(settings.get('gemini') or {}).get('primary_models')}"
                     if mock is False else
                     f"mock_mode={mock!r} — a mocked run would prove nothing")
        report.check("Testing-only is ON in the running app",
                     bool((settings.get("testing") or {}).get("only")),
                     (settings.get("testing") or {}).get("reason", ""))
        if mock is not False:
            return 1

        report.note("\n== live correction ==")
        outcome, stored = mark_once(app_instance, session, student, scheme,
                                    report, args.timeout)
        if outcome is None:
            return 1
        if not outcome.model:
            report.check("the stored result names the model that marked",
                         False, "marking_model is empty")
            return 1
        report.check("the stored result names the model that marked", True,
                     outcome.model)
        report.check("the app read the mark scheme as a real scheme",
                     outcome.available > 0 and bool(outcome.maximum),
                     f"{len(outcome.maximum)} question(s), "
                     f"{outcome.available:g} marks available")

        # ---- verdict ----------------------------------------------------
        # The verdict is the RESULT of the run, not a check: it is reported and
        # turned into the exit code, so "not certified" can never look like a
        # successful verification.
        report.note("\n== accuracy against ground truth ==")
        if expected:
            verdict = compare_marks(outcome, expected, args.tolerance)
        else:
            verdict = Verdict(
                status="REPORTED",
                lines=["no ground truth was supplied, so accuracy is NOT "
                       "certified: pass --expected with human-verified marks "
                       "for this paper to certify it"])
        for line in _report_lines(outcome, verdict, expected or {},
                                  fingerprint(key), student, scheme):
            report.note(line)

        outcome_path.write_text(json.dumps({
            "verdict": verdict.status,
            "certified": verdict.certified,
            "key_fingerprint": fingerprint(key),
            "model": outcome.model,
            "seconds": outcome.seconds,
            "result_id": outcome.result_id,
            "earned": outcome.earned,
            "available": outcome.available,
            "percentage": outcome.percentage,
            "grade": outcome.grade,
            "questions": {number: {"awarded": outcome.awarded.get(number),
                                   "max": outcome.maximum.get(number)}
                          for number in outcome.maximum},
            "expected": {k: v for k, v in (expected or {}).items()
                         if k != "source"},
            "mismatches": verdict.lines,
        }, indent=2) + "\n", encoding="utf-8")
        report.note(f"outcome JSON: {outcome_path}")

        code = 0 if verdict.certified else (3 if expected else 4)

        report.note("\n== stop and clean up ==")
        report.check("the app stops cleanly", app_instance.stop(),
                     f"port {port} free afterwards")
        report.check("the real installation's config and secrets are untouched",
                     snapshot_files(source_data,
                                    ("config.toml", "secrets.toml")) == real_files,
                     "byte-identical" if source_data else "there is no real "
                     "installation's file to touch")
    finally:
        if app_instance.proc and app_instance.proc.poll() is None:
            try:
                app_instance.stop(timeout=30)
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
        if not args.keep:
            # The throwaway folder holds a COPY of the credential: prove it is
            # gone rather than assume it.
            removed = robust_rmtree(work)
            report.check("the throwaway data (and its copy of the key) is deleted",
                         removed and not work.exists(),
                         f"{work} removed" if removed else
                         f"STILL PRESENT: {work} — delete it by hand")
        else:
            report.note(f"kept: {work} (contains a copy of the key — delete it "
                        f"by hand when you are done)")

    failed = [name for name, ok, _ in report.results if not ok]
    report.summary()
    if failed:
        return 1                      # the run itself was not trustworthy
    return code                       # 0 certified / 3 not certified / 4 reported


if __name__ == "__main__":
    raise SystemExit(main())
