"""Retention behaviour tests (§19) — the setting must do real work."""

import time
from pathlib import Path

import app.db as db
from app.config import get_config, save_config
from app.engine.service import cleanup_job_files, sweep_expired_retained


def _stage_submission(job_id: str, data_dir: Path) -> Path:
    path = data_dir / "tmp" / f"{job_id}_submission.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 student work")
    db.execute(
        "INSERT INTO pending_manual_files(job_id, kind, path, filename) VALUES(?,?,?,?)",
        (job_id, "submission", str(path), "work.pdf"))
    return path


def test_until_complete_retains_nothing(fresh_state):
    cfg = get_config()
    cfg.retention.keep_submissions = "until_complete"
    save_config(cfg)
    path = _stage_submission("j-uc", fresh_state)
    cleanup_job_files("j-uc")
    assert not path.exists()
    assert db.query("SELECT * FROM retained_files") == []


def test_always_retains_submission_not_mark_scheme(fresh_state):
    cfg = get_config()
    cfg.retention.keep_submissions = "always"
    save_config(cfg)
    path = _stage_submission("j-al", fresh_state)
    ms = fresh_state / "tmp" / "j-al_ms.pdf"
    ms.write_bytes(b"%PDF-1.4 scheme")
    db.execute(
        "INSERT INTO pending_manual_files(job_id, kind, path, filename) VALUES(?,?,?,?)",
        ("j-al", "mark_scheme", str(ms), "scheme.pdf"))

    cleanup_job_files("j-al")

    rows = db.query("SELECT * FROM retained_files")
    assert len(rows) == 1
    assert rows[0]["kind"] == "submission"
    assert Path(rows[0]["path"]).exists()
    # The shared mark scheme is never retained.
    assert all(r["kind"] == "submission" for r in rows)


def test_days_sets_expiry_and_sweep_reclaims(fresh_state):
    cfg = get_config()
    cfg.retention.keep_submissions = "days"
    cfg.retention.keep_days = 7
    save_config(cfg)
    _stage_submission("j-d", fresh_state)
    cleanup_job_files("j-d")

    row = db.query_one("SELECT * FROM retained_files")
    assert row is not None
    assert row["retain_until"] > time.time()  # not yet expired

    # Force it into the past, as if a week had passed.
    db.execute("UPDATE retained_files SET retain_until = ? WHERE job_id = 'j-d'",
               (time.time() - 10,))
    removed = sweep_expired_retained()
    assert removed == 1
    assert not Path(row["path"]).exists()
    assert db.query("SELECT * FROM retained_files") == []


def test_never_retains_nothing(fresh_state):
    cfg = get_config()
    cfg.retention.keep_submissions = "never"
    save_config(cfg)
    path = _stage_submission("j-n", fresh_state)
    cleanup_job_files("j-n")
    assert not path.exists()
    assert db.query("SELECT * FROM retained_files") == []
