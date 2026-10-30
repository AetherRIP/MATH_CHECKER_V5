"""Teacher-certified marking accuracy benchmark.

The package is deliberately separate from the correction pipeline it measures:

``dataset``   loads and validates the versioned reference set,
``store``     keeps the append-only teacher verification overlay,
``metrics``   computes accuracy, coverage and disagreement statistics,
``runner``    executes a run and writes reproducible artifacts,
``reports``   renders the human-readable report.

Nothing in here writes to the production ``results`` table, Classroom, or the
retry scheduler. A benchmark is an observation of the marker, never an edit of
a student's grade.
"""

from __future__ import annotations

from .dataset import (DEFAULT_DATASET, DatasetError, ReferenceDataset,
                      ReferenceItem, load_dataset)
from .metrics import compute_metrics
from .store import VerificationStore, resolve_references

__all__ = [
    "DEFAULT_DATASET",
    "DatasetError",
    "ReferenceDataset",
    "ReferenceItem",
    "VerificationStore",
    "compute_metrics",
    "load_dataset",
    "resolve_references",
]
