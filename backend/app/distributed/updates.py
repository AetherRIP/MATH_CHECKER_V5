"""Update management — server side of the update system (§27-§45).

Implemented here: update REQUESTS (Developer→devices), per-device STATUS
tracking, and HISTORY. The DEVICE-SIDE installer lives in
``device_updater.py`` in the agent's own process (§41: it must not depend on
the application it may have to repair).

Safety rules baked into the request model (§32/§42/§60):
* a request is validated on creation: version format, checksum format,
  scope shape, priority values. An invalid request is REJECTED, not stored.
* ``auto_install`` may only be requested for small/optional updates (§30);
  required updates always go through the explicit install path with
  backup → install → health-check → rollback protection (§31/§38).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid

from .. import db
from . import audit, signing, sync

VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")

# §29: the exact states a device reports (implemented in device_updater.py).
STATES = (
    "CURRENT", "UPDATE_AVAILABLE", "DOWNLOADING", "VERIFYING",
    "WAITING_FOR_IDLE", "INSTALLING", "HEALTH_CHECK", "UPDATED", "FAILED",
    "ROLLED_BACK", "RECOVERY_REQUIRED", "OFFLINE",
)

PRIORITIES = ("low", "normal", "high", "critical")
SCOPES_ALL = "ALL"
SCOPES = (SCOPES_ALL, "DEVICE", "ACCOUNT", "SCHOOL", "WORKERS")

# Release lifecycle (§release management). A release is an update request that
# has been DRAFTED, VALIDATED and PUBLISHED — drafting and publishing are
# separate on purpose: an incomplete or invalid release must never become
# visible to a client merely because a row was inserted.
STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_WITHDRAWN = "withdrawn"
STATUSES = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_WITHDRAWN)

CHANNELS = ("stable", "beta")
# A release that names no platform is offered to every platform it can run on;
# a release that names one is only ever offered to that platform (§12).
PLATFORMS = ("windows", "linux", "darwin", "any")

COMPONENTS = ("app", "agent", "worker", "update_service")


def version_tuple(version: str | None) -> tuple[int, ...] | None:
    """Parse ``1.8`` / ``1.8.4`` into a comparable tuple, or None if invalid."""
    if not version or not VERSION_RE.match(str(version).strip()):
        return None
    return tuple(int(part) for part in str(version).strip().split("."))


def create_request(*, target_version: str, created_by: str,
                   target_component: str = "app",
                   minimum_supported_version: str | None = None,
                   release_notes: str | None = None,
                   priority: str = "normal", required: bool = False,
                   auto_install: bool = False,
                   scope: dict | None = None,
                   package_sha256: str | None = None,
                   package_url: str | None = None,
                   package_signature: str | None = None,
                   signing_key_id: str | None = None,
                   signing_public_key: str | None = None,
                   channel: str = "stable",
                   status: str = STATUS_PUBLISHED,
                   platform: str | None = None,
                   arch: str | None = None) -> dict:
    if not VERSION_RE.match(target_version or ""):
        raise ValueError("target_version must look like 1.8.4")
    if target_component not in COMPONENTS:
        raise ValueError("unknown target_component")
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}")
    if (channel or "").lower() not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    if (status or "").lower() not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    channel = channel.lower()
    status = status.lower()
    if platform is not None and str(platform).lower() not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}")
    platform = str(platform).lower() if platform else None
    if minimum_supported_version and not VERSION_RE.match(minimum_supported_version):
        raise ValueError("minimum_supported_version must look like 1.8.4")
    if package_sha256 and not re.match(r"^[0-9a-f]{64}$",
                                       package_sha256.lower().strip()):
        raise ValueError("package_sha256 must be a 64-char hex SHA-256")
    scope = scope or {"kind": SCOPES_ALL}
    if scope.get("kind") not in SCOPES:
        raise ValueError(f"scope.kind must be one of {SCOPES}")
    if auto_install and required:
        raise ValueError("a required update can never be auto-installed (§30)")
    # §19/§25: a real update request must describe a SIGNED package. The
    # signature is validated on the device against a PINNED key — the key
    # carried here is informational only and is checked for consistency.
    if package_signature is not None:
        try:
            base64.b64decode(package_signature, validate=True)
        except (ValueError, TypeError):
            raise ValueError("package_signature must be valid base64")
    if signing_public_key and signing_key_id and \
            signing.key_id(signing_public_key) != signing_key_id:
        raise ValueError("signing_key_id does not match the signing public key")

    update_id = f"upd-{uuid.uuid4().hex[:12]}"
    now = time.time()
    db.execute(
        """INSERT INTO update_requests(update_id, target_version,
           target_component, minimum_supported_version, release_notes, priority,
           required, auto_install, scope, created_by, created_at,
           package_sha256, package_url, package_signature, signing_key_id,
           signing_public_key, channel, status, platform, arch, published_at,
           updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (update_id, target_version, target_component,
         minimum_supported_version, release_notes, priority,
         1 if required else 0, 1 if auto_install else 0,
         json.dumps(scope, ensure_ascii=False), created_by, now,
         (package_sha256 or "").lower() or None, package_url,
         package_signature, signing_key_id, signing_public_key,
         channel, status, platform, arch,
         now if status == STATUS_PUBLISHED else None, now))
    audit.record(action="update.requested", target_type="update",
                 target_id=update_id,
                 actor_account=created_by,
                 details={"target_version": target_version,
                          "scope": scope, "required": required,
                          "channel": channel, "status": status})
    # Only a PUBLISHED release is announced on the sync stream — a draft is the
    # Developer's private work-in-progress and must not appear to clients.
    if status == STATUS_PUBLISHED:
        sync.append_event(
            type_="update.request", payload={"update_id": update_id,
                                             "target_version": target_version,
                                             "required": required,
                                             "auto_install": auto_install},
            client_event_id=f"update-request-{update_id}")
    return get_request(update_id)


def get_request(update_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM update_requests WHERE update_id = ?",
                       (update_id,))
    if not row:
        return None
    return _hydrate(row)


def _hydrate(row) -> dict:
    item = dict(row)
    item["scope"] = json.loads(item.get("scope") or "{}")
    item["required"] = bool(item.get("required"))
    item["auto_install"] = bool(item.get("auto_install"))
    # Old rows (and rows written before the lifecycle existed) default sensibly
    # rather than reporting a NULL status to the UI.
    item["status"] = (item.get("status") or STATUS_PUBLISHED).lower()
    item["channel"] = (item.get("channel") or "stable").lower()
    item["has_package"] = package_available(item["update_id"])
    return item


def list_requests() -> list[dict]:
    rows = db.query("SELECT * FROM update_requests ORDER BY created_at DESC")
    return [_hydrate(row) for row in rows]


def request_applies(request: dict, device_id: str) -> bool:
    scope = request.get("scope") or {}
    kind = scope.get("kind", SCOPES_ALL)
    if kind == SCOPES_ALL:
        return True
    if kind == "DEVICE":
        return device_id in (scope.get("device_ids") or [])
    if kind == "WORKERS":
        device = db.query_one("SELECT device_id FROM devices WHERE device_id = ?",
                              (device_id,))
        if not device:
            return False
        workers = db.query("SELECT worker_id FROM workers WHERE device_id = ?",
                           (device_id,))
        ids = [w["worker_id"] for w in workers]
        return any(w in (scope.get("worker_ids") or []) for w in ids)
    # ACCOUNT scope: does any worker on this device run that account?
    if kind == "ACCOUNT":
        rows = db.query("SELECT account_id FROM workers WHERE device_id = ?",
                        (device_id,))
        return any(r["account_id"] in (scope.get("account_ids") or [])
                   for r in rows)
    # SCHOOL scope: workers whose account is scoped to the school.
    if kind == "SCHOOL":
        rows = db.query("SELECT account_id FROM workers WHERE device_id = ?",
                        (device_id,))
        for r in rows:
            if not r["account_id"]:
                continue
            hit = db.query_one(
                "SELECT 1 FROM account_scope WHERE account_id = ? AND school_id = ?",
                (r["account_id"], scope.get("school_id")))
            if hit:
                return True
    return False


def device_context(device_id: str) -> tuple[str | None, str | None]:
    """(current_version, platform) for a device, from what it last reported.

    The version is the HIGHEST app version any of the device's workers has
    reported — a device with several installations is only offered an update
    that is newer than all of them. Platform is unknown (None) unless a device
    row records it, in which case a platform-specific release is filtered out
    for other platforms by :func:`release_compatible`.
    """
    from . import agents
    versions, platform = [], None
    for worker in agents.list_workers():
        if worker.get("device_id") != device_id:
            continue
        parsed = version_tuple(worker.get("app_version"))
        if parsed:
            versions.append(parsed)
    row = db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    if row:
        columns = set(row.keys())
        if "platform" in columns and row["platform"]:
            platform = str(row["platform"]).lower()
    current = ".".join(str(p) for p in max(versions)) if versions else None
    return current, platform


def release_compatible(request: dict, *, current_version: str | None = None,
                       platform: str | None = None) -> bool:
    """Is this release offered to a device at ``current_version``?

    Four independent gates, each of which must pass (§4/§7/§12):
      * the release is PUBLISHED (a draft or a withdrawn release is never sent);
      * its platform matches the device (``any``/None matches everything);
      * it is not the version the device is already running;
      * it is not a DOWNGRADE — an older version must never be pushed over a
        newer one by accident.
    """
    if (request.get("status") or STATUS_PUBLISHED).lower() != STATUS_PUBLISHED:
        return False
    req_platform = (request.get("platform") or "any").lower()
    if req_platform not in ("any", "") and platform and req_platform != platform.lower():
        return False
    target = version_tuple(request.get("target_version"))
    if target is None:
        return False
    current = version_tuple(current_version)
    if current is None:
        return True          # device version unknown -> offer it (it can refuse)
    if target == current:
        return False         # already on this version
    if target < current:
        return False         # never downgrade a device automatically
    return True


def requests_for_device(device_id: str, *, current_version: str | None = None,
                        platform: str | None = None) -> list[dict]:
    """Newest-first releases that actually apply to this device.

    Filtered by publication status, platform, version and scope so a Windows
    device is never offered a package built for another platform, and a
    withdrawn release stops being offered (§6/§12). When the caller does not
    supply the device's version/platform they are derived from what the device
    last REPORTED — never from a value the client asserts.
    """
    # BOTH the version and the platform are taken from what the device last
    # REPORTED. Deriving only the version left `platform` as None, which made
    # release_compatible's platform gate a no-op — so a Linux-only release was
    # offered to a Windows installation. Fixed here (the single call site that
    # serves /updates/for-device).
    ctx_version, ctx_platform = device_context(device_id)
    if current_version is None:
        current_version = ctx_version
    if platform is None:
        platform = ctx_platform
    pending = [r for r in list_requests()
               if request_applies(r, device_id)
               and release_compatible(r, current_version=current_version,
                                      platform=platform)]
    pending.sort(key=lambda r: version_tuple(r.get("target_version")) or (0,),
                 reverse=True)
    return pending


# -- release lifecycle (§release management) -----------------------------------

#: The canonical on-the-wire package shape (``device_updater.Package``).
_PACKAGE_KEYS = ("target_version", "files")


def inspect_package(data: bytes) -> dict:
    """Structural inspection of a candidate package WITHOUT executing it.

    A package is the canonical JSON the device reconstructs into a
    ``Package``: ``{"target_version": ..., "files": {path: hex-bytes}}``. We
    only ever parse and inspect these bytes here — the control plane never
    runs uploaded code to validate it.
    """
    problems: list[str] = []
    info: dict = {"files": 0, "size": len(data or b""), "target_version": None}
    if not data:
        return {"ok": False, "problems": ["package is empty"], "info": info}
    if len(data) > MAX_PACKAGE_BYTES:
        return {"ok": False, "problems": ["package exceeds the size limit"],
                "info": info}
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": False,
                "problems": ["package is not the expected UTF-8 JSON manifest"],
                "info": info}
    if not isinstance(payload, dict):
        return {"ok": False, "problems": ["package manifest must be an object"],
                "info": info}
    missing = [k for k in _PACKAGE_KEYS if k not in payload]
    if missing:
        problems.append(f"manifest is missing {', '.join(missing)}")
    if payload.get("target_version") is not None:
        info["target_version"] = str(payload["target_version"])
        if not VERSION_RE.match(str(payload["target_version"])):
            problems.append("manifest target_version is not a valid version")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        problems.append("package contains no files")
    else:
        info["files"] = len(files)
        for rel in files:
            parts = str(rel).replace("\\", "/").split("/")
            if str(rel).startswith("/") or ".." in parts or not str(rel).strip():
                problems.append(f"unsafe path in package: {rel!r}")
    return {"ok": not problems, "problems": problems, "info": info}


def validate_release(update_id: str) -> dict:
    """Validate a release before it may be published.

    Checks the metadata, the stored package's STRUCTURE and checksum, the
    package's own manifest version, and (for a republish) records what is
    missing. Nothing here publishes — it only reports.
    """
    request = get_request(update_id)
    if request is None:
        raise ValueError("release not found")
    problems: list[str] = []
    if version_tuple(request.get("target_version")) is None:
        problems.append("target version is not a valid version")
    if (request.get("target_component") or "") not in COMPONENTS:
        problems.append("target component is unknown")
    if not (request.get("channel") or "") in CHANNELS:
        problems.append("channel is unknown")
    package = get_package(update_id)
    if package is None:
        problems.append("no package has been uploaded")
        info = {"files": 0, "size": 0, "target_version": None}
    else:
        inspected = inspect_package(package["content"])
        problems.extend(inspected["problems"])
        info = inspected["info"]
        digest = hashlib.sha256(package["content"]).hexdigest()
        if digest != package["sha256"]:
            problems.append("stored package checksum does not match its bytes")
        if request.get("package_sha256") and \
                digest != request["package_sha256"].lower():
            problems.append("package checksum does not match the release record")
        if info["target_version"] and \
                version_tuple(info["target_version"]) != \
                version_tuple(request.get("target_version")):
            problems.append("package manifest version differs from the release "
                            "version")
    signature = request.get("package_signature")
    if not signature:
        problems.append("package is not signed (a signature is required to "
                        "establish the publisher)")
    elif not request.get("signing_key_id"):
        problems.append("package signature has no signing key id")
    return {
        "ok": not problems,
        "problems": problems,
        "update_id": update_id,
        "target_version": request.get("target_version"),
        "channel": request.get("channel"),
        "status": request.get("status"),
        "package": info,
        "sha256": (get_package(update_id) or {}).get("sha256"),
    }


def _published_versions(*, component: str = "app", channel: str | None = None,
                        platform: str | None = None) -> list[tuple]:
    out = []
    for request in list_requests():
        if request.get("status") != STATUS_PUBLISHED:
            continue
        if (request.get("target_component") or "app") != component:
            continue
        if channel and request.get("channel") != channel:
            continue
        if platform and (request.get("platform") or "any") not in (platform, "any"):
            continue
        parsed = version_tuple(request.get("target_version"))
        if parsed:
            out.append(parsed)
    return out


def publish_release(update_id: str, *, actor_account: str | None = None) -> dict:
    """Publish a (validated) release so clients may discover it.

    Publishing is a separate, explicit step: an invalid or incomplete release
    is REFUSED here rather than being made available to clients. Publishing
    never downgrades the recommended pointer and never rewrites an existing
    published release in place (§6).
    """
    request = get_request(update_id)
    if request is None:
        raise ValueError("release not found")
    if request["status"] == STATUS_PUBLISHED and package_available(update_id):
        raise ValueError("this release is already published")
    validation = validate_release(update_id)
    if not validation["ok"]:
        raise ValueError("release is not valid: " + "; ".join(validation["problems"]))
    target = version_tuple(request["target_version"])
    identical = [v for v in _published_versions(
        component=request.get("target_component") or "app",
        channel=request.get("channel")) if v == target]
    if identical:
        raise ValueError(f"a release for {request['target_version']} is already "
                         "published on this channel; do not silently overwrite it")
    now = time.time()
    db.execute(
        "UPDATE update_requests SET status = ?, published_at = COALESCE(published_at, ?),"
        " withdrawn_at = NULL, updated_at = ? WHERE update_id = ?",
        (STATUS_PUBLISHED, now, now, update_id))
    audit.record(action="update.published", target_type="update",
                 target_id=update_id, actor_account=actor_account,
                 details={"target_version": request["target_version"],
                          "channel": request["channel"]})
    sync.append_event(
        type_="update.request", payload={"update_id": update_id,
                                         "target_version": request["target_version"],
                                         "required": request.get("required"),
                                         "channel": request["channel"]},
        client_event_id=f"update-publish-{update_id}")
    return get_request(update_id)


def withdraw_release(update_id: str, *, actor_account: str | None = None,
                     reason: str | None = None) -> dict:
    """Withdraw a release from FUTURE distribution.

    History and audit survive, and nothing is uninstalled: a device that
    already updated keeps the version it has (§6). The row is never deleted.
    """
    request = get_request(update_id)
    if request is None:
        raise ValueError("release not found")
    if request["status"] == STATUS_WITHDRAWN:
        raise ValueError("this release is already withdrawn")
    now = time.time()
    db.execute(
        "UPDATE update_requests SET status = ?, withdrawn_at = ?, updated_at = ?"
        " WHERE update_id = ?", (STATUS_WITHDRAWN, now, now, update_id))
    audit.record(action="update.withdrawn", target_type="update",
                 target_id=update_id, actor_account=actor_account,
                 details={"target_version": request["target_version"],
                          "reason": reason})
    sync.append_event(
        type_="update.withdraw", payload={"update_id": update_id,
                                          "target_version": request["target_version"]},
        client_event_id=f"update-withdraw-{update_id}")
    return get_request(update_id)


def edit_release(update_id: str, *, release_notes: str | None = None,
                 minimum_supported_version: str | None = None,
                 actor_account: str | None = None) -> dict:
    """Edit a release's metadata. The version, channel and package are
    immutable — editing them would silently change what clients receive."""
    request = get_request(update_id)
    if request is None:
        raise ValueError("release not found")
    if minimum_supported_version and not VERSION_RE.match(minimum_supported_version):
        raise ValueError("minimum_supported_version must look like 1.8.4")
    db.execute(
        "UPDATE update_requests SET release_notes = COALESCE(?, release_notes),"
        " minimum_supported_version = COALESCE(?, minimum_supported_version),"
        " updated_at = ? WHERE update_id = ?",
        (release_notes, minimum_supported_version, time.time(), update_id))
    audit.record(action="update.edited", target_type="update",
                 target_id=update_id, actor_account=actor_account,
                 details={"target_version": request["target_version"]})
    return get_request(update_id)


def recommended_version(*, component: str = "app", channel: str = "stable") -> str | None:
    """The highest PUBLISHED version on a channel — what a fresh client targets."""
    versions = _published_versions(component=component, channel=channel)
    return ".".join(str(p) for p in max(versions)) if versions else None


def rollback_recommended(update_id: str, *, actor_account: str | None = None) -> dict:
    """Point the recommended release back to an earlier published version.

    This does NOT touch installed software and does not delete anything: it
    withdraws the newer published releases on the same component+channel so
    future offers fall back to the earlier one (§6 'roll back the recommended
    release'). Already-updated installs are left exactly as they are.
    """
    request = get_request(update_id)
    if request is None:
        raise ValueError("release not found")
    target = version_tuple(request["target_version"])
    if target is None:
        raise ValueError("release version is not valid")
    if request["status"] != STATUS_PUBLISHED:
        raise ValueError("only a published release can become the recommended "
                         "version")
    now = time.time()
    newer = [r for r in list_requests()
             if r.get("status") == STATUS_PUBLISHED
             and (r.get("target_component") or "app") ==
                 (request.get("target_component") or "app")
             and r.get("channel") == request.get("channel")
             and (version_tuple(r.get("target_version")) or (0,)) > target]
    for r in newer:
        db.execute("UPDATE update_requests SET status = ?, withdrawn_at = ?,"
                   " updated_at = ? WHERE update_id = ?",
                   (STATUS_WITHDRAWN, now, now, r["update_id"]))
        audit.record(action="update.withdrawn", target_type="update",
                     target_id=r["update_id"], actor_account=actor_account,
                     details={"reason": f"rolled back to {request['target_version']}"})
    audit.record(action="update.rollback", target_type="update",
                 target_id=update_id, actor_account=actor_account,
                 details={"recommended": request["target_version"],
                          "withdrew": [r["update_id"] for r in newer]})
    return {"ok": True, "recommended": request["target_version"],
            "withdrew": [r["update_id"] for r in newer],
            "release": get_request(update_id)}


# -- read models for the Developer dashboard -----------------------------------

#: Device-reported update states that mean something went wrong (§6/§29).
_FAILED_STATES = ("FAILED", "RECOVERY_REQUIRED", "ROLLED_BACK")
_DOWNLOADING_STATES = ("DOWNLOADING",)
_INSTALLING_STATES = ("VERIFYING", "INSTALLING", "HEALTH_CHECK", "WAITING_FOR_IDLE")
#: A progress row older than this says nothing about the device right now.
_PROGRESS_WINDOW_SECONDS = 24 * 3600


def _latest_device_states(device_ids: list[str], now: float) -> dict[str, str]:
    """Newest update state each device REPORTED, ignoring stale rows.

    One query for the whole fleet (not one per device), and only rows a device
    actually sent — the dashboard never invents progress.
    """
    ids = [d for d in {d for d in device_ids if d}]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = db.query(
        f"SELECT device_id, state, updated_at FROM update_device_status "
        f"WHERE device_id IN ({placeholders}) ORDER BY updated_at DESC",
        tuple(ids))
    out: dict[str, str] = {}
    for row in rows:
        device_id = row["device_id"]
        if device_id in out:
            continue          # first row per device is the newest
        if now - float(row["updated_at"] or 0) > _PROGRESS_WINDOW_SECONDS:
            continue          # too old to describe the present
        out[device_id] = row["state"]
    return out


def install_status(now: float | None = None) -> dict:
    """Per-installation update status across the fleet.

    An installation that has not reported recently is OFFLINE — it is NEVER
    counted as 'up to date' just because its stored version happens to match
    the latest release (§4: an offline installation must not be reported as up
    to date).
    """
    from . import agents
    now = now if now is not None else time.time()
    latest = recommended_version(channel="stable") or recommended_version(channel="beta")
    latest_t = version_tuple(latest)
    workers = agents.list_workers()
    # The device's OWN reported progress, not a guess from its version. Only the
    # newest status per device counts, and only while it is still meaningful.
    reported = _latest_device_states([w.get("device_id") for w in workers], now)
    states: dict[str, list[dict]] = {}
    for worker in workers:
        presence = worker.get("presence") or agents.effective_status(worker, now)
        version = worker.get("app_version")
        vt = version_tuple(version)
        # A silent device is OFFLINE — it must not be shown mid-download from a
        # stale row, nor counted as up to date (§4).
        if presence == agents.PRESENCE_OFFLINE:
            state = "OFFLINE"
        elif reported.get(worker.get("device_id")) in _FAILED_STATES:
            state = "FAILED"
        elif reported.get(worker.get("device_id")) in _DOWNLOADING_STATES:
            state = "DOWNLOADING"
        elif presence in (agents.PRESENCE_UPDATING,) or \
                reported.get(worker.get("device_id")) in _INSTALLING_STATES:
            state = "INSTALLING"
        elif latest_t is None or vt is None:
            state = "UNKNOWN"
        elif vt >= latest_t:
            state = "UP_TO_DATE"
        else:
            state = "UPDATE_AVAILABLE"
        states.setdefault(state, []).append({
            "worker_id": worker.get("worker_id"),
            "device_id": worker.get("device_id"),
            "device_label": worker.get("device_label"),
            "account_name": worker.get("account_name"),
            "app_version": version, "presence": presence,
            "reported_state": reported.get(worker.get("device_id")),
            "last_heartbeat": worker.get("last_heartbeat"),
        })
    counts = {k: len(v) for k, v in states.items()}
    return {
        "latest_version": latest,
        "counts": counts,
        "total": sum(counts.values()),
        "installations": states,
    }


def overview(now: float | None = None) -> dict:
    """Everything the Developer dashboard header needs, from real data."""
    from . import agents
    now = now if now is not None else time.time()
    requests = list_requests()
    published = [r for r in requests if r["status"] == STATUS_PUBLISHED]
    stable = recommended_version(channel="stable")
    beta = recommended_version(channel="beta")
    latest = None
    if published:
        latest = max(published, key=lambda r: version_tuple(r.get("target_version")) or (0,))
    installs = install_status(now)
    counts = installs["counts"]
    workers = agents.list_workers()
    reporting = sum(1 for w in workers
                    if (w.get("presence") or agents.effective_status(w, now))
                    != agents.PRESENCE_OFFLINE)
    # Pending/failed installs come from the real device-status rows, never
    # invented: only states a device actually reported are counted.
    pending_states = ("UPDATE_AVAILABLE", "DOWNLOADING", "VERIFYING",
                      "WAITING_FOR_IDLE")
    failed_states = ("FAILED", "RECOVERY_REQUIRED", "ROLLED_BACK")
    rows = db.query("SELECT state, COUNT(*) n FROM update_device_status GROUP BY state")
    by_state = {r["state"]: int(r["n"]) for r in rows}
    return {
        "latest": ({
            "update_id": latest["update_id"],
            "version": latest["target_version"],
            "channel": latest["channel"],
            "published_at": latest.get("published_at") or latest.get("created_at"),
            "release_notes": latest.get("release_notes"),
            "required": latest.get("required"),
            "package_size": (get_package(latest["update_id"]) or {}).get("size"),
        } if latest else None),
        "stable_version": stable,
        "beta_version": beta,
        "channels": [c for c in CHANNELS if any(
            r["channel"] == c and r["status"] == STATUS_PUBLISHED for r in requests)],
        "counts": {
            "registered_installations": len(workers),
            "reporting": reporting,
            "up_to_date": counts.get("UP_TO_DATE", 0),
            "outdated": counts.get("UPDATE_AVAILABLE", 0),
            "installing": counts.get("INSTALLING", 0) + counts.get("DOWNLOADING", 0),
            "offline": counts.get("OFFLINE", 0),
            "unknown": counts.get("UNKNOWN", 0),
            "failed": sum(by_state.get(s, 0) for s in failed_states),
            "pending": sum(by_state.get(s, 0) for s in pending_states),
            "draft_releases": sum(1 for r in requests if r["status"] == STATUS_DRAFT),
            "withdrawn_releases": sum(1 for r in requests
                                      if r["status"] == STATUS_WITHDRAWN),
            "total_releases": len(requests),
        },
        "install_status": installs,
        "generated_at": now,
    }


def set_device_status(update_id: str, device_id: str, state: str,
                      detail: str | None = None) -> None:
    if state not in STATES:
        raise ValueError(f"unknown update state {state}")
    db.execute(
        """INSERT INTO update_device_status(update_id, device_id, state,
           detail, updated_at) VALUES(?,?,?,?,?)
           ON CONFLICT(update_id, device_id) DO UPDATE SET state = excluded.state,
             detail = excluded.detail, updated_at = excluded.updated_at""",
        (update_id, device_id, state, (detail or "")[:500], time.time()))


def device_statuses(update_id: str) -> list[dict]:
    rows = db.query(
        "SELECT * FROM update_device_status WHERE update_id = ? ORDER BY updated_at DESC",
        (update_id,))
    out = []
    for row in rows:
        item = dict(row)
        item["freshness"] = _status_freshness(item)
        out.append(item)
    return out


def _status_freshness(item: dict) -> str:
    age = time.time() - float(item.get("updated_at") or 0)
    if age < 60:
        return "live"
    if age < 24 * 3600:
        return "recent"
    return "stale"


def record_history(*, device_id: str, update_id: str | None, old_version: str,
                   new_version: str, result: str, started_at: float | None,
                   completed_at: float | None, failure_reason: str | None = None,
                   rollback_result: str | None = None) -> int:
    """Append-only history (§45): never overwrite an earlier attempt."""
    cur = db.execute(
        """INSERT INTO update_history(device_id, update_id, old_version,
           new_version, started_at, completed_at, result, failure_reason,
           rollback_result) VALUES(?,?,?,?,?,?,?,?,?)""",
        (device_id, update_id, old_version, new_version, started_at,
         completed_at, result, failure_reason, rollback_result))
    audit.record(action=f"update.{result.lower()}", target_type="update",
                 target_id=update_id, device_id=device_id,
                 details={"old_version": old_version, "new_version": new_version,
                          "failure_reason": failure_reason,
                          "rollback_result": rollback_result})
    return cur.lastrowid if cur else 0


# -- delivery (§19/§25): the signed package body -------------------------------

#: Upper bound on a stored package (the control plane refuses anything larger).
MAX_PACKAGE_BYTES = 200 * 1024 * 1024


def store_package(update_id: str, data: bytes) -> dict:
    """Store the signed package body for an update request.

    The body must match the request's ``package_sha256`` when one was set, so
    a control-plane record and its delivery payload can never disagree. The
    bytes are stored verbatim: the device verifies the SAME bytes it downloads.
    """
    request = get_request(update_id)
    if not request:
        raise ValueError("update request not found")
    if not data:
        raise ValueError("package is empty")
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("package exceeds the size limit")
    digest = hashlib.sha256(data).hexdigest()
    declared = (request.get("package_sha256") or "").lower()
    if declared and digest != declared:
        raise ValueError("package checksum does not match the request's "
                         "package_sha256")
    db.execute(
        """INSERT INTO update_packages(update_id, size, sha256, content, created_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(update_id) DO UPDATE SET size = excluded.size,
             sha256 = excluded.sha256, content = excluded.content,
             created_at = excluded.created_at""",
        (update_id, len(data), digest, data, time.time()))
    if not request.get("package_sha256"):
        # Record the real digest so the device's checksum check is meaningful.
        db.execute("UPDATE update_requests SET package_sha256 = ? WHERE update_id = ?",
                   (digest, update_id))
    audit.record(action="update.package_stored", target_type="update",
                 target_id=update_id, details={"size": len(data),
                                               "sha256": digest})
    return {"ok": True, "update_id": update_id, "size": len(data),
            "sha256": digest}


def get_package(update_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM update_packages WHERE update_id = ?",
                       (update_id,))
    return dict(row) if row else None


def package_available(update_id: str) -> bool:
    return db.query_one("SELECT 1 FROM update_packages WHERE update_id = ?",
                        (update_id,)) is not None


def list_history(device_id: str | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    if device_id:
        rows = db.query(
            "SELECT * FROM update_history WHERE device_id = ? "
            "ORDER BY id DESC LIMIT ?", (device_id, limit))
    else:
        rows = db.query("SELECT * FROM update_history ORDER BY id DESC LIMIT ?",
                        (limit,))
    return [dict(r) for r in rows]
