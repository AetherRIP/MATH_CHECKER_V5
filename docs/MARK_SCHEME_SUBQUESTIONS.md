# Mark-scheme subquestions: detection, answer mapping and mark allocation

This records the audit of how an Edexcel mark scheme is turned into marking
instructions, what was actually broken, what changed, and what remains a
limitation. It describes the **current** behaviour of the code; the regression
suite that pins it down is `backend/tests/test_subquestion_scheme.py`.

## 1. Root causes (measured, not assumed)

Four independent faults compounded each other. Only the first is in the AI path;
the rest are deterministic and were reproducible offline.

### 1.1 Two different subquestions shared one identity

`question_inventory.canonical_question_id` keeps the main number plus the FIRST
letter group, and stops there:

```
canonical_question_id("4(b)(i)")   -> "4b"
canonical_question_id("4(b)(ii)")  -> "4b"
```

That id is exactly what the reconciliation groups the marker's returned rows
onto (`blueprint.apply_blueprint`). Two marking points under one identity means
a result for `4(b)(ii)` can be filed under `4(b)(i)`: the answer attached to the
wrong subquestion, and one part's marks standing in for another's.

### 1.2 Sub-parts were collected and then thrown away

The extraction schema (both `blueprint.EXTRACTION_SCHEMA` and
`mark_scheme_verify.VERIFY_SCHEMA`) asks the model for a per-question `parts`
array, and the verification prompt spells out that every labelled sub-part must
be its own entry. But `blueprint._clean_questions` (and
`mark_scheme_verify.canonicalize`) rebuilt each question as
`question_number / max_marks / criteria` and **dropped `parts` entirely**. Every
sub-part's own answer and M/A/B rules were lost, so nothing downstream could
attribute an answer to a part even if it wanted to.

### 1.3 The marking prompt never carried the sub-parts

`legacy_marking.marking_user_prompt(blueprint)` (the text actually sent after
the uploaded files) appended `blueprint.prompt_block(blueprint)`. That renderer
walked `question["subquestions"]` — a key raw extracted rows do not have; their
parts live under `parts`. So the prompt printed only whole-question lines and
truncated each `criteria` string at 400 characters *silently*. A prompt that
looks structured while carrying none of the structure is the worst case: the
marker is still asked to reconstruct every answer→subquestion relationship from
one wall of text, and it cannot.

### 1.4 A partly-marked question read as complete

Because no sub-parts were declared, `apply_blueprint` had nothing to reconcile
against: a result covering only `4(a)` and `4(b)(i)` produced a question 4 row
that looked finished, with `4(b)(ii)` silently unmarked. That is how an
un-assessed marking point becomes a silent zero. A second, subtler instance of
the same fault: a returned row labelled in the ordinary `4(a)` style was matched
to question 4 by the `_matches` shortcut, which never resolved *which* declared
subquestion it covered — so the row's marks were kept on the question but
attributed to no marking point, and every declared part read as unmarked.

## 2. The structured subquestion layer

`backend/app/engine/scheme_structure.py` (new) is a pure, deterministic module —
no model, no network, no I/O:

* **Identity.** `subquestion_id` keeps every nesting level
  (`"4(b)(ii)"` → `"4bii"`, `"2.1"` → `"2.1"`), `parent_id` walks the nesting
  (`"4bii"` → `"4b"` → `"4"`), `root_id` gives the top-level question.
  A local label such as `a`, `i` or `ii` is never an identity on its own;
  `local_subquestion("4", "b(ii)")` scopes it to `4bii`.
* **Marks.** `extract_mark_codes` separates M (method), A (accuracy) and B
  (independent) marks and keeps the modifiers that change their meaning
  (`ft`, `oe`, `cao`, `ECF`, `ISW`, `dep`, `AG`, `SP`). The count of codes FOUND
  is evidence about the criteria; it never replaces the official max marks.
* **Answers.** `extract_answers` splits a row on the words a real scheme uses to
  introduce an alternative (`or`, `accept`, `oe`, `e.g.`, `any equivalent`) and
  strips the mark annotations, so every accepted answer stays attached to the
  subquestion it was read from. An answer printed *before* its mark annotation
  (`"x = 7 (M1 A1)"`) is read correctly.
* **Structure.** `build_structure` turns extracted rows into parents +
  subquestions with explicit `id`, `label`, `parent_id`, `criteria`, `answers`,
  `marks`, `max_marks`, `page`, `confidence`, `validation` and `issues`.
  Local labels are scoped to their parent, so two questions can both have a part
  `a` without colliding.
* **Validation.** `validate_structure` reports duplicate identities, a
  subquestion with no answer or criteria, sub-part marks that do not add up to
  the parent, a subquestion listed under the wrong question, a missing
  allocation, a question with no subquestions, an implausible subquestion count,
  and a mark total that disagrees with the printed total. It **describes**; it
  never changes a mark or rewrites the source.
* **Prompt.** `prompt_block` prints one labelled line per subquestion with its
  own id, answers and M/A/B codes, and marks any truncation explicitly
  (`…[criteria truncated for length]`). `declared_questions` accepts either a
  raw extracted blueprint (parts under `parts`) or an already-built structure,
  which is the guard that stops the prompt from silently printing no
  subquestions at all (§1.3).

## 3. Mapping answers and marking criteria to their identifier

`question_inventory.expected_inventory` declares every subquestion as expected
in its own right (a question that declares `parts` contributes one inventory row
per part, with `parent_question`); `missing_from_results` treats a parent's
result as *covering* the question but not as proof that a declared sub-part was
assessed, while a returned sub-part does cover its own question.

`blueprint.apply_blueprint` reconciles a marking result against that structure:

* Each returned row is attributed to the declared subquestion it **names**,
  through `_declared_subquestion_for`: the full nested label decides
  (`4(b)(ii)` → `4bii`), never the first letter group, and the `4(a)` style of
  row is resolved the same way the exotic ones are.
* A row that names a label the question does not declare at all (`4(c)(iv)`
  under a scheme with no `(c)`) is reported in `unmatched_rows` and stays
  unattributed; a grouping label that is genuinely ambiguous (`4(b)` while
  `(b)(i)` and `(b)(ii)` are declared) is reported in `unattributed_rows`. An
  ancestor label is never guessed into one of its children.
* Each declared subquestion gets its own breakdown entry carrying its own
  awarded marks (capped at **its own** maximum), status (`MARKED`/`UNMARKED`),
  the rows that produced it, its answers and its mark codes.
* Unreturned declared sub-parts are reported in `missing_subquestions` and the
  row is held at `REVIEW` with an explicit reason; two rows claiming one
  subquestion are reported in `subquestion_conflicts` (they are never silently
  summed into one number); an over-awarded row is clamped and reported in
  `award_clamped`.
* An UNDECLARED question row is still flagged, never added to score.

Consequently one subquestion's marks cannot leak into a sibling: the sibling's
allowance is its own maximum, and the reliability flags travel with the row.

## 4. Uncertain or malformed extractions

* `verify_result` turns the reconciliation report into anomalies (missing
  subquestion, conflicts, clamped awards, unreadable rows) with the subquestion
  named by its scoped label (`4b(ii)`, not `ii`), and every `status != VALID`
  row enters `review_questions`. Finalization is blocked there, exactly as it
  already was for a missing *question*.
* The `/assignments/{id}/mark-scheme` endpoint returns the per-subquestion
  structure, the subquestion counts, the subquestion-level anomalies and the
  confidence reasons. `AssignmentsPage` renders them as a *Subquestion
  structure* panel, so review can target the marking point that is wrong
  instead of invalidating an otherwise-correct scheme.
* `ResultsPage` shows the per-subquestion breakdown inside the existing
  correction viewer, including the parts the marker did not return, labelled
  `UNMARKED`.
* A scheme whose structure is rejected outright (`blueprint.validate` false —
  duplicate numbers, a missing allocation) falls back to free-form marking
  rather than blocking the work.
* Existing safeguard preserved: a valid alternative method still wins. A
  `scheme_check` disagreement with a defensible justification does **not** lower
  the award and does not by itself raise a review flag; the original mark scheme
  is never edited, and interpretations are proposed separately in the report.

## 5. What the checker validates

`mark_scheme_verify.cross_validate` runs the same subquestion validation over
the reconstructed structure and adds `subquestion_counts` and `subquestions` to
its checks; `blueprint.structure_report` exposes `has_subquestions`,
`subquestion_count`, `subquestions` (with per-subquestion confidence),
`subquestion_anomalies` and `subquestions_complete`, which
`mark_scheme_confidence` and `validation_warnings` fold into the headline
verdict. A flat scheme of whole questions gains no subquestion findings — the
check is only applied where the scheme actually declares parts.

## 6. Tests

`backend/tests/test_subquestion_scheme.py` — 81 tests, all deterministic. They
cover, among others: several subquestions with different answers; repeated local
labels under different questions; nested `4(b)(i)`/`4(b)(ii)`; a question
spanning two pages with a part whose answer continues onto the next page;
method/accuracy/independent marks per subquestion; multiple accepted answers;
follow-through; notation (`3/4`, `-2.5`, `n > 5`, `(3, -2)`, `3.14 (2 dp)`);
dotted `2.1` numbering; an answer before its mark annotation; missing,
duplicated and malformed labels; an ambiguous mapping that must be flagged
rather than guessed; a valid alternative method; a corrupted/incomplete scheme;
and confirmation that one subquestion's marks cannot appear under another. The
full backend suite (1862 tests) and the frontend TypeScript build pass.

Fixtures are synthetic. They reproduce the formatting problems the audit found
(including the layout patterns listed above) but they are not real examination
material, and passing them does not establish compatibility with every Edexcel
mark scheme.

## 7. Remaining limitations

* Extraction quality still depends on the PDF: a scanned or diagram-only page
  has no text layer, and the module works on the text the extractor produces.
  Equations, matrices and vectors survive only as well as that text does.
* Mathematical equivalence is still the marker's judgement. The structure says
  which answers belong to which part; it does not symbolically prove that
  `2x/4` equals `x/2`. Cases the marker cannot resolve are flagged for review
  rather than decided.
* The `parts` schema is the extracted contract: a mark scheme whose sub-parts
  the model never returns cannot be reconstructed from the document alone, and
  an image-only or two-column PDF is more likely to come back partial.
* Subquestion-level review is surfaced per subquestion, but a teacher still has
  to resolve a conflicted or unmarked part manually; the pipeline refuses to
  choose for them by design.
* `mark_scheme_verify` and the structure report are read-only diagnostics: they
  report an unreliable extraction, they never repair the PDF or rewrite the
  cached blueprint.
