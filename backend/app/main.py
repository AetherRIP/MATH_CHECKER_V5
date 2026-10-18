"""FastAPI application for Math Checker V5.

Startup order (§29): configuration -> database -> key manager -> job queue /
worker pool -> Classroom -> UI. Shutdown is graceful: workers drain, keys are
released, temp workspaces removed.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ensure_dirs, get_config
from .db import init_db
from .errors import AppError, brief_traceback
from .logging_setup import log_event, setup_logging
from .testing_guard import PublicationBlocked

FRONTEND_DIST = (Path(__file__).resolve().parent.parent.parent
                 / "frontend" / "dist").resolve()


def safe_static_path(full_path: str) -> Path | None:
    """Resolve a request path to a file *inside* ``frontend/dist``, else None.

    Containment is enforced rather than assumed. The ASGI server percent-decodes
    the URL before routing, so a request for ``/%2e%2e/%2e%2e/data/secrets.toml``
    arrives here as ``../../data/secrets.toml``; joining that onto the build
    directory and calling ``is_file()`` would happily serve the teacher's secret
    store to anyone on the internet. Only files that resolve inside the built
    frontend are ever returned.
    """
    if not full_path or "\x00" in full_path or "\\" in full_path:
        return None
    if full_path.startswith(("/", "~")):
        return None
    try:
        candidate = (FRONTEND_DIST / full_path).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if candidate == FRONTEND_DIST or not candidate.is_relative_to(FRONTEND_DIST):
        return None
    return candidate if candidate.is_file() else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(level=logging.DEBUG if get_config().debug else logging.INFO)
    ensure_dirs()
    init_db()
    # Distributed layer bootstrap: additive CREATE TABLE IF NOT EXISTS on every
    # start. The one-time DEVELOPER/TEACHER accounts are created ONLY in
    # Multi-Account Mode: a fresh single-account install must have NO accounts,
    # so its sign-in screen offers "Create the first account" instead of
    # demanding a password nobody has been told (see distributed.bootstrap).
    try:
        result = distributed_bootstrap.bootstrap()
        if result.get("accounts") == "created":
            log_event("app", "multi-account admin accounts created",
                      credentials=result.get("credentials_path"))
    except Exception as exc:  # noqa: BLE001 — must never block app startup
        log_event("app", "distributed bootstrap skipped",
                  level=logging.WARNING, error=str(exc))

    # Demo Mode (§13): seed synthetic classes/students/results into the isolated
    # demo database and force the AI engine into mock mode. Classroom is hard
    # disabled while demo is active (see demo.guard_classroom).
    from .demo import is_demo, seed as seed_demo
    if is_demo():
        get_config().mock_mode = True
        try:
            summary = seed_demo()
            log_event("app", "demo data ready", **summary)
        except Exception as exc:  # noqa: BLE001 — demo must never block startup
            log_event("app", "demo seed failed", level=logging.WARNING, error=str(exc))

    # Restart-safety: orphaned in-flight jobs are requeued/terminalized here.
    # Jobs in any in-flight state are recovered (or failed cleanly if their
    # attempt budget is exhausted); a restarted app never silently re-marks a
    # job it was already working on (§12).
    from .jobs.manager import JobManagerHolder
    recovered = JobManagerHolder.instance().recover_on_boot()
    log_event("jobs", "boot recovery done", recovered=recovered)

    # Automation crash recovery (§41): release claims left by a killed process
    # and make every unfinished workflow job immediately due again. Each job
    # keeps its exact state, so it resumes from the step it reached rather
    # than restarting from NEW.
    from .automation import store as automation_store
    automation_recovery = automation_store.recover_in_flight()
    log_event("automation", "boot recovery done", **automation_recovery)

    # Large PDF Worker: reuse the SAME correction engine for long documents.
    # Boot recovery requeues any batch interrupted by a crash; the scheduler
    # then drives queued/ready jobs, so work resumes without re-doing the
    # batches that already completed.
    from .large_pdf import store as large_pdf_store
    from .large_pdf.scheduler import LargePdfSchedulerHolder
    large_recovery = large_pdf_store.recover_in_flight()
    log_event("large_pdf", "boot recovery done", **large_recovery)

    # Worker pool: the orchestrator around the trusted correction engine.
    from .workers.pool import WorkerPoolHolder
    pool = WorkerPoolHolder.instance()
    pool.start()
    app.state.pool = pool

    large_scheduler = LargePdfSchedulerHolder.instance()
    if get_config().large_pdf.enabled:
        large_scheduler.start()
    app.state.large_pdf = large_scheduler

    # Disk hygiene: drop orphaned per-job staging dirs from earlier crashes,
    # and reclaim retained originals whose teacher-set expiry has passed (§19).
    from .engine.service import sweep_expired_retained, sweep_stale_temp
    swept = sweep_stale_temp()
    if swept:
        log_event("app", "stale temp job dirs removed", removed=swept)
    sweep_expired_retained()

    # CorrectionService keeps the enqueue/status/result API surface (jobs are
    # executed by the WorkerPool); it also still owns cleanup of manual-upload
    # staging rows when a job reaches a terminal state.
    from .engine.service import CorrectionService
    service = CorrectionService()
    app.state.correction = service

    # Automation monitor (§5): ONE centralised scheduler for all enabled
    # assignments. It starts regardless of the enabled flag and idles when
    # disabled, so toggling automation on in the UI takes effect without a
    # restart. Demo Mode never touches Google Classroom, so it is not started.
    from .demo import is_demo
    from .automation.scheduler import AutomationSchedulerHolder
    scheduler = AutomationSchedulerHolder.instance()
    if not is_demo() and not get_config().mock_mode:
        scheduler.start()
    app.state.automation = scheduler

    # Public Address (Settings): when the switch is ON, bring the endpoint back
    # after a restart. Started in a background thread so a slow tunnel can
    # never delay the app's readiness — the state the UI reads is "starting"
    # until the address is genuinely verified (app/public_access.py).
    try:
        from . import public_access
        if public_access.requested():
            threading.Thread(target=public_access.autostart_if_requested,
                             daemon=True,
                             name="public-access-autostart").start()
    except Exception:  # noqa: BLE001 — never block startup on public access
        log_event("app", "public access autostart skipped", level=logging.WARNING)

    # Tiny Agent availability (§1 of the simplification work): this version
    # does not run one. Recorded at boot so an operator reading the log knows
    # that silence from a device is expected, and that an old auto-start entry
    # is inert rather than broken.
    try:
        from .distributed import agent_control
        log_event("app", "tiny agent availability",
                  enabled=agent_control.product_enabled(),
                  source=agent_control.product_source(),
                  setting=(f"[{agent_control.CONFIG_SECTION}] "
                           f"{agent_control.CONFIG_KEY}"))
    except Exception:  # noqa: BLE001 — never block startup on a log line
        pass

    # Server lifecycle (app.lifecycle): the backend decides when NOBODY is
    # using the website any more and then asks its own process to stop cleanly
    # (workers drained, tunnel stopped, temp workspaces cleaned). Started LAST
    # so the idle timer only ever runs against a fully initialised app: no
    # countdown can begin while boot recovery or the worker pool is still
    # coming up. Starting it can never fail the boot — an idle-stop that is not
    # running keeps the app up, which is the safe direction.
    try:
        from .lifecycle.monitor import LifecycleMonitorHolder
        lifecycle = LifecycleMonitorHolder.instance()
        lifecycle.start()
        app.state.lifecycle = lifecycle
    except Exception as exc:  # noqa: BLE001 — never block startup on it
        log_event("app", "lifecycle monitor not started",
                  level=logging.WARNING, error=str(exc))

    log_event("app", "Math Checker V5 started",
              version=app.version, workers=len(pool._workers),
              automation=scheduler.running())
    yield
    # The idle monitor goes first: once the app is shutting down there is
    # nothing left for it to decide.
    try:
        monitor = getattr(app.state, "lifecycle", None)
        if monitor is not None:
            await monitor.stop()
    except Exception:  # noqa: BLE001
        pass
    # Stop our own tunnel on the way out (no orphan cloudflared, no advertised
    # address from a process that has gone); the saved preference is kept so
    # the next launch restores it.
    try:
        from . import public_access
        public_access.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        scheduler.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        LargePdfSchedulerHolder.instance().stop()
    except Exception:  # noqa: BLE001
        pass
    pool.shutdown(drain=True)
    log_event("app", "Math Checker V5 stopped")


from .version import APP_NAME, APP_VERSION  # noqa: E402

app = FastAPI(title=f"{APP_NAME} v{APP_VERSION}", version=APP_VERSION,
              lifespan=lifespan)

# Multi-account mode guard (see app/multi_account.py): while the switch is
# OFF, the multi-account OPERATIONS are refused (403 multi_account_disabled)
# and only the single-account essentials keep working. Added BEFORE the public
# gate so it sits INSIDE it: an unauthenticated caller still gets 401 rather
# than a hint that the route exists, and a signed-in one gets the real reason.
from .multi_account import MultiAccountGuard  # noqa: E402

app.add_middleware(MultiAccountGuard)

# Public-access gate: a no-op locally, and mandatory authentication for every
# API request when the app is exposed through a tunnel (MC5_PUBLIC=1 or the
# [security] config flag). Added before any routes so it wraps them all.
from .auth_gate import PublicAuthMiddleware  # noqa: E402

app.add_middleware(PublicAuthMiddleware)

# Cross-origin access for a SHARED backend (multi-laptop deployment).
# One installation is the always-on backend; the other laptops' browser UIs
# address it by URL, which the browser refuses unless the backend names that
# origin. Registered AFTER the session gate so it is the OUTERMOST middleware:
# the preflight OPTIONS is answered before the local login gate can see it
# (that gate is a no-op for /api/distributed anyway — those routes authenticate
# every request themselves with a bearer token).
#
# Nothing is allowed by default and credentials are deliberately disabled: the
# browser is never given permission to send the app session cookie to another
# machine, so a cross-origin caller still needs a real distributed token.
from .config import allowed_origins  # noqa: E402

_ALLOWED_ORIGINS = allowed_origins()
if _ALLOWED_ORIGINS:
    from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Device-Id",
                       "X-CSRF-Token"],
        max_age=600,
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    include_detail = get_config().role == "developer"
    return JSONResponse(status_code=exc.status, content=exc.to_dict(include_detail=include_detail))


@app.exception_handler(PublicationBlocked)
async def publication_blocked_handler(request: Request, exc: PublicationBlocked):
    """Testing-only mode refused a write: a deliberate gate, not a crash.

    Without this the guard's refusal reached the generic 500 handler, so the
    UI reported "Something went wrong" for the one condition whose fix is
    written on the tin (turn Testing only mode off).
    """
    log_event("app", "write refused by testing-only mode", level=logging.INFO,
              action=getattr(exc, "action", None), method=request.method,
              path=request.url.path)
    return JSONResponse(status_code=403,
                        content={"code": "testing_only_blocked",
                                 "message": str(exc)})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    # The request is part of the evidence: without the method/path a one-line
    # "unhandled error" cannot be reproduced or traced back to a route.
    log_event("app", "unhandled error", level=logging.ERROR, error=str(exc),
              exc_type=type(exc).__name__, method=request.method,
              path=request.url.path, traceback=brief_traceback(exc))
    detail = str(exc)
    # Belt and braces: the testing-only guard's refusal is its own gate even
    # when something wrapped it before it escaped.
    from .testing_guard import PublicationBlocked as _Blocked
    if isinstance(exc, _Blocked):
        return JSONResponse(status_code=403,
                            content={"code": "testing_only_blocked",
                                     "message": detail})
    # A revoked/expired Google grant is ACTIONABLE, not an internal error.
    # The app log recorded real 500s whose message was the one instruction
    # that fixes the problem ("Reconnect Google Classroom…"), because only
    # routes that call routes_all._classroom_error translate it. This is the
    # safety net for every other endpoint that touches Classroom — the same
    # status + code, so the UI reads them identically.
    lowered = detail.lower()
    if ("reconnect google classroom" in lowered
            or "connect google classroom on the classroom page" in lowered):
        return JSONResponse(
            status_code=401,
            content={"code": "classroom_reconnect_required",
                     "message": detail},
        )
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error",
                 "message": "Something went wrong processing this request. "
                            "The error has been logged for the developer diagnostics page."},
    )


# Routes -----------------------------------------------------------------------

from .api import routes_all  # noqa: E402  (after app creation by design)

routes_all.register(app)

from .classroom import tracking_routes  # noqa: E402

tracking_routes.register(app)

from .api import student_routes  # noqa: E402

student_routes.register(app)

from .api import report_routes  # noqa: E402

report_routes.register(app)

from .api import setup_routes  # noqa: E402

setup_routes.register(app)

from .api import product_routes  # noqa: E402

product_routes.register(app)

from .api import scheme_routes  # noqa: E402

scheme_routes.register(app)

# Marking-workspace viewer (read-only): the original submission's pages, or an
# honest "not stored" answer. Registered before the SPA fallback.
from .api import viewer_routes  # noqa: E402

viewer_routes.register(app)

# Read-only analytics aggregates for the dashboard charts (no schema change).
from .api import analytics_routes  # noqa: E402

analytics_routes.register(app)

from .api import privacy_routes  # noqa: E402

privacy_routes.register(app)

from .api import auth_routes  # noqa: E402

auth_routes.register(app)

from .large_pdf import routes as large_pdf_routes  # noqa: E402

large_pdf_routes.register(app)

from .automation import routes as automation_routes  # noqa: E402

app.include_router(automation_routes.router)

# Read-only testing/accuracy audit view (no writes, no publishing).
from .audit import routes as audit_routes  # noqa: E402

audit_routes.register(app)

# Assignment repair session view (testing-only repair records, read-only).
from .repair import routes as repair_routes  # noqa: E402

repair_routes.register(app)

# Distributed multi-school layer: additive tables + shared-identity API.
# The existing correction engine, Classroom integration and automation are
# untouched; this only adds accounts/schools/workers/sync/updates around them.
from .distributed import bootstrap as distributed_bootstrap  # noqa: E402
from .distributed.routes import router as distributed_router  # noqa: E402

app.include_router(distributed_router)

# Access controls (Settings): the Multi-Account Mode state and the Public
# Address ON/OFF switch that starts/stops the app-managed tunnel. Owner-only,
# enforced server-side in the router itself.
from .api import access_routes  # noqa: E402

access_routes.register(app)

# Data portability (§2): versioned student/result export, import preview and a
# transactional import. The package never contains keys, tokens or sessions.
from .api import data_routes  # noqa: E402

data_routes.register(app)

# Server lifecycle: heartbeat, goodbye and the real idle/stop state the
# launcher and the Settings page read. Registered before the SPA fallback so
# /api/lifecycle/* is never swallowed by the catch-all route.
from .lifecycle import routes as lifecycle_routes  # noqa: E402

lifecycle_routes.register(app)


# SPA fallback ------------------------------------------------------------------

@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    """Serve a real static file, else the SPA shell (never cached)."""
    if full_path.startswith(("api/", "api")):
        return JSONResponse(status_code=404, content={"code": "not_found", "message": "Unknown API endpoint."})
    candidate = safe_static_path(full_path)
    if candidate is not None:
        return FileResponse(candidate)
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        # The shell ALWAYS comes from the server: it names the hashed bundle to
        # load, so a cached copy would keep serving an old interface after an
        # in-place upgrade (the bytes are identical even though the JS changed).
        # Hashed assets themselves stay cacheable; this is the tiny index only.
        return FileResponse(index, headers={"Cache-Control": "no-store"})
    return JSONResponse(status_code=503, content={
        "code": "frontend_missing",
        "message": "The frontend build was not found. Run: cd frontend && npm run build",
    })
