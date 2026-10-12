"""Legacy-vs-V5 comparison harness (regression protection, V5 §25).

Runs the SAME preserved engine that V5 uses and prints the result in the
legacy app's format, so a teacher can compare totals question-by-question
against the original Streamlit app's output for the same PDFs.

Usage (from backend/):
    python scripts/compare_legacy.py --student path/to/work.pdf --scheme path/to/scheme.pdf

Requires a real Gemini API key in data/secrets.toml (mock mode is ignored
for this harness on purpose: the point is to compare real marking output).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config, get_secrets  # noqa: E402
from app.engine import legacy_marking as legacy  # noqa: E402
from app.gemini.client_proxy import KeyAwareClient  # noqa: E402
from app.gemini.tester import KeyManagerHolder  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the preserved engine on one submission")
    parser.add_argument("--student", required=True, help="Student work PDF/image")
    parser.add_argument("--scheme", required=True, help="Official mark scheme PDF")
    parser.add_argument("--json", action="store_true", help="Emit full JSON result")
    args = parser.parse_args()

    keys = get_secrets().gemini_keys()
    if not keys:
        print("No Gemini API keys configured. Add keys to data/secrets.toml first.")
        return 2
    if get_config().mock_mode:
        print("NOTE: mock_mode is ON in config.toml — results will be simulated.")

    student_path = Path(args.student)
    scheme_path = Path(args.scheme)
    if not student_path.exists() or not scheme_path.exists():
        print("Input file(s) not found.")
        return 2

    client = KeyAwareClient(KeyManagerHolder.instance())
    remote_student = client.upload_file(str(student_path))
    remote_scheme = client.upload_file(str(scheme_path))
    try:
        raw, model = legacy.grade_with_model(
            client=client,
            model_names=get_config().gemini.primary_models,
            student_file=[remote_student],
            mark_scheme_file=remote_scheme,
        )
    finally:
        client.files_delete(remote_student)
        client.files_delete(remote_scheme)

    result = legacy.normalise_result(raw)
    earned, available, percentage = legacy.calculate_totals(result)
    totals = {
        "earned": earned,
        "available": available,
        "percentage": percentage,
        "provisional_grade": legacy.percentage_band_grade(percentage),
    }

    print()
    print(f"Marking model: {model}")
    print(f"Score: {totals['earned']} / {totals['available']}  "
          f"({totals['percentage']:.2f}%  ·  band {totals['provisional_grade']})")
    print()
    print("Question marks (for line-by-line comparison with the legacy app):")
    for q in result["questions"]:
        print(f"  Q{q['question_number']:>4}  {q['awarded_marks']}/{q['max_marks']}  {q['topic']}")

    if args.json:
        print()
        print(json.dumps({"result": result, "totals": totals,
                          "marking_model": model}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
