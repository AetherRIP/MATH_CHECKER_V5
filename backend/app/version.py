"""Single source of truth for application + data-schema versions (§44).

Keeping the version in one module (rather than as string literals scattered
across ``__init__``, ``main`` and the API routes) means the About/Diagnostics
surfaces, the boot log and the frontend can never disagree about which build
is running — and a future update can compare ``SCHEMA_VERSION`` to the value
recorded in the database before it touches anything.

Semantic versioning: MAJOR.MINOR.PATCH.
``SCHEMA_VERSION`` is tracked separately from the app version because a data
migration (a new column, a new table) is not the same event as a feature
release: many app releases ship with an unchanged schema.
"""

from __future__ import annotations

APP_NAME = "Math Checker"
APP_VERSION = "5.2.1"

# Build identity (§24). Bumped by the release process — the release pipeline
# only READS these, it never rewrites the master project. ``BUILD_DATE`` is
# informational; ``BUILD_NUMBER`` is what a support conversation quotes.
BUILD_NUMBER = "20261012"
BUILD_DATE = "2026-10-12"

# Bump ONLY when a change requires migrating stored data. ``db._migrate``
# records the version it has brought the database up to in ``app_meta``; an
# update that finds an older database migrates it forward, and one that finds
# a NEWER database than it understands refuses to silently downgrade it.
SCHEMA_VERSION = 5

def build_info() -> dict:
    """Public build identity (no secrets, safe to display and export)."""
    return {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "build_number": BUILD_NUMBER,
        "build_date": BUILD_DATE,
        "schema_version": SCHEMA_VERSION,
    }


__all__ = ["APP_NAME", "APP_VERSION", "BUILD_NUMBER", "BUILD_DATE",
           "SCHEMA_VERSION", "build_info"]
