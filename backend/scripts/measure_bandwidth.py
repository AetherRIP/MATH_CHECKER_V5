"""Bandwidth measurement harness (§26).

Runs the REAL pipeline code paths — worker-pool staging (Drive download +
PDF normalization), prepared-input reuse on retry, and the KeyAwareClient
upload path with the cross-job file registry — against a COUNTED fake
provider (no network, no Gemini quota). It prints per-student byte counts so
"retries do not re-download / re-upload" can be verified with numbers.

Usage:  .venv/bin/python backend/scripts/measure_bandwidth.py [students]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
# Isolated data dir BEFORE app modules import (mirrors conftest).
os.environ["MC5_DATA_DIR"] = tempfile.mkdtemp(prefix="mc5_bw_")

from app import api_stats, config, db  # noqa: E402
from app.config import get_config, get_secrets, save_config  # noqa: E402
from app.gemini.client_proxy import KeyAwareClient  # noqa: E402
from app.gemini.key_manager import KeyManager, fingerprint  # noqa: E402

STUDENTS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
SUBMISSION_BYTES = 4 * 1024 * 1024        # 4 MB student scan
MARK_SCHEME_BYTES = 6 * 1024 * 1024       # 6 MB mark scheme
KEY = "measure-bandwidth-key-0123456789"
MB = 1024 * 1024


# ------------------------------------------------------------ fake provider

class _Remote:
    def __init__(self, name):
        self.name = name
        self.state = types.SimpleNamespace(name="ACTIVE")


class _Files:
    def __init__(self, world):
        self._world = world

    def upload(self, *, file):
        size = os.path.getsize(file)
        self._world["uploads"].append({"path": file, "bytes": size})
        name = f"files/{len(self._world['uploads'])}"
        self._world["live"].add(name)
        return _Remote(name)

    def get(self, *, name):
        if name not in self._world["live"]:
            raise RuntimeError("404 NOT_FOUND")
        return _Remote(name)

    def delete(self, *, name):
        self._world["live"].discard(name)


class _SDK:
    def __init__(self, world):
        self.files = _Files(world)


def _pdf(path: Path, size: int) -> Path:
    """A real, parseable PDF padded to roughly ``size`` bytes (so pypdf can
    normalize it exactly like a real submission)."""
    from pypdf import PdfWriter
    import io

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)
    data = buf.getvalue()
    path.write_bytes(data + b" " * max(0, size - len(data) - 1) + b"\n")
    return path


def main() -> int:
    db.init_db()          # create schema in the isolated data dir
    cfg = get_config()
    cfg.mock_mode = False
    save_config(cfg)
    get_secrets().set_gemini_keys([KEY])

    world = {"uploads": [], "live": set()}
    KeyAwareClient._sdk_client_for_key = lambda self, key: _SDK(world)

    work = Path(config.DATA_DIR) / "measure"
    work.mkdir(parents=True, exist_ok=True)

    # --- seed one assignment, N students, one mark scheme -------------------
    scheme = _pdf(work / "scheme.pdf", MARK_SCHEME_BYTES)
    scheme_dir = Path(config.DATA_DIR) / "mark_schemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    (scheme_dir / "w1.pdf").write_bytes(scheme.read_bytes())

    now = time.time()
    db.execute("INSERT INTO courses(id, name, fetched_at) VALUES('c1','Maths',?)", (now,))
    db.execute("INSERT INTO coursework(id, course_id, title, fetched_at, allowlisted) "
               "VALUES('w1','c1','HW',?,1)", (now,))

    student_files: dict[str, Path] = {}
    for i in range(STUDENTS):
        sid, uid = f"s{i}", f"u{i}"
        pdf = _pdf(work / f"submission_{i}.pdf", SUBMISSION_BYTES)
        student_files[sid] = pdf
        db.execute("INSERT INTO submissions(id, coursework_id, course_id, user_id, "
                   "state, attachment_ids, fetched_at) VALUES(?,?,?,?,?,?,?)",
                   (sid, "w1", "c1", uid, "TURNED_IN", json.dumps([f"f{i}"]), now))

    import app.classroom.auth as classroom_auth
    import app.classroom.client as classroom_client

    classroom_auth.get_access_token = lambda: "fake-token"
    drive_downloads = {"n": 0, "bytes": 0}

    def fake_download(token, file_id):
        idx = int(file_id[1:])
        data = student_files[f"s{idx}"].read_bytes()
        drive_downloads["n"] += 1
        drive_downloads["bytes"] += len(data)
        return (f"submission_{idx}.pdf", data, "application/pdf")

    classroom_client.download_drive_file = fake_download

    from app.workers.pool import WorkerPool, cleanup_job_files

    pool = WorkerPool(worker_count=1)
    fp = fingerprint(KEY)
    km = KeyManager()

    rows = []
    retry_bytes = 0
    for i in range(STUDENTS):
        job_id = f"job{i}"
        job = {"id": job_id, "submission_id": f"s{i}", "coursework_id": "w1",
               "student_name": f"Student {i}"}

        # Attempt 1
        pool._stage_inputs(job)
        c1 = KeyAwareClient(km, preferred_fp=fp, job_id=job_id)
        sub1 = c1.upload_file(str(student_files[f"s{i}"]), purpose="submission")
        sch1 = c1.upload_file(str(scheme), purpose="mark_scheme", shared=True)
        c1.release_job_files()          # attempt ended (429) — keep uploads

        # Retry (429 simulation): a NEW worker process re-stages + re-uploads.
        before = len(world["uploads"])
        pool._stage_inputs(job)
        c2 = KeyAwareClient(km, preferred_fp=fp, job_id=job_id)
        sub2 = c2.upload_file(str(student_files[f"s{i}"]), purpose="submission")
        sch2 = c2.upload_file(str(scheme), purpose="mark_scheme", shared=True)
        c2.release_job_files()
        retry_bytes += sum(u["bytes"] for u in world["uploads"][before:])
        assert sub2.name == sub1.name and sch2.name == sch1.name, "retry re-uploaded!"

        bw = api_stats.job_bandwidth(job_id) or {}
        rows.append((i, len(student_files[f"s{i}"].read_bytes()), bw))
        cleanup_job_files(job_id)

    # --- report -------------------------------------------------------------
    print(f"\nStudents simulated: {STUDENTS}   "
          f"(submission ~{SUBMISSION_BYTES / MB:.1f} MB, "
          f"mark scheme ~{MARK_SCHEME_BYTES / MB:.1f} MB)\n")
    print(f"{'#':>2}  {'sub':>7}  {'drive dl':>8}  {'dl bytes':>9}  "
          f"{'prepare':>7}  {'uploads':>7}  {'up bytes':>9}  {'reused':>6}")
    total_upload = total_download = 0
    for i, size, bw in rows:
        total_upload += bw.get("upload_bytes", 0)
        total_download += bw.get("download_bytes", 0)
        print(f"{i:>2}  {size / MB:>6.2f}M  {bw.get('download_count', 0):>8}  "
              f"{bw.get('download_bytes', 0):>9}  {bw.get('prepare_count', 0):>7}  "
              f"{bw.get('upload_count', 0):>7}  {bw.get('upload_bytes', 0):>9}  "
              f"{bw.get('reused_uploads', 0):>6}")

    uploads = world["uploads"]
    print(f"\nGoogle Drive downloads total : {drive_downloads['n']} "
          f"({drive_downloads['bytes'] / MB:.2f} MB)")
    print(f"AI (Files API) uploads total : {len(uploads)} "
          f"({sum(u['bytes'] for u in uploads) / MB:.2f} MB)")
    print(f"Retry-phase upload bytes     : {retry_bytes / MB:.2f} MB (expected 0.00)")
    print(f"Duplicate Drive downloads    : {drive_downloads['n'] - STUDENTS} "
          f"(expected 0)")
    print(f"Duplicate prepare runs       : "
          f"{sum(r[2].get('prepare_count', 0) for r in rows) - STUDENTS} (expected 0)")
    print(f"Mark scheme uploads          : "
          f"{sum(1 for u in uploads if u['path'].endswith('w1.pdf') or u['path'].endswith('scheme.pdf'))}"
          f" (expected 1)")

    naive = (STUDENTS * 2) * (SUBMISSION_BYTES + MARK_SCHEME_BYTES)
    actual = drive_downloads["bytes"] + sum(u["bytes"] for u in uploads)
    print(f"\nTotal network bytes          : {actual / MB:.2f} MB "
          f"(avg {actual / STUDENTS / MB:.2f} MB/student)")
    print(f"Without reuse (naive model)  : {naive / MB:.2f} MB "
          f"(avg {naive / STUDENTS / MB:.2f} MB/student)")
    print(f"Saved by prepared-input reuse: {(naive - actual) / MB:.2f} MB "
          f"({100 * (naive - actual) / naive:.1f}%)")
    print(f"\nAvoided counters: {api_stats.snapshot()['avoided']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
