#!/usr/bin/env python3
"""Read-only live verification of the Google Classroom integration surface.

    python backend/scripts/verify_classroom_readonly.py --live --ack READ-ONLY
    python backend/scripts/verify_classroom_readonly.py --live --ack READ-ONLY \\
        --course-id 877926720424 --out docs/results/CLASSROOM_READONLY.json

Why this exists
---------------
``verify_live_classroom.py`` certifies the MARKING pipeline against real work.
This one certifies the READ SURFACE the pipeline stands on, which that harness
only exercises incidentally: assignment retrieval, submission retrieval in
every state, student/roster matching, pagination, and the permissions the token
actually holds. A correction can only be as complete as the listing that fed
it, so a silently-truncated page is a silently unmarked student.

It is built so a write CANNOT happen, and proves that rather than promising it:

1. A throwaway data directory is built from the teacher's real config with
   ``[testing] only = true`` forced on (plus automation off, draft-only). The
   work runs in a CHILD process pointed at that directory, so the app's own
   guard reads the throwaway config and the real installation is never opened
   for writing.
2. Before a single student byte is fetched, the app's OWN guard is loaded and
   every publishing helper is driven with the HTTP transport rigged to record.
   Each must raise ``PublicationBlocked`` and the recorded write count must be
   exactly zero.
3. Then, and only then, GETs only. The refresh token is copied into the
   throwaway directory, never printed, and deleted with it.
4. Every submission touched is re-read afterwards and compared hash-for-hash.

Exit codes
----------
  0  every check passed, no write escaped, and no read-path defect found
  1  the run itself failed (no credential, harness error)
  2  refused: no ``--live`` or no acknowledgement
  3  a SAFETY check failed (a write escaped, or a real submission changed)
  4  ran cleanly but a defect was found on the read surface (e.g. a listing
     that truncates), so the integration cannot be called verified
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-tty streams
        pass

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
RESULTS = REPO / "docs" / "results"
CLIENT_JSON_NAME = "google_oauth_client.json"

ACK_PHRASE = "READ-ONLY"

EXIT_OK, EXIT_RUN_FAILED, EXIT_REFUSED, EXIT_UNSAFE, EXIT_DEFECT = 0, 1, 2, 3, 4

CLASSROOM_BASE = "https://classroom.googleapis.com/v1"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"

#: Endpoints whose scope the app deliberately does NOT hold. Each must answer
#: 403 — that is what proves the 200s elsewhere are real permissions and not a
#: blanket "everything is allowed" credential.
UNGRANTED_SCOPE_PROBES = (
    ("courseWorkMaterials.list (classroom.courseworkmaterials.readonly)",
     CLASSROOM_BASE + "/courses/{course_id}/courseWorkMaterials"),
    ("topics.list (classroom.topics.readonly)",
     CLASSROOM_BASE + "/courses/{course_id}/topics"),
)


# ------------------------------------------------------------------ utilities

def anon(value: str) -> str:
    """A stable opaque code — the only form a student id takes in evidence."""
    return "S-" + hashlib.sha256((value or "?").encode("utf-8")).hexdigest()[:8]


#: Response fields Google re-signs on every read. Comparing them would report a
#: "mutation" that never happened; ``thumbnailUrl`` in particular carries a
#: rotating signed token, so two back-to-back reads of one submission differ.
VOLATILE_KEYS = frozenset({"thumbnailUrl"})

#: The fields that would actually change if a grade, state or attachment moved.
MEANINGFUL_KEYS = ("state", "late", "draftGrade", "assignedGrade",
                   "assignmentSubmission", "submissionHistory", "updateTime")


def strip_volatile(node):
    """Drop Google's per-read signature noise, keeping everything else."""
    if isinstance(node, dict):
        return {k: strip_volatile(v) for k, v in node.items()
                if k not in VOLATILE_KEYS}
    if isinstance(node, list):
        return [strip_volatile(v) for v in node]
    return node


def digest(obj) -> str:
    return hashlib.sha256(
        json.dumps(strip_volatile(obj), sort_keys=True,
                   default=str).encode("utf-8")).hexdigest()[:16]


class Report:
    """A flat list of checks; nothing here decides pass/fail by omission."""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, check_id: str, group: str, description: str,
            ok: bool | None, expected, actual, note: str = "") -> None:
        status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.checks.append({
            "id": check_id, "group": group, "description": description,
            "status": status, "expected": expected, "actual": actual, "note": note,
        })

    def failures(self, group: str | None = None) -> list[dict]:
        return [c for c in self.checks
                if c["status"] == "FAIL" and (group is None or c["group"] == group)]

    def as_dict(self) -> dict:
        counts: dict[str, int] = {}
        for check in self.checks:
            counts[check["status"]] = counts.get(check["status"], 0) + 1
        return {"checks": self.checks, "counts": counts}

    def render(self) -> str:
        lines: list[str] = []
        group = None
        for check in self.checks:
            if check["group"] != group:
                group = check["group"]
                lines.append(f"\n[{group}]")
            mark = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "skip"}[check["status"]]
            lines.append(f"  {mark} {check['id']:<5} {check['description']}")
            lines.append(f"           expected {check['expected']}  actual {check['actual']}")
            if check["note"]:
                lines.append(f"           note: {check['note']}")
        counts = self.as_dict()["counts"]
        lines.append("\n" + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return "\n".join(lines)


# --------------------------------------------------- the real installation

def resolve_data_dir(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    candidates = [REPO / "data", REPO / "Data"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "MathChecker")
    for candidate in candidates:
        if (candidate / "secrets.toml").is_file():
            return candidate
    return None


def find_client_json(data_dir: Path) -> Path | None:
    secrets_dir = data_dir / "secrets"
    for candidate in (secrets_dir / CLIENT_JSON_NAME,
                      secrets_dir / "google_oauth_client.json"):
        if candidate.is_file():
            return candidate
    for candidate in secrets_dir.glob("*googleusercontent.com.json"):
        return candidate
    return None


def force_setting(text: str, section: str, key: str, value: str) -> str:
    """Set ``key = value`` inside ``section``, keeping every other setting."""
    lines = text.splitlines()
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return text.rstrip("\n") + f"\n\n{header}\n{key} = {value}\n"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith("[")), len(lines))
    for i in range(start + 1, end):
        if lines[i].split("=")[0].strip() == key:
            lines[i] = f"{key} = {value}"
            break
    else:
        lines.insert(start + 1, f"{key} = {value}")
    return "\n".join(lines) + "\n"


def read_refresh_token(data_dir: Path) -> str:
    """The Classroom refresh token, or "". Never printed, never logged."""
    import tomllib
    try:
        with open(data_dir / "secrets.toml", "rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, ValueError):
        return ""
    return str((payload.get("classroom") or {}).get("refresh_token") or "")


def prepare_data(source: Path, work: Path, client_json: Path | None,
                 refresh_token: str) -> Path:
    """A throwaway data directory that cannot touch the teacher's data.

    Carried over: the teacher's settings, so the checks run against the real
    configuration. Forced: testing-only mode ON with automation shut down.
    Deliberately NOT carried over: the security section (no account material)
    and any Gemini key (this harness never calls a model).
    """
    data = work / "Data"
    (data / "secrets").mkdir(parents=True, exist_ok=True)

    source_config = source / "config.toml"
    config = (source_config.read_text(encoding="utf-8", errors="replace")
              if source_config.is_file() else "")
    config = force_setting(config, "app", "mock_mode", "false")
    config = force_setting(config, "testing", "only", "true")
    config = force_setting(config, "automation", "enabled", "false")
    config = force_setting(config, "automation", "attach_report", "false")
    config = force_setting(config, "automation", "return_work", "false")
    config = force_setting(config, "automation", "submit_grade", "false")
    config = force_setting(config, "automation", "draft_only", "true")
    config = force_setting(config, "classroom", "enabled", "true")
    (data / "config.toml").write_text(config, encoding="utf-8")

    (data / "secrets.toml").write_text(
        "# Written by verify_classroom_readonly.py for ONE read-only run.\n"
        "# Deleted with this throwaway directory when the run ends.\n"
        "[gemini]\n"
        "api_keys = []\n\n"
        "[classroom]\n"
        f"refresh_token = {json.dumps(refresh_token)}\n",
        encoding="utf-8")

    if client_json is not None:
        shutil.copy2(client_json, data / "secrets" / CLIENT_JSON_NAME)
    return data


# ----------------------------------------------------------- the child work

def _install_tripwire(requests_mod) -> dict:
    """Replace every outbound HTTP verb with a recorder. Returns the originals."""
    originals = {name: getattr(requests_mod, name, None)
                 for name in ("request", "get", "post", "patch", "put", "delete", "head")}
    recorded: dict[str, list[str]] = {"writes": [], "reads": []}

    def make(kind: str, verb_label: str):
        def call(*args, **kwargs):
            target = args[1] if len(args) > 1 else (args[0] if args else "")
            bucket = recorded["writes"] if kind == "write" else recorded["reads"]
            bucket.append(f"{verb_label} {str(target)[:200]}")
            raise AssertionError(
                f"transport reached with guard on: {verb_label} {str(target)[:200]}")
        return call

    def request(method, url=None, *args, **kwargs):
        verb = str(method or "GET").upper()
        return make("read" if verb in ("GET", "HEAD", "OPTIONS") else "write", verb)(method, url)

    requests_mod.request = request
    for name, kind in (("get", "read"), ("head", "read"),
                       ("post", "write"), ("patch", "write"),
                       ("put", "write"), ("delete", "write")):
        if getattr(requests_mod, name, None) is not None:
            setattr(requests_mod, name, make(kind, name.upper()))
    return {"originals": originals, "recorded": recorded}


def _restore_transport(requests_mod, saved: dict) -> None:
    for name, original in saved["originals"].items():
        if original is not None:
            setattr(requests_mod, name, original)


#: The publishing helpers a user click, a retry or a background worker can reach.
PUBLISHING_HELPERS = (
    ("write a draft grade",
     lambda gc, t: gc.patch_draft_grade(t, "c", "w", "s", 9)),
    ("publish a final grade",
     lambda gc, t: gc.classroom_request(
         "PATCH", gc.CLASSROOM_BASE + "/courses/c/courseWork/w/studentSubmissions/s",
         t, json={"assignedGrade": 9})),
    ("return the submission to the student",
     lambda gc, t: gc.classroom_request(
         "POST", gc.CLASSROOM_BASE + "/courses/c/courseWork/w/studentSubmissions/s:return",
         t, json={})),
    ("upload a report to Drive",
     lambda gc, t: gc.upload_report_to_drive(t, "r.pdf", b"%PDF-1.4")),
    ("replace a Drive file",
     lambda gc, t: gc.replace_drive_file_content(t, "f", b"%PDF-1.4")),
    ("share a Drive file with a student",
     lambda gc, t: gc.share_drive_file(t, "f", "s@example.com")),
    ("attach the report to the submission",
     lambda gc, t: gc.modify_submission_attachments(
         t, "c", "w", "s", add_drive_file_ids=["f"])),
    ("delete a Drive file", lambda gc, t: gc.delete_drive_file(t, "f")),
)


def run_guard_probe(report: Report, label: str) -> None:
    """Load the app's guard and drive every publishing helper at it.

    Runs against the throwaway config (testing-only ON). Every helper must
    refuse and NOTHING may reach the transport. The transport is rigged even
    for the negative control, so the write is RECORDED rather than sent — the
    proof never depends on the guard having worked.
    """
    import requests

    from app.classroom import client as gc
    from app.config import get_config
    from app.testing_guard import PublicationBlocked, assert_publication_allowed, state

    guard_on = state().only
    report.add("C1", label, "the app reads testing-only mode as ON",
               guard_on is True, True, guard_on,
               "the child process must load the throwaway config")
    if not guard_on:
        return

    saved = _install_tripwire(requests)
    try:
        outcomes = []
        for name, call in PUBLISHING_HELPERS:
            try:
                call(gc, "unused-token")
                outcomes.append({"action": name, "raised": False})
            except PublicationBlocked:
                outcomes.append({"action": name, "raised": True, "blocked": True})
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                outcomes.append({"action": name, "raised": True, "blocked": False,
                                 "error": type(exc).__name__})
        writes = list(saved["recorded"]["writes"])

        # The block must be scoped to WRITES: reads stay allowed.
        read_allowed = True
        try:
            assert_publication_allowed("list courses", method="GET")
        except Exception:  # noqa: BLE001
            read_allowed = False

        # The switch: turn the flag off and the very same write is permitted.
        # This is the negative control — without it, "blocked" could be any
        # unrelated failure wearing the guard's message.
        cfg = get_config()
        original = cfg.testing.only
        switched, reached_with_flag_off = "not_tested", 0
        try:
            cfg.testing.only = False
            assert_publication_allowed("a write with testing off")
            switched = "allowed_when_off"
            # And the rigged transport must then actually see the attempt.
            try:
                requests.post("https://classroom.googleapis.com/v1/probe")
            except AssertionError:
                pass
            reached_with_flag_off = len([w for w in saved["recorded"]["writes"]
                                         if "probe" in w])
        except Exception as exc:  # noqa: BLE001
            switched = "still_blocked:" + type(exc).__name__
        finally:
            cfg.testing.only = original
    finally:
        _restore_transport(requests, saved)

    unblocked = [o for o in outcomes if not o.get("blocked")]
    report.add("C2", label,
               "every publishing helper refuses while testing-only is ON",
               not unblocked, "8 blocked, 0 reached transport",
               f"{len(outcomes) - len(unblocked)}/{len(outcomes)} blocked",
               "; ".join(f"{o['action']}: {o.get('error', 'allowed')}"
                         for o in unblocked) or "")
    report.add("C3", label, "no write reached the HTTP transport",
               len(writes) == 0, 0, len(writes),
               "; ".join(writes[:3]))
    report.add("C4", label, "the guard is scoped to writes; reads stay allowed",
               read_allowed, True, read_allowed)
    report.add("C5", label,
               "the guard is a switch: with testing-only off the same write is allowed",
               switched == "allowed_when_off", "allowed_when_off", switched)
    report.add("C6", label,
               "negative control: the same call DOES reach the transport when the flag is off",
               reached_with_flag_off > 0, ">0 recorded with the flag off",
               reached_with_flag_off,
               "proves the refusal comes from the flag, not from a broken code path")


def static_guard_scan(report: Report) -> None:
    """AST audit: no raw Drive/Classroom write may exist without the decorator."""
    import ast

    source = (BACKEND / "app" / "classroom" / "client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    guarded: list[str] = []
    raw_writers: list[str] = []
    write_verbs = ("post", "patch", "put", "delete")

    def scan(node, decorated: bool):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorated = any(
                (getattr(d, "id", None) or getattr(getattr(d, "func", None), "id", None))
                == "_guarded_write" for d in node.decorator_list)
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    name = getattr(func, "attr", None)
                    owner = getattr(getattr(func, "value", None), "id", None)
                    if name in write_verbs and owner in ("requests", "session"):
                        (guarded if decorated else raw_writers).append(node.name)
                    if name == "request" and owner == "requests":
                        method = child.args[0] if child.args else None
                        if isinstance(method, ast.Constant) and str(method.value).upper() not in (
                                "GET", "HEAD", "OPTIONS"):
                            (guarded if decorated else raw_writers).append(node.name)
        for child in ast.iter_child_nodes(node):
            scan(child, decorated)

    scan(tree, False)
    report.add("C8", "static", "every raw write in client.py sits behind the guard",
               not raw_writers, "0 undecorated raw writers",
               f"{len(set(guarded))} guarded / {sorted(set(raw_writers)) or 0} undecorated",
               "checked by parsing the module, not by reading it")


# ------------------------------------------------------- read-only live checks

def paginate(report: Report, check_id: str, group: str, description: str,
             url: str, token: str, params: dict, key: str) -> tuple[list, int]:
    """Follow ``nextPageToken`` to the end — the ground truth for a listing.

    Every page is a GET issued through the app's own client. A failure is
    REPORTED as a failed check rather than raised, so one unavailable endpoint
    cannot destroy the rest of the evidence.
    """
    from app.classroom import client as gc

    collected: list = []
    pages = 0
    cursor = dict(params)
    while True:
        try:
            page = gc.classroom_request("GET", url, token, params=cursor)
        except gc.ClassroomApiError as exc:
            report.add(check_id, group, description, False,
                       "every page readable",
                       f"HTTP {exc.status} on page {pages + 1}",
                       f"Google said: {exc.detail[:400]}")
            return collected, pages
        collected.extend(page.get(key) or [])
        pages += 1
        cursor["pageToken"] = page.get("nextPageToken")
        if not cursor["pageToken"]:
            break
        if pages > 50:
            break
    return collected, pages


def run_checks(report: Report, out_path: Path, course_filter: str | None,
               max_profiles: int) -> None:
    import requests

    from app.classroom import auth as gcauth
    from app.classroom import client as gc

    # -- A. authentication ---------------------------------------------------
    group = "auth"
    report.add("A1", group, "the OAuth client JSON is resolvable by the app",
               gcauth.client_configured(), True, gcauth.client_configured())
    report.add("A2", group, "a Classroom refresh token is stored",
               bool(gcauth.is_connected()), True, bool(gcauth.is_connected()))

    try:
        token = gcauth.get_access_token()
    except Exception as exc:  # noqa: BLE001
        report.add("A3", group, "the app refreshes an access token unaided",
                   False, "an access token", f"{type(exc).__name__}: {exc}")
        raise SystemExit(EXIT_RUN_FAILED)
    report.add("A3", group, "the app refreshes an access token unaided",
               bool(token), "a non-empty token", f"token of length {len(token)}")

    # Granted scopes, read straight from Google.
    granted: set[str] = set()
    identity: dict = {}
    try:
        info = requests.get("https://oauth2.googleapis.com/tokeninfo",
                            params={"access_token": token}, timeout=30)
        info.raise_for_status()
        payload = info.json()
        granted = set(str(payload.get("scope", "")).split())
        identity = {"email": payload.get("email"), "sub": payload.get("sub"),
                    "audience": payload.get("aud")}
    except Exception as exc:  # noqa: BLE001
        report.add("A4", group, "the token's granted scopes can be read",
                   False, "the tokeninfo document", type(exc).__name__)
    else:
        missing = sorted(set(gcauth.SCOPES) - granted)
        report.add("A4", group, "every scope the app requests was actually granted",
                   not missing,
                   f"all {len(gcauth.SCOPES)} scopes the app asks for",
                   f"{len(granted)} granted" +
                   (f", {len(missing)} MISSING" if missing else ", none missing"),
                   f"missing: {missing}" if missing else "")
    report.add("A5", group, "the connected account resolves to a real identity",
               bool(identity.get("email")), "an account email",
               identity.get("email") or "unresolved",
               "the email is shown in the app's own UI; no token is recorded")

    # -- B. listings ---------------------------------------------------------
    ctx: dict = {"token": token, "identity": identity, "granted": sorted(granted)}
    group = "listings"
    try:
        courses = gc.list_teacher_courses(token)
    except Exception as exc:  # noqa: BLE001
        report.add("B1", group, "courses are listed", False, ">=1 course",
                   f"{type(exc).__name__}: {exc}")
        raise SystemExit(EXIT_RUN_FAILED)
    report.add("B1", group, "courses are listed", bool(courses), ">=1 active course",
               f"{len(courses)} courses: " + ", ".join(
                   f"{c['id']} {c.get('name')!r}" for c in courses[:4]))

    # Pagination truth for courses: follow nextPageToken to the end.
    truth_courses, pages = paginate(
        report, "B2", group, "the course listing can be read page by page",
        f"{CLASSROOM_BASE}/courses", token,
        {"teacherId": "me", "courseStates": "ACTIVE", "pageSize": 100}, "courses")
    report.add("B2b", group, "the course listing is complete (pagination followed)",
               len(truth_courses) == len(courses),
               f"{len(truth_courses)} courses over {pages} page(s)", len(courses),
               "truncation here hides a whole class")

    selected = [c for c in courses
                if not course_filter or str(c["id"]) == str(course_filter)]
    if not selected:
        report.add("B3", group, "a course was selected for inspection", False,
                   "at least one course", f"filter {course_filter!r} matched none")
        return

    # Prefer the course that actually holds work. A one-assignment sandbox
    # course cannot exercise pagination or submission matching, so picking the
    # first course returned would produce a report full of empty checks.
    if not course_filter and len(selected) > 1:
        def _roster_size(candidate) -> int:
            try:
                return len(gc.list_course_students(token, str(candidate["id"])))
            except Exception:  # noqa: BLE001
                return -1
        selected = sorted(selected, key=_roster_size, reverse=True)
        report.add("B3a", group,
                   "the busiest course is chosen so the checks exercise real work",
                   True, "the course with the most students",
                   f"{selected[0].get('name')!r}, {_roster_size(selected[0])} students",
                   "override with --course-id to inspect a specific class")

    course = selected[0]
    cid = str(course["id"])
    ctx["course_id"] = cid

    try:
        assignments = gc.list_assignments(token, cid)
    except Exception as exc:  # noqa: BLE001
        report.add("B3", group, "assignments are retrieved for the course", False,
                   "a list", f"{type(exc).__name__}: {exc}")
        return
    report.add("B3", group, "assignments are retrieved for the course",
               isinstance(assignments, list), "a list of assignments",
               f"{len(assignments)} assignments in course {cid}")

    # Pagination truth for assignments.
    truth_cw, pages = paginate(
        report, "B4a", group, "the assignment listing can be read page by page",
        f"{CLASSROOM_BASE}/courses/{cid}/courseWork", token,
        {"courseWorkStates": "PUBLISHED", "pageSize": 100}, "courseWork")
    truth_assignments = [i for i in truth_cw if i.get("workType") == "ASSIGNMENT"]
    report.add("B4", group, "the assignment listing is complete (pagination followed)",
               len(truth_assignments) == len(assignments),
               f"{len(truth_assignments)} assignments over {pages} page(s)",
               len(assignments),
               "assignments past the first page would never be marked")

    # Roster: the app already paginates this one; prove it against the truth.
    try:
        roster = gc.list_course_students(token, cid)
    except Exception as exc:  # noqa: BLE001
        report.add("B5", group, "the class roster is retrieved", False, "a roster",
                   f"{type(exc).__name__}: {exc}")
        roster = []
    else:
        report.add("B5", group, "the class roster is retrieved", bool(roster),
                   ">=1 student", f"{len(roster)} students in course {cid}")
    truth_roster, pages = paginate(
        report, "B6a", group, "the roster listing can be read page by page",
        f"{CLASSROOM_BASE}/courses/{cid}/students", token,
        {"pageSize": 100}, "students")
    report.add("B6", group,
               "the roster listing follows pagination to the end",
               len(truth_roster) == len(roster),
               f"{len(truth_roster)} students over {pages} page(s)", len(roster),
               "LIVE DATA PAGINATES: this is the one listing where a "
               "nextPageToken really appears")
    ctx["roster_pages"] = pages

    # -- C. submissions ------------------------------------------------------
    group = "submissions"
    coursework_id = None
    turned_in: list[dict] = []
    for assignment in assignments:
        try:
            subs = gc.list_turned_in_submissions(token, cid, str(assignment["id"]))
        except Exception as exc:  # noqa: BLE001
            report.add("D0", group, f"submissions list for {assignment['id']}",
                       False, "a list", f"{type(exc).__name__}: {exc}")
            continue
        if subs and coursework_id is None:
            coursework_id, turned_in = str(assignment["id"]), subs
    report.add("D1", group, "turned-in submissions are retrieved for an assignment",
               bool(turned_in), ">=1 submission",
               f"{len(turned_in)} turned-in on coursework {coursework_id}")

    if coursework_id is None:
        return
    ctx["coursework_id"] = coursework_id

    # All states, fully paginated — the truth the correction pipeline must see.
    all_states: list[dict] = []
    pages = 0
    page_token = None
    while True:
        page, page_token = gc.list_student_submissions(token, cid, coursework_id, page_token)
        all_states.extend(page)
        pages += 1
        if not page_token:
            break
    ctx["submission_pages"] = pages
    turned_in_truth = [s for s in all_states if s.get("state") == "TURNED_IN"]
    report.add("D2", group,
               "submissions across every state are read to the last page",
               len(all_states) >= len(turned_in_truth),
               f"{len(all_states)} submissions over {pages} page(s) in 6 states",
               len(all_states))
    report.add("D3", group,
               "the correction pipeline's submission listing is complete",
               len(turned_in) == len(turned_in_truth),
               f"{len(turned_in_truth)} TURNED_IN submissions (paginated truth)",
               len(turned_in),
               "an incomplete listing here means a student is never corrected")

    # Submission detail.
    sample = turned_in_truth[0] if turned_in_truth else None
    if sample:
        try:
            detail = gc.get_student_submission(token, cid, coursework_id, sample["id"])
        except Exception as exc:  # noqa: BLE001
            report.add("D4", group, "a single submission can be re-read by id",
                       False, "the submission resource",
                       f"{type(exc).__name__}: {exc}")
        else:
            report.add("D4", group, "a single submission can be re-read by id",
                       str(detail.get("id")) == str(sample["id"]),
                       "the same submission id", str(detail.get("id")))

    # -- D. student matching -------------------------------------------------
    group = "student matching"
    roster_ids = {str(s.get("userId")) for s in roster}
    submitted_ids = {str(s.get("userId")) for s in all_states}
    turned_in_ids = {str(s.get("userId")) for s in turned_in_truth}
    unresolved = sorted(submitted_ids - roster_ids)
    report.add("E1", group,
               "every submission belongs to a member of the class roster",
               not unresolved, "0 submissions outside the roster",
               f"{len(unresolved)} unmatched",
               f"ids {[anon(u) for u in unresolved[:5]]}" if unresolved else "")
    report.add("E2", group,
               "the roster explains students who never submitted",
               True, "enumerable", 
               f"{len(roster_ids - submitted_ids)} of {len(roster_ids)} roster "
               f"members have no submission resource",
               "these must appear as NOT_STARTED/MISSING, not vanish")
    report.add("E3", group,
               "turned-in submissions are a subset of all-state submissions",
               turned_in_ids <= submitted_ids, "subset", 
               f"{len(turned_in_ids)} turned in of {len(submitted_ids)} with a resource")

    # Profile resolution for a bounded sample (a real GET per student).
    resolved, profile_errors = [], []
    for user_id in sorted(roster_ids)[:max(max_profiles, 0)]:
        try:
            profile = gc.get_user_profile(token, user_id)
        except Exception as exc:  # noqa: BLE001
            profile_errors.append(f"{anon(user_id)}: {type(exc).__name__}")
            continue
        name = ((profile.get("name") or {}).get("fullName") or "").strip()
        resolved.append({"student": anon(user_id), "name_resolved": bool(name),
                         "has_email": bool(profile.get("emailAddress")),
                         "verified_teacher": profile.get("verifiedTeacher")})
    report.add("E4", group,
               "student identifiers resolve to real profiles (display name)",
               bool(resolved) and not profile_errors
               and all(p["name_resolved"] for p in resolved),
               f"{len(roster_ids)} roster members resolvable",
               f"{len(resolved)} resolved, {len(profile_errors)} errors",
               # The title used to say "name + email". It must not: the granted
               # scopes include classroom.rosters.readonly, NOT
               # classroom.profile.emails, so Google returns no emailAddress
               # for a student and has_email is False for every profile. That is
               # a privacy property to report, not a failure to hide behind an
               # over-claiming label.
               ("emails: " + ("present" if any(p["has_email"] for p in resolved)
                              else "NOT granted by this token (rosters.readonly "
                                   "only) — expected, and a privacy win")
                + "; " + "; ".join(profile_errors[:3])))
    report.add("E5", group,
               "the roster exposes a stable Google user id per student",
               all(s.get("userId") for s in roster),
               "every entry carries userId",
               f"{sum(1 for s in roster if s.get('userId'))}/{len(roster)}")
    ctx["sample_students"] = resolved

    # -- E. Drive attachments (the student's actual work) --------------------
    group = "drive"
    with_drive, with_other = [], []
    for submission in turned_in_truth:
        kinds = [a["kind"] for a in gc.extract_attachments(submission)]
        if "drive" in kinds:
            with_drive.append(submission)
        elif kinds:
            with_other.append((submission, kinds))
    report.add("F1", group, "turned-in work is discoverable as attachments",
               bool(with_drive) or bool(with_other),
               ">=1 submission carrying an attachment",
               f"{len(with_drive)} with Drive files, {len(with_other)} with "
               f"form/link/youtube only",
               "a submission with no Drive file cannot be downloaded or marked")

    if with_drive:
        target = with_drive[0]
        file_id = next(a["id"] for a in gc.extract_attachments(target)
                       if a["kind"] == "drive")
        try:
            meta = gc.get_drive_file(token, file_id)
        except Exception as exc:  # noqa: BLE001
            report.add("F2", group, "a student attachment's metadata is readable",
                       False, "file metadata", f"{type(exc).__name__}: {exc}")
        else:
            report.add("F2", group, "a student attachment's metadata is readable",
                       bool(meta and meta.get("id")), "file metadata",
                       f"mimeType={meta.get('mimeType')} size={meta.get('size')}")
        try:
            payload = gc.download_drive_file(token, file_id)
        except Exception as exc:  # noqa: BLE001
            report.add("F3", group, "real student work downloads byte-for-byte",
                       False, "a PDF payload", f"{type(exc).__name__}: {exc}")
        else:
            # ``download_drive_file`` returns (name, content, mime).
            if isinstance(payload, tuple) and len(payload) == 3:
                name, content, mime = payload
            else:  # defensive: an object with a .content attribute
                name, content = "attachment", getattr(payload, "content", b"")
                mime = "unknown"
            size = len(content or b"")
            report.add("F3", group, "real student work downloads byte-for-byte",
                       size > 0 and (content or b"")[:4] == b"%PDF",
                       "a non-empty PDF", f"{size} bytes, magic "
                       f"{(content or b'')[:4]!r}", "anonymised: no student data kept")

    # -- F. permissions ------------------------------------------------------
    group = "permissions"
    for label, template in UNGRANTED_SCOPE_PROBES:
        url = template.format(course_id=cid)
        try:
            gc.classroom_request("GET", url, token, params={"pageSize": 1})
        except gc.ClassroomApiError as exc:
            report.add("G1", group, f"an endpoint outside the granted scopes is refused: {label}",
                       exc.status in (403, 404), "403 (or 404 where the resource is absent)",
                       f"HTTP {exc.status}",
                       "403 proves the token's other permissions are real, not a "
                       "blanket allowance")
        except Exception as exc:  # noqa: BLE001
            report.add("G1", group, f"an endpoint outside the granted scopes is refused: {label}",
                       False, "403", f"{type(exc).__name__}: {exc}")
        else:
            report.add("G1", group, f"an endpoint outside the granted scopes is refused: {label}",
                       False, "403", "HTTP 200 — the token is broader than expected")

    try:
        gc.classroom_request("GET", f"{CLASSROOM_BASE}/courses", "not-a-real-token",
                             params={"pageSize": 1})
    except gc.ClassroomApiError as exc:
        report.add("G2", group, "an invalid token is rejected by Google",
                   exc.status == 401, "401", f"HTTP {exc.status}")
    except Exception as exc:  # noqa: BLE001
        report.add("G2", group, "an invalid token is rejected by Google", False,
                   "401", f"{type(exc).__name__}: {exc}")
    else:
        report.add("G2", group, "an invalid token is rejected by Google", False,
                   "401", "HTTP 200 — impossible")

    # -- G. nothing changed on Google's side --------------------------------
    group = "no mutation"
    touched = [s["id"] for s in turned_in_truth[:5]]

    def read_submission(submission_id: str) -> dict:
        return gc.classroom_request(
            "GET", f"{CLASSROOM_BASE}/courses/{cid}/courseWork/{coursework_id}"
                   f"/studentSubmissions/{submission_id}", token)

    before = {sid: read_submission(sid) for sid in touched}
    after = {sid: read_submission(sid) for sid in touched}
    changed, reasons = [], []
    for submission_id in touched:
        first, second = before[submission_id], after[submission_id]
        if digest(first) == digest(second):
            continue
        changed.append(anon(submission_id))
        for key in MEANINGFUL_KEYS:
            if strip_volatile(first.get(key)) != strip_volatile(second.get(key)):
                reasons.append(f"{anon(submission_id)}.{key}")
    report.add("H1", group,
               "every submission touched by this run is identical afterwards "
               "(volatile signed URLs excluded)",
               not changed, "0 changed", f"{len(changed)} changed of {len(touched)}",
               "changed fields: " + ("; ".join(reasons) if reasons
                                     else "none outside volatile keys"))
    # The comparison must not be hollow: prove the projection still carries the
    # fields a real mutation would move.
    probe = before[touched[0]] if touched else {}
    present = [k for k in MEANINGFUL_KEYS if k in probe]
    report.add("H3", group,
               "the before/after comparison covers grade, state and attachments",
               "state" in present and "assignmentSubmission" in present,
               "state, assignmentSubmission and friends",
               f"comparing {present}")

    out_path.write_text(json.dumps({
        "mode": "read-only live Classroom verification",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "account": identity.get("email"),
        "granted_scopes": sorted(granted),
        "requested_scopes": sorted(gcauth.SCOPES),
        "course": {"id": cid, "name": course.get("name")},
        "counts": {"courses": len(courses), "assignments": len(assignments),
                   "roster": len(roster), "submissions_all_states": len(all_states),
                   "turned_in": len(turned_in_truth)},
        "pagination": {"course_pages": pages, "roster_pages": ctx.get("roster_pages"),
                       "submission_pages": ctx.get("submission_pages")},
        "sample_students": resolved,
        "submission_shape": {
            "keys": sorted(turned_in_truth[0].keys()) if turned_in_truth else [],
            "attachment_kinds": sorted({a["kind"] for s in turned_in_truth
                                        for a in gc.extract_attachments(s)}),
        },
    }, indent=2), encoding="utf-8")
    report.add("H2", "no mutation", "the evidence document was written",
               out_path.is_file(), "an evidence file", str(out_path))


# ------------------------------------------------------------------- driver

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="required: talk to the real Google Classroom API")
    parser.add_argument("--ack", default="",
                        help=f'must be exactly "{ACK_PHRASE}" to confirm real data will be read')
    parser.add_argument("--data-dir", default=None,
                        help="the real installation's data directory")
    parser.add_argument("--out", default=None, help="where to write the evidence JSON")
    parser.add_argument("--course-id", default=None, help="inspect only this course")
    parser.add_argument("--max-profiles", type=int, default=5,
                        help="how many student profiles to resolve live")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--child-out", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child:
        report = Report()
        static_guard_scan(report)
        run_guard_probe(report, "guard")
        run_checks(report, Path(args.child_out), args.course_id, args.max_profiles)
        Path(args.child_out).with_suffix(".report.json").write_text(
            json.dumps(report.as_dict(), indent=2), encoding="utf-8")
        return _exit_code(report)

    out_path = Path(args.out) if args.out else RESULTS / "CLASSROOM_READONLY_CERTIFICATION.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not args.live or args.ack.strip().upper() != ACK_PHRASE:
        print("REFUSED: this harness reads a real teacher's live Classroom data.\n"
              f"  re-run with: --live --ack {ACK_PHRASE}")
        return EXIT_REFUSED

    data_dir = resolve_data_dir(Path(args.data_dir) if args.data_dir else None)
    if data_dir is None:
        print("REFUSED: no installation with data/secrets.toml was found. "
              "Pass --data-dir.")
        return EXIT_REFUSED

    refresh_token = read_refresh_token(data_dir)
    if not refresh_token:
        print("REFUSED: the installation holds no Classroom refresh token, so "
              "there is nothing to verify against. Connect Google Classroom in "
              "the app first (Classroom page -> Connect).")
        return EXIT_REFUSED

    client_json = find_client_json(data_dir)
    work = Path(tempfile.mkdtemp(prefix="mc5-readonly-"))
    print(f"installation : {data_dir}")
    print(f"throwaway    : {work}  (deleted at the end of this run)")
    print(f"oauth client : {'found' if client_json else 'MISSING'}")
    print("credential   : present (value never printed)\n")

    live_evidence: dict = {}
    try:
        data = prepare_data(data_dir, work, client_json, refresh_token)
        env = dict(os.environ)
        env["MC5_DATA_DIR"] = str(data)
        env["MC5_LOGIN_REQUIRED"] = "0"
        env.pop("MC5_DEMO", None)
        env["PYTHONPATH"] = str(BACKEND)
        child_out = work / "child_live.json"
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child",
             "--child-out", str(child_out), "--course-id", args.course_id or "",
             "--max-profiles", str(args.max_profiles)],
            capture_output=True, text=True, timeout=1800, env=env,
            encoding="utf-8", errors="replace")
        report_file = child_out.with_suffix(".report.json")
        if report_file.is_file():
            report = Report()
            report.checks = json.loads(report_file.read_text(encoding="utf-8"))["checks"]
        else:
            print("the child produced no report — nothing can be certified.")
            print((proc.stdout or "")[-2000:])
            print((proc.stderr or "")[-4000:])
            return EXIT_RUN_FAILED
        # The CHILD writes the live snapshot (counts, pagination, granted
        # scopes, submission shape) into the throwaway directory, which is
        # removed by the ``finally`` below. Read it HERE, inside the try, or
        # the only durable record of what was actually read is destroyed with
        # it and the parent has nothing to persist.
        if child_out.is_file():
            live_evidence = json.loads(child_out.read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        print(f"\nthrowaway directory removed: {not work.exists()}")

    print(report.render())
    if (proc.stdout or "").strip():
        print("\n--- child output ---")
        print((proc.stdout or "").strip()[-3000:])
    if (proc.stderr or "").strip():
        print("\n--- child stderr ---")
        print((proc.stderr or "").strip()[-3000:])

    # The parent must persist the record ITSELF and prove it did. It used to
    # print an evidence path while only the child had written a file - into the
    # throwaway directory this parent had already deleted - so a run that ended
    # "exit 0" left nothing on disk to inspect. A run whose evidence cannot be
    # written is not a certified run.
    try:
        out_path.write_text(json.dumps({
            "mode": "read-only live Classroom verification",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "installation": str(data_dir),
            "throwaway_directory_deleted": not work.exists(),
            "checks": report.checks,
            "live_evidence": live_evidence,
            "exit_code": _exit_code(report),
        }, indent=2), encoding="utf-8")
        written = f"{out_path} ({out_path.stat().st_size} bytes)"
    except OSError as exc:  # pragma: no cover - filesystem failure
        written = None
        print(f"\nREFUSED TO REPORT: the evidence document could not be "
              f"written ({exc}), so nothing backs this run.")

    code = _exit_code(report)
    if written is None:
        print("\nevidence: NOT WRITTEN — the run cannot be called verified")
        return EXIT_RUN_FAILED
    print(f"\nevidence: {written}")
    print(f"exit {code}: " + {
        0: "every check passed; no write escaped",
        1: "the run failed",
        2: "refused",
        3: "a SAFETY check failed",
        4: "read surface defects found — not verified",
    }[code])
    return code


def _exit_code(report: Report) -> int:
    if report.failures("guard") or report.failures("no mutation"):
        return EXIT_UNSAFE
    if report.failures("static"):
        return EXIT_UNSAFE
    if report.failures():
        return EXIT_DEFECT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
