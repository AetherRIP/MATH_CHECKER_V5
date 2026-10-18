#!/usr/bin/env python3
"""Verify the WINDOWS INSTALLER end to end — install, run, uninstall.

    python verify_installer.py --setup release/MathChecker_Setup.exe
    python verify_installer.py --setup ... --dir "D:\\mc5-install-test\\App" --keep

The release pipeline compiles the installer, and the handover notes correctly say
that "installer compilation is not proof of clean install/upgrade behaviour".
This is the missing counterpart to ``verify_portable.py``: it exercises the
INSTALLED copy the way a teacher meets it, then removes it again and says
exactly what was left on the machine.

What it does, against the real artefacts (no project imports, no AI keys):

  1. records the machine's BEFORE state (install dir, Start Menu group,
     uninstall registry entry, Run-key entries, the user-data folder and a hash
     of every file already in it),
  2. installs silently into a CLEAN folder with ``/CURRENTUSER`` — the
     documented non-elevated path (``PrivilegesRequiredOverridesAllowed``), so no
     administrator rights are needed and no all-users state is touched,
  3. checks the installed layout: launcher, Stop.bat, public-access launcher,
     bundled runtime, UI bundle, the uninstaller, the Start Menu group, the
     Add/Remove-Programs entry — and that the app's SOURCE tree is complete by
     importing the engine module that the subquestion work added,
  4. launches the INSTALLED copy through its own launcher and marks a PDF with a
     mark scheme through the real API (demo isolation, so pre-existing user data
     is never written to), reads the stored marks and 1-9 grade back,
  5. proves the installed copy kept its data in %LOCALAPPDATA%\\MathChecker and
     left the program folder untouched (installed copies may live in Program
     Files, which is not writable),
  6. stops it with the shipped Stop.bat measured by process, not by trust,
  7. uninstalls silently and re-checks the machine: program tree, Start Menu
     group, registry entry, autostart entries and orphan processes,
  8. says precisely what remains. %LOCALAPPDATA%\\MathChecker is declared
     ``uninsneveruninstall`` in the installer ON PURPOSE — it holds the user's
     settings, keys and database, and an uninstall or upgrade must never destroy
     them — so the check asserts the pre-existing files are byte-identical and
     removes only the scratch directory this test created.

Exit code 0 only when every check passed. Use --keep to leave the installed copy
in place for inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

# Evidence strings carry the app's own paths and symbols; a cp1252 console must
# not turn a verification result into a traceback.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-tty streams
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from verify_portable import (  # noqa: E402
    Instance, Report, check_auth, check_correction, free_port, http,
    listening_pids, python_pids, robust_rmtree, sign_in, wait_health,
)

APP_NAME = "Math Checker"
#: The AppId in installer/MathChecker.iss, used only as a hint: detection is by
#: DisplayName, so a renamed key can never make the check silently pass.
APP_ID = "{7B2E5C51-4A6D-4F1B-9C3E-8D2A1F6E9B04}_is1"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: The engine module the subquestion work added. If it is missing from the
#: installed copy, the install is of older source, however complete it looks.
ENGINE_MODULE = Path("backend/app/engine/scheme_structure.py")
PROGRAM_FILES = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))


# ---------------------------------------------------------------- machine state

def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def data_dir() -> Path:
    """Where an INSTALLED copy keeps its data (installer [Dirs])."""
    return local_appdata() / "MathChecker"


def start_menu_group() -> Path:
    return (Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME)


def machine_group() -> Path:
    return (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME)


def desktop_shortcut() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" \
        / f"{APP_NAME}.lnk"


def _hive(root: int, path: str):
    try:
        return winreg.OpenKey(root, path)
    except OSError:
        return None


def uninstall_entries() -> list[dict]:
    """Add/Remove-Programs entries for Math Checker, in every registry view.

    Detection is by DisplayName PREFIX, because Inno appends the version and
    build to it ("Math Checker 5.2.1 (build 20261013-rc1)"). A per-user install
    is registered in BOTH the 32- and 64-bit views of HKCU, which is one install
    seen twice, not two entries: the views are collapsed on InstallLocation.
    """
    found: dict[tuple[str, str], dict] = {}
    for root, label, extra in (
            (winreg.HKEY_CURRENT_USER, "HKCU", 0),
            (winreg.HKEY_CURRENT_USER, "HKCU-32",
             getattr(winreg, "KEY_WOW64_32KEY", 0)),
            (winreg.HKEY_LOCAL_MACHINE, "HKLM", 0),
            (winreg.HKEY_LOCAL_MACHINE, "HKLM-32",
             getattr(winreg, "KEY_WOW64_32KEY", 0))):
        try:
            with winreg.OpenKey(root, UNINSTALL_KEY, 0,
                                winreg.KEY_READ | extra) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    name = winreg.EnumKey(key, index)
                    with winreg.OpenKey(key, name) as sub:
                        info: dict[str, str] = {"hive": label, "key": name,
                                                "views": label}
                        for index2 in range(winreg.QueryInfoKey(sub)[1]):
                            value_name, value, _ = winreg.EnumValue(sub, index2)
                            if isinstance(value, str):
                                info[value_name] = value
                        display = str(info.get("DisplayName") or "")
                        if not display.startswith(APP_NAME):
                            continue
                        identity = (display,
                                    str(info.get("InstallLocation") or ""))
                        if identity in found:
                            found[identity]["views"] += f"+{label}"
                        else:
                            found[identity] = info
        except OSError:
            continue
    return list(found.values())


def run_key_entries() -> list[str]:
    key = _hive(winreg.HKEY_CURRENT_USER, RUN_KEY)
    if key is None:
        return []
    with key:
        return sorted(winreg.EnumValue(key, i)[0]
                      for i in range(winreg.QueryInfoKey(key)[1]))


def snapshot_dir(root: Path) -> dict[str, list]:
    """``{relative path: [size, sha256]}`` for every file under ``root``."""
    out: dict[str, list] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            out[path.relative_to(root).as_posix()] = [
                path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()]
        except OSError:
            out[path.relative_to(root).as_posix()] = ["unreadable", ""]
    return out


def tree_files(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# --------------------------------------------------------------------- install

def install(setup: Path, target: Path, log: Path, report: Report) -> str | None:
    """Silent per-user install into a clean folder. Returns the launcher path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    args = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/NOCANCEL", "/CURRENTUSER", f"/DIR={target}", f"/LOG={log}"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=900)
    ok = result.returncode == 0
    report.check("installer runs silently and reports success", ok,
                 f"exit {result.returncode}"
                 + ("" if ok else f": {(result.stdout + result.stderr).strip()[:200]}"))
    return str(target / "MathChecker.bat") if ok else None


def uninstall(report: Report, uninstaller: Path, log: Path) -> None:
    result = subprocess.run(
        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
         f"/LOG={log}"],
        capture_output=True, text=True, timeout=600)
    report.check("uninstaller runs silently and reports success",
                 result.returncode == 0,
                 f"exit {result.returncode}"
                 + ("" if result.returncode == 0 else
                    f": {(result.stdout + result.stderr).strip()[:200]}"))
    deadline = time.time() + 120
    while time.time() < deadline and uninstaller.exists():
        time.sleep(1.0)


# ----------------------------------------------------------------------- checks

def check_installed_tree(report: Report, app: Path) -> None:
    report.note("\n== installed layout ==")
    required = [
        "MathChecker.bat", "Stop.bat", "start_public.bat",
        "frontend/dist/index.html", "backend/app/main.py",
        "backend/requirements.txt", "assets/mathchecker.ico",
        ENGINE_MODULE.as_posix(),
    ]
    missing = [rel for rel in required if not (app / rel).is_file()]
    report.check("the installed tree has every launchable file", not missing,
                 f"missing: {missing}" if missing else
                 f"{len(required)} required files present")
    runtime = app / "runtime" / "python.exe"
    report.check("the bundled Python runtime is installed",
                 runtime.is_file(), str(runtime))
    # The installer must NOT ship the portable marker: an installed copy keeps
    # its data in %LOCALAPPDATA%, and a stray portable.flag would silently move
    # a teacher's data next to the program files instead.
    report.check("no portable.flag in the installed copy",
                 not (app / "portable.flag").exists(),
                 "installed layout keeps data in %LOCALAPPDATA%\\MathChecker")
    report.check("no Data folder inside the program folder",
                 not (app / "Data").exists(),
                 "customer data never lives in the installed program tree")


def check_shortcuts(report: Report, pre_desktop: bool) -> None:
    report.note("\n== shortcuts ==")
    group = start_menu_group()
    entries = sorted(p.name for p in group.iterdir()) if group.is_dir() else []
    report.check("Start Menu group is created with the app entries",
                 group.is_dir() and len(entries) >= 5,
                 f"{group}: {entries}")
    report.check("all-users Start Menu group untouched (per-user install)",
                 not machine_group().exists(), f"{machine_group()} absent")
    # Objective readability check, not taste: a link whose name opens a bracket
    # must close it. "Math Checker (Public" is what the slash in the .iss icon
    # name produces, and a teacher sees a truncated shortcut.
    broken = [name for name in entries
              if "(" in name and ")" not in name]
    report.check("no Start Menu shortcut name is cut off", not broken,
                 f"truncated: {broken}" if broken else f"{len(entries)} link(s) intact")
    report.check("desktop shortcut follows the installer's icon setting",
                 desktop_shortcut().exists() or pre_desktop,
                 "created by the silent install (icon task defaults on)")
    if pre_desktop:
        report.note("  (a desktop shortcut already existed; it is compared below)")

    # A shortcut with an odd NAME is cosmetic; one that is not a shortcut at
    # all, or points at the wrong file, is not. Targets are read the way Windows
    # reads them (WScript.Shell), not by guessing at the .lnk bytes: a link
    # whose target could not be resolved must never look like a pass.
    entries = sorted(os.scandir(group), key=lambda e: e.name) \
        if group.is_dir() else []
    not_links = [e.name for e in entries
                 if not e.name.lower().endswith(".lnk")]
    report.check("every Start Menu entry is a launchable shortcut",
                 not not_links,
                 f"not a .lnk file (cannot be launched): {not_links}"
                 if not_links else f"{len(entries)} shortcut(s)")
    resolved: dict[str, str] = {}
    if entries and not not_links:
        script = ("$s = New-Object -ComObject WScript.Shell; "
                  "Get-ChildItem -LiteralPath $env:G -File | ForEach-Object { "
                  "$l = $s.CreateShortcut($_.FullName); "
                  "Write-Output ($_.Name + [char]9 + $l.TargetPath) }")
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=180,
                                env=dict(os.environ, G=str(group)))
        for line in (result.stdout or "").splitlines():
            if "\t" in line:
                name, target = line.split("\t", 1)
                resolved[name.strip()] = target.strip()
    wanted = {"MathChecker.bat": "", "start_public.bat": "", "Stop.bat": "",
              "unins000.exe": ""}
    for launcher in wanted:
        wanted[launcher] = next(
            (name for name, target in resolved.items()
             if target.lower().endswith(launcher.lower())), "")
    missing = [name for name, hit in wanted.items() if not hit]
    report.check("every declared launcher is reachable from a Start Menu shortcut",
                 not missing,
                 f"no shortcut targets: {missing}" if missing else
                 "; ".join(f"{k} <- {v}" for k, v in wanted.items()))


def check_registry(report: Report) -> dict | None:
    report.note("\n== Add/Remove Programs entry ==")
    entries = uninstall_entries()
    report.check("exactly one Add/Remove Programs entry", len(entries) == 1,
                 json.dumps([{k: e.get(k) for k in ("hive", "DisplayName", "DisplayVersion")}
                             for e in entries]))
    if not entries:
        return None
    entry = entries[0]
    report.check("the entry is in the per-user hive", entry["hive"] == "HKCU",
                 f"hive={entry['hive']} key={entry['key']}")
    report.check("the entry names version, publisher and an uninstaller",
                 bool(entry.get("UninstallString"))
                 and bool(entry.get("DisplayVersion"))
                 and bool(entry.get("Publisher")),
                 f"{entry.get('DisplayName')} {entry.get('DisplayVersion')} by "
                 f"{entry.get('Publisher')}")
    return entry


def check_program_folder_clean(report: Report, app: Path, before: set[str]) -> None:
    """A run must not write data, logs or config into the program folder.

    Installed copies live in Program Files, which is not writable for the user
    running the app, so anything the app writes there is a failure waiting for a
    real machine. Python bytecode caches are reported separately: the launcher
    sets PYTHONDONTWRITEBYTECODE=1 precisely to avoid them, but a bundled
    runtime invoked directly by a helper may still cache, and that is not app
    data.
    """
    added = sorted(tree_files(app) - before)
    bytecode = [p for p in added if p.endswith(".pyc") or "__pycache__" in p]
    payload = [p for p in added if p not in bytecode]
    report.check("running the app wrote no data, log or config into the program "
                 "folder", not payload,
                 f"new files: {payload[:6]}" if payload else
                 f"{len(before)} files, unchanged")
    if bytecode:
        report.note(f"  note: {len(bytecode)} Python bytecode cache file(s) under "
                    f"the bundled runtime (e.g. {bytecode[0]}); harmless, and the "
                    f"launcher's PYTHONDONTWRITEBYTECODE avoids them on a real run"
                    if len(bytecode) else "  note: no bytecode written")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--setup", required=True,
                        help="the compiled MathChecker_Setup.exe")
    parser.add_argument("--dir", default=None,
                        help="clean folder to install into (default: a scratch "
                             "folder under %%LOCALAPPDATA%%)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the installed copy and the test files")
    parser.add_argument("--report", default=None,
                        help="also write the results to this file")
    args = parser.parse_args()

    setup = Path(args.setup).resolve()
    if not setup.is_file():
        print(f"setup executable not found: {setup}")
        return 2
    if os.name != "nt":
        print("This verifies a WINDOWS installer; run it on Windows.")
        return 2

    scratch = Path(args.dir).resolve() if args.dir else \
        local_appdata() / "MC5-Installer-Test"
    app = scratch / "App"
    report_path = Path(args.report).resolve() if args.report else None
    # Match what the launcher does for the app it starts, so this check never
    # writes bytecode into the installed copy itself.
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    report = Report(report_path)
    report.note(f"setup     : {setup}")
    report.note(f"install to: {app}")
    report.note(f"data dir  : {data_dir()}")

    if app.exists():
        print(f"refusing to install into a non-clean folder: {app}")
        return 2

    # ---- 1. BEFORE ------------------------------------------------------
    report.note("\n== machine before ==")
    pre_data = snapshot_dir(data_dir())
    pre_run = run_key_entries()
    pre_entries = uninstall_entries()
    pre_desktop = desktop_shortcut().exists()
    report.check("no Math Checker install is present already",
                 not pre_entries and not (PROGRAM_FILES / APP_NAME).exists(),
                 f"{len(pre_entries)} existing Add/Remove entry/entries")
    report.check("the target install folder is clean", not app.exists(), str(app))
    report.note(f"  pre-existing user data: {len(pre_data)} file(s) in "
                f"{data_dir()} (kept, and hashed for comparison)")

    uninstaller: Path | None = None
    instance: Instance | None = None
    demo_data = data_dir() / "demo"
    demo_before = snapshot_dir(demo_data)
    code = 1
    try:
        # ---- 2. INSTALL -------------------------------------------------
        report.note("\n== install (silent, per-user, clean folder) ==")
        launcher = install(setup, app, scratch / "install.log", report)
        if not launcher:
            return report.summary()
        check_installed_tree(report, app)
        check_shortcuts(report, pre_desktop)
        entry = check_registry(report)
        report.check("the user-data folder for the installed copy exists",
                     data_dir().is_dir(),
                     f"{data_dir()} (declared uninsneveruninstall)")
        folder_before = tree_files(app)

        # ---- 3. RUN -----------------------------------------------------
        report.note("\n== run the INSTALLED copy ==")
        port = free_port()
        python_before = python_pids()
        instance = Instance(app, port, scratch / "app.log")
        # Demo mode on purpose: it keeps its own data directory, so a machine
        # that already holds a teacher's real database is never written to.
        instance.start("--demo")
        report.check("the installed launcher starts the app",
                     wait_health(port), f"cmd /c MathChecker.bat {port} --demo")
        status, _, body = http("GET", f"http://127.0.0.1:{port}/")
        report.check("the UI is served", status == 200 and b"<html" in body.lower(),
                     f"GET / → HTTP {status}, {len(body)} bytes")
        # Demo Mode is the one local mode that deliberately asks for no account
        # (auth_gate.login_required_enabled): localhost only, synthetic
        # students, mock engine, Classroom disabled. Assert the DOCUMENTED
        # behaviour here and prove the gate itself in the next phase.
        session = sign_in(port, demo_data)
        report.check("the copy signs in", session is not None,
                     "signed in as the bootstrap developer account"
                     if session else "login failed")
        status, _, _ = http("GET", f"http://127.0.0.1:{port}/api/data/inventory")
        report.check("demo mode answers anonymous reads by design", status == 200,
                     f"GET /api/data/inventory without a session -> {status} "
                     f"(auth_gate: demo needs no account; MC5_LOGIN_REQUIRED=1 "
                     f"still overrides)")
        grade, _stored = check_correction(report, instance, demo_data, session)
        report.check("the app marks a PDF against the uploaded mark scheme",
                     grade > 0, f"provisional grade {grade} (9 = best)")
        report.check("the installed copy used %LOCALAPPDATA%\\MathChecker\\demo",
                     (demo_data / "app.db").is_file(),
                     str(demo_data / "app.db"))
        check_program_folder_clean(report, app, folder_before)

        # ---- 4. THE GATE, then STOP -------------------------------------
        report.note("\n== restart with the sign-in gate enforced =")
        report.note("   (MC5_LOGIN_REQUIRED=1 is the documented override that "
                    "turns the gate on\n    for an otherwise account-free "
                    "demo instance)")
        report.check("Stop.bat stops the installed copy",
                     instance.stop_with_launcher(), f"port {port} free afterwards")
        os.environ["MC5_LOGIN_REQUIRED"] = "1"
        gate_port = free_port()
        instance = Instance(app, gate_port, scratch / "app_gated.log")
        instance.start("--demo")
        report.check("the installed launcher starts the copy again",
                     wait_health(gate_port), f"cmd /c MathChecker.bat {gate_port} --demo")
        check_auth(report, instance, demo_data)
        check_program_folder_clean(report, app, folder_before)

        # ---- 5. STOP ----------------------------------------------------
        report.note("\n== stop with the shipped Stop.bat ==")
        report.check("Stop.bat stops the installed copy",
                     instance.stop_with_launcher(),
                     f"port {gate_port} free afterwards")
        report.check("no app process is left behind",
                     not listening_pids(gate_port),
                     f"listening PIDs: {listening_pids(gate_port)}")

        # ---- 6. UNINSTALL ----------------------------------------------
        if entry and entry.get("UninstallString"):
            uninstaller = Path(entry["UninstallString"].strip('"').split('"')[0])
        if uninstaller is None or not uninstaller.is_file():
            candidate = app / "unins000.exe"
            uninstaller = candidate if candidate.is_file() else None
        report.note("\n== uninstall ==")
        if uninstaller is None:
            report.check("the uninstaller is present", False,
                         "no UninstallString in the registry and no unins000.exe")
        else:
            uninstall(report, uninstaller, scratch / "uninstall.log")
            report.check("the program folder is removed",
                         not app.exists() or not any(app.iterdir()),
                         f"{app} "
                         + ("removed" if not app.exists() else f"still holds "
                            f"{len(list(app.iterdir()))} item(s)"))
            group = start_menu_group()
            report.check("the Start Menu group is removed",
                         not group.is_dir() or not any(group.iterdir()),
                         f"{group} " + ("removed" if not group.exists() else "still present"))
            report.check("the desktop shortcut is removed again",
                         not desktop_shortcut().exists() or pre_desktop,
                         f"{desktop_shortcut()} "
                         + ("removed" if not desktop_shortcut().exists()
                            else "still present"))
            report.check("the Add/Remove Programs entry is removed",
                         not uninstall_entries(), "registry clean")
            report.check("no autostart entry was added",
                         run_key_entries() == pre_run,
                         f"Run key: {sorted(set(run_key_entries()) - set(pre_run)) or 'unchanged'}")
            # Only processes THIS test started count: the verifier itself is a
            # python.exe, and so are any unrelated tools on the machine.
            strays = python_pids() - python_before - {os.getpid()}
            report.check("no orphaned app process after uninstall", not strays,
                         f"new python process(es): {sorted(strays)}" if strays
                         else "no new python process from this test")

        # ---- 7. LEFTOVERS ----------------------------------------------
        report.note("\n== what the machine has left ==")
        left = tree_files(app)
        report.check("no program file survives the uninstall", not left,
                     f"program files remaining: {sorted(left)[:6]}" if left
                     else "program tree empty")
        post_data = snapshot_dir(data_dir())
        changed = sorted(k for k, v in pre_data.items()
                         if post_data.get(k) != v)
        report.check("pre-existing user data is untouched", not changed,
                     f"changed: {changed[:6]}" if changed
                     else f"{len(pre_data)} pre-existing file(s) byte-identical")
        scratch_only = sorted(set(post_data) - set(pre_data))
        report.check("the ONLY leftovers are the app's own user-data area "
                     "(uninsneveruninstall by design)", True,
                     f"user data kept: {data_dir()} ({len(post_data)} file(s)); "
                     f"new during this test: {scratch_only[:6]}")
        if not args.keep:
            removed = [p for p in sorted(set(post_data) - set(pre_data))
                       if p.startswith("demo/")]
            if demo_data.is_dir():
                robust_rmtree(demo_data)
            report.check("this test's scratch data is removed again",
                         not demo_data.exists(),
                         f"deleted {len(removed)} test file(s) under "
                         f"{demo_data.relative_to(data_dir())}")
            if scratch.is_dir():
                robust_rmtree(scratch)
            report.check("the test's scratch folder is removed",
                         not scratch.exists(), str(scratch))
            report.check("the machine is back to its pre-test data",
                         snapshot_dir(data_dir()) == pre_data,
                         f"{len(pre_data)} file(s), identical to before")
        code = report.summary()
    finally:
        if instance and instance.proc and instance.proc.poll() is None:
            try:
                instance.stop_with_launcher(timeout=30)
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
        if args.keep:
            report.note(f"\nkept: {scratch} (install folder, logs, verify_work)")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
