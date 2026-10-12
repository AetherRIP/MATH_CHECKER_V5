"""Centralised batch planning for long student answer PDFs.

The correction engine (``legacy_marking.grade_with_model``) is a single-shot
marker: it receives one or more student files plus the mark scheme and returns
question-level results. Feeding it a 52-page answer PDF works until the request
becomes too large / too slow / too lossy to be reliable. This module solves
that *purely* — it turns a page count (and optional question/page evidence)
into a fixed, complete plan of page ranges for the Large PDF Worker.

Design rules (mirrors the task acceptance criteria):

* Target **24-28 pages** per batch. Anything at or below ``target_max`` stays a
  SINGLE batch, so the existing single-pass workflow is preserved unchanged.
* Pages are distributed **as evenly as practical** — batches differ by at most
  one page. No "28 + 28 + 1" tail is ever produced.
* Every page has **exactly one primary owner**. Context overlap (if configured)
  is recorded separately and never scored twice.
* Question-aware: no question is (knowingly) split across a boundary. When the
  evidence says a question spans a boundary the boundary is moved, inside a
  bounded exception band; if it cannot be moved safely the split is *flagged*
  rather than silently accepted.

Everything here is a pure function of integers and small maps: no network, no
model, no I/O — which is what makes the plan reproducible and auditable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

# Centralised batch configuration (the ONLY place these numbers live).
TARGET_MIN = 24
TARGET_MAX = 28
# A boundary may be moved at most this many pages to keep a question whole.
MAX_SHIFT = 6


@dataclass(frozen=True)
class Batch:
    """One batch: a contiguous, owned page range (1-based, inclusive)."""

    index: int
    start: int
    end: int
    context_before: int = 0
    context_after: int = 0
    question_ids: tuple[str, ...] = ()
    adjusted: bool = False
    notes: tuple[str, ...] = ()

    @property
    def pages(self) -> int:
        return self.end - self.start + 1

    def owns(self, page: int) -> bool:
        return self.start <= page <= self.end

    def read_range(self) -> tuple[int, int]:
        """Inclusive range to *extract* (owned pages + context)."""
        return (max(1, self.start - self.context_before),
                self.end + self.context_after)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "pages": self.pages,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "question_ids": list(self.question_ids),
            "adjusted": self.adjusted,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Batch":
        return cls(
            index=int(data["index"]),
            start=int(data["start"]),
            end=int(data["end"]),
            context_before=int(data.get("context_before", 0) or 0),
            context_after=int(data.get("context_after", 0) or 0),
            question_ids=tuple(str(q) for q in (data.get("question_ids") or [])),
            adjusted=bool(data.get("adjusted")),
            notes=tuple(str(n) for n in (data.get("notes") or [])),
        )




@dataclass
class BatchPlan:
    """The complete, validated batch plan for one student answer PDF."""

    page_count: int
    batches: list[Batch]
    single_pass: bool
    target_min: int = TARGET_MIN
    target_max: int = TARGET_MAX
    notes: list[str] = field(default_factory=list)
    question_pages: dict[str, list[int]] = field(default_factory=dict)
    # Questions whose pages straddle a batch boundary that could NOT be moved
    # safely: a batch sees only part of the answer unless the boundary is
    # resolved. The merge layer turns these into NEEDS_REVIEW so the missing
    # continuation can never silently look like 0 marks.
    flagged_questions: list[str] = field(default_factory=list)

    @property
    def batch_count(self) -> int:
        return len(self.batches)

    def owner_of(self, page: int) -> int | None:
        """Batch index that owns ``page`` (exactly one by construction)."""
        for batch in self.batches:
            if batch.owns(page):
                return batch.index
        return None

    def owned_pages(self) -> list[int]:
        return [p for b in self.batches for p in range(b.start, b.end + 1)]

    def question_owner(self, question_id: str) -> int | None:
        """The batch that owns a question (its first appearance page)."""
        pages = self.question_pages.get(str(question_id))
        if not pages:
            return None
        return self.owner_of(min(pages))

    def to_dict(self) -> dict:
        return {
            "page_count": self.page_count,
            "single_pass": self.single_pass,
            "batch_count": self.batch_count,
            "target_min": self.target_min,
            "target_max": self.target_max,
            "notes": list(self.notes),
            "batches": [b.to_dict() for b in self.batches],
            "question_pages": {k: list(v) for k, v in self.question_pages.items()},
            "flagged_questions": list(self.flagged_questions),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchPlan":
        return cls(
            page_count=int(data["page_count"]),
            batches=[Batch.from_dict(b) for b in data.get("batches", [])],
            single_pass=bool(data.get("single_pass")),
            target_min=int(data.get("target_min", TARGET_MIN)),
            target_max=int(data.get("target_max", TARGET_MAX)),
            notes=list(data.get("notes") or []),
            question_pages={str(k): [int(p) for p in v]
                            for k, v in (data.get("question_pages") or {}).items()},
            flagged_questions=[str(q) for q in
                               (data.get("flagged_questions") or [])],
        )

    def identity(self) -> str:
        """Stable hash of the plan boundaries (for idempotency keys)."""
        payload = json.dumps(
            [[b.start, b.end, b.context_before, b.context_after]
             for b in self.batches],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def even_sizes(page_count: int, n: int) -> list[int]:
    """Split ``page_count`` into ``n`` sizes that differ by at most one.

    The larger batches come last so the sequence reads ascending, matching the
    documented examples (29 -> 14+15, 57 -> 19+19+19).
    """
    if n <= 0:
        raise ValueError("batch count must be positive")
    if page_count < n:
        raise ValueError("cannot split more batches than pages")
    base, remainder = divmod(page_count, n)
    return [base] * (n - remainder) + [base + 1] * remainder


def _batch_count_for(page_count: int, target_max: int) -> int:
    # ceil(page_count / target_max), computed without floats.
    return -(-page_count // target_max)


def _adjust_boundaries(bounds: list[list[int]], page_count: int,
                       question_pages: dict[str, list[int]],
                       target_min: int, target_max: int, max_shift: int,
                       notes: list[str]) -> tuple[list[int], set[int]]:
    """Move internal boundaries so complete questions stay in one batch.

    Returns ``(flagged_boundaries, adjusted_batch_indices)``. Flagged
    boundaries are the ones left splitting a question (surfaced for review);
    adjusted indices are the batches whose range was changed.
    """
    flagged: list[int] = []
    flagged_questions: list[str] = []
    adjusted: set[int] = set()
    if not question_pages or len(bounds) < 2:
        return flagged, flagged_questions,adjusted

    # Normalise the evidence: question id -> sorted unique pages.
    spans: dict[str, list[int]] = {}
    for qid, pages in question_pages.items():
        clean = sorted({int(p) for p in pages if 1 <= int(p) <= page_count})
        if clean:
            spans[str(qid)] = clean

    for i in range(len(bounds) - 1):
        left = bounds[i]
        near = bounds[i + 1]
        boundary = left[1]
        straddling = sorted(
            qid for qid, pages in spans.items()
            if boundary in pages and (boundary + 1) in pages)
        if not straddling:
            continue

        # Each bridging question wants either the boundary just before it
        # starts (whole question -> next batch) or just after it ends (whole
        # question stays -> this batch). Pick the smallest feasible move.
        best: tuple[int, int] | None = None  # (shift, new_boundary)
        for qid in straddling:
            pages = spans[qid]
            options = (min(pages) - 1, max(pages))
            for new_boundary in options:
                if new_boundary < left[0]:
                    continue  # would empty the left batch
                if new_boundary >= near[1]:
                    continue  # would empty the right batch
                shift = abs(new_boundary - boundary)
                if shift > max_shift:
                    continue
                # Never let an adjustment push a batch outside the small
                # documented exception band.
                left_size = new_boundary - left[0] + 1
                right_size = near[1] - new_boundary
                if left_size > target_max + max_shift:
                    continue
                if right_size > target_max + max_shift:
                    continue
                if best is None or shift < best[0]:
                    best = (shift, new_boundary)

        if best is None:
            flagged.append(boundary)
            # The split question's continuation pages live in the NEXT batch
            # while its pages live here: each batch sees only part of the
            # answer. Neither fragment may be trusted as a complete result,
            # and the un-marked remainder must never be silently treated as
            # zero — the merge layer therefore flags these rows for review.
            flagged_questions.extend(straddling)
            notes.append(
                f"Page {boundary}|{boundary + 1} splits question(s) "
                f"{', '.join(straddling)} and could not be moved safely — "
                f"flagged for teacher review.")
            continue

        _, new_boundary = best
        left[1] = new_boundary
        near[0] = new_boundary + 1
        adjusted.add(i)
        adjusted.add(i + 1)
        notes.append(
            f"Boundary moved {boundary}->{new_boundary} to keep question(s) "
            f"{', '.join(straddling)} whole.")
    return flagged, sorted(set(flagged_questions)), adjusted


def plan_batches(page_count: int, *,
                 target_min: int = TARGET_MIN,
                 target_max: int = TARGET_MAX,
                 question_pages: dict[str, list[int]] | None = None,
                 max_shift: int = MAX_SHIFT,
                 overlap: int = 0) -> BatchPlan:
    """Build the complete batch plan for a student answer PDF.

    ``question_pages`` maps a question identifier to the student-PDF pages on
    which it appears (from deterministic inspection). It is optional: without
    it the plan is still a valid, balanced partition — it simply cannot make
    question-aware adjustments.

    ``overlap`` adds context pages (never scored) so a batch can see a page
    before/after its owned range. Each page still has exactly one owner.
    """
    if page_count < 1:
        raise ValueError("page_count must be >= 1")
    if target_min < 1 or target_max < target_min:
        raise ValueError("invalid batch size window")

    notes: list[str] = []
    if page_count <= target_max:
        batches = [Batch(index=0, start=1, end=page_count)]
        return BatchPlan(page_count=page_count, batches=batches, single_pass=True,
                         target_min=target_min, target_max=target_max,
                         notes=notes, question_pages=dict(question_pages or {}))

    n = _batch_count_for(page_count, target_max)
    sizes = even_sizes(page_count, n)
    bounds: list[list[int]] = []
    start = 1
    for size in sizes:
        bounds.append([start, start + size - 1])
        start += size

    evidence = {str(k): v for k, v in (question_pages or {}).items()}
    flagged, flagged_questions, adjusted = _adjust_boundaries(
        bounds, page_count, evidence, target_min, target_max, max_shift,
        notes)

    overlap = max(0, int(overlap or 0))
    batches: list[Batch] = []
    for index, (lo, hi) in enumerate(bounds):
        question_ids = tuple(sorted(
            (qid for qid, pages in evidence.items()
             if any(lo <= p <= hi for p in pages)),
            key=_question_sort_key))
        batches.append(Batch(
            index=index, start=lo, end=hi,
            context_before=overlap if index > 0 else 0,
            context_after=overlap if index < len(bounds) - 1 else 0,
            question_ids=question_ids,
            adjusted=index in adjusted,
        ))

    if flagged:
        notes.append(
            f"{len(flagged)} ambiguous boundary(ies) — those questions may be "
            f"split; the merge step requires teacher review if coverage fails.")

    return BatchPlan(page_count=page_count, batches=batches, single_pass=False,
                     target_min=target_min, target_max=target_max,
                     notes=notes, question_pages=evidence,
                     flagged_questions=flagged_questions)


def _question_sort_key(qid: str) -> tuple:
    """Sort question ids numerically when possible, lexically otherwise."""
    text = str(qid).strip()
    lead = ""
    for ch in text:
        if ch.isdigit():
            lead += ch
        else:
            break
    try:
        return (0, int(lead), text)
    except ValueError:
        return (1, 0, text)


def validate_plan(plan: BatchPlan) -> list[str]:
    """Return a list of structural problems (empty == a safe plan).

    Guarantees enforced here:
      * every page in 1..page_count is owned by exactly one batch;
      * page ranges are contiguous and non-overlapping;
      * no batch exceeds the documented exception band.
    """
    problems: list[str] = []
    if plan.page_count < 1:
        problems.append("page_count must be >= 1")
        return problems
    if not plan.batches:
        problems.append("plan has no batches")
        return problems

    seen: dict[int, int] = {}
    for batch in plan.batches:
        if batch.start < 1 or batch.end > plan.page_count:
            problems.append(
                f"batch {batch.index} range {batch.start}-{batch.end} is outside "
                f"1..{plan.page_count}")
        if batch.end < batch.start:
            problems.append(f"batch {batch.index} has an inverted range")
        for page in range(batch.start, batch.end + 1):
            if page in seen:
                problems.append(
                    f"page {page} is owned by both batch {seen[page]} and "
                    f"batch {batch.index}")
            seen[page] = batch.index
        if not plan.single_pass and batch.pages > plan.target_max + MAX_SHIFT:
            problems.append(
                f"batch {batch.index} has {batch.pages} pages, beyond the "
                f"documented exception band "
                f"({plan.target_max + MAX_SHIFT})")

    expected = set(range(1, plan.page_count + 1))
    missing = sorted(expected - set(seen))
    if missing:
        problems.append(f"pages not owned by any batch: {missing}")
    return problems
