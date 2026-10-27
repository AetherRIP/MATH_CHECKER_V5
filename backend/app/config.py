"""Central configuration and secret store for Math Checker V5.

Two files live under the runtime ``data/`` directory (git-ignored):

* ``config.toml``  — non-secret application settings (safe to edit).
* ``secrets.toml`` — Gemini API keys and OAuth material. Never logged,
  never returned by any API response, never embedded in diagnostics.

The friend's original app kept its Gemini key in ``.env`` and Classroom
credentials in ``.streamlit/secrets.toml``; V5 consolidates both into one
local secret store so nothing is duplicated across multiple files.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .data_location import resolve_data_dir

# Repository root = parent of backend/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# The data directory is NOT a literal "data/ next to the code" any more: an
# installed copy lives where writing is unreliable and reinstalls replace it,
# so ONE resolver decides (app.data_location): MC5_DATA_DIR, then data beside
# the app, then a development checkout, then the platform's per-user folder.
# Tests and scripts keep pointing MC5_DATA_DIR at a scratch directory, and a
# checkout keeps using its own data/, so nothing that already worked moves.
DATA_DIR = resolve_data_dir(PROJECT_ROOT)
SECRETS_PATH = DATA_DIR / "secrets.toml"
CONFIG_PATH = DATA_DIR / "config.toml"
OAUTH_CLIENT_PATH = DATA_DIR / "secrets" / "google_oauth_client.json"


def data_dir() -> Path:
    """The runtime data directory, resolved on EVERY call.

    Callers must use this instead of importing ``DATA_DIR`` by value: tests,
    ``MC5_DATA_DIR`` overrides and a future config reload all rebind the
    module global, and a copy frozen at import time would write to the wrong
    directory. This is the single definition; routers and workers must not
    each carry their own re-export.
    """
    return DATA_DIR


def allowed_origins() -> list[str]:
    """Browser origins allowed to call this backend cross-origin (§ shared backend).

    In a multi-laptop deployment ONE installation is the shared backend while
    the other laptops' browser UIs talk to it directly, which the browser
    treats as cross-origin. Nothing is allowed unless the operator names the
    exact origins in ``MC5_ALLOWED_ORIGINS`` (comma separated), so a
    single-machine install behaves exactly as it did before — no wildcard, no
    implicit LAN trust.

    Cookies are never allowed cross-origin (see ``main.py``): every distributed
    request carries its own bearer token, so opening the API to another origin
    cannot expose an app session.
    """
    out: list[str] = []
    for item in (os.environ.get("MC5_ALLOWED_ORIGINS") or "").split(","):
        origin = item.strip().rstrip("/")
        if origin and origin not in out:
            out.append(origin)
    return out


# Directories never searched for the OAuth client JSON (they can contain
# tens of thousands of files: a recursive scan would stall every status call).
_OAUTH_SEARCH_SKIP = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "dist",
    ".pytest_cache", ".mypy_cache", "data",
}
_OAUTH_SEARCH_MAX_DEPTH = 3


def _iter_project_json_files(pattern_prefix: str):
    """Bounded, deterministic walk for ``<prefix>*.json`` files.

    Skips dependency/VCS directories and stops after a few levels so the
    lookup stays instant even with frontend/node_modules present. ``data/``
    is handled explicitly (its secrets dir is the canonical home) instead of
    being skipped entirely below.
    """
    import os
    patterns = (f"{pattern_prefix}*.json", "google_oauth_client*.json")

    def scan(folder: Path, depth: int):
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if entry.is_file():
                for pat in patterns:
                    if Path(entry.name).match(pat):
                        yield Path(entry.path)
        if depth >= _OAUTH_SEARCH_MAX_DEPTH:
            return
        for entry in entries:
            if entry.is_dir() and entry.name not in _OAUTH_SEARCH_SKIP:
                yield from scan(Path(entry.path), depth + 1)

    yield from scan(PROJECT_ROOT, 0)
    # The data/ tree holds the canonical location plus a teacher-placed copy.
    try:
        for path in sorted(DATA_DIR.rglob("client_secret_*.json")):
            yield path
        for path in sorted(DATA_DIR.rglob("google_oauth_client*.json")):
            yield path
    except OSError:
        pass


#: How long the project-tree scan's answer is reused.
#:
#: The walk below is a recursive scan of the INSTALL tree, and a released copy
#: ships a bundled portable Python runtime containing thousands of directories,
#: so its cost grows with the size of the installation. The skip list keeps it
#: off node_modules/.venv, but a stale answer is invisible for a file a teacher
#: places by hand, whereas paying the walk on every request is not: measured on
#: the live backend, ``/api/dashboard`` resolved this path three times per call
#: and spent ~127 ms of CPU per walk (3075 ``os.scandir`` + 51 810
#: ``pathlib.match`` calls), which made a 156 ms endpoint take ~1 s under six
#: concurrent clients. The canonical-location check still runs on every call.
_OAUTH_SCAN_TTL_SECONDS = 5.0

#: (PROJECT_ROOT, DATA_DIR) -> (checked_at_monotonic, found_path_or_None).
#: Keyed on both directories because tests and a relocated data folder rebind
#: ``DATA_DIR``, and an answer computed for a previous root would be wrong.
_oauth_scan_cache: dict[tuple[str, str], tuple[float, "Path | None"]] = {}

#: Serializes the walk itself. Without it, several pages polling at the same
#: instant all miss the cache and all walk the tree: measured p95 for six
#: concurrent dashboard clients was 691 ms on the refresh tick, against 45 ms
#: once the answer is warm. One thread walks, the rest read the fresh answer.
_oauth_scan_lock = threading.Lock()


def _project_oauth_client() -> Path | None:
    """A teacher-placed OAuth client JSON, or None. Memoized for a moment.

    Only the expensive recursive search is memoized; the caller still checks
    the canonical location first on every call, so dropping the file into
    ``data/secrets/`` takes effect immediately.
    """
    key = (str(PROJECT_ROOT), str(DATA_DIR))

    def cached(now: float) -> tuple[bool, "Path | None"]:
        """(is_fresh, value) for this key at ``now``."""
        entry = _oauth_scan_cache.get(key)
        if entry is None or now - entry[0] >= _OAUTH_SCAN_TTL_SECONDS:
            return False, None
        found = entry[1]
        # A path that has since been deleted must not be reported as present.
        return True, (found if (found is not None and found.exists()) else None)

    is_fresh, hit = cached(time.monotonic())
    if is_fresh:
        return hit

    with _oauth_scan_lock:
        # Re-check inside the lock: a thread that queued behind the walk would
        # otherwise repeat it on the same cold cache.
        is_fresh, hit = cached(time.monotonic())
        if is_fresh:
            return hit
        found: Path | None = None
        for candidate in _iter_project_json_files("client_secret_"):
            if candidate.exists() and candidate != OAUTH_CLIENT_PATH:
                found = candidate
                break
        _oauth_scan_cache[key] = (time.monotonic(), found)
        return found


def resolve_oauth_client_path() -> Path:
    """Single source of truth for the Google OAuth client JSON.

    Resolution order (first existing file wins):
      1. ``data/secrets/google_oauth_client.json``  (V5 canonical location)
      2. ``client_secret_*.json`` / ``google_oauth_client*.json`` found in
         the project (teacher-placed copy, e.g. at the repository root) —
         auto-detected, never copied, never displayed, gitignored.

    Step 1 is a single ``stat`` and is checked on every call. Step 2 is a
    recursive walk whose result is reused for ``_OAUTH_SCAN_TTL_SECONDS``, so a
    request that merely asks "is Classroom configured?" does not re-scan the
    installation. Tests monkeypatch ``config.OAUTH_CLIENT_PATH`` and this
    function honours it (module attribute read at call time).
    """
    if OAUTH_CLIENT_PATH.exists():
        return OAUTH_CLIENT_PATH
    found = _project_oauth_client()
    if found is not None:
        return found
    # Fall back to the configured path even if it is currently missing; tests
    # and lazy-configuring setups may create it later, and module callers that
    # already hold a bound reference keep working.
    return OAUTH_CLIENT_PATH


#: The model chain Google currently serves for correction, most capable first.
#: Verified against https://ai.google.dev/gemini-api/docs/deprecations and
#: .../docs/pricing on 2026-10-24: Gemini 3.8 Flash is the current Flash model,
#: 3.6 Flash is the previous generation, and 3.5 Flash-Lite is the cheap
#: fallback. None of the three has a shutdown date announced.
#:
#: These are DEFAULTS only: ``[gemini] primary_models`` in data/config.toml
#: overrides them, and that file is what a deployment should edit. No model ID
#: is hardcoded anywhere else (the engine, worker pool, double-check pass and
#: diagnostics all read this chain), so switching models never needs a code
#: change.
DEFAULT_PRIMARY_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
)

#: Second-opinion chain for the (off-by-default) audit pass.
DEFAULT_AUDIT_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
)

#: Lightweight models are deliberately NOT part of the default marking chain.
#: A live TEST 3 run fell back to ``gemini-3.5-flash-lite`` after the full
#: models returned 503, then produced 103/103 by inventing answers that were
#: not on the page (a blank question was returned as "implied complete" with
#: full marks).  Cheap is not a substitute for evidence in high-stakes marking;
#: when no full model is available the correction must wait/retry instead of
#: being graded by a model that is known to fabricate handwritten answers.
#: The check stays suffix-based so a future lightweight model cannot silently
#: re-enter the trusted chain.
UNTRUSTED_MARKING_SUFFIXES = ("-lite",)


def is_trusted_marking_model(model: str | None) -> bool:
    """Whether this model may produce the marks a teacher will use.

    Configuration still accepts every model ID (a preview or lightweight model
    can be useful for auxiliary work), but the correction engine refuses to
    treat a ``-lite`` variant as its marking model.
    """
    name = str(model or "").strip().lower()
    return bool(name) and not name.endswith(UNTRUSTED_MARKING_SUFFIXES)

#: Models Google still ACCEPTS but no longer serves itself: a request to these
#: IDs is automatically routed to the replacement. Leaving one in a fallback
#: chain is actively harmful — after the replacement model failed, the next
#: entry re-sends the whole request to the SAME model (wasting quota and
#: latency), and a result marked through the alias is labelled with a model
#: that never ran. ``normalize_model_chain`` collapses them.
#:
#: Kept as data (not a hardcoded replacement) so a future deprecation is a
#: one-line change here. Unknown IDs are passed through untouched: a teacher
#: may deliberately configure a preview model.
DEPRECATED_MODEL_ROUTES = {
    "gemini-3.5-flash": "gemini-3.6-flash",
    "gemini-3.7-flash": "gemini-3.8-flash",
}


def normalize_model_chain(models) -> list[str]:
    """Clean a configured fallback chain: trim, resolve aliases, de-duplicate.

    Order is preserved (it is the fallback priority). Two entries that resolve
    to the same served model collapse into one, so a deprecated alias can
    never cause a second full request to a model that just failed.
    """
    out: list[str] = []
    for raw in models or []:
        name = str(raw).strip()
        if not name:
            continue
        name = DEPRECATED_MODEL_ROUTES.get(name, name)
        if name not in out:
            out.append(name)
    return out


@dataclass
class GeminiConfig:
    # Ordered fallback chain from the friend's app (app5_1_automated.py,
    # PRIMARY_MODELS), with the two auto-routed model IDs replaced by the
    # models Google actually serves for them (see DEPRECATED_MODEL_ROUTES).
    primary_models: list[str] = field(
        default_factory=lambda: list(DEFAULT_PRIMARY_MODELS))
    # Audit second pass is not run by default in V5 (deterministic
    # verification replaces it); kept for parity with the legacy app.
    audit_models: list[str] = field(
        default_factory=lambda: list(DEFAULT_AUDIT_MODELS))
    # A full marking request is ONE multimodal call carrying the whole answer
    # scan plus the whole mark scheme, so it is bounded by how fast the model
    # reads 30-40 pages, not by a text prompt. Measured on a 15-page scan
    # against a 26-page scheme (2026-10-25): the marking call took 230-440 s,
    # and five repeat runs of the same paper finished in 271-536 s end to end.
    # The old 180_000 ms ceiling cut that call off mid-answer: Gemini answered
    # 504 DEADLINE_EXCEEDED, the single bounded retry timed out the same way,
    # and a perfectly good paper failed permanently - while the marking itself
    # was reproducible (5/5 runs, identical per-question marks).
    #
    # This value is a FLOOR, not a ceiling. The effective per-request timeout
    # is max(this, 120 s + 15 s per page image), capped at 30 minutes - see
    # gemini/client_proxy.timeout_for_pages. 600_000 therefore covers papers up
    # to about 32 pages, and every larger paper gets a budget that grows with
    # it instead of inheriting a number chosen for a 15-page script. Key
    # probes clamp to 30 s in gemini/tester.py, so testing a key stays quick.
    request_timeout_ms: int = 600_000
    # Safeguard against a retry storm, not a way to skip work: one correction
    # legitimately needs 1 generation (single pass), 2 (independent second
    # check), or up to 4 with both targeted recovery rounds. Anything beyond
    # that is the same failing request being resent. 0 disables the cap.
    max_requests_per_job: int = 12
    # Developer prompt editor (prompt-inspection page): optional overrides
    # stored in config.toml so EVERY process (parent, worker child, automation
    # scheduler) reads the same text. None = the built-in legacy prompts are
    # used unchanged — the default and the recommended state.
    system_prompt_override: str | None = None
    user_prompt_override: str | None = None


@dataclass
class PdfCompressionConfig:
    """Upload-size reduction for the PDFs sent to Gemini.

    ON by default because the optimizer is conservative and every candidate
    must pass an independent page/ink integrity check before it is used. A
    teacher can turn it off (nothing but the original is ever uploaded) or
    keep only the lossless pass (``allow_lossy = false``), which removes
    structure/metadata bloat but never re-encodes page images.

    The numeric quality/DPI policy lives in app.engine.pdf_optimize and is
    deliberately not exposed here: a low quality value would silently damage
    handwriting, and the conservative defaults were measured on exam scans.
    """
    enabled: bool = True
    allow_lossy: bool = True


@dataclass
class ClassroomConfig:
    enabled: bool = False
    # Draft grades are written back only when explicitly enabled per
    # assignment; Google may refuse (403) for coursework the app did not
    # create — the UI falls back to showing suggested grades.
    write_draft_grades: bool = False


@dataclass
class WorkerConfig:
    concurrency: int = 2
    max_retries: int = 2


@dataclass
class DoubleCheckConfig:
    """High-score double-check (independent verification of suspiciously
    high AI corrections).

    When a first correction reaches ``threshold_percentage`` (unrounded), the
    existing correction engine is re-run INDEPENDENTLY on the same submission
    + mark scheme. ``disagreement_points`` is the percentage-point gap between
    the two corrections above which NEITHER result is applied blindly and the
    correction is flagged for teacher review instead."""
    enabled: bool = True
    threshold_percentage: float = 95.0   # >= triggers the check (exactly 95 too)
    #: <= this unrounded percentage ALSO triggers a check (§7 "high/Low score"):
    #: a suspiciously low result gets an independent second look for marks the
    #: first pass may have unfairly withheld. 0 disables the low branch.
    low_threshold_percentage: float = 10.0
    disagreement_points: float = 15.0    # larger gap -> REVIEW_REQUIRED


@dataclass
class SchemeAuditConfig:
    """Mark-scheme error detection + approval-gated bulk correction.

    When the marking engine has a defensible mathematical reason to disagree
    with the supplied mark scheme it records a discrepancy. ``confirm_confidence``
    is the model-confidence at or above which a mathematical disagreement is
    labelled CONFIRMED rather than POSSIBLE. No previously graded submission is
    ever changed without explicit human approval of a staged proposal - this
    setting only controls how a disagreement is labelled.
    """
    enabled: bool = True
    confirm_confidence: float = 0.85


@dataclass
class UsageConfig:
    """Rolling internet-usage budget (V5 network budget target: 500 MB/h).
    0 disables a limit."""
    hourly_mb: int = 500
    daily_mb: int = 2000


@dataclass
class TeacherConfig:
    """Local identity + first-run state (§3/§4).

    Nothing here is personal data the developer ships: these fields are
    EMPTY on a fresh install and filled in by the teacher during setup. The
    app never hard-codes a teacher, school or storage location.
    """
    teacher_name: str = ""
    school_name: str = ""
    onboarded: bool = False        # has the first-run wizard been completed?
    onboarded_at: float = 0.0

    #: Permanent report branding (§14-§15).
    #: These values are the teacher's identity on every PDF Math Correct produces.
    #: They are filled in from Settings → Report branding (or the first-run
    #: wizard) and persisted to data/config.toml. Google Classroom sync, OAuth
    #: account metadata and any other account information MUST NOT overwrite
    #: them.
    #:
    #: They are DELIBERATELY EMPTY. A shipped build must never carry one
    #: person's name, centre, academic year, quote or monogram into somebody
    #: else's reports — every install starts letterhead-neutral and the teacher
    #: fills these in. The report designer already degrades cleanly: an empty
    #: centre falls back to the school name, an empty monogram is derived from
    #: the teacher's own initial, and empty title/quote/year lines are simply
    #: not drawn (§20, §32, §33).
    _default_teacher_name: str = ""
    _default_teacher_title: str = ""
    _default_center_name: str = ""
    _default_academic_year: str = ""
    #: Tagline under the centred mark on a formal report (§33).
    _default_quote: str = ""
    #: The stylised single-letter mark at the top of a formal report (§32).
    _default_monogram: str = ""


@dataclass
class ReportConfig:
    """Branding for every PDF Math Correct produces (§2, §4, §5, §20).

    This is presentation only, and it is EMPTY on a fresh install: the app
    never hard-codes a teacher, centre, quote or logo. Everything here is
    filled in from Settings → Report branding, so changing the letterhead never
    means editing Python (§20).
    """
    #: Professional title line under the teacher's name, e.g.
    #: "IGCSE AND A LEVEL MATHEMATICS TEACHER".
    teacher_title: str = ""
    #: Centre / department, e.g. "Your School - O Level".
    center_name: str = ""
    #: e.g. "2026-2027".
    academic_year: str = ""
    #: A short motivational line. Rendered small and italic, never dominant.
    quote: str = ""
    #: Absolute or DATA_DIR-relative path to a logo image. Empty = monogram.
    logo_path: str = ""
    #: Absolute or DATA_DIR-relative path to a signature image. Empty = none.
    #: A teacher supplies their own file here; a distribution must never carry
    #: somebody else's signature.
    signature_path: str = ""
    #: Opt-in switch for the branding images that sit in
    #: backend/app/reporting/assets/. OFF by default, because those files are
    #: the build owner's personal logo/signature and must never appear on a
    #: different teacher's reports. They are also not shipped in a release, so
    #: this flag only ever does something in the private development tree.
    use_bundled_assets: bool = False
    #: Letters used by the generated monogram when no logo file is set.
    monogram: str = ""
    #: Small print under the footer rule. Empty = a sensible default.
    footer_note: str = ""
    #: Accent colour as #RRGGBB. Empty = the design system default.
    accent: str = ""
    #: Default administrative label shown under a report title (§6).
    document_label: str = "TEACHER COPY"


@dataclass
class GradeBoundaryConfig:
    """Configurable 1-9 grade boundaries (§13).

    The default is the percentage band the legacy engine used, kept ONLY as
    a sensible starting point — the teacher replaces it with the real
    boundaries for their exam session. Boundaries are stored as the MINIMUM
    percentage required for each grade, so the AI never invents a grade: a
    grade is a deterministic lookup of (raw marks -> percentage -> band).
    """
    # (grade, minimum percentage) pairs, lowest grade first.
    boundaries: list[tuple[str, float]] = field(default_factory=lambda: [
        ("1", 0.0), ("2", 20.0), ("3", 30.0), ("4", 40.0),
        ("5", 50.0), ("6", 60.0), ("7", 70.0), ("8", 80.0), ("9", 90.0),
    ])
    # Free-text label so a report can say which session's boundaries were used.
    session_label: str = ""


#: The polling intervals the automation monitor is allowed to use (§4).
#: Deliberately a fixed set so a typo cannot turn the monitor into a
#: continuous poller.
AUTOMATION_INTERVALS = (5, 10, 15, 30)
DEFAULT_AUTOMATION_INTERVAL = 10


@dataclass
class AutomationConfig:
    """Fully-automated correction monitor settings (§2, §4, §45).

    The automation layer is OFF by default: nothing polls Google Classroom
    until the teacher explicitly enables it, and even then only assignments
    they individually ticked are ever monitored (§3).

    The polling interval is restricted to :data:`AUTOMATION_INTERVALS` so the
    monitor can never be turned into a continuous poller (§6).
    """
    enabled: bool = False
    interval_minutes: int = DEFAULT_AUTOMATION_INTERVAL
    #: Which post-correction steps run automatically. Attaching the report,
    #: writing the draft grade and returning the work are each independently
    #: controllable, but default ON so a normal submission needs no teacher.
    attach_report: bool = True
    submit_grade: bool = True
    return_work: bool = True
    #: Profile used for the attached report PDF. Default is the FULL report:
    #: after the score and question table it explains EVERY question on its
    #: own — the student's answer, what the mark scheme required, the awarded
    #: mark points and the marking comment — so the report a student receives
    #: is self-explanatory without the teacher's screen.
    #: summary | standard | detailed
    report_detail: str = "detailed"
    #: Default OFF: the normal automated workflow posts the FINAL grade and
    #: returns the work. Set True only when the teacher wants grades left as
    #: drafts for manual review.
    draft_only: bool = False
    #: TEST / REVIEW MODE (safety gate): when enabled, the pipeline still
    #: corrects, saves results and generates/attaches reports, but it NEVER
    #: publishes a final grade to Classroom and NEVER returns work to students
    #: — grades stay as teacher-approved-only drafts. Enable while accuracy is
    #: being proven on real submissions; disable to restore full automation.
    review_mode: bool = True

    def effective_interval(self) -> int:
        value = int(self.interval_minutes or DEFAULT_AUTOMATION_INTERVAL)
        return value if value in AUTOMATION_INTERVALS else DEFAULT_AUTOMATION_INTERVAL


@dataclass
class RetentionConfig:
    """Student-file + temp retention (§19). Predictable, teacher-controlled."""
    # never | until_complete | days | always
    keep_submissions: str = "until_complete"
    keep_days: int = 30            # only used when keep_submissions == "days"
    cleanup_temp_on_start: bool = True


@dataclass
class NetworkConfig:
    """Access mode (§26). Local-only is the safe default.

    ``access_mode`` is deliberately limited to loopback vs. LAN. Public
    exposure is NOT implemented here (it would need auth, TLS, CSRF and rate
    limiting); the UI states that plainly rather than pretending a checkbox
    secures the internet.
    """
    access_mode: str = "local"    # local | lan
    port: int = 8642


@dataclass
class SecurityConfig:
    """Access security (sign-in + tunnel exposure).

    ``enabled`` turns the laptop into a public HTTPS service behind a tunnel;
    while it is on, every API request must be authenticated (see auth_gate).
    ``login_required`` enforces the main sign-in page for LOCAL use too: the
    app opens on the login screen and API requests need a session cookie.
    Both default so that authentication is never silently off; set
    ``MC5_LOGIN_REQUIRED=0`` (or flip it in Settings) for a shared-trust
    machine, and tests run with it off by default.
    """
    enabled: bool = False
    login_required: bool = True
    session_hours: int = 12
    #: The last VERIFIED public address this installation published. Kept so
    #: the Public Address switch can show where it was last reachable without
    #: ever presenting that address as active (see app.public_access).
    last_public_url: str = ""


@dataclass
class AccountsConfig:
    """Multi-account (shared accounts/roles/teams) availability.

    The distributed layer — its tables, records, modules and routes — is
    always present; this flag decides whether the multi-account INTERFACE and
    OPERATIONS are available on this installation. Default OFF: a fresh
    install runs as a single-account install.

    Turning it off never deletes or migrates anything, so turning it back on
    restores the full system as it was (see ``app.multi_account``). The
    environment override ``MULTI_ACCOUNT_ENABLED`` is deliberately NOT folded
    into the loaded config: it must not leak into ``config.toml`` on the next
    save, so ``app.multi_account.enabled()`` layers it over this value.
    """
    multi_account: bool = False


@dataclass
class AgentConfig:
    """Tiny Agent availability on THIS installation.

    The Tiny Agent (``agent/agent.py``) is no longer used by this version: it
    is DISABLED by default, so nothing heartbeats, polls or synchronises in the
    background. Everything about it — the module, the tables, the records and
    the endpoints — is preserved, and turning this back on restores the old
    behaviour without a rewrite (see ``app.distributed.agent_control`` and
    DISTRIBUTED_DEPLOYMENT.md for the re-enable steps).

    The environment override ``MC_AGENT_ENABLED`` is deliberately NOT folded
    into the loaded config: it must never leak into ``config.toml`` on the next
    save, so ``agent_control.product_enabled()`` layers it over this value.
    """
    tiny_agent: bool = False


@dataclass
class TestingConfig:
    """TESTING-ONLY mode (strict no-publication gate).

    While the marker is being validated against real students' work, NOTHING
    may leave the machine. ``only = true`` makes every mutating Google
    Classroom / Drive call raise before it is sent (see ``app.testing_guard``),
    so a retry, a scheduled job or a stray API call cannot publish a grade or
    attach a report even though the correction itself still runs normally.

    Default OFF: a normal install behaves exactly as before. Reading courses,
    rosters, student files and mark schemes is never blocked — testing must
    still see the real submissions.
    """
    only: bool = False


@dataclass
class LifecycleConfig:
    """Start on demand, stay up while a browser is using the app, stop when idle.

    Two halves, and they are separately useful:

    * ``enabled`` — the STOP half. With it on, the backend stops itself once no
      browser session has been seen for the grace period AND nothing is running
      (no correction, no long document, no database write in flight). Logging
      back in is not needed: opening the site again starts it (see below).
    * ``supervisor`` — the START half. The launcher runs a tiny stdlib relay
      that owns the public port, starts the backend on the first connection and
      keeps it as a single child process, so a stopped app is one page load
      away instead of something the teacher has to find and start. Turning it
      off restores the legacy behaviour: one always-on process on the port.

    All timeouts are seconds. ``backend_port`` is the private port the backend
    listens on behind the relay; 0 lets the operating system choose a free one,
    which is what almost every install wants (an adjacent port would collide
    with a second copy's public port).
    """
    enabled: bool = True
    supervisor: bool = True
    heartbeat_seconds: int = 30
    session_timeout_seconds: int = 90
    idle_grace_seconds: int = 120
    max_shutdown_wait_seconds: int = 900
    last_chance_seconds: int = 5
    backend_port: int = 0


def apply_lifecycle_settings(cfg: "AppConfig", values: dict) -> None:
    """Clamp and apply the ``[lifecycle]`` block.

    ONE implementation for the config file and the Settings page, because the
    two must not be able to disagree: a heartbeat of 0 seconds, or a grace
    period of a month, are both ways of making the app unusable — and a screen
    that accepted what the file rejects is a bug waiting to happen.
    """
    lifecycle = cfg.lifecycle
    if "enabled" in values:
        lifecycle.enabled = bool(values["enabled"])
    if "supervisor" in values:
        lifecycle.supervisor = bool(values["supervisor"])
    for key, floor, ceiling in (
            ("heartbeat_seconds", 5, 3600),
            ("session_timeout_seconds", 15, 7200),
            ("idle_grace_seconds", 0, 86400),
            ("max_shutdown_wait_seconds", 0, 604800),
            ("last_chance_seconds", 0, 300)):
        if key in values:
            try:
                setattr(lifecycle, key,
                        max(floor, min(ceiling, int(values[key]))))
            except (TypeError, ValueError):
                pass
    if "backend_port" in values:
        try:
            picked = int(values["backend_port"])
            lifecycle.backend_port = picked if 1 <= picked <= 65535 else 0
        except (TypeError, ValueError):
            pass
    # A session must not be declared dead before the browser is due to beat
    # again, or one slow heartbeat would look like a departure and the
    # countdown would start under a live user.
    if lifecycle.session_timeout_seconds < lifecycle.heartbeat_seconds * 2:
        lifecycle.session_timeout_seconds = lifecycle.heartbeat_seconds * 2


@dataclass
class AssignmentNamingConfig:
    """OPTIONAL default display name for NEW Classroom assignments.

    Every field defaults to empty, so the default behaviour is to preserve the
    original Classroom name EXACTLY. No prefix/suffix ("Via Math Checker")
    is ever added unless the teacher typed one in themselves.
    """
    mode: str = "original"         # original | custom
    custom_name: str = ""
    prefix: str = ""
    suffix: str = ""

    def is_noop(self) -> bool:
        return (self.mode != "custom" and not self.prefix.strip()
                and not self.suffix.strip())


@dataclass
class AppConfig:
    gemini: GeminiConfig = field(default_factory=GeminiConfig)
    pdf_compression: PdfCompressionConfig = field(
        default_factory=PdfCompressionConfig)
    classroom: ClassroomConfig = field(default_factory=ClassroomConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    double_check: DoubleCheckConfig = field(default_factory=DoubleCheckConfig)
    scheme_audit: SchemeAuditConfig = field(default_factory=SchemeAuditConfig)
    usage: UsageConfig = field(default_factory=UsageConfig)
    teacher: TeacherConfig = field(default_factory=TeacherConfig)
    reports: ReportConfig = field(default_factory=ReportConfig)
    grades: GradeBoundaryConfig = field(default_factory=GradeBoundaryConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    assignment_naming: AssignmentNamingConfig = field(
        default_factory=AssignmentNamingConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    testing: TestingConfig = field(default_factory=TestingConfig)
    lifecycle: LifecycleConfig = field(default_factory=LifecycleConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    accounts: AccountsConfig = field(default_factory=AccountsConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    role: str = "teacher"          # teacher | coteacher | developer
    mock_mode: bool = False        # offline demo mode: FakeClient replaces Gemini
    debug: bool = False

    def is_first_run(self) -> bool:
        """True until the teacher completes the setup wizard (§41)."""
        return not self.teacher.onboarded


#: Module logger: used for the ONE diagnostic that must happen before the app's
#: logging is configured. Entries still pass through ``logging_setup.redact``
#: once logging is up, so a credential can never be written out.
_log = logging.getLogger(__name__)


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        # Unparsable configuration must never take the app down — and it must
        # never print secret values — but it must not fail SILENTLY either. A
        # damaged secrets.toml used to look exactly like "no Gemini keys
        # configured", sending a teacher to re-enter keys that were already
        # there. Only the error CLASS and message are logged, never the file
        # contents, and the record is redacted like every other log entry.
        _log.warning("%s could not be read (%s): %s — falling back to defaults",
                     path.name, type(exc).__name__, exc)
        return {}


def _write_toml(path: Path, payload: dict) -> None:
    def fmt(value) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "[" + ", ".join(fmt(v) for v in value) + "]"
        return json.dumps(str(value), ensure_ascii=False)

    lines: list[str] = []
    for section, values in payload.items():
        if isinstance(values, dict):
            lines.append(f"[{section}]")
            for key, value in values.items():
                if isinstance(value, dict):
                    lines.append(f"[{section}.{key}]")
                    for sub_key, sub_value in value.items():
                        lines.append(f"{sub_key} = {fmt(sub_value)}")
                else:
                    lines.append(f"{key} = {fmt(value)}")
            lines.append("")
        else:
            lines.append(f"{section} = {fmt(values)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class SecretStore:
    """Thread-safe access to ``data/secrets.toml``.

    Secrets are read on demand and never cached in plain attributes that
    could leak into diagnostics dumps.
    """

    def __init__(self, path: Path = SECRETS_PATH):
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        with self._lock:
            return _read_toml(self._path)

    def _save(self, payload: dict) -> None:
        with self._lock:
            _write_toml(self._path, payload)

    # -- Gemini keys -----------------------------------------------------------

    def gemini_keys(self) -> list[str]:
        """Return the configured Gemini API keys in priority order."""
        data = self._load()
        keys = data.get("gemini", {}).get("api_keys", [])
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        return [str(k).strip() for k in keys if str(k).strip()]

    def set_gemini_keys(self, keys: list[str]) -> None:
        data = self._load()
        data.setdefault("gemini", {})["api_keys"] = keys
        self._save(data)

    # -- OAuth -------------------------------------------------------------------

    def oauth_client_config(self) -> dict | None:
        """Load the Google OAuth client JSON wherever it was placed.

        Uses the SAME resolver as the connection flow, so a teacher-placed
        ``client_secret_*.json`` at the project root is honoured everywhere:
        configuration checks, the sign-in flow and token refresh can never
        disagree about whether the client exists.
        """
        path = resolve_oauth_client_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def oauth_client_id(self) -> str | None:
        cfg = self.oauth_client_config()
        if not cfg:
            return None
        installed = cfg.get("installed") or cfg.get("web") or {}
        return installed.get("client_id")

    # -- public-access security ------------------------------------------------

    def security_credentials(self) -> dict | None:
        """The teacher login record (username + PBKDF2 hash/salt + signing key).

        Secret material only — never returned by any API response.
        """
        data = self._load()
        section = data.get("security")
        return section if isinstance(section, dict) else None

    def set_security_credentials(self, payload: dict) -> None:
        data = self._load()
        data["security"] = payload
        self._save(data)

    def oauth_refresh_token(self) -> str | None:
        data = self._load()
        token = data.get("classroom", {}).get("refresh_token", "")
        return token or None

    def set_oauth_refresh_token(self, token: str | None) -> None:
        data = self._load()
        data.setdefault("classroom", {})
        data["classroom"]["refresh_token"] = token or ""
        self._save(data)

    def exists(self) -> bool:
        return self._path.exists()


# Module-level singleton accessors -------------------------------------------

_config: AppConfig | None = None
_secrets: SecretStore | None = None
_config_lock = threading.Lock()


def get_config(reload: bool = False) -> AppConfig:
    global _config
    with _config_lock:
        if _config is None or reload:
            data = _read_toml(CONFIG_PATH)
            cfg = AppConfig()
            gemini = data.get("gemini", {})
            if isinstance(gemini, dict):
                # Normalised on READ as well as in the defaults: an existing
                # install's data/config.toml may still carry a model ID that
                # Google now routes to a replacement, and merely opening the
                # app must not keep retrying that alias. The file itself is
                # not rewritten here.
                if "primary_models" in gemini and isinstance(gemini["primary_models"], list):
                    cfg.gemini.primary_models = normalize_model_chain(
                        gemini["primary_models"])
                if "audit_models" in gemini and isinstance(gemini["audit_models"], list):
                    cfg.gemini.audit_models = normalize_model_chain(
                        gemini["audit_models"])
                if "request_timeout_ms" in gemini:
                    try:
                        cfg.gemini.request_timeout_ms = int(gemini["request_timeout_ms"])
                    except (TypeError, ValueError):
                        pass
                if "max_requests_per_job" in gemini:
                    try:
                        cfg.gemini.max_requests_per_job = max(
                            0, int(gemini["max_requests_per_job"]))
                    except (TypeError, ValueError):
                        pass
                # Prompt overrides: a missing/empty value means "built-in
                # prompt" — never the literal string "None".
                if gemini.get("system_prompt_override"):
                    cfg.gemini.system_prompt_override = str(
                        gemini["system_prompt_override"])
                if gemini.get("user_prompt_override"):
                    cfg.gemini.user_prompt_override = str(
                        gemini["user_prompt_override"])
            compression = data.get("pdf_compression", {})
            if isinstance(compression, dict):
                cfg.pdf_compression.enabled = bool(
                    compression.get("enabled", cfg.pdf_compression.enabled))
                cfg.pdf_compression.allow_lossy = bool(
                    compression.get("allow_lossy",
                                     cfg.pdf_compression.allow_lossy))
            classroom = data.get("classroom", {})
            if isinstance(classroom, dict):
                cfg.classroom.enabled = bool(classroom.get("enabled", False))
                cfg.classroom.write_draft_grades = bool(classroom.get("write_draft_grades", False))
            worker = data.get("worker", {})
            if isinstance(worker, dict):
                if "concurrency" in worker:
                    try:
                        cfg.worker.concurrency = max(1, int(worker["concurrency"]))
                    except (TypeError, ValueError):
                        pass
                if "max_retries" in worker:
                    try:
                        cfg.worker.max_retries = max(0, int(worker["max_retries"]))
                    except (TypeError, ValueError):
                        pass
            double_check = data.get("double_check", {})
            if isinstance(double_check, dict):
                cfg.double_check.enabled = bool(double_check.get(
                    "enabled", cfg.double_check.enabled))
                try:
                    cfg.double_check.threshold_percentage = float(
                        double_check.get("threshold_percentage",
                                         cfg.double_check.threshold_percentage))
                except (TypeError, ValueError):
                    pass
                try:
                    cfg.double_check.disagreement_points = float(
                        double_check.get("disagreement_points",
                                         cfg.double_check.disagreement_points))
                except (TypeError, ValueError):
                    pass
                try:
                    cfg.double_check.low_threshold_percentage = float(
                        double_check.get("low_threshold_percentage",
                                         cfg.double_check.low_threshold_percentage))
                except (TypeError, ValueError):
                    pass
            scheme_audit = data.get("scheme_audit", {})
            if isinstance(scheme_audit, dict):
                cfg.scheme_audit.enabled = bool(scheme_audit.get(
                    "enabled", cfg.scheme_audit.enabled))
                try:
                    cfg.scheme_audit.confirm_confidence = float(
                        scheme_audit.get("confirm_confidence",
                                         cfg.scheme_audit.confirm_confidence))
                except (TypeError, ValueError):
                    pass
            usage = data.get("usage", {})
            if isinstance(usage, dict):
                if "hourly_mb" in usage:
                    try:
                        cfg.usage.hourly_mb = max(0, int(usage["hourly_mb"]))
                    except (TypeError, ValueError):
                        pass
                if "daily_mb" in usage:
                    try:
                        cfg.usage.daily_mb = max(0, int(usage["daily_mb"]))
                    except (TypeError, ValueError):
                        pass
            general = data.get("app", {})
            if isinstance(general, dict):
                cfg.role = str(general.get("role", cfg.role))
                cfg.mock_mode = bool(general.get("mock_mode", False))
                cfg.debug = bool(general.get("debug", False))
            teacher = data.get("teacher", {})
            if isinstance(teacher, dict):
                cfg.teacher.teacher_name = str(teacher.get("teacher_name", ""))
                cfg.teacher.school_name = str(teacher.get("school_name", ""))
                cfg.teacher.onboarded = bool(teacher.get("onboarded", False))
                try:
                    cfg.teacher.onboarded_at = float(teacher.get("onboarded_at", 0.0) or 0.0)
                except (TypeError, ValueError):
                    pass
            reports = data.get("reports", {})
            # §14-§15: permanent teacher branding. If the config file has no
            # branding at all (fresh install / cleared config), seed the defaults
            # so the identity is never lost. A stored value always wins over the
            # default, so an explicit teacher edit is preserved.
            # A malformed branding value must never take the app down, and
            # must never be able to inject markup into a PDF.
            if isinstance(reports, dict):
                for key in ("teacher_title", "center_name", "academic_year",
                            "quote", "logo_path", "signature_path", "monogram",
                            "footer_note", "accent", "document_label"):
                    if key in reports and reports[key] is not None:
                        setattr(cfg.reports, key,
                                str(reports[key])[:400])
                if "use_bundled_assets" in reports:
                    cfg.reports.use_bundled_assets = bool(
                        reports["use_bundled_assets"])
                # The teacher NAME is only seeded when the file already has a
                # [reports] section: on a genuinely fresh install the setup
                # wizard must still ask for it (first-run detection relies on
                # the name being empty).
                if not cfg.teacher.teacher_name:
                    cfg.teacher.teacher_name = cfg.teacher._default_teacher_name
            # Re-apply the shipped report defaults ONLY when the config file
            # carries no value for that field (fresh install / wiped config).
            # A stored value is always left untouched above. The shipped
            # defaults are EMPTY on purpose: the letterhead belongs to the
            # teacher running the app, never to whoever built it. The one
            # useful fallback is the teacher's own school name for an empty
            # centre line - the report designer derives an empty monogram from
            # the teacher's initial and simply omits empty title/quote/year
            # lines, so the letterhead degrades cleanly instead of showing
            # somebody else's identity.
            if not cfg.reports.teacher_title:
                cfg.reports.teacher_title = cfg.teacher._default_teacher_title
            if not cfg.reports.center_name and not cfg.teacher.school_name:
                cfg.reports.center_name = cfg.teacher._default_center_name
            if not cfg.reports.academic_year:
                cfg.reports.academic_year = cfg.teacher._default_academic_year
            if not cfg.reports.quote:
                cfg.reports.quote = cfg.teacher._default_quote
            if not cfg.reports.monogram:
                cfg.reports.monogram = cfg.teacher._default_monogram
            if not cfg.reports.center_name and cfg.teacher.school_name:
                cfg.reports.center_name = cfg.teacher.school_name
            grades = data.get("grades", {})
            if isinstance(grades, dict):
                cfg.grades.session_label = str(grades.get("session_label", ""))
                parsed = _parse_boundaries(grades.get("boundaries"))
                if parsed:
                    cfg.grades.boundaries = parsed
            automation = data.get("automation", {})
            if isinstance(automation, dict):
                cfg.automation.enabled = bool(automation.get("enabled", False))
                try:
                    cfg.automation.interval_minutes = int(automation.get(
                        "interval_minutes", cfg.automation.interval_minutes))
                except (TypeError, ValueError):
                    pass
                cfg.automation.attach_report = bool(
                    automation.get("attach_report", cfg.automation.attach_report))
                cfg.automation.submit_grade = bool(
                    automation.get("submit_grade", cfg.automation.submit_grade))
                cfg.automation.return_work = bool(
                    automation.get("return_work", cfg.automation.return_work))
                cfg.automation.draft_only = bool(
                    automation.get("draft_only", cfg.automation.draft_only))
                cfg.automation.review_mode = bool(
                    automation.get("review_mode", cfg.automation.review_mode))
                cfg.automation.review_mode = bool(
                    automation.get("review_mode", cfg.automation.review_mode))
                detail = str(automation.get("report_detail", cfg.automation.report_detail))
                if detail in ("summary", "standard", "detailed"):
                    cfg.automation.report_detail = detail
            retention = data.get("retention", {})
            if isinstance(retention, dict):
                mode = str(retention.get("keep_submissions", cfg.retention.keep_submissions))
                if mode in ("never", "until_complete", "days", "always"):
                    cfg.retention.keep_submissions = mode
                try:
                    cfg.retention.keep_days = max(0, int(retention.get("keep_days", cfg.retention.keep_days)))
                except (TypeError, ValueError):
                    pass
                cfg.retention.cleanup_temp_on_start = bool(
                    retention.get("cleanup_temp_on_start", cfg.retention.cleanup_temp_on_start))
            testing = data.get("testing", {})
            if isinstance(testing, dict):
                cfg.testing.only = bool(testing.get("only", False))
            lifecycle = data.get("lifecycle", {})
            if isinstance(lifecycle, dict):
                # Values are clamped, never trusted (see the helper: the
                # Settings page uses the very same rules).
                apply_lifecycle_settings(cfg, lifecycle)
            network = data.get("network", {})
            if isinstance(network, dict):
                mode = str(network.get("access_mode", cfg.network.access_mode))
                cfg.network.access_mode = mode if mode in ("local", "lan") else "local"
                try:
                    cfg.network.port = int(network.get("port", cfg.network.port))
                except (TypeError, ValueError):
                    pass
            naming = data.get("assignment_naming", {})
            if isinstance(naming, dict):
                mode = str(naming.get("mode", cfg.assignment_naming.mode))
                cfg.assignment_naming.mode = mode if mode in (
                    "original", "custom") else "original"
                cfg.assignment_naming.custom_name = str(
                    naming.get("custom_name", "") or "")
                cfg.assignment_naming.prefix = str(naming.get("prefix", "") or "")
                cfg.assignment_naming.suffix = str(naming.get("suffix", "") or "")
            security = data.get("security", {})
            if isinstance(security, dict):
                cfg.security.enabled = bool(security.get("enabled", False))
                cfg.security.login_required = bool(
                    security.get("login_required", cfg.security.login_required))
                cfg.security.last_public_url = str(
                    security.get("last_public_url", "") or "")[:300]
                try:
                    cfg.security.session_hours = max(
                        1, int(security.get("session_hours", cfg.security.session_hours)))
                except (TypeError, ValueError):
                    pass
            # Environment override: the public startup script sets MC5_PUBLIC=1
            # so exposure can never depend on a config file being edited first.
            if os.environ.get("MC5_PUBLIC") == "1":
                cfg.security.enabled = True
            # Same idea for the local sign-in requirement (tests, portable
            # diagnostics and packaging smoke runs set this explicitly).
            env_login = os.environ.get("MC5_LOGIN_REQUIRED")
            if env_login in ("0", "1"):
                cfg.security.login_required = env_login == "1"
            accounts = data.get("accounts", {})
            if isinstance(accounts, dict):
                # Only an explicit true enables the layer; a typo, a missing
                # key or a broken file leaves it OFF (fail closed).
                cfg.accounts.multi_account = bool(
                    accounts.get("multi_account", False))
            _config = cfg
        return _config


def _parse_boundaries(raw) -> list[tuple[str, float]]:
    """Accept boundaries as ``[["9", 90], ...]`` and normalise to a clean,
    sorted (lowest-first) list. Bad rows are dropped rather than crashing
    config load — a malformed boundary must never take the app down."""
    if not isinstance(raw, list):
        return []
    parsed: list[tuple[str, float]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        grade = str(entry[0]).strip()
        if not grade:
            continue
        try:
            minimum = float(entry[1])
        except (TypeError, ValueError):
            continue
        parsed.append((grade, max(0.0, min(100.0, minimum))))
    parsed.sort(key=lambda item: item[1])
    return parsed


def save_config(cfg: AppConfig) -> None:
    global _config
    payload = {
        "app": {"role": cfg.role, "mock_mode": cfg.mock_mode, "debug": cfg.debug},
        "teacher": {
            "teacher_name": cfg.teacher.teacher_name,
            "school_name": cfg.teacher.school_name,
            "onboarded": cfg.teacher.onboarded,
            "onboarded_at": cfg.teacher.onboarded_at,
        },
        "reports": {
            "teacher_title": cfg.reports.teacher_title,
            "center_name": cfg.reports.center_name,
            "academic_year": cfg.reports.academic_year,
            "quote": cfg.reports.quote,
            "logo_path": cfg.reports.logo_path,
            "signature_path": cfg.reports.signature_path,
            "use_bundled_assets": cfg.reports.use_bundled_assets,
            "monogram": cfg.reports.monogram,
            "footer_note": cfg.reports.footer_note,
            "accent": cfg.reports.accent,
            "document_label": cfg.reports.document_label,
        },
        "gemini": {
            # Saved normalised, so a config file written by an older build is
            # cleaned up the first time settings are saved.
            "primary_models": normalize_model_chain(cfg.gemini.primary_models),
            "audit_models": normalize_model_chain(cfg.gemini.audit_models),
            "request_timeout_ms": cfg.gemini.request_timeout_ms,
            "max_requests_per_job": cfg.gemini.max_requests_per_job,
            # Prompt overrides (Developer prompt page). Omitted entirely when
            # unset: _write_toml would stringify None, and the engine must
            # fall back to the built-in legacy prompts.
            **({"system_prompt_override": cfg.gemini.system_prompt_override}
               if cfg.gemini.system_prompt_override else {}),
            **({"user_prompt_override": cfg.gemini.user_prompt_override}
               if cfg.gemini.user_prompt_override else {}),
        },
        "pdf_compression": {
            "enabled": cfg.pdf_compression.enabled,
            "allow_lossy": cfg.pdf_compression.allow_lossy,
        },
        "classroom": {
            "enabled": cfg.classroom.enabled,
            "write_draft_grades": cfg.classroom.write_draft_grades,
        },
        "worker": {"concurrency": cfg.worker.concurrency, "max_retries": cfg.worker.max_retries},
        "double_check": {
            "enabled": cfg.double_check.enabled,
            "threshold_percentage": cfg.double_check.threshold_percentage,
            "low_threshold_percentage": cfg.double_check.low_threshold_percentage,
            "disagreement_points": cfg.double_check.disagreement_points,
        },
        "scheme_audit": {
            "enabled": cfg.scheme_audit.enabled,
            "confirm_confidence": cfg.scheme_audit.confirm_confidence,
        },
        "usage": {"hourly_mb": cfg.usage.hourly_mb, "daily_mb": cfg.usage.daily_mb},
        "grades": {
            "session_label": cfg.grades.session_label,
            "boundaries": [[g, m] for g, m in cfg.grades.boundaries],
        },
        "automation": {
            "enabled": cfg.automation.enabled,
            "interval_minutes": cfg.automation.effective_interval(),
            "attach_report": cfg.automation.attach_report,
            "submit_grade": cfg.automation.submit_grade,
            "return_work": cfg.automation.return_work,
            "draft_only": cfg.automation.draft_only,
            "review_mode": cfg.automation.review_mode,
            "review_mode": cfg.automation.review_mode,
            "report_detail": cfg.automation.report_detail,
        },
        "assignment_naming": {
            "mode": cfg.assignment_naming.mode,
            "custom_name": cfg.assignment_naming.custom_name,
            "prefix": cfg.assignment_naming.prefix,
            "suffix": cfg.assignment_naming.suffix,
        },
        "retention": {
            "keep_submissions": cfg.retention.keep_submissions,
            "keep_days": cfg.retention.keep_days,
            "cleanup_temp_on_start": cfg.retention.cleanup_temp_on_start,
        },
        "network": {
            "access_mode": cfg.network.access_mode,
            "port": cfg.network.port,
        },
        "security": {
            "enabled": cfg.security.enabled,
            "login_required": cfg.security.login_required,
            "session_hours": cfg.security.session_hours,
            **({"last_public_url": cfg.security.last_public_url}
               if cfg.security.last_public_url else {}),
        },
        "accounts": {
            "multi_account": cfg.accounts.multi_account,
        },
        "testing": {
            "only": cfg.testing.only,
        },
        "lifecycle": {
            "enabled": cfg.lifecycle.enabled,
            "supervisor": cfg.lifecycle.supervisor,
            "heartbeat_seconds": cfg.lifecycle.heartbeat_seconds,
            "session_timeout_seconds": cfg.lifecycle.session_timeout_seconds,
            "idle_grace_seconds": cfg.lifecycle.idle_grace_seconds,
            "max_shutdown_wait_seconds": cfg.lifecycle.max_shutdown_wait_seconds,
            "last_chance_seconds": cfg.lifecycle.last_chance_seconds,
            "backend_port": cfg.lifecycle.backend_port,
        },
    }
    _write_toml(CONFIG_PATH, payload)
    with _config_lock:
        _config = cfg


def get_secrets() -> SecretStore:
    global _secrets
    with _config_lock:
        if _secrets is None:
            _secrets = SecretStore()
        return _secrets


def ensure_dirs() -> None:
    for sub in ("", "secrets", "tmp", "logs"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
