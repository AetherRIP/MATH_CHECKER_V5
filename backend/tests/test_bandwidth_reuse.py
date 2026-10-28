"""Bandwidth regression tests (§6/§9/§17): prove that the correction pipeline

  * uploads a mark scheme ONCE and reuses it across every student job,
  * reuses an already-uploaded submission across retries / a new worker
    process instead of pushing the same bytes again,
  * never re-downloads from Drive or re-prepares a PDF on a retry,
  * records honest per-job network counters (and the work avoided),
  * keeps shared remote files alive at job cleanup while deleting the
    job-owned (submission) ones.

All Google/Gemini access is faked — no network, no quota, no keys.
"""

from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from app import api_stats, db
from app import config
from app.config import get_config, get_secrets, save_config
from app.gemini.client_proxy import KeyAwareClient
from app.gemini.key_manager import KeyManager, fingerprint

KEYS = ["bandwidth-key-one-0123456789", "bandwidth-key-two-0123456789"]


# ------------------------------------------------------------------ fakes

class _FakeRemote:
    def __init__(self, name: str):
        self.name = name
        self.state = types.SimpleNamespace(name="ACTIVE")


class _FakeFiles:
    """Minimal google-genai files namespace: upload/get/delete + counting."""

    def __init__(self, key: str, world: dict):
        self._key = key
        self._world = world

    def upload(self, *, file: str):
        self._world["uploads"].append((self._key, str(file)))
        name = f"files/{self._key}/{len(self._world['uploads'])}"
        self._world["live"].add(name)
        return _FakeRemote(name)

    def get(self, *, name: str):
        # ``get`` is the liveness check before reusing a registered remote.
        self._world["gets"].append(name)
        if name not in self._world["live"]:
            raise RuntimeError("404 NOT_FOUND")
        return _FakeRemote(name)

    def delete(self, *, name: str):
        self._world["deletes"].append(name)
        self._world["live"].discard(name)


class _FakeSDK:
    def __init__(self, key: str, world: dict):
        self._key = key
        self.files = _FakeFiles(key, world)


@pytest.fixture
def gemini_world(fresh_state, monkeypatch):
    """Real (non-mock) code path with a fake SDK + shared world counters."""
    cfg = get_config()
    cfg.mock_mode = False
    save_config(cfg)
    get_secrets().set_gemini_keys(KEYS)
    world = {"uploads": [], "gets": [], "deletes": [], "live": set()}

    def factory(self, key):
        return _FakeSDK(key, world)

    monkeypatch.setattr(KeyAwareClient, "_sdk_client_for_key", factory)
    return world


def _client(job_id: str, key_index: int = 0) -> KeyAwareClient:
    return KeyAwareClient(KeyManager(), preferred_fp=fingerprint(KEYS[key_index]),
                          job_id=job_id)


def _file(path: Path, size: int = 4096) -> Path:
    path.write_bytes(b"%PDF-1.4\n" + b"x" * size)
    return path


# =============================================== mark scheme uploaded once (§9)

def test_mark_scheme_uploaded_once_and_reused_across_jobs(gemini_world, tmp_path):
    scheme = _file(tmp_path / "scheme.pdf", 10_000)

    c1 = _client("job-one")
    r1 = c1.upload_file(str(scheme), purpose="mark_scheme", shared=True)
    c2 = _client("job-two")                      # a NEW worker process
    r2 = c2.upload_file(str(scheme), purpose="mark_scheme", shared=True)

    assert len(gemini_world["uploads"]) == 1, "mark scheme must upload once"
    assert r2.name == r1.name

    # Job 1 paid the upload; job 2 records a reused upload, not a new one.
    assert api_stats.job_bandwidth("job-one")["upload_count"] == 1
    assert api_stats.job_bandwidth("job-two")["upload_count"] == 0
    assert api_stats.job_bandwidth("job-two")["reused_uploads"] == 1
    assert api_stats.snapshot()["avoided"]["uploads"] >= 1


def test_mark_scheme_reuse_is_per_key(gemini_world, tmp_path):
    """Files are key-scoped: a different key must upload its own copy."""
    scheme = _file(tmp_path / "scheme.pdf", 8_000)

    _client("j1", 0).upload_file(str(scheme), purpose="mark_scheme", shared=True)
    _client("j2", 1).upload_file(str(scheme), purpose="mark_scheme", shared=True)

    keys_used = [k for k, _ in gemini_world["uploads"]]
    assert keys_used == [KEYS[0], KEYS[1]]


def test_stale_registry_reference_falls_back_to_a_fresh_upload(gemini_world,
                                                               tmp_path):
    scheme = _file(tmp_path / "scheme.pdf", 2_000)
    _client("j1").upload_file(str(scheme), purpose="mark_scheme", shared=True)
    # Provider expired/removed the file.
    gemini_world["live"].clear()

    _client("j2").upload_file(str(scheme), purpose="mark_scheme", shared=True)
    assert len(gemini_world["uploads"]) == 2   # old ref rejected, re-uploaded


# ============================================ retry reuses uploaded input (§4)

def test_retry_in_new_process_does_not_re_upload_submission(gemini_world,
                                                            tmp_path):
    submission = _file(tmp_path / "work.pdf", 5_000)

    first = _client("job-x").upload_file(str(submission), purpose="submission")
    retry = _client("job-x").upload_file(str(submission), purpose="submission")

    assert retry.name == first.name
    assert len(gemini_world["uploads"]) == 1, "retry must not re-upload"
    assert api_stats.job_bandwidth("job-x")["reused_uploads"] >= 1


def test_attempt_release_keeps_remote_so_retry_uploads_nothing(gemini_world,
                                                               tmp_path):
    """A worker attempt ending (429) must not delete the submission upload:
    the pool's retry reuses it exactly like a same-process retry."""
    submission = _file(tmp_path / "work.pdf", 9_000)
    client = _client("job-rel")
    first = client.upload_file(str(submission), purpose="submission")
    client.release_job_files()                    # attempt 1 ended

    retry = _client("job-rel").upload_file(str(submission), purpose="submission")
    assert retry.name == first.name
    assert len(gemini_world["uploads"]) == 1

    # Reaching a terminal state DOES delete the job-owned remote.
    from app.engine.service import cleanup_job_files
    cleanup_job_files("job-rel")
    assert first.name in gemini_world["deletes"]
    assert first.name not in gemini_world["live"]


def test_same_client_second_upload_is_a_cache_hit(gemini_world, tmp_path):
    submission = _file(tmp_path / "work.pdf", 3_000)
    client = _client("job-y")
    a = client.upload_file(str(submission), purpose="submission")
    b = client.upload_file(str(submission), purpose="submission")
    assert a.name == b.name
    assert len(gemini_world["uploads"]) == 1


# ======================================================= cleanup behaviour (§20)

def test_cleanup_deletes_submission_but_keeps_shared_mark_scheme(gemini_world,
                                                                 tmp_path):
    submission = _file(tmp_path / "work.pdf", 1_000)
    scheme = _file(tmp_path / "scheme.pdf", 6_000)

    client = _client("job-z")
    sub_remote = client.upload_file(str(submission), purpose="submission")
    scheme_remote = client.upload_file(str(scheme), purpose="mark_scheme",
                                       shared=True)

    # The blueprint step deletes its remote when done; that must be a no-op
    # for a shared mark scheme so sibling jobs can still reuse it.
    client.files_delete(scheme_remote)
    client.cleanup_files()

    assert sub_remote.name in gemini_world["deletes"]
    assert scheme_remote.name not in gemini_world["deletes"]
    assert scheme_remote.name in gemini_world["live"]


# ================================================= per-job counters (§17)

def test_job_bandwidth_counters_accumulate_and_batch(gemini_world):
    api_stats.record_job_bandwidth("job-m", download_count=1,
                                   download_bytes=4_000_000)
    api_stats.record_job_bandwidth("job-m", prepare_count=1, upload_count=1,
                                   upload_bytes=4_200_000, ai_attempts=2)
    api_stats.record_job_bandwidth("job-m", avoided_downloads=2,
                                   avoided_reprepare=1, reused_uploads=1)

    row = api_stats.job_bandwidth("job-m")
    assert row["download_count"] == 1
    assert row["download_bytes"] == 4_000_000
    assert row["prepare_count"] == 1
    assert row["upload_count"] == 1
    assert row["ai_attempts"] == 2
    assert row["avoided_downloads"] == 2
    assert row["reused_uploads"] == 1

    mapping = api_stats.job_bandwidth_map(["job-m", "missing-job"])
    assert set(mapping) == {"job-m"}


def test_registry_purges_expired_rows(gemini_world, tmp_path):
    from app.gemini import file_registry

    scheme = _file(tmp_path / "scheme.pdf", 500)
    _client("jp").upload_file(str(scheme), purpose="mark_scheme", shared=True)
    assert db.query_one("SELECT COUNT(*) AS n FROM gemini_remote_files")["n"] == 1
    assert file_registry.purge_expired(now_ts=time.time() + 10 * 24 * 3600) == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM gemini_remote_files")["n"] == 0


# ============== key rotation must not pay blind re-uploads (§5/§6)

class _RotModels:
    def __init__(self, key, world):
        self._key = key
        self._world = world

    def generate_content(self, *, model, contents, config=None, **kw):
        is_probe = contents == "ping"
        self._world["calls"].append((self._key, "probe" if is_probe else "real"))
        if self._key in self._world["dead"]:
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")
        if is_probe:
            return types.SimpleNamespace(text="pong")
        return types.SimpleNamespace(text='{"ok": true}')


class _RotSDK:
    def __init__(self, key, world):
        self.files = _FakeFiles(key, world)
        self.models = _RotModels(key, world)


@pytest.fixture
def rotation_world(fresh_state, monkeypatch):
    cfg = get_config()
    cfg.mock_mode = False
    save_config(cfg)
    get_secrets().set_gemini_keys(KEYS)
    world = {"uploads": [], "gets": [], "deletes": [], "live": set(),
             "calls": [], "dead": set()}
    monkeypatch.setattr(KeyAwareClient, "_sdk_client_for_key",
                        lambda self, key: _RotSDK(key, world))
    return world


def test_quota_dead_key_is_probed_not_re_uploaded(rotation_world, tmp_path):
    """All keys quota-exhausted: the job must NOT re-upload the submission +
    mark scheme under every key (the 58 MB single-student regression)."""
    rotation_world["dead"] = set(KEYS)          # every key is out of quota
    # Small inputs: below the "re-upload too large" limit, so each candidate
    # key is probed (cheap) rather than blindly receiving a re-upload.
    submission = _file(tmp_path / "work.pdf", 5_000)
    scheme = _file(tmp_path / "scheme.pdf", 9_000)

    client = KeyAwareClient(KeyManager(), preferred_fp=fingerprint(KEYS[0]),
                            job_id="job-dead")
    sub = client.upload_file(str(submission), purpose="submission")
    client.upload_file(str(scheme), purpose="mark_scheme", shared=True)
    uploads_before = len(rotation_world["uploads"])
    assert uploads_before == 2               # one per file, on key 1

    with pytest.raises(RuntimeError):
        client.models.generate_content(model="m-test", contents=[sub, "mark this"])

    # Only cheap probes on the other keys — NO big re-uploads.
    assert len(rotation_world["uploads"]) == uploads_before
    reals = [c for c in rotation_world["calls"] if c[1] == "real"]
    probes = [c for c in rotation_world["calls"] if c[1] == "probe"]
    # The primary key gets one real attempt (nothing is skippable there);
    # every OTHER dead key is only probed, never re-uploaded to.
    assert reals == [(KEYS[0], "real")]
    assert len(probes) >= 1
    assert all(c[0] != KEYS[0] for c in probes)


def test_healthy_key_still_gets_the_request_after_probe(rotation_world,
                                                        tmp_path):
    """Failover still works: a key that can actually serve the request is
    probed first, then receives the re-upload and the real call."""
    rotation_world["dead"] = {KEYS[0]}          # only the first key is dead
    submission = _file(tmp_path / "work.pdf", 1_000)

    client = KeyAwareClient(KeyManager(), preferred_fp=fingerprint(KEYS[0]),
                            job_id="job-live")
    sub = client.upload_file(str(submission), purpose="submission")

    resp = client.models.generate_content(model="m-test", contents=[sub, "mark"])
    assert resp.text == '{"ok": true}'
    assert len(rotation_world["uploads"]) == 2   # initial + re-upload to key 2
    assert any(c == (KEYS[1], "probe") for c in rotation_world["calls"])
    assert (KEYS[1], "real") in rotation_world["calls"]


def test_large_submission_is_not_re_uploaded_on_rate_limit(rotation_world,
                                                          tmp_path):
    """34 MB scan + rate-limited key: do NOT re-upload it to the next key.
    Park instead (the retry reuses this key's already-paid upload)."""
    rotation_world["dead"] = {KEYS[0]}          # others are healthy
    submission = _file(tmp_path / "big.pdf", 5_000_000)

    client = KeyAwareClient(KeyManager(), preferred_fp=fingerprint(KEYS[0]),
                            job_id="job-big")
    remote = client.upload_file(str(submission), purpose="submission")
    with pytest.raises(RuntimeError):
        client.models.generate_content(model="m-test", contents=[remote, "mark"])

    assert len(rotation_world["uploads"]) == 1, "large file must not be re-uploaded"
    assert [c for c in rotation_world["calls"] if c[1] == "real"] == [(KEYS[0], "real")]


def test_rotation_reupload_keeps_mark_scheme_shared(rotation_world, tmp_path):
    """A re-uploaded mark scheme must stay a SHARED mark scheme, not become a
    job-owned 'submission' that cleanup would delete."""
    rotation_world["dead"] = {KEYS[0]}
    scheme = _file(tmp_path / "scheme.pdf", 2_000)

    client = KeyAwareClient(KeyManager(), preferred_fp=fingerprint(KEYS[0]),
                            job_id="job-purp")
    remote = client.upload_file(str(scheme), purpose="mark_scheme", shared=True)
    client.models.generate_content(model="m-test", contents=[remote, "extract"])

    purpose_row = db.query_one(
        "SELECT purpose FROM gemini_remote_files WHERE purpose = 'mark_scheme'")
    assert purpose_row is not None
    # the re-upload under key 2 is registered as a mark scheme too
    rows = db.query(
        "SELECT fingerprint, purpose FROM gemini_remote_files "
        "WHERE purpose = 'mark_scheme'")
    assert {r["fingerprint"] for r in rows} == {fingerprint(KEYS[0]),
                                               fingerprint(KEYS[1])}


# ============================================== stale temp sweep (§20/§25)

def test_sweep_stale_temp_removes_old_job_dirs_only(fresh_state):
    import os
    from app.engine.service import sweep_stale_temp

    tmp = config.DATA_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    old = tmp / "job-old"
    fresh = tmp / "job-fresh"
    other = tmp / "submission_x.pdf"
    for p in (old, fresh):
        p.mkdir()
        (p / "prepared.json").write_text("{}")
    other.write_bytes(b"x")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))
    os.utime(old / "prepared.json", (past, past))

    removed = sweep_stale_temp(max_age_seconds=24 * 3600)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert other.exists()          # non-job files are never touched

    # Precise rule: a dir whose job has reached a TERMINAL state is orphaned
    # even when it is fresh; an ACTIVE job's dir is never touched.
    terminal = tmp / "job-terminal123"
    active = tmp / "job-active123"
    for p in (terminal, active):
        p.mkdir()
    now_ts = time.time()
    db.execute("INSERT INTO correction_jobs(id, status, created_at, updated_at) "
               "VALUES('terminal123','COMPLETED',?,?)", (now_ts, now_ts))
    db.execute("INSERT INTO correction_jobs(id, status, created_at, updated_at) "
               "VALUES('active123','MARKING',?,?)", (now_ts, now_ts))
    removed = sweep_stale_temp(max_age_seconds=24 * 3600)
    assert removed == 1
    assert not terminal.exists()
    assert active.exists()


# ================================ staging: download/prepare exactly once (§3/§7)

def _blank_pdf() -> bytes:
    import io

    from pypdf import PdfWriter
    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)
    return buf.getvalue()


def test_retry_stage_records_one_download_and_one_prepare(fresh_state,
                                                          monkeypatch):
    import app.classroom.auth as classroom_auth
    import app.classroom.client as classroom_client

    monkeypatch.setattr(classroom_auth, "get_access_token", lambda: "fake-token")
    pdf = _blank_pdf()
    downloads = {"n": 0}

    def fake_download(token, file_id):
        downloads["n"] += 1
        return ("homework.pdf", pdf, "application/pdf")

    monkeypatch.setattr(classroom_client, "download_drive_file", fake_download)

    now_ts = time.time()
    db.execute("INSERT INTO courses(id, name, fetched_at) VALUES('c1','Maths',?)",
               (now_ts,))
    db.execute("INSERT INTO coursework(id, course_id, title, fetched_at, allowlisted) "
               "VALUES('w1','c1','HW',?,1)", (now_ts,))
    db.execute("INSERT INTO submissions(id, coursework_id, course_id, user_id, "
               "state, attachment_ids, fetched_at) VALUES(?,?,?,?,?,?,?)",
               ("s1", "w1", "c1", "u1", "TURNED_IN", json.dumps(["f1"]), now_ts))
    scheme_dir = config.DATA_DIR / "mark_schemes"   # live per-test data dir
    scheme_dir.mkdir(parents=True, exist_ok=True)
    (scheme_dir / "w1.pdf").write_bytes(pdf)

    job = {"id": "job-bw", "submission_id": "s1", "coursework_id": "w1",
           "student_name": "Kim"}
    from app.workers.pool import WorkerPool, cleanup_job_files

    pool = WorkerPool(worker_count=1)
    pool._stage_inputs(job)
    pool._stage_inputs(job)          # retry / new attempt

    assert downloads["n"] == 1, "retry must NOT re-download from Drive"
    row = api_stats.job_bandwidth("job-bw")
    assert row["download_count"] == 1
    assert row["prepare_count"] == 1
    assert row["avoided_downloads"] == 1
    assert row["avoided_reprepare"] == 1
    cleanup_job_files("job-bw")


# ===== a refused key must never be handed the submission to re-upload (§5)

THREE_KEYS = ["bw-key-one-0000000000009", "bw-key-two-0000000000009",
              "bw-key-three-000000000009"]

BILLING_403 = (
    "403 PERMISSION_DENIED. {'error': {'code': 403, 'message': 'The Gemini API "
    "free tier is not available for this project. To use the Gemini API, "
    "please set up billing to upgrade to the paid tier.', 'status': "
    "'PERMISSION_DENIED'}}")
FORBIDDEN_403 = ("403 PERMISSION_DENIED: The caller does not have permission "
                 "to use this API.")


class _RefusedModels:
    def __init__(self, key, world):
        self._key, self._world = key, world

    def generate_content(self, *, model, contents, config=None, **kw):
        is_probe = contents == "ping"
        self._world["calls"].append((self._key, "probe" if is_probe else "real"))
        reason = self._world["refused"].get(self._key)
        if reason:
            raise RuntimeError(reason)
        if is_probe:
            return types.SimpleNamespace(text="pong")
        return types.SimpleNamespace(text='{"ok": true}')


class _RefusedSDK:
    def __init__(self, key, world):
        self.files = _FakeFiles(key, world)
        self.models = _RefusedModels(key, world)


@pytest.fixture
def refused_world(fresh_state, monkeypatch):
    cfg = get_config()
    cfg.mock_mode = False
    save_config(cfg)
    get_secrets().set_gemini_keys(THREE_KEYS)
    world = {"uploads": [], "gets": [], "deletes": [], "live": set(),
             "calls": [], "refused": {}}
    monkeypatch.setattr(KeyAwareClient, "_sdk_client_for_key",
                        lambda self, key: _RefusedSDK(key, world))
    return world


@pytest.mark.parametrize("refusal", [BILLING_403, FORBIDDEN_403])
def test_the_preflight_probe_says_no_to_a_403(refused_world, refusal):
    """The probe exists so rotation never pays a big upload to a key that
    cannot answer. A 403 is exactly that — and treating it as "worth trying"
    spent a whole submission re-upload to learn the same 403 again."""
    refused_world["refused"] = {THREE_KEYS[1]: refusal}
    client = KeyAwareClient(KeyManager(),
                            preferred_fp=fingerprint(THREE_KEYS[1]),
                            job_id="job-probe")

    assert client._key_has_quota(fingerprint(THREE_KEYS[1]), "m-test") is False
    assert client._key_has_quota(fingerprint(THREE_KEYS[2]), "m-test") is True

    rec = KeyManager()._get_or_create(fingerprint(THREE_KEYS[1]))
    assert rec.invalid is False, "a 403 is not a rejected credential"
    assert rec.cooldown_until > time.time() + 600, "it is parked, not retried"


@pytest.mark.parametrize("refusal", [BILLING_403, FORBIDDEN_403])
def test_rotation_never_pays_an_upload_to_a_refused_key(refused_world,
                                                        tmp_path, refusal):
    """Two keys, so the rotation order is not a variable: the first is out of
    quota, the second refuses the operation. The submission must never be
    pushed under the refusing key — the re-upload is the expensive half of a
    rotation and it cannot make a 403 succeed."""
    get_secrets().set_gemini_keys(THREE_KEYS[:2])
    refused_world["refused"] = {
        THREE_KEYS[0]: "429 RESOURCE_EXHAUSTED quota exceeded",
        THREE_KEYS[1]: refusal,
    }
    submission = _file(tmp_path / "work.pdf", 6_000)
    client = KeyAwareClient(KeyManager(),
                            preferred_fp=fingerprint(THREE_KEYS[0]),
                            job_id="job-refused")
    remote = client.upload_file(str(submission), purpose="submission")

    with pytest.raises(RuntimeError):
        client.models.generate_content(model="m-test", contents=[remote, "mark"])

    assert (THREE_KEYS[1], "probe") in refused_world["calls"]
    assert not [u for u in refused_world["uploads"] if u[0] == THREE_KEYS[1]],         "a key that refuses the operation must not be paid a full re-upload"
    assert (THREE_KEYS[1], "real") not in refused_world["calls"]
