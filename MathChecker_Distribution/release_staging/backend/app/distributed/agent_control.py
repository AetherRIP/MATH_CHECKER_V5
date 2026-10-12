"""Tiny Agent ON/OFF control (§extension: local agent control).

The Tiny Agent runs as its own lightweight process, so the switch that pauses
it cannot live only in the backend's memory — the agent has to be able to read
the same answer, *without* the backend being alive (the agent must keep working,
and keep honouring a pause, even when the main app fails to start after an
update).

Design decisions, each deliberate:

* **A local file is the single source of truth.** ``agent_state.json`` sits in
  the agent directory next to ``device_id`` (the existing persistent-identity
  file). Both the backend and the agent process read it, so they can never
  disagree, and the preference survives restarts, reboots and updates.
* **OFF is durable.** The file is written atomically (tmp + os.replace) so a
  crash mid-write cannot leave a half-written "enabled" state behind.
* **Local, never global.** The file is per installation. Switching the agent
  off on one laptop writes only that laptop's file, so it can never disable the
  teacher's or another assistant's agent (§extension 5).
* **Fail-safe direction.** A missing or unreadable file means ENABLED, which
  preserves the behaviour of every installation that existed before this
  switch. A corrupt file is reported in the returned state so the UI can say
  "status unknown" instead of silently lying.
* **Pausing never touches corrections.** This module only decides whether the
  agent sends heartbeats and transfers; the marking engine, the job pool and
  Google Classroom know nothing about it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATE_FILENAME = "agent_state.json"

# ---------------------------------------------------------------- availability
#
# THE PRODUCT SWITCH (§1 of the simplification work): this version does not use
# the Tiny Agent. ``[agent] tiny_agent`` in ``config.toml`` is the single source
# of truth, defaulting to FALSE, with ``MC_AGENT_ENABLED`` as an explicit
# operator/test override that wins.
#
# The distinction that matters:
#   * ``is_enabled()`` / the state file  -> does THIS installation want the agent
#     to run, historically (a user preference).
#   * ``product_enabled()``             -> does this VERSION run an agent at all?
#
# While the product switch is OFF nothing may heartbeat, poll, flush a queue or
# download an update, no matter what the per-installation preference says, and
# the installers refuse to recreate an auto-start entry (otherwise the agent
# would silently come back after a restart or an update). Nothing is deleted:
# the module, the tables, the records and the endpoints all stay.
CONFIG_SECTION = "agent"
CONFIG_KEY = "tiny_agent"
ENV_VAR = "MC_AGENT_ENABLED"
DEFAULT_ENABLED = False

# Status labels the UI shows. Kept here (not in the front end) so the backend
# and the agent describe the same state with the same words.
LABEL_RUNNING = "Agent Running"
LABEL_PAUSED = "Agent Paused"
LABEL_STARTING = "Agent Starting"
LABEL_NOT_REPORTING = "Agent Not Reporting"
LABEL_UNKNOWN = "Status Unknown"
LABEL_DISABLED = "Disabled in this version"

_TRUTHY = {"1", "true", "yes", "on", "enabled"}
_FALSY = {"0", "false", "no", "off", "disabled"}

#: How to switch the Tiny Agent back on (used in refusals and in the docs, so
#: the message a user sees and the documentation cannot drift apart).
REENABLE_HINT = (
    f"Set [{CONFIG_SECTION}] {CONFIG_KEY} = true in config.toml (or "
    f"{ENV_VAR}=1) and restart the application; the source, the tables and "
    "the records were never removed.")


def _env_value() -> bool | None:
    raw = (os.environ.get(ENV_VAR) or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return None


def _config_value() -> bool | None:
    try:
        from ..config import get_config
        return bool(get_config().agent.tiny_agent)
    except Exception:  # noqa: BLE001 — an unreadable config keeps the default
        return None


def product_enabled() -> bool:
    """Does this installation run the Tiny Agent? (env > config > default OFF)."""
    env = _env_value()
    if env is not None:
        return env
    config = _config_value()
    return DEFAULT_ENABLED if config is None else config


def product_source() -> str:
    """Where the effective availability came from: env / config / default."""
    if _env_value() is not None:
        return "env"
    config = _config_value()
    if config is None or config == DEFAULT_ENABLED:
        return "default"
    return "config"


def product_capabilities() -> dict:
    """Availability payload for clients (no secrets, no records).

    ``available`` is the product switch; ``setting`` names the exact key so an
    operator reading the UI or the docs finds the same string.
    """
    return {
        "available": product_enabled(),
        "default": DEFAULT_ENABLED,
        "source": product_source(),
        "setting": f"[{CONFIG_SECTION}] {CONFIG_KEY}",
        "env_var": ENV_VAR,
        "label": LABEL_RUNNING if product_enabled() else LABEL_DISABLED,
        "restart_required": True,
        "reenable": REENABLE_HINT,
    }


def require_product_enabled() -> None:
    """Raise the standard refusal when an agent-only action is attempted."""
    if product_enabled():
        return
    from ..errors import AppError
    raise AppError(
        "The Tiny Agent is not used by this version of Math Checker, so this "
        "action is unavailable. Nothing was deleted and no correction, "
        "Classroom or sync feature depends on the agent. " + REENABLE_HINT,
        status=409, code="tiny_agent_disabled")

# §4: "a heartbeat received within the last two heartbeat intervals can be
# considered recently active". The agent heartbeats every 5 minutes, so two
# intervals is 10 minutes; we allow 11 to match the presence window used by the
# distributed layer, so the switch and the worker list can never disagree.
RECENTLY_ACTIVE_SECONDS = 11 * 60

VALID_REASONS = ("user", "default", "startup")


def agent_dir(explicit: str | Path | None = None) -> Path:
    """Where the agent's own files live.

    ``MC_AGENT_DIR`` lets a deployment place the agent elsewhere; otherwise the
    repository's ``agent/`` folder is used, which is where the standalone
    ``agent.py`` and its ``device_id`` already live.
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("MC_AGENT_DIR")
    if env:
        return Path(env)
    # backend/app/distributed/agent_control.py -> project root
    return Path(__file__).resolve().parents[3] / "agent"


def state_path(agent_dir_path: str | Path | None = None) -> Path:
    return agent_dir(agent_dir_path) / STATE_FILENAME


def load_state(agent_dir_path: str | Path | None = None) -> dict:
    """Read the persisted preference.

    Returns ``{enabled, reason, updated_at, updated_by, corrupt}``. A missing
    file is not an error: it means "never switched off", i.e. enabled.
    """
    path = state_path(agent_dir_path)
    if not path.exists():
        return {"enabled": True, "reason": "default", "updated_at": None,
                "updated_by": None, "corrupt": False}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state file is not an object")
        return {
            "enabled": bool(raw.get("enabled", True)),
            "reason": str(raw.get("reason") or "user"),
            "updated_at": raw.get("updated_at"),
            "updated_by": raw.get("updated_by"),
            "corrupt": False,
        }
    except (OSError, ValueError, json.JSONDecodeError):
        # Unreadable file: keep the agent running (previous behaviour) but say
        # so, rather than pretending we know the user's choice.
        return {"enabled": True, "reason": "default", "updated_at": None,
                "updated_by": None, "corrupt": True}


def is_enabled(agent_dir_path: str | Path | None = None) -> bool:
    """Should the agent do any work right now?

    False while the Tiny Agent is unavailable in this version, whatever the
    stored preference says: a legacy installation (or an auto-start entry that
    survived an upgrade) must not keep heartbeating or polling.
    """
    if not product_enabled():
        return False
    return bool(load_state(agent_dir_path)["enabled"])


def save_state(enabled: bool, *, updated_by: str | None = None,
               reason: str = "user",
               agent_dir_path: str | Path | None = None) -> dict:
    """Persist ON/OFF atomically and return the stored state."""
    path = state_path(agent_dir_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(enabled),
        "reason": reason if reason in VALID_REASONS else "user",
        "updated_at": time.time(),
        "updated_by": updated_by,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return {**payload, "corrupt": False}


def status_label(state: dict, *, agent_seen_seconds_ago: float | None = None) -> str:
    """Human label for the switch.

    THE IMPORTANT RULE: a stored "enabled" flag is a *preference*, not evidence
    that anything is running. A heartbeat older than two intervals must never
    be reported as "running" — otherwise the UI would claim an agent is online
    just because the backend is up (§4: do not claim a machine is online merely
    because the server is).

    * corrupt state            -> status unknown
    * switched off             -> agent paused
    * on, never heard from     -> agent starting
    * on, heard recently       -> agent running
    * on, but heartbeat stale  -> agent not reporting
    """
    if state.get("corrupt"):
        return LABEL_UNKNOWN
    if not state.get("enabled"):
        return LABEL_PAUSED
    if agent_seen_seconds_ago is None:
        return LABEL_STARTING
    if float(agent_seen_seconds_ago) > RECENTLY_ACTIVE_SECONDS:
        return LABEL_NOT_REPORTING
    return LABEL_RUNNING


def describe(*, agent_dir_path: str | Path | None = None,
             last_heartbeat: float | None = None,
             now: float | None = None) -> dict:
    """Full state for the settings UI: preference + observed reality."""
    state = load_state(agent_dir_path)
    now = now if now is not None else time.time()
    seen = (now - float(last_heartbeat)) if last_heartbeat else None
    available = product_enabled()
    return {
        "enabled": state["enabled"],
        "reason": state["reason"],
        "updated_at": state["updated_at"],
        "updated_by": state["updated_by"],
        "corrupt": state["corrupt"],
        # The VERSION switch is reported separately from the per-installation
        # preference, so a client can say "not used in this version" instead of
        # showing a switch that would change nothing. The label follows it: a
        # disabled agent is never described as running, paused or starting.
        "product_enabled": available,
        "product": product_capabilities(),
        "label": (status_label(state, agent_seen_seconds_ago=seen)
                  if available else LABEL_DISABLED),
        "last_heartbeat": last_heartbeat,
        "seconds_since_heartbeat": seen,
    }
