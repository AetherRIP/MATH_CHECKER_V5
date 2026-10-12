"""Read-only accuracy-audit API (testing-only view).

Serves the recorded evidence from the C1L13 - Sets audit: the live Classroom
inventory, the before/after real correction runs, per-question comparisons
against the mark scheme, confirmations and limitations, and the generated
report artefacts for in-app preview.

Nothing here writes: no grade, no report upload, no Classroom call. It only
reads artefacts under ``data/`` that the audit scripts produced.
"""
