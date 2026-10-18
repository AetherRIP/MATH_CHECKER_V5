"""Original-vs-V5 correction comparison on the user's REAL Test Data.

Fixture: Downloads/Test Data → copied to data/fixtures/real/
  - Student Answer.pdf   (the submission to mark)
  - Answer Sheet.pdf     (the official mark scheme)
  - Question paper.pdf   (optional context, passed by all three engines)

Three runs, identical inputs:

  A. ORIGINAL engine  — the friend's app5_1_automated.py imported as-is
                        (streamlit stubbed), its own google-genai client.
                        Needs the reference copy under ``Friend code/``; it
                        has been removed from the project, so this run is
                        skipped unless the folder is restored. Use
                        ``--skip-original`` to silence it explicitly.
  B. V5 engine direct — preserved copy driven through the KeyAwareClient.
  C. V5 production    — HTTP upload to the live server, full job pipeline.

Prints marks/feedback comparison. Never prints key material.
Usage: python scripts/compare_original_vs_v5.py [--production] [--skip-original]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

#: Where the friend's original Streamlit app used to be kept as the untouched
#: reference implementation. That folder has been removed from the project, so
#: run A only works where a copy of it still exists.
ORIGINAL_APP = (BACKEND.parent / "Friend code" / "edexcel-maths-correctorv2-main"
                / "app5_1_automated.py")

from app.config import DATA_DIR, get_config, get_secrets  # noqa: E402
from app.engine import legacy_marking as v5_legacy  # noqa: E402
from app.gemini.client_proxy import KeyAwareClient  # noqa: E402
from app.gemini.tester import KeyManagerHolder  # noqa: E402

FIXTURES = DATA_DIR / "fixtures" / "real"
SUBMISSION = FIXTURES / "Student Answer.pdf"
SCHEME = FIXTURES / "Answer Sheet.pdf"
QUESTION = FIXTURES / "Question paper.pdf"
BASE_URL = "http://127.0.0.1:8642"


def load_original_module():
    """Import the friend's original app with streamlit stubbed out."""
    if "streamlit" not in sys.modules:
        stub = MagicMock()
        stub.cache_resource = lambda fn: fn  # used WITHOUT parentheses there
        stub.secrets = {}
        stub.button.return_value = False
        stub.checkbox.return_value = False
        stub.file_uploader.return_value = None
        stub.text_input.return_value = ""
        stub.selectbox.return_value = None
        stub.tabs.side_effect = lambda n, **kw: [MagicMock() for _ in range(n)]
        stub.columns.side_effect = lambda n, **kw: [MagicMock() for _ in range(n)]
        stub.fragment.return_value = lambda fn: fn
        sys.modules["streamlit"] = stub

    if not ORIGINAL_APP.exists():
        # Say exactly what is missing and how to get it back, instead of dying
        # with a bare "no such file" deep inside importlib.
        raise FileNotFoundError(
            f"The friend's original app is not present ({ORIGINAL_APP}). "
            "Run A compares against that untouched reference copy; restore "
            "'Friend code/edexcel-maths-correctorv2-main/app5_1_automated.py' "
            "to include it, or pass --skip-original.")
    spec = importlib.util.spec_from_file_location("legacy_original_app",
                                                  str(ORIGINAL_APP))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_original() -> dict:
    original = load_original_module()
    import google.genai as genai
    from google.genai import types

    api_key = get_secrets().gemini_keys()[0]
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=get_config().gemini.request_timeout_ms),
    )

    remote_sub = original.upload_bytes_to_gemini(
        client, SUBMISSION.read_bytes(), SUBMISSION.name)
    remote_scheme = original.upload_bytes_to_gemini(
        client, SCHEME.read_bytes(), SCHEME.name)
    remote_question = original.upload_bytes_to_gemini(
        client, QUESTION.read_bytes(), QUESTION.name)
    try:
        raw, model = original.grade_with_model(
            client=client,
            model_names=original.PRIMARY_MODELS,
            student_file=[remote_sub],
            mark_scheme_file=remote_scheme,
            question_file=remote_question,
            status_callback=None,
        )
    finally:
        for remote in (remote_sub, remote_scheme, remote_question):
            try:
                client.files.delete(name=remote.name)
            except Exception:
                pass

    result = original.normalise_result(raw)
    earned, available, percentage = original.calculate_totals(result)
    return {
        "engine": "A. ORIGINAL (friend's code, own client)",
        "marking_model": model,
        "earned": earned, "available": available,
        "percentage": round(percentage, 2),
        "questions": [
            {"number": q["question_number"], "awarded": q["awarded_marks"],
             "max": q["max_marks"], "loss_reason": q["loss_reason"][:70]}
            for q in result["questions"]
        ],
        "feedback": result["overall_notes"]["summary"][:100],
    }


def _mark_with_cooldown_wait(client, remote_sub, remote_scheme, remote_question,
                             max_waits: int = 8):
    """V5 engine mark; if every key is mid-cooldown (429s from earlier runs),
    wait out the cooldown — the same policy the production pipeline applies
    via WAITING_FOR_API."""
    from app.gemini.key_manager import WaitForApiError
    waits = 0
    while True:
        try:
            return v5_legacy.grade_with_model(
                client=client,
                model_names=get_config().gemini.primary_models,
                student_file=[remote_sub],
                mark_scheme_file=remote_scheme,
                question_file=remote_question,
                status_callback=None,
            )
        except (WaitForApiError, RuntimeError) as exc:
            message = str(exc)
            cooldown_hit = isinstance(exc, WaitForApiError) or "cooling down" in message
            if not cooldown_hit:
                raise
            waits += 1
            if waits > max_waits:
                raise
            remaining = max(
                (k["cooldown_remaining"]
                 for k in KeyManagerHolder.instance().status()["keys"]),
                default=0,
            )
            pause = max(remaining, 5) + 3
            print(f"  [cooldown] all keys cooling — waiting {pause}s ({waits}/{max_waits})")
            time.sleep(pause)


def run_v5_direct() -> dict:
    client = KeyAwareClient(KeyManagerHolder.instance())
    remote_sub = client.upload_file(str(SUBMISSION))
    remote_scheme = client.upload_file(str(SCHEME))
    remote_question = client.upload_file(str(QUESTION))
    try:
        raw, model = _mark_with_cooldown_wait(client, remote_sub, remote_scheme, remote_question)
    finally:
        client.files_delete(remote_sub)
        client.files_delete(remote_scheme)
        client.files_delete(remote_question)

    result = v5_legacy.normalise_result(raw)
    verification = v5_verify(result)
    earned, available, percentage = v5_legacy.calculate_totals(result)
    return {
        "engine": "B. V5 engine + key manager",
        "marking_model": model,
        "earned": earned, "available": available,
        "percentage": round(percentage, 2),
        "verified": verification["verified"],
        "questions": [
            {"number": q["question_number"], "awarded": q["awarded_marks"],
             "max": q["max_marks"], "loss_reason": q["loss_reason"][:70]}
            for q in result["questions"]
        ],
        "feedback": result["overall_notes"]["summary"][:100],
    }


def v5_verify(result: dict) -> dict:
    from app.engine.verify import verify_result
    return verify_result(result)


def run_v5_production() -> dict:
    boundary = "----mc5boundary7f3a"

    def part(name: str, filename: str, data: bytes, mime: str = "application/pdf") -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + data + b"\r\n"

    body = b""
    body += part("submission", SUBMISSION.name, SUBMISSION.read_bytes())
    body += part("mark_scheme", SCHEME.name, SCHEME.read_bytes())
    body += part("question_paper", QUESTION.name, QUESTION.read_bytes())
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="student_name"\r\n\r\n'
        f"Real Test Data Student\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    req = urllib.request.Request(
        f"{BASE_URL}/api/corrections/manual", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        job = json.loads(response.read())

    job_id = job["id"]
    print(f"  [production] job {job_id[:8]}… queued; following stages:")
    last_stage = None
    deadline = time.time() + 900
    while time.time() < deadline:
        with urllib.request.urlopen(f"{BASE_URL}/api/jobs/{job_id}", timeout=30) as r:
            current = json.loads(r.read())
        stage_line = f"{current['status']} — {current.get('stage_detail') or ''}"
        if stage_line != last_stage:
            print(f"    {stage_line[:100]}")
            last_stage = stage_line
        if current["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(3)
    if current["status"] != "COMPLETED":
        return {"engine": "C. V5 production pipeline", "status": current["status"],
                "error": current.get("error")}

    with urllib.request.urlopen(f"{BASE_URL}/api/results/{current['result_id']}", timeout=30) as r:
        result = json.loads(r.read())
    return {
        "engine": "C. V5 production pipeline (HTTP -> job -> engine)",
        "status": current["status"],
        "marking_model": result["marking_model"],
        "earned": result["earned"], "available": result["available"],
        "percentage": round(result["percentage"], 2),
        "verified": (result.get("verification_json") or {}).get("verified"),
        "questions": [
            {"number": q["question_number"], "awarded": q["awarded_marks"],
             "max": q["max_marks"], "loss_reason": q["loss_reason"][:70]}
            for q in result["result_json"]["questions"]
        ],
        "feedback": result["result_json"]["overall_notes"]["summary"][:100],
    }


def marks_signature(run: dict) -> str:
    return " ".join(f"{q['number']}:{q['awarded']}/{q['max']}" for q in run["questions"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--production", action="store_true",
                        help="also run the full HTTP pipeline (C)")
    parser.add_argument("--skip-original", action="store_true",
                        help="skip run A (the friend's original app, whose "
                             "reference folder has been removed)")
    args = parser.parse_args()

    for f in (SUBMISSION, SCHEME, QUESTION):
        if not f.exists():
            print(f"Missing fixture: {f}")
            print("Copy Downloads/Test Data into data/fixtures/real/")
            return 2

    print("Original vs V5 correction comparison — REAL Test Data")
    print("=" * 64)
    print(f"fixture: {SUBMISSION.name} ({SUBMISSION.stat().st_size // 1024} KB), "
          f"{SCHEME.name} ({SCHEME.stat().st_size // 1024} KB), "
          f"{QUESTION.name} ({QUESTION.stat().st_size // 1024} KB)")

    runs = []
    runners = [run_v5_direct]
    if args.skip_original or not ORIGINAL_APP.exists():
        print("[A. ORIGINAL] skipped — the friend's reference app "
              f"({ORIGINAL_APP.parent}) is not in the project.\n"
              "              (runs B and C are unaffected.)")
    else:
        runners.insert(0, run_original)
    if args.production:
        runners.append(run_v5_production)

    for runner in runners:
        started = time.time()
        try:
            run = runner()
            run["seconds"] = round(time.time() - started, 1)
            runs.append(run)
            print(f"\n[{run['engine']}]  {run['seconds']}s  model={run.get('marking_model')}")
            print(f"  total: {run['earned']}/{run['available']} ({run['percentage']}%)")
            print(f"  marks: {marks_signature(run)}")
            for q in run["questions"]:
                print(f"    Q{q['number']:>4} {q['awarded']}/{q['max']}  {q['loss_reason'] or '(no marks lost)'}")
            print(f"  feedback: {run['feedback']}")
        except Exception as exc:
            print(f"\n[{runner.__name__}] FAILED: {str(exc)[:280]}")
            runs.append({"engine": runner.__name__, "error": str(exc)[:280]})

    print()
    print("=" * 64)
    completed = [r for r in runs if "earned" in r]
    if len(completed) >= 2:
        base = completed[0]
        all_match = True
        for other in completed[1:]:
            match = (base["earned"] == other["earned"]
                     and base["available"] == other["available"]
                     and marks_signature(base) == marks_signature(other))
            all_match &= match
            print(f"{base['engine'][:24]:<26} vs {other['engine'][:30]:<32} "
                  f"-> {'MATCH' if match else 'DIFFER'}  "
                  f"({base['earned']}/{base['available']} vs {other['earned']}/{other['available']})")
        return 0 if all_match else 2
    if len(completed) == 1:
        print("Only one run completed — nothing to compare against "
              "(see the skip/failure notes above).")
    else:
        print("One or more runs failed — see output above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
