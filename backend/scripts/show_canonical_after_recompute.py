import sys, uuid, json, time
from pathlib import Path
sys.path.insert(0, ".")

data_dir = Path("data")
data_dir.mkdir(parents=True, exist_ok=True)
for f in list(data_dir.glob("app.db")) + list(data_dir.glob("data.db")):
    try:
        f.unlink()
    except OSError:
        pass

import app.config as c
c.DATA_DIR = data_dir
c.SECRETS_PATH = data_dir / 'secrets.toml'
c.CONFIG_PATH = data_dir / 'config.toml'
c.get_config(reload=True)

from app import db
db.init_db()

import app.classroom.report_attachment as ra


data_dir = Path("data")
data_dir.mkdir(parents=True, exist_ok=True)
for f in list(data_dir.glob("app.db")) + list(data_dir.glob("data.db")):
    try:
        f.unlink()
    except OSError:
        pass

import app.config as c
c.DATA_DIR = data_dir
c.SECRETS_PATH = data_dir / "secrets.toml"
c.CONFIG_PATH = data_dir / "config.toml"
c.get_config(reload=True)

from app import db
db.init_db()

import app.classroom.report_attachment as ra

COURSE_ID = "c1"
CW_ID = "cw1"
SUB_ID = "sub1"
RES1 = uuid.uuid4().hex
RES2 = uuid.uuid4().hex
now = time.time()

with db.transaction() as conn:
    conn.execute(
        "INSERT INTO canonical_reports(course_id, coursework_id, submission_id, canonical_result_id, drive_file_id, updated_at) VALUES(?,?,?,?,?,?)",
        (COURSE_ID, CW_ID, SUB_ID, RES1, "driveFile123_old", now),
    )

row = ra.query_one(
    "SELECT canonical_result_id, drive_file_id FROM canonical_reports WHERE course_id = ? AND coursework_id = ? AND submission_id = ?",
    (COURSE_ID, CW_ID, SUB_ID),
)
print("canonical row before interaction:", dict(row))

# Recreate the cleanup file to store a superseded_results entry for RES2.
import app.classroom.report_cleanup as _cleanup
_cleanup.recompute_superseded_for(result_id=RES2, course_id=COURSE_ID, coursework_id=CW_ID, submission_id=SUB_ID)

# Re-read the canonical row to confirm superseded_results got restored.
row2 = ra.query_one(
    "SELECT canonical_result_id, drive_file_id FROM canonical_reports WHERE course_id = ? AND coursework_id = ? AND submission_id = ?",
    (COURSE_ID, CW_ID, SUB_ID),
)
print("canonical row after recompute_superseded_for:", dict(row2))
