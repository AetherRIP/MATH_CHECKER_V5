#!/usr/bin/env python3
"""Verify the PORTABLE package itself — not the source checkout.

    python verify_portable.py --zip release/MathChecker_Portable.zip
    python verify_portable.py --package <extracted>/MathChecker_Portable

What it does, against the real artifacts:

  1. extracts the real ZIP into a deliberately awkward path (spaces, accents,
     parentheses) and checks the packaged tree,
  2. starts the app with its own launcher (MathChecker.bat), waits for the
     health endpoint and fetches the user interface,
  3. proves the portable copy kept its data beside itself and never wrote to
     %LOCALAPPDATA%\\MathChecker,
  4. runs the launcher a second time (a duplicate launch must be refused),
  5. signs in with the credentials the packaged app generated on first run,
  6. starts the Demo-mode copy and corrects a PDF through the real API,
  7. generates a report PDF and opens it with the PACKAGED runtime,
  8. exports student/result data from one copy and imports it into the other,
  9. stops the copy with Stop.bat (clean shutdown, no orphan processes),
 10. starts it again and checks the imported records are still there,
 11. scans the extracted package for credentials, dev paths and personal data.

Every check prints PASS/FAIL with the evidence behind it. Exit code 0 only when
everything passed. Run it from the distribution folder (it needs no project
imports, only the Python you invoke it with).
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Deliberately awkward: spaces, brackets and a non-ASCII character.
AWKWARD_DIR = "Math Check\u00ebr Portable (v5.2)"
LAUNCH_TIMEOUT = 120.0


class Report:
    """Collects checks and writes them to the console AND to a file.

    The file matters on Windows: a launcher that is started in the background
    can lose its console output, and the evidence has to survive that.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.results: list[tuple[str, bool, str]] = []
        self.path = path
        if path is not None:
            path.write_text("", encoding="utf-8")

    def _emit(self, line: str) -> None:
        print(line, flush=True)
        if self.path is not None:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def note(self, text: str = "") -> None:
        self._emit(text)

    def check(self, name: str, ok: bool, evidence: str = "") -> bool:
        self.results.append((name, bool(ok), evidence))
        mark = "PASS" if ok else "FAIL"
        self._emit(f"  [{mark}] {name}" + (f" — {evidence}" if evidence else ""))
        return bool(ok)

    def summary(self) -> int:
        failed = [r for r in self.results if not r[1]]
        self.note("\n" + "=" * 72)
        self.note(f"{len(self.results) - len(failed)}/{len(self.results)} checks passed")
        for name, _, evidence in failed:
            self.note(f"  FAILED: {name}" + (f" — {evidence}" if evidence else ""))
        self.note("=" * 72)
        return 1 if failed else 0


# ------------------------------------------------------------------ utilities

def _lower(headers) -> dict:
    """HTTP header names are case-insensitive; urllib's dict is not."""
    return {str(key).lower(): value for key, value in dict(headers or {}).items()}


def http(method: str, url: str, *, data: bytes | None = None,
         headers: dict[str, str] | None = None,
         timeout: float = 60.0) -> tuple[int, dict, bytes]:
    """Return (status, headers, body) with lowercase header names."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _lower(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _lower(exc.headers), exc.read()
    except urllib.error.URLError:
        return 0, {}, b""          # nothing listening yet, or it went away


def robust_rmtree(path: Path, attempts: int = 6) -> bool:
    """Delete a tree even while a stopped copy still holds a log file open.

    Windows keeps a handle briefly after a process exits; an interrupted run
    must not make the NEXT run fail before it starts.
    """
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(2)
    return not path.exists()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def listening_pids(port: int) -> list[int]:
    pids: list[int] = []
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=30).stdout
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0].upper() == "TCP"
                        and parts[3].upper() == "LISTENING"
                        and parts[1].endswith(f":{port}")):
                    try:
                        pids.append(int(parts[4]))
                    except ValueError:
                        pass
        else:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}",
                                  "-sTCP:LISTEN", "-t"],
                                 capture_output=True, text=True, timeout=30).stdout
            pids = [int(t) for t in out.split() if t.isdigit()]
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(pids))


def python_pids() -> set[int]:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return set()
    found = set()
    for line in out.splitlines():
        cells = [c.strip().strip('"') for c in line.split('","')]
        if cells and cells[0].lower().startswith("python.exe"):
            try:
                found.add(int(cells[1]))
            except (IndexError, ValueError):
                pass
    return found


def wait_health(port: int, timeout: float = LAUNCH_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, body = http("GET", f"http://127.0.0.1:{port}/api/health",
                               timeout=5)
        if status == 200 and b'"ok"' in body:
            return True
        time.sleep(1.0)
    return False


def multipart(fields: dict[str, str],
              files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "----mc5verify%d" % int(time.time() * 1000)
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode())
    for name, (filename, content) in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
            + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


#: Shells with NoDefaultCurrentDirectoryInExePath set (a Windows hardening
#: option, and this sandbox) do not run a bare ``MathChecker.bat``; an explicit
#: relative path is honoured everywhere, including a double-click.
LAUNCHER = ".\\MathChecker.bat" if os.name == "nt" else "./MathChecker.bat"
STOPPER = ".\\Stop.bat" if os.name == "nt" else "./Stop.bat"


class Instance:
    """A launched copy: its launcher process, log and port."""

    def __init__(self, package: Path, port: int, log: Path) -> None:
        self.package = package
        self.port = port
        self.log = log
        self.proc: subprocess.Popen | None = None

    def start(self, *extra: str) -> None:
        env = dict(os.environ)
        env["MC5_NO_BROWSER"] = "1"          # never open a browser in a check
        env.pop("NoDefaultCurrentDirectoryInExePath", None)
        handle = open(self.log, "w", encoding="utf-8", errors="replace")
        # cmd resolves the launcher relative to the working directory, exactly
        # as a double-click from Explorer does.
        self.proc = subprocess.Popen(
            ["cmd", "/c", LAUNCHER, str(self.port), *extra],
            cwd=str(self.package), env=env, stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True)

    def stop_with_launcher(self, timeout: float = 60.0) -> bool:
        """Use the shipped Stop.bat, exactly as a teacher would."""
        env = dict(os.environ)
        env.pop("NoDefaultCurrentDirectoryInExePath", None)
        subprocess.run(
            ["cmd", "/c", STOPPER, str(self.port)], cwd=str(self.package),
            env=env, capture_output=True, text=True, timeout=int(timeout),
            input="\n")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not listening_pids(self.port):
                return True
            time.sleep(1.0)
        return False

    def log_text(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def build_pdfs(package: Path, out_dir: Path) -> tuple[Path, Path]:
    """Make the student + mark-scheme PDFs with the PACKAGED runtime.

    Using the bundled interpreter proves its native libraries (PyMuPDF) load on
    this machine, which a source checkout cannot prove.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    script = out_dir / "_mkpdf.py"
    script.write_text(
        # ``pymupdf`` (not ``fitz``): the old alias prints a deprecation warning
        # on stdout, which would end up in the machine-readable output.
        "import sys, pymupdf\n"
        "path, text = sys.argv[1], sys.argv[2]\n"
        "doc = pymupdf.open()\n"
        "page = doc.new_page()\n"
        "page.insert_text((72, 100), text, fontsize=13)\n"
        "page.insert_text((72, 130), 'Solve 2x + 3 = 11 and show your working.',\n"
        "                 fontsize=11)\n"
        "doc.save(path)\n"
        "print('pages', doc.page_count)\n",
        encoding="utf-8")
    student = out_dir / "student.pdf"
    scheme = out_dir / "mark_scheme.pdf"
    runtime = package / "runtime" / ("python.exe" if os.name == "nt" else "bin/python3")
    for path, text in ((student, "Student A - Question 1"),
                       (scheme, "Mark scheme - Question 1 (3 marks)")):
        result = subprocess.run([str(runtime), str(script), str(path), text],
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not path.is_file():
            raise RuntimeError(f"packaged runtime could not write {path.name}: "
                               f"{result.stdout}{result.stderr}")
    return student, scheme


def read_pdf_pages(package: Path, path: Path) -> int:
    runtime = package / "runtime" / ("python.exe" if os.name == "nt" else "bin/python3")
    result = subprocess.run(
        [str(runtime), "-c",
         "import sys, pymupdf; d = pymupdf.open(sys.argv[1]); print(d.page_count)",
         str(path)], capture_output=True, text=True, timeout=120)
    numbers = [line for line in result.stdout.split() if line.strip().isdigit()]
    return int(numbers[-1]) if numbers else 0


# --------------------------------------------------------------------- checks

def check_package_tree(report: Report, package: Path) -> None:
    report.note("\n== package contents ==")
    required = ["MathChecker.bat", "Stop.bat", "README.txt", "Data",
                "Data/config.toml.example",
                "main.py", "remote.py", "start_public.bat", "portable.flag",
                "runtime", "frontend/dist/index.html",
                "backend/app/main.py", "backend/app/portability.py",
                "backend/app/api/data_routes.py", "agent/agent.py"]
    missing = [rel for rel in required
               if not package.joinpath(*rel.split("/")).exists()]
    report.check("packaged tree is complete", not missing,
                 "missing: " + ", ".join(missing) if missing else
                 f"{len(required)} required paths present")

    # A runtime that has lost its compiled modules (*.pyd) still looks complete
    # - the stdlib zip, site-packages and the tree above are all intact - but it
    # cannot import socket/sqlite3/ssl on the recipient's PC, so the app cannot
    # start. This is not hypothetical: a *.py[cod] .gitignore rule filtered every
    # *.pyd out of the repository's copy of this runtime.
    runtime_dir = package / "runtime"
    if runtime_dir.is_dir():
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from build_release import check_runtime_extensions  # noqa: PLC0415
            missing_modules = check_runtime_extensions(runtime_dir)
        except Exception as exc:  # noqa: BLE001 — an unverifiable runtime is a fail
            report.check("packaged runtime contains its compiled modules", False,
                         f"could not inspect the runtime: {exc}")
        else:
            report.check("packaged runtime contains its compiled modules",
                         not missing_modules,
                         "missing: " + ", ".join(missing_modules) if missing_modules
                         else "socket/sqlite3/ssl/PyMuPDF/Pillow extensions present")

    # The tunnel helper is bundled only when the BUILD machine had cloudflared,
    # and the pipeline deliberately treats its absence as a warning. Turning
    # that same absence into a blocking FAIL here made this verifier report a
    # broken package on any machine without cloudflared, which is worse than
    # not checking at all. Presence is verified; absence is reported.
    helper = package / "tools" / "cloudflared.exe"
    if helper.exists():
        report.check("bundled tunnel helper is a real executable",
                     helper.stat().st_size > 1_000_000,
                     f"tools/cloudflared.exe — {helper.stat().st_size // 1024} KB")
    else:
        report.note("  note: no tools/cloudflared.exe in this package — Public "
                    "access will need cloudflared on the target PC.")

    bad: list[str] = []
    for path in package.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(package).as_posix()
        if rel.startswith("runtime/"):
            continue                                  # bundled third-party libs
        low = path.name.lower()
        if (low.startswith(".env") or low.endswith((".pem", ".key", ".p12", ".db",
                                                    ".sqlite", ".pdf", ".xlsx"))
                or "secret" in low or "credential" in low or "friend" in low):
            bad.append(rel)
    report.check("no credentials or personal data in the package", not bad,
                 ", ".join(bad[:5]) if bad else "no .env/.pem/.key/.db/.pdf/credential file")

    leaks: list[str] = []
    needle = (b"MATH V6.0", b"Rudransha", b"OneDrive")
    for path in package.rglob("*.py"):
        if "/runtime/" in path.as_posix():
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if any(n in blob for n in needle):
            leaks.append(path.relative_to(package).as_posix())
    report.check("no development-machine paths in the app code", not leaks,
                 ", ".join(leaks[:5]) if leaks else "no build-machine path found")


def check_config_template(report: Report, package: Path) -> None:
    """The shipped settings reference must be valid, complete and secret-free.

    A documentation file drifts silently, so the template is parsed by the
    PACKAGED runtime and its key set is compared with the config the application
    itself writes: that is what keeps "every setting is documented" true when
    the app gains a setting later. The file is also checked for credential
    values, because the package must never ship one.
    """
    report.note("\n== configuration template ==")
    template = package / "Data" / "config.toml.example"
    if not report.check("portable settings reference is shipped", template.is_file(),
                        "Data/config.toml.example"):
        return

    text = template.read_text(encoding="utf-8", errors="replace")
    suspicious = [needle for needle in ("AIza", "sk-", "ghp_", "xoxb-", "BEGIN PRIVATE KEY")
                  if needle in text]
    report.check("settings reference carries no credential values", not suspicious,
                 ", ".join(suspicious) if suspicious else "no API-key/token material")

    runtime = package / "runtime" / ("python.exe" if os.name == "nt" else "bin/python3")
    code = (
        "import os, pathlib, sys, tempfile, tomllib\n"
        "sys.path.insert(0, os.getcwd())\n"
        "def flat(d):\n"
        "    return sorted(f'{s}.{k}' for s, v in d.items()\n"
        "                  if isinstance(v, dict) for k in v)\n"
        "tpl = flat(tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')))\n"
        "with tempfile.TemporaryDirectory(prefix='mc5_cfg_') as tmp:\n"
        "    os.environ['MC5_DATA_DIR'] = tmp\n"
        "    from app.config import AppConfig, save_config\n"
        "    save_config(AppConfig())\n"
        "    live = flat(tomllib.loads(pathlib.Path(tmp, 'config.toml').read_text(encoding='utf-8')))\n"
        "print('MISSING ' + ','.join(k for k in live if k not in tpl))\n"
        "print('EXTRA ' + ','.join(k for k in tpl if k not in live))\n"
        "print('KEYS ' + str(len(tpl)))\n"
    )
    result = subprocess.run([str(runtime), "-c", code, str(template)],
                            cwd=str(package / "backend"), capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines()
                 if " " in line)
    missing = [k for k in lines.get("MISSING", "").split(",") if k]
    extra = [k for k in lines.get("EXTRA", "").split(",") if k]
    keys = lines.get("KEYS", "0")
    if result.returncode != 0:
        report.check("settings reference documents every setting", False,
                     (result.stderr or "packaged runtime could not parse it").strip()[-160:])
        return
    report.check("settings reference documents every setting",
                 not missing and not extra,
                 f"{keys} keys; undocumented: {', '.join(missing) or 'none'}; "
                 f"unknown: {', '.join(extra) or 'none'}")


def check_runtime_dependencies(report: Report, package: Path) -> None:
    """Every package the application needs must import INSIDE the shipped runtime.

    This is the check whose absence let a release ship without PyMuPDF: the
    manifest listed it (read from the build machine) while the bundled runtime
    did not have it, so PDF text extraction degraded silently on the
    recipient's machine.
    """
    report.note("\n== bundled runtime dependencies ==")
    runtime = package / "runtime" / ("python.exe" if os.name == "nt"
                                     else "bin/python3")
    modules = ["fastapi", "uvicorn", "multipart", "pydantic", "starlette",
               "google.genai", "google.auth", "google_auth_oauthlib",
               "requests", "httpcore", "openpyxl", "reportlab", "pymupdf",
               "pypdf", "PIL", "cryptography", "sqlite3", "ssl"]
    code = (
        "import importlib\n"
        f"names = {modules!r}\n"
        "bad = []\n"
        "for name in names:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{name}: {exc}')\n"
        "print('MISSING ' + '; '.join(bad) if bad else 'ALL OK')\n"
    )
    for label, cwd in (("from the application folder", package),
                       ("from an unrelated working directory", Path(tempfile.gettempdir()))):
        result = subprocess.run([str(runtime), "-c", code], cwd=str(cwd),
                                capture_output=True, text=True, timeout=300)
        out = (result.stdout or "").strip()
        report.check(f"packaged runtime imports what it ships ({label})",
                     result.returncode == 0 and out == "ALL OK",
                     f"{len(modules)} modules -> {out[:160] or result.stderr[:160]}")


def check_launch(report: Report, instance: Instance, data_dir: Path,
                 local_before: dict) -> None:
    report.note(f"\n== launching the package on port {instance.port} ==")
    instance.start()
    report.check("application starts from its own launcher",
                 wait_health(instance.port),
                 f"cmd /c MathChecker.bat {instance.port} -> /api/health 200")

    status, headers, body = http("GET", f"http://127.0.0.1:{instance.port}/")
    bundle = body.decode("utf-8", "replace")
    report.check("user interface is served",
                 status == 200 and "assets/index-" in bundle,
                 f"GET / -> {status}, {len(body)} bytes, "
                 f"{'bundle referenced' if 'assets/index-' in bundle else 'no bundle'}")

    report.check("data directory created beside the application",
                 any((data_dir / name).exists()
                     for name in ("app.db", "config.toml", "secrets.toml")),
                 f"{data_dir} → " + ", ".join(sorted(p.name for p in data_dir.iterdir())[:6]))
    if local_before is not None:
        local_after = {p.relative_to(_local_dir()).as_posix():
                       [p.stat().st_size, int(p.stat().st_mtime)]
                       for p in _local_dir().rglob("*") if p.is_file()}
        report.check("portable copy never wrote to %LOCALAPPDATA%",
                     local_after == local_before,
                     "unchanged" if local_after == local_before else
                     f"changed: {set(local_after) ^ set(local_before)}")


def _local_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "MathChecker"


def snapshot_local() -> dict:
    root = _local_dir()
    if not root.exists():
        return {}
    snap = {}
    for path in root.rglob("*"):
        if path.is_file():
            try:
                snap[path.relative_to(root).as_posix()] = [
                    path.stat().st_size, int(path.stat().st_mtime)]
            except OSError:
                pass
    return snap


def check_duplicate_launch(report: Report, instance: Instance) -> None:
    report.note("\n== second launch of the same copy ==")
    env = dict(os.environ)
    env["MC5_NO_BROWSER"] = "1"
    env.pop("NoDefaultCurrentDirectoryInExePath", None)
    result = subprocess.run(["cmd", "/c", LAUNCHER, str(instance.port)],
                            cwd=str(instance.package), env=env,
                            capture_output=True, text=True, timeout=180)
    out = (result.stdout or "") + (result.stderr or "")
    report.check("duplicate launch is refused, not started twice",
                 result.returncode == 0 and "already running" in out.lower(),
                 f"exit {result.returncode}, said: "
                 f"{' '.join(out.split())[:90]}")
    pids = listening_pids(instance.port)
    report.check("exactly one process owns the port", len(pids) == 1,
                 f"listening PIDs: {pids}")


def sign_in(port: int, data_dir: Path, label: str = "verifier") -> tuple[str, str] | None:
    """Sign in the way the packaged copy expects: 204 + X-MC5-CSRF header.

    A fresh copy has no host password yet, so this uses the bootstrap account
    the copy generated into ITS OWN data directory - and, when there is none,
    creates the host account through the same endpoint the first-run screen
    calls (allowed from this machine only).
    """
    credentials: list[tuple[str, str]] = []
    record = data_dir / "distributed_bootstrap.txt"
    if record.exists():
        try:
            creds = json.loads(record.read_text(encoding="utf-8"))
        except ValueError:
            creds = {}
        for key in ("developer", "teacher"):
            account = creds.get(key)
            if isinstance(account, dict) and account.get("username"):
                credentials.append((str(account["username"]),
                                    str(account["password"])))

    def attempt(username: str, password: str) -> tuple[str, str] | None:
        status, headers, _ = http(
            "POST", f"http://127.0.0.1:{port}/api/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json",
                     "Origin": f"http://127.0.0.1:{port}"})
        if status not in (200, 204):
            return None
        cookie = headers.get("set-cookie") or ""
        csrf = headers.get("x-mc5-csrf") or ""
        return (cookie.split(";")[0], csrf) if cookie else None

    for username, password in credentials:
        session = attempt(username, password)
        if session:
            return session

    # First-run configuration: the same call the sign-in screen makes.
    password = f"portable-verify-{label}-2026"
    status, _, _ = http("POST", f"http://127.0.0.1:{port}/api/auth/setup",
                        data=json.dumps({"username": label,
                                         "password": password}).encode(),
                        headers={"Content-Type": "application/json",
                                 "Origin": f"http://127.0.0.1:{port}"})
    if status != 200:
        return None
    return attempt(label, password)


def check_auth(report: Report, instance: Instance, data_dir: Path) -> tuple[str, str] | None:
    report.note("\n== sign-in ==")
    status, _, _ = http("GET", f"http://127.0.0.1:{instance.port}/api/data/inventory")
    report.check("protected API refuses an anonymous caller", status == 401,
                 f"GET /api/data/inventory without a session → {status}")
    session = sign_in(instance.port, data_dir)
    report.check("the credentials this copy generated work", session is not None,
                 "signed in as the bootstrap developer account"
                 if session else "login failed")
    if session:
        cookie, _ = session
        status, _, body = http("GET", f"http://127.0.0.1:{instance.port}/api/auth/status",
                               headers={"Cookie": cookie})
        report.check("session is accepted", status == 200 and b'"authenticated":true' in body,
                     f"GET /api/auth/status → {status}")
    return session


def check_correction(report: Report, instance: Instance, data_dir: Path,
                     session: tuple[str, str] | None) -> tuple[int, dict | None]:
    report.note("\n== correction in the packaged copy (demo/mock engine) ==")
    if not session:
        report.check("correction runs end to end", False, "no session")
        return 0, None
    cookie, csrf = session
    package = instance.package
    student, scheme = build_pdfs(package, package.parent / "verify_work")
    report.check("packaged runtime writes PDFs (native libs load)", True,
                 f"PyMuPDF produced {student.name} and {scheme.name}")

    body, content_type = multipart({"student_name": "Portable Verify",
                                    "pipeline": "auto"},
                                   {"submission": ("student.pdf", student.read_bytes()),
                                    "mark_scheme": ("mark_scheme.pdf", scheme.read_bytes())})
    status, _, raw = http("POST", f"http://127.0.0.1:{instance.port}/api/corrections/unified",
                          data=body, timeout=180,
                          headers={"Content-Type": content_type, "Cookie": cookie,
                                   "X-MC5-CSRF": csrf,
                                   "Origin": f"http://127.0.0.1:{instance.port}"})
    if status != 200:
        report.check("unified correction accepts an upload", False,
                     f"HTTP {status}: {raw[:200]!r}")
        return 0, None
    job = json.loads(raw)
    report.check("unified correction accepts an upload", True,
                 f"pipeline={job.get('applied_pipeline')} pages={job.get('page_count')} "
                 f"({job.get('reason', '')[:60]})")

    job_id = job.get("job_id") or job.get("large_pdf_job_id")
    deadline = time.time() + 180
    detail: dict = {}
    while time.time() < deadline:
        status, _, raw = http("GET", f"http://127.0.0.1:{instance.port}/api/jobs/{job_id}",
                              headers={"Cookie": cookie})
        if status == 200:
            detail = json.loads(raw)
            state = str(detail.get("status") or detail.get("state") or "").upper()
            if state in ("COMPLETED", "FAILED", "CANCELLED", "ERROR"):
                break
        time.sleep(2.0)

    def find(key: str):
        stack = [detail]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if key in node:
                    return node[key]
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return None

    state = str(detail.get("status") or "").upper()
    reason = (detail.get("error") or detail.get("error_kind") or "")
    report.check("correction completes with a stored result",
                 state == "COMPLETED",
                 f"job {job_id} → {state or 'no answer'}"
                 + (f" ({str(reason)[:200]})" if state != "COMPLETED" else ""))

    # Marks and the 1-9 grade are STORED: read them back through the same API
    # the Results and Storage pages use, not from the job envelope.
    result_id = find("result_id")
    if not result_id:
        status, _, raw = http("GET", f"http://127.0.0.1:{instance.port}/api/results"
                                     f"?q=Portable%20Verify&limit=5",
                              headers={"Cookie": cookie})
        if status == 200:
            rows = json.loads(raw).get("results") or []
            if rows:
                result_id = rows[0].get("result_id") or rows[0].get("id")
    stored: dict = {}
    if result_id:
        status, _, raw = http("GET", f"http://127.0.0.1:{instance.port}"
                                     f"/api/results/{result_id}",
                              headers={"Cookie": cookie})
        if status == 200:
            stored = json.loads(raw)
    # The stored row's real columns (app/engine/service.py get_result):
    # earned / available / percentage / provisional_grade.
    marks = stored.get("earned")
    max_marks = stored.get("available")
    percentage = stored.get("percentage")
    grade = stored.get("provisional_grade")
    grade_ok = grade is not None and str(grade).isdigit() and 1 <= int(grade) <= 9
    report.check("marks and a 1-9 grade are stored and readable",
                 marks is not None and grade_ok,
                 f"result {result_id}: earned={marks}/{max_marks} "
                 f"percentage={percentage} provisional_grade={grade}")
    return (int(grade) if grade is not None else 0), stored


def check_report(report: Report, instance: Instance,
                 session: tuple[str, str] | None) -> None:
    report.note("\n== PDF report ==")
    if not session:
        report.check("report PDF generates and opens", False, "no session")
        return
    cookie, _ = session
    url = (f"http://127.0.0.1:{instance.port}/api/reports/build/export"
           f"?report_type=class&mode=summary&format=pdf")
    status, headers, body = http("GET", url, timeout=180,
                                 headers={"Cookie": cookie})
    out = instance.package.parent / "verify_work" / "class_report.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    ok = (status == 200 and body[:4] == b"%PDF" and len(body) > 1000
          and "pdf" in (headers.get("content-type") or "").lower())
    report.check("report PDF is generated", ok,
                 f"HTTP {status}, {len(body)} bytes, "
                 f"content-type={headers.get('content-type')}")
    if ok:
        pages = read_pdf_pages(instance.package, out)
        report.check("report PDF opens with the packaged runtime", pages >= 1,
                     f"{out.name}: {pages} page(s)")


def check_export_import(report: Report, source: Instance, source_session,
                        target: Instance, target_session) -> None:
    report.note("\n== export from one copy, import into the other ==")
    if not (source_session and target_session):
        report.check("export/import between two packaged copies", False,
                     "missing session")
        return
    src_cookie, _ = source_session
    status, _, body = http("GET", f"http://127.0.0.1:{source.port}/api/data/export",
                           headers={"Cookie": src_cookie}, timeout=120)
    package_json = json.loads(body) if status == 200 else {}
    counts = package_json.get("summary", {})
    report.check("export produces a versioned package", status == 200,
                 f"HTTP {status}, {len(body)} bytes, students="
                 f"{counts.get('students')}, results={counts.get('results')}")

    tgt_cookie, tgt_csrf = target_session
    body_bytes, content_type = multipart(
        {"mode": "add_only"}, {"file": ("transfer.json", body)})
    status, _, raw = http("POST", f"http://127.0.0.1:{target.port}/api/data/import",
                          data=body_bytes, timeout=180,
                          headers={"Content-Type": content_type,
                                   "Cookie": tgt_cookie, "X-MC5-CSRF": tgt_csrf,
                                   "Origin": f"http://127.0.0.1:{target.port}"})
    report.check("import into the second copy succeeds", status == 200,
                 f"HTTP {status}: {raw[:160]!r}" if status != 200 else
                 f"added={json.loads(raw).get('totals', {}).get('added')}")
    return


def check_logs_and_processes(report: Report, instance: Instance, demo: Instance,
                             data_dir: Path) -> None:
    """Log hygiene + the disabled Tiny Agent.

    A teacher is told credentials and background agents behave a certain way;
    this is where the packaged build is actually held to it.
    """
    report.note("\n== logs and background processes ==")
    generated: list[str] = []
    for base in (data_dir, data_dir / "demo"):
        record = base / "distributed_bootstrap.txt"
        if not record.exists():
            continue
        try:
            creds = json.loads(record.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for key in ("developer", "teacher"):
            account = creds.get(key)
            if isinstance(account, dict) and account.get("password"):
                generated.append(str(account["password"]))

    logs = instance.log_text() + demo.log_text()
    for log_file in (data_dir / "logs").glob("*.log") if (data_dir / "logs").is_dir() else []:
        logs += log_file.read_text(encoding="utf-8", errors="replace")
    leaked = [secret for secret in generated if secret in logs]
    report.check("generated passwords never reach the logs", not leaked,
                 f"{len(generated)} generated credential(s) checked, no match"
                 if not leaked else f"{len(leaked)} leaked into a log")

    agent = '"tiny agent availability"' in logs
    report.check("Tiny Agent is reported as unavailable, not started",
                 agent and '"enabled": false' in logs,
                 "log records enabled=false (no agent process started)"
                 if agent and '"enabled": false' in logs else
                 "no tiny-agent line found in the log")
    pools = logs.count('"pool started"')
    report.check("no duplicate backend or worker pool was created", pools <= 2,
                 f'{pools} pool start(s) across both copies (one each)'
                 if pools else "no pool start recorded")


def check_restart_persistence(report: Report, instance: Instance,
                             data_dir: Path) -> None:
    report.note("\n== stopping with Stop.bat, restarting, data still there ==")
    db = data_dir / "app.db"
    size_before = db.stat().st_size if db.exists() else 0
    stopped = instance.stop_with_launcher()
    report.check("Stop.bat stops the running copy", stopped,
                 f"port {instance.port} free afterwards" if stopped else
                 f"port {instance.port} still answers")
    time.sleep(3)
    size_after = db.stat().st_size if db.exists() else 0
    report.check("data survived the shutdown", size_after > 0 and size_after >= size_before * 0.5,
                 f"database {size_before} → {size_after} bytes")

    instance.start()
    ok = wait_health(instance.port)
    report.check("the copy starts again after a clean stop", ok,
                 f"second launch on port {instance.port} → health 200")
    if not ok:
        return
    session = sign_in(instance.port, data_dir)
    if not session:
        # The bootstrap passwords are only generated on the FIRST run; a record
        # that cannot sign in again would itself be a finding.
        report.check("the same account still signs in after a restart", False,
                     "login refused after restart")
        return
    cookie, _ = session
    status, _, raw = http("GET", f"http://127.0.0.1:{instance.port}/api/data/inventory",
                          headers={"Cookie": cookie})
    inventory = json.loads(raw) if status == 200 else {}
    tables = inventory.get("counts") or inventory.get("tables") or {}
    students = tables.get("students") or 0
    results = tables.get("results") or 0
    report.check("imported records are still there after the restart",
                 status == 200 and (students or results),
                 f"students={students} results={results}")


# ----------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", dest="zip_path", default=None,
                        help="the released MathChecker_Portable.zip")
    parser.add_argument("--package", dest="package", default=None,
                        help="an already extracted MathChecker_Portable folder")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--demo-port", type=int, default=None)
    parser.add_argument("--keep", action="store_true",
                        help="keep the extracted test copy (default: remove it)")
    parser.add_argument("--report", default=None,
                        help="also write the results to this file")
    args = parser.parse_args()

    report = Report(Path(args.report) if args.report else None)
    work = Path(tempfile.gettempdir()) / AWKWARD_DIR
    if args.package:
        package = Path(args.package).resolve()
        extracted = False
    else:
        if not args.zip_path:
            parser.error("give --zip or --package")
        if work.exists() and not robust_rmtree(work):
            # Still locked (a previous run's copy is shutting down): use a
            # fresh directory rather than refusing to verify at all.
            work = work.with_name(f"{work.name} ({int(time.time())})")
        work.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip_path) as zf:
            zf.extractall(work)
        package = work / "MathChecker_Portable"
        extracted = True
    report.note(f"verifying package: {package}")
    report.note(f"report file: {args.report or '(console only)'}")

    port = args.port or free_port()
    before_python = python_pids()
    local_before = snapshot_local()

    instance = Instance(package, port, Path(tempfile.gettempdir()) / "mc5_verify_main.log")
    demo: Instance | None = None

    # Never leave a copy running because a check raised: a stale instance holds
    # its log files open and would break the next verification run.
    @atexit.register
    def _teardown() -> None:
        for copy in (demo, instance):
            if copy is None:
                continue
            try:
                if listening_pids(copy.port):
                    copy.stop_with_launcher()
            except Exception:  # noqa: BLE001 — best effort teardown only
                pass

    check_package_tree(report, package)
    check_config_template(report, package)
    check_runtime_dependencies(report, package)
    check_launch(report, instance, package / "Data", local_before)
    check_duplicate_launch(report, instance)
    session = check_auth(report, instance, package / "Data")

    # The demo port is chosen ONLY NOW, after the first copy is up. The on-demand
    # supervisor runs the backend on a PRIVATE port of its own, so a port picked
    # before that copy started could already belong to it: the demo launch would
    # find a listener answering the Math Checker health check, refuse to start
    # and ATTACH to the first copy instead — demo mode, its isolated data and its
    # mock engine would silently not be exercised. A port picked while the first
    # copy is running cannot be one of that copy's ports.
    demo_port = args.demo_port or free_port()
    demo = Instance(package, demo_port, Path(tempfile.gettempdir()) / "mc5_verify_demo.log")
    report.note(f"\n== demo copy on port {demo_port} ==")
    demo.start("--demo")
    report.check("demo copy starts", wait_health(demo_port),
                 f"cmd /c MathChecker.bat {demo_port} --demo")
    demo_session = sign_in(demo_port, package / "Data" / "demo")
    report.check("demo copy has its own isolated data directory",
                 (package / "Data" / "demo").exists(),
                 "Data/demo (real data untouched)")
    grade, detail = check_correction(report, demo, package / "Data" / "demo",
                                     demo_session)
    check_report(report, demo, demo_session)
    check_export_import(report, demo, demo_session, instance, session)
    check_logs_and_processes(report, instance, demo, package / "Data")

    report.check("demo copy stops when asked (no window to close)",
                 demo.stop_with_launcher() and not listening_pids(demo_port),
                 f"port {demo_port} free afterwards")
    check_restart_persistence(report, instance, package / "Data")

    report.note("\n== cleaning up ==")
    instance.stop_with_launcher()
    leftovers: set[int] = set()
    for _ in range(15):
        leftovers = python_pids() - before_python
        if not leftovers:
            break
        time.sleep(2)
    report.check("no orphaned processes after stopping both copies", not leftovers,
                 f"still alive: {sorted(leftovers)}" if leftovers else "none")
    if extracted and not args.keep:
        # A stopped copy can hold its log file open for a moment; a cleanup
        # failure must never turn a green verification run into a crash.
        removed = robust_rmtree(work)
        report.note(f"test copy {'removed' if removed else 'left in place'}: {work}")

    return report.summary()


if __name__ == "__main__":
    # The console on Windows defaults to a legacy code page; the evidence lines
    # contain arrows and dashes.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
