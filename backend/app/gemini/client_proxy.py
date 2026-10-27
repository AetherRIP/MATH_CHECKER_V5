"""KeyAwareClient — drop-in ``client.models.generate_content(...)`` proxy.

The legacy engine calls ``client.models.generate_content(model=…, contents=…,
config=…)``. This proxy resolves a real google-genai client for whichever
key the manager selects, rotates keys on key-level failures, and reports
outcomes back to the KeyManager. Cooldowns apply per key, so rotation
composes with (does not replace) the legacy per-model fallback.

API-efficiency rules (retry architecture §1/§3/§4/§8):
* a classified provider overload closes the provider gate — the failed
  request is NOT immediately resent with the whole submission + mark scheme;
* key-level failures (auth/quota/503) fail over to another healthy key
  exactly once per key, never looping on the same credential;
* request/avoided/failover counters feed the local usage dashboard without
  any extra API call.
"""

from __future__ import annotations

import logging
import threading
import time

from ..errors import classify_gemini_error, http_status_of
from ..logging_setup import log_event
from ..retry_policy import classify_failure
from .call_audit import ProviderBudgetExceeded
from .key_manager import (
    KeyManager,
    NoKeysError,
    WaitForApiError,
)
from .key_manager import fingerprint as fingerprint_of

log = logging.getLogger("gemini.proxy")

#: A Files API lookup used to validate a reused upload is a request of its
#: own. A remote verified this recently in THIS process is still alive (the
#: API keeps uploads for 48 h), so re-checking it before every sibling job is
#: pure overhead: one paper with 20 prepared pages paid 20 lookups per job.
REMOTE_VERIFY_TTL_SECONDS = 900.0

# Errors that justify trying ANOTHER KEY within the same request:
# invalid_key (this credential is dead) and rate_limited (this key's quota is
# exhausted). "temporary" (503 high demand) is included as well: free-tier
# quota pools are per project/key, so a second key frequently succeeds where
# the first gets a demand spike — and staying on the SAME model is important
# for run-to-run marking consistency (switching models changes the marks).
_KEY_LEVEL_KINDS = {"invalid_key", "rate_limited", "temporary"}
_MAX_ATTEMPTS = 8

# Rotating to another key means re-uploading every attached file (Gemini files
# are key-scoped). For a large scanned submission that is tens of MB per key,
# and quota pools are usually account-wide, so rotation buys nothing while
# costing the whole submission again. Above this size we PARK the job instead
# — the retry reuses the SAME key's already-paid upload, or fails over only
# once the file's owner key is healthy again.
_MAX_REUPLOAD_BYTES = 3 * 1024 * 1024


# --- how long a request may take, given how big the paper is --------------
# A marking request sends EVERY page as an image, so its duration is bounded
# by how long the model takes to read the paper, not by the prompt text.
# Measured on 2026-10-25 (15-page scan + 26-page scheme = 41 pages): 230-440 s
# for the call, i.e. 6-11 s per page. 15 s per page leaves room for a slower
# model or a denser page, and the fixed floor covers start-up and queueing.
PER_PAGE_TIMEOUT_MS = 15_000
FIXED_REQUEST_TIMEOUT_MS = 120_000
#: Never wait longer than this however many pages arrive: a wedged request has
#: to release its worker eventually.
MAX_REQUEST_TIMEOUT_MS = 1_800_000  # 30 minutes


def timeout_for_pages(pages: int | None, configured_ms: int | None = None) -> int:
    """Per-request timeout for a paper of ``pages`` pages.

    ``gemini.request_timeout_ms`` is a FLOOR for small papers, not a ceiling
    for big ones: the same 10 minutes that comfortably marks a 15-page script
    would cut off a 70-page booklet mid-answer, because the request time grows
    with the number of page images the model has to read. The budget therefore
    grows with the page count and is capped so one stuck request cannot hold a
    worker indefinitely.

    ``configured_ms`` is passed in by callers that already resolved the
    setting; ``None`` reads it from config.
    """
    base = int(configured_ms if configured_ms is not None else get_timeout())
    try:
        count = max(0, int(pages or 0))
    except (TypeError, ValueError):
        count = 0
    budget = FIXED_REQUEST_TIMEOUT_MS + count * PER_PAGE_TIMEOUT_MS
    return min(MAX_REQUEST_TIMEOUT_MS, max(base, budget))


class ProviderCoolingError(RuntimeError):
    """The provider is known to be overloaded — the request was NOT sent.

    Raised BEFORE any bytes leave the process, so retrying later costs
    nothing now (§1/§11: never immediately resend the full request).
    """


class _ModelsNamespace:
    """Mimics ``client.models`` of the google-genai SDK."""

    def __init__(self, outer: "KeyAwareClient"):
        self._outer = outer

    def generate_content(self, *, model: str, contents, config=None, **kwargs):
        return self._outer.generate_content(model=model, contents=contents, config=config, **kwargs)

    # Staged key testing (Test All Keys) and legacy parity helpers.
    def list(self):
        client = self._outer._sdk_client_for_current_key()
        with self._outer._client_lock:
            self._outer._current_fp = self._outer._current_fp  # no-op; acquire done
        return client.models.list()


class _FilesNamespace:
    """Mimics ``client.files`` of the google-genai SDK, routed through the
    sticky/rotating key selection (uploads stay inside the job's key lease)."""

    def __init__(self, outer: "KeyAwareClient"):
        self._outer = outer

    def upload(self, *, file: str):
        return self._outer.upload_file(file)

    def get(self, *, name: str):
        return self._outer.files_get_by_name(name)

    def delete(self, *, name: str):
        self._outer.files_delete_by_name(name)


class KeyAwareClient:
    """Rotating facade over google-genai clients (or a mock in mock mode)."""

    def __init__(self, key_manager: KeyManager, preferred_fp: str | None = None,
                 job_id: str | None = None, pages: int = 0):
        self.keys = key_manager
        # Page images in this transaction (submission + mark scheme, plus a
        # question paper when one is attached). They decide the request
        # timeout — see ``timeout_for_pages``. 0 keeps the configured value.
        try:
            self.pages = max(0, int(pages or 0))
        except (TypeError, ValueError):
            self.pages = 0
        # Bandwidth diagnostics (§17): per-job counters live in SQLite so the
        # child process can record uploads/attempts against the right job.
        self.job_id = job_id
        # Remote files that must OUTLIVE this job (shared mark schemes reused
        # across every student of an assignment). Cleanup skips these.
        self._shared_remotes: set[str] = set()
        # remote name -> when THIS process last confirmed it is alive
        self._verified_remotes: dict[str, float] = {}
        # Cheap pre-flight probes: before paying a FULL re-upload of the
        # submission + mark scheme under another key, ask that key for one
        # token. A quota-exhausted key answers 429 in a few KB, so a dead key
        # never costs 15 MB of uploads (§5/§6 — observed: 4 quota-dead keys
        # cost ~58 MB of pointless re-uploads for one student).
        self._probe_cache: dict[tuple[str, str], bool] = {}
        self._client_lock = threading.Lock()
        self._sdk_clients: dict[str, object] = {}
        self._current_fp: str | None = None
        # Sticky key for this transaction. The worker pool passes the key it
        # leased exclusively for this job, so parallel jobs keep their own key
        # while a mid-job 429 can still fall through to another healthy key
        # instead of forcing the model-fallback chain to change models.
        self._sticky_fp = preferred_fp
        # The legacy engine calls ``client.models.generate_content(...)``;
        # expose the same namespace shape (plus ``files`` for the child
        # process's upload/delete path).
        self.models = _ModelsNamespace(self)
        self.files = _FilesNamespace(self)
        # Gemini Files are scoped to the API key that uploaded them: a file
        # uploaded with KEY_1 cannot be read by KEY_2 (403 PERMISSION_DENIED).
        # We therefore track which key owns each remote file and, when a
        # mid-job rotation happens, re-upload the inputs under the new key
        # instead of letting the request die on a cross-key file 403.
        self._file_owner: dict[str, str] = {}
        self._file_sources: dict[str, str] = {}
        self._file_purposes: dict[str, str] = {}
        self._file_sizes: dict[str, int] = {}
        # (fingerprint, absolute path) -> remote: one upload per key per file
        # per job. Without it every model-chain retry after a rotation would
        # push the whole PDF again (bandwidth + usage-budget blow-up).
        self._upload_cache: dict[tuple[str, str], object] = {}
        # Usage accounting lives here (exact byte counts), so wrappers around
        # this client must not record uploads a second time.
        self.records_usage = True

    def current_fingerprint(self) -> str | None:
        """Safe identifier (KEY_n label lives in the KeyManager) of the key
        that actually served the last request — recorded for diagnostics."""
        return self._sticky_fp

    def _acquire_for_call(self) -> str:
        """Sticky per-transaction key selection.

        A job = one transaction (uploads + all model attempts share one key).
        A 429 recorded mid-transaction must steer the NEXT job's rotation but
        must NOT abort THIS transaction's model chain — otherwise a single
        rate-limited model would block every remaining model in the legacy
        fallback order (surfaced by real-world testing)."""
        if self._sticky_fp is not None:
            # Still configured? (secret store may have changed)
            for key in self._configured_keys():
                if fingerprint_of(key) == self._sticky_fp:
                    return self._sticky_fp
            self._sticky_fp = None
        fp, _rec = self.keys.acquire()
        self._sticky_fp = fp
        return fp

    def _configured_keys(self) -> list[str]:
        from ..config import get_secrets
        return get_secrets().gemini_keys()

    # -- SDK client construction -------------------------------------------

    def _sdk_client_for_key(self, key: str):
        from google.genai import types  # local import: mock mode never needs it

        import google.genai as genai
        # Scale with the paper, not just the configured floor: a 70-page
        # booklet must not inherit the budget that fits 15 pages.
        cfg = timeout_for_pages(self.pages)
        return genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=cfg),
        )

    def _sdk_client_for_current_key(self):
        from google.genai import types
        import google.genai as genai
        cfg = timeout_for_pages(self.pages)
        return genai.Client(
            api_key=self._current_key(),
            http_options=types.HttpOptions(timeout=cfg),
        )

    def _current_key(self) -> str:
        from ..config import get_secrets
        keys = get_secrets().gemini_keys()
        if not keys:
            raise NoKeysError("No Gemini API keys are configured.")
        return keys[0]

    # -- rotation ------------------------------------------------------------

    def _resolve(self, attempt: int):
        """Return (key, fp) for this attempt, respecting rotation."""
        if attempt == 0:
            return self.keys.acquire()
        return self.keys.acquire()  # manager round-robins; cooldowns steer away

    def generate_content(self, *, model: str, contents, config=None, **kwargs):
        from ..config import get_config
        cfg = get_config()
        if cfg.mock_mode:
            from .mock import MockModels
            return MockModels().generate_content(model=model, contents=contents, config=config)

        # Provider circuit gate (§11): while THIS MODEL is cooling down after
        # repeated 503/high-demand failures, do NOT re-send the full prepared
        # request — fail fast locally so the caller can park or try the next
        # model in the fallback chain. The gate is per model on purpose: a 503
        # on one model says nothing about the other models or about the keys,
        # and treating it as provider-wide falsely marked every key as cooling
        # down (measured: 11 of 12 keys serving normally during a 503).
        from .provider_health import provider_health
        allowed, gate_wait = provider_health().available(model)
        if not allowed:
            try:
                from . import call_audit
                call_audit.note_skip(f"{model} cooling down")
            except Exception:
                pass
            raise ProviderCoolingError(
                f"Gemini model {model} is cooling down after high demand — "
                f"request not sent ({int(gate_wait)}s cooldown remaining).")

        last_error: Exception | None = None
        tried: set[str] = set()
        cooldown_escalation = 0
        failed_fp: str | None = None

        for attempt in range(_MAX_ATTEMPTS):
            try:
                fp = self._acquire_for_call()
            except WaitForApiError:
                if last_error is not None:
                    # Mid-transaction: a fresh key is cooling down, but we
                    # already have a concrete error (e.g. 429) for THIS model.
                    # Re-raise it so the legacy per-model loop can move on to
                    # the next model instead of aborting the whole chain.
                    raise last_error
                raise
            except NoKeysError:
                raise

            if failed_fp is not None and fp != failed_fp:
                # This request is being re-sent through a DIFFERENT key —
                # count the failover for the usage dashboard (local only).
                try:
                    from ..api_stats import record_failover
                    record_failover()
                except Exception:
                    pass
                failed_fp = None

            if fp in tried:
                # Only one key (or all others failed this call): hand the
                # error to the legacy loop, which advances to the next model.
                raise last_error or WaitForApiError(
                    "All Gemini keys failed for this request."
                )

            tried.add(fp)

            transient: list = []
            original_contents = contents   # pre-re-upload form (rotation policy)
            try:
                sdk_client = self._sdk_clients.get(fp)
                if sdk_client is None:
                    key_value = self._key_value(fp)
                    sdk_client = self._sdk_client_for_key(key_value)
                    with self._client_lock:
                        self._sdk_clients[fp] = sdk_client

                # Cross-key file guard: re-upload inputs owned by another key
                # so the request never dies on a file-permission 403.
                # Re-uploads are cached per (key, file) and cleaned up by
                # cleanup_files() at the end of the job.
                needs_reupload = self._needs_reupload(contents, fp)
                if needs_reupload and last_error is not None \
                        and self._resolve_cost_is_too_high(contents, last_error):
                    # Rotating would pay the whole (large) submission again for
                    # a quota pool that is usually account-wide. Park instead:
                    # the retry reuses the SAME key's already-paid upload.
                    log_event("gemini.proxy",
                              "key rotation skipped (re-upload too large)",
                              level=logging.WARNING, fingerprint=fp, model=model,
                              reupload_bytes=self._reupload_cost(contents))
                    raise last_error
                if needs_reupload and not self._key_has_quota(fp, model):
                    # This key cannot serve the request — do NOT pay a full
                    # re-upload to find out. Park/skip it instead.
                    last_error = self._quota_probe_error(fp)
                    self.keys.record_failure(fp, last_error)
                    failed_fp = fp
                    # Release the sticky key so the next iteration probes a
                    # DIFFERENT key (a healthy one still gets the request).
                    self._sticky_fp = None
                    continue
                contents, transient = self._reupload_for_key(contents, fp)

                started = time.time()
                # A referenced provider file can disappear mid-job (an expired
                # upload, or a sibling job's terminal cleanup of the shared
                # submission before this guard existed). Gemini answers 403
                # PERMISSION_DENIED "... or it may not exist", which used to be
                # terminal AND to cool down a perfectly healthy key. Repair by
                # re-uploading the inputs under this key and retry once.
                for file_attempt in range(2):
                    # Counted BEFORE the send: the budget must stop a storm
                    # before a request leaves the process, and a request that
                    # is refused by the budget was never sent at all.
                    from . import call_audit as _audit
                    # Charged to THIS job: the ledger is process-wide, so a
                    # caller that does not name its job (the API's in-process
                    # mark-scheme recheck) is not charged at all.
                    _audit.note_send(_audit.KIND_GENERATE, model=model,
                                     job_id=self.job_id)
                    try:
                        response = sdk_client.models.generate_content(
                            model=model,
                            contents=contents,
                            config=config,
                            **kwargs,
                        )
                        break
                    except Exception as file_exc:
                        if file_attempt or not self._is_file_permission_error(file_exc):
                            raise
                        log_event("gemini.proxy",
                                  "referenced file unreadable; re-uploading",
                                  level=logging.WARNING, fingerprint=fp,
                                  model=model)
                        contents = self._repair_missing_files(contents, fp)
                try:
                    from . import call_audit as _audit_ok
                    _audit_ok.note_result(ok=True)
                except Exception:
                    pass
                self.keys.record_success(fp)
                try:
                    from ..api_stats import record_request
                    record_request(ok=True)
                    provider_health().record_success(model)
                except Exception:
                    pass
                log_event(
                    "gemini.proxy", "generate_content ok",
                    fingerprint=fp, model=model,
                    elapsed_ms=int((time.time() - started) * 1000),
                    reuploaded=len(transient),
                )
                return response

            except Exception as exc:
                last_error = exc
                if isinstance(exc, ProviderBudgetExceeded):
                    raise
                try:
                    from . import call_audit as _audit_bad
                    _audit_bad.note_result(ok=False, error=str(exc))
                except Exception:
                    pass
                kind = classify_gemini_error(exc)["kind"]
                self.keys.record_failure(fp, exc)
                try:
                    error_class = classify_failure(exc).cls
                except Exception:
                    error_class = "UNKNOWN_ERROR"
                try:
                    from ..api_stats import record_request
                    record_request(ok=False, error_class=error_class)
                    provider_health().record_failure(error_class, str(exc),
                                                     model)
                except Exception:
                    pass
                # Safe diagnostics: provider (this module), operation
                # (generate_content), error category/class and the provider's
                # own HTTP status. Never the request payload or the credential.
                log_event(
                    "gemini.proxy", "generate_content failed",
                    level=logging.WARNING, fingerprint=fp, model=model,
                    kind=kind, error_class=error_class, status=http_status_of(exc),
                )
                if kind not in _KEY_LEVEL_KINDS:
                    # Model-level, invalid-request, timeout or unknown error:
                    # do not burn another key on the same failing request —
                    # hand it to the legacy per-model loop / job policy.
                    raise
                # key-level problem → release the sticky key and let the next
                # attempt pick a fresh one (same call) — or surface the error
                # to the legacy loop when no other key exists.
                self._sticky_fp = None
                failed_fp = fp
                if kind == "rate_limited":
                    cooldown_escalation += 1
                    if cooldown_escalation >= 3:
                        # The key is genuinely quota-limited; stop re-probing
                        # the remaining models on it (anti-hammer).
                        raise
                continue

        raise last_error or WaitForApiError("All Gemini keys failed for this request.")

    def _resolve_cost_is_too_high(self, contents, last_error) -> bool:
        """True when a key rotation would re-upload more than the limit for a
        quota/overload failure (i.e. rotating buys little, costs a lot)."""
        try:
            kind = classify_gemini_error(last_error)["kind"]
        except Exception:
            kind = "unknown"
        if kind not in ("rate_limited", "temporary"):
            return False   # a DEAD key must still fail over, cost regardless
        return self._reupload_cost(contents) > _MAX_REUPLOAD_BYTES

    def _reupload_cost(self, contents) -> int:
        """Total bytes that a key rotation would have to re-upload."""
        if not isinstance(contents, (list, tuple)):
            return 0
        total = 0
        for item in contents:
            name = getattr(item, "name", None)
            if name:
                total += self._file_sizes.get(name, 0)
        return total

    @staticmethod
    def _is_file_permission_error(exc: Exception) -> bool:
        """True for a Files-API permission/not-found failure (not a key fault)."""
        text = str(exc).lower()
        if "permission" not in text and "403" not in text:
            return False
        return "file" in text or "not exist" in text

    def _repair_missing_files(self, contents, fp: str):
        """Re-upload every referenced local file the provider no longer serves.

        The registry entry and the in-memory owner mapping are dropped first,
        otherwise ``upload_file`` would keep returning the dead reference.
        """
        import os

        if not isinstance(contents, (list, tuple)):
            return contents
        repaired = []
        for item in contents:
            name = getattr(item, "name", None)
            source = self._file_sources.get(name) if name else None
            if not (name and source and os.path.exists(source)):
                repaired.append(item)
                continue
            purpose = self._file_purposes.get(name, "submission")
            abs_source = os.path.abspath(source)
            with self._client_lock:
                self._upload_cache.pop((fp, abs_source), None)
                self._file_owner.pop(name, None)
            try:
                from . import file_registry
                file_registry.forget(fp, file_registry.content_sha256(abs_source),
                                      purpose)
            except Exception:
                pass
            try:
                repaired.append(self.upload_file(
                    source, prefer_fp=fp, purpose=purpose,
                    shared=(purpose == "mark_scheme")))
            except Exception:
                repaired.append(item)
        return repaired

    def _needs_reupload(self, contents, fp: str) -> bool:
        """True when some file reference in ``contents`` is owned by another
        key (files are key-scoped, so it would 403 without a re-upload)."""
        if not isinstance(contents, (list, tuple)):
            return False
        for item in contents:
            name = getattr(item, "name", None)
            owner = self._file_owner.get(name) if name else None
            if owner is not None and owner != fp \
                    and self._file_sources.get(name):
                return True
        return False

    def _quota_probe_error(self, fp: str) -> Exception:
        """Synthetic 429 for the manager so the probed key cools down."""
        return RuntimeError(
            "429 RESOURCE_EXHAUSTED: key quota exhausted "
            "(pre-flight probe — no re-upload attempted)")

    def _key_has_quota(self, fp: str, model: str) -> bool:
        """One-token probe, cached per (key, model): is this key worth
        re-uploading to?

        Costs a few KB instead of the whole submission + mark scheme, and uses
        the SAME model the pending request needs (quota is per key+model). A
        probe failure also cools the key down, so the manager stops handing
        it out and the job parks instead of burning bandwidth."""
        cache_key = (fp, model)
        with self._client_lock:
            cached = self._probe_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            client = self._client_for_fp(fp)
            client.models.generate_content(
                model=model,
                contents="ping",
                config={"max_output_tokens": 1},
            )
            ok = True
        except Exception as exc:  # noqa: BLE001
            kind = classify_gemini_error(exc)["kind"]
            ok = kind not in {"rate_limited", "invalid_key", "temporary",
                              "model_no_access"}
            if not ok:
                try:
                    self.keys.record_failure(fp, exc)
                except Exception:
                    pass
            log_event("gemini.proxy", "key pre-flight probe",
                      level=logging.WARNING, fingerprint=fp, model=model,
                      kind=kind, reupload_allowed=ok)
        with self._client_lock:
            self._probe_cache[cache_key] = ok
        return ok

    def _reupload_for_key(self, contents, fp: str):
        """Return ``(contents, transient_remotes)`` with every remote file
        re-uploaded under ``fp`` when it was originally uploaded by another
        key. Non-file contents pass through untouched."""
        if not isinstance(contents, (list, tuple)):
            return contents, []
        out = []
        transient = []
        for item in contents:
            name = getattr(item, "name", None)
            owner = self._file_owner.get(name) if name else None
            source = self._file_sources.get(name) if name else None
            if owner is not None and owner != fp and source:
                # Preserve the ORIGINAL purpose/ownership: a re-uploaded mark
                # scheme must stay a shared mark scheme (not become a
                # job-owned "submission" that cleanup would delete).
                purpose = self._file_purposes.get(name, "submission")
                replacement = self.upload_file(
                    source, prefer_fp=fp, purpose=purpose,
                    shared=(purpose == "mark_scheme"))
                transient.append(replacement)
                out.append(replacement)
            else:
                out.append(item)
        return out, transient

    def _key_value(self, fp: str) -> str:
        for key in self._configured_keys():
            if fingerprint_of(key) == fp:
                return key
        raise NoKeysError("Key fingerprint no longer matches any configured key.")

    # -- upload / files API (used by CorrectionService; legacy parity) --------

    def upload_file(self, path: str, prefer_fp: str | None = None, *,
                    purpose: str = "submission", shared: bool = False):
        """Upload a local input to the Gemini Files API, reusing a live
        remote when possible.

        ``shared=True`` marks a file that must survive this job so later jobs
        (every other student of the same assignment) reuse it — the registry
        keyed by (key fingerprint, content SHA-256, purpose) makes that
        possible. A reused remote is verified with ``files.get`` first; a
        dead/expired reference simply falls through to a fresh upload.
        """
        if get_mock():
            from .mock import MockFile
            return MockFile(path)
        import os
        # uploads bind this transaction's key (``prefer_fp`` when the caller
        # already knows which key must own the file)
        fp = prefer_fp or self._acquire_for_call()
        abs_path = os.path.abspath(path)
        cache_key = (fp, abs_path)
        with self._client_lock:
            cached = self._upload_cache.get(cache_key)
        if cached is not None:
            # Same key + same file: already uploaded this job — count the
            # upload we did NOT have to repeat (§12 dashboard, local only).
            self._note_reuse(cached, purpose)
            return cached

        client = self._client_for_fp(fp)

        # Cross-job reuse (§6/§9): a mark scheme (or a submission retried in
        # another process) uploaded earlier on THIS key can be referenced
        # again instead of pushing the same bytes up the wire.
        remote = self._reuse_registered(client, fp, abs_path, purpose, cache_key)
        if remote is not None:
            return remote

        from . import call_audit as _audit
        _audit.note_send(_audit.KIND_UPLOAD, job_id=self.job_id)
        try:
            remote = client.files.upload(file=path)
        except Exception as exc:
            _audit.note_result(ok=False, error=str(exc))
            raise
        _audit.note_result(ok=True)
        self._wait_active(client, remote, source=path)
        with self._client_lock:
            self._file_owner[remote.name] = fp
            self._file_sources[remote.name] = path
            self._file_purposes[remote.name] = purpose
            self._upload_cache[cache_key] = remote
            try:
                self._file_sizes[remote.name] = os.path.getsize(path)
            except OSError:
                self._file_sizes[remote.name] = 0
            if shared:
                self._shared_remotes.add(remote.name)
            self._verified_remotes[remote.name] = time.time()
        size = 0
        try:
            size = os.path.getsize(path)
            from ..net_usage import record
            record("gemini_upload", size, 0)
        except Exception:
            pass
        try:
            from . import file_registry
            file_registry.store(fp, file_registry.content_sha256(abs_path),
                                purpose, remote.name, size)
            if not shared:
                file_registry.record_job_file(self.job_id, fp, remote.name,
                                              purpose)
        except Exception:
            pass
        try:
            from ..api_stats import record_job_bandwidth
            record_job_bandwidth(self.job_id, upload_count=1, upload_bytes=size)
        except Exception:
            pass
        return remote

    def _client_for_fp(self, fp: str):
        client = self._sdk_clients.get(fp)
        if client is None:
            client = self._sdk_client_for_key(self._key_value(fp))
            with self._client_lock:
                self._sdk_clients[fp] = client
        return client

    def _reuse_registered(self, client, fp: str, abs_path: str, purpose: str,
                          cache_key: tuple[str, str]):
        """Return a verified, still-alive remote from the registry or None."""
        try:
            from . import file_registry
            sha = file_registry.content_sha256(abs_path)
            row = file_registry.lookup(fp, sha, purpose)
        except Exception:
            return None
        if not row:
            return None
        remote_name = str(row.get("remote_name") or "")
        now_ts = time.time()
        recently_verified = False
        with self._client_lock:
            verified_at = self._verified_remotes.get(remote_name)
        if verified_at is not None and now_ts - verified_at < REMOTE_VERIFY_TTL_SECONDS:
            recently_verified = True
        if recently_verified:
            # Same process already confirmed this upload is alive moments
            # ago: reuse it without another Files API round-trip.
            class _Known:
                name = remote_name
                state = type("state", (), {"name": "ACTIVE"})()
            remote = _Known()
        else:
            from . import call_audit as _audit
            _audit.note_send(_audit.KIND_FILE_GET, job_id=self.job_id)
            try:
                remote = client.files.get(name=remote_name)
                _audit.note_result(ok=True)
            except Exception as exc:
                _audit.note_result(ok=False, error=str(exc))
                try:
                    from . import file_registry as reg
                    reg.forget(fp, sha, purpose)
                except Exception:
                    pass
                return None
            with self._client_lock:
                self._verified_remotes[remote_name] = time.time()
        state = getattr(getattr(remote, "state", None), "name", "ACTIVE")
        if state and state not in {"ACTIVE", "SUCCEEDED"}:
            return None
        with self._client_lock:
            self._file_owner[getattr(remote, "name", "")] = fp
            self._file_sources[getattr(remote, "name", "")] = abs_path
            self._file_purposes[getattr(remote, "name", "")] = purpose
            self._upload_cache[cache_key] = remote
            if purpose == "mark_scheme":
                self._shared_remotes.add(getattr(remote, "name", ""))
        try:
            from . import file_registry as reg
            reg.touch(fp, sha, purpose)
            if purpose != "mark_scheme":
                reg.record_job_file(self.job_id, fp,
                                    getattr(remote, "name", ""), purpose)
        except Exception:
            pass
        self._note_reuse(remote, purpose)
        log_event("gemini.proxy", "reused remote file (no re-upload)",
                  fingerprint=fp, purpose=purpose,
                  remote=getattr(remote, "name", ""),
                  sha=sha[:12])
        return remote

    def _note_reuse(self, remote, purpose: str) -> None:
        """Count an upload we did NOT have to repeat (local diagnostics)."""
        try:
            from ..api_stats import record_avoided, record_job_bandwidth
            record_avoided("uploads")
            record_job_bandwidth(self.job_id, reused_uploads=1)
        except Exception:
            pass

    def release_job_files(self) -> None:
        """Called when a worker ATTEMPT ends: drop in-memory tracking but do
        NOT delete the remotes, so a retry / another worker process can reuse
        the same upload instead of pushing the bytes again (§4/§6).

        The pool deletes a job's provider-side files only when the job
        reaches a terminal state (cleanup_job_files)."""
        with self._client_lock:
            self._upload_cache.clear()

    def delete_remote_for_key(self, fp: str, name: str) -> None:
        """Delete one provider file, using the key that owns it (files are
        key-scoped, so the wrong key would get a 403 anyway)."""
        if not name:
            return
        try:
            self._client_for_fp(fp).files.delete(name=name)
        except Exception:
            pass

    def cleanup_files(self) -> None:
        """Delete every remote file this client uploaded (originals and any
        rotation re-uploads). Called once when the job reaches a terminal
        state; failures are ignored because Gemini also expires files.

        SHARED files (mark schemes registered for cross-job reuse, §9) are
        kept: deleting them here would make the next student's job upload the
        same mark scheme again. They expire provider-side, and are dropped
        from the local registry once expired."""
        with self._client_lock:
            remotes = [r for r in self._upload_cache.values()
                       if getattr(r, "name", None) not in self._shared_remotes]
            self._upload_cache.clear()
        seen: set[str] = set()
        for remote in remotes:
            name = getattr(remote, "name", None)
            if not name or name in seen:
                continue
            seen.add(name)
            self.files_delete(remote)

    def upload_bytes(self, data: bytes, filename: str):
        """Legacy upload_bytes_to_gemini, rewritten onto the rotating client."""
        import os
        import tempfile
        suffix = os.path.splitext(filename)[1].lower() or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            return self.upload_file(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _client_for_file(self, remote):
        """Client that may actually access this remote: files are scoped to
        the key that uploaded them, so prefer the OWNER's cached client."""
        owner = self._file_owner.get(getattr(remote, "name", ""))
        if owner:
            owned = self._sdk_clients.get(owner)
            if owned is not None:
                return owned
        return self._client_for_remote(remote)

    def files_get(self, remote):
        client = self._client_for_file(remote)
        return client.files.get(name=remote.name)

    def files_get_by_name(self, name: str):
        fp = self._acquire_for_call()
        client = self._sdk_clients.get(fp)
        if client is None:
            client = self._sdk_client_for_key(self._key_value(fp))
            with self._client_lock:
                self._sdk_clients[fp] = client
        return client.files.get(name=name)

    def files_delete(self, remote, force: bool = False) -> None:
        name = getattr(remote, "name", "")
        if not force and name in self._shared_remotes:
            # Shared mark scheme: keep it so sibling jobs reuse it (§9).
            return
        with self._client_lock:
            for key, cached in list(self._upload_cache.items()):
                if getattr(cached, "name", None) == name:
                    self._upload_cache.pop(key, None)
        try:
            client = self._client_for_file(remote)
            client.files.delete(name=remote.name)
        except Exception:
            pass

    def files_delete_by_name(self, name: str, force: bool = False) -> None:
        if not force and name in self._shared_remotes:
            return
        with self._client_lock:
            for key, cached in list(self._upload_cache.items()):
                if getattr(cached, "name", None) == name:
                    self._upload_cache.pop(key, None)
        owner = self._file_owner.get(name)
        client = self._sdk_clients.get(owner) if owner else None
        if client is None:
            try:
                fp = self._acquire_for_call()
                client = self._sdk_clients.get(fp)
                if client is None:
                    client = self._sdk_client_for_key(self._key_value(fp))
                    with self._client_lock:
                        self._sdk_clients[fp] = client
            except Exception:
                return
        try:
            client.files.delete(name=name)
        except Exception:
            pass

    def _client_for_remote(self, remote):
        if get_mock():
            from .mock import MockClientHolder
            return MockClientHolder()
        fp = self._acquire_for_call()  # remote files belong to the sticky key
        client = self._sdk_clients.get(fp)
        if client is None:
            client = self._sdk_client_for_key(self._key_value(fp))
            with self._client_lock:
                self._sdk_clients[fp] = client
        return client

    def _wait_active(self, client, remote, *, source: str, max_wait_seconds: int = 120):
        """Legacy upload poll loop (PROCESSING → ACTIVE), verbatim semantics."""
        waited = 0.0
        while getattr(remote.state, "name", "") == "PROCESSING":
            if waited >= max_wait_seconds:
                raise RuntimeError(
                    f"Gemini has been stuck processing '{source}' for over "
                    f"{max_wait_seconds}s without finishing. This usually means "
                    f"a transient issue on Gemini's side — try again."
                )
            time.sleep(0.5)
            waited += 0.5
            remote = client.files.get(name=remote.name)
        state_name = getattr(remote.state, "name", "")
        if state_name and state_name not in {"ACTIVE", "SUCCEEDED"}:
            raise RuntimeError(
                f"Gemini could not finish processing '{source}'. File state: {state_name}"
            )
        return remote


def get_timeout() -> int:
    from ..config import get_config
    return get_config().gemini.request_timeout_ms


def get_mock() -> bool:
    from ..config import get_config
    return get_config().mock_mode
