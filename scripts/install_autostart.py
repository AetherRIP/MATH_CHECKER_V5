"""Math Correct — automatic Windows startup installer.

Two mechanisms, chosen automatically (§11/§13):

1. ``service`` — the ROBUST path. Registers Windows Scheduled Tasks that run
   **at system start-up, as SYSTEM, with highest privileges**, so Math Correct
   starts with NO user sign-in and keeps running across logon/logoff. This
   needs administrator rights, so the installer re-launches itself through the
   normal **UAC** prompt (``Start-Process -Verb RunAs``) — it never bypasses or
   silently elevates.
2. ``all`` / ``backend`` / ``agent`` — the no-admin FALLBACK. Installs silent
   launchers into the current user's Startup folder (per-user, no UAC).

Both write the same silent crash-restart launchers, so a crash is recovered by
the launcher loop regardless of which mechanism started it. The loop is
deliberately NOT tight: a clean exit stops it, a crash retries with exponential
backoff (5s → 60s cap) and gives up after 60 consecutive failures, and the log
is rotated so it cannot grow without bound.

Startup management (duplicate / obsolete registration control):

    python scripts/install_autostart.py diagnose      # what starts, and why
    python scripts/install_autostart.py launchers     # rewrite the .bat files
    python scripts/install_autostart.py enable        # re-enable boot tasks
    python scripts/install_autostart.py disable       # pause boot tasks (UAC)
    python scripts/install_autostart.py cleanup-obsolete   # disable dead tasks (UAC)

Usage:
    python scripts/install_autostart.py service        # UAC -> boot tasks
    python scripts/install_autostart.py service --register-only   # internal
    python scripts/install_autostart.py service-status
    python scripts/install_autostart.py service-remove
    python scripts/install_autostart.py all            # no-admin fallback
    python scripts/install_autostart.py verify
    python scripts/install_autostart.py status
    python scripts/install_autostart.py remove

Verification (§14): ``service`` verifies the task exists, that it points at the
right launcher, and (optionally) that starting it makes the component healthy.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BACKEND_VBS = "MathCorrect Backend.vbs"
AGENT_VBS = "MathCorrect Agent.vbs"
TASK_BACKEND = "MathCorrectBackend"
TASK_AGENT = "MathCorrectAgent"

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(__file__).resolve()


# --------------------------------------------------------------- environment

def agent_available() -> tuple[bool, str, str]:
    """Is the Tiny Agent used by this version? ``(available, setting, hint)``.

    Read from the APPLICATION itself (``app.distributed.agent_control``) so the
    installer and the running app can never disagree about it. An import
    failure means "no app to ask" — the conservative answer is that the agent is
    NOT used, because generating an auto-start entry is exactly how a disabled
    agent would come back after a restart (see the simplification work, §1).
    """
    try:
        sys.path.insert(0, str(BACKEND_ROOT / "backend"))
        from app.distributed import agent_control
        return (bool(agent_control.product_enabled()),
                f"[{agent_control.CONFIG_SECTION}] {agent_control.CONFIG_KEY}",
                agent_control.REENABLE_HINT)
    except Exception as exc:  # noqa: BLE001 — no app tree: stay disabled
        return False, "(unavailable)", f"could not read the setting ({exc})"


def is_elevated() -> bool:
    """True when this process already has administrator rights."""
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def resolve_python(root: Path) -> str:
    """Resolve the interpreter exactly like start.bat: bundled runtime, then a
    project venv, then a system Python."""
    for candidate in (root / "runtime" / "python.exe",
                      root / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    for probe in (["py", "-3"], ["python"]):
        try:
            exe = subprocess.run(
                probe + ["-c", "import sys;print(sys.executable)"],
                capture_output=True, text=True, check=True).stdout.strip()
            if exe:
                return exe
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            continue
    raise SystemExit(
        "Python was not found. Run setup.bat once, or install Python 3.12+ "
        "and tick 'Add Python to PATH'.")


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("APPDATA is not set — this installer is for Windows.")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


# ----------------------------------------------------------------- launchers

def _write_backend_launcher(root: Path, python: str, out_dir: Path) -> Path:
    """Write the backend launcher.

    Deliberately does NOT force ``MC5_DATA_DIR`` except for a portable install:
    the app's own default is ``<project>/data`` (independent of the user
    profile), so the boot task as SYSTEM uses the SAME data directory as an
    interactive run. Overriding it here would point a reboot at an empty
    database and look like data loss.

    Restart policy (critical for §1): ``main.py`` exits 0 when another healthy
    Math Correct instance already owns the port (single-instance guard), so the
    loop STOPS instead of fighting the official application for port 8642.
    Only genuine crashes retry, with exponential backoff, and a bounded number
    of consecutive failures ends the loop with a logged reason.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "autostart_backend.log"
    bat = out_dir / "mathcorrect_backend_autostart.bat"
    data_line = ""
    if (root / "portable.flag").exists():
        data_line = f'set "MC5_DATA_DIR={root / "data"}"\r\n'
    bat.write_text(
        "@echo off\r\n"
        "rem Auto-generated by scripts/install_autostart.py.\r\n"
        "rem Backend launcher: exit 0 stops the loop (instance already\r\n"
        "rem running / clean shutdown); crashes retry with backoff.\r\n"
        "setlocal\r\n"
        f'cd /d "{root}"\r\n'
        'set "PYTHONDONTWRITEBYTECODE=1"\r\n'
        'set "MC5_AUTOSTART=1"\r\n'
        'set "MC5_NO_BROWSER=1"\r\n'
        f"{data_line}"
        "set /a FAILS=0\r\n"
        "set /a DELAY=5\r\n"
        ":loop\r\n"
        f'"{python}" main.py >> "{log}" 2>&1\r\n'
        "if not errorlevel 1 exit /b 0\r\n"
        "set /a FAILS+=1\r\n"
        "if %FAILS% GEQ 60 goto giveup\r\n"
        "set /a DELAY=DELAY*2\r\n"
        "if %DELAY% GTR 60 set DELAY=60\r\n"
        "set /a MOD=FAILS %% 10\r\n"
        'if %MOD%==0 move /y "' + str(log) + '" "' + str(log) + '.1" >nul 2>&1\r\n'
        "ping -n %DELAY% 127.0.0.1 >nul\r\n"
        "goto loop\r\n"
        ":giveup\r\n"
        f'echo [%date% %time%] launcher gave up after %FAILS% consecutive failures >> "{log}"\r\n'
        "exit /b 1\r\n",
        encoding="utf-8", newline="")
    return bat


def _write_agent_launcher(agent_dir: Path, python: str, backend_url: str,
                          out_dir: Path) -> Path:
    """Write the tiny-agent launcher (never starts a backend — §1)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "autostart_agent.log"
    bat = out_dir / "mathcorrect_agent_autostart.bat"
    bat.write_text(
        "@echo off\r\n"
        "rem Auto-generated by scripts/install_autostart.py.\r\n"
        "rem Tiny agent only (heartbeat/events). It NEVER starts the backend.\r\n"
        "setlocal\r\n"
        f'cd /d "{agent_dir}"\r\n'
        "set /a FAILS=0\r\n"
        "set /a DELAY=5\r\n"
        ":loop\r\n"
        f'"{python}" "{agent_dir / "agent.py"}" --backend {backend_url} '
        f'>> "{log}" 2>&1\r\n'
        "if not errorlevel 1 exit /b 0\r\n"
        "set /a FAILS+=1\r\n"
        "if %FAILS% GEQ 60 goto giveup\r\n"
        "set /a DELAY=DELAY*2\r\n"
        "if %DELAY% GTR 60 set DELAY=60\r\n"
        "set /a MOD=FAILS %% 10\r\n"
        'if %MOD%==0 move /y "' + str(log) + '" "' + str(log) + '.1" >nul 2>&1\r\n'
        "ping -n %DELAY% 127.0.0.1 >nul\r\n"
        "goto loop\r\n"
        ":giveup\r\n"
        f'echo [%date% %time%] launcher gave up after %FAILS% consecutive failures >> "{log}"\r\n'
        "exit /b 1\r\n",
        encoding="utf-8", newline="")
    return bat


def _write_vbs(bat: Path, out_dir: Path) -> Path:
    vbs = out_dir / (bat.stem + ".vbs")
    vbs.write_text(
        'CreateObject("Wscript.Shell").Run "cmd /c ""' + str(bat) + '""", 0, False\r\n',
        encoding="utf-8")
    return vbs


def _install_into_startup(vbs: Path, name: str) -> Path:
    target_dir = startup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    target.write_text(vbs.read_text(encoding="utf-8"), encoding="utf-8")
    return target


# ------------------------------------------------------------------- tasks

def _schtasks(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True)


def _create_task(task: str, bat: Path) -> tuple[bool, str]:
    """Create a boot-time SYSTEM task pointing at the launcher batch file.

    ``/SC ONSTART`` runs it at system start-up (no sign-in required);
    ``/RU SYSTEM /RL HIGHEST`` gives it the privileges a service needs.
    """
    result = _schtasks([
        "/Create", "/TN", task, "/SC", "ONSTART", "/RU", "SYSTEM",
        "/RL", "HIGHEST", "/TR", f'"{bat}"', "/F"])
    return result.returncode == 0, (result.stdout or result.stderr or "").strip()


def _delete_task(task: str) -> tuple[bool, str]:
    result = _schtasks(["/Delete", "/TN", task, "/F"])
    return result.returncode == 0, (result.stdout or result.stderr or "").strip()


def _query_task(task: str) -> tuple[bool, str]:
    result = _schtasks(["/Query", "/TN", task, "/V", "/FO", "LIST"])
    return result.returncode == 0, (result.stdout or result.stderr or "").strip()


def _run_task(task: str) -> tuple[bool, str]:
    result = _schtasks(["/Run", "/TN", task])
    return result.returncode == 0, (result.stdout or result.stderr or "").strip()


# ------------------------------------------------------------------- service

def _marker_path(root: Path) -> Path:
    return root / "autostart" / "service_install.json"


def _log(msg: str, logpath: Path | None = None) -> None:
    print(msg)
    if logpath:
        try:
            with logpath.open("a", encoding="utf-8") as fh:
                fh.write(msg + "\n")
        except OSError:
            pass


def _disable_task(task: str) -> None:
    """Disable (never delete) a task; missing tasks are fine."""
    ok, detail = _query_task(task)
    if not ok:
        return
    result = _schtasks(["/Change", "/TN", task, "/DISABLE"])
    print(f"[service] {task}: disabled (was starting the Tiny Agent) "
          f"{(result.stdout or result.stderr or '').strip()[:80]}")


def _remove_agent_startup_entries() -> None:
    """Remove the agent's per-user Startup shortcut (safe when absent)."""
    try:
        shortcut = startup_dir() / AGENT_VBS
        if shortcut.exists():
            shortcut.unlink()
            print(f"[agent] removed stale auto-start shortcut: {shortcut.name}")
    except (OSError, SystemExit):
        pass


def register_service(root: Path, agent_dir: Path, backend_url: str,
                     *, logpath: Path | None = None,
                     force_agent: bool = False) -> dict:
    """(Elevated) register both boot tasks and VERIFY them (§14)."""
    out = root / "autostart"
    python = resolve_python(root)
    backend_bat = _write_backend_launcher(root, python, out)
    agent_bat = _write_agent_launcher(agent_dir, python, backend_url, out)

    results: dict = {"tasks": {}, "verify": {}, "ok": True}
    available, _setting, _hint = agent_available()
    pairs = [(TASK_BACKEND, backend_bat)]
    if available or force_agent:
        pairs.append((TASK_AGENT, agent_bat))
    else:
        # A boot task for an agent this version does not run would be a silent
        # resurrection path: it is DISABLED (never deleted — the task can be
        # re-enabled with `install_autostart.py enable`) and the Startup
        # shortcut removed, so nothing starts it after a reboot or update.
        print("[service] Tiny Agent is not used by this version — installing "
              "the application task only.")
        _disable_task(TASK_AGENT)
        _remove_agent_startup_entries()
    for task, bat in pairs:
        ok, detail = _create_task(task, bat)
        _log(f"[service] create {task}: {'OK' if ok else 'FAILED'} {detail[:120]}",
             logpath)
        results["tasks"][task] = {"created": ok, "launcher": str(bat)}

        q_ok, q_detail = _query_task(task)
        points_ok = bat.name in q_detail
        fields = {line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
                  for line in q_detail.splitlines() if ":" in line}
        results["verify"][task] = {
            "query_ok": q_ok, "points_at_launcher": points_ok,
            "run_as": fields.get("run as user"),
            "schedule": fields.get("schedule type"),
        }
        _log(f"[service] verify {task}: exists={q_ok} "
             f"correct_target={points_ok} run_as={fields.get('run as user')} "
             f"schedule={fields.get('schedule type')}", logpath)
        if not (ok and q_ok and points_ok):
            results["ok"] = False

    if results["ok"]:
        # The boot tasks now own startup: remove the per-user Startup
        # shortcuts so there is exactly ONE mechanism (no duplicate processes).
        try:
            for name in (BACKEND_VBS, AGENT_VBS):
                shortcut = startup_dir() / name
                if shortcut.exists():
                    shortcut.unlink()
                    _log(f"[service] removed legacy Startup shortcut {name}",
                         logpath)
        except (OSError, SystemExit):
            pass

    results["elevated"] = True
    results["marker"] = str(_marker_path(root))
    _marker_path(root).parent.mkdir(parents=True, exist_ok=True)
    _marker_path(root).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def _elevate(args: list[str]) -> int:
    """Re-launch this installer through the normal UAC prompt (§12).

    Uses ``Start-Process -Verb RunAs``, Windows' standard elevation mechanism.
    It does NOT bypass UAC or run silently.
    """
    quoted = ", ".join("'" + a.replace("'", "''") + "'" for a in args)
    ps = (f"Start-Process -FilePath '{sys.executable}' "
          f"-ArgumentList @({quoted}) -Verb RunAs -Wait")
    print("[service] administrator rights are required — requesting UAC ...")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])
    return result.returncode


def service(root: Path, agent_dir: Path, backend_url: str,
            register_only: bool, force_agent: bool = False) -> int:
    if not is_elevated():
        marker = _marker_path(root)
        if register_only:
            print("[service] --register-only was given but this process is not "
                  "elevated; refusing to continue.")
            return 1
        if marker.exists():
            marker.unlink()
        args = [str(SCRIPT), "service", "--register-only",
                "--root", str(root), "--dir", str(agent_dir),
                "--backend", backend_url]
        if force_agent:
            args.append("--force-agent")
        rc = _elevate(args)
        if marker.exists():
            results = json.loads(marker.read_text(encoding="utf-8"))
            print(json.dumps(results, indent=2))
            return 0 if results.get("ok") else 1
        print("[service] elevation did not complete (UAC declined or failed), "
              f"exit code {rc}. No startup task was created.")
        return 1
    results = register_service(root, agent_dir, backend_url,
                               force_agent=force_agent)
    return 0 if results.get("ok") else 1


def _marker_results(root: Path) -> dict | None:
    marker = _marker_path(root)
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def service_status(root: Path = BACKEND_ROOT) -> int:
    """Report the boot-task state.

    A LIVE query of a SYSTEM task is itself privileged: when it is denied we
    fall back to the install-time verification recorded in the marker, so an
    ordinary user never sees a wrongly-healthy "NOT registered".
    """
    rc = 0
    marker = _marker_results(root) or {}
    for task in (TASK_BACKEND, TASK_AGENT):
        ok, detail = _query_task(task)
        if ok:
            fields = {line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
                      for line in detail.splitlines() if ":" in line}
            print(f"[{task}] registered | status={fields.get('Status','?')} "
                  f"| run-as={fields.get('Run As User','?')} "
                  f"| schedule={fields.get('Schedule Type','?')}")
            continue
        denied = "denied" in detail.lower()
        recorded = (marker.get("verify") or {}).get(task)
        if denied and recorded and recorded.get("query_ok"):
            print(f"[{task}] registered at install time (live query needs "
                  "administrator rights) | target_verified="
                  f"{recorded.get('points_at_launcher')}")
            continue
        print(f"[{task}] NOT registered")
        rc = 1
    return rc


def service_remove() -> int:
    rc = 0
    for task in (TASK_BACKEND, TASK_AGENT):
        if not is_elevated():
            print(f"[{task}] removing requires administrator rights — "
                  "re-run with the UAC prompt.")
            return 1
        ok, detail = _delete_task(task)
        print(f"[{task}] {'removed' if ok else detail[:100]}")
        if not ok and "cannot find" not in detail.lower():
            rc = 1
    return rc


# -------------------------------------------------------- no-admin fallback

def install_backend(root: Path) -> int:
    python = resolve_python(root)
    out = root / "autostart"
    bat = _write_backend_launcher(root, python, out)
    vbs = _write_vbs(bat, out)
    target = _install_into_startup(vbs, BACKEND_VBS)
    print(f"[backend] launcher : {bat}")
    print(f"[backend] auto-start installed: {target}")
    return 0


def install_agent(agent_dir: Path, backend_url: str,
                  *, force: bool = False) -> int:
    """Install the agent auto-start — REFUSED while the agent is disabled.

    This is the specific case the simplification work calls out: the installer
    must not recreate the agent's background start-up behind the user's back.
    """
    available, setting, hint = agent_available()
    if not available and not force:
        print("[agent] the Tiny Agent is NOT used by this version "
              f"({setting}), so no auto-start entry was created.")
        print(f"        {hint}")
        _remove_agent_startup_entries()
        return 0
    agent_dir = agent_dir.resolve()
    if not (agent_dir / "agent.py").exists():
        print(f"agent.py was not found in {agent_dir}")
        return 1
    python = resolve_python(BACKEND_ROOT)
    out = BACKEND_ROOT / "autostart"
    bat = _write_agent_launcher(agent_dir, python, backend_url, out)
    vbs = _write_vbs(bat, out)
    target = _install_into_startup(vbs, AGENT_VBS)
    print(f"[agent] launcher : {bat}")
    print(f"[agent] auto-start installed: {target}")
    return 0


def status() -> int:
    """Report the auto-start entries. A missing AGENT entry is EXPECTED here."""
    target_dir = startup_dir()
    available, setting, _hint = agent_available()
    rc = 0
    for name in (BACKEND_VBS, AGENT_VBS):
        path = target_dir / name
        if path.exists():
            print(f"[{name}] installed ({path})")
            if name == AGENT_VBS and not available:
                print(f"[{name}]   note: the Tiny Agent is not used by this "
                      f"version ({setting}) — this entry is left over and can "
                      "be removed with 'remove agent'")
        elif name == AGENT_VBS and not available:
            print(f"[{name}] not installed (expected: the Tiny Agent is not "
                  f"used by this version — {setting})")
        else:
            print(f"[{name}] NOT installed")
            rc = 1
    return rc


def remove(which: str) -> int:
    target_dir = startup_dir()
    names = []
    if which in ("all", "backend"):
        names.append(BACKEND_VBS)
    if which in ("all", "agent"):
        names.append(AGENT_VBS)
    for name in names:
        path = target_dir / name
        if path.exists():
            path.unlink()
            print(f"[{name}] removed")
        else:
            print(f"[{name}] not present")
    return 0


# ------------------------------------------------- diagnose / manage (§1)

#: Action tokens that identify a task's target executable/script.
_PATH_RE = re.compile(r'"?([A-Za-z]:\\[^"\r\n]+?\.(?:exe|bat|cmd|ps1|py|vbs))"?')


def _iter_tasks():
    """Yield (name, state, [target paths]) for every visible scheduled task.

    Prefers the Task Scheduler COM API (complete, sees SYSTEM tasks); falls
    back to parsing ``schtasks`` output when COM is unavailable.
    """
    try:
        from win32com.client import DISPATCH  # type: ignore
        service = DISPATCH("Schedule.Service")
        service.Connect()
        folder = service.GetFolder("\\")
        return _iter_tasks_com(folder)
    except Exception:  # noqa: BLE001 — fall back to schtasks parsing
        return _iter_tasks_schtasks()


def _iter_tasks_com(folder):
    out = []
    def walk(f):
        for t in f.GetTasks(0):
            xml = t.Xml
            actions = []
            for match in _PATH_RE.finditer(xml):
                actions.append(match.group(1))
            out.append((t.Path, t.State, actions))
        for sub in f.GetFolders(0):
            walk(sub)
    walk(folder)
    return out


def _iter_tasks_schtasks():
    """Portable fallback: parse ``schtasks /query /fo csv`` (no COM, no admin)."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/V"],
            capture_output=True, check=True)
    except (subprocess.CalledProcessError, OSError):
        return []
    raw = result.stdout
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    name_i = header.index("taskname") if "taskname" in header else 0
    torun_i = header.index("task to run") if "task to run" in header else None
    state_i = header.index("status") if "status" in header else None
    out = []
    for row in rows[1:]:
        if len(row) <= max(name_i, state_i or 0):
            continue
        actions = []
        if torun_i is not None and len(row) > torun_i:
            for match in _PATH_RE.finditer(row[torun_i]):
                actions.append(match.group(1))
        out.append((row[name_i], row[state_i] if state_i is not None else "",
                    actions))
    return out


def diagnose(root: Path, agent_dir: Path, backend_url: str) -> int:
    """Report every startup registration, its purpose and its health (§1).

    Read-only. It answers three questions without changing anything:
      * what starts Math Correct automatically, and from where;
      * is the running backend/agent a single instance or a duplicate;
      * which registrations point at files that no longer exist (obsolete).
    """
    rc = 0
    print("=== Math Correct startup diagnosis")
    out = root / "autostart"
    available, setting, _hint = agent_available()
    entries = [("backend", out / "mathcorrect_backend_autostart.bat",
                root / "main.py")]
    if available:
        entries.append(("agent", out / "mathcorrect_agent_autostart.bat",
                        agent_dir / "agent.py"))
    else:
        print(f"[agent] not used by this version ({setting}) — its launcher is "
              "optional and an auto-start entry for it is NOT wanted")
    for label, bat, entry in entries:
        if not bat.exists():
            print(f"[{label}] launcher MISSING: {bat} (run 'launchers' to recreate)")
            rc = 1
            continue
        body = bat.read_text(encoding="utf-8", errors="replace")
        old_loop = "goto loop" in body and "ping -n 16" in body
        print(f"[{label}] launcher: {bat.name} -> {entry.name} "
              f"({'OBSOLETE tight-loop version — regenerate with launchers'
                 if old_loop else 'current version'})")
        if not entry.exists():
            print(f"[{label}] ENTRY POINT MISSING: {entry}")
            rc = 1
        python_in_bat = re.search(r'"([A-Za-z]:\\[^"\r\n]+python\.exe)"', body)
        if python_in_bat and not Path(python_in_bat.group(1)).exists():
            print(f"[{label}] interpreter in launcher no longer exists: "
                  f"{python_in_bat.group(1)} (run 'launchers' to repair)")
            rc = 1
        if old_loop:
            rc = 1

    print("=== scheduled tasks")
    tasks = _iter_tasks()
    ours = [t for t in tasks if "mathcorrect" in t[0].lower()]
    if not ours:
        print("  (no MathCorrect* task visible to this user — a live query of "
              "SYSTEM tasks needs administrator rights; try 'service-status')")
    for name, state, actions in ours:
        print(f"  {name} [{state}] -> {actions or '(unparsed)'}")
        for action in actions:
            if not Path(action).exists():
                print(f"     TARGET MISSING: {action}")
                rc = 1

    print("=== obsolete registrations (target file no longer exists)")
    obsolete = []
    for name, state, actions in tasks:
        lower = name.lower()
        if lower.startswith("\\microsoft\\") or lower.startswith("microsoft\\"):
            continue
        for action in actions:
            if not Path(action).exists():
                obsolete.append((name, action))
    if not obsolete:
        print("  none found")
    for name, action in obsolete:
        print(f"  {name}\n      missing target: {action}")
        print("      -> safe to disable with 'cleanup-obsolete' (nothing is deleted)")
        rc = 1

    print("=== startup folder")
    try:
        entries = [p.name for p in startup_dir().glob("*")
                   if p.name.lower() != "desktop.ini"]
        print("  " + (", ".join(entries) if entries else "empty"))
    except SystemExit as exc:
        print(f"  unavailable: {exc}")

    print("=== running instances")
    import urllib.request
    try:
        with urllib.request.urlopen(backend_url.rstrip("/") + "/api/health",
                                    timeout=2) as resp:
            print(f"  backend on {backend_url}: healthy (HTTP {resp.status})")
    except Exception as exc:  # noqa: BLE001
        print(f"  backend on {backend_url}: not answering ({exc})")
    record = root / "data" / "instance.json"
    if record.exists():
        try:
            info = json.loads(record.read_text(encoding="utf-8"))
            alive = _pid_alive(int(info.get("pid") or 0))
            print(f"  instance record: pid={info.get('pid')} "
                  f"port={info.get('port')} "
                  f"{'alive' if alive else 'STALE (crashed instance)'}")
        except (OSError, ValueError):
            print("  instance record: unreadable")

    print("=== RESULT:", "OK" if rc == 0 else "ACTION NEEDED (see above)")
    return rc


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                      False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong(0)
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def launchers(root: Path, agent_dir: Path, backend_url: str) -> int:
    """Rewrite the .bat/.vbs launchers in place (no admin required).

    The scheduled tasks already point at these file paths, so regenerating
    them updates the boot behaviour without touching the tasks at all.
    """
    python = resolve_python(root)
    out = root / "autostart"
    backend_bat = _write_backend_launcher(root, python, out)
    agent_bat = _write_agent_launcher(agent_dir, python, backend_url, out)
    for bat in (backend_bat, agent_bat):
        _write_vbs(bat, out)
    print(f"[launchers] rewritten: {backend_bat.name}, {agent_bat.name}")
    print(f"[launchers] interpreter: {python}")
    return 0


def _set_tasks_disabled(root: Path, disabled: bool, tasks: list[str]) -> int:
    """Enable/disable tasks WITHOUT deleting them (safe, reversible)."""
    if not is_elevated():
        action = "disable" if disabled else "enable"
        marker = _marker_path(root)
        if marker.exists():
            marker.unlink()
        args = [str(SCRIPT), "service-set",
                "--state", "disabled" if disabled else "enabled",
                "--root", str(root), "--dir", str(root / "agent")]
        _elevate(args)
        if marker.exists():
            try:
                results = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                results = {}
            print(f"[tasks] {action}: " + ("OK" if results.get("ok") else "FAILED"))
            return 0 if results.get("ok") else 1
        print(f"[tasks] {action}: elevation declined or failed — nothing changed.")
        return 1

    results = {"ok": True, "tasks": {}}
    flag = "/DISABLE" if disabled else "/ENABLE"
    for task in tasks:
        result = _schtasks(["/Change", "/TN", task, flag])
        detail = (result.stdout or result.stderr or "").strip()
        missing = "cannot find" in detail.lower() or "cannot find" in (result.stdout or "").lower()
        ok = result.returncode == 0 or missing
        results["tasks"][task] = {"ok": ok, "detail": detail[:200],
                                   "existed": not missing}
        results["ok"] = results["ok"] and ok
        state = "missing" if missing else ("disabled" if disabled else "enabled")
        print(f"[tasks] {task}: {state}")
    _marker_path(root).parent.mkdir(parents=True, exist_ok=True)
    _marker_path(root).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if results["ok"] else 1


def cleanup_obsolete(root: Path) -> int:
    """Disable (never delete) scheduled tasks whose target no longer exists.

    Each finding is printed with the missing path so the owner can recognise
    which application it belonged to before anything is changed.
    """
    if not is_elevated():
        marker = _marker_path(root)
        if marker.exists():
            marker.unlink()
        _elevate([str(SCRIPT), "cleanup-obsolete", "--root", str(root),
                  "--dir", str(root / "agent")])
        if marker.exists():
            try:
                results = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                results = {}
            print(json.dumps(results, indent=2))
            return 0 if results.get("ok") else 1
        print("[cleanup] elevation declined or failed — nothing changed.")
        return 1

    results = {"ok": True, "disabled": []}
    for name, state, actions in _iter_tasks():
        if name.lower().startswith(("\\microsoft\\", "microsoft\\")):
            continue
        missing = [a for a in actions if not Path(a).exists()]
        if not missing:
            continue
        print(f"[cleanup] {name}: target missing ({missing[0]})")
        # Keep MathCorrect tasks untouched here — they are repaired, not
        # disabled, by 'service'/'launchers'.
        if "mathcorrect" in name.lower():
            print("[cleanup]   (Math Correct task — use 'service' to repair)")
            continue
        result = _schtasks(["/Change", "/TN", name, "/DISABLE"])
        detail = (result.stdout or result.stderr or "").strip()
        ok = result.returncode == 0
        results["disabled"].append({"task": name, "ok": ok,
                                    "detail": detail[:200],
                                    "missing_target": missing[0]})
        results["ok"] = results["ok"] and ok
        print(f"[cleanup]   -> {'disabled' if ok else 'FAILED: ' + detail[:120]} "
              "(not deleted — re-enable with 'enable')")
    if not results["disabled"]:
        print("[cleanup] no obsolete tasks found")
    _marker_path(root).parent.mkdir(parents=True, exist_ok=True)
    _marker_path(root).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if results["ok"] else 1


def verify(root: Path, agent_dir: Path, backend_url: str) -> int:
    """§14: prove the startup setup is real, not assumed."""
    ok = True
    out = root / "autostart"
    backend_bat = out / "mathcorrect_backend_autostart.bat"
    agent_bat = out / "mathcorrect_agent_autostart.bat"
    try:
        python = resolve_python(root)
        print(f"[verify] interpreter: {python}")
    except SystemExit as exc:
        print(f"[verify] interpreter: NOT FOUND ({exc})")
        return 1
    available, setting, _hint = agent_available()
    expected = [(backend_bat, root / "main.py")]
    if available:
        expected.append((agent_bat, agent_dir / "agent.py"))
    else:
        print(f"[verify] Tiny Agent entries are not expected: not used by this "
              f"version ({setting})")
    for bat, entry in expected:
        if not bat.exists():
            print(f"[verify] {bat.name}: MISSING")
            ok = False
            continue
        body = bat.read_text(encoding="utf-8", errors="replace")
        if not entry.exists():
            print(f"[verify] {entry}: MISSING (launcher would fail)")
            ok = False
        elif entry.name not in body:
            print(f"[verify] {bat.name}: does not reference {entry.name}")
            ok = False
        else:
            print(f"[verify] {bat.name}: OK (runs {entry.name})")

    # Service tasks (boot, no sign-in) count; the Startup folder is a fallback.
    have_service = False
    required_tasks = ((TASK_BACKEND, TASK_AGENT) if available else (TASK_BACKEND,))
    for task in required_tasks:
        q_ok, q_detail = _query_task(task)
        if q_ok:
            have_service = True
            continue
        recorded = (_marker_results(root) or {}).get("verify", {}).get(task) or {}
        if "denied" in q_detail.lower() and recorded.get("query_ok"):
            have_service = True
    if have_service:
        print("[verify] startup: boot service task registered")
    else:
        target_dir = startup_dir()
        wanted = ((BACKEND_VBS, AGENT_VBS) if available else (BACKEND_VBS,))
        for name in wanted:
            if (target_dir / name).exists():
                print(f"[verify] startup: {name} installed (per-user fallback)")
            else:
                print(f"[verify] startup: {name} MISSING and no boot task")
                ok = False

    try:
        import urllib.request
        with urllib.request.urlopen(backend_url.rstrip("/") + "/api/health",
                                    timeout=3) as resp:
            print(f"[verify] backend health: {resp.status}")
            ok = ok and resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] backend health: not reachable yet ({exc}) — "
              "expected before the first start")
    print("[verify] RESULT:", "OK - startup is correctly configured" if ok
          else "INCOMPLETE - see the lines marked MISSING above")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Math Correct auto-start installer")
    parser.add_argument("mode", choices=["backend", "agent", "all", "status",
                                         "verify", "remove", "service",
                                         "service-status", "service-remove",
                                         "diagnose", "launchers", "enable",
                                         "disable", "service-set",
                                         "cleanup-obsolete"])
    parser.add_argument("--root", default=str(BACKEND_ROOT))
    parser.add_argument("--dir", default=str(BACKEND_ROOT / "agent"))
    parser.add_argument("--backend", default="http://127.0.0.1:8642")
    parser.add_argument("--which", default="all",
                        choices=["all", "backend", "agent"])
    parser.add_argument("--state", default="disabled",
                        choices=["enabled", "disabled"])
    parser.add_argument("--register-only", action="store_true",
                        help="internal: run the privileged registration directly")
    parser.add_argument("--force-agent", action="store_true",
                        help="install the Tiny Agent auto-start even though this "
                             "version disables it (only for the documented "
                             "re-enable path; see DISTRIBUTED_DEPLOYMENT.md)")
    args = parser.parse_args()

    if args.mode == "backend":
        return install_backend(Path(args.root))
    if args.mode == "agent":
        return install_agent(Path(args.dir), args.backend,
                             force=args.force_agent)
    if args.mode == "all":
        rc = install_backend(Path(args.root))
        rc |= install_agent(Path(args.dir), args.backend,
                            force=args.force_agent)
        return rc
    if args.mode == "status":
        return status()
    if args.mode == "verify":
        return verify(Path(args.root), Path(args.dir), args.backend)
    if args.mode == "service":
        return service(Path(args.root), Path(args.dir), args.backend,
                       args.register_only, args.force_agent)
    if args.mode == "service-status":
        return service_status(Path(args.root))
    if args.mode == "service-remove":
        return service_remove()
    if args.mode == "diagnose":
        return diagnose(Path(args.root), Path(args.dir), args.backend)
    if args.mode == "launchers":
        return launchers(Path(args.root), Path(args.dir), args.backend)
    if args.mode == "enable":
        return _set_tasks_disabled(Path(args.root), False,
                                   [TASK_BACKEND, TASK_AGENT])
    if args.mode == "disable":
        return _set_tasks_disabled(Path(args.root), True,
                                   [TASK_BACKEND, TASK_AGENT])
    if args.mode == "service-set":
        # internal: elevated half of enable/disable
        disabled = args.state == "disabled"
        results = {"ok": True, "tasks": {}}
        flag = "/DISABLE" if disabled else "/ENABLE"
        for task in (TASK_BACKEND, TASK_AGENT):
            result = _schtasks(["/Change", "/TN", task, flag])
            detail = (result.stdout or result.stderr or "").strip()
            missing = "cannot find" in detail.lower()
            ok = result.returncode == 0 or missing
            results["tasks"][task] = {"ok": ok, "detail": detail[:200]}
            results["ok"] = results["ok"] and ok
            print(f"[tasks] {task}: " +
                  ("missing" if missing else
                   ("disabled" if disabled else "enabled")))
        marker = _marker_path(Path(args.root))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(results, indent=2), encoding="utf-8")
        return 0 if results["ok"] else 1
    if args.mode == "cleanup-obsolete":
        return cleanup_obsolete(Path(args.root))
    return remove(args.which)


if __name__ == "__main__":
    raise SystemExit(main())
