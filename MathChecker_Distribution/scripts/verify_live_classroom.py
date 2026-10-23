#!/usr/bin/env python3
"""LIVE certification against REAL Google Classroom data — opt-in, read-only.

    python verify_live_classroom.py --live --process-real-student-work
    python verify_live_classroom.py --live --process-real-student-work \\
        --course-id 877926720424 --max-submissions 2

Why this exists
---------------
``verify_portable.py`` and ``verify_installer.py`` run the app in DEMO mode
(mock engine, synthetic PDFs). ``verify_live_marking.py`` certifies the
MARKING with a real key, but only against the bundled fixture quiz. Neither
touches a real student's work.

This does. It marks real submissions pulled from the teacher's own classes
against their real mark schemes — and it is built so that it CANNOT publish
anything, proveably rather than by promise:

1. THE BARRIER IS SET AND PROVEN FIRST. A throwaway data directory is built
   with ``[testing] only = true`` (and automation disabled, draft-only on, so
   the belt has braces). Nothing is fetched from Classroom until:
     * a subprocess loads the app's OWN guard against that throwaway config and
       drives every publishing helper — grade, report upload, report replace,
       report delete, share, modifyAttachments, return — with the HTTP
       transport rigged to record. Every one must raise PublicationBlocked and
       the recorded write count must be ZERO;
     * the running app's own API is asked to publish, and must record that
       nothing was sent;
     * a read must still succeed, proving the block is scoped to writes.
2. REAL WORK, READ ONLY. Courses, rosters, coursework and submissions are read
   with the teacher's existing refresh token using GET requests only. Student
   work is downloaded to the throwaway directory.
3. THE REAL MARKING PIPELINE. Each paper is marked through the app's own
   correction API with the live model, and the stored result is read back.
4. BEFORE/AFTER PROOF. Every submission it touched is re-read from Google
   afterwards. State, draft grade, return history and attachment ids must be
   IDENTICAL, hash for hash, or the run fails.
5. CLEANUP. The throwaway directory (including the copied credential and every
   downloaded PDF) is deleted; the real installation is never opened for
   writing. Files that cannot be proven to be this run's are left alone and
   reported.

Exit codes
----------
  0  every check passed and no write escaped
  1  the run itself failed (app would not start, no token, unusable install)
  2  refused: no ``--live``, no real-data acknowledgement, or no credential
  3  a SAFETY check failed (a write escaped, or a real submission changed)
  4  ran cleanly, but accuracy is NOT certified: there were no human-verified
     marks. Accuracy-only findings — a repeat disagreement, a paper that would
     not download — also land here, so they cannot be mistaken for a safety
     failure or for a clean pass.

A run without ground truth can NEVER return 0: cross-checking is not
certification, and this script will not pretend otherwise.
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

# Evidence strings carry arrows and dashes; a Windows console is cp1252 and an
# unencodable character must not turn a paid-for live run into a traceback.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-tty streams
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_portable import (  # noqa: E402
    Report, free_port, http, listening_pids, multipart, robust_rmtree, sign_in,
    wait_health,
)

DIST = Path(__file__).resolve().parent.parent        # MathChecker_Distribution
REPO = DIST.parent                                   # the workspace root
LOGS = REPO / "docs" / "logs"
RESULTS = REPO / "docs" / "results"
REPORTS = REPO / "docs" / "reports"

#: The app-created sandbox class. Everything in this run is read-only, so no
#: Classroom write flow runs at all; the id is recorded so that a future run
#: which DOES need one can only ever use this course.
SANDBOX_COURSE_ID = "886515702341"

EXIT_OK, EXIT_RUN_FAILED, EXIT_REFUSED = 0, 1, 2
EXIT_NOT_CERTIFIED, EXIT_NO_TRUTH = 3, 4

CLASSROOM_BASE = "https://classroom.googleapis.com/v1"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"

CLIENT_JSON_NAME = "google_oauth_client.json"
LAUNCH_TIMEOUT = 180.0
DEFAULT_JOB_TIMEOUT = 1200.0


# ------------------------------------------------------------------ utilities

def anon(value: str) -> str:
    """A stable opaque code — the ONLY form a student identifier takes in any
    artefact this script writes. Real names never leave the machine."""
    return "S-" + hashlib.sha256((value or "?").encode("utf-8")).hexdigest()[:8]


def digest(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "absent"


def stamp(path: Path) -> dict:
    try:
        info = path.stat()
        return {"size": info.st_size, "mtime": int(info.st_mtime)}
    except OSError:
        return {"size": None, "mtime": None}


# --------------------------------------------------- the real installation

def resolve_data_dir(explicit: Path | None) -> Path | None:
    """Where the teacher's real config/secrets live."""
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


def mine_secrets(data_dir: Path) -> dict:
    """The two credentials a read-only run needs, and nothing else.

    The Classroom refresh token is required to READ the teacher's own classes.
    It is read, copied into a throwaway directory and deleted with it — it is
    never printed, logged or written back to the real installation.
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return {}
    try:
        with open(data_dir / "secrets.toml", "rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    keys = (payload.get("gemini") or {}).get("api_keys") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    return {
        "gemini_keys": [str(k).strip() for k in keys if str(k).strip()],
        "refresh_token": str((payload.get("classroom") or {}).get("refresh_token") or ""),
    }


def find_client_json(data_dir: Path) -> Path | None:
    for candidate in (data_dir / "secrets" / CLIENT_JSON_NAME,
                      data_dir / "secrets" / "google_oauth_client.json"):
        if candidate.is_file():
            return candidate
    for candidate in (data_dir / "secrets").glob("*googleusercontent.com.json"):
        return candidate
    return None


def force_setting(text: str, section: str, key: str, value: str) -> str:
    """Set ``key = value`` inside ``section``, keeping every other setting.

    The teacher's own configuration is what is under test, so it is copied and
    only these switches are forced. The running app then CONFIRMS the result
    (``GET /api/settings``), so a botched edit fails the run instead of quietly
    marking in mock mode — or worse, publishing.
    """
    lines = text.splitlines()
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines)
                  if line.strip() == header), None)
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


def prepare_data(source: Path, work: Path, client_json: Path | None,
                 secrets: dict) -> Path:
    """A throwaway data directory that cannot touch the teacher's data.

    Carried over: the settings (so the run marks with the teacher's own model
    chain and the real Classroom switch) and the two credentials a read-only
    run needs. Deliberately NOT carried over: the security section, so the
    verifier account is created fresh, and every other secret the real
    installation holds.
    """
    data = work / "Data"
    (data / "secrets").mkdir(parents=True, exist_ok=True)

    config = ((source / "config.toml").read_text(encoding="utf-8", errors="replace")
              if (source / "config.toml").is_file() else "")
    config = force_setting(config, "app", "mock_mode", "false")
    config = force_setting(config, "testing", "only", "true")
    # Defensive second layer: even if something lifted the guard, an automation
    # worker with nothing to do cannot attach a report, return work or publish.
    config = force_setting(config, "automation", "enabled", "false")
    config = force_setting(config, "automation", "attach_report", "false")
    config = force_setting(config, "automation", "return_work", "false")
    config = force_setting(config, "automation", "draft_only", "true")
    config = force_setting(config, "classroom", "enabled", "true")
    (data / "config.toml").write_text(config, encoding="utf-8")

    keys = ", ".join(json.dumps(k) for k in secrets.get("gemini_keys") or [])
    (data / "secrets.toml").write_text(
        "# Written by verify_live_classroom.py for ONE read-only run.\n"
        "# Deleted with this throwaway directory when the run ends.\n"
        "[gemini]\n"
        f"api_keys = [{keys}]\n\n"
        "[classroom]\n"
        f"refresh_token = {json.dumps(secrets.get('refresh_token') or '')}\n",
        encoding="utf-8")

    if client_json is not None:
        shutil.copy2(client_json, data / "secrets" / CLIENT_JSON_NAME)
    return data


# ------------------------------------------------------------- app under test

def _python_processes() -> dict[int, tuple[int, str]]:
    """{pid: (parent_pid, command line)} for every running python.exe.

    The app's launcher spawns a PRIVATE server child (``main.py --serve <port>``)
    on a port it picks itself, so the port cannot be known in advance — the
    parent/child relationship is what identifies it.
    """
    if os.name != "nt":
        return {}
    script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t"
              "$($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=90).stdout
    except Exception:  # noqa: BLE001 - never fail a stop on a lookup
        return {}
    procs: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        pid, _, rest = line.partition("\t")
        parent, _, cmd = rest.partition("\t")
        if pid.strip().isdigit() and parent.strip().isdigit():
            procs[int(pid)] = (int(parent), cmd.strip())
    return procs


def app_python(app: Path) -> str:
    for rel in ("runtime/python.exe", "runtime/bin/python3"):
        candidate = app / rel
        if candidate.is_file():
            return str(candidate)
    return sys.executable


class LiveApp:
    """The application under test: started, and stopped again, by this script."""

    def __init__(self, app: Path, data: Path, port: int, log: Path) -> None:
        self.app, self.data, self.port, self.log = app, data, port, log
        self.proc: subprocess.Popen | None = None
        #: PIDs that already existed, so a stop only ever kills OUR children.
        self.preexisting: set[int] = set()

    def env(self) -> dict:
        env = dict(os.environ)
        env["MC5_DATA_DIR"] = str(self.data)
        env["MC5_NO_BROWSER"] = "1"
        env["MC5_LOGIN_REQUIRED"] = "1"     # certify through the gated app
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("MC5_DEMO", None)           # demo would force mock mode
        env.pop("MC5_AUTOSTART", None)
        return env

    def start(self) -> None:
        self.preexisting = set(_python_processes())
        handle = open(self.log, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [app_python(self.app), "main.py", str(self.port)],
            cwd=str(self.app), env=self.env(), stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True)

    def output(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self, timeout: float = 90.0) -> bool:
        try:
            subprocess.run([app_python(self.app), "main.py", "--stop", str(self.port)],
                           cwd=str(self.app), env=self.env(), capture_output=True,
                           text=True, timeout=int(timeout))
        except Exception:  # noqa: BLE001 - a failed stop must not hide a result
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not listening_pids(self.port):
                break
            time.sleep(1.0)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=20)
            except Exception:  # noqa: BLE001
                pass
        self._kill_our_children()
        return not listening_pids(self.port)

    def _kill_our_children(self) -> None:
        """Stop the private ``--serve`` child the launcher started.

        ``main.py --stop <port>`` stops the instance but can leave the backend
        server child alive; it keeps the throwaway database open, so the
        directory — which holds a COPY OF THE CREDENTIAL — cannot be deleted.
        This is why the first interrupted run leaked one. Only processes that
        are NEW since we started, and descended from our own launcher, are
        touched.
        """
        killer = ["taskkill", "/PID", "%s", "/F"]
        killed: set[int] = set()
        for _ in range(4):
            procs = _python_processes()
            victims = [pid for pid, (parent, _cmd) in procs.items()
                       if pid not in self.preexisting and pid not in killed
                       and (parent == (self.proc.pid if self.proc else -1)
                            or parent in killed)]
            if not victims:
                break
            for pid in victims:
                try:
                    subprocess.run([part % pid if "%s" in part else part
                                    for part in killer],
                                   capture_output=True, timeout=30)
                except Exception:  # noqa: BLE001
                    pass
                killed.add(pid)
            time.sleep(2.0)


# ------------------------------------------------------- the guard, proven

#: Run in a CHILD process against the throwaway config. It loads the app's own
#: guard, rigs the HTTP transport so every outbound request is recorded instead
#: of sent, and drives every publishing helper. This is the difference between
#: "we will not click upload" and "the write cannot be sent".
GUARD_PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])

import requests

writes, reads = [], []

def _tripwire(kind):
    def call(*a, **k):
        if kind == "read":
            reads.append(str(a[:1]))
            raise AssertionError("probe needs no live read")
        writes.append(str(a[:1]))
        raise AssertionError("a WRITE reached the transport with the guard on")
    return call

def _request(method, url=None, *a, **k):
    verb = str(method or "GET").upper()
    return _tripwire("read" if verb in ("GET", "HEAD", "OPTIONS") else "write")(method, url)

requests.request = _request
for _name in ("post", "patch", "put", "delete"):
    setattr(requests, _name, _tripwire("write"))
for _name in ("get", "head"):
    setattr(requests, _name, _tripwire("read"))

from app.testing_guard import PublicationBlocked, assert_publication_allowed, state
from app.classroom import client as gc

result = {"testing_only": state().only, "helpers": [], "transport_writes": None,
          "negative_control": None, "config_error": None}

if not result["testing_only"]:
    result["config_error"] = "the app read testing-only mode as OFF"
    print("GUARDPROBE " + json.dumps(result))
    raise SystemExit(0)

UA = "unused-token"
calls = [
    ("write a draft grade", lambda: gc.patch_draft_grade(UA, "c", "w", "s", 9)),
    ("publish a final grade", lambda: gc.classroom_request(
        "PATCH", gc.CLASSROOM_BASE + "/courses/c/courseWork/w/studentSubmissions/s", UA,
        json={"assignedGrade": 9})),
    ("return the submission to the student", lambda: gc.classroom_request(
        "POST", gc.CLASSROOM_BASE + "/courses/c/courseWork/w/studentSubmissions/s:return",
        UA, json={})),
    ("upload a report to Drive", lambda: gc.upload_report_to_drive(UA, "r.pdf", b"%PDF-1.4")),
    ("replace a Drive file", lambda: gc.replace_drive_file_content(UA, "f", b"%PDF-1.4")),
    ("share a Drive file with a student", lambda: gc.share_drive_file(UA, "f", "s@example.com")),
    ("attach the report to the submission", lambda: gc.modify_submission_attachments(
        UA, "c", "w", "s", add_drive_file_ids=["f"])),
    ("delete a Drive file", lambda: gc.delete_drive_file(UA, "f")),
]
for label, call in calls:
    try:
        call()
        result["helpers"].append({"action": label, "raised": False})
    except PublicationBlocked:
        result["helpers"].append({"action": label, "raised": True, "blocked": True})
    except Exception as exc:
        result["helpers"].append({"action": label, "raised": True,
                                  "blocked": False, "error": type(exc).__name__})

# The scope control: the block is a WRITE gate, not a blanket refusal of the
# app's Classroom access, and it is a switch rather than a permanent wall.
assert_publication_allowed("list courses", method="GET")
from app.config import get_config
get_config().testing.only = False
try:
    assert_publication_allowed("a write with testing off")
    result["negative_control"] = "allowed_when_off"
except Exception as exc:
    result["negative_control"] = "still_blocked:" + type(exc).__name__

result["transport_writes"] = len(writes)
result["transport_reads"] = len(reads)
print("GUARDPROBE " + json.dumps(result))
'''


def run_guard_probe(work: Path, data: Path, repo: Path) -> dict:
    script = work / "guard_probe.py"
    script.write_text(GUARD_PROBE, encoding="utf-8")
    env = dict(os.environ)
    env["MC5_DATA_DIR"] = str(data)
    env["MC5_LOGIN_REQUIRED"] = "0"
    env.pop("MC5_DEMO", None)
    proc = subprocess.run([sys.executable, str(script), str(repo / "backend")],
                          capture_output=True, text=True, timeout=180, env=env,
                          encoding="utf-8", errors="replace")
    for line in (proc.stdout or "").splitlines():
        if line.startswith("GUARDPROBE "):
            return json.loads(line[len("GUARDPROBE "):])
    return {"error": (proc.stdout or "")[-400:] + (proc.stderr or "")[-600:]}


# ------------------------------------------------------ read-only Google side

class GoogleRead:
    """GET-only Google access, using the teacher's own stored refresh token.

    Every method issues a GET. The app already holds ``classroom.*.readonly``
    and ``drive.readonly``, so nothing here is capable of writing even if it
    tried — the guard is the second line, not the only one.
    """

    def __init__(self, client_json: Path, refresh_token: str):
        import json as _json

        payload = _json.loads(Path(client_json).read_text(encoding="utf-8"))
        block = payload.get("installed") or payload.get("web") or {}
        self.client_id = block.get("client_id") or ""
        self.client_secret = block.get("client_secret") or ""
        self.refresh_token = refresh_token
        self._token = ""

    def token(self) -> str:
        if self._token:
            return self._token
        from google.auth.transport.requests import Request as GARequest
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=None, refresh_token=self.refresh_token,
                            client_id=self.client_id,
                            client_secret=self.client_secret, token_uri=TOKEN_URI)
        creds.refresh(GARequest())
        self._token = creds.token or ""
        return self._token

    def get(self, url: str, params: dict | None = None) -> dict:
        import requests

        response = requests.get(url, headers={"Authorization": f"Bearer {self.token()}"},
                                params=params, timeout=60)
        if response.status_code >= 400:
            raise RuntimeError(f"GET {url} -> {response.status_code} "
                               f"{response.text[:200]}")
        return response.json() if response.content else {}

    def courses(self) -> list[dict]:
        return self.get(f"{CLASSROOM_BASE}/courses",
                        {"teacherId": "me", "courseStates": "ACTIVE",
                         "pageSize": 100}).get("courses", [])

    def coursework(self, course_id: str) -> list[dict]:
        return [c for c in self.get(f"{CLASSROOM_BASE}/courses/{course_id}/courseWork",
                                    {"courseWorkStates": "PUBLISHED", "pageSize": 100}
                                    ).get("courseWork", [])
                if c.get("workType") == "ASSIGNMENT"]

    def submissions(self, course_id: str, cw_id: str) -> list[dict]:
        """EVERY state, not just TURNED_IN, following pagination to the end."""
        out: list[dict] = []
        token = None
        while True:
            params = {"pageSize": 100}
            if token:
                params["pageToken"] = token
            data = self.get(f"{CLASSROOM_BASE}/courses/{course_id}/courseWork/"
                            f"{cw_id}/studentSubmissions", params)
            out.extend(data.get("studentSubmissions", []))
            token = data.get("nextPageToken")
            if not token:
                return out

    def submission(self, course_id: str, cw_id: str, sub_id: str) -> dict:
        return self.get(f"{CLASSROOM_BASE}/courses/{course_id}/courseWork/{cw_id}"
                        f"/studentSubmissions/{sub_id}")

    def download(self, file_id: str, name: str,
                 target_dir: Path) -> tuple[Path | None, str]:
        """Download one attachment; Google Docs are exported to PDF.

        Returns ``(path, reason)`` so a failure is reported with WHY it failed
        instead of being indistinguishable from "this submission has nothing".
        """
        import requests

        meta = self.get(f"{DRIVE_BASE}/files/{file_id}",
                        {"fields": "id,name,mimeType,capabilities"})
        caps = meta.get("capabilities") or {}
        if caps and caps.get("canDownload") is False:
            return None, "Drive does not permit downloading this file"
        mime = meta.get("mimeType") or "application/octet-stream"
        headers = {"Authorization": f"Bearer {self.token()}"}
        if mime == "application/vnd.google-apps.document":
            url = f"{DRIVE_BASE}/files/{file_id}/export"
            params = {"mimeType": "application/pdf"}
            out_name = name.rsplit(".", 1)[0] + ".pdf"
        else:
            url = f"{DRIVE_BASE}/files/{file_id}"
            params = {"alt": "media"}
            out_name = name
        response = requests.get(url, headers=headers, params=params, timeout=300)
        if response.status_code >= 400:
            return None, f"Drive answered HTTP {response.status_code}"
        if not response.content:
            return None, "Drive returned an empty file"
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / _safe_name(out_name)
        path.write_bytes(response.content)
        return path, ""


def _safe_name(name: str) -> str:
    keep = [c if (c.isalnum() or c in " ._-()") else "_" for c in (name or "file")]
    cleaned = "".join(keep).strip().strip(".")
    return (cleaned or "file")[:120]


# ------------------------------------------------- what a real submission holds

def submission_fingerprint(sub: dict) -> str:
    """The parts of a StudentSubmission this run must not change."""
    attachments = []
    for att in ((sub.get("assignmentSubmission") or {}).get("attachments") or []):
        drive = att.get("driveFile") or {}
        attachments.append({"id": drive.get("id"), "title": drive.get("title")})
    history = []
    for entry in (sub.get("submissionHistory") or []):
        state = (entry.get("stateHistory") or {}).get("state")
        grade = (entry.get("gradeHistory") or {}).get("grade")
        history.append({"t": entry.get("updateTime"), "state": state,
                        "grade": grade, "actor": (entry.get("stateHistory") or {}
                                                  ).get("actorUserId")
                        or (entry.get("gradeHistory") or {}).get("actorUserId")})
    return digest({
        "state": sub.get("state"),
        "draft_grade": sub.get("draftGrade"),
        "assigned_grade": sub.get("assignedGrade"),
        "late": sub.get("late"),
        "associated_with_developer": sub.get("associatedWithDeveloper"),
        "attachments": sorted(attachments, key=lambda a: str(a.get("id"))),
        "history": sorted(history, key=lambda h: str(h.get("t"))),
    })


def snapshot(google: GoogleRead, pairs, report: Report) -> dict[str, str]:
    """Hash every submission this run will touch, BEFORE it touches anything."""
    out: dict[str, str] = {}
    for course_id, cw_id, sub_id in pairs:
        sub = google.submission(course_id, cw_id, sub_id)
        out[sub_id] = submission_fingerprint(sub)
    report.note(f"  before-snapshot taken for {len(out)} submission(s)")
    return out


# ---------------------------------------------------------------- marking

def _find(node, key):
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if key in current:
                return current[key]
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return None


def mark_once(app: LiveApp, session, student: Path, scheme: Path, label: str,
              report: Report, timeout: float) -> dict | None:
    """One real correction through the app's own pipeline, read back stored."""
    cookie, csrf = session
    body, content_type = multipart(
        {"student_name": label, "pipeline": "auto"},
        {"submission": (student.name, student.read_bytes()),
         "mark_scheme": (scheme.name, scheme.read_bytes())})
    started = time.time()
    status, _, raw = http(
        "POST", f"http://127.0.0.1:{app.port}/api/corrections/unified",
        data=body, timeout=300,
        headers={"Content-Type": content_type, "Cookie": cookie,
                 "X-MC5-CSRF": csrf, "Origin": f"http://127.0.0.1:{app.port}"})
    if status != 200:
        report.check(f"{label}: the app accepts the real paper", False,
                     f"HTTP {status}: {raw[:200]!r}")
        return None
    job = json.loads(raw)
    job_id = job.get("job_id")

    detail: dict = {}
    state = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _, raw = http("GET", f"http://127.0.0.1:{app.port}/api/jobs/{job_id}",
                            headers={"Cookie": cookie})
        if code == 200:
            detail = json.loads(raw)
            state = str(detail.get("status") or detail.get("state") or "").upper()
            if state in ("COMPLETED", "FAILED", "CANCELLED", "ERROR"):
                break
        time.sleep(2.0)
    elapsed = time.time() - started
    if state != "COMPLETED":
        report.check(f"{label}: the live model completes the correction", False,
                     f"{state or 'no answer'} after {int(elapsed)}s")
        return None
    report.check(f"{label}: the live model completes the correction", True,
                 f"{int(elapsed)}s")

    result_id = _find(detail, "result_id")
    if not result_id:
        code, _, raw = http("GET", f"http://127.0.0.1:{app.port}/api/results"
                                   f"?q={label}&limit=5", headers={"Cookie": cookie})
        rows = (json.loads(raw).get("results") or []) if code == 200 else []
        result_id = rows[0].get("result_id") or rows[0].get("id") if rows else None
    if not result_id:
        report.check(f"{label}: the stored result is readable", False,
                     "no result id in the job detail")
        return None
    code, _, raw = http("GET", f"http://127.0.0.1:{app.port}/api/results/{result_id}",
                        headers={"Cookie": cookie})
    if code != 200:
        report.check(f"{label}: the stored result is readable", False, f"HTTP {code}")
        return None
    stored = json.loads(raw)
    payload = stored.get("result_json") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    questions = {}
    for question in (payload.get("questions") or []):
        number = str(question.get("question_number") or "?")
        try:
            questions[number] = {
                "awarded": int(float(question.get("awarded_marks") or 0)),
                "max": int(float(question.get("max_marks") or 0)),
                "reason": str(question.get("loss_reason")
                              or question.get("summary") or "")[:200],
            }
        except (TypeError, ValueError):
            continue
    return {
        "label": label, "result_id": str(result_id), "seconds": round(elapsed, 1),
        "model": str(stored.get("marking_model") or ""),
        "earned": float(stored.get("earned") or 0),
        "available": float(stored.get("available") or 0),
        "percentage": float(stored.get("percentage") or 0),
        "grade": str(stored.get("provisional_grade") or ""),
        "confidence": str(stored.get("confidence") or ""),
        "review_required": bool(stored.get("review_required")),
        "double_check_status": str(stored.get("double_check_status") or ""),
        "questions": questions,
        "verification_present": bool(stored.get("verification_json")),
    }


def _first_submission_with_work(google: "GoogleRead",
                                classes: list[dict]) -> tuple[str, str, str] | None:
    """One real turned-in submission, used to aim the publish probe at.

    Read-only: this only lists coursework and submissions.
    """
    for course in classes:
        cid = str(course.get("id"))
        try:
            for cw in google.coursework(cid):
                subs = google.submissions(cid, str(cw.get("id")))
                turned = [s for s in subs
                          if s.get("state") in ("TURNED_IN",
                                                "STUDENT_EDITED_AFTER_TURN_IN")]
                if turned:
                    return cid, str(cw.get("id")), str(turned[0].get("id"))
        except Exception:  # noqa: BLE001 - a course we cannot read is not fatal
            continue
    return None


def full_sync(app: LiveApp, session, timeout: float = 900.0) -> tuple[bool, dict]:
    """Drive the app's full Classroom sync through its POLLABLE transport.

    ``GET /api/classroom/sync`` is a Server-Sent Events stream; a plain HTTP
    client reading it can be cut off mid-chunk (it was, on the first run). The
    app ships a polling pair for exactly that case — ``/sync/start`` plus
    ``/sync/progress`` — so the harness uses the transport meant for a client
    that cannot hold a stream open. Read-only either way: the sync only GETs
    from Google.
    """
    cookie, _csrf = session
    code, _, raw = http("GET", f"http://127.0.0.1:{app.port}/api/classroom/sync/start",
                        headers={"Cookie": cookie}, timeout=120)
    if code != 200:
        return False, {"error": f"HTTP {code}: {raw[:200]!r}"}
    snapshot = json.loads(raw)
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _, raw = http("GET",
                            f"http://127.0.0.1:{app.port}/api/classroom/sync/progress",
                            headers={"Cookie": cookie}, timeout=60)
        if code == 200:
            snapshot = json.loads(raw)
            if not snapshot.get("running") and snapshot.get("finished_at"):
                break
        time.sleep(1.5)
    return True, snapshot


def _drive_attachments(sub: dict) -> list[dict]:
    """The attachments of a submission that are downloadable Drive files.

    A Classroom submission can attach a LINK (or a Form/YouTube payload)
    instead of a Drive file, and only a Drive file has an id we can fetch.
    """
    atts = (sub.get("assignmentSubmission") or {}).get("attachments") or []
    return [a for a in atts if ((a.get("driveFile") or {}).get("id"))]


def compare_marks(first: dict, second: dict) -> dict:
    """Per-question agreement between two independent corrections."""
    lines, mismatches = [], 0
    for number in sorted(set(first.get("questions", {})) |
                         set(second.get("questions", {})),
                         key=lambda n: (len(n), n)):
        a = first["questions"].get(number, {})
        b = second["questions"].get(number, {})
        if a.get("awarded") != b.get("awarded"):
            mismatches += 1
            lines.append(f"Q{number}: {a.get('awarded')} then {b.get('awarded')}")
    return {"questions": len(set(first.get("questions", {})) |
                             set(second.get("questions", {}))),
            "mismatches": mismatches, "detail": lines}


# --------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="required: without it nothing is fetched or marked")
    parser.add_argument("--process-real-student-work", action="store_true",
                        dest="ack",
                        help="required: acknowledge real student work is processed")
    parser.add_argument("--app", type=Path, default=REPO,
                        help="the application under test (default: this checkout)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="the real installation's data dir (auto-detected)")
    parser.add_argument("--course-id", action="append", default=[],
                        help="limit to these courses (repeatable)")
    parser.add_argument("--max-submissions", type=int, default=2,
                        help="submissions to mark per assignment (default 2)")
    parser.add_argument("--max-total", type=int, default=3,
                        help="cap on submissions marked in the whole run "
                             "(default 3); each one costs one live call per "
                             "--repeat, so this is what bounds the run")
    parser.add_argument("--repeat", type=int, default=2,
                        help="times to mark each paper, for repeatability "
                             "(default 2; 1 disables the cross-check)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_JOB_TIMEOUT,
                        help="seconds allowed per correction")
    parser.add_argument("--keep", action="store_true",
                        help="keep the throwaway directory (it holds a copied "
                             "credential) instead of deleting it")
    args = parser.parse_args()

    report = Report(LOGS / "verify_live_classroom.txt")
    report.note("== live Classroom certification (read-only) ==")

    if not args.live or not args.ack:
        report.note("\nREFUSED: this run downloads and marks REAL student work.")
        report.note("Pass --live --process-real-student-work to confirm you "
                    "have authorised that. Nothing was fetched.")
        return EXIT_REFUSED

    app = args.app.resolve()
    data_dir = resolve_data_dir(args.data_dir)
    if data_dir is None or not app.joinpath("backend").is_dir():
        report.note("\nREFUSED: could not find the installation or its data dir.")
        report.note(f"  app      : {app}")
        report.note(f"  data dir : {data_dir}")
        return EXIT_REFUSED

    secrets = mine_secrets(data_dir)
    client_json = find_client_json(data_dir)
    report.note(f"\ninstallation : {app}")
    report.note(f"data dir     : {data_dir}")
    report.note(f"gemini keys  : {len(secrets.get('gemini_keys') or [])}")
    report.note(f"classroom    : {'refresh token present' if secrets.get('refresh_token') else 'NO TOKEN'}")
    report.note(f"oauth client : {'present' if client_json else 'MISSING'}")
    if not secrets.get("refresh_token") or client_json is None:
        report.note("\nREFUSED: a Classroom refresh token and the OAuth client "
                    "JSON are both required to read your classes.")
        return EXIT_REFUSED
    if not secrets.get("gemini_keys"):
        report.note("\nREFUSED: no Gemini key to mark with.")
        return EXIT_REFUSED

    real_state = {name: stamp(data_dir / name)
                  for name in ("config.toml", "secrets.toml", "app.db")}

    work = Path(tempfile.mkdtemp(prefix="mc5_live_"))
    report.note(f"throwaway dir: {work}")
    report.note(f"sandbox class: {SANDBOX_COURSE_ID} (no Classroom write flow runs "
                f"in this mode, so it is recorded rather than used)")

    instance: LiveApp | None = None
    evidence: dict = {"run": {"started_at": time.time(), "app": str(app),
                              "data_dir": str(data_dir), "argv": sys.argv[1:]},
                      "safety": {}, "safety_breach": False, "classes": [],
                      "marks": [], "review_queue": [],
                      "after": {}, "cleanup": {}, "cleanup_left_alone": []}
    status = EXIT_OK
    try:
        data = prepare_data(data_dir, work, client_json, secrets)

        # ---- 1. THE BARRIER, before a single student byte is fetched --------
        report.note("\n== 1. the write barrier, proven before anything is read ==")
        probe = run_guard_probe(work, data, app)
        evidence["safety"]["guard_probe"] = probe
        if probe.get("error"):
            report.check("the guard probe runs against the throwaway config",
                         False, probe["error"][-300:])
            return EXIT_RUN_FAILED
        report.check("the app reads testing-only mode as ON", probe.get("testing_only") is True,
                     f"testing_only={probe.get('testing_only')}")
        not_blocked = [h["action"] for h in probe.get("helpers", [])
                       if not h.get("blocked")]
        report.check("every publishing helper is refused", not not_blocked,
                     f"{len(probe.get('helpers', []))} helpers; not blocked: {not_blocked}")
        report.check("ZERO writes reached the HTTP transport",
                     probe.get("transport_writes") == 0,
                     f"recorded writes: {probe.get('transport_writes')}")
        report.check("the gate is a switch, not a wall",
                     probe.get("negative_control") == "allowed_when_off",
                     f"with testing off: {probe.get('negative_control')}")
        if (probe.get("testing_only") is not True
                or not_blocked or probe.get("transport_writes") != 0):
            evidence["safety_breach"] = True
            report.note("\nSTOPPING: the barrier is not sound, so nothing is fetched.")
            return EXIT_NOT_CERTIFIED

        # ---- 2. the app, on the throwaway directory -------------------------
        instance = LiveApp(app, data, free_port(), LOGS / "live_classroom_app.log")
        instance.start()
        if not wait_health(instance.port, LAUNCH_TIMEOUT):
            report.check("the application starts against the throwaway directory",
                         False, instance.output()[-600:])
            return EXIT_RUN_FAILED
        report.check("the application starts against the throwaway directory", True,
                     f"port {instance.port}")
        session = sign_in(instance.port, data, "live-verifier")
        if session is None:
            report.check("the verifier can sign in", False, "no session")
            return EXIT_RUN_FAILED
        report.check("the verifier can sign in", True, "session established")

        cookie, csrf = session
        # ``csrf`` is not optional: the auth gate refuses a mutating request
        # without X-MC5-CSRF, and a probe that never reached the guard would
        # say nothing about the guard (it said "403" on the first run).
        code, _, raw = http("GET", f"http://127.0.0.1:{instance.port}/api/settings",
                            headers={"Cookie": cookie})
        settings = json.loads(raw) if code == 200 else {}
        evidence["safety"]["settings"] = {"testing": settings.get("testing"),
                                          "mock_mode": settings.get("mock_mode")}
        report.check("the RUNNING app reports testing-only ON",
                     bool((settings.get("testing") or {}).get("only")),
                     f"testing={settings.get('testing')}")
        report.check("the RUNNING app is not in mock mode",
                     settings.get("mock_mode") is False,
                     f"mock_mode={settings.get('mock_mode')}")

        # ---- 3. the API's own publication paths, through HTTP ---------------
        report.note("\n== 3. the app's own publish paths, through its API ==")
        started, sync = full_sync(instance, session)
        report.check("a READ of real Classroom still works (the gate is scoped)",
                     started and not sync.get("error"),
                     f"stage={sync.get('stage')} error={sync.get('error')}")
        evidence["safety"]["sync"] = {k: sync.get(k) for k in
                                      ("kind", "running", "stage", "progress",
                                       "summary", "error")}
        code, _, raw = http("GET", f"http://127.0.0.1:{instance.port}/api/classes",
                            headers={"Cookie": cookie})
        classes = json.loads(raw).get("classes", []) if code == 200 else []
        report.check("the teacher's real classes are visible", bool(classes),
                     f"{len(classes)} course(s)")

        google = GoogleRead(client_json, secrets["refresh_token"])
        probe_touched: list[tuple[str, str, str]] = []

        # The app's OWN publish path, aimed at a REAL submission through its
        # real HTTP API. ``attempt_unmanaged`` is set on purpose: it removes the
        # "this assignment is not app-managed anyway" escape, so the ONLY thing
        # that can stop the write is the testing-only guard itself.
        probe = _first_submission_with_work(google, classes)
        if probe is None:
            report.check("the app's own publish path sends nothing to a real "
                         "submission", False, "no turned-in work found to aim at")
        else:
            p_course, p_cw, p_sub = probe
            body = json.dumps({
                "course_id": p_course,
                "operations": [{"type": "draft_grade", "submission_id": p_sub,
                                "grade": 1}],
                "attempt_unmanaged": True}).encode()
            code, _, raw = http(
                "POST", f"http://127.0.0.1:{instance.port}/api/classroom/assignments/"
                        f"{p_cw}/publish", data=body, timeout=180,
                headers={"Content-Type": "application/json", "Cookie": cookie,
                         "X-MC5-CSRF": csrf,
                         "Origin": f"http://127.0.0.1:{instance.port}"})
            payload = json.loads(raw) if raw[:1] in (b"{",) else {}
            ops = payload.get("results") or []
            published = [o for o in ops if str(o.get("status")) == "PUBLISHED"]
            told = [str(o.get("message") or "") for o in ops]
            evidence["safety"]["publish_probe"] = {
                "http": code, "operations": len(ops), "published": len(published),
                "messages": [m[:200] for m in told]}
            published_ok = (code == 200 and bool(ops) and not published
                            and all("TESTING ONLY" in m.upper() for m in told))
            if published:
                evidence["safety_breach"] = True
            report.check("the app's own publish path sends nothing to a real "
                         "submission", published_ok,
                         f"HTTP {code}; {len(published)} published of {len(ops)}; "
                         f"{told[0][:120] if told else 'no operations reported'}")
            probe_touched.append((p_course, p_cw, p_sub))

        # ---- 4. read-only inventory + before-snapshot ----------------------
        wanted = set(args.course_id)
        targets: list[dict] = []
        for course in classes:
            if wanted and str(course.get("id")) not in wanted:
                continue
            cid = str(course.get("id"))
            cws = google.coursework(cid)
            for cw in cws:
                cwid = str(cw.get("id"))
                scheme = data_dir / "mark_schemes" / f"{cwid}.pdf"
                subs = google.submissions(cid, cwid)
                turned = [s for s in subs if s.get("state") in
                          ("TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN")]
                attachments = [s for s in turned
                               if ((s.get("assignmentSubmission") or {}
                                    ).get("attachments") or [])]
                targets.append({"course_id": cid, "coursework_id": cwid,
                                "title": str(cw.get("title") or ""),
                                "scheme": scheme, "scheme_present": scheme.is_file(),
                                "submissions": len(subs), "turned_in": len(turned),
                                "with_attachments": len(attachments)})
        report.note(f"\nassignments seen: {len(targets)}")
        for t in targets:
            report.note(f"  cw={t['coursework_id']} subs={t['submissions']} "
                        f"turned_in={t['turned_in']} scheme={t['scheme_present']}")
        eligible = [t for t in targets if t["scheme_present"] and t["with_attachments"]]
        evidence["classes"] = [
            {k: v for k, v in t.items() if k != "scheme"} for t in targets]
        report.check("assignments with real work AND a stored mark scheme exist",
                     bool(eligible),
                     f"{len(eligible)} of {len(targets)} assignments are markable")
        if not eligible:
            report.note("\nNo assignment has both turned-in work and a stored "
                        "mark scheme; nothing to mark. Reporting without "
                        "certifying accuracy.")
            status = EXIT_NO_TRUTH
            return status

        # Before-snapshot of every submission this run may touch.
        picked: list[dict] = []
        link_only: list[str] = []
        for target in eligible:
            subs = google.submissions(target["course_id"], target["coursework_id"])
            chosen = [s for s in subs
                      if s.get("state") in ("TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN")
                      and _drive_attachments(s)
                      ][:max(0, args.max_submissions)]
            # A submitted attachment that is a LINK (or a Form/YouTube payload)
            # carries no downloadable file: the student typed a URL instead of
            # attaching work. The old filter accepted any attachment and then
            # failed the run on that submission ("attachment 1: not a Drive
            # file") - an unmarkable-data condition reported as a harness
            # defect. Such submissions are counted and noted instead, so the
            # count is never silently dropped from the evidence.
            link_only.extend(
                f"{target['course_id']}/{target['coursework_id']}"
                for s in subs
                if s.get("state") in ("TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN")
                and not _drive_attachments(s)
                and ((s.get("assignmentSubmission") or {}).get("attachments") or []))
        if link_only:
            report.note(f"\n  {len(link_only)} turned-in submission(s) attach a "
                        "link, not a Drive file; nothing is downloadable from "
                        "them, so they are exempt from marking here "
                        "(a UI/teacher decision, not a marking failure).")
            evidence["link_only_submissions"] = len(link_only)
            for sub in chosen:
                picked.append({**target, "submission_id": str(sub.get("id")),
                               "user_id": anon(str(sub.get("userId"))),
                               "_sub": sub})
        # A per-assignment limit still multiplies across assignments, which is
        # how the first run ended up queueing 24 papers. The whole run is
        # bounded here instead.
        if args.max_total > 0 and len(picked) > args.max_total:
            report.note(f"  trimming {len(picked)} candidates to "
                        f"--max-total {args.max_total}")
            picked = picked[:args.max_total]
        pairs = [(p["course_id"], p["coursework_id"], p["submission_id"]) for p in picked]
        pairs.extend(probe_touched)
        before = snapshot(google, pairs, report)
        evidence["after"]["before"] = before

        # ---- 5. mark real papers with the live model -----------------------
        report.note("\n== 5. marking real papers (live model) ==")
        downloads = work / "downloads"
        upload_probe_done = False
        for item in picked:
            label = item["user_id"]
            files: list[Path] = []
            problems: list[str] = []
            for index, att in enumerate((item["_sub"].get("assignmentSubmission") or {}
                                        ).get("attachments") or [], start=1):
                drive = att.get("driveFile") or {}
                if not drive.get("id"):
                    problems.append(f"attachment {index}: not a Drive file")
                    continue
                # An attachment TITLE can contain the student's name, so only
                # the position is ever reported.
                path, why = google.download(str(drive["id"]),
                                            str(drive.get("title") or "file"),
                                            downloads)
                if path is None:
                    problems.append(f"attachment {index}: {why}")
                else:
                    files.append(path)
            if not files:
                report.check(f"{label}: the student's work downloads", False,
                             "; ".join(problems)[:200] or "no attachments")
                continue
            paper = max(files, key=lambda p: p.stat().st_size)
            report.check(f"{label}: the student's work downloads", True,
                         f"{len(files)} file(s), {paper.stat().st_size // 1024} KB")

            runs = []
            for index in range(max(1, args.repeat)):
                run = mark_once(instance, session, paper, item["scheme"],
                                f"{label}" + ("" if index == 0 else f"-r{index + 1}"),
                                report, args.timeout)
                if run is None:
                    break
                runs.append(run)
            if not runs:
                continue
            first = runs[0]
            report.check(f"{label}: marked by a REAL model",
                         bool(first["model"]) and "mock" not in first["model"].lower(),
                         f"model={first['model']}")

            # The report-upload path, through its real HTTP endpoint, on a real
            # stored result. Whatever the app answers, it must NOT have
            # uploaded: a refusal or a pre-condition failure is fine, a Drive
            # file id would not be.
            if not upload_probe_done:
                upload_probe_done = True
                body = json.dumps({"action": "upload"}).encode()
                code, _, raw = http(
                    "POST", f"http://127.0.0.1:{instance.port}/api/results/"
                            f"{first['result_id']}/retry", data=body, timeout=180,
                    headers={"Content-Type": "application/json", "Cookie": cookie,
                             "X-MC5-CSRF": csrf,
                             "Origin": f"http://127.0.0.1:{instance.port}"})
                payload = json.loads(raw) if raw[:1] == b"{" else {}
                uploaded = bool(payload.get("drive_file_id"))
                if uploaded:
                    evidence["safety_breach"] = True
                evidence["safety"]["upload_probe"] = {
                    "http": code, "code": payload.get("code"),
                    "drive_file_id": payload.get("drive_file_id"),
                    "message": str(payload.get("message"))[:200]}
                report.check("uploading a correction report to Drive is refused, "
                             "not merely unlikely",
                             not uploaded,
                             f"HTTP {code} code={payload.get('code')} "
                             f"{str(payload.get('message') or payload.get('detail') or '')[:120]}")
            entry = {
                "student": label, "coursework_id": item["coursework_id"],
                "assignment": item["title"], "paper_pages": None,
                **{k: v for k, v in first.items() if k != "label"},
                "runs": [{"result_id": r["result_id"], "earned": r["earned"],
                          "awarded": {n: q["awarded"] for n, q in r["questions"].items()},
                          "model": r["model"]} for r in runs],
            }
            if len(runs) > 1:
                check = compare_marks(runs[0], runs[1])
                entry["repeatability"] = check
                report.check(f"{label}: two independent marks agree",
                             check["mismatches"] == 0,
                             f"{check['mismatches']} of {check['questions']} "
                             f"questions differed: {check['detail']}")
                if check["mismatches"]:
                    evidence["review_queue"].append({
                        "student": label, "coursework_id": item["coursework_id"],
                        "issue": "repeat disagreement", "detail": check["detail"]})
            if first["review_required"]:
                evidence["review_queue"].append({
                    "student": label, "coursework_id": item["coursework_id"],
                    "issue": "the app itself flagged this result for review",
                    "detail": {"confidence": first["confidence"],
                               "double_check": first["double_check_status"]}})
            evidence["marks"].append(entry)

        # ---- 6. the after-snapshot: did anything change? -------------------
        report.note("\n== 6. nothing on Google's side changed ==")
        after = {}
        changed = []
        for course_id, cw_id, sub_id in pairs:
            live = google.submission(course_id, cw_id, sub_id)
            after[sub_id] = submission_fingerprint(live)
            if before.get(sub_id) != after[sub_id]:
                changed.append(sub_id)
        evidence["after"]["after"] = after
        evidence["after"]["changed"] = changed
        report.check("every touched submission is byte-for-byte unchanged",
                     not changed, f"{len(pairs)} checked; changed: {changed}")
        if changed:
            evidence["safety_breach"] = True
            status = EXIT_NOT_CERTIFIED

        # ---- 7. what the report can and cannot claim -----------------------
        report.note("\n== 7. accuracy ==")
        if evidence["marks"]:
            report.note("  The live model marked real papers and the marks are "
                        "recorded, question by question.")
            report.note("  They are CROSS-CHECKED, not CERTIFIED: no human-"
                        "verified mark key was supplied, so nothing here can "
                        "upgrade 'plausible' to 'correct'.")
        else:
            report.note("  No paper was marked successfully.")
        status = EXIT_NO_TRUTH if status == EXIT_OK else status

    finally:
        if instance is not None:
            stopped = instance.stop()
            report.check("the application stops cleanly", stopped,
                         "port released" if stopped else "port still listening")
        if args.keep:
            evidence["cleanup"] = {"kept": True, "path": str(work)}
            report.note(f"\nkeeping {work} (--keep): it holds a copied credential")
        else:
            deleted = robust_rmtree(work)
            evidence["cleanup"] = {"kept": False, "path": str(work),
                                   "deleted": deleted}
            report.check("the throwaway directory is gone", deleted,
                         str(work) if not deleted else "removed")
        evidence["cleanup_left_alone"] = [
            str(data_dir / name) for name in ("audit_downloads", "audit_reports",
                                              "audit_inventory.json", "app.db",
                                              "config.toml", "secrets.toml")
            if (data_dir / name).exists()]
        report.note("\nleft untouched (pre-existing, not provably this run's):")
        for path in evidence["cleanup_left_alone"]:
            report.note(f"  {path}")

    # The real installation must not have been written to.
    after_state = {name: stamp(data_dir / name) for name in real_state}
    evidence["real_installation"] = {"before": real_state, "after": after_state}
    if real_state != after_state:
        evidence["safety_breach"] = True
    report.check("the real installation's config, secrets and database are "
                 "untouched", real_state == after_state,
                 "unchanged" if real_state == after_state
                 else f"changed: {[k for k in real_state if real_state[k] != after_state[k]]}")

    evidence["exit_code"] = status
    evidence["checks"] = [{"name": n, "ok": ok, "evidence": e}
                          for n, ok, e in report.results]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "LIVE_CLASSROOM_CERTIFICATION.json"
    out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    report.note(f"\nevidence written: {out}")

    failed = report.summary()
    # Exit-code contract: 3 means SAFETY failed (a write escaped, or a real
    # submission changed). An accuracy-only finding — a repeat disagreement, a
    # paper that would not download — is reported as a failed check but leaves
    # the run at "not certified" (4), never dressed up as a clean pass.
    if evidence.get("safety_breach"):
        return EXIT_NOT_CERTIFIED
    if failed:
        return status if status != EXIT_OK else EXIT_NO_TRUTH
    return status


if __name__ == "__main__":
    raise SystemExit(main())
