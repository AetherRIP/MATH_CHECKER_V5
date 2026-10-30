# Submission retention — how a paper is kept, shown and deleted

Teacher-facing setting: **Settings → Privacy & Data → Keep original
submissions**. Stored in `data/config.toml` under `[retention]`
(`keep_submissions`, `keep_days`) and read by the backend, never by the
frontend alone.

| `keep_submissions` | Meaning |
|---|---|
| `never` | Nothing is kept; the staged file is deleted when the job ends. |
| `until_complete` | Default. The file survives retries and is deleted when the job finishes. |
| `days` | The submission is copied into `data/retained/<job_id>/` with `retain_until = now + keep_days`. |
| `always` | Same copy, `retain_until = 0` (never expires). |

## What is kept, and which copy is which

A submission reaches a job in one of two ways:

* **Manual upload** (the New correction page). The uploaded bytes are the
  original and are registered in `pending_manual_files`.
* **Classroom**. The attachment is downloaded and normalised (PDF/A-style
  compression) into `data/tmp/job-<id>/submission_*`. That normalised file is
  what the marking models see and the only local copy the job has; there is no
  separate pristine download kept on disk.

When retention is on, `retain_job_submissions()` copies every submission file
the job owns — manual upload and/or staged normalised copy — into
`data/retained/<job_id>/` and records it in `retained_files`
(`job_id, path, kind='submission', filename, retain_until`). Mark schemes and
question papers are never retained: they are shared reference documents, not
personal data.

The retained copy is what the student-work viewer serves
(`GET /api/results/{id}/pages`), so the pages a teacher sees after marking are
the same pages the marker saw. Temporary processing copies under
`data/tmp/job-<id>/` are always deleted; the retained copy lives outside that
tree.

## Deletion paths that must respect retention

1. `cleanup_job_files()` — terminal cleanup (completed, permanently failed,
   cancelled). Retains first, then deletes staging.
2. `sweep_stale_temp()` — startup hygiene for orphaned/terminal job dirs.
   Retains the submission before removing the directory, so a job that never
   reached terminal cleanup (awaiting review, crash) cannot lose a paper the
   teacher asked to keep.
3. `sweep_expired_retained()` — deletes only copies whose `retain_until` has
   passed, and the database row with them.

Both deletion paths refuse to run when retention is on, a submission exists,
and no retained row could be written (`retention_incomplete`): a failed copy
(disk full, permissions, locked file) leaves the input in place and logs a
warning instead of destroying the only copy. The next cleanup or startup sweep
retries it.

## What the viewer reports

`GET /api/results/{id}/pages` answers from the file itself, not from a flag:

| Situation | `available` | `reason` |
|---|---|---|
| Retained file exists and opens | `true` | `null` |
| Retention off, file deleted as intended | `false` | `retention_disabled` |
| Retention on, no stored copy (legacy or removed outside the app) | `false` | `file_missing` |
| Job still running | `false` | `processing` |
| Stored copy exists but no page opens | `false` | `file_unreadable` |
| Result has no job link (defensive) | `false` | `no_link` |

A failed HTTP request is a client-side error state ("could not check"), never a
claim that the file was deleted. Every payload also carries the effective
`retention` block and `checked_at`, so the UI never has to guess from a toggle.

## Known limits

* Papers marked before retention was enabled (or before this lifecycle fix) have
  no retained copy and cannot be recovered from the database; they report
  `file_missing`, which says exactly that.
* Changing the setting does not retroactively keep old papers; it takes effect
  for the next terminal cleanup.
