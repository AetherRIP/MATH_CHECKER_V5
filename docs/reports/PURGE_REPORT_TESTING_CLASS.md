# TESTING Class — Targeted Data Purge Report

**Operation:** remove all Math Correct data for the whole **TESTING** class.
**Result:** `PURGE OK` — all 186 course-scoped rows and 4 mark-scheme files
removed, plus one now-orphaned mark-scheme blueprint; every other class and all
shared/global data preserved. Nothing touched in Google Classroom or Drive.
**Tool:** `backend/scripts/purge_course.py` (run with the app stopped).

## 1. Target class

| | |
|---|---|
| Course id | `886515702341` |
| Name | *TESTING* (section `12`, enrollment code `vrwl3bc3`) |
| Coursework | 6: `826552183997` CA1 LO7 · `873785733817` Chapter 1 Lesson 7 · `886515667028` Math Chapter 1 lesson 1 · `888962463788` Chapter 1 Lesson 5 · `889319812809` Lol 5 · `889601798341` Ch1L13 - Sets |

Note: `889601798341` ("Ch1L13 - Sets") is the homonym of the earlier C1L13
assignment in the O Level Math class and is part of this class — it is removed
here too.

## 2. Rows deleted (186 total)

| Table | Rows |
|---|---|
| courses | 1 |
| coursework | 6 |
| submissions | 5 |
| correction_jobs | 9 |
| results | 5 |
| result_versions | 8 |
| classroom_submissions | 10 |
| classroom_state_events | 8 |
| classroom_publish_ops | 13 |
| classroom_managed_assignments | 6 |
| report_attachments | 5 |
| automation_assignments | 2 |
| automation_jobs | 4 |
| automation_log | 90 |
| pending_mark_schemes | 4 |
| job_bandwidth | 8 |
| job_remote_files | 2 |
| **Total** | **186** |

All correction jobs were terminal (9 COMPLETED); the 4 automation jobs were
COMPLETE and the 2 automation selections (one of which had `enabled=1`) were
removed, so the scheduler can no longer poll this class.

## 3. Files deleted (4 + 1 blueprint)

* `mark_schemes/826552183997.pdf`, `mark_schemes/873785733817.pdf`,
  `mark_schemes/889319812809.pdf`, `mark_schemes/889601798341.pdf`.
* `blueprints/1428ebc6…json` — the Ch1L13 mark-scheme blueprint, deleted only
  because no remaining mark scheme shares that content hash.

**Kept reliance check:** blueprints `6a01cbbc…` and `abdb24dd…` were **retained**
because those mark-scheme hashes are still used by assignments in the remaining
O Level Math class.

## 4. Backup (recovery)

`data/purge_backups/20261011_121813_course_886515702341/` — a copy of `app.db`
(with the deleted rows), the deleted files under `files/`, and `manifest.json`.

## 5. Verification (independent)

* Direct SQLite: **0** rows anywhere reference course `886515702341`.
* Remaining state: only one course (`877926720424` *WBIS - O Level Mathematics*)
  and its **16** coursework survive; the shared blueprints are intact.
* App read layer (in-process): the courses list contains only the O Level Math
  class, and `sync._active_coursework('886515702341')` returns `[]`.
* TESTING mark-scheme files and the orphaned blueprint are gone.

## 6. Deliberately NOT deleted (and why)

* **Google Classroom** — the real TESTING class, its coursework, submissions and
  grades were not modified; no Classroom/Drive API call was made.
* **Google Drive report files** — 3 files from this class remain in Drive (not
  deleted; see below), the same caution applied to the earlier C1L13 report
  deletion: an attached report is a live reference and the app's own rules
  refuse to delete it.
  * `1UkGsZZX4epG89XXMg5d2VmFCiWwGfS5F` (Student / Chapter 1 Lesson 7 — status FAILED)
  * `1iX5rPjrLs17gVhJwoApwayobt_w-na3Z` (Shino Asada / CA1 LO7 — RETAINED)
  * `1BQKz4y2JbrJm10TXo68gryZjR-4w8qzE` (Yassin Elgarhy / CA1 LO7 — RETAINED)
* **Shared / global resources** — the `students` table, `net_usage`,
  `gemini_keys`/`key_stats`, `gemini_remote_files`, and shared mark-scheme
  blueprints.
* **`data/logs/`, root `reports/` previews, `tmp/rep_*.pdf`** — app-level
  artifacts, not class-specific.

## 7. Re-import caveat

The TESTING class still exists in Google Classroom, and `sync_coursework()`
re-imports all Classroom assignments. A future sync will re-create the *empty*
course and its coursework titles; it will **not** restore any corrections,
results, marks or reports (those rows are gone).

## 8. Tooling

`backend/scripts/purge_course.py` — targeted, backed-up, self-verifying purge for
any course id (`--course-id … --confirm-purge … [--dry-run] [--no-backup]`).
Companion to `backend/scripts/purge_assignment.py`.
