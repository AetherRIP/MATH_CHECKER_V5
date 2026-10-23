# Math Checker 5.2.1 — build 20261013-rc1

## Release candidate, not live-Classroom certification

The portable application and installer are built from the audited source.
Local tests use synthetic students and mocked external services. Live Gemini
mathematical accuracy and a full Google Classroom workflow require an authorized
teacher, a designated test class and provider quota before production handover.
Multi-School is Work in Progress; its implementation/data are preserved.

## Launch and initialize

1. Extract the entire MathChecker_Portable.zip into a writable folder. Do not
   copy individual files. Run MathChecker.bat; Stop.bat stops this installation.
2. Portable data lives in Data beside the application. Installed data lives in
   %LOCALAPPDATA%\MathChecker. Never distribute your populated Data directory.
3. Complete first-time setup. Configure a host account/password in Settings.
   Local first-account setup is available before an account exists; after setup
   protected API operations require sign-in. Do not expose a fresh, unconfigured
   installation to the internet.
4. Add the teacher's Gemini keys through AI → API & providers. Supply your own
   OAuth client through the documented data/secrets configuration; use Google
   Classroom → Connect, then sync. Keys/tokens must never be sent to support.
5. Keep automatic grade return off/review mode on during the designated-class
   acceptance test. External coursework may not permit write-back: Google allows
   only the creating integration to grade/return certain coursework.

## Correction and review

- Assignments: upload the matching original mark scheme; correct eligible
  submissions. Correct a PDF supports local manual work. Task Manager shows the
  correction states; Results shows the correction.
- A mathematical disagreement is a recommendation, not proof merely because a
  model reports high confidence. Conflicting awards and ambiguous cases go to
  Review Queue. Mark-Scheme Review documents evidence and human decisions.
- Bulk proposals do not change grades. A teacher/developer must inspect the
  scope and explicitly confirm approval for that batch. Assistants cannot use
  these global approval endpoints. Rejection leaves grades untouched.
- Retry failed items only resumes failed local batch items. Successful items are
  not re-applied; stale/teacher-overridden results require individual review.
- Optional bulk Classroom return uses the existing grade 1–9 conversion and
  checks the assigned grade and RETURNED state. A write failure does not undo a
  locally approved correction. Inspect Classroom before retrying uncertain
  external writes; there is no dedicated bulk publish-retry UI in this build.

## Recovery, reports and updates

- Correction, report generation, attachment, grade write and return are separate
  outcomes. Check job detail, not a generic completion badge, for delivery.
- Retry a failed report/grade stage rather than re-running completed correction.
  Canonical report replacement updates the app-owned Drive file; do not remove
  unrelated teacher/student attachments. Unsupported operations remain blocked.
- Automation → Clear activity hides feed entries only. Stored audit history,
  pending jobs, correction records and grades are retained.
- Before updating, stop the app and back up Data (or the installed user-data
  folder). Replace application files, preserving the user's data folder. Never
  copy the developer's data/configuration into a recipient installation.
- Use Privacy & Data diagnostic export for redacted diagnostics. Never share
  raw databases, secrets, bootstrap passwords or student reports with support.

## Certifying live marking accuracy (opt-in)

The automated verifications run the app in Demo mode, where the AI engine is
forced into mock mode: they prove packaging, launch, the correction pipeline and
uninstall, but nothing about whether the marks are right. Marking accuracy is
certified separately, deliberately, with a real key:

```bat
python scripts\verify_live_marking.py --live
```

- It is OPT-IN. Without `--live` it sends nothing and exits 2.
- It marks a real PDF against a real mark scheme with the key the app is already
  configured with (or `--key-file`), in a throwaway data folder with Testing-only
  mode ON, so your classes, database and keys stay untouched and nothing can be
  published to Google during a certification run.
- It reports the outcome (model, timings, every question's marks and reason,
  total, grade) and compares the marks with ground truth: `--expected` for your
  own papers, or the built-in answer key for the bundled fixture quiz, whose
  correct marks follow from how the fixture is built.
- Exit codes: 0 certified, 1 the run failed, 2 refused (no `--live`/no key),
  3 marked but NOT certified, 4 reported without ground truth. Only a comparison
  can certify, so a run with no ground truth never returns 0.
- `--tolerance N` accepts N marks of slack per question; the report says so,
  because slack weakens the claim. The evidence lands in `docs/logs/`.

For a real paper, write the ground truth yourself: `{"questions": {"1": 3,
"2": 2}, "total": 9}` — human-verified marks for that student's paper against
that mark scheme. Without it the run reports the model's marks but must not be
quoted as a certification.

## Limitations requiring acceptance before real-world use

- Live Classroom/OAuth and quota failover have not been certified by this
  release pass. Marking accuracy is certified only by an OPERATOR-run
  `verify_live_marking.py --live` pass; a default test run stays mocked, so
  treat a build with no such run on record as uncertified.
- Multi-school/class scoping across all legacy endpoints needs a complete
  multi-user security audit; WIP is not certification of tenant isolation.
- The mark-scheme verification is reasoning from the current marking pass, not
  an independent symbolic proof engine or separately verified second pass.
- Bulk grade changes preserve original/current versions, but report attachment
  regeneration after a bulk change is not automatic. Check/regenerate affected
  reports deliberately before sharing them.
- Installer compilation is not proof of clean install/upgrade behavior; consult
  the workspace release report for what was actually exercised.
