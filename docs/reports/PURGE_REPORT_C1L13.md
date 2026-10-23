# C1L13 Set — Targeted Data Purge Report

**Operation:** remove all Math Correct data for one assignment ("C1L13 Set").
**Result:** `PURGE OK` — all assignment-scoped rows and files removed; all other
data preserved; nothing touched in Google Classroom or Google Drive.
**Scope:** local Math Correct only. No Classroom/Drive API call was made.

## 1. Assignment identifiers

| | |
|---|---|
| Class (Course) | `877926720424` — *WBIS - O Level Mathematics - Mr. Teacher (redacted)* |
| Assignment (CourseWork) | `889601795010` — *Ch1L13 - Sets* (the user's "C1L13 Set") |

Identification was by **class + title**, not title alone. A homonym exists:
`889601798341` — also *"Ch1L13 - Sets"* but in the separate **TESTING** class
(`886515702341`). It is a *different* assignment and was **left untouched**
(its row and its mark scheme `mark_schemes/889601798341.pdf` survive).

## 2. Tables and storage inspected

SQLite `data/app.db` and the file stores under `data/`
(`mark_schemes/`, `blueprints/`, `tmp/`, `repairs/`, `audit_runs/`, audit JSONs).

## 3. Rows deleted (all keyed to `889601795010`)

| Table | Rows |
|---|---|
| coursework | 1 |
| submissions | 13 |
| correction_jobs | 24 |
| results | 10 |
| result_versions | 37 |
| result_reviews | 0 |
| classroom_submissions | 78 |
| classroom_state_events | 34 |
| classroom_publish_ops | 32 |
| classroom_managed_assignments | 1 |
| report_attachments | 7 |
| canonical_reports | 3 |
| automation_assignments | 1 |
| automation_jobs | 16 |
| automation_log | 4654 |
| large_pdf_jobs | 4 |
| large_pdf_batches | 8 |
| job_bandwidth | 24 |
| job_remote_files | 5 |
| retained_files | 0 |
| pending_manual_files | 0 |
| pending_mark_schemes | 1 |
| grade_boundaries | 0 |
| repair_runs / repair_items | 2 / 21 |
| **Total** | **4976** |

## 4. Jobs / queues cancelled

No correction job was active — all 24 `correction_jobs` were terminal
(19 COMPLETED, 4 CANCELLED, 1 FAILED) and no large-PDF job was in flight.
Removing the rows cleared the queues regardless: **8 non-terminal
`automation_jobs`** (4 NEW, 1 ATTENTION_REQUIRED, 1 CORRECTION_FAILED,
1 REPORT_FAILED) and the `automation_assignments` row that kept the assignment
under automated polling, so no worker/scheduler can recreate work for it.

## 5. Files deleted

* `mark_schemes/889601795010.pdf` (11.3 MB — identical content also held as the
  homonym's `889601798341.pdf`, which is kept) and its previous version
  `mark_schemes/_previous/889601795010-20261007-185353.pdf`.
* `tmp/router_merged/08dd92601038410abaef24ffb230c2b1_student.pdf` (53 MB).
* `repairs/f02c60b92c2f4491884d428d8b542128/` (41 files) and
  `repairs/1aaaa753f3674a0b9e8d7f54f0be2d78/` (18 files) — both repair runs.
* Audit artifacts exclusive to C1L13: `audit_findings.json`,
  `audit_inventory.json`, `audit_consistency.json`, `audit_findings_notes.json`,
  `repair_safety_snapshot.json`, and the whole `audit_runs/` (8 files).
* Two C1L13 files inside the mixed `audit_downloads/` cache (Student A's
  submission scan `1ZnMNQRoCN…` and report `1qIi-8qWNl…`).

**77 files** total.

## 6. Backup (recovery)

`data/purge_backups/20261011_114610_889601795010/` (112 MB) holds a copy of
`app.db` (with every deleted row), the deleted files under `files/`, and
`manifest.json` (counts, coursework row, file list).

## 7. Verification (independent)

* Direct SQLite: **every** table returns 0 rows referencing `889601795010`; no
  `coursework_id`/`id` anywhere equals the target.
* Filesystem: the mark-scheme file and the repair/audit artifacts are gone; the
  homonym's mark scheme remains.
* App read layer (in-process, no server): `repair.load_summary()` returns
  empty runs/items; `sync._active_coursework(877926720424)` **no longer lists**
  "Ch1L13 - Sets"; the audit findings file no longer loads; 0 results, 0
  tracking rows, 0 publish ops for the target.
* Reference assignment `889601798341` unchanged; the class's other **143**
  results still load; other assignments' tracking/publish rows intact.

## 8. Deliberately NOT deleted (and why)

* **Shared resources:** `blueprints/1428ebc6…json` (mark-scheme blueprint keyed
  by content hash, shared with the homonym), `mark_schemes/889601798341.pdf`,
  the `students` table, and global `net_usage` / `gemini_keys` / `key_stats`.
* **Mixed audit caches:** remaining `audit_downloads/` (12 files: Ch1L9/Ch1L10),
  `audit_results/`, `audit_reports/`, and `audit_applied.json` — these belong to
  other assignments or are not uniquely attributable; left per the
  "ambiguous → leave and report" rule. (`Student J__1Roaia1r2R__…` is a
  non-C1L13 attachment and was correctly kept.)
* **Google Drive report PDFs** — 7 generated reports plus 1 previous version,
  **kept deliberately**. Drive file ids:
  `1o5Rc9CcteXmQTcPqBqr8FCCi6HywEPDm`,
  `1qIi-8qWNlrcyyPZ1Mk0IQ90mfvifzNsn`,
  `1Ap1lzYwpOyWnBYpddeSHA3qsynubVV9e`,
  `14J_xN4QOSTSc5r55_awwl_6F_XBAp9T8` (+ previous `1stCMjDzVZUPHbuwYZUahm9pJ_yl8OoPM`),
  `1a9D1f6w7lcA0X9hu_RfxZ1h0ZuYQEuai`,
  `1OGj0yyk5Mu6dx7_N5F-O779fdWMJ4_f2`,
  `1vLnlxr1cuJL9tEzE9T0sdo7t7rybMgwY`.
  A read-only Drive/Classroom check (2026-10-11) proved each file is **untrashed
  and still attached** to the student's Classroom submission (state RETURNED),
  and that each is **owned by the student's own Google account**
  (e.g. `<redacted-email>`, `<redacted-email>`), not
  the connected teacher account `<redacted-email>`. Deleting an
  attached report is exactly the bug `report_cleanup.py` exists to prevent; the
  testing-only guard and the app's cleanup rules both refuse it, and a forced
  delete would likely return 403 (not our file) and, if it succeeded, would
  break the seven students' report links. **Decision: keep all 8 files.**
* **Google Classroom** — the real assignment, student submissions and live
  grades were never modified; no Classroom/Drive API call was made.
* **`data/logs/`** app logs and the root `reports/` preview folders.

## 9. Re-import caveat

The assignment still exists in Google Classroom, and `sync_coursework()`
re-imports **all** Classroom assignments. A future Classroom sync will therefore
re-create the *empty* CourseWork row "Ch1L13 - Sets"; it will **not** restore any
corrections, results, marks or reports (those rows are gone). It is not under
automation and will not be processed unless re-enabled.

## 10. Tooling

`backend/scripts/purge_assignment.py` — targeted, backed-up, self-verifying
purge for any coursework id (`--coursework-id … --confirm-purge … [--dry-run]
[--reference-id …]`). Run with the application stopped.
