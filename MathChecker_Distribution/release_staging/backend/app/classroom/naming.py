"""Teacher-controlled Google Classroom assignment DISPLAY names.

The automation must never force a prefix/suffix such as "Via Math Checker"
onto an assignment. An assignment has two distinct things:

* **internal identity** - Course ID + Coursework ID + the original Classroom
  name. These are how we find the assignment again and are never lost.
* **display name** - the title students and the teacher see in Classroom.
  This is editable metadata, and the teacher controls it completely.

Two behaviours live here:

1. :func:`compose_assignment_name` - an OPTIONAL default template applied when
   the app creates a NEW assignment. Every field defaults to empty, so the
   default behaviour is to preserve the original name EXACTLY. No prefix or
   suffix is ever added unless the teacher configured one.
2. :func:`rename_coursework` - change the displayed name of an existing
   assignment, verified against Google's own response (a 200 is not proof the
   title changed to what we asked for).
"""

from __future__ import annotations

import logging
import time

from .. import db
from ..logging_setup import log_event
from . import client as gc

log = logging.getLogger("classroom.naming")

#: The reserved text the old system used to append. Never produced unless the
#: teacher literally types it into their own custom name/prefix/suffix.
RESERVED_AUTO_TEXT = "Via Math Checker"


def _clean(value: str | None) -> str:
    return (value or "").strip()


def compose_assignment_name(original: str | None, *, mode: str = "original",
                            custom_name: str | None = None,
                            prefix: str | None = None,
                            suffix: str | None = None) -> str:
    """Return the display name to use for a (new) assignment.

    ``original``           - the Classroom assignment's own name.
    ``mode``               - ``"original"`` keeps it exactly; ``"custom"``
                             uses ``custom_name`` (falling back to original
                             when ``custom_name`` is empty).

    The prefix/suffix are applied around the base name and DEFAULT TO EMPTY.
    With every field empty the original name is returned byte-for-byte - so
    the app can never invent "Via Math Checker: ...".
    """
    orig = _clean(original)
    pre = _clean(prefix)
    suf = _clean(suffix)

    if mode == "custom" and _clean(custom_name):
        base = _clean(custom_name)
    else:
        base = orig

    parts = " ".join(p for p in (pre, base, suf) if p).strip()
    # If literally everything is empty, fall back to the original (never an
    # empty title, which Classroom rejects).
    return parts or orig


def naming_defaults() -> dict:
    """The stored default naming template (all fields empty by default)."""
    try:
        from ..config import get_config
        cfg = getattr(get_config(), "assignment_naming", None)
    except Exception:  # noqa: BLE001
        cfg = None
    if cfg is None:
        return {"mode": "original", "custom_name": "", "prefix": "", "suffix": ""}
    return {
        "mode": getattr(cfg, "mode", "original"),
        "custom_name": getattr(cfg, "custom_name", "") or "",
        "prefix": getattr(cfg, "prefix", "") or "",
        "suffix": getattr(cfg, "suffix", "") or "",
    }


def default_name_for(original: str | None) -> str:
    """Apply the stored default template to an original Classroom name."""
    return compose_assignment_name(original, **naming_defaults())


def rename_coursework(coursework_id: str, course_id: str, display_name: str,
                      *, verify: bool = True) -> dict:
    """Change an assignment's DISPLAY name, verifying Google's response.

    Internal identity (course_id, coursework_id) is untouched. The change is
    only reported as applied when the resource Google returns carries exactly
    the requested title (optionally re-read to confirm).

    Raises ``ClassroomApiError`` when Google rejects the change (typically a
    403 for a coursework another project created) and ``ValueError`` for an
    empty name.
    """
    from .auth import get_access_token

    name = _clean(display_name)
    if not name:
        raise ValueError("The assignment display name cannot be empty.")

    token = get_access_token()
    url = f"{gc.CLASSROOM_BASE}/courses/{course_id}/courseWork/{coursework_id}"
    updated = gc.classroom_request(
        "PATCH", url, token, params={"updateMask": "title"},
        json={"title": name},
    )
    returned = updated.get("title")
    verified = returned == name
    if verify and not verified:
        current = gc.classroom_request("GET", url, token)
        returned = current.get("title")
        verified = returned == name
    if not verified:
        raise gc.ClassroomApiError(
            502,
            "Google did not confirm the new assignment name (returned "
            f"{returned!r}). The Classroom assignment was not renamed.",
        )

    # Refresh local caches so every surface shows the new display name. The
    # internal id columns are deliberately left alone. ``custom_title`` is the
    # teacher's own display name and wins over Google's on every future sync,
    # so this rename can never be silently undone by a Classroom resync.
    db.execute(
        "UPDATE coursework SET title = ?, custom_title = ?, classroom_title = ?, "
        "fetched_at = ? WHERE id = ?",
        (name, name, name, time.time(), coursework_id))
    db.execute(
        "UPDATE classroom_managed_assignments SET title = ?, last_verified_at = ? "
        "WHERE coursework_id = ?", (name, time.time(), coursework_id))
    log_event("classroom.naming", "assignment renamed",
              coursework_id=coursework_id, course_id=course_id)
    return {
        "coursework_id": coursework_id,
        "course_id": course_id,
        "display_name": name,
        "verified": True,
        "classroom": updated,
    }
