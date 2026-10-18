#!/usr/bin/env python3
"""Generate the marking reference set: PDFs + manifest + a review sheet.

    python backend/scripts/make_marking_reference.py

Why this exists
---------------
There was no way to answer "how accurate is the marking?" with evidence. The
only fixture was a four-question quiz with a built-in answer key, and every
accuracy claim in the project's reports was either cross-checked (the model
agreeing with itself) or unmeasured. Neither is certification.

This builds a reference set that CAN certify: real O-Level style questions, the
mark scheme as it would actually be printed, a student answer for every item,
and — the part that matters — an EXPECTED mark for every item that is derived
from the printed scheme clause by clause, never from the model's own output.

Every expectation carries ``ruling`` (which clause earns which mark) and
``basis`` (how the ruling follows from the scheme text). Those two fields are
what a teacher checks; the review sheet renders them as a sign-off list.

One source of truth
-------------------
The PDFs and the manifest are generated from the same data below, so the scheme
a student sees and the expectations the harness measures can never drift apart.

A caveat this file does not hide: the expectations here are derived from the
printed scheme by the author of this file, not signed off by a teacher. The
manifest therefore records ``verified_by: null`` and the harness refuses to
report "certified" until a named human fills it in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[2]
REF = REPO / "docs" / "reference-marking"
PAPERS = REF / "papers"

#: A question the candidate left blank. It is rendered as NOTHING after the
#: question number — labelling it "no attempt" would hand the marker the answer
#: and destroy the very thing this item exists to measure (that an unanswered
#: question is reported as 0, not silently omitted from the paper).
NOT_ATTEMPTED = ""


# --------------------------------------------------------------------- data
#
# ``expected`` is the mark a competent marker awards FROM THE SCHEME, and
# ``ruling`` says which clause earns which mark. Nothing here was taken from a
# model's output.

PAPER_A_ITEMS = [
    {
        "question": "1",
        "prompt": "Solve 4x + 7 = 31.",
        "marks_available": 3,
        "expected": 3,
        "categories": ["fully-correct", "method-and-accuracy"],
        "scheme": ["M1: 4x = 24 seen (subtract 7 from both sides)",
                   "M1: divide both sides by 4",
                   "A1: x = 6 (cao)"],
        "student": ["4x + 7 = 31", "4x = 24", "x = 6"],
        "ruling": "3 of 3: 4x = 24 shown (M1), division by 4 performed (M1), "
                  "x = 6 stated as the answer (A1).",
        "basis": "each of the three printed clauses is evidenced in the working",
    },
    {
        "question": "2",
        "prompt": "Write 0.36 as a fraction in its simplest form.",
        "marks_available": 2,
        "expected": 2,
        "categories": ["fully-correct", "equivalent-expression"],
        "scheme": ["M1: 36/100 seen", "A1: 9/25 (cao)"],
        "student": ["0.36 = 36/100", "= 9/25"],
        "ruling": "2 of 2: 36/100 seen (M1) and reduced correctly to 9/25 (A1). "
                  "An unsimplified 36/100 would earn M1 only.",
        "basis": "both printed clauses evidenced; the answer is in the form the "
                 "scheme requires (lowest terms)",
    },
    {
        "question": "3",
        "prompt": "The nth term of a sequence is 3n - 2. "
                  "(a) Write down the 4th term. (b) Which term of the sequence "
                  "equals 40?",
        "marks_available": 3,
        "expected": 3,
        "categories": ["nested-subquestion", "fully-correct"],
        "scheme": ["B1: 4th term = 10",
                   "M1: sets 3n - 2 = 40",
                   "A1: n = 14 (cao)"],
        "student": ["(a) 3(4) - 2 = 10", "(b) 3n - 2 = 40",
                    "3n = 42", "n = 14"],
        "ruling": "3 of 3: (a) B1 for 10, (b) M1 for 3n - 2 = 40, A1 for n = 14.",
        "basis": "the two sub-parts map onto the three printed clauses",
    },
    {
        "question": "4",
        "prompt": "Work out (2^3)^2.",
        "marks_available": 2,
        "expected": 0,
        "categories": ["missing-answer"],
        "scheme": ["M1: 2^6 seen (or 8 squared)",
                   "A1: 64 (cao)"],
        "student": [NOT_ATTEMPTED],
        "ruling": "0 of 2: no attempt. Neither clause can be credited, and this "
                  "must be reported as 0 rather than silently omitted.",
        "basis": "a clause not evidenced earns nothing; a blank answer still "
                 "occupies marks in the denominator",
    },
    {
        "question": "5",
        "prompt": "Expand (x + 3)(x - 5).",
        "marks_available": 2,
        "expected": 2,
        "categories": ["alternative-method", "fully-correct"],
        "scheme": ["M1: four correct terms or a complete correct area/grid "
                   "product (x^2, -5x, 3x, -15)",
                   "A1: x^2 - 2x - 15 (cao)"],
        "student": ["grid:", "       x      +3",
                    "  x | x^2    +3x",
                    " -5 | -5x    -15",
                    "x^2 - 5x + 3x - 15 = x^2 - 2x - 15"],
        "ruling": "2 of 2: the grid evidences all four terms (M1) and the "
                  "simplified answer matches (A1). The method is not the "
                  "scheme's prose, but the clause says 'or a complete correct "
                  "grid', so it earns the mark.",
        "basis": "the scheme's own 'or' alternative admits the grid method",
    },
    {
        "question": "6",
        "prompt": "Simplify 12a^5 b^3 / (4a^2 b).",
        "marks_available": 3,
        "expected": 3,
        "categories": ["equivalent-expression", "fully-correct"],
        "scheme": ["M1: coefficient 3",
                   "M1: a^3",
                   "A1: 3a^3 b^2 (or any equivalent ordering such as 3b^2 a^3)"],
        "student": ["12 / 4 = 3", "a^5 / a^2 = a^3", "b^3 / b = b^2",
                    "= 3b^2a^3"],
        "ruling": "3 of 2: all three clauses evidenced. The final answer is "
                  "written 3b^2a^3 rather than 3a^3b^2 — the scheme explicitly "
                  "accepts an equivalent ordering, so A1 stands.",
        "basis": "A1's own wording admits equivalent orderings; multiplication "
                 "is commutative",
    },
    {
        "question": "7",
        "prompt": "Factorise x^2 + 7x + 12.",
        "marks_available": 2,
        "expected": 0,
        "categories": ["incorrect"],
        "scheme": ["M1: (x + 3)(x + 4) seen",
                   "A1: both factors correct (cao)"],
        "student": ["(x - 3)(x - 4)"],
        "ruling": "0 of 2: the signs are wrong. (x - 3)(x - 4) expands to "
                  "x^2 - 7x + 12, so neither the factorisation clause nor the "
                  "answer clause is met. A mark would be an over-mark.",
        "basis": "the student's product does not expand to the printed "
                 "expression, so no clause is evidenced",
    },
    {
        "question": "8",
        "prompt": "Solve simultaneously: 2x + y = 11 and x - y = 1.",
        "marks_available": 3,
        "expected": 3,
        "categories": ["fully-correct", "method-and-accuracy"],
        "scheme": ["M1: adds (or otherwise eliminates) to get 3x = 12",
                   "M1: x = 4",
                   "A1: y = 3 (cao, dependent on a correct x)"],
        "student": ["2x + y = 11", "x - y = 1", "adding: 3x = 12",
                    "x = 4", "8 + y = 11 so y = 3"],
        "ruling": "3 of 3: 3x = 12 (M1), x = 4 (M1), y = 3 (A1).",
        "basis": "all three printed clauses evidenced in order",
    },
    {
        "question": "9",
        "prompt": "A triangle has base 12 cm and perpendicular height 9 cm. "
                  "Work out its area.",
        "marks_available": 2,
        "expected": 1,
        "categories": ["partial-credit", "method-and-accuracy"],
        "scheme": ["M1: 1/2 x 12 x 9 (correct substitution)",
                   "A1: 54 cm^2 (cao, units required)"],
        "student": ["= 1/2 x 12 x 9", "= 108 cm^2"],
        "ruling": "1 of 2: the correct formula with correct substitution earns "
                  "M1. The arithmetic and the answer are wrong, so A1 (54 cm^2) "
                  "is not earned.",
        "basis": "M1 follows the printed substitution clause; A1 requires the "
                 "cao value 54, which the student did not produce",
    },
    {
        "question": "10",
        "prompt": "Hence solve x^2 + 7x + 12 = 0.",
        "marks_available": 2,
        "expected": 2,
        "categories": ["follow-through"],
        "scheme": ["M1 (ft): solves their own factorisation from the previous "
                   "part",
                   "A1 (ft): the two roots consistent with that factorisation"],
        "student": ["using my factors: (x - 3)(x - 4) = 0",
                    "x = 3 or x = 4"],
        "ruling": "2 of 2 by follow-through: this candidate's earlier "
                  "factorisation was wrong, but the scheme marks this part from "
                  "THEIR work. x = 3 and x = 4 are exactly the roots of "
                  "(x - 3)(x - 4), so both ft marks are earned even though the "
                  "final values differ from the correct pair -4 and -3.",
        "basis": "the scheme's (ft) clauses are printed against 'their own "
                 "factorisation', so the correct pair is not required here",
        "trap": "a marker that applies the correct roots (-4, -3) rather than "
                "the candidate's has not done follow-through marking",
    },
    {
        "question": "11",
        "prompt": "Solve 5x - 3 = 2x + 9.",
        "marks_available": 2,
        "expected": 1,
        "categories": ["ambiguous"],
        "scheme": ["M1: 3x = 12 seen",
                   "A1: x = 4 (cao, a single unambiguous value required)"],
        "student": ["5x - 3 = 2x + 9", "3x = 12", "x = 4 or 12"],
        "ruling": "1 of 2: 3x = 12 earns M1. The final line offers two values "
                  "and does not identify one, so the answer is not "
                  "unambiguous and A1 cannot be awarded. Accepted without "
                  "comment only when the working names the required value.",
        "basis": "A1 is 'cao' - correct answer only - which cannot be met by an "
                 "answer that leaves the correct value unresolved",
    },
    {
        "question": "12",
        "prompt": "A car travels 150 km in 2.5 hours. Work out its average "
                  "speed.",
        "marks_available": 3,
        "expected": 2,
        "categories": ["partial-credit", "method-and-accuracy"],
        "scheme": ["M1: 150 / 2.5 seen",
                   "A1: 60 (cao)",
                   "B1: km/h stated"],
        "student": ["150 / 2.5 = 60"],
        "ruling": "2 of 3: the division earns M1 and the value 60 earns A1. The "
                  "unit km/h is never written, so B1 is not earned.",
        "basis": "B1 is a printed clause about stating the unit; the student "
                 "did not state it",
    },
]

PAPER_B_ITEMS = [
    {
        "question": "1a",
        "prompt": "1 (a) Write down the value of 3^2.",
        "marks_available": 1,
        "expected": 1,
        "categories": ["nested-subquestion", "overlapping-lesson-id"],
        "scheme": ["B1: 9"],
        "student": ["(a) 9"],
        "ruling": "1 of 1: the single printed clause is met.",
        "basis": "clause evidenced exactly",
    },
    {
        "question": "1b",
        "prompt": "1 (b) Write down the value of 2^5.",
        "marks_available": 1,
        "expected": 1,
        "categories": ["nested-subquestion", "overlapping-lesson-id"],
        "scheme": ["B1: 32"],
        "student": ["(b) 32"],
        "ruling": "1 of 1: the single printed clause is met.",
        "basis": "clause evidenced exactly",
    },
    {
        "question": "1c",
        "prompt": "1 (c) Work out 3^2 + 2^5.",
        "marks_available": 2,
        "expected": 1,
        "categories": ["nested-subquestion", "partial-credit",
                       "overlapping-lesson-id"],
        "scheme": ["M1: 9 + 32 seen",
                   "A1: 41 (cao)"],
        "student": ["(c) 9 + 32", "= 42"],
        "ruling": "1 of 2: 9 + 32 is correct substitution (M1). The total 42 is "
                  "wrong and 41 is the cao value, so A1 fails. Note the parts "
                  "are nested inside question 1 and must be marked "
                  "individually, against 1a/1b/1c.",
        "basis": "M1 from the substitution clause; A1 requires the cao value",
    },
    {
        "question": "2",
        "prompt": "2 Solve 7x = 42.",
        "marks_available": 3,
        "expected": 3,
        "categories": ["nested-subquestion", "fully-correct",
                       "scheme-total-disagrees"],
        "scheme": ["M1: divide both sides by 7", "M1: 42 / 7", "A1: x = 6"],
        "student": ["x = 42 / 7", "x = 6"],
        "ruling": "3 of 3: both method clauses and the answer clause are "
                  "evidenced. The scheme HEADER states a total that disagrees "
                  "with the sum of its own per-question marks, so the app "
                  "should flag the scheme rather than silently trusting either "
                  "number.",
        "basis": "clauses evidenced; the header/sum disagreement is a "
                 "scheme-integrity observation, not a marking judgement",
    },
]

PAPER_A = {
    "id": "paper-a",
    "title": "Reference Paper A - Algebra, Number and Ratio (O Level style)",
    "lesson_id": "Ch2L3",
    "scheme_stated_total": 29,
    "items": PAPER_A_ITEMS,
}

PAPER_B = {
    "id": "paper-b",
    "title": "Reference Paper B - Nested subquestions, repeated lesson id and a "
             "defective scheme header",
    "lesson_id": "Ch2L3",
    #: Deliberately WRONG. The per-question marks sum to 7; the header claims 8.
    #: This is the mark-scheme extraction error the app must surface.
    "scheme_stated_total": 8,
    "items": PAPER_B_ITEMS,
}

PAPERS_DATA = [PAPER_A, PAPER_B]


def scheme_total(paper: dict) -> int:
    return sum(item["marks_available"] for item in paper["items"])


def expected_total(paper: dict) -> int:
    return sum(item["expected"] for item in paper["items"])


# ------------------------------------------------------------------ writing

def write_pdf(path: Path, title: str, heading: str, lines: list[str]) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 9, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, heading, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    for line in lines:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(190, 5.4, line)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


def scheme_lines(paper: dict) -> list[str]:
    lines = [f"Lesson: {paper['lesson_id']}",
             f"Total: {paper['scheme_stated_total']} marks",
             "Marks in brackets are the marks available for that part.", ""]
    for item in paper["items"]:
        lines.append(f"Question {item['question']} "
                     f"[{item['marks_available']} mark"
                     f"{'s' if item['marks_available'] != 1 else ''}]")
        lines.append(f"  {item['prompt']}")
        for clause in item["scheme"]:
            lines.append(f"    {clause}")
        lines.append("")
    return lines


def student_lines(paper: dict) -> list[str]:
    lines = [f"Lesson: {paper['lesson_id']}", "Name: Reference Candidate", ""]
    for item in paper["items"]:
        lines.append(f"Question {item['question']}")
        lines.extend(f"  {line}" for line in item["student"] if line)
        lines.append("")
    return lines


def manifest(paper: dict, asset: dict) -> dict:
    return {
        "id": paper["id"],
        "title": paper["title"],
        "lesson_id": paper["lesson_id"],
        "scheme_pdf": asset["scheme_pdf"],
        "student_pdf": asset["student_pdf"],
        "marks_available": scheme_total(paper),
        "expected_total": expected_total(paper),
        "scheme_stated_total": paper["scheme_stated_total"],
        "scheme_header_disagrees": paper["scheme_stated_total"] != scheme_total(paper),
        "items": [
            {
                "question": item["question"],
                "prompt": item["prompt"],
                "marks_available": item["marks_available"],
                "expected": item["expected"],
                "categories": item["categories"],
                "scheme": item["scheme"],
                "student_answer": [line for line in item["student"] if line]
                                 or ["(blank — the candidate wrote nothing)"],
                "ruling": item["ruling"],
                "basis": item["basis"],
                **({"trap": item["trap"]} if item.get("trap") else {}),
            }
            for item in paper["items"]
        ],
    }


def review_sheet(papers: list[dict]) -> str:
    out = [
        "# Marking reference set — human verification sheet",
        "",
        "**This sheet is the certification step, and it is not done.** Every",
        "expected mark below is derived from the printed mark scheme clause by",
        "clause by the person who wrote the dataset. A competent maths teacher",
        "must confirm each row against the scheme before any accuracy figure",
        "from this set may be described as certified.",
        "",
        "How to sign off: read the scheme clause, read the student answer, read",
        "the ruling, and tick the box only if you agree with the expected mark.",
        "Then record your name in `docs/reference-marking/manifest.json` under",
        "`review.verified_by` (and the date under `review.verified_on`) and",
        "re-run the harness — it will not report CERTIFIED until you do.",
        "",
    ]
    for paper in papers:
        out.append(f"## {paper['id']} — {paper['title']}")
        out.append("")
        out.append(f"- Marks available: **{scheme_total(paper)}**")
        out.append(f"- Expected awarded: **{expected_total(paper)}**")
        if paper["scheme_stated_total"] != scheme_total(paper):
            out.append(f"- **The scheme header claims "
                       f"{paper['scheme_stated_total']} marks while the parts "
                       f"sum to {scheme_total(paper)}.** This is deliberate: it "
                       f"is the mark-scheme extraction error the app must flag.")
        out.append("")
        out.append("| ✓ | Q | Available | Expected | Categories | Ruling |")
        out.append("|---|---|---|---|---|---|")
        for item in paper["items"]:
            ruling = item["ruling"].replace("|", "/")
            out.append(f"| [ ] | {item['question']} | {item['marks_available']} "
                       f"| **{item['expected']}** | "
                       f"{', '.join(item['categories'])} | {ruling} |")
        out.append("")
        out.append("<details><summary>Scheme clauses and student answers</summary>")
        out.append("")
        for item in paper["items"]:
            out.append(f"**Q{item['question']}** — {item['prompt']}")
            out.append("")
            out.append("Scheme:")
            out.extend(f"- {clause}" for clause in item["scheme"])
            out.append("")
            out.append("Student wrote:")
            out.append("```")
            out.extend(item["student"])
            out.append("```")
            out.append(f"Basis for the expectation: {item['basis']}")
            if item.get("trap"):
                out.append(f"Watch out: {item['trap']}")
            out.append("")
        out.append("</details>")
        out.append("")
    out.append("## Sign-off")
    out.append("")
    out.append("| Paper | Reviewer | Date | Decision |")
    out.append("|---|---|---|---|")
    for paper in papers:
        out.append(f"| {paper['id']} |  |  | [ ] agreed / [ ] disputed |")
    out.append("")
    return "\n".join(out)


def main() -> int:
    REF.mkdir(parents=True, exist_ok=True)
    PAPERS.mkdir(parents=True, exist_ok=True)

    papers, categories = [], set()
    for paper in PAPERS_DATA:
        scheme_pdf = PAPERS / f"{paper['id']}-mark-scheme.pdf"
        student_pdf = PAPERS / f"{paper['id']}-student.pdf"
        write_pdf(scheme_pdf, paper["title"], "MARK SCHEME", scheme_lines(paper))
        write_pdf(student_pdf, paper["title"], "CANDIDATE SCRIPT",
                  student_lines(paper))
        asset = {
            "scheme_pdf": str(scheme_pdf.relative_to(REF)).replace("\\", "/"),
            "student_pdf": str(student_pdf.relative_to(REF)).replace("\\", "/"),
        }
        papers.append(manifest(paper, asset))
        for item in paper["items"]:
            categories.update(item["categories"])

    payload = {
        "version": 1,
        "generated_by": "backend/scripts/make_marking_reference.py",
        "purpose": (
            "Measure marking accuracy against expectations derived from the "
            "printed mark scheme. The model's own output is never ground truth."),
        "review": {
            "required": True,
            "verified_by": None,
            "verified_on": None,
            "instructions": (
                "A competent maths teacher must confirm every expected mark in "
                "REVIEW_SHEET.md against the scheme, then record their name and "
                "the date here. Until then the harness reports NOT CERTIFIED."),
        },
        "categories": sorted(categories),
        "covers": {
            "fully-correct": "Q1, Q2, Q3, Q5, Q6, Q8 of paper A",
            "incorrect": "Q7 of paper A",
            "partially-correct-working": "Q9, Q12 of paper A; Q1c of paper B",
            "correct-alternative-method": "Q5 of paper A (grid/area method)",
            "equivalent-mathematical-expressions": "Q2, Q6 of paper A",
            "method-and-accuracy-marks": "Q1, Q8, Q9, Q12 of paper A",
            "follow-through-marking": "Q10 of paper A",
            "missing-answers": "Q4 of paper A",
            "ambiguous-answers": "Q11 of paper A",
            "mark-scheme-extraction-errors": (
                "paper B: the scheme header claims 8 marks while its parts sum "
                "to 7"),
            "nested-subquestions": "Q1a, Q1b, Q1c of paper B; Q3 of paper A",
            "overlapping-lesson-identifiers": (
                "both papers carry the same lesson id Ch2L3, and paper B nests "
                "parts inside question 1"),
        },
        "papers": papers,
    }
    (REF / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n",
                                       encoding="utf-8")
    (REF / "REVIEW_SHEET.md").write_text(review_sheet(PAPERS_DATA),
                                         encoding="utf-8")

    print(f"reference set written to {REF}")
    for paper in papers:
        marker = "  <-- scheme header disagrees with its parts" \
            if paper["scheme_header_disagrees"] else ""
        print(f"  {paper['id']}: {len(paper['items'])} items, "
              f"{paper['marks_available']} marks available, "
              f"{paper['expected_total']} expected{marker}")
    print(f"  categories covered: {len(payload['categories'])}")
    print(f"  human verification: {payload['review']['verified_by'] or 'NOT DONE'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
