"""TEMPORARY diagnostic: can Gemini actually READ the scanned submission?

The e2e run marked 0/51 with "No answer submitted for this question" for
every question. Two possible causes, and they must be told apart:

  (a) the mark scheme does not belong to this submission (a genuine mismatch),
  (b) Gemini received the PDF but could not read the handwriting / the file
      never arrived intact.

This asks the model to describe the first page of the submission directly, so
the answer comes from Gemini's own perception of the file.
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
    "This is a scanned student homework PDF. Looking ONLY at page 1:\n"
    "1. Is there any visible handwriting or printed maths work? yes/no\n"
    "2. Transcribe the first 3 maths expressions or question numbers you can "
    "read, exactly as written.\n"
    "3. Quote one short line verbatim.\n"
    "If the page is blank or unreadable, say BLANK."
)


def main() -> int:
    keys = get_secrets().gemini_keys()
    cfg = get_config()
    client = genai.Client(api_key=keys[0],
                          http_options={"timeout": cfg.gemini.request_timeout_ms})

    for label, path in (("SUBMISSION", SUBMISSION), ("MARK_SCHEME", MARK_SCHEME)):
        print(f"\n===== {label}: {path.name} =====", flush=True)
        started = time.time()
        remote = client.files.upload(file=str(path))
        while getattr(getattr(remote, "state", None), "name", "") == "PROCESSING":
            time.sleep(2)
            remote = client.files.get(name=remote.name)
        print(f"[probe] uploaded {remote.name} "
              f"({(time.time() - started):.0f}s)", flush=True)
        try:
            resp = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=[remote, PROMPT],
                config={"max_output_tokens": 900},
            )
            print(f"[probe] Gemini read-back ({time.time() - started:.0f}s):")
            print((getattr(resp, "text", "") or "").strip()[:1200])
        except Exception as exc:  # noqa: BLE001
            print(f"[probe] ERROR {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            try:
                client.files.delete(name=remote.name)
                print(f"[probe] deleted {remote.name}")
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())