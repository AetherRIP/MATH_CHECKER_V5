"""Runtime proof: with testing-only mode armed, NO publishing helper sends anything.

Run from `backend/` with MC5_DATA_DIR pointing at a config that has
`[testing] only = true`. Every publishing helper is driven with a rigged
transport that records any attempt to leave the machine. Expected outcome:
every write refused (PublicationBlocked), ZERO write-phase transport attempts,
and the read probe still allowed through.

Audit evidence only — the permanent regression coverage lives in
backend/tests/test_testing_only_guard.py.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.getcwd())

from app import testing_guard  # noqa: E402
from app.classroom import client as gc  # noqa: E402

#: Transport attempts made while the WRITE helpers were driven. Anything here
#: is egress the guard failed to stop.
WRITE_EGRESS: list[str] = []
#: Transport attempts made by the READ probe (expected: exactly one, because a
#: read is allowed and the rigged transport is what stops it).
READ_EGRESS: list[str] = []

_IN_WRITE_PHASE = True


def _rig_transport() -> None:
    """Replace every outbound transport with a recorder that never sends."""
    def boom(*args, **kwargs):
        (WRITE_EGRESS if _IN_WRITE_PHASE else READ_EGRESS).append("attempted")
        raise AssertionError("TRANSPORT REACHED - the guard did not block this")

    gc.requests.request = boom
    gc.requests.get = boom
    gc.requests.post = boom
    gc.requests.patch = boom
    gc.requests.put = boom
    gc.requests.delete = boom


def main() -> int:
    state = testing_guard.state()
    if not state.only:
        print(json.dumps({"error": "testing-only mode is NOT armed; refusing to run",
                          "armed": False}))
        return 2

    _rig_transport()

    cases = [
        ("upload_report_to_drive",
         lambda: gc.upload_report_to_drive("tok", "report.pdf", b"%PDF-1.4 test")),
        ("replace_drive_file_content",
         lambda: gc.replace_drive_file_content("tok", "file123", b"%PDF-1.4 test")),
        ("share_drive_file",
         lambda: gc.share_drive_file("tok", "file123", "student@example.com")),
        ("delete_drive_file",
         lambda: gc.delete_drive_file("tok", "file123")),
        ("classroom_request POST (return work)",
         lambda: gc.classroom_request(
             "POST",
             f"{gc.CLASSROOM_BASE}/courses/1/courseWork/2/studentSubmissions/3:return",
             "tok", json={})),
        ("classroom_request PATCH (grade)",
         lambda: gc.classroom_request(
             "PATCH",
             f"{gc.CLASSROOM_BASE}/courses/1/courseWork/2/studentSubmissions/3",
             "tok", params={"updateMask": "draftGrade"}, json={"draftGrade": 9})),
    ]

    results = []
    for name, call in cases:
        try:
            call()
        except testing_guard.PublicationBlocked as exc:
            results.append({"helper": name, "outcome": "BLOCKED",
                            "message": str(exc)[:160]})
        except AssertionError as exc:
            results.append({"helper": name, "outcome": "EGRESS", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - any other error is also a finding
            results.append({"helper": name, "outcome": f"OTHER:{type(exc).__name__}",
                            "message": str(exc)[:160]})

    # A read must STILL work: testing has to be able to see real submissions.
    global _IN_WRITE_PHASE
    _IN_WRITE_PHASE = False
    read_blocked = None
    try:
        gc.classroom_request("GET", f"{gc.CLASSROOM_BASE}/courses", "tok")
        read_error = "reached no transport at all"
    except testing_guard.PublicationBlocked as exc:
        read_blocked = str(exc)
        read_error = None
    except AssertionError:
        read_error = None  # reached the rigged transport: not blocked, correct
    except Exception as exc:  # noqa: BLE001
        read_error = f"unexpected {type(exc).__name__}: {exc}"
    _IN_WRITE_PHASE = True

    blocked = [r for r in results if r["outcome"] == "BLOCKED"]
    summary = {
        "testing_only_armed": True,
        "helpers_driven": len(results),
        "helpers_blocked": len(blocked),
        "write_egress_attempts": len(WRITE_EGRESS),
        "read_egress_attempts": len(READ_EGRESS),
        "read_still_allowed": read_blocked is None and read_error is None,
        "read_error": read_error,
        "results": results,
    }
    print(json.dumps(summary, indent=2))

    ok = (len(blocked) == len(results)
          and not WRITE_EGRESS
          and read_blocked is None
          and read_error is None
          and len(READ_EGRESS) == 1)
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
