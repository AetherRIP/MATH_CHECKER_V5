"""Dedicated Large PDF Worker for long student answer PDFs.

Inspects a long student answer PDF, splits it into balanced question-aware
batches, corrects each batch through the EXISTING correction engine, validates
the batch results, merges them by canonical question id, and only then writes
the final result. The existing single-pass correction page and engine are
untouched; this package reuses them.
"""

from __future__ import annotations

__all__ = ["runner", "store", "planner", "validate", "states"]
