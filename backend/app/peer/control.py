"""The peer ON/OFF switch — one file, and the truth about what it means.

This mirrors :mod:`app.distributed.agent_control`, deliberately: a local file is
the single source of truth so the backend and the peer process cannot disagree,
OFF is durable and atomic, and a corrupt file is *reported* rather than guessed
at.

Two rules matter more than the encoding:

* **OFF is the default.** Nothing about peer networking runs until someone
  switches it on, which is what "additive and inert" (plan §10.4) requires in
  practice: a fresh installation has no peer process, no state file and no
  listener.
* **A preference is not an observation.** ``describe()`` combines the stored
  preference with the *actual* service state, so the UI can never show "running"
  because a flag was set. A switched-on installation whose service is stopped is
  reported as stopped, with the reason.

No secrets, no device identifiers and no certificate material are written here.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATE_FILENAME = "peer_state.json"

#: Approved default (plan §13 Phase 2, D-series): the feature ships off.
DEFAULT_ENABLED = False

VALID_REASONS = ("user", "default", "startup")

# Reason codes the UI shows when no service state is available.
LABEL_OFF = "Peer networking off"
LABEL_STARTING = "Peer service starting"
LABEL_RUNNING = "Peer service running"
LABEL_STOPPED = "Peer service stopped"
LABEL_UNHEALTHY = "Peer service unhealthy"
LABEL_AUTH_FAILED = "Peer sign-in failed"
LABEL_CREDENTIALS_UNAVAILABLE = "Peer credentials unavailable"
LABEL_UNKNOWN = "Peer status unknown"


def _directory(explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from .identity import peer_dir

    return peer_dir()


def state_path(directory: str | Path | None = None) -> Path:
    return _directory(directory) / STATE_FILENAME


def load_state(directory: str | Path | None = None) -> dict:
    """Read the stored preference.

    A missing file is not an error: it means "never switched on", i.e. disabled.
    An unreadable file is reported with ``corrupt`` so the UI can say "status
    unknown" instead of pretending to know the user's choice.
    """
    path = state_path(directory)
    if not path.exists():
        return {"enabled": DEFAULT_ENABLED, "reason": "default",
                "updated_at": None, "updated_by": None, "corrupt": False}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state file is not an object")
        return {
            "enabled": bool(raw.get("enabled", DEFAULT_ENABLED)),
            "reason": str(raw.get("reason") or "user"),
            "updated_at": raw.get("updated_at"),
            "updated_by": raw.get("updated_by"),
            "corrupt": False,
        }
    except (OSError, ValueError):
        return {"enabled": DEFAULT_ENABLED, "reason": "default",
                "updated_at": None, "updated_by": None, "corrupt": True}


def is_enabled(directory: str | Path | None = None) -> bool:
    """Should this installation run the peer service at all?

    A corrupt file answers *no*: starting a service on a preference nobody can
    read would be acting on a guess. The state is still reported so the UI can
    ask the user to switch it on again.
    """
    state = load_state(directory)
    return bool(state["enabled"]) and not state["corrupt"]


def save_state(enabled: bool, *, updated_by: str | None = None,
               reason: str = "user",
               directory: str | Path | None = None) -> dict:
    """Persist ON/OFF atomically and return the stored state."""
    path = state_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled),
        "reason": reason if reason in VALID_REASONS else "user",
        "updated_at": time.time(),
        "updated_by": updated_by,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return {**payload, "corrupt": False}


def label_for_state(state: str | None) -> str:
    """The one place a service state becomes a human sentence."""
    return {
        "DISABLED": LABEL_OFF,
        "STARTING": LABEL_STARTING,
        "RUNNING": LABEL_RUNNING,
        "STOPPING": "Peer service stopping",
        "STOPPED": LABEL_STOPPED,
        "UNHEALTHY": LABEL_UNHEALTHY,
        "AUTH_FAILED": LABEL_AUTH_FAILED,
        "CREDENTIALS_UNAVAILABLE": LABEL_CREDENTIALS_UNAVAILABLE,
    }.get((state or "").upper(), LABEL_UNKNOWN)


def describe(*, service: dict | None = None,
             directory: str | Path | None = None) -> dict:
    """Preference + observed reality, for a status surface.

    ``service`` is :meth:`app.peer.service.PeerService.status` as a dict (or
    ``None`` when nothing supervises the process). The returned ``label`` always
    describes the combination, so a switched-on service that failed to start is
    never shown as running.
    """
    state = load_state(directory)
    enabled = bool(state["enabled"]) and not state["corrupt"]
    if state["corrupt"]:
        return {**state, "enabled": False, "product_enabled": True,
                "service": service, "state": "UNKNOWN", "label": LABEL_UNKNOWN,
                "detail": "the peer switch file could not be read; switch peer "
                          "networking on again to reset it"}
    if not enabled:
        return {**state, "product_enabled": True, "service": service,
                "state": "DISABLED", "label": LABEL_OFF,
                "detail": "peer networking is off; nothing is started"}
    observed = (service or {}).get("state") or "STOPPED"
    return {
        **state,
        "product_enabled": True,
        "service": service,
        "state": observed,
        "label": label_for_state(observed),
        "detail": (service or {}).get("detail") or "switched on; waiting for the service",
    }
