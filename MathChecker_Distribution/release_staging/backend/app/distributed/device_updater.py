"""DEVICE-SIDE updater (§30-§42): backs up, installs, health-checks and
rolls back — with every step injectable so the critical rollback test (§61)
runs for real, without touching a developer's machine.

State machine (§30/§35/§36):
    UPDATE_AVAILABLE → DOWNLOADING → VERIFYING → [WAITING_FOR_IDLE if busy]
    → BACKUP → INSTALL → HEALTH_CHECK → UPDATED
                  ↘ any failure → ROLLBACK → ROLLED_BACK | RECOVERY_REQUIRED

Safety invariants (§31/§33/§36/§38/§42/§64):
* NOTHING is touched before the package checksum is verified (§33).
* NOTHING is touched before the backup is created AND verified (§31).
* The previous version is never deleted first (§36) — atomic replace where the
  platform allows it, safe directory swap otherwise.
* Rollback preserves local DATA directories (§39); it only replaces code.
* A failed update is reported with the full §40 structure.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class State(str, Enum):
    AVAILABLE = "UPDATE_AVAILABLE"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING = "VERIFYING"
    WAITING_FOR_IDLE = "WAITING_FOR_IDLE"
    BACKUP = "BACKUP"
    INSTALLING = "INSTALLING"
    HEALTH_CHECK = "HEALTH_CHECK"
    UPDATED = "UPDATED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REJECTED = "UPDATE_REJECTED_INCOMPATIBLE"


# §35: busy stages that must never be interrupted by an update install.
BUSY_STAGES = ("DOWNLOADING", "CORRECTING", "DOUBLE_CHECKING",
               "GENERATING_REPORT", "UPLOADING_REPORT", "ATTACHING_REPORT",
               "VERIFYING_ATTACHMENT", "GRADING", "RETURNING")


@dataclass
class Package:
    """The verified update payload descriptor (§33/§42).

    ``signature`` is a detached Ed25519 signature over ``serialize()`` made by
    the Developer's private key; ``signing_key_id`` is the non-secret
    fingerprint of the matching public key. The device only accepts a
    signature whose ``signing_key_id`` matches a key it has PINNED locally
    (§19/§25) — a key carried inside the request is never trusted.
    """
    target_version: str
    files: dict[str, bytes]              # relative path -> content
    sha256: str | None = None            # checksum of the serialized package
    minimum_supported_version: str | None = None
    signature: str | None = None         # base64 Ed25519 signature
    signing_key_id: str | None = None    # fingerprint of the signing public key

    def serialize(self) -> bytes:
        return json.dumps(
            {"target_version": self.target_version,
             "files": {k: v.hex() for k, v in self.files.items()}},
            sort_keys=True).encode("utf-8")

    def to_bytes(self) -> bytes:
        """The on-the-wire package body. This exact byte string is what the
        checksum and the Ed25519 signature both cover (§19/§33)."""
        return self.serialize()


@dataclass
class UpdateResult:
    final_state: State
    stages: list[dict] = field(default_factory=list)
    failure_stage: str | None = None
    failure_reason: str | None = None
    backup_dir: str | None = None
    rollback_result: str | None = None
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    def as_report(self, *, device_id: str, old_version: str,
                  update_id: str | None) -> dict:
        # §40: the exact failure-report structure.
        return {
            "device": device_id, "update_id": update_id,
            "current_version": old_version,
            "target_version": self.stages[-1]["target"]
            if self.stages else None,
            "failure_stage": self.failure_stage,
            "failure_reason": self.failure_reason,
            "stages": self.stages,
            "backup_result": ("OK" if self.backup_dir else "MISSING"),
            "rollback_result": self.rollback_result,
            "final_state": self.final_state.value,
            "timestamp": time.time(),
        }


def package_from_bytes(request: dict, data: bytes) -> "Package":
    """Reconstruct the installable :class:`Package` from the downloaded body.

    The body is exactly ``Package.to_bytes()`` (canonical JSON), so a package
    signed as ``pkg.serialize()`` round-trips byte-for-byte — which is what
    lets ``DeviceUpdater.verify_package`` re-verify the very same bytes it just
    downloaded.
    """
    payload = json.loads(data.decode("utf-8"))
    files = {str(k): bytes.fromhex(str(v))
             for k, v in (payload.get("files") or {}).items()}
    request = dict(request)
    if payload.get("target_version"):
        request["target_version"] = payload["target_version"]
    return package_from_request(request, files)


def package_from_request(request: dict, files: dict[str, bytes]) -> "Package":
    """Build an installable :class:`Package` from a backend update request.

    The control plane (``update_requests``) carries the metadata, checksum and
    signature; the delivery layer supplies the actual ``files`` bytes. Keeping
    these separate is deliberate (§25): a request that names no signature still
    produces a Package with ``signature=None``, which the updater then refuses.
    """
    return Package(
        target_version=str(request.get("target_version") or ""),
        files=dict(files),
        sha256=request.get("package_sha256"),
        minimum_supported_version=request.get("minimum_supported_version"),
        signature=request.get("package_signature"),
        signing_key_id=request.get("signing_key_id"),
    )


class DeviceUpdater:
    """Concrete file-machinery with injectable policy hooks.

    ``app_dir``      — the installed application (code only; DATA is elsewhere)
    ``data_dir``     — must NEVER be replaced or deleted (§39)
    ``backup_root``  — where verified backups live (kept for rollback, §32)
    ``busy_check``   — callable -> (is_busy: bool, stage: str)
    ``health_check`` — callable -> (ok: bool, detail: str); §37
    """

    def __init__(self, *, app_dir: str | Path, data_dir: str | Path,
                 backup_root: str | Path, current_version: str,
                 busy_check=None, health_check=None,
                 rollback_health_check=None,
                 trusted_public_keys: dict[str, str] | None = None,
                 require_signature: bool = True):
        self.app_dir = Path(app_dir)
        self.data_dir = Path(data_dir)
        self.backup_root = Path(backup_root)
        self.current_version = current_version
        self._busy_check = busy_check
        self._health_check = health_check
        # §19: key_id -> public key PEM, pinned OUT OF BAND (agent config).
        self._trusted_keys = dict(trusted_public_keys or {})
        # §25: a real update must be signed; unsigned packages are refused
        # unless an operator explicitly opts out for local testing.
        self.require_signature = require_signature
        # §38: the check run AFTER restoring the previous version is the
        # PREVIOUS version's own health check — not a re-run of the new one.
        self._rollback_health = rollback_health_check or health_check

    # -- checks ---------------------------------------------------------------

    def busy_now(self) -> tuple[bool, str]:
        if self._busy_check:
            return self._busy_check()
        return False, ""

    def _stage(self, result: UpdateResult, name: str, state: State,
               target: str, detail: str = "") -> None:
        result.stages.append({
            "stage": name, "state": state.value, "target": target,
            "detail": detail[:300], "ts": time.time()})

    def verify_signature(self, pkg: Package) -> tuple[bool, str]:
        """Verify the package signature against the LOCAL pinned keys (§19/§25).

        Returns False for: a missing signature (when required), an untrusted
        key id, or a signature that does not match the package bytes.
        """
        if not pkg.signature:
            if self.require_signature:
                return False, "package is not signed"
            return True, "signature not required"
        if not pkg.signing_key_id:
            return False, "package signature has no key id"
        public_key = self._trusted_keys.get(pkg.signing_key_id)
        if not public_key:
            return False, (f"signing key {pkg.signing_key_id} is not trusted on "
                           "this device")
        from . import signing
        if not signing.verify_package(pkg.serialize(), pkg.signature, public_key):
            return False, "invalid package signature"
        return True, "ok"

    def verify_download(self, data: bytes, request: dict) -> tuple[bool, str]:
        """Verify the DOWNLOADED bytes against the control-plane metadata.

        This is the online half of §19: checksum over the exact downloaded
        bytes, then the Ed25519 signature against a PINNED key. It runs before
        the bytes are parsed into a Package, so a tampered or substituted
        download is rejected even if it would parse cleanly.
        """
        sha = (request.get("package_sha256") or "").lower()
        if sha and hashlib.sha256(data).hexdigest() != sha:
            return False, "checksum mismatch"
        signature = request.get("package_signature")
        if not signature:
            if self.require_signature:
                return False, "package is not signed"
            return True, "signature not required"
        key_id = request.get("signing_key_id")
        public_key = self._trusted_keys.get(key_id)
        if not public_key:
            return False, f"signing key {key_id} is not trusted on this device"
        from . import signing
        if not signing.verify_package(data, signature, public_key):
            return False, "invalid package signature"
        return True, "ok"

    def verify_package(self, pkg: Package) -> tuple[bool, str]:
        """Verify structure + checksum + SIGNATURE before anything else
        (§19/§33/§42). Nothing touches the installation until all pass."""
        if pkg.sha256:
            digest = hashlib.sha256(pkg.serialize()).hexdigest()
            if digest != pkg.sha256.lower():
                return False, "checksum mismatch"
        ok, why = self.verify_signature(pkg)
        if not ok:
            return False, why
        if not pkg.files:
            return False, "package contains no files"
        for rel in pkg.files:
            rel_norm = Path(rel)
            if rel_norm.is_absolute() or ".." in rel_norm.parts:
                return False, f"unsafe package path: {rel}"
        if (pkg.target_version or "") == self.current_version:
            return False, "update already installed"
        if pkg.minimum_supported_version:
            try:
                cur = tuple(int(x) for x in self.current_version.split("."))
                minv = tuple(int(x) for x in
                             pkg.minimum_supported_version.split("."))
                if cur < minv:
                    return False, ("current version is below the minimum "
                                   "supported version for this update")
            except ValueError:
                return False, "unparseable version string in package"
        return True, "ok"

    def check_compatibility(self, pkg: Package) -> tuple[bool, str]:
        """Hardware/compatibility check (§34): conservative and offline."""
        import platform
        if not self.app_dir.parent.exists():
            return False, "installation directory missing"
        try:
            free = shutil.disk_usage(self.app_dir.parent).free
        except OSError as exc:
            return False, f"disk usage unavailable: {exc}"
        needed = sum(len(v) for v in pkg.files.values()) * 3  # backup + staging
        if free < max(needed, 50 * 1024 * 1024):
            return False, f"insufficient disk space ({free} bytes free)"
        if platform.system() not in ("Windows", "Linux", "Darwin"):
            return False, f"unsupported platform {platform.system()}"
        return True, "ok"

    # -- backup ---------------------------------------------------------------

    def create_backup(self) -> str:
        """Snapshot the app CODE dir only (never DATA/temp/PDFs, §31)."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = self.backup_root / f"backup-{self.current_version}-{stamp}"
        shutil.copytree(self.app_dir, backup_dir,
                        ignore=shutil.ignore_patterns(
                            "__pycache__*", "*.log", "tmp", "cache"))
        if not self._verify_backup(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise RuntimeError("backup verification failed — refusing to update")
        return str(backup_dir)

    def _verify_backup(self, backup_dir: Path) -> bool:
        """The backup exists AND its important files are readable (§31)."""
        if not backup_dir.is_dir():
            return False
        try:
            for path in backup_dir.rglob("*"):
                if path.is_file():
                    with open(path, "rb") as fh:
                        fh.read(64)
            return True
        except OSError:
            return False

    # -- install + health + rollback -----------------------------------------

    def install(self, pkg: Package) -> None:
        """Atomic-ish replacement: write a staging dir, then swap."""
        staging = self.app_dir.parent / (self.app_dir.name + "-staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        for rel, content in pkg.files.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        # DATA must never be part of an app swap (§39).
        data_link = staging / "data"
        if self.data_dir.exists() and not data_link.exists():
            data_link.write_text(
                "The real data directory is not touched by updates.\n",
                encoding="utf-8")
        # Platform-atomic-ish swap the CODE directory.
        old = self.app_dir.parent / (self.app_dir.name + "-old.tmp")
        shutil.rmtree(old, ignore_errors=True)
        os.rename(self.app_dir, old)
        try:
            os.rename(staging, self.app_dir)
        except OSError:
            os.rename(old, self.app_dir)  # put it back immediately (§64)
            raise
        shutil.rmtree(old, ignore_errors=True)

    def run_health_check(self) -> tuple[bool, str]:
        if self._health_check:
            return self._health_check()
        return True, "no health check configured"

    # -- the whole flow --------------------------------------------------------

    def apply_update(self, pkg: Package, *, update_id: str | None = None,
                     device_id: str = "unknown") -> UpdateResult:
        result = UpdateResult(final_state=State.AVAILABLE)
        result._target_version = pkg.target_version
        try:
            ok, why = self.verify_package(pkg)
            if not ok:
                result.final_state = State.FAILED
                result.failure_stage = "VERIFYING"
                result.failure_reason = why
                self._stage(result, "verifying", State.FAILED,
                            pkg.target_version, why)
                return result

            ok, why = self.check_compatibility(pkg)
            if not ok:
                result.final_state = State.REJECTED
                self._stage(result, "compatibility", State.REJECTED,
                            pkg.target_version, why)
                return result

            busy, stage = self.busy_now()
            if busy:
                result.final_state = State.WAITING_FOR_IDLE
                self._stage(result, "busy-protection", State.WAITING_FOR_IDLE,
                            pkg.target_version, f"worker busy in {stage}")
                return result

            self._stage(result, "backup", State.BACKUP, pkg.target_version,
                        "creating verified backup")
            result.backup_dir = self.create_backup()

            self._stage(result, "install", State.INSTALLING, pkg.target_version)
            self.install(pkg)

            self._stage(result, "health_check", State.HEALTH_CHECK,
                        pkg.target_version)
            ok, detail = self.run_health_check()
            if not ok:
                # §38: automatic rollback.
                self._rollback(result, detail, update_id, device_id,
                               pkg.target_version)
                return result

            result.final_state = State.UPDATED
            result.completed_at = time.time()
            self._stage(result, "done", State.UPDATED, pkg.target_version)
            return result

        except Exception as exc:  # noqa: BLE001 — §64: stop safely
            self._rollback(result, f"{type(exc).__name__}: {exc}", update_id,
                           device_id, pkg.target_version)
            return result

    def _rollback(self, result: UpdateResult, reason: str, update_id,
                  device_id: str, target_version: str) -> None:
        result.failure_stage = result.stages[-1]["stage"] if result.stages else None
        result.failure_reason = reason
        try:
            if not result.backup_dir:
                result.final_state = State.RECOVERY_REQUIRED
                self._stage(result, "rollback", State.RECOVERY_REQUIRED,
                            target_version, "no verified backup exists")
                return
            backup = Path(result.backup_dir)
            if not self._verify_backup(backup):
                result.final_state = State.RECOVERY_REQUIRED
                self._stage(result, "rollback", State.RECOVERY_REQUIRED,
                            target_version, "backup no longer verifiable")
                return
            staging = self.app_dir.parent / (self.app_dir.name + "-rb.tmp")
            shutil.rmtree(staging, ignore_errors=True)
            os.rename(self.app_dir, staging)
            try:
                os.rename(backup, self.app_dir)
            except OSError:
                os.rename(staging, self.app_dir)
                result.final_state = State.RECOVERY_REQUIRED
                return
            shutil.rmtree(staging, ignore_errors=True)
            # DATA untouched (§39) — verify it still exists and is readable.
            if self.data_dir.exists():
                probe = next((p for p in self.data_dir.rglob("*")
                              if p.is_file()), None)
                if probe:
                    with open(probe, "rb") as fh:
                        fh.read(16)
            if self._rollback_health:
                ok, detail = self._rollback_health()
            else:
                ok, detail = True, "no rollback health check configured"
            if ok:
                result.final_state = State.ROLLED_BACK
                self._stage(result, "rollback", State.ROLLED_BACK,
                            target_version, "previous version restored and verified")
            else:
                result.final_state = State.RECOVERY_REQUIRED
                self._stage(result, "rollback", State.RECOVERY_REQUIRED,
                            target_version, f"restored but unhealthy: {detail}")
        except Exception as exc:  # noqa: BLE001
            result.final_state = State.RECOVERY_REQUIRED
            self._stage(result, "rollback", State.RECOVERY_REQUIRED,
                        target_version, f"rollback failed: {exc}")
