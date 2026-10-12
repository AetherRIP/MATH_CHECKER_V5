"""Assignment repair session (TESTING ONLY · REPAIRED RESULTS NOT PUBLISHED).

This package audits previously stored corrections against the official
mark-scheme blueprint, re-marks affected submissions through the corrected
pipeline, and stores the repaired results as DEDICATED TEST RECORDS
(``repair_runs`` / ``repair_items`` + ``data/repairs/`` artifacts).

Nothing in this package writes to ``results``, ``correction_jobs``,
``classroom_publish_ops`` or any Google Classroom / Drive surface. The
original correction records are preserved for audit and comparison.
"""

from .audit import Finding, audit_result  # noqa: F401
from .store import (  # noqa: F401
    add_item, create_run, finish_run, item_path, load_run, load_summary,
)
