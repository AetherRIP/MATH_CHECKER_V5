"""Peer networking: the switch and the supervisor, as a desktop surface sees them.

Phase 2 has no network surface. These two routes start and stop a LOCAL child
process and report what is actually observed; nothing here opens a socket,
accepts a hostname, a path or a command, or reaches the network.

Two rules drive the design:

* **A preference is not an observation.** ``GET /status`` reports the stored
  switch separately from the process state, and ``POST /preference`` answers with
  a fresh observation — the caller can never learn "started" from a request that
  only recorded an intention.
* **The supervisor lives in this process.** A ``PeerService`` owns at most one
  child (enforced by a lock file inside the service), so a page that repaints,
  retries or double-clicks still results in one child. ``GET`` never constructs a
  service at all, so merely opening the page cannot spawn anything.

Loopback only: an installation that later exposes its API over the network must
not let a remote caller start processes on the teacher's machine.
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Request

from ..errors import AppError
from ..peer import control as peer_control
from ..peer import service as peer_service

router = APIRouter(prefix="/api/peer")

#: Hosts allowed to touch the switch. The desktop talks to 127.0.0.1; the
#: in-process test client identifies itself as "testclient".
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

_LOCK = threading.RLock()
_SERVICE: peer_service.PeerService | None = None


def _service() -> peer_service.PeerService:
    """The one supervisor for this process. Created lazily, never on a read."""
    global _SERVICE
    with _LOCK:
        if _SERVICE is None:
            _SERVICE = peer_service.PeerService()
        return _SERVICE


def reset_supervisor() -> None:
    """Drop the memoised supervisor (tests, and a future 'reload' action).

    Deliberately does not stop a child: the caller must ask for that first, so
    a reset can never orphan a process behind the operator's back.
    """
    global _SERVICE
    with _LOCK:
        _SERVICE = None


def _require_local(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host not in LOCAL_HOSTS:
        raise AppError("Peer controls are only available on this machine.",
                       status=403, code="local_only")


def _credentials_path():
    return peer_control.state_path().parent / "credentials.json"


def _snapshot() -> dict:
    """Preference + observed reality, with no key material anywhere."""
    with _LOCK:
        service = _SERVICE
        observed = service.status_dict() if service is not None else None
    described = peer_control.describe(service=observed)
    credentials = _credentials_path().exists()
    return {
        "enabled": described["enabled"],          # the stored preference
        "state": described["state"],              # what is observed right now
        "label": described["label"],
        "detail": described["detail"],
        "corrupt": described["corrupt"],
        "updated_at": described.get("updated_at"),
        "updated_by": described.get("updated_by"),
        # True only when THIS process owns a supervisor; a switch that is on in
        # a fresh process is "on, not started" rather than "running".
        "supervised": observed is not None,
        "service": observed,
        "credentials_present": credentials,
    }


@router.get("/status")
def peer_status() -> dict:
    """The stored switch and the observed state. Starts nothing."""
    return _snapshot()


@router.post("/preference")
def peer_preference(payload: dict, request: Request) -> dict:
    """Turn the peer service on or off, and answer with what is observed.

    ``confirmed`` is computed from the observation, not from the request: it is
    true only when enabling reached ``RUNNING`` or disabling reached
    ``STOPPED``/``DISABLED``. An enable that could not start (no credentials, an
    unprotected store, a crash) comes back with its real state and reason, and
    the stored preference stays on so the next launch tries again.
    """
    _require_local(request)
    if not isinstance(payload, dict) or "enabled" not in payload:
        raise AppError('Send {"enabled": true} or {"enabled": false}.',
                       status=400, code="bad_request")
    enabled = payload["enabled"]
    if not isinstance(enabled, bool):
        raise AppError("'enabled' must be true or false.", status=400,
                       code="bad_request")

    with _LOCK:
        try:
            peer_control.save_state(enabled, updated_by="desktop")
        except OSError as exc:
            # Nothing was written and nothing was started: say so plainly.
            raise AppError(f"The peer switch could not be saved: {exc}",
                           status=500, code="state_unwritable")

        service = _service()
        try:
            current = (service.status_dict().get("state")
                       or peer_service.STOPPED)
            if enabled:
                # A second enable must not spawn a second child: the service is
                # already supervising one.
                if current not in (peer_service.RUNNING, peer_service.STARTING):
                    service.start()
            elif current in (peer_service.RUNNING, peer_service.STARTING,
                             peer_service.UNHEALTHY):
                service.stop()
        except Exception as exc:                   # noqa: BLE001 - reported
            raise AppError(
                f"The peer service did not accept the request: {exc}",
                status=500, code="service_failed")

    snapshot = _snapshot()
    snapshot["requested"] = enabled
    snapshot["confirmed"] = (
        snapshot["state"] == peer_service.RUNNING if enabled
        else snapshot["state"] in (peer_service.STOPPED, peer_service.DISABLED))
    return snapshot


def register(app) -> None:
    app.include_router(router)
