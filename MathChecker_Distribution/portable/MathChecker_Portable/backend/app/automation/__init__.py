"""Automated Google Classroom correction workflow (orchestration layer).

This package does NOT implement a second correction system. It drives the
EXISTING pipeline end to end:

    monitor -> detect -> existing correction queue -> existing AI engine ->
    deterministic IGCSE grade -> existing report generator -> existing Drive
    upload + Classroom attachment -> Classroom grade -> return.

Every step is persisted, so a restart resumes from the exact step that failed
instead of re-running the AI marking, regenerating the report or submitting a
second grade.
"""

from . import states  # noqa: F401
