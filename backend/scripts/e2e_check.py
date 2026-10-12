"""End-to-end smoke test: real HTTP → job queue → worker child → Gemini → DB.

Submits the fixture student PDF + mark scheme through the production pipeline
(same path a teacher's click takes), waits for the job to reach a terminal
state, then prints the stored result: totals, verification status, blueprint
source, marking model and the safe key fingerprint — plus the per-key usage
table so uneven rotation is visible.

Usage (from backend/, server must be running on --port):
    python scripts/e2e_check.py [--port 8642] [--timeout 600]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import DATA_DIR  # noqa: E402

FIXTURES = DATA_DIR / "fixtures" / "real"
BOUNDARY = "----mc5e2e"


def _request(url: str, *, method: str = "GET", body: bytes | None = None,
             content_type: str | None = None, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{BOUNDARY}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(value.encode() + b"\r\n")
    for name, path in files.items():
        chunks.append(f"--{BOUNDARY}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{path.name}"\r\n'.encode())
        chunks.append(b"Content-Type: application/pdf\r\n\r\n")
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={BOUNDARY}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--student", default=str(FIXTURES / "Student Answer.pdf"))
    parser.add_argument("--scheme", default=str(FIXTURES / "Answer Sheet.pdf"))
    parser.add_argument("--question", default=str(FIXTURES / "Question paper.pdf"))
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}/api"

    student, scheme = Path(args.student), Path(args.scheme)
    question = Path(args.question)
    if not student.exists() or not scheme.exists():
        print("fixture PDFs missing:", student, scheme)
        return 2

    files = {"submission": student, "mark_scheme": scheme}
    if question.exists():
        files["question_paper"] = question
    body, content_type = _multipart({"student_name": "E2E Smoke"}, files)

    print("submitting manual correction…")
    try:
        job = _request(f"{base}/corrections/manual", method="POST",
                       body=body, content_type=content_type, timeout=120)
    except urllib.error.HTTPError as exc:
        print("SUBMIT FAILED:", exc.read().decode()[:400])
        return 3
    job_id = job["id"]
    print("job:", job_id)

    deadline = time.time() + args.timeout
    final = job
    while time.time() < deadline:
        time.sleep(5)
        final = _request(f"{base}/jobs/{job_id}")
        print(f"  {final['status']:<16} {final.get('stage_detail') or ''}")
        if final["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
    if final["status"] != "COMPLETED":
        print("JOB DID NOT COMPLETE:", final["status"], final.get("error"))
        return 4

    result = _request(f"{base}/results/{final['result_id']}")
    inner = result["result_json"]
    verification = result.get("verification_json") or {}
    print("\n=== RESULT ===")
    print(f"score        : {result['earned']}/{result['available']} "
          f"({result['percentage']:.2f}%) band {result['provisional_grade']}")
    print(f"status       : {verification.get('status')}")
    print(f"model        : {result.get('marking_model')}")
    print(f"key          : {result.get('key_fingerprint')}")
    print(f"blueprint    : {(verification.get('blueprint') or {}).get('source')}")
    print(f"questions    : {len(inner['questions'])}")
    print(f"anomalies    : {verification.get('anomalies')}")
    print(f"warnings     : {len(verification.get('warnings') or [])}")
    print("per-question :")
    print("  " + "  ".join(
        f"Q{q['question_number']}={q['awarded_marks']}/{q['max_marks']}"
        f"{'*' if q.get('status') == 'REVIEW' else ''}"
        for q in inner["questions"]))

    usage = _request(f"{base}/keys")
    print("\n=== KEY USAGE (safe identifiers only) ===")
    for key in usage["keys"]:
        print(f"  {key['label']:<7} [{key['fingerprint']}] "
              f"status={key['status']:<8} requests={key['requests']} "
              f"successes={key['successes']} failures={key['failures']} "
              f"rate_limited={key['rate_limit_events']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
