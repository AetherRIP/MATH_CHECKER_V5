"""Offline demonstration of the subquestion layer (no AI, no keys, no network).

Run it to see, for one synthetic two-question mark scheme, exactly what the
mapping pipeline does:

  1. the OLD collapsed identity ("4b" for both (b)(i) and (b)(ii)) next to the
     nested one the pipeline now uses;
  2. the subquestion lines a marking prompt actually carries;
  3. a marker result in the ordinary "4(a)" / "4(b)(ii)" style, reconciled to
     the subquestion each row NAMES, with the per-subquestion marks;
  4. what happens when a declared sub-part is never returned (held for review,
     not a silent zero), and when an answer is attributed to the wrong part.

    python backend/scripts/probe_subquestion_mapping.py

Nothing here writes to the database or touches a real student's work.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.engine import blueprint as bp  # noqa: E402
from app.engine import legacy_marking as legacy  # noqa: E402
from app.engine import scheme_structure as st  # noqa: E402
from app.engine.question_inventory import (  # noqa: E402
    canonical_question_id, expected_inventory, missing_from_results,
)
from app.engine.verify import verify_result  # noqa: E402

SCHEME = {
    "paper": "1H",
    "total_marks": 10,
    "questions": [
        {"question_number": "3", "max_marks": 3, "criteria": "See parts",
         "parts": [
             {"number": "a", "max_marks": 1, "criteria": "B1 cao (1)",
              "answers": ["x = 5"], "page": 4},
             {"number": "b", "max_marks": 2, "criteria": "M1 A1 oe (2)",
              "answers": ["x = 8"], "page": 4},
         ]},
        {"question_number": "4", "max_marks": 7, "criteria": "See parts",
         "parts": [
             {"number": "a", "max_marks": 2, "criteria": "M1 A1 (2)",
              "answers": ["x = 12"], "page": 5},
             {"number": "b(i)", "max_marks": 2, "criteria": "M1 A1 cao (2)",
              "answers": ["x = 12"], "page": 5},
             {"number": "b(ii)", "max_marks": 3,
              "criteria": "M1 A1 A1 ft from (b)(i) (3)",
              "answers": ["x = 15"], "page": 6},
         ]},
    ],
}


#: The PRE-FIX implementation, kept here purely to show what it did: it kept
#: the main number and the first letter group and stopped, so every nested
#: sub-part of one lettered part collapsed onto the same id.
_FORMER_TOKEN = re.compile(
    r"^\s*(?:[Qq](?:uestion)?\s*)?\.?\s*(\d{1,3})\s*"
    r"(?:[\.\)\:\-]?\s*\(?\s*([a-zA-Z]{1,2})\s*\)?)?")


def former_canonical_id(text: str) -> str:
    match = _FORMER_TOKEN.match(str(text or "").strip())
    if match:
        number, part = match.group(1), match.group(2)
        return str(int(number)) + (part.lower() if part else "")
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def row(number: str, awarded: int, max_marks: int, answer: str = "work") -> dict:
    return {
        "question_number": number, "max_marks": max_marks,
        "awarded_marks": awarded, "topic": "", "student_answer": answer,
        "working_summary": "", "loss_reason": "",
        "mark_breakdown": [{"code": "M1", "awarded": awarded > 0,
                            "reason": "seen"}],
        "correct_method": "", "full_mark_solution": "",
    }


def main() -> int:
    print("=" * 78)
    print("1. IDENTITY — the collapse that merged two marking points")
    for label in ("4(b)(i)", "4(b)(ii)"):
        print(f"  {label:11} before={former_canonical_id(label):5} "
              f"now={canonical_question_id(label):5} "
              f"subquestion_id={st.subquestion_id(label)}")
    collapsed = (former_canonical_id("4(b)(i)")
                 == former_canonical_id("4(b)(ii)"))
    nested = (canonical_question_id("4(b)(i)")
              != canonical_question_id("4(b)(ii)"))
    print(f"  before, both ids were the SAME identity: {collapsed}")
    print(f"  now they are distinct (and scoped to their question): {nested}")

    print("=" * 78)
    print("2. WHAT THE MARKING PROMPT CARRIES")
    block = bp.prompt_block(SCHEME)
    for line in block.splitlines():
        if "subquestion=" in line or "question_number=" in line:
            print("  " + line.strip()[:150])
    carried = sum(1 for line in block.splitlines() if "subquestion=" in line)
    print(f"  -> {carried} subquestion line(s) reach the marker "
          f"(was 0 before the fix)")
    print(f"  -> present in the real marking prompt: "
          f"{'subquestion=4bii' in legacy.marking_user_prompt(SCHEME)}")

    print("=" * 78)
    print("3. ANSWERS AND MARKS MAPPED TO THEIR OWN SUBQUESTION")
    result = {"questions": [
        row("4(a)", 2, 2, "x = 12"),
        row("4(b)(i)", 1, 2, "x = 12 attempted"),
        row("4(b)(ii)", 3, 3, "x = 15"),
    ]}
    reconciled, report = bp.apply_blueprint(result, SCHEME)
    for question in reconciled["questions"]:
        if question["question_number"] != "4":
            continue
        print(f"  question 4: {question['awarded_marks']}/"
              f"{question['max_marks']}  status={question['status']}")
        for sub in question.get("subquestions") or []:
            print(f"    {sub['id']:5} {sub['awarded_marks']}/{sub['max_marks']}"
                  f"  status={sub['status']:8} rows={sub['rows']}"
                  f"  answers={sub['answers']}  codes={sub['mark_codes']}")
    print(f"  conflicts={report['subquestion_conflicts']} "
          f"unattributed={report['unattributed_rows']} "
          f"clamped={report['award_clamped']}")

    print("=" * 78)
    print("4. A PART THE MARKER NEVER RETURNED (not a silent zero)")
    partial = {"questions": [row("3(a)", 1, 1), row("3(b)", 2, 2),
                             row("4(a)", 2, 2), row("4(b)(i)", 2, 2)]}
    reconciled, report = bp.apply_blueprint(partial, SCHEME)
    verification = verify_result(reconciled, report)
    print(f"  inventory still expected: "
          f"{missing_from_results(partial, SCHEME)}")
    print(f"  missing subquestions: "
          f"{[s['label'] for s in report['missing_subquestions']]}")
    print(f"  held for review: {report['still_unresolved']} "
          f"-> verdict {verification['status']}")
    for anomaly in verification["anomalies"]:
        print("    ! " + anomaly[:140])

    print("=" * 78)
    print("5. AN ANSWER THAT WOULD LAND ON THE WRONG PART IS REFUSED")
    wrong = {"questions": [row("4(c)(iv)", 1, 1, "an answer for a part "
                                 "that does not exist")]}
    _reconciled, report = bp.apply_blueprint(wrong, SCHEME)
    print(f"  unmatched rows: {json.dumps(report['unmatched_rows'])}")
    print(f"  declared marking points still unmarked: "
          f"{sorted(s['subquestion'] for s in report['missing_subquestions'])}")
    print(f"  declared inventory: {sorted(expected_inventory(SCHEME))}")
    print("=" * 78)
    print("All checks ran offline: no model, no API key, no student data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
