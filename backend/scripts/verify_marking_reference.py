#!/usr/bin/env python3
"""Measure marking accuracy against a human-verifiable reference set.

    python backend/scripts/verify_marking_reference.py --live --ack REFERENCE
    python backend/scripts/verify_marking_reference.py --live --ack REFERENCE \\
        --only paper-a --timeout 900

Why this exists
---------------
``verify_live_marking.py`` certifies ONE paper against a key you hand it. That
answers "did it get this paper right?", not "how accurate is the marking?" —
and the project had no dataset large enough to answer the second question, so
every accuracy claim was either a cross-check (the model agreeing with itself)
or an unmeasured assertion.

This drives that harness once per paper in the reference set
(``docs/reference-marking/``) and aggregates the numbers the question needs:
exact-mark agreement, mean absolute error, over- and under-marking, question and
subquestion matching errors, mark-scheme extraction errors, valid alternatives
that were rejected, and assessments that went wrong because the scheme was
wrong.

Ground truth
------------
The expectations come from the printed mark scheme, clause by clause, and are
recorded in the manifest with the ruling and its basis. The model's own output
is never used as ground truth — that is the whole point of the set.

The harness will NOT report CERTIFIED while ``review.verified_by`` in the
manifest is empty. Not being able to certify is a legitimate outcome and is
reported as one (exit 4), not fudged.

Exit codes
----------
  0  CERTIFIED: human-verified expectations, and every item matched exactly
  1  the run itself failed
  2  refused: no --live, no acknowledgement, or an unusable manifest
  3  NOT CERTIFIED: at least one item disagreed with the reference
  4  MEASURED BUT NOT CERTIFIED: nobody has signed the reference set off yet
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

REPO = Path(__file__).resolve().parents[2]
REF_DIR = REPO / "docs" / "reference-marking"
MANIFEST = REF_DIR / "manifest.json"
LIVE_HARNESS = (REPO / "MathChecker_Distribution" / "scripts"
                / "verify_live_marking.py")
RESULTS = REPO / "docs" / "results"

ACK_PHRASE = "REFERENCE"

EXIT_CERTIFIED, EXIT_RUN_FAILED, EXIT_REFUSED = 0, 1, 2
EXIT_NOT_CERTIFIED, EXIT_UNVERIFIED = 3, 4

#: Categories where a shortfall means the marker rejected work the scheme
#: accepts, rather than merely making an arithmetic slip. Reported separately
#: because "it marked a wrong answer wrong" and "it marked a right answer
#: wrong" are not the same defect.
ALTERNATIVE_CATEGORIES = {"alternative-method", "equivalent-expression",
                          "follow-through"}


# ------------------------------------------------------------------ matching

def canonical(question: str) -> str:
    """The numeric stem of a question label: ``1c`` -> ``1``, ``12`` -> ``12``."""
    match = re.match(r"\s*(\d+)", str(question))
    return match.group(1) if match else str(question).strip()


#: A label naming a MARKING POINT rather than a whole question (``1c``).
_SUBQUESTION_LABEL = re.compile(r"^\s*\d+\s*[a-z]+\s*$", re.IGNORECASE)


def is_subquestion_key(key: str) -> bool:
    return bool(_SUBQUESTION_LABEL.match(str(key)))


def question_of(label: str) -> str:
    return canonical(label)


def align(model_questions: dict, items: list[dict]) -> tuple[dict, dict, list]:
    """Line the model's question labels up with the manifest's.

    Google's numbering and the app's normalisation do not always agree with the
    scheme's (a nested ``1c`` may come back as ``1``). Matching is therefore
    tried exactly first and by numeric stem second, and HOW each label matched
    is recorded — a mismatch that was papered over would corrupt every metric
    below it.
    """
    manifest_keys = [item["question"] for item in items]
    model_keys = [str(k) for k in model_questions]

    mapping: dict[str, str] = {}
    method: dict[str, str] = {}

    for key in model_keys:
        if key in manifest_keys:
            mapping[key] = key
            method[key] = "exact"

    unmatched_manifest = [k for k in manifest_keys if k not in mapping.values()]
    unmatched_model = [k for k in model_keys if k not in mapping]

    for key in unmatched_model:
        stem = canonical(key)
        same_stem = [k for k in unmatched_manifest if canonical(k) == stem]
        if len(same_stem) == 1:
            # Two DIFFERENT marking points (``1b`` returned where the key says
            # ``1a``) must never be aligned with each other: that would turn a
            # misplaced mark into an agreement. Stem matching is for a label
            # that names a whole question where the key names a part (or the
            # reverse); it is not an identity for sub-parts.
            if is_subquestion_key(key) and is_subquestion_key(same_stem[0]):
                continue
            mapping[key] = same_stem[0]
            method[key] = "stem"
            unmatched_manifest.remove(same_stem[0])

    # A model label covering a whole nested question (``1`` for 1a/1b/1c) is
    # compared against the group's total, and labelled as such.
    groups: dict[str, list[str]] = {}
    for key in list(unmatched_model):
        stem = canonical(key)
        same_stem = [k for k in unmatched_manifest if canonical(k) == stem]
        if same_stem and not is_subquestion_key(key):
            groups[key] = same_stem
            method[key] = "group"
            for k in same_stem:
                unmatched_manifest.remove(k)

    return ({"mapping": mapping, "method": method, "groups": groups},
            {"unmatched_model": unmatched_model, "unmatched_manifest":
             unmatched_manifest},
            items)


def compare_paper(paper: dict, model_questions: dict,
                  subquestions: dict | None = None) -> dict:
    """Per-item comparison of awarded marks against the printed-scheme ruling.

    ``subquestions`` is the app's per-marking-point result (``1a``, ``1b`` …),
    which is where the marks for a sub-part scheme are actually awarded. It is
    merged in as extra comparable labels so a reference item that names a
    marking point is compared against THAT marking point instead of against the
    question's roll-up. Reading only the roll-up is what made an accurate
    paper-b run report ``compared 2/4 items`` with three "no mark returned"
    errors: the marks existed, in a field nobody looked at.
    """
    items = paper["items"]
    by_key = {item["question"]: item for item in items}
    candidates = {str(k): v for k, v in model_questions.items()}
    for sub_id, sub in (subquestions or {}).items():
        if sub is None or str(sub_id) in candidates:
            continue
        candidates[str(sub_id)] = {"awarded": sub.get("awarded"),
                                   "max": sub.get("max")}
    alignment, residue, _ = align(candidates, items)
    model_questions = candidates

    # A question's roll-up row repeats marks that were just compared one
    # marking point at a time. Counting it as "a question the model invented"
    # would be a false error, so it is recorded as a roll-up instead.
    rollup_labels = {}
    mapped = set(alignment["mapping"].values())
    for label in list(residue["unmatched_model"]):
        if is_subquestion_key(label):
            continue
        children = [sub_id for sub_id in (subquestions or {})
                    if question_of(sub_id) == canonical(label)]
        if children and all(child in mapped for child in children):
            rollup_labels[label] = sorted(children)
            residue["unmatched_model"].remove(label)

    rows, over, under, rejected, wrong_scheme = [], [], [], [], []
    exact = 0
    errors: list[float] = []
    compared = 0

    for model_key, manifest_key in alignment["mapping"].items():
        item = by_key[manifest_key]
        awarded = model_questions.get(model_key, {}).get("awarded")
        if awarded is None:
            continue
        expected = item["expected"]
        compared += 1
        diff = int(awarded) - int(expected)
        errors.append(abs(diff))
        row = {
            "question": manifest_key,
            "model_label": model_key,
            "matched_by": alignment["method"].get(model_key),
            "expected": expected,
            "awarded": int(awarded),
            "difference": diff,
            "marks_available": item["marks_available"],
            "categories": item["categories"],
        }
        if diff == 0:
            exact += 1
        elif diff > 0:
            over.append(row)
        else:
            under.append(row)
            if ALTERNATIVE_CATEGORIES & set(item["categories"]):
                rejected.append(row)
        if "scheme-total-disagrees" in item["categories"] and diff != 0:
            wrong_scheme.append(row)
        rows.append(row)

    # A model label that matches nothing is a matching error, and so is a
    # manifest question the model never returned: the teacher sees neither.
    grouped_away: list[str] = []
    for model_key in list(residue["unmatched_model"]):
        if model_key in alignment["groups"]:
            members = [by_key[k] for k in alignment["groups"][model_key]]
            expected = sum(m["expected"] for m in members)
            awarded = model_questions.get(model_key, {}).get("awarded")
            if awarded is None:
                continue
            # This label WAS compared (against the group's total), so it is not
            # an unmatched model label as well.
            grouped_away.append(model_key)
            compared += 1
            diff = int(awarded) - int(expected)
            errors.append(abs(diff))
            row = {
                "question": "+".join(m["question"] for m in members),
                "model_label": model_key,
                "matched_by": "group",
                "expected": expected,
                "awarded": int(awarded),
                "difference": diff,
                "marks_available": sum(m["marks_available"] for m in members),
                "categories": sorted({c for m in members
                                      for c in m["categories"]}),
            }
            if diff == 0:
                exact += 1
            elif diff > 0:
                over.append(row)
            else:
                under.append(row)
            rows.append(row)

    for label in grouped_away:
        if label in residue["unmatched_model"]:
            residue["unmatched_model"].remove(label)

    max_errors = []
    for model_key, manifest_key in alignment["mapping"].items():
        maximum = model_questions.get(model_key, {}).get("max")
        item = by_key.get(manifest_key) or {}
        if maximum is not None and item.get("marks_available") is not None \
                and int(maximum) != int(item["marks_available"]):
            max_errors.append({"question": manifest_key,
                               "scheme_says": item["marks_available"],
                               "app_read": int(maximum)})

    return {
        "items_in_reference": len(items),
        "items_compared": compared,
        "exact_agreement": exact,
        "mean_absolute_error": round(statistics.mean(errors), 3) if errors else None,
        "over_marked": over,
        "under_marked": under,
        "rejected_valid_alternatives": rejected,
        "wrong_because_scheme_wrong": wrong_scheme,
        "mark_allocation_disagreements": max_errors,
        "matching_errors": residue,
        "rollup_labels": rollup_labels,
        "subquestions_compared": len(
            [r for r in rows if is_subquestion_key(r["question"])]),
        "rows": rows,
    }


# -------------------------------------------------------------------- runner

def run_paper(paper: dict, args, work: Path) -> dict:
    key_path = work / f"{paper['id']}-expected.json"
    outcome_path = work / f"{paper['id']}-outcome.json"
    report_path = work / f"{paper['id']}-harness.txt"

    key_path.write_text(json.dumps({
        "questions": {item["question"]: item["expected"]
                      for item in paper["items"]},
        "total": paper["expected_total"],
        "total_marks": paper["marks_available"],
        "tolerance": 0,
        "source": f"docs/reference-marking/manifest.json#{paper['id']} "
                  f"(derived from the printed scheme; see REVIEW_SHEET.md)",
    }, indent=2), encoding="utf-8")

    command = [
        sys.executable, str(LIVE_HARNESS), "--live",
        "--student-pdf", str(REF_DIR / paper["student_pdf"]),
        "--scheme-pdf", str(REF_DIR / paper["scheme_pdf"]),
        "--expected", str(key_path),
        "--outcome-json", str(outcome_path),
        "--report", str(report_path),
        "--timeout", str(args.timeout),
    ]
    if args.data_dir:
        command += ["--data-dir", args.data_dir]
    if args.key_file:
        command += ["--key-file", args.key_file]

    started = time.time()
    proc = subprocess.run(command, capture_output=True, text=True,
                          timeout=args.timeout + 300,
                          encoding="utf-8", errors="replace")
    outcome = {}
    if outcome_path.is_file():
        try:
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        except ValueError:
            outcome = {}

    result = {
        "paper": paper["id"],
        "title": paper["title"],
        "exit_code": proc.returncode,
        "seconds": round(time.time() - started, 1),
        "unverified_in_harness": proc.returncode != 0 and not outcome,
        "model": outcome.get("model"),
        "app_earned": outcome.get("earned"),
        "app_available": outcome.get("available"),
        "app_percentage": outcome.get("percentage"),
        "app_grade": outcome.get("grade"),
        "harness_verdict": outcome.get("verdict"),
        "result_id": outcome.get("result_id"),
        "stdout_tail": (proc.stdout or "")[-1500:],
        "stderr_tail": (proc.stderr or "")[-1500:],
        "harness_report": str(report_path),
    }
    if outcome.get("questions"):
        result["comparison"] = compare_paper(paper, outcome["questions"],
                                             outcome.get("subquestions"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="required: send real corrections to Gemini")
    parser.add_argument("--ack", default="",
                        help=f'must be exactly "{ACK_PHRASE}"')
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--only", default=None,
                        help="comma-separated paper ids to run")
    parser.add_argument("--data-dir", default=None,
                        help="the app's config/secrets directory")
    parser.add_argument("--key-file", default=None,
                        help="file whose first line is a Gemini key")
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("--out", default=None,
                        help="where to write the results JSON")
    args = parser.parse_args()

    if not args.live or args.ack.strip().upper() != ACK_PHRASE:
        print("REFUSED: this harness sends real, paid corrections to Gemini "
              "using your own quota.\n"
              f"  re-run with: --live --ack {ACK_PHRASE}")
        return EXIT_REFUSED

    manifest_path = Path(args.manifest).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"REFUSED: the reference manifest is unusable: {exc}")
        return EXIT_REFUSED

    review = manifest.get("review") or {}
    verified_by = (review.get("verified_by") or "").strip()
    verified_on = (review.get("verified_on") or "").strip()
    papers = manifest.get("papers") or []
    if args.only:
        wanted = {p.strip() for p in args.only.split(",") if p.strip()}
        papers = [p for p in papers if p["id"] in wanted]
    if not papers:
        print("REFUSED: no papers selected.")
        return EXIT_REFUSED
    for paper in papers:
        for field in ("student_pdf", "scheme_pdf"):
            if not (REF_DIR / paper[field]).is_file():
                print(f"REFUSED: {paper['id']} needs {paper[field]}; run "
                      f"backend/scripts/make_marking_reference.py first.")
                return EXIT_REFUSED

    print(f"reference set : {manifest_path}")
    print(f"papers        : {', '.join(p['id'] for p in papers)}")
    print(f"human review  : " + (f"{verified_by} on {verified_on}"
                                 if verified_by else
                                 "NOT DONE — results can be measured but not "
                                 "certified (this run will exit 4)"))
    print(f"live harness  : {LIVE_HARNESS}\n")

    out_path = Path(args.out).resolve() if args.out else \
        RESULTS / "MARKING_REFERENCE_RESULTS.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="mc5-reference-"))
    results = []
    try:
        for paper in papers:
            print(f"--- {paper['id']}: {paper['title']}")
            result = run_paper(paper, args, work)
            comparison = result.get("comparison") or {}
            print(f"    harness exit {result['exit_code']}, "
                  f"{result['seconds']}s, model {result['model']}")
            if result.get("unverified_in_harness"):
                print("    the harness produced no outcome — see the tail below")
                print("    " + (result["stdout_tail"] or "")[-600:])
                print("    " + (result["stderr_tail"] or "")[-600:])
            elif comparison:
                print(f"    compared {comparison['items_compared']}/"
                      f"{comparison['items_in_reference']} items, "
                      f"exact {comparison['exact_agreement']}, "
                      f"MAE {comparison['mean_absolute_error']}, "
                      f"over {len(comparison['over_marked'])}, "
                      f"under {len(comparison['under_marked'])}")
                for row in comparison["over_marked"] + comparison["under_marked"]:
                    print(f"      Q{row['question']} expected {row['expected']} "
                          f"got {row['awarded']} ({','.join(row['categories'])})")
            results.append(result)
            out_path.write_text(json.dumps({
                "manifest": str(manifest_path),
                "review": review,
                "papers": results,
            }, indent=2) + "\n", encoding="utf-8")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    # ---- aggregate ---------------------------------------------------------
    totals = {"items_compared": 0, "exact_agreement": 0, "over": 0, "under": 0,
              "rejected_alternatives": 0, "matching_errors": 0,
              "allocation_disagreements": 0}
    error_terms: list[float] = []
    failures: list[str] = []
    for result in results:
        comparison = result.get("comparison")
        if not comparison:
            failures.append(f"{result['paper']}: no usable outcome "
                            f"(harness exit {result['exit_code']})")
            continue
        totals["items_compared"] += comparison["items_compared"]
        totals["exact_agreement"] += comparison["exact_agreement"]
        totals["over"] += len(comparison["over_marked"])
        totals["under"] += len(comparison["under_marked"])
        totals["rejected_alternatives"] += len(
            comparison["rejected_valid_alternatives"])
        totals["matching_errors"] += len(comparison["matching_errors"][
            "unmatched_model"]) + len(comparison["matching_errors"][
            "unmatched_manifest"])
        totals["allocation_disagreements"] += len(
            comparison["mark_allocation_disagreements"])
        if comparison["mean_absolute_error"] is not None:
            error_terms.append(comparison["mean_absolute_error"])
        if comparison["items_compared"] != comparison["items_in_reference"]:
            failures.append(
                f"{result['paper']}: only {comparison['items_compared']} of "
                f"{comparison['items_in_reference']} reference items were "
                f"compared")

    aggregate = {
        "papers_run": len(results),
        "papers_with_a_usable_outcome": len(results) - len(
            [r for r in results if not r.get("comparison")]),
        **totals,
        "exact_agreement_rate": (
            round(totals["exact_agreement"] / totals["items_compared"], 4)
            if totals["items_compared"] else None),
        "mean_absolute_error_per_item": (
            round(statistics.mean(error_terms), 3) if error_terms else None),
        "mean_absolute_error_overall": (
            round(sum(error_terms) / totals["items_compared"], 3)
            if error_terms and totals["items_compared"] else None),
        "human_verified_by": verified_by or None,
        "failures": failures,
    }

    payload = {"manifest": str(manifest_path), "review": review,
               "papers": results, "aggregate": aggregate}
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print("\n== measured ==")
    for key, value in aggregate.items():
        if key in ("failures",):
            continue
        print(f"  {key:<34} {value}")
    for failure in failures:
        print(f"  FAILURE: {failure}")

    # ---- verdict -----------------------------------------------------------
    if failures:
        print("\nexit 1: the run did not complete for every paper")
        return EXIT_RUN_FAILED
    if totals["exact_agreement"] != totals["items_compared"]:
        print(f"\nexit 3: NOT CERTIFIED — "
              f"{totals['items_compared'] - totals['exact_agreement']} of "
              f"{totals['items_compared']} items disagreed with the reference")
        return EXIT_NOT_CERTIFIED
    if not verified_by:
        print("\nexit 4: every item matched, but the reference set has NOT "
              "been signed off by a competent human reviewer, so the result is "
              "MEASURED and not CERTIFIED. See "
              "docs/reference-marking/REVIEW_SHEET.md.")
        return EXIT_UNVERIFIED
    print(f"\nexit 0: CERTIFIED — every item matched, verified by "
          f"{verified_by} on {verified_on}")
    return EXIT_CERTIFIED


if __name__ == "__main__":
    raise SystemExit(main())
