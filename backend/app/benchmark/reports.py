"""Render a benchmark run as a human-readable, auditable report.

The report deliberately leads with certification status and sample size, not
with an accuracy percentage. A number computed from five unverified questions
must never look like a system-wide claim.
"""

from __future__ import annotations

from typing import Any


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _num(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}" if isinstance(value, float) else str(value)


def _ci(interval: Any) -> str:
    if not interval:
        return "—"
    return f"{interval[0] * 100:.1f}–{interval[1] * 100:.1f}%"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def render_report(record: dict) -> str:
    metrics = record.get("metrics") or {}
    sample = metrics.get("sample") or {}
    agreement = metrics.get("agreement") or {}
    error = metrics.get("error") or {}
    coverage = metrics.get("coverage") or {}
    review = metrics.get("review") or {}
    grades = metrics.get("grades") or {}
    cert = record.get("certification") or {}
    dataset = record.get("dataset") or {}
    config = record.get("configuration") or {}
    env = record.get("environment") or {}

    status = str(record.get("status") or "unknown")
    certified = bool(record.get("certified"))
    lines: list[str] = []
    lines.append("# Marking accuracy benchmark")
    lines.append("")
    if certified:
        lines.append("## Teacher-Verified Benchmark Accuracy")
        lines.append("")
        lines.append(
            "Every reference item in this result carries an explicit teacher "
            "confirmation. The score describes this dataset, not every future "
            "student paper.")
    else:
        lines.append("## Certified accuracy: UNAVAILABLE")
        lines.append("")
        lines.append(
            f"**Do not read this run as a teacher-certified accuracy figure.** "
            f"Status: `{status}`. Reason: {cert.get('reason') or 'not certified'}.")
        if sample.get("certification_basis") == "scheme_reference_not_certified":
            lines.append("")
            lines.append(
                "This run measured the marker against scheme-derived reference "
                "marks. Those marks were transcribed clause-by-clause from a "
                "printed scheme, but no qualified teacher has confirmed them.")
    lines.append("")
    lines.append(_table(
        ["Field", "Value"],
        [
            ["Run", record.get("run_id")],
            ["Status", status],
            ["Certification basis", cert.get("basis")],
            ["Partition", record.get("partition")],
            ["Mode", record.get("mode")],
            ["Started", record.get("started_at")],
            ["Finished", record.get("finished_at")],
            ["Dataset version", dataset.get("version")],
            ["Dataset SHA-256", dataset.get("sha256")],
            ["References evaluated", sample.get("references_expected", 0)],
            ["Scripts evaluated", sample.get("papers_processed", 0)],
            ["Marks represented", sample.get("marks_available", 0)],
        ],
    ))
    lines.append("")

    if status == "not_eligible":
        lines.append("No provider request was made: there was nothing eligible "
                     "to measure against.")
        lines.append("")

    # -- primary metrics ---------------------------------------------------
    lines.append("## Agreement")
    lines.append("")
    lines.append("Every denominator is explicit. Missing items are reported "
                 "separately and are never counted as agreement.")
    lines.append("")
    rows = []
    for key, label in (
        ("items", "Exact item agreement"),
        ("questions", "Exact question-level agreement"),
        ("subquestions", "Exact subquestion-level agreement"),
        ("total", "Exact total-mark agreement"),
    ):
        part = agreement.get(key) or {}
        rows.append([
            label,
            f"{part.get('exact', 0)}/{part.get('compared', 0)}",
            _pct(part.get("rate")),
            _ci(part.get("ci95")),
            (part.get("denominator") or ""),
        ])
    lines.append(_table(
        ["Metric", "Exact", "Rate", "95% Wilson CI", "Denominator"],
        rows,
    ))
    lines.append("")

    lines.append("## Error size and direction")
    lines.append("")
    over = error.get("overmarking") or {}
    under = error.get("undermarking") or {}
    lines.append(_table(
        ["Metric", "Value", "Denominator / note"],
        [
            ["Mean absolute error (marks)", _num(error.get("mean_absolute_error")),
             "matched items with a valid AI mark"],
            ["Normalised MAE (fraction of marks)", _num(error.get("normalized_mae")),
             "matched items with maximum marks > 0"],
            ["Overmarking rate", _pct(over.get("rate")),
             f"{over.get('count', 0)} overmarked / matched items"],
            ["Average overmarking", _num(over.get("mean_magnitude")),
             "overmarked items only"],
            ["Undermarking rate", _pct(under.get("rate")),
             f"{under.get('count', 0)} undermarked / matched items"],
            ["Average undermarking", _num(under.get("mean_magnitude")),
             "undermarked items only"],
            ["Scripts with exact total", _pct((agreement.get("total") or {}).get("rate")),
             f"{(agreement.get('total') or {}).get('compared', 0)} fully covered scripts"],
        ],
    ))
    lines.append("")

    # -- coverage / safety -------------------------------------------------
    lines.append("## Coverage and safety metrics")
    lines.append("")
    lines.append(_table(
        ["Metric", "Value", "Meaning"],
        [
            ["Question coverage", _pct(coverage.get("question_coverage")),
             f"{coverage.get('questions_processed', 0)} processed / "
             f"{sample.get('questions_expected', 0)} eligible"],
            ["Subquestion coverage", _pct(coverage.get("subquestion_coverage")),
             f"{coverage.get('subquestions_processed', 0)} processed / "
             f"{sample.get('subquestions_expected', 0)} eligible"],
            ["Missing mark count", coverage.get("missing_mark_count", 0),
             "eligible items the marker did not return"],
            ["Invalid result count", coverage.get("invalid_result_count", 0),
             "marks outside 0..max or not integral"],
            ["Scripts failed", coverage.get("scripts_failed", 0),
             "provider/parsing failures"],
            ["Schema/validation failure rate",
             _pct(coverage.get("schema_validation_failure_rate")),
             "failed scripts / scripts attempted"],
            ["Review-flag rate", _pct(review.get("review_flag_rate")),
             f"{review.get('flagged_scripts', 0)} flagged / scripts processed"],
            ["Flagged with disagreement", review.get("flagged_with_disagreement", 0),
             "flags are evaluated separately from actual errors"],
            ["Flagged without disagreement", review.get("flagged_without_disagreement", 0),
             "a review flag is not proof of a wrong mark"],
            ["Human review rate", _pct(review.get("human_review_rate")),
             f"{review.get('items_requiring_investigation', 0)} disagreements / matched items"],
            ["Unresolved references", coverage.get("unresolved_reference_count", 0),
             "disputed or incomplete teacher assessments"],
            ["Grade agreement", _pct(grades.get("rate")),
             grades.get("reason") or f"{grades.get('compared', 0)} verified grades"],
        ],
    ))
    lines.append("")

    items = record.get("reference_snapshot", {}).get("items") or []
    by_level = {"question": 0, "subquestion": 0}
    categories: dict[str, int] = {}
    papers = {item.get("paper_id") for item in items}
    topics = sorted({str(item.get("topic") or "Not recorded") for item in items})
    for item in items:
        by_level[item.get("level", "question")] = (
            by_level.get(item.get("level", "question"), 0) + 1)
        for category in item.get("categories") or []:
            categories[category] = categories.get(category, 0) + 1
    lines.append("## Dataset")
    lines.append("")
    lines.append(_table(
        ["Field", "Value"],
        [
            ["Teacher-verified references", cert.get("teacher_verified_references", 0)],
            ["Reference items in full dataset", dataset.get("item_count", 0)],
            ["Coverage of dataset", _pct(dataset.get("coverage_of_dataset"))],
            ["Distinct papers", len(papers)],
            ["Topics", ", ".join(topics) or "—"],
            ["Question-level references evaluated", by_level.get("question", 0)],
            ["Subquestion-level references evaluated", by_level.get("subquestion", 0)],
            ["Categories", ", ".join(f"{k} ({v})" for k, v in sorted(categories.items()))
             or "—"],
            ["Dataset warnings", "; ".join(dataset.get("warnings") or []) or "none"],
        ],
    ))
    lines.append("")

    # -- disagreements -----------------------------------------------------
    discrepancies = metrics.get("disagreements") or []
    lines.append("## Disagreement analysis")
    lines.append("")
    if not discrepancies:
        lines.append("No disagreements between the awarded and reference marks "
                     "were found in the matched items.")
    else:
        lines.append(
            f"{len(discrepancies)} matched item(s) disagree. Error categories are "
            "hypotheses derived from the reference item's tags; no root cause is "
            "asserted from a numerical mismatch alone.")
        lines.append("")
        table_rows = []
        for row in discrepancies:
            table_rows.append([
                row.get("reference_id"),
                row.get("question"),
                f"{row.get('ai_mark')}/{row.get('max_marks')}",
                row.get("teacher_mark"),
                f"{row.get('difference'):+d}",
                row.get("direction"),
                row.get("error_category"),
                "yes",
            ])
        lines.append(_table(
            ["Reference", "Q", "AI", "Teacher", "Diff", "Direction",
             "Hypothesis", "Investigate"],
            table_rows,
        ))
        lines.append("")
        groups = metrics.get("disagreement_groups") or {}
        if groups:
            lines.append("Grouped by likely error category:")
            lines.append("")
            for name, count in sorted(groups.items(), key=lambda kv: -kv[1]):
                lines.append(f"- {name}: {count}")
            lines.append("")
        lines.append("### Scheme evidence for each disagreement")
        lines.append("")
        for row in discrepancies:
            lines.append(f"**{row.get('reference_id')}** — "
                         f"{row.get('paper_title')}")
            lines.append("")
            lines.append(f"- Reference ruling: {row.get('basis') or '—'}")
            lines.append(f"- Scheme: {'; '.join(row.get('scheme') or []) or '—'}")
            lines.append(f"- AI: {row.get('ai_mark')}/{row.get('max_marks')}; "
                         f"teacher: {row.get('teacher_mark')}; "
                         f"difference {row.get('difference'):+d}")
            lines.append(f"- Hypothesis: {row.get('hypothesis')} "
                         "(unconfirmed; human investigation required)")
            lines.append("")
    lines.append("")

    excluded = metrics.get("excluded") or []
    lines.append("## Excluded references")
    lines.append("")
    if not excluded:
        lines.append("None.")
    else:
        reasons = metrics.get("excluded_reasons") or {}
        lines.append(f"{len(excluded)} reference item(s) were not part of the "
                     "evaluated set:")
        lines.append("")
        for reason, count in sorted(reasons.items()):
            lines.append(f"- `{reason}`: {count}")
    lines.append("")

    lines.append("## Run configuration and reproducibility")
    lines.append("")
    lines.append(_table(
        ["Field", "Value"],
        [
            ["Model", config.get("model")],
            ["Prompt version", config.get("prompt_version")],
            ["Correction mode", config.get("mode")],
            ["Config SHA-256", config.get("config_sha256")],
            ["Verification snapshot SHA-256",
             (record.get("verification") or {}).get("snapshot_sha256") or "not recorded"],
            ["App version", env.get("app_version")],
            ["Build", env.get("build_number")],
            ["Git commit", env.get("git_commit") or "not recorded"],
            ["Python", env.get("python")],
            ["Metrics reproduction", "PASS" if (record.get("reproduction") or {}).get(
                "reproduced") else "NOT VERIFIED"],
        ],
    ))
    lines.append("")
    lines.append(
        "The run record contains the reference snapshot and every raw outcome. "
        "`reproduce_run` recomputes these metrics from those inputs without "
        "contacting the provider or reading the live verification store.")
    lines.append("")

    lines.append("## Limitations")
    lines.append("")
    for limitation in record.get("limitations") or []:
        lines.append(f"- {limitation}")
    if not record.get("limitations"):
        lines.append("- None recorded.")
    lines.append("")
    lines.append("## Data safety")
    lines.append("")
    lines.append(
        "This benchmark wrote only files under its own artifact directory. It "
        "did not modify a production result, a student grade, a retry budget, a "
        "provider health state, or Google Classroom.")
    lines.append("")
    return "\n".join(lines)
