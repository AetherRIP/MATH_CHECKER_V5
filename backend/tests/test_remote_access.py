"""Remote-access tests (spec §9-§13, §19, §25, §31).

The rule these tests lock: nothing in the system may *claim* public access
without verification. The server binds from one resolver, the public address
comes only from the environment or a verified status file (never a stale one),
the frontend never hardcodes an origin, the tunnel always targets the actual
running port, and the verifier refuses to publish an address whose health,
UI or API checks fail.
"""

from __future__ import annotations

import importlib.util
import json
import time
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import auth_gate as ag
from app import remote_access as ra
from app.api import auth_routes
from app.errors import AppError

ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------- test app
def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ag.PublicAuthMiddleware)
    auth_routes.register(app)

    @app.get("/api/dashboard")
    def _dash():
        return {"ok": True}

    @app.get("/")
    def _index():
        return HTML_OK

    @app.exception_handler(AppError)
    async def _handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    return app


HTML_OK = '<html><body><div id="root">Math Checker</div></body></html>'


@pytest.fixture
def local_client(fresh_state):
    with TestClient(_build_app(), base_url="http://testserver") as c:
        yield c


@pytest.fixture
def public_client(fresh_state, monkeypatch):
    monkeypatch.setenv("MC5_PUBLIC", "1")
    with TestClient(_build_app(), base_url="https://testserver") as c:
        yield c


@pytest.fixture
def remote():
    """The launcher module (repo root remote.py) imported as a module."""
    spec = importlib.util.spec_from_file_location("mc_remote", ROOT / "remote.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------- binding (§8)
class TestBinding:
    def test_local_mode_binds_loopback_only(self):
        assert ra.resolve_host("local", env={}) == "127.0.0.1"

    def test_lan_mode_binds_all_interfaces(self):
        assert ra.resolve_host("lan", env={}) == "0.0.0.0"

    def test_public_mode_binds_all_interfaces_even_in_local_config(self):
        assert ra.resolve_host("local", env={"MC5_PUBLIC": "1"}) == "0.0.0.0"

    def test_unknown_mode_stays_local(self):
        assert ra.resolve_host("weird", env={}) == "127.0.0.1"


# ---------------------------------------------------------- /api/health (§11)
class TestHealth:
    def test_health_returns_ok_without_student_data(self, local_client):
        body = local_client.get("/api/health")
        assert body.status_code == 200
        assert body.json() == {"status": "ok"}

    def test_health_is_reachable_without_a_session_in_public_mode(self,
                                                                 public_client):
        assert public_client.get("/api/health").status_code == 200

    def test_remote_status_is_public_and_leaks_no_secrets(self, public_client):
        r = public_client.get("/api/remote/status")
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["status"] in ("online", "starting", "offline")
        text = json.dumps(payload).lower()
        for forbidden in ("password", "secret", "token", "key"):
            assert forbidden not in text

    def test_remote_status_reports_offline_without_a_launcher(self, local_client):
        payload = local_client.get("/api/remote/status").json()
        assert payload["status"] == "offline"
        assert payload["public_url"] is None
        assert payload["reason"]  # a reason, never a silent blank


# -------------------------------------------------- public URL freshness (§10)
class TestPublicUrl:
    """The address is only advertised while the Public Address switch is ON.

    ``public_url`` is what every surface in the app reads (sign-in screen,
    discovery, the Remote Access card), so switching public access OFF has to
    stop publication there — not merely hide a panel. Cases about a *verified*
    address therefore run with the switch on, and ``TestPublicUrlGating``
    covers the OFF direction.
    """

    @pytest.fixture
    def public_switch_on(self, fresh_state):
        from app.config import get_config
        get_config().security.enabled = True
        yield

    def test_environment_variable_wins(self, fresh_state, monkeypatch):
        monkeypatch.setenv("MC5_PUBLIC_URL", "https://live-env.example")
        assert ra.public_url() == "https://live-env.example"

    def test_verified_status_file_is_used_when_online(self, fresh_state,
                                                      public_switch_on):
        ra.write_status({"status": "online", "public_url": "https://ok.example",
                         "last_verified": "2026-10-06 01:00:00"})
        assert ra.public_url() == "https://ok.example"

    def test_offline_status_never_returns_a_stale_url(self, fresh_state):
        ra.write_status({"status": "offline", "public_url": "https://dead.example",
                         "reason": "Tunnel died"})
        assert ra.public_url() is None

    def test_no_status_file_means_no_url(self, fresh_state):
        assert ra.public_url() is None

    def test_corrupt_status_file_reads_as_offline(self, fresh_state):
        path = ra.status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        state = ra.read_status()
        assert state["status"] == "offline"
        assert ra.public_url() is None

    def test_status_write_is_atomic_and_round_trips(self, fresh_state):
        ra.write_status({"status": "starting", "port": 9001})
        state = ra.read_status()
        assert state["status"] == "starting" and state["port"] == 9001
        assert state["updated_at"] > 0

    def test_a_stale_online_file_expires(self, fresh_state):
        """A killed launcher leaves no chance to write offline.

        Without this the panel would keep advertising an address whose tunnel
        no longer exists - the exact failure the spec calls out (§10, §22).
        """
        stale = time.time() - (ra.STATUS_TTL_SECONDS + 30)
        ra.write_status({"status": "online", "public_url": "https://gone.example",
                         "verified_at": stale, "updated_at": stale,
                         "last_verified": "2026-10-06 01:00:00"})
        assert ra.read_status()["status"] == "online"  # the file still says so
        assert ra.status_is_fresh() is False
        assert ra.public_url() is None
        assert ra.summary()["status"] == "offline"
        assert "stale" in ra.summary()["reason"].lower()

    def test_a_recent_heartbeat_keeps_the_address_alive(self, fresh_state,
                                                        public_switch_on):
        ra.write_status({"status": "online", "public_url": "https://ok.example",
                         "verified_at": time.time() - 5})
        assert ra.status_is_fresh() is True
        assert ra.public_url() == "https://ok.example"

    def test_status_is_never_fresh_unless_online(self, fresh_state):
        ra.write_status({"status": "offline", "verified_at": time.time(),
                         "public_url": "https://dead.example"})
        assert ra.status_is_fresh() is False


class TestPublicUrlGating:
    """OFF means OFF: no surface may advertise an address (Settings switch)."""

    def test_switch_off_hides_even_a_verified_address(self, fresh_state):
        ra.write_status({"status": "online", "public_url": "https://ok.example",
                         "verified_at": time.time()})
        assert ra.public_url() is None
        assert ra.summary()["public_url"] is None
        assert ra.summary()["reachable"]["public"] is False

    def test_environment_override_still_wins_while_switched_off(
            self, fresh_state, monkeypatch):
        """An operator-managed tunnel (MC5_PUBLIC_URL) is not ours to hide."""
        monkeypatch.setenv("MC5_PUBLIC_URL", "https://operator.example")
        assert ra.public_url() == "https://operator.example"

    def test_launcher_started_process_still_publishes(self, fresh_state,
                                                     monkeypatch):
        """``MC5_PUBLIC=1`` means remote.py started this server to be public."""
        monkeypatch.setenv("MC5_PUBLIC", "1")
        ra.write_status({"status": "online", "public_url": "https://ok.example",
                         "verified_at": time.time()})
        assert ra.public_url() == "https://ok.example"


# ------------------------------------------------------------- summary (§23)
class TestSummary:
    def test_offline_summary_carries_reason_and_local_url(self, fresh_state):
        s = ra.summary()
        assert s["status"] == "offline"
        assert s["public_url"] is None
        assert s["reason"]
        assert s["local_url"].startswith("http://127.0.0.1:")

    def test_online_summary_exposes_the_verified_address(self, fresh_state,
                                                        monkeypatch):
        from app.config import get_config
        get_config().security.enabled = True     # the switch is ON in this case
        ra.write_status({"status": "online", "public_url": "https://ok.example",
                         "last_verified": "2026-10-06 01:00:00", "port": 9001,
                         "verified_at": time.time(),
                         "checks": {"health": True}})
        s = ra.summary()
        assert s["status"] == "online"
        assert s["public_url"] == "https://ok.example"
        assert s["verified_checks"]["health"] is True
        assert s["local_url"] == "http://127.0.0.1:9001"

    def test_summary_reports_measured_reachability(self, fresh_state,
                                                   monkeypatch):
        """The panel's rows come from a boolean, never from a guess.

        Pointed at a port nothing listens on, so "not reachable" is a real
        measurement rather than an assumption about the test machine.
        """
        monkeypatch.setenv("MC5_PORT", str(_free_port()))
        s = ra.summary()
        r = s["reachable"]
        assert set(r) == {"local", "lan", "public", "api", "websocket",
                          "events"}
        assert r["local"] is False and r["lan"] is False
        assert r["public"] is False
        assert r["websocket"] == "not_required"  # the app uses SSE, not WS

    def test_summary_withholds_the_lan_address_from_public_callers(
            self, fresh_state):
        assert ra.summary(expose_lan=False)["lan_url"] is None

    def test_summary_uses_the_running_process_port(self, fresh_state,
                                                   monkeypatch):
        """The Local row must describe THIS server, not an older record.

        Found by the clean-install test: a second copy of the app on port 8799
        still advertised 127.0.0.1:8642 because a stale status file carried
        the port of an earlier launcher session.
        """
        ra.write_status({"status": "offline", "port": 8642})
        monkeypatch.setenv("MC5_PORT", "8799")
        s = ra.summary()
        assert s["port"] == 8799
        assert s["local_url"] == "http://127.0.0.1:8799"

    def test_summary_falls_back_to_a_recorded_port(self, fresh_state,
                                                   monkeypatch):
        monkeypatch.delenv("MC5_PORT", raising=False)
        ra.write_status({"status": "offline", "port": 9123})
        assert ra.summary()["local_url"] == "http://127.0.0.1:9123"

    def test_probe_health_says_no_when_nothing_answers(self, fresh_state):
        # Port 1 is never a Math Correct server; the probe must answer, not hang.
        assert ra.probe_health("http://127.0.0.1:1", timeout=0.5) is False

    def test_probe_health_says_yes_to_a_real_health_endpoint(self, fresh_state):
        """Proof the probe is a real request, not a process check."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                body = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):  # keep the test output clean
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            assert ra.probe_health(url, timeout=2.0) is True
        finally:
            server.shutdown()
            server.server_close()

    def test_public_status_endpoint_hides_the_internal_lan_address(
            self, public_client):
        payload = public_client.get("/api/remote/status").json()
        # The Starlette test client is not a loopback address, so the panel
        # must not advertise this machine's private network.
        assert payload["lan_url"] is None

    def test_local_status_endpoint_may_show_the_lan_address(self, fresh_state):
        s = ra.summary(expose_lan=True)
        assert (s["lan_url"] is None) == (not s["lan_enabled"] or
                                          s["lan_url"] is None)


# ---------------------------------------------- frontend origin audit (§13)
class TestFrontendOrigins:
    def test_frontend_never_hardcodes_a_server_origin(self):
        """No API/SSE call may point at localhost, an IP or a fixed port.

        The browser must always use the origin it was loaded from (relative
        URLs), otherwise the public page would call the teacher's own
        127.0.0.1 and fail on every external device.
        """
        src = ROOT / "frontend" / "src"
        assert src.is_dir(), "frontend source not found"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.ts*")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("*"):
                    continue
                calls_origin = ("fetch(" in line or "EventSource(" in line
                                or "XMLHttpRequest" in line)
                if not calls_origin:
                    continue
                lowered = line.lower()
                if ("http://localhost" in lowered
                        or "http://127.0.0.1" in lowered
                        or "https://127.0.0.1" in lowered
                        or ":8642" in line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped}")
        assert offenders == [], "hardcoded origins found:\n" + "\n".join(offenders)

    def test_frontend_uses_the_page_origin_for_api_calls(self):
        api_ts = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        assert 'fetch(`/api${path}`' in api_ts


# ------------------------------------------------------------ tunnel (§19)
class TestTunnelConfig:
    def test_the_bundled_helper_wins_over_a_system_install(self, remote,
                                                          tmp_path,
                                                          monkeypatch):
        """An installed copy must use the cloudflared the release shipped.

        The installer bundles it into tools/, and that version is the one the
        build verified - a different one on the teacher's PATH must not win.
        """
        import os as _os

        exe = "cloudflared.exe" if _os.name == "nt" else "cloudflared"
        tools = tmp_path / "tools"
        tools.mkdir()
        bundled = tools / exe
        bundled.write_bytes(b"bundled")
        monkeypatch.setattr(remote, "ROOT", tmp_path)
        monkeypatch.setattr(remote.shutil, "which",
                            lambda _name: "C:/somewhere-else/cloudflared.exe")
        assert remote.find_cloudflared() == str(bundled)

    def test_the_env_override_wins_over_everything(self, remote, tmp_path,
                                                   monkeypatch):
        chosen = tmp_path / "custom-cloudflared.exe"
        chosen.write_bytes(b"custom")
        monkeypatch.setenv("MC5_CLOUDFLARED", str(chosen))
        assert remote.find_cloudflared() == str(chosen)

    def test_tunnel_targets_the_exact_running_port(self, remote):
        argv = remote.tunnel_argv("C:/cloudflared.exe", 9007)
        assert "--url" in argv
        assert argv[argv.index("--url") + 1] == "http://127.0.0.1:9007"

    def test_cloudflared_url_is_parsed_from_its_log(self, remote):
        line = ("2026-10-06 INF +--------------------------+----------------+\n"
                "https://random-words-1234.trycloudflare.com")
        assert (remote.URL_RE.search("https://random-words-1234.trycloudflare.com")
                .group(0) == "https://random-words-1234.trycloudflare.com")
        assert remote.URL_RE.search(line) is not None

    def test_url_capture_reads_child_output(self, remote, tmp_path):
        proc = remote.Managed(
            "demo", [sys.executable, "-c",
                     "import time; print('INF starting'); "
                     "print('https://demo-tunnel-42.trycloudflare.com', flush=True); "
                     "time.sleep(30)"])
        try:
            url = remote.wait_for_tunnel_url(proc, seconds=15)
            assert url == "https://demo-tunnel-42.trycloudflare.com"
        finally:
            proc.stop()

    def test_url_capture_fails_when_the_tunnel_dies_silently(self, remote):
        proc = remote.Managed("demo", [sys.executable, "-c", "print('boom')"])
        url = remote.wait_for_tunnel_url(proc, seconds=5)
        assert url is None

    def test_wait_for_health_requires_a_live_server(self, remote):
        proc = remote.Managed("demo", [sys.executable, "-c", "print('bye')"])
        assert remote.wait_for_health(_free_port(), proc, seconds=5) is False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------ verification (§25/§26)
class _FakeOrigin(BaseHTTPRequestHandler):
    """A stand-in public origin that behaves like a healthy Math Correct."""

    def do_GET(self):  # noqa: N802
        if self.path == "/api/health":
            self._ok('{"status": "ok"}')
        elif self.path == "/":
            self._ok(HTML_OK, ctype="text/html")
        elif self.path == "/api/auth/status":
            self._ok('{"public_mode": true, "required": true, '
                     '"configured": true, "authenticated": false}')
        elif self.path == "/api/dashboard":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"code": "auth_required"}')
        else:
            self.send_response(404)
            self.end_headers()

    def _ok(self, body: str, ctype: str = "application/json") -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence the stdlib logger
        return


class TestVerification:
    @pytest.fixture
    def origin(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOrigin)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()

    def test_healthy_origin_passes_every_check(self, remote, origin):
        ok, checks, reason = remote.verify_public(origin, timeout=10)
        assert ok is True, reason
        assert checks == {"health": True, "ui": True, "auth_status": True,
                          "api_gated": True}

    def test_unreachable_origin_is_refused_with_a_reason(self, remote):
        ok, checks, reason = remote.verify_public(
            f"http://127.0.0.1:{_free_port()}", timeout=1)
        assert ok is False
        assert reason
        assert checks.get("health") is False

    def test_open_api_is_refused(self, remote, origin, monkeypatch):
        """A data API that answers unauthenticated must never be published."""
        original = remote.http

        def open_api(url, timeout=15):
            if url.endswith("/api/dashboard"):
                return 200, '{"students": []}'
            return original(url, timeout)

        monkeypatch.setattr(remote, "http", open_api)
        ok, checks, reason = remote.verify_public(origin, timeout=10)
        assert ok is False
        assert checks["api_gated"] is False
        assert "unauthenticated" in reason

    def test_wrong_ui_is_refused(self, remote, origin, monkeypatch):
        original = remote.http

        def wrong_ui(url, timeout=15):
            if url.rstrip("/").endswith(origin.rstrip("/")) or url.endswith("/"):
                if url.endswith("/") and "/api/" not in url:
                    return 200, "<html>some other site</html>"
            return original(url, timeout)

        monkeypatch.setattr(remote, "http", wrong_ui)
        ok, checks, reason = remote.verify_public(origin, timeout=10)
        assert ok is False
        assert checks.get("ui") is False
        assert "UI" in reason


# ------------------------------------------- idle internet cost (PHASE 1A)
class TestIdleTraffic:
    """An idle tunnel must not cost a page fetch every minute.

    The heartbeat asks only for `/api/health` (15 bytes, no student data, no
    Classroom sync, no Drive, no AI). The four-check verification that also
    pulls the UI page through the tunnel runs when the address is first
    established, every FULL_VERIFY_EVERY beats, and after a failure.
    """

    @pytest.fixture
    def origin(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOrigin)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}"
        server.shutdown()

    def test_heartbeat_is_a_single_tiny_request(self, remote, origin,
                                                monkeypatch):
        seen: list[str] = []
        original = remote.http

        def record(url, timeout=15):
            seen.append(url)
            return original(url, timeout)

        monkeypatch.setattr(remote, "http", record)
        ok, reason = remote.verify_alive(origin, timeout=10)
        assert ok is True, reason
        assert seen == [f"{origin}/api/health"]

    def test_heartbeat_says_no_and_explains_when_nothing_answers(self, remote):
        ok, reason = remote.verify_alive(
            f"http://127.0.0.1:{_free_port()}", timeout=1)
        assert ok is False
        assert "/api/health" in reason

    def test_full_verification_is_not_run_every_beat(self, remote):
        assert remote.HEARTBEAT_SECONDS >= 30
        assert remote.FULL_VERIFY_EVERY >= 2

    def test_full_verification_costs_four_requests(self, remote, origin,
                                                   monkeypatch):
        seen: list[str] = []
        original = remote.http

        def record(url, timeout=15):
            seen.append(url)
            return original(url, timeout)

        monkeypatch.setattr(remote, "http", record)
        ok, _checks, reason = remote.verify_public(origin, timeout=10)
        assert ok is True, reason
        assert len(seen) == 4
        assert seen[0].endswith("/api/health")


# ------------------------------------------------------- failure reporting (§22)
class TestFailureReporting:
    def test_fail_writes_offline_status_with_the_reason(self, remote, fresh_state):
        code = remote.fail("Tunnel could not connect to Math Correct on port 9001.",
                           9001)
        assert code == 1
        state = ra.read_status()
        assert state["status"] == "offline"
        assert state["public_url"] is None
        assert "port 9001" in state["reason"]
        assert state["port"] == 9001
