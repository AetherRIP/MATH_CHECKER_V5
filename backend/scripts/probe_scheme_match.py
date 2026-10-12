"""TEMPORARY diagnostic: do the submission and the mark scheme cover the
SAME paper?

The e2e run produced 0/51 with "No answer submitted" everywhere. Gemini read
the submission's page 1 fine (simple & compound interest), while the mark
scheme's page 1 is titled "Limits of Accuracy". This lists the question
headings of BOTH documents so the pairing can be judged on evidence rather
than on the first page alone.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import google.genai as genai  # noqa: E402

from app.config import get_config, get_secrets  # noqa: E402

SUBMISSION = ROOT / "data" / "fixtures" / "real" / "Student Answer.pdf"
MARK_SCHEME = ROOT / "data" / "mark_schemes" / "886161920765.pdf"

PROMPT = (
    "Scan the WHOLE document. List every question number you can see, in "
    "order, one per line, exactly as printed. Then state the mathematical "
    "topic(s) covered. Be concise. If none are legible, say NONE."
)


def describe(client, label: str, path: Path) -> None:
    print(f"\n===== {label}: {path.name} =====", flush=True)
    remote = client.files.upload(file=str(path))
    while getattr(getattr(remote, "state", None), "name", "") == "PROCESSING":
        time.sleep(2)
        remote = client.files.get(name=remote.name)
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[remote, PROMPT],
                config={"max_output_tokens": 1200},
            )
            print((getattr(resp, "text", "") or "").strip()[:1500])
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[probe] attempt {attempt + 1} failed: "
                  f"{type(exc).__name__}: {str(exc)[:150]}")
            time.sleep(5)
    try:
        client.files.delete(name=remote.name)
    except Exception:
        pass


def main() -> int:
    cfg = get_config()
    client = genai.Client(api_key=get_secrets().gemini_keys()[0],
                          http_options={"timeout": cfg.gemini.request_timeout_ms})
    describe(client, "SUBMISSION", SUBMISSION)
    describe(client, "MARK_SCHEME", MARK_SCHEME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())