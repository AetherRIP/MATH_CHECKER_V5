"""Math Checker V5 — single startup entry point.

Initialisation order (§29):
    configuration -> database -> API key manager -> job queue / worker pool
    -> Google Classroom (lazy, on first use) -> UI

Usage:
    python main.py            # serves http://127.0.0.1:8642 and opens the UI
    python main.py 9000       # custom port
    python main.py --stop     # stop the instance already running (Stop.bat)

The launcher (MathChecker.bat / start.bat) sets MC5_DATA_DIR; ``--stop``
uses it too, so it always stops the instance that belongs to THIS install.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND))


def preflight() -> list[str]:
    """Validate configuration before starting workers; returns warnings.

    Startup health summary is computed from the secret store + local DB only
    (spec: lightweight startup — no automatic key testing, no network).
    """
    from app.config import get_secrets
    warnings: list[str] = []
    keys = get_secrets().gemini_keys()
    if not keys:
        warnings.append(
            "No Gemini API keys configured yet — add them on the API Keys page.")
    if not get_secrets().oauth_client_config():
        warnings.append(
            "Google Classroom OAuth client not found — Classroom features "
            "disabled until data/secrets/google_oauth_client.json exists.")
    return warnings


def startup_key_summary() -> str:
    """One-line Gemini key inventory from local records (no API requests)."""
    from app.config import get_secrets
    from app.db import init_db
    from app.gemini.key_manager import KeyManager
    init_db()
    manager = KeyManager()
    status = manager.status()
    lines = [
        f"Gemini API Manager — configured keys: {status['total']} "
        f"(healthy {status['healthy']}, cooling {status['cooldown']}, "
        f"invalid {status['invalid']}, disabled {status['disabled']})",
    ]
    for item in status["keys"]:
        name = item.get("label") or "Gemini Key"
        lines.append(f"  {name} [{item['fingerprint']}] : {item['status']}")
    return "\n".join(lines)


def probe_port(port: int) -> str:
    """Classify what is on 127.0.0.1:<port>: ``free`` / ``ours`` / ``foreign``.

    A plain bind (NO SO_REUSEADDR) is the reliable "is it taken" test on
    Windows: uvicorn cannot start when another process already holds the port,
    so the official launcher must detect that itself instead of dying with an
    opaque bind error (exit code 3).

    When the port is taken we then call ``/api/health`` to decide whether the
    holder is Math Correct (attach to it) or a different program (report a
    port conflict).
    """
    import json
    import socket
    import time as time_module
    import urllib.error
    import urllib.request

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        pass
    else:
        sock.close()
        return "free"

    # A failed bind does NOT by itself prove that something is listening. A
    # socket left in TIME_WAIT by a connection that has just closed also makes a
    # plain bind fail — and that is exactly the state of this app's port right
    # after it was stopped, because the browser's connections to the UI close
    # when it exits. Treating it as "taken" made Stop.bat report a failed stop
    # after a SUCCESSFUL one, and made the next launch refuse to start with a
    # bogus "PORT CONFLICT" for as long as the socket lingered (uvicorn binds
    # with SO_REUSEADDR, so the app itself would have started fine).
    #
    # An active connect settles the question: with nothing bound to the port the
    # OS answers with a refusal, whereas a real listener accepts the connection
    # and can then be identified over HTTP below.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            pass
    except ConnectionRefusedError:
        return "free"
    except OSError:
        pass  # unexplained: keep probing rather than claim the port is free

    # Port taken — is the holder Math Correct?
    url = f"http://127.0.0.1:{port}/api/health"
    last_error = "no response"
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                body = resp.read(512)
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            if payload.get("status") == "ok":
                return "ours"
            return "foreign"
        except urllib.error.HTTPError:
            return "foreign"  # an HTTP server, but not Math Correct
        except Exception as exc:  # noqa: BLE001 — any socket-level failure
            last_error = str(exc)
            time_module.sleep(0.6)
    # Something holds the port but never answers HTTP (or is mid-restart).
    return f"unresponsive:{last_error}"


def port_pids(port: int) -> list[int]:
    """Process IDs currently LISTENING on ``port`` (best effort per platform)."""
    import os
    import subprocess

    pids: list[int] = []
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True,
                                 timeout=20).stdout
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
                                 capture_output=True, text=True,
                                 timeout=20).stdout
            for token in out.split():
                try:
                    pids.append(int(token))
                except ValueError:
                    pass
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return []
    return sorted(set(pids))


def _may_terminate(pid: int) -> bool:
    """May THIS process ask the OS to terminate ``pid``?

    Asked of the operating system rather than parsed out of a message, because
    the answer the teacher needs must not depend on the display language. The
    one refusal they can fix themselves is a copy started by another account —
    the Windows boot task runs Math Checker as SYSTEM, and an ordinary window is
    refused with "Access is denied".

    True means "permission is NOT the problem", which includes the cases where
    the answer cannot be established at all: ``OpenProcess`` also fails for a
    process that no longer exists, and blaming SYSTEM for a dead PID would be
    inventing a reason rather than reporting one. Only ``ERROR_ACCESS_DENIED``
    is treated as a permission refusal.
    """
    import os

    if os.name != "nt":
        return True
    try:
        import ctypes

        PROCESS_TERMINATE = 0x0001
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() != ERROR_ACCESS_DENIED
    except Exception:  # noqa: BLE001 — cannot tell: say nothing rather than guess
        return True


def _report_stop_refused(pid: int, result) -> None:
    """Explain a refused kill: the OS's own words, then what to do about it.

    ``taskkill``'s output used to be captured and dropped, which turned a
    permissions refusal into the vague "Port still answers after the stop
    request" — the message a teacher cannot act on.
    """
    out = getattr(result, "stdout", "") or ""
    err = getattr(result, "stderr", "") or ""
    print(f"  ! The stop was refused for PID {pid} "
          f"(taskkill exit {getattr(result, 'returncode', '?')}).")
    for line in ((out + "\n" + err).splitlines()):
        if line.strip():
            print(f"    {line.strip()}")
    if not _may_terminate(pid):
        print("  ! That is a permissions refusal, not a broken stop command: this")
        print("    copy was started by another account — typically the Windows")
        print("    boot task, which runs Math Checker as SYSTEM.")
        print("    Right-click Stop.bat and choose \"Run as administrator\"; an")
        print("    ordinary window cannot stop a process owned by SYSTEM.")
    else:
        print("    Retry once: a copy that is mid-shutdown can refuse the kill.")


def stop_running_instance(port: int) -> int:
    """Stop the Math Checker instance serving ``port`` (``main.py --stop``).

    Identity is proven through the health endpoint BEFORE anything is killed,
    so a process that merely happens to hold the port is never touched. The
    whole process tree goes down with it, which is what removes the correction
    workers and any tunnel this install started — the reason a teacher needs
    this command and not just a closed console window (§2 of the packaging
    work: a reliable way to stop application-managed background processes).
    """
    import json
    import os
    import signal
    import subprocess
    import time as time_module

    state = probe_port(port)
    if state == "free":
        print(f"Nothing to stop: port {port} is free — Math Checker is not running.")
        return 0
    if state != "ours":
        print("=" * 56)
        print(f"  REFUSING TO STOP: the program holding port {port} does not")
        print("  answer the Math Checker health check, so it is not this")
        print("  application. Stop it from Task Manager / Activity Monitor.")
        print("=" * 56)
        return 5

    pids = port_pids(port)
    if not pids:
        # Fall back to the record this application writes while it runs.
        try:
            from app.config import DATA_DIR
            info = json.loads((Path(DATA_DIR) / "instance.json").read_text(
                encoding="utf-8"))
            if (int(info.get("port") or 0) == port
                    and int(info.get("pid") or 0) > 0):
                pids = [int(info["pid"])]
        except Exception:  # noqa: BLE001 — best effort only
            pids = []
    if not pids:
        print(f"Math Checker answers on port {port}, but its process ID could")
        print("not be found — stop it from Task Manager / Activity Monitor.")
        return 5

    print(f"Stopping Math Checker on port {port} "
          f"(PID {', '.join(map(str, pids))})…")
    for pid in pids:
        try:
            if os.name == "nt":
                result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                        capture_output=True, timeout=30)
                # A refused kill is reported with its reason: dropping the
                # output is how "Run as administrator" became unknowable.
                if getattr(result, "returncode", 0) != 0:
                    _report_stop_refused(pid, result)
                    return 5
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Could not stop PID {pid}: {exc}")
            return 5

    deadline = time_module.time() + 20
    while time_module.time() < deadline:
        if probe_port(port) == "free":
            # The record is only useful while the instance runs; a forced stop
            # never reaches the app's own cleanup, so sweep it here (otherwise
            # diagnostics report a crashed instance that is simply gone).
            try:
                from app.config import DATA_DIR
                record = Path(DATA_DIR) / "instance.json"
                info = json.loads(record.read_text(encoding="utf-8"))
                if int(info.get("port") or 0) == port:
                    record.unlink()
            except Exception:  # noqa: BLE001 — cleanup only
                pass
            print("Math Checker stopped. Your data was left untouched,")
            print("and any correction that was in flight resumes when the")
            print("app is started again.")
            return 0
        time_module.sleep(0.5)
    print(f"  ! Port {port} still answers after the stop request.")
    return 5


def recorded_instance_port() -> int:
    """The port this install's running instance recorded, or 0.

    ``instance.json`` is written by the launch itself, so it is the only
    reliable source when the app was started on a port that is neither the
    configured one nor the documented default (for example because the default
    port was already taken, or because the user launched it with an explicit
    port).
    """
    import json

    try:
        from app.config import DATA_DIR
        info = json.loads((Path(DATA_DIR) / "instance.json").read_text(
            encoding="utf-8"))
        port = int(info.get("port") or 0)
        return port if 0 < port < 65536 else 0
    except Exception:  # noqa: BLE001 — best effort only
        return 0


def resolve_stop_port() -> int:
    """Which port ``--stop`` should act on when none was given on the CLI.

    The port this install's own instance record says it is serving comes FIRST:
    the record lives next to this install's data and is written by the launch
    that is actually running, so it is the only thing that identifies *this*
    copy when the configured port was taken and the app moved to another one.

    Probing the configured port first would be wrong for exactly that case: if
    a second Math Checker (or anything else) happens to hold it, Stop.bat would
    report on — and could act on — the wrong port while this install keeps
    running. The configured port and then the documented default follow as
    fallbacks for an install that never recorded anything. The first candidate
    that answers as Math Checker wins; if none does, the first non-free
    candidate is returned so the caller can report the port conflict honestly
    instead of claiming there is nothing to stop.
    """
    candidates: list[int] = [recorded_instance_port()]
    try:
        from app.config import get_config
        candidates.append(int(get_config().network.port))
    except Exception:  # noqa: BLE001 — fall back to the defaults below
        pass
    candidates.append(8642)

    seen: set[int] = set()
    ordered: list[int] = []
    for port in candidates:
        if port and port not in seen:
            seen.add(port)
            ordered.append(port)

    first_taken = 0
    for port in ordered:
        state = probe_port(port)
        if state == "ours":
            return port
        if state != "free" and not first_taken:
            first_taken = port
    return first_taken or (ordered[0] if ordered else 8642)


def open_ui_when_ready(port: int, timeout: float = 90.0) -> None:
    """Open the browser once the server really answers (interactive launches).

    Opening the URL before uvicorn is listening shows the teacher a connection
    error, so the launch waits for the health endpoint first. Boot/autostart
    launches never open a window (session 0 has no desktop) and
    ``MC5_NO_BROWSER=1`` suppresses it for scripted runs.
    """
    import os
    import time as time_module
    import urllib.request
    import webbrowser

    if (os.environ.get("MC5_AUTOSTART") == "1"
            or os.environ.get("MC5_NO_BROWSER") == "1"):
        return
    url = f"http://127.0.0.1:{port}"
    deadline = time_module.time() + timeout
    while time_module.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1.5) as resp:
                if resp.status == 200:
                    break
        except Exception:  # noqa: BLE001 — keep polling until the deadline
            pass
        time_module.sleep(0.5)
    else:
        return  # never became ready: stay silent, the console shows the reason
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — a browser is a convenience, not a service
        pass


def write_instance_record(port: int) -> Path | None:
    """Record this process in ``<data>/instance.json`` for diagnostics."""
    import json
    import os
    import time

    try:
        from app.config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:  # noqa: BLE001 — diagnostics must never block startup
        base = BACKEND.parent / "data"
    path = base / "instance.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": os.getpid(), "port": port, "started_at": int(time.time()),
            "argv": sys.argv,
        }, indent=2), encoding="utf-8")
    except OSError:
        return None
    return path


def serve(port: int, host: str, *, supervised: bool = False) -> int:
    """Run the web server, wired to the server-lifecycle stop hook.

    The process is NOT killed from the outside when nobody is using it: the
    backend asks its own uvicorn server to exit, which is the only clean way
    there is. Uvicorn then finishes in-flight requests and runs the
    application's lifespan shutdown — worker pool drained, tunnel stopped, temp
    workspaces cleaned — so an idle stop is a normal shutdown, not a crash.
    The cancel hook takes that request back when a teacher arrives inside the
    last-chance window.
    """
    import uvicorn

    from app.lifecycle import control

    config = uvicorn.Config("app.main:app", host=host, port=port,
                            log_level="info")
    server = uvicorn.Server(config)
    control.attach(lambda: setattr(server, "should_exit", True),
                   lambda: setattr(server, "should_exit", False))
    if supervised:
        # The relay in front of us prints the banner and owns instance.json;
        # saying it twice would only confuse whoever reads the console.
        print(f"  Backend serving http://{host}:{port} "
              "(supervised — starts and stops on demand)", flush=True)
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover — a person pressed Ctrl+C
        pass
    finally:
        control.attach(None, None)
    return 0


def main() -> int:
    import os
    import threading

    # `--stop` (Stop.bat) must run before anything else touches the data
    # directory, demo mode or the port: it only reports on and stops the
    # instance that is already serving this install's port.
    if "--stop" in sys.argv[1:]:
        rest = [a for a in sys.argv[1:] if a != "--stop"]
        if rest:
            # An explicit port always wins: the caller said which one to stop.
            try:
                return stop_running_instance(int(rest[0]))
            except ValueError:
                return stop_running_instance(8642)
        # No port given: find the instance THIS install is actually serving,
        # including one started on a non-default port (its own record knows).
        return stop_running_instance(resolve_stop_port())

    # Demo Mode must be decided BEFORE app.config is imported: configuration
    # reads MC5_DATA_DIR at import time, and demo runs in its own isolated
    # data directory so it can never touch real data or credentials.
    demo = "--demo" in sys.argv[1:] or os.environ.get("MC5_DEMO") == "1"
    if demo:
        base = os.environ.get("MC5_DATA_DIR") or str(BACKEND.parent / "data")
        from app.demo import activate
        activate(base)
        sys.argv = [a for a in sys.argv if a != "--demo"]

    from app.config import ensure_dirs, get_config
    from app.version import APP_NAME, APP_VERSION, BUILD_NUMBER

    ensure_dirs()
    cfg = get_config()
    if demo:
        cfg.mock_mode = True

    # --- server lifecycle modes (backend/app/lifecycle) -----------------------
    #   (no flag)        the normal launch: the on-demand relay when
    #                    [lifecycle] supervisor = true, the plain server when
    #                    it is off. The teacher's command never changes.
    #   --supervise      the relay explicitly, in the foreground.
    #   --serve  PORT    the backend ONLY, on a private port. The relay uses
    #                    this to start the app; a person never needs to.
    argv = sys.argv[1:]
    serve_only = "--serve" in argv
    supervise_only = "--supervise" in argv
    rest = [a for a in argv if a not in ("--serve", "--supervise")]
    under_supervisor = os.environ.get("MC5_SUPERVISED") == "1"

    # Port/access precedence: CLI arg > config > default. The bind address
    # comes from ONE place (app.remote_access.resolve_host): local-only is the
    # safe default, LAN mode is an explicit opt-in, and public mode
    # (MC5_PUBLIC=1, set by remote.py) always binds 0.0.0.0 so the tunnel and
    # LAN tests can both reach the server (§8).
    port = cfg.network.port
    if rest:
        # A teacher who mistypes the port must get a sentence, not a traceback
        # followed by a window that waits for a key press (the launcher pauses
        # on any non-zero exit).
        raw_port = rest[0]
        try:
            port = int(raw_port)
        except ValueError:
            print("=" * 56)
            print(f"  '{raw_port}' is not a port number, so nothing was started.")
            print("  Usage:  MathChecker.bat            (default port)")
            print("          MathChecker.bat 8643       (another port)")
            print("          MathChecker.bat --demo     (synthetic data, no setup)")
            print("          Stop.bat 8643              (stop a running copy)")
            print("=" * 56)
            return 2
        if not 1 <= port <= 65535:
            print("=" * 56)
            print(f"  Port {port} is outside the valid range 1-65535.")
            print("=" * 56)
            return 2

    if serve_only:
        # The backend behind the relay. It listens on loopback only: the relay
        # owns the address people actually use, so LAN and public traffic keep
        # arriving at the one entry point that can also start the app again.
        # The port a user should be told about stays MC5_PORT (set by the
        # relay); this private one is only how the relay reaches us.
        if not os.environ.get("MC5_PORT"):
            os.environ["MC5_PORT"] = str(port)
        # The same startup summary an unsupervised launch prints: the teacher's
        # console is the only place these warnings appear, and supervision must
        # not swallow them.
        warnings = preflight()
        print("=" * 56, flush=True)
        print(f"{APP_NAME} v{APP_VERSION} (build {BUILD_NUMBER})", flush=True)
        print("=" * 56, flush=True)
        for warning in warnings:
            print(f"  ! {warning}", flush=True)
        if not warnings:
            print("  Configuration OK.", flush=True)
        try:
            print(startup_key_summary(), flush=True)
        except Exception as exc:  # noqa: BLE001 — never block startup on it
            print(f"  (key summary unavailable: {exc})", flush=True)
        return serve(port, "127.0.0.1", supervised=under_supervisor)

    # One authoritative record of the ACTUAL port for this process, so the
    # auth/status and remote-status endpoints never report a stale config port.
    os.environ["MC5_PORT"] = str(port)

    # --- single instance guard (Windows startup conflict fix) ----------------
    # A boot-time autostart instance, a second copy of start.bat or the
    # background launcher must never fight over the same port or start a
    # second correction backend (two worker pools = duplicate jobs).
    state = probe_port(port)
    if state == "ours":
        print(f"Math Correct is already running at http://127.0.0.1:{port}")
        print("Attaching to the running application — not starting a second copy.")
        # Interactive launches may open the app; boot/autostart launches must
        # never open a window or a browser (session 0 has no desktop).
        if (os.environ.get("MC5_AUTOSTART") != "1"
                and os.environ.get("MC5_NO_BROWSER") != "1"):
            import webbrowser
            try:
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception:  # noqa: BLE001 — a browser is a convenience
                pass
        return 0
    if state == "foreign":
        print("=" * 56)
        print(f"  PORT CONFLICT: another program already uses port {port}.")
        print("  Math Correct was NOT started. Either close that program or")
        print(f"  start Math Correct on another port:  python main.py {port + 1}")
        print("=" * 56)
        return 4
    if state.startswith("unresponsive:"):
        print("=" * 56)
        print(f"  PORT CONFLICT: something holds port {port} but does not")
        print(f"  answer HTTP ({state.split(':', 1)[1][:120]}).")
        print("  Math Correct was NOT started to avoid two competing copies.")
        print("=" * 56)
        return 4
    from app.remote_access import resolve_host
    host = resolve_host(cfg.network.access_mode)
    lan = host == "0.0.0.0"

    # --- on-demand startup ----------------------------------------------------
    # The relay owns the public port and starts/stops the backend behind it, so
    # a stopped app is one page load away instead of something the teacher has
    # to find and double-click. This is the default whenever
    # [lifecycle] supervisor = true, and it never nests: a backend started BY
    # the relay never starts a relay of its own.
    if not under_supervisor and (supervise_only or cfg.lifecycle.supervisor):
        from app.lifecycle.supervisor import run_supervisor

        if not cfg.lifecycle.supervisor and supervise_only:
            print("  Note: [lifecycle] supervisor is off in Settings — running "
                  "it anyway because --supervise was asked for.", flush=True)
        return run_supervisor(
            port,
            listen_host=host,
            backend_port=cfg.lifecycle.backend_port,
            project_root=BACKEND.parent,
            # A boot/autostart launch (session 0) must never open a window or a
            # browser, exactly as before.
            open_browser=(os.environ.get("MC5_AUTOSTART") != "1"
                          and os.environ.get("MC5_NO_BROWSER") != "1"),
        )

    warnings = preflight()
    print("=" * 56)
    print(f"{APP_NAME} v{APP_VERSION} (build {BUILD_NUMBER})")
    print("=" * 56)
    if demo:
        print("  DEMO MODE — synthetic data only; Google Classroom is disabled.")
        print(f"  Demo data directory: {os.environ.get('MC5_DATA_DIR')}")
    for warning in warnings:
        print(f"  ! {warning}")
    if not warnings:
        print("  Configuration OK.")
    if cfg.is_first_run():
        print("  First run: open the app in your browser to finish setup.")
    try:
        print(startup_key_summary())
    except Exception as exc:
        print(f"  (key summary unavailable: {exc})")
    if lan:
        print(f"  Serving on http://{host}:{port}")
        if os.environ.get("MC5_PUBLIC") == "1":
            print("  Public mode: authentication required for every API request.")
        else:
            print("  ! Anyone on your network can reach this app while it is running.")
    else:
        print(f"  Serving on http://127.0.0.1:{port}  (local only)")
    print("=" * 56)

    record = write_instance_record(port)
    # A teacher double-clicking the launcher must end up looking at the app,
    # not at a console window with a URL to retype. The wait happens in the
    # background so a slow start never delays the server itself.
    try:
        threading.Thread(target=open_ui_when_ready, args=(port,), daemon=True,
                         name="open-ui").start()
    except Exception:  # noqa: BLE001 — never block startup on a convenience
        pass
    try:
        serve(port, host)
    finally:
        if record is not None:
            try:
                record.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
