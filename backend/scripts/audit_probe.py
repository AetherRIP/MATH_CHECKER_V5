"""Temporary reconnaissance: what does the local DB know about Ch1L13 - Sets?

Usage: python scripts/audit_probe.py [db] [coursework_id ...]
Student identity is anonymised in output (surname-free initials + hash).
"""
import hashlib
import json
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "data/app.db"
CW = sys.argv[2:] or ["889601795010"]

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def anon(name):
    name = name or "?"
    tok = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"S-{tok}"


for cid in CW:
    print("=" * 72)
    print("COURSEWORK", cid)
    row = conn.execute(
        "select id, course_id, title, state, due_date from coursework where id=?",
        (cid,),
    ).fetchone()
    print("  row:", dict(row) if row else None)
    subs = list(
        conn.execute(
            "select * from classroom_submissions where coursework_id=?", (cid,)
        )
    )
    print("  classroom_submissions:", len(subs))
    for s in subs:
        try:
            att = json.loads(s["attachments_json"] or "[]")
        except Exception:
            att = []
        print(
            "   -", anon(s["student_name"]),
            "| sid=", (s["student_id"] or "")[-4:],
            "| state=", s["current_state"],
            "| late=", s["late"],
            "| grade=", s["assigned_grade"],
            "| max=", s["max_points"],
            "| atts=", len(att),
        )
        for a in att:
            if isinstance(a, dict):
                keep = {k: v for k, v in a.items()
                        if k in ("driveFile", "title", "type", "id", "extension")}
                print("        att:", json.dumps(keep)[:300])
            else:
                print("        att:", str(a)[:300])
    su = list(
        conn.execute(
            "select id,user_id,state,late,attachment_ids from submissions where coursework_id=?",
            (cid,),
        )
    )
    print("  submissions rows:", len(su))
    for r in su:
        print("   *", dict(r))

print("=" * 72)
print("LARGE PDF JOBS")
for r in conn.execute(
    "select id,submission_id,coursework_id,student_name,page_count,batch_count,"
    "status,step_detail,error,error_code,student_pdf_path,mark_scheme_path,created_at "
    "from large_pdf_jobs"
):
    print("  ", dict(r))

print("=" * 72)
print("CORRECTION JOBS")
for r in conn.execute(
    "select id,submission_id,coursework_id,status,stage_detail,progress,error,created_at "
    "from correction_jobs where coursework_id in (%s)"
    % ",".join("?" * len(CW)),
    CW,
):
    print("  ", dict(r))

print("=" * 72)
print("RESULTS")
for r in conn.execute(
    "select id,job_id,submission_id,coursework_id,student_name,earned,available,"
    "percentage,provisional_grade,review_required,created_at from results "
    "where coursework_id in (%s)" % ",".join("?" * len(CW)),
    CW,
):
    d = dict(r)
    d["student_name"] = anon(d["student_name"])
    print("  ", d)

print("=" * 72)
print("MARK SCHEME / retained files / pending_mark_schemes")
for t in ("retained_files", "pending_mark_schemes", "gemini_remote_files", "job_remote_files"):
    try:
        cols = [d[1] for d in conn.execute("PRAGMA table_info(%s)" % t)]
        print(t, "cols:", cols)
        n = conn.execute("select count(*) from %s" % t).fetchone()[0]
        print("   count:", n)
        for r in conn.execute("select * from %s limit 6" % t):
            d = dict(r)
            blob = json.dumps(d)
            print("   ", blob[:280])
    except Exception as e:
        print(t, "ERR", e)
